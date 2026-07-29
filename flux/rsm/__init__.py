"""
flux/rsm/__init__.py

Recursive Self-Modification (RSM) package — Stage 6.

Layer 1 (implemented): Knowledge RSM
  Flux monitors her own KG for gaps, scores them by impact,
  synthesises candidate TEACH blocks, verifies via Clingo + MC Dropout,
  and auto-commits or queues for review.

Layer 2 (stub): Reasoning RSM
  Flux proposes new Clingo axioms — requires approval.

Layer 3 (stub): Architectural RSM
  Flux proposes changes to her own neural components — requires Ed25519 approval.

Constitutional invariants enforced at every layer:
  - Immutable nodes (Layer 0 axioms) can never be modified.
  - Auto-commits require Clingo consistency + MC Dropout uncertainty < 0.15.
  - All proposals logged to logs/review_queue.jsonl.
"""

from flux.rsm.gap_analyser       import GapAnalyser, GapScore
from flux.rsm.proposal_queue     import ProposalQueue, ProposalState
from flux.rsm.proposal_generator import ProposalGenerator

__all__ = [
    "GapAnalyser",
    "GapScore",
    "ProposalQueue",
    "ProposalState",
    "ProposalGenerator",
]
