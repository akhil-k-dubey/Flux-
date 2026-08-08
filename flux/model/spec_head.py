"""
flux/model/spec_head.py

Predicts the formal specification language required for a concept, and the
confidence in that prediction.

Architecture:
  Shared trunk:       Linear(128 → 32) → GELU → Dropout(0.1)
  Language branch:    Linear(32 → 4)              — softmax logits for [NONE, CLINGO, Z3, LEAN4]
  Confidence branch:  Linear(32 → 1) → Sigmoid   — how confident in the language prediction

Input:
  final_state — (128,) — Cell 2's x[T] from LiquidTransformer

Outputs:
  lang_logits  — (4,)  raw logits for CrossEntropyLoss
  spec_conf    — ()    scalar [0, 1]

Language index mapping (must stay in sync with schema.py SPEC_LANGUAGES):
  0 = NONE
  1 = CLINGO
  2 = Z3
  3 = LEAN4

Training target:
  From flux_spec_targets.jsonl:
    lang_index — integer 0-3
    spec_confidence — float [0, 1]
  Loss: CrossEntropyLoss(lang_logits, lang_index)
        + 0.3 × BCELoss(spec_conf, spec_confidence)
  Note: spec loss is annealed from weight 0.1 to 0.5 over Phase 2 training.

Parameter count:
  Linear(128→32): 128×32 + 32 = 4,128
  lang_head(32→4): 32×4 + 4  = 132
  conf_head(32→1): 32×1 + 1  = 33
  Total: 4,293

Known gap: LEAN4 has zero training examples (no lean4_map in FLUX_PRIMITIVES.md).
  lang_logits[3] is trained only from negative signal (never the correct class).
  Spec head will learn LEAN4 = 'not NONE, CLINGO, or Z3' by elimination.
  Acceptable for Phase 1-2. Real Lean4 examples needed for Phase 3.
"""

from __future__ import annotations
import torch
import torch.nn as nn


# Language → index. Must match schema.py SPEC_LANGUAGES ordering.
LANG_TO_IDX: dict[str, int] = {
    "NONE":   0,
    "CLINGO": 1,
    "Z3":     2,
    "LEAN4":  3,
}
IDX_TO_LANG: dict[int, str] = {v: k for k, v in LANG_TO_IDX.items()}


class SpecHead(nn.Module):
    """
    Shared-trunk dual-head: language classifier + spec confidence regressor.

    The shared trunk learns 'what kind of specification does this concept need?'
    The language branch identifies which formal language.
    The confidence branch estimates certainty of that identification.
    """

    def __init__(self, hidden_dim: int = 128, bottleneck: int = 32, dropout: float = 0.1):
        super().__init__()
        n_langs = len(LANG_TO_IDX)

        self.trunk = nn.Sequential(
            nn.Linear(hidden_dim, bottleneck),
            nn.GELU(),
            nn.Dropout(dropout),
        )
        self.lang_head = nn.Linear(bottleneck, n_langs)   # raw logits → CrossEntropyLoss
        self.conf_head = nn.Linear(bottleneck, 1)          # raw logit → BCEWithLogitsLoss

    def forward(
        self, final_state: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Args:
            final_state: (hidden_dim,) or (batch, hidden_dim)
        Returns:
            lang_logits: (4,) or (batch, 4)  — raw logits for CrossEntropyLoss
            spec_conf:   ()   or (batch,)    — raw logit for BCEWithLogitsLoss
                         Apply torch.sigmoid() at inference for probability.
        """
        trunk_out   = self.trunk(final_state)                      # (..., 32)
        lang_logits = self.lang_head(trunk_out)                    # (..., 4)
        spec_conf   = self.conf_head(trunk_out).squeeze(-1)        # (...)
        return lang_logits, spec_conf

    def predict(self, final_state: torch.Tensor) -> dict:
        """
        Inference helper: return language name and confidence probability.
        No gradient tracked.
        """
        with torch.no_grad():
            lang_logits, spec_conf_logit = self.forward(final_state)
            lang_idx  = int(lang_logits.argmax(-1).item())
            lang_name = IDX_TO_LANG[lang_idx]
            return {
                "spec_language":   lang_name,
                "spec_confidence": float(torch.sigmoid(spec_conf_logit).item()),
                "lang_probs":      lang_logits.softmax(-1).tolist(),
            }