"""
flux/language/dual_encoder/kg_node_encoder.py

Character n-gram encoder for KG node definitions.

Architecture:
  chars → unigram_embed (masked mean) + bigram_embed (masked mean)
        → concat(128) → Linear(256) → LayerNorm → GELU → Dropout → Linear(128) → L2-norm

Stable by construction: no ODE, no hidden states, no temporal coupling.
Trains aggressively with hard negatives and margin loss without affecting
the LTC query encoder.

Vocab: reuses Flux's char_to_idx (vocab_size=152) for consistency.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

log = logging.getLogger(__name__)

VOCAB_PATH  = Path("logs/flux_vocab.json")
CKPT_DIR    = Path("checkpoints/dual_encoder")
PHASE1_CKPT = CKPT_DIR / "node_enc_phase1.pt"
PHASE2_CKPT = CKPT_DIR / "node_enc_phase2.pt"
PHASE2_BEST = CKPT_DIR / "node_enc_phase2_best.pt"

OUT_DIM   = 128
MAX_CHARS = 512


def _load_vocab() -> tuple[dict[str, int], int]:
    data = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    return data["char_to_idx"], data["vocab_size"]


class KGNodeEncoder(nn.Module):
    """
    Encodes KG node definitions into 128-dim L2-normalised semantic vectors.
    Character unigram + bigram features, mean-pooled, projected.
    """

    def __init__(self, vocab_size: int = 152, ngram_dim: int = 64, out_dim: int = OUT_DIM):
        super().__init__()
        self.vocab_size = vocab_size
        self.ngram_dim  = ngram_dim
        self.out_dim    = out_dim

        # padding_idx=0 → embedding[0] is always zero → masked mean works naturally
        self.char_embed   = nn.Embedding(vocab_size, ngram_dim, padding_idx=0)
        self.bigram_embed = nn.Embedding(vocab_size * vocab_size, ngram_dim, padding_idx=0)

        self.proj = nn.Sequential(
            nn.Linear(ngram_dim * 2, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(0.10),
            nn.Linear(256, out_dim),
        )
        self._init_weights()
        self._c2i: dict[str, int] = {}

    def _init_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def _ensure_vocab(self) -> None:
        if not self._c2i:
            self._c2i, _ = _load_vocab()

    def tokenise(self, text: str) -> torch.Tensor:
        """str → 1-D long tensor of char indices (truncated to MAX_CHARS)."""
        self._ensure_vocab()
        ids = [self._c2i.get(ch, 0) for ch in text.lower()[:MAX_CHARS]]
        return torch.tensor(ids or [0], dtype=torch.long)

    def forward(self, char_ids: torch.Tensor) -> torch.Tensor:
        """
        char_ids: (B, T) zero-padded or (T,) single.
        Returns:  (B, out_dim) or (out_dim,)  L2-normalised.
        """
        squeeze = char_ids.dim() == 1
        if squeeze:
            char_ids = char_ids.unsqueeze(0)

        # Non-padding mask (padding_idx=0 → those positions are zeroed embeddings anyway,
        # but explicit masking gives correct mean denominator)
        mask    = (char_ids != 0).float()                      # (B, T)
        lengths = mask.sum(dim=1, keepdim=True).clamp(min=1)  # (B, 1)

        uni = self.char_embed(char_ids)                        # (B, T, D)
        unigram = (uni * mask.unsqueeze(-1)).sum(1) / lengths  # (B, D)

        T = char_ids.size(1)
        if T > 1:
            bi_ids  = (char_ids[:, :-1] * self.vocab_size + char_ids[:, 1:]) % (self.vocab_size ** 2)
            bi_mask = mask[:, :-1] * mask[:, 1:]
            bi_len  = bi_mask.sum(dim=1, keepdim=True).clamp(min=1)
            bi_emb  = self.bigram_embed(bi_ids)                          # (B, T-1, D)
            bigram  = (bi_emb * bi_mask.unsqueeze(-1)).sum(1) / bi_len   # (B, D)
        else:
            bigram = torch.zeros(char_ids.size(0), self.ngram_dim, device=char_ids.device)

        out = self.proj(torch.cat([unigram, bigram], dim=-1))  # (B, out_dim)
        out = F.normalize(out, p=2, dim=-1)
        return out.squeeze(0) if squeeze else out

    def encode(self, text: str, device: torch.device | None = None) -> torch.Tensor:
        """Convenience: str → (out_dim,) no-grad tensor."""
        ids = self.tokenise(text)
        if device is not None:
            ids = ids.to(device)
        with torch.no_grad():
            return self(ids)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "state_dict": self.state_dict(),
            "config": {"vocab_size": self.vocab_size, "ngram_dim": self.ngram_dim, "out_dim": self.out_dim},
        }, str(path))
        log.info("KGNodeEncoder: saved → %s", path)

    @classmethod
    def load(cls, path: Path) -> "KGNodeEncoder":
        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
        model = cls(**ckpt.get("config", {}))
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        return model
