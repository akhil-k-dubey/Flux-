"""
flux/language/language_encoder.py

Transformer Encoder — maps English utterances to manifold concept vectors.

This is the language-to-math translation layer. It strips English entirely
and produces two 257-dim vectors in Lorentz manifold space:

  op_vec     — where in manifold space the operator lives
               (nearest operator concept = routing decision, no hardcoding)
  target_vec — where in manifold space the target concept lives

The LTC/manifold receives these vectors directly — it never sees English.

Architecture:
  Input:  English text (character-level tokenized, CLS prepended)
  → CharEmbedding (vocab_size × d_model=128)
  → 4-layer Transformer Encoder (nhead=4, dim_ff=256, dropout=0.1)
  → CLS token representation (128-dim)
  → Two projection heads (Linear 128 → 257):
        op_head:     predicts operator manifold position
        target_head: predicts target concept manifold position
  Output: (op_vec: Tensor[257], target_vec: Tensor[257])

Training signal (from encoder_trainer.py and rl_trainer.py):
  MSE(op_vec,     manifold.get_embedding(correct_op_id))
  MSE(target_vec, manifold.get_embedding(correct_concept_id))

Checkpoint: checkpoints/language_encoder.pt
Singleton — loaded once, reused across all calls.
"""
from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

log = logging.getLogger(__name__)

ENCODER_CKPT  = Path("checkpoints/language_encoder.pt")
MANIFOLD_DIM  = 257        # 256 spatial + 1 Lorentz time coordinate
D_MODEL       = 128
N_HEADS       = 4
N_LAYERS      = 2
DIM_FF        = 256
MAX_SEQ_LEN   = 64
DROPOUT       = 0.1

OP_NAMES = [
    "Build", "CanYou", "Connect", "Assess", "Simulate",  # Phase 1
    "Know", "Understand", "Explain",                      # Phase 2
    "Greet", "Sync",                                      # Phase 3
    "None",                                               # null / fall-through
]
N_OPS = len(OP_NAMES)  # 12

# Character vocabulary — printable ASCII + special tokens
_CHARS    = [chr(i) for i in range(32, 127)] + ["<PAD>", "<CLS>", "<UNK>"]
_C2I      = {c: i for i, c in enumerate(_CHARS)}
VOCAB_SIZE = len(_CHARS)
PAD_IDX   = _C2I["<PAD>"]
CLS_IDX   = _C2I["<CLS>"]
UNK_IDX   = _C2I["<UNK>"]

_lock:    threading.Lock = threading.Lock()
_encoder: Optional["LanguageEncoder"] = None


class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = MAX_SEQ_LEN) -> None:
        super().__init__()
        pe  = torch.zeros(max_len, d_model)
        pos = torch.arange(max_len).unsqueeze(1).float()
        div = torch.exp(torch.arange(0, d_model, 2).float()
                        * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, :x.size(1)]


