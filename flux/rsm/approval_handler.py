"""
flux/rsm/approval_handler.py

Stage 6 Layer 2 — approval handler (real implementation).

Delegates to reasoning_approval.ReasoningApprovalHandler.
Layer 3 stub preserved with NotImplementedError.

Constitutional invariant (unchanged): Layer 1 RSM MUST NOT modify
flux_kg_rules.lp or any .lp axiom file. Those are Layer 2 concerns.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ReasoningRSMHandler:
    """Layer 2: Reasoning RSM — proposes and merges inference axiom changes."""

    def __init__(self) -> None:
        from flux.rsm.reasoning_approval import ReasoningApprovalHandler
        self._handler = ReasoningApprovalHandler()

    def approve(self, proposal_id: str, method: str = "cli") -> str:
        return self._handler.approve(proposal_id, method=method)

    def reject(self, proposal_id: str, reason, note: str = "") -> str:
        from flux.rsm.reasoning_approval import RejectionReason
        if isinstance(reason, str):
            reason = RejectionReason(reason.upper())
        return self._handler.reject(proposal_id, reason, note)

    def show_pending(self) -> list[dict]:
        return self._handler.show_pending()

    def propose_axiom(self, axiom: str, justification: str) -> str:
        """Legacy stub signature — log and defer to automated pipeline."""
        log.info("propose_axiom called directly (use run_cycle for automated proposals): %s",
                 axiom[:120])
        return "deferred_to_pipeline"


class ArchitecturalRSMHandler:
    """Layer 3: Architectural RSM — requires Ed25519 signed approval. NOT YET IMPLEMENTED."""

    def propose_change(self, component: str, change_description: str) -> str:
        raise NotImplementedError(
            "Layer 3 (Architectural RSM) not yet implemented. "
            "Wire in at Stage 7."
        )
