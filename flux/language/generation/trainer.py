"""
flux/language/generation/trainer.py

Training loop for FluxLanguageModel on (structured_input, gold_output) pairs.

Data format (logs/generation/train_gold.jsonl):
  {"concept_id": "entropy", "input": "concept: entropy\\ntype: CONCEPT\\n...", "output": "Entropy is..."}

Training:
  - Teacher forcing: given the gold output tokens, predict the next token at each step
  - Loss: cross-entropy (ignoring [PAD] tokens)
  - Optimizer: AdamW with linear warmup
  - Early stopping: stop when val_loss hasn't improved for `patience` epochs
  - Memory cache: NodeEncoder encodes all records ONCE before the loop (frozen encoder)

Usage:
  python -m flux.language.generation.trainer
  python -m flux.language.generation.trainer --epochs 500 --lr 3e-4
"""
from __future__ import annotations

import argparse
import json
import logging
import random
from pathlib import Path

import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import LambdaLR

from flux.language.generation.vocab_builder import (
    VOCAB_PATH, PAD_ID, BOS_ID, EOS_ID,
    build_vocab, save_vocab, load_vocab,
    encode as vocab_encode,
)
from flux.language.generation.training_data import load_gold, GOLD_PATH
from flux.language.generation.flux_language_model import (
    FluxLanguageModel, encode_kg_node_as_memory, CKPT_PATH,
)

log = logging.getLogger(__name__)

TRAIN_LOG = Path("logs/generation/train_log.jsonl")


# ── Data helpers ──────────────────────────────────────────────────────────────

def _parse_input_fields(input_text: str) -> dict[str, str]:
    fields = {}
    for line in input_text.strip().split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k and v:
                fields[k] = v
    return fields


def precompute_memories(
    records: list[dict],
    encode_fn,         # ignored — NodeEncoder is loaded inside encode_kg_node_as_memory
    device: torch.device,
) -> list[torch.Tensor]:
    """
    Encode all records with the frozen NodeEncoder — done ONCE before the loop.
    NodeEncoder is frozen: same input → same output every time.
    Pre-caching gives a 300× speedup vs re-encoding every epoch.
    """
    log.info("Pre-encoding %d records with frozen NodeEncoder (one-time)...", len(records))
    memories = []
    for i, rec in enumerate(records):
        fields = _parse_input_fields(rec["input"])
        mem = encode_kg_node_as_memory(fields, None, device)
        memories.append(mem)
        if (i + 1) % 10 == 0:
            log.info("  Encoded %d/%d", i + 1, len(records))
    log.info("Pre-encoding complete: %d memory tensors cached", len(memories))
    return memories