class LanguageEncoder(nn.Module):
    """
    Transformer encoder: English utterance → (op_vec, target_vec) in 257-dim
    Lorentz manifold space. No hardcoded verb lists. Routing is purely geometric:
    nearest operator concept embedding = selected operator.
    """

    def __init__(self, manifold_dim: int = MANIFOLD_DIM) -> None:
        super().__init__()
        self.manifold_dim = manifold_dim

        self.embedding = nn.Embedding(VOCAB_SIZE, D_MODEL, padding_idx=PAD_IDX)
        self.pos_enc   = PositionalEncoding(D_MODEL)

        enc_layer = nn.TransformerEncoderLayer(
            d_model=D_MODEL, nhead=N_HEADS,
            dim_feedforward=DIM_FF, dropout=DROPOUT,
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(enc_layer, num_layers=N_LAYERS)

        # Auxiliary classifier — cross-entropy training signal during transition.
        # Op routing by class-label argmax → phased out as op_head matures.
        self.op_classifier = nn.Linear(D_MODEL, N_OPS)

        # op_head: projects CLS embedding → operator manifold vector.
        # Trained to align with manifold.get_embedding(op_concept_id).
        # Once trained: VectorRouter uses cosine distance to operator centroids
        # instead of argmax on op_classifier logits.
        self.op_head = nn.Sequential(
            nn.Linear(D_MODEL, D_MODEL), nn.GELU(),
            nn.Linear(D_MODEL, manifold_dim),
        )

        # target_head: projects CLS embedding → target concept manifold vector.
        self.target_head = nn.Sequential(
            nn.Linear(D_MODEL, D_MODEL), nn.GELU(),
            nn.Linear(D_MODEL, manifold_dim),
        )
        self._init_weights()

    def _init_weights(self) -> None:
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def tokenize(self, text: str) -> torch.Tensor:
        """Text → [1, MAX_SEQ_LEN] token tensor with CLS prepended."""
        text = text.lower().strip()[:MAX_SEQ_LEN - 1]
        ids  = [CLS_IDX] + [_C2I.get(c, UNK_IDX) for c in text]
        ids  = ids[:MAX_SEQ_LEN]
        ids += [PAD_IDX] * (MAX_SEQ_LEN - len(ids))
        return torch.tensor(ids, dtype=torch.long).unsqueeze(0)

    def forward(self, tokens: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """tokens: [B, seq] → (op_logits [B,N_OPS], op_vec [B,257], target_vec [B,257])"""
        pad_mask = (tokens == PAD_IDX)
        emb = self.pos_enc(self.embedding(tokens))
        out = self.transformer(emb, src_key_padding_mask=pad_mask)
        cls = out[:, 0, :]
        return self.op_classifier(cls), self.op_head(cls), self.target_head(cls)

    @torch.no_grad()
    def encode(self, utterance: str) -> tuple[str, float, torch.Tensor, torch.Tensor]:
        """
        Encode one utterance.
        Returns: (op_name, confidence, target_vec, op_vec)
          op_name, confidence: from op_classifier (class-label routing, transition fallback)
          target_vec: 257-dim manifold vector for target concept
          op_vec: 257-dim manifold vector for operator (used by VectorRouter)
        """
        device    = next(self.parameters()).device
        tokens    = self.tokenize(utterance).to(device)
        logits, op_v, tgt_v = self.forward(tokens)
        probs     = torch.softmax(logits, dim=-1).squeeze(0)
        op_idx    = int(probs.argmax().item())
        return (
            OP_NAMES[op_idx],
            float(probs[op_idx].item()),
            tgt_v.squeeze(0).cpu(),
            op_v.squeeze(0).cpu(),
        )

    @torch.no_grad()
    def encode_with_attention(
        self, utterance: str
    ) -> tuple[str, float, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Like encode(), but also returns last-layer CLS-row attention weights.

        Returns: (op_name, confidence, target_vec, op_vec, cls_attn)
          cls_attn: [seq_len] — CLS→token attention (averaged across all heads).
            Position 0 = CLS self-attention (ignored).
            Positions 1..N = attention from CLS to each input character.
        """
        device  = next(self.parameters()).device
        tokens  = self.tokenize(utterance).to(device)
        pad_mask = (tokens == PAD_IDX)
        x = self.pos_enc(self.embedding(tokens))

        # Run all but the last transformer layer normally
        for i, layer in enumerate(self.transformer.layers):
            if i < len(self.transformer.layers) - 1:
                x = layer(x, src_key_padding_mask=pad_mask)
                continue

            # Last layer: manually extract attention
            q = k = v = layer.norm1(x) if layer.norm_first else x
            attn_out, attn_w = layer.self_attn(
                q, k, v,
                key_padding_mask=pad_mask,
                need_weights=True,
                average_attn_weights=True,  # [B, seq, seq] averaged across heads
            )
            # Residual + norm
            x = x + layer.dropout1(attn_out)
            if not layer.norm_first:
                x = layer.norm1(x)
            x_ff = layer.norm2(x) if layer.norm_first else x
            x_ff = layer.linear2(layer.dropout(layer.activation(layer.linear1(x_ff))))
            x = x + layer.dropout2(x_ff)
            if not layer.norm_first:
                x = layer.norm2(x)

        cls = x[:, 0, :]
        logits = self.op_classifier(cls)
        op_v   = self.op_head(cls)
        tgt_v  = self.target_head(cls)

        probs  = torch.softmax(logits, dim=-1).squeeze(0)
        op_idx = int(probs.argmax().item())

        # cls_attn: CLS row, averaged heads, squeezed to [seq]
        cls_attn = attn_w.squeeze(0)[0].cpu()  # [seq]

        return (
            OP_NAMES[op_idx],
            float(probs[op_idx].item()),
            tgt_v.squeeze(0).cpu(),
            op_v.squeeze(0).cpu(),
            cls_attn,
        )


def get_encoder(manifold_dim: int = MANIFOLD_DIM) -> LanguageEncoder:
    """Load the singleton LanguageEncoder. Always returns a model (may be untrained)."""
    global _encoder
    with _lock:
        if _encoder is not None:
            return _encoder
        enc = LanguageEncoder(manifold_dim=manifold_dim)
        if ENCODER_CKPT.exists():
            try:
                state = torch.load(ENCODER_CKPT, map_location="cpu",
                                   weights_only=True)
                # strict=False: new op_head weights won't be in old checkpoints —
                # they start from Xavier init and are trained in Phase 1 retraining.
                missing, unexpected = enc.load_state_dict(state, strict=False)
                if missing:
                    log.info("LanguageEncoder: new heads initialised: %s", missing)
                log.info("LanguageEncoder loaded from %s", ENCODER_CKPT)
            except Exception as e:
                log.warning("LanguageEncoder load failed (%s) — untrained", e)
        else:
            log.info("LanguageEncoder untrained. Run: python -m flux.language.encoder_trainer")
        enc.eval()
        _encoder = enc
        return _encoder


def save_encoder(enc: LanguageEncoder) -> None:
    ENCODER_CKPT.parent.mkdir(parents=True, exist_ok=True)
    torch.save(enc.state_dict(), ENCODER_CKPT)
    log.info("LanguageEncoder saved → %s", ENCODER_CKPT)


def reload_encoder() -> None:
    global _encoder
    with _lock:
        _encoder = None
