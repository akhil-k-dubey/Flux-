"""
flux/model/consolidation_head.py

Predicts the commit_confidence scalar for a processed session.

Architecture:
  Linear(128 → 32) → GELU → Dropout(0.1) → Linear(32 → 1) → Sigmoid

Input:
  final_state — (128,) — Cell 2's x[T] from LiquidTransformer

Output:
  confidence — scalar float in [0, 1]
    0.0 = reject / negative session
    0.2–0.8 = gradient (speculative → well-reasoned)
    0.95 = synthetic standard
    1.0 = directly taught, gold standard

Training target:
  ground_truth_commit.commit_confidence from flux_kg_commits.jsonl
  Loss: BCELoss (binary cross-entropy on [0,1] targets)

Parameter count:
  Linear(128→32): 128×32 + 32 = 4,128
  Linear(32→1):   32×1  + 1  = 33
  Total:          4,161
"""

from __future__ import annotations
import torch
import torch.nn as nn


class ConsolidationHead(nn.Module):
    """
    Scalar confidence predictor from the LTC final hidden state.

    Maps x[T] ∈ ℝ^128 → confidence ∈ [0, 1].
    Width 32 bottleneck balances expressivity vs. overfitting risk at 313 samples.
    """

    def __init__(self, hidden_dim: int = 128, bottleneck: int = 32, dropout: float = 0.1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(hidden_dim, bottleneck),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(bottleneck, 1),
            # No Sigmoid here — BCEWithLogitsLoss (trainer) applies it
            # internally with better numerical stability than BCELoss(Sigmoid(x)).
            # At inference: apply torch.sigmoid() to get probability.
        )
        # Root cause of all three training collapses: the proj2 bias learns
        # logit(0.948) ≈ 2.9 within 2-3 epochs on this 85%-high-conf dataset.
        # Once the bias is large enough, gradients through the weight become
        # negligible relative to the bias gradient. The head stops using the
        # hidden state entirely and just predicts the dataset mean.
        #
        # Fix:
        #   gain=0.01  — weights start tiny, must earn magnitude from real signal
        #   zeros bias — bias must also earn its offset, cannot coast to 2.9 for free
        #
        # Combined with the 10× slower learning rate + weight_decay=0.5 on this
        # bias in the trainer optimizer, the head is forced to use hidden state
        # direction before it is allowed to rely on the bias shortcut.
        nn.init.xavier_uniform_(self.net[-1].weight, gain=0.01)
        nn.init.zeros_(self.net[-1].bias)

    def forward(self, final_state: torch.Tensor) -> torch.Tensor:
        """
        Args:
            final_state: (hidden_dim,) or (batch, hidden_dim)
        Returns:
            logit: scalar () or (batch,)  — raw logit, NOT probability.
            Apply torch.sigmoid() at inference for confidence in [0, 1].
        """
        out = self.net(final_state)          # (..., 1)
        return out.squeeze(-1)               # (...) — raw logit

    def predict_confidence(self, final_state: torch.Tensor) -> float:
        """Inference helper — returns confidence probability in [0, 1]."""
        with torch.no_grad():
            return float(torch.sigmoid(self.forward(final_state)).item())

    @property
    def threshold(self) -> float:
        """Consolidation gate: sigmoid(logit) >= threshold commits to KG."""
        return 0.85