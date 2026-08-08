"""
flux/model/embedding.py

Character-level embedding layer for Flux's LTC model.
Converts char index sequences -> dense normalised vectors for LTC cell input.

Design decisions (deliberate — not to revisit without explicit review):

  embed_dim=32     82 * 32 = 2,624 embedding params.
                   Expressive enough for an 82-char vocabulary; small enough
                   to train fast on CPU or a single GPU.

  No PE            Sinusoidal / learnable positional encodings are mandatory
                   for permutation-invariant Transformers. LTC is sequential:
                   the ODE integrates through time, so x[t] already contains
                   the entire history x[0..t-1] baked in through dynamics.
                   Adding PE on top would be redundant and potentially
                   conflicting. If architecture ever changes to Transformer,
                   PE must be added back.

  LayerNorm        Normalises input variance before ODE integration.
                   Without it, early-training embedding variance drives large
                   dx steps and destabilises the state trajectory. At embed_dim
                   = 32 the cost is negligible; the stability benefit is real.
                   elementwise_affine=True (default) — gamma/beta give the layer
                   freedom to learn the right output scale for the LTC input range.

  padding_idx=0    UNK maps to a fixed zero vector. LayerNorm(zero) = zero (eps
                   prevents NaN). UNK produces no signal — correct behaviour.

Vocab drift:
  expand_vocab(new_size) adds new char rows while preserving all existing
  embeddings. Training-time only. Inference vocab is frozen at checkpoint time.
  Unknown chars at inference → UNK (index 0) → zero signal → model abstains.
"""

from __future__ import annotations
import torch
import torch.nn as nn
from pathlib import Path


class FluxEmbedding(nn.Module):
    """
    Character index tensor -> LayerNorm-normalised embedding tensor.

    Accepts any leading shape — batch, sequence, both, or scalar:
      (B, T) -> (B, T, embed_dim)
      (T,)   -> (T, embed_dim)
      ()     -> (embed_dim,)

    The LTC cell pulls one time-step slice (embed_dim,) per step.
    """

    def __init__(self, vocab_size: int, embed_dim: int = 32, padding_idx: int = 0):
        super().__init__()
        self.vocab_size  = vocab_size
        self.embed_dim   = embed_dim
        self.padding_idx = padding_idx
        self.embedding   = nn.Embedding(vocab_size, embed_dim, padding_idx=padding_idx)
        self.norm        = nn.LayerNorm(embed_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: integer tensor of char indices. Returns float tensor."""
        return self.norm(self.embedding(x))

    # -- Vocab drift handling --------------------------------------------------

    def expand_vocab(self, new_vocab_size: int) -> int:
        """
        Expand the embedding matrix to accommodate new characters.

        Preserves all existing embedding vectors exactly — trained
        representations are not disturbed. New rows are randomly initialised
        (correct: the model has never seen these characters before).

        Returns the number of new tokens added (0 if already large enough).

        Contract:
          - Training-time only. Never called during inference.
          - Caller (trainer.py) is responsible for saving the updated
            checkpoint after expansion.
          - Vocab hash must have already drifted in flux_meta.json before
            this method is called — expansion is not silent.
        """
        if new_vocab_size <= self.vocab_size:
            return 0

        old_weights = self.embedding.weight.data.clone()

        self.embedding = nn.Embedding(
            new_vocab_size, self.embed_dim, padding_idx=self.padding_idx
        ).to(old_weights.device)

        # Restore existing embeddings precisely
        self.embedding.weight.data[: self.vocab_size] = old_weights
        # Rows [self.vocab_size : new_vocab_size] are already randomly init'd

        added = new_vocab_size - self.vocab_size
        self.vocab_size = new_vocab_size
        return added

    # -- Checkpoint helpers ----------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Serialise to disk. Stores config alongside weights for safe reload."""
        torch.save(
            {
                "vocab_size":  self.vocab_size,
                "embed_dim":   self.embed_dim,
                "padding_idx": self.padding_idx,
                "state_dict":  self.state_dict(),
            },
            path,
        )

    @classmethod
    def load(cls, path: str | Path, map_location: str = "cpu") -> "FluxEmbedding":
        """Load from checkpoint. Config is read from the file — no manual args needed."""
        ckpt = torch.load(path, map_location=map_location)
        obj  = cls(ckpt["vocab_size"], ckpt["embed_dim"], ckpt["padding_idx"])
        obj.load_state_dict(ckpt["state_dict"])
        return obj

    def param_count(self) -> int:
        return sum(p.numel() for p in self.parameters())

    def __repr__(self) -> str:
        return (
            f"FluxEmbedding("
            f"vocab={self.vocab_size}, dim={self.embed_dim}, "
            f"params={self.param_count():,})"
        )
