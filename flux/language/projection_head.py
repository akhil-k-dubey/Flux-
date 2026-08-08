"""
flux/language/projection_head.py

Small trainable layer on top of the frozen LTC base.

Architecture
------------
  LTC final_state (256-dim, bidirectional)
      ↓  Linear(256, 256)
      ↓  LayerNorm
      ↓  GELU
      ↓  Dropout(0.1)
      ↓  Linear(256, 128)
      ↓  L2-normalise
  → 128-dim unit vector

Only the ProjectionHead trains.  LTC base weights are frozen —
EWC protection by construction (gradient never reaches them).

This follows the SimCSE/CLIP pattern:
  - Projection head is discarded after training in the original SimCSE
  - Here we KEEP it — it becomes the permanent embedding pipeline
  - At inference: LTC(text) → ProjectionHead → 128-dim embedding

Checkpoint saved to:  checkpoints/flux_proj_head.pt
(Separate from LTC checkpoints so they don't interfere.)
"""
from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

PROJ_DIM    = 128       # output embedding dimensionality
INPUT_DIM   = 256       # bidirectional LTC output (128 fwd + 128 bwd)
CKPT_PATH   = Path("checkpoints/flux_proj_head.pt")


class ProjectionHead(nn.Module):
    """
    Maps 256-dim bidirectional LTC embedding → 128-dim unit vector.

    Trainable parameters only (~100K). LTC is frozen at all times.
    """

    def __init__(
        self,
        input_dim:  int = INPUT_DIM,
        hidden_dim: int = 256,
        output_dim: int = PROJ_DIM,
        dropout:    float = 0.10,
    ):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim, bias=True),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, output_dim, bias=True),
        )
        self._init_weights()

    def _init_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: (batch, 256) or (256,)
        Returns: (batch, 128) or (128,) — L2-normalised.
        """
        squeeze = x.dim() == 1
        if squeeze:
            x = x.unsqueeze(0)
        out = self.net(x)                          # (batch, 128)
        out = F.normalize(out, p=2, dim=-1)        # unit-norm
        return out.squeeze(0) if squeeze else out

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: Path = CKPT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({"projection_head": self.state_dict()}, str(path))

    @classmethod
    def load(cls, path: Path = CKPT_PATH, **kwargs) -> "ProjectionHead":
        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
        model = cls(**kwargs)
        model.load_state_dict(ckpt["projection_head"])
        model.eval()
        return model

    @classmethod
    def is_trained(cls, path: Path = CKPT_PATH) -> bool:
        return path.exists()
