"""
flux/language/generation/flux_language_model.py

FluxLanguageModel: frozen LTC encoder + 4-layer causal Transformer decoder.

Architecture:
  Encoder  — your existing frozen LTC, encodes each KG field separately
             to produce a (N_fields, 128) memory sequence (not a single vector).
             Cross-attention has real structure to attend over.

  Decoder  — 4-layer causal Transformer with:
             • Word-level token embedding (Flux vocab, ~3,500 tokens)
             • Sinusoidal positional encoding
             • TransformerDecoder with cross-attention to LTC memory
             • Linear output projection → logits over vocab

  At inference: autoregressive word-by-word generation, greedy or temperature
                sampling. Stops at [EOS] or max_words.

Why frozen LTC:
  The LTC already encodes Flux's vocabulary well (validated at 0.77 cosine
  on same-concept query→definition pairs). Freezing it means:
    1. No LTC training instability (the problem this whole effort started with)
    2. Faster training — only the 4-layer decoder updates
    3. The encoder stays consistent between retrieval and generation

Multi-field memory:
  Instead of one 128-dim vector (which gives the decoder no structure to attend
  over), each KG field (definition, examples, connections, requires) is encoded
  separately → stacked into (N_fields, 128). The decoder learns to attend to the
  definition slot when writing the opening sentence, examples when writing
  illustrations, etc. This is real cross-attention, not FiLM conditioning.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

from flux.language.generation.vocab_builder import (
    VOCAB_PATH, PAD_ID, UNK_ID, BOS_ID, EOS_ID,
    encode as vocab_encode, load_vocab, _tokenize,
)

log = logging.getLogger(__name__)

CKPT_PATH = Path("checkpoints/generation/flux_lm_best.pt")
LTC_DIM   = 128   # encoder output dimension (NodeEncoder also outputs 128)


# ── Positional Encoding ──────────────────────────────────────────────────────

class SinusoidalPE(nn.Module):
    """Standard sinusoidal positional encoding."""

    def __init__(self, d_model: int, max_len: int = 512) -> None:
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(max_len).unsqueeze(1).float()
        div = torch.exp(
            torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0))  # (1, max_len, d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, d_model)
        return x + self.pe[:, : x.size(1)]


# ── Multi-field LTC encoder ──────────────────────────────────────────────────

def _get_node_encoder():
    """Lazy-load KGNodeEncoder (dual encoder). Cached as module-level singleton."""
    global _NODE_ENC
    if _NODE_ENC is None:
        try:
            from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder
            import torch as _torch
            enc = KGNodeEncoder()
            ckpt_p = Path("checkpoints/dual_encoder/node_enc_phase2_best.pt")
            if not ckpt_p.exists():
                ckpt_p = Path("checkpoints/dual_encoder/node_enc_phase1.pt")
            state = _torch.load(ckpt_p, map_location="cpu")
            # Checkpoint stores state under "state_dict" key
            sd = state["state_dict"] if "state_dict" in state else state
            enc.load_state_dict(sd)
            enc.eval()
            _NODE_ENC = enc
            log.info("KGNodeEncoder loaded for memory encoding")
        except Exception as exc:
            log.warning("KGNodeEncoder unavailable (%s) — using zero fallback", exc)
            _NODE_ENC = None
    return _NODE_ENC

_NODE_ENC = None  # module-level cache


def encode_kg_node_as_memory(
    node_fields: dict[str, str],
    _unused_encode_fn,          # kept for API compat, ignored
    device: torch.device,
) -> torch.Tensor:
    """
    Encode each KG field separately using the frozen KGNodeEncoder.

    KGNodeEncoder (dual encoder, phase 2 InfoNCE) is validated to produce
    semantically distinct vectors:
      entropy vs backpropagation ≈ -0.24 cosine
      entropy vs entropy         ≈  1.00 cosine
    This gives the decoder real structure to cross-attend over.

    LTC_fwd had 0.98-1.00 cosine between ALL concepts — unusable for generation.

    Returns (N_fields, 128) memory sequence.
    """
    enc = _get_node_encoder()
    embeddings = []

    for field_name, field_text in node_fields.items():
        if not field_text or not field_text.strip():
            continue
        text = f"{field_name}: {field_text[:300]}"
        if enc is not None:
            with torch.no_grad():
                vec = enc.encode(text)   # returns torch.Tensor (128,)
            if vec is not None:
                if not isinstance(vec, torch.Tensor):
                    vec = torch.tensor(vec, dtype=torch.float32)
                embeddings.append(vec.float().cpu())
        else:
            embeddings.append(torch.zeros(LTC_DIM))

    if not embeddings:
        return torch.zeros(1, LTC_DIM, device=device)

    return torch.stack(embeddings).to(device)   # (N_fields, 128)



def build_node_fields(concept_id: str, stage3: dict, store) -> dict[str, str]:
    """
    Build the per-field dict for a KG node from stage3 data + store.
    Used at inference time to feed the encoder.
    """
    fields = {}

    name = concept_id.replace("_", " ")
    fields["name"] = name

    defn = stage3.get("definition", "")
    if defn:
        fields["definition"] = defn

    examples = stage3.get("examples", [])
    if examples:
        ex_str = " | ".join(str(e) for e in examples[:3] if e)
        if ex_str:
            fields["examples"] = ex_str

    non_ex = stage3.get("non_examples", [])
    if non_ex:
        ne_str = str(non_ex[0]) if non_ex else ""
        if ne_str:
            fields["non_examples"] = ne_str

    # Top connections from stage3 related
    related = stage3.get("related", [])
    connects = [r for r in related if r.get("edge") not in ("REQUIRES", "IS_A")][:4]
    if connects:
        conn_parts = [
            f"{r['concept'].replace('_', ' ')} ({r.get('edge', '').lower()})"
            for r in connects
        ]
        fields["connects_to"] = ", ".join(conn_parts)

    requires = [r for r in related if r.get("edge") == "REQUIRES"][:3]
    if requires:
        req_names = [r["concept"].replace("_", " ") for r in requires]
        fields["requires"] = ", ".join(req_names)

    return fields


# ── FluxLanguageModel ────────────────────────────────────────────────────────

class FluxLanguageModel(nn.Module):
    """
    Small autoregressive language model for Flux.

    Encoder:  frozen LTC (per-field → N_fields × 128 memory sequence)
    Decoder:  4-layer causal Transformer, 128-dim, word-level vocab

    Training: teacher forcing on (structured_input, natural_output) pairs
    Inference: autoregressive greedy decoding (or temperature sampling)
    """

    def __init__(
        self,
        vocab:     dict[str, int],
        ltc_encode_fn,               # callable(str) → np.ndarray(128) | None
        d_model:   int = 128,
        n_heads:   int = 4,
        n_layers:  int = 4,
        dim_ff:    int = 512,
        dropout:   float = 0.1,
        max_len:   int = 256,
    ) -> None:
        super().__init__()

        self.vocab         = vocab
        self.vocab_size    = len(vocab)
        self._ltc_encode   = ltc_encode_fn
        self.d_model       = d_model
        self.max_len       = max_len

        # ── Context projection: LTC_DIM (128) → d_model ─────────────────────
        # Handles the case where d_model ≠ LTC_DIM in the future
        self.mem_proj = nn.Linear(LTC_DIM, d_model)

        # ── Decoder ──────────────────────────────────────────────────────────
        self.token_embed = nn.Embedding(self.vocab_size, d_model, padding_idx=PAD_ID)
        self.pos_enc     = SinusoidalPE(d_model, max_len)

        decoder_layer = nn.TransformerDecoderLayer(
            d_model        = d_model,
            nhead          = n_heads,
            dim_feedforward= dim_ff,
            dropout        = dropout,
            batch_first    = True,
        )
        self.decoder    = nn.TransformerDecoder(decoder_layer, num_layers=n_layers)
        self.output_proj = nn.Linear(d_model, self.vocab_size)

        self._init_weights()

    def _init_weights(self) -> None:
        nn.init.normal_(self.token_embed.weight, std=0.02)
        nn.init.normal_(self.output_proj.weight, std=0.02)
        nn.init.zeros_(self.output_proj.bias)

    # ── Forward (training) ────────────────────────────────────────────────────

    def forward(
        self,
        memory:     torch.Tensor,          # (B, N_fields, 128) from LTC encoder
        target_ids: torch.Tensor,          # (B, T) teacher-forced token IDs
        tgt_padding_mask: Optional[torch.Tensor] = None,  # (B, T) True=pad
    ) -> torch.Tensor:
        """
        Forward pass for training (teacher forcing).

        Returns
        -------
        logits: (B, T, vocab_size)
        """
        B, T = target_ids.shape
        device = target_ids.device

        # Project memory to d_model
        mem = self.mem_proj(memory)            # (B, N_fields, d_model)

        # Embed + position encode target tokens
        tgt = self.pos_enc(
            self.token_embed(target_ids)       # (B, T, d_model)
        )

        # Causal mask (attend only to previous positions)
        causal_mask = nn.Transformer.generate_square_subsequent_mask(T, device=device)

        # Decode
        out = self.decoder(
            tgt,
            mem,
            tgt_mask              = causal_mask,
            tgt_key_padding_mask  = tgt_padding_mask,
        )                                      # (B, T, d_model)

        logits = self.output_proj(out)         # (B, T, vocab_size)
        return logits

    # ── Inference ────────────────────────────────────────────────────────────

    @torch.no_grad()
    def generate(
        self,
        node_fields:  dict[str, str],
        device:       torch.device = torch.device("cpu"),
        max_words:    int = 150,
        temperature:  float = 1.0,
        top_k:        int = 0,
    ) -> str:
        """
        Generate natural English from KG node fields.

        Parameters
        ----------
        node_fields : dict — per-field text (from build_node_fields())
        max_words   : int  — max output tokens
        temperature : float — sampling temperature (1.0 = unmodified)
        top_k       : int  — top-k sampling (0 = greedy)

        Returns
        -------
        str — natural English paragraph
        """
        self.eval()

        # Encode KG fields with frozen LTC → memory sequence
        memory = encode_kg_node_as_memory(node_fields, self._ltc_encode, device)
        memory = memory.unsqueeze(0)           # (1, N_fields, 128)
        mem    = self.mem_proj(memory)         # (1, N_fields, d_model)

        # Start decoding from [BOS]
        generated = [BOS_ID]

        for _ in range(max_words):
            inp = torch.tensor([generated], device=device)
            tgt = self.pos_enc(self.token_embed(inp))

            T           = len(generated)
            causal_mask = nn.Transformer.generate_square_subsequent_mask(T, device=device)

            out    = self.decoder(tgt, mem, tgt_mask=causal_mask)
            logits = self.output_proj(out[:, -1, :])   # (1, vocab_size)

            if temperature != 1.0:
                logits = logits / temperature

            if top_k > 0:
                # Top-k sampling
                top_vals, top_idx = torch.topk(logits, top_k, dim=-1)
                probs   = torch.softmax(top_vals, dim=-1)
                sampled = top_idx[0, torch.multinomial(probs[0], 1)]
                next_id = sampled.item()
            else:
                # Greedy
                next_id = logits.argmax(dim=-1).item()

            if next_id == EOS_ID:
                break
            generated.append(next_id)

        # Decode to text (skip BOS)
        idx_to_word = {v: k for k, v in self.vocab.items()}
        tokens = [
            idx_to_word.get(i, "[UNK]")
            for i in generated[1:]
            if i not in (PAD_ID,)
        ]
        return _detokenize(tokens)

    # ── Save / Load ───────────────────────────────────────────────────────────

    def save(self, path: Path = CKPT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Infer actual n_heads and n_layers from the decoder
        n_layers = len(self.decoder.layers)
        # dim_ff from first layer's linear1
        dim_ff = self.decoder.layers[0].linear1.out_features
        # n_heads from first layer's self_attn
        n_heads = self.decoder.layers[0].self_attn.num_heads
        torch.save({
            "state_dict": self.state_dict(),
            "vocab":      self.vocab,
            "d_model":    self.d_model,
            "n_heads":    n_heads,
            "n_layers":   n_layers,
            "dim_ff":     dim_ff,
            "max_len":    self.max_len,
        }, path)
        log.debug("FluxLanguageModel saved → %s", path)

    @classmethod
    def load(
        cls,
        ltc_encode_fn,
        path: Path = CKPT_PATH,
        device: torch.device = torch.device("cpu"),
    ) -> "FluxLanguageModel":
        ckpt  = torch.load(path, map_location=device)
        vocab = ckpt["vocab"]
        model = cls(
            vocab         = vocab,
            ltc_encode_fn = ltc_encode_fn,
            d_model       = ckpt.get("d_model",  128),
            n_heads       = ckpt.get("n_heads",    4),
            n_layers      = ckpt.get("n_layers",   4),
            dim_ff        = ckpt.get("dim_ff",   512),
            max_len       = ckpt.get("max_len",  256),
        )
        model.load_state_dict(ckpt["state_dict"])
        model.to(device)
        log.info(
            "FluxLanguageModel loaded: d=%d L=%d ff=%d vocab=%d",
            ckpt.get("d_model", 128), ckpt.get("n_layers", 4),
            ckpt.get("dim_ff", 512), len(vocab),
        )
        return model


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detokenize(tokens: list[str]) -> str:
    """Join word-level tokens back into readable text."""
    import re
    text = " ".join(tokens)
    # Fix spacing around punctuation
    text = re.sub(r" ([.,!?;:'\)])", r"\1", text)
    text = re.sub(r"\( ", "(", text)
    text = re.sub(r"' s\b", "'s", text)
    text = text.capitalize()
    if text and text[-1] not in ".!?":
        text += "."
    return text
