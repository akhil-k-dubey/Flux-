"""
flux/manifold/training/finetune_flm_phase3.py

Fine-tunes FluxLanguageModel to read query + KG subgraph context and generate responses.

The FLM architecture already has cross-attention to a memory sequence (N_fields × 128).
Phase 3 replaces that single-node memory with a multi-concept subgraph memory:

  Current:  memory = LTC(single_KG_node_fields)  → (N_fields, 128)
  Phase 3:  memory = [query_vec | concept_1_vec | concept_2_vec | ...]  → (M, 128)

Both are (M, 128). The decoder's cross-attention sees richer context in Phase 3.
The query vector is encoded using the same KGNodeEncoder (treats query as a "KG field").
Concept vectors are encoded from their definition + edge text.

Training:
  - Load Phase 3 corpus from logs/flux_phase3_corpus.jsonl
  - For each record: encode (query + context) as memory, encode response as target
  - Fine-tune only the decoder layers (mem_proj is also updated)
  - KGNodeEncoder stays FROZEN (same as pre-Phase3 FLM training)

Run: python3 -m flux.manifold.training.finetune_flm_phase3
"""
from __future__ import annotations
import json, logging, sys
from pathlib import Path
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
log = logging.getLogger(__name__)

import torch
import torch.nn as nn

from flux.language.generation.flux_language_model import (
    FluxLanguageModel, CKPT_PATH, LTC_DIM,
)
from flux.language.generation.vocab_builder import (
    load_vocab, encode as vocab_encode, BOS_ID, EOS_ID, PAD_ID,
)

CORPUS_PATH = Path("logs/flux_phase3_corpus.jsonl")
OUT_PATH    = Path("checkpoints/generation/flux_lm_phase3.pt")
MAX_RESP    = 120   # max response tokens
MAX_MEM     = 12    # max memory slots (query + concepts)


# ── Memory encoder ─────────────────────────────────────────────────────────────

def _get_node_encoder():
    """Load the frozen KGNodeEncoder used by the base FLM."""
    from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder
    enc = KGNodeEncoder()
    for p in [
        Path("checkpoints/dual_encoder/node_enc_phase2_best.pt"),
        Path("checkpoints/dual_encoder/node_enc_phase1.pt"),
    ]:
        if p.exists():
            state = torch.load(p, map_location="cpu", weights_only=True)
            sd = state["state_dict"] if "state_dict" in state else state
            enc.load_state_dict(sd)
            break
    enc.eval()
    for param in enc.parameters():
        param.requires_grad = False
    log.info("KGNodeEncoder loaded and frozen")
    return enc


def _text_to_char_ids(text: str, char_to_idx: dict, max_chars: int = 256) -> torch.Tensor:
    ids = [char_to_idx.get(c, 1) for c in text[:max_chars]]
    return torch.tensor(ids, dtype=torch.long)


def encode_text_to_memory(text: str, node_enc, char_to_idx: dict, device) -> torch.Tensor:
    """Encode a text string as a single memory slot [1, LTC_DIM=128]."""
    char_ids = _text_to_char_ids(text, char_to_idx).to(device)
    with torch.no_grad():
        vec = node_enc(char_ids.unsqueeze(0))   # (1, 128)
    return vec.to(device)


def build_memory(query: str, context_lines: list[str],
                 node_enc, char_to_idx: dict, device, max_slots=MAX_MEM) -> torch.Tensor:
    """[query_vec, line1_vec, ...] → (1, M, LTC_DIM)"""
    slots = [encode_text_to_memory(query, node_enc, char_to_idx, device)]
    for line in context_lines[:max_slots - 1]:
        if line.strip():
            slots.append(encode_text_to_memory(line, node_enc, char_to_idx, device))
    mem = torch.cat(slots, dim=0)    # (M, 128)
    return mem.unsqueeze(0)          # (1, M, 128)



# ── Dataset ────────────────────────────────────────────────────────────────────

def load_corpus():
    if not CORPUS_PATH.exists():
        log.error("Corpus not found. Run train_phase3.py first.")
        return []
    records = []
    with open(CORPUS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    log.info("Loaded %d training records from corpus", len(records))
    return records


# ── Fine-tuning ────────────────────────────────────────────────────────────────

def finetune(epochs: int = 30, lr: float = 5e-4, batch_size: int = 1):
    """
    Fine-tune the FLM on Phase 3 corpus.
    Batch size 1 because each example has a different memory size.
    """
    vocab = load_vocab()
    if not vocab:
        log.error("Vocab not found.")
        return

    records = load_corpus()
    if not records:
        return

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("Device: %s", device)

    # Load base FLM
    if not CKPT_PATH.exists():
        log.error("Base FLM checkpoint not found at %s", CKPT_PATH)
        return

    # We need a dummy ltc_encode_fn since we'll build memory directly
    model = FluxLanguageModel.load(
        ltc_encode_fn=lambda x: None,
        path=CKPT_PATH,
        device=device,
    )

    # Load KGNodeEncoder for memory building
    try:
        node_enc = _get_node_encoder().to(device)
        import json as _json
        from flux.language.dual_encoder.kg_node_encoder import VOCAB_PATH
        _vocab_data = _json.loads(VOCAB_PATH.read_text())
        char_to_idx = _vocab_data["char_to_idx"]
    except Exception as exc:
        log.error("KGNodeEncoder unavailable (%s) — cannot build memory", exc)
        return

    # Fine-tune only the decoder + mem_proj (not the node encoder)
    trainable = list(model.decoder.parameters()) + list(model.mem_proj.parameters()) + \
                list(model.output_proj.parameters())
    opt   = torch.optim.AdamW(trainable, lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    best_loss = float("inf")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    for ep in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        n_batches  = 0

        import random
        random.shuffle(records)

        for rec in records:
            query   = rec["query"]
            context = rec["context"]
            resp    = rec["response"]

            # Build memory: query + serialized context lines
            ctx_lines = [l for l in context.split("\n") if l.strip()]
            memory = build_memory(query, ctx_lines, node_enc, char_to_idx, device)  # (1, M, 128)


            # Encode response as token sequence
            resp_ids = vocab_encode(resp, vocab)
            if not resp_ids:
                continue
            resp_ids = [BOS_ID] + resp_ids[:MAX_RESP] + [EOS_ID]
            tgt = torch.tensor([resp_ids], dtype=torch.long, device=device)  # (1, T)

            # Forward: teacher forcing (input = tgt[:-1], target = tgt[1:])
            logits = model(memory, tgt[:, :-1])   # (1, T-1, vocab_size)
            loss   = nn.functional.cross_entropy(
                logits.reshape(-1, logits.size(-1)),
                tgt[:, 1:].reshape(-1),
                ignore_index=PAD_ID,
            )

            if loss.isnan():
                continue

            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(trainable, 1.0)
            opt.step()

            total_loss += loss.item()
            n_batches  += 1

        sched.step()
        avg = total_loss / max(n_batches, 1)

        if avg < best_loss:
            best_loss = avg
            model.save(OUT_PATH)

        if ep % 5 == 0 or ep == 1:
            log.info("[Epoch %2d/%d] loss=%.4f  best=%.4f", ep, epochs, avg, best_loss)

    log.info("FLM Phase 3 fine-tuning done. Best loss: %.4f  Saved: %s", best_loss, OUT_PATH)


if __name__ == "__main__":
    finetune()
