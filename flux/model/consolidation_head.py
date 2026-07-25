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
        # Small initial weights on the output projection.
        # Default Kaiming init (weights ~ ±0.35) lets the head immediately
        # amplify hidden state variation — but the bias still learns fast.
        # gain=0.1 (weights ~ ±0.035) makes the head start near-constant,
        # so it must earn weight magnitude from real gradient signal.
        # Works with tier_weight rebalancing; ineffective without it.
        nn.init.xavier_uniform_(self.net[-1].weight, gain=0.1)

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