def make_batch(
    indices: list[int],
    memories: list[torch.Tensor],
    records: list[dict],
    vocab: dict[str, int],
    device: torch.device,
    max_out_len: int = 200,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Build a training batch from pre-cached memory tensors.
    Pure tensor indexing — no encoder calls.
    Returns: (memory_batch, tgt_in, tgt_out)
    """
    batch_mems = [memories[i] for i in indices]
    batch_recs = [records[i] for i in indices]

    # Pad memory to same N_fields
    max_fields = max(m.size(0) for m in batch_mems)
    mem_dim    = batch_mems[0].size(-1)
    padded_mem = torch.zeros(len(indices), max_fields, mem_dim, device=device)
    for k, m in enumerate(batch_mems):
        padded_mem[k, :m.size(0)] = m

    # Encode output tokens
    out_ids_list = [
        vocab_encode(rec["output"], vocab)[:max_out_len]
        for rec in batch_recs
    ]
    max_T = max(len(ids) for ids in out_ids_list)

    tgt_in  = torch.full((len(indices), max_T + 1), PAD_ID, dtype=torch.long, device=device)
    tgt_out = torch.full((len(indices), max_T + 1), PAD_ID, dtype=torch.long, device=device)
    for k, ids in enumerate(out_ids_list):
        tgt_in [k, 0]            = BOS_ID
        tgt_in [k, 1:len(ids)+1] = torch.tensor(ids, dtype=torch.long)
        tgt_out[k, :len(ids)]    = torch.tensor(ids, dtype=torch.long)
        tgt_out[k, len(ids)]     = EOS_ID

    return padded_mem, tgt_in, tgt_out


# ── Training loop ─────────────────────────────────────────────────────────────

def train(
    epochs:       int   = 500,
    lr:           float = 3e-4,
    batch_size:   int   = 8,
    warmup_steps: int   = 20,
    val_split:    float = 0.15,
    patience:     int   = 30,    # stop when val_loss hasn't improved for this many epochs
    d_model:      int   = 64,    # small model for small data (26 pairs → ~200K params)
    n_heads:      int   = 2,
    n_layers:     int   = 2,
    dim_ff:       int   = 128,
    device_str:   str   = "auto",
    gold_path:    Path  = GOLD_PATH,
    ckpt_path:    Path  = CKPT_PATH,
) -> dict:
    """
    Train FluxLanguageModel with early stopping.

    Requires logs/generation/train_gold.jsonl with ≥5 pairs.
    Default architecture: 2-layer 64-dim (~200K params) — correct size for 26 training pairs.
    A 4-layer 128-dim (2M params) overfits in <20 epochs with this data.
    """
    # Device
    device = (
        torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device_str == "auto" else torch.device(device_str)
    )
    log.info("Training on: %s", device)

    # ── Merge any [CORRECT_RESPONSE] blocks from the interaction log ──────────
    # This runs before load_gold so corrections captured during normal Flux
    # conversations are automatically included in this training run.
    try:
        from flux.language.generation.training_data import merge_corrections_from_log
        merge_result = merge_corrections_from_log(gold_path=gold_path)
        if merge_result["merged"]:
            log.info(
                "Auto-merged %d correction(s) from interaction log into %s",
                merge_result["merged"], gold_path,
            )
    except Exception as _merge_exc:
        log.debug("merge_corrections_from_log skipped: %s", _merge_exc)

    # Data
    gold = load_gold(gold_path)
    if len(gold) < 5:
        raise RuntimeError(
            f"Need ≥5 gold pairs in {gold_path}. Found {len(gold)}."
        )
    random.shuffle(gold)
    n_val   = max(1, int(len(gold) * val_split))
    val_set = gold[:n_val]
    trn_set = gold[n_val:]
    log.info("Training: %d  |  Validation: %d", len(trn_set), len(val_set))

    # Vocabulary — rebuilt each run including gold outputs (zero OOV guarantee)
    log.info("Building vocabulary (KG corpus + gold outputs)...")
    import sys as _sys; _sys.path.insert(0, ".")
    from flux.kg.store import get_store as _get_store
    _store    = _get_store()
    gold_txts = [r["output"] for r in gold]
    vocab     = build_vocab(_store.all_nodes(), gold_texts=gold_txts, max_vocab=5000)
    save_vocab(vocab)
    log.info("Vocab: %d tokens (zero OOV in gold outputs)", len(vocab))

    # Model
    def _noop(t):
        return None   # NodeEncoder loaded lazily inside encode_kg_node_as_memory

    model = FluxLanguageModel(
        vocab         = vocab,
        ltc_encode_fn = _noop,
        d_model       = d_model,
        n_heads       = n_heads,
        n_layers      = n_layers,
        dim_ff        = dim_ff,
        dropout       = 0.1,
        max_len       = 256,
    ).to(device)
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log.info("FluxLanguageModel: %d parameters  (d=%d, L=%d)", n_params, d_model, n_layers)

    # Pre-encode all inputs ONCE with frozen NodeEncoder
    all_data     = trn_set + val_set
    all_memories = precompute_memories(all_data, _noop, device)
    trn_memories = all_memories[:len(trn_set)]
    val_memories = all_memories[len(trn_set):]

    # Optimizer + scheduler
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=1e-4)

    def _warmup(step: int) -> float:
        if step < warmup_steps:
            return step / max(1, warmup_steps)
        return max(0.1, 1.0 - (step - warmup_steps) / max(1, epochs - warmup_steps))

    scheduler = LambdaLR(optimizer, _warmup)
    criterion = nn.CrossEntropyLoss(ignore_index=PAD_ID)

    # Training
    best_val   = float("inf")
    best_epoch = 0
    no_improve = 0
    train_log  = []
    trn_idx    = list(range(len(trn_set)))

    TRAIN_LOG.parent.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, epochs + 1):
        # ── Train ──
        model.train()
        random.shuffle(trn_idx)
        ep_loss, n_bat = 0.0, 0

        for i in range(0, len(trn_idx), batch_size):
            idx = trn_idx[i : i + batch_size]
            mem, t_in, t_out = make_batch(idx, trn_memories, trn_set, vocab, device)
            logits = model(mem, t_in)
            B, T, V = logits.shape
            loss = criterion(logits.view(B * T, V), t_out.view(B * T))
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            ep_loss += loss.item()
            n_bat   += 1

        avg_trn = ep_loss / max(1, n_bat)

        # ── Validate ──
        model.eval()
        vl_loss, vl_bat = 0.0, 0
        with torch.no_grad():
            val_idx = list(range(len(val_set)))
            for i in range(0, len(val_idx), batch_size):
                idx = val_idx[i : i + batch_size]
                mem, t_in, t_out = make_batch(idx, val_memories, val_set, vocab, device)
                logits = model(mem, t_in)
                B, T, V = logits.shape
                vl_loss += criterion(logits.view(B * T, V), t_out.view(B * T)).item()
                vl_bat  += 1
        avg_val = vl_loss / max(1, vl_bat)

        if epoch % 10 == 0 or epoch == 1:
            log.info(
                "Epoch %3d/%d | train=%.4f | val=%.4f | lr=%.2e | patience=%d/%d",
                epoch, epochs, avg_trn, avg_val,
                scheduler.get_last_lr()[0], no_improve, patience,
            )

        # Early stopping
        if avg_val < best_val:
            best_val   = avg_val
            best_epoch = epoch
            no_improve = 0
            model.save(ckpt_path)
        else:
            no_improve += 1
            if no_improve >= patience:
                log.info(
                    "Early stopping at epoch %d — best val=%.4f at epoch %d",
                    epoch, best_val, best_epoch,
                )
                break

        train_log.append({"epoch": epoch, "train": avg_trn, "val": avg_val})

    # Save log
    with TRAIN_LOG.open("w", encoding="utf-8") as f:
        for r in train_log:
            f.write(json.dumps(r) + "\n")

    log.info("Done. Best val=%.4f at epoch %d → %s", best_val, best_epoch, ckpt_path)
    return {"best_val_loss": best_val, "best_epoch": best_epoch, "n_train": len(trn_set)}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(name)s | %(message)s")

    parser = argparse.ArgumentParser(description="Train FluxLanguageModel")
    parser.add_argument("--epochs",     type=int,   default=500)
    parser.add_argument("--lr",         type=float, default=3e-4)
    parser.add_argument("--batch-size", type=int,   default=8)
    parser.add_argument("--patience",   type=int,   default=30)
    parser.add_argument("--d-model",    type=int,   default=64)
    parser.add_argument("--n-layers",   type=int,   default=2)
    parser.add_argument("--device",     type=str,   default="auto")
    args = parser.parse_args()

    import sys
    sys.path.insert(0, ".")

    result = train(
        epochs     = args.epochs,
        lr         = args.lr,
        batch_size = args.batch_size,
        patience   = args.patience,
        d_model    = args.d_model,
        n_layers   = args.n_layers,
        device_str = args.device,
    )
    print(f"\nResult: {result}")
