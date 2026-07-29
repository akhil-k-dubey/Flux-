"""
flux/rsm/approval_handler.py

Stub for RSM Layer 2 (Reasoning RSM) and Layer 3 (Architectural RSM).

Layer 2 — Proposes new Clingo axioms or reasoning rules.
    Requires explicit human approval via review_queue.jsonl inspection.
    Not yet implemented.

Layer 3 — Proposes architectural changes to Flux's neural components.
    Requires Ed25519 signed approval.
    Not yet implemented.

These stubs exist to make the Layer 1 → Layer 2 boundary explicit
and to prevent Layer 1 from accidentally making reasoning-rule changes.

Constitutional invariant: Layer 1 RSM MUST NOT modify flux_kg_rules.lp
or any .lp axiom file. Those are Layer 2 concerns.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ReasoningRSMHandler:
    """
    Layer 2: Reasoning RSM — proposes new inference axioms.
    Requires explicit human approval (never auto-commits).
    NOT YET IMPLEMENTED.
    """

    def propose_axiom(self, axiom: str, justification: str) -> str:
        """
        Stub — logs the proposed axiom but does not act on it.
        Returns "deferred" until Layer 2 is implemented.
        """
        log.info(
            "Layer 2 (stub): axiom proposed but not acted on (requires human approval).\n"
            "  axiom: %s\n  reason: %s",
            axiom[:200], justification[:200],
        )
        return "deferred"


class ArchitecturalRSMHandler:
    """
    Layer 3: Architectural RSM — proposes changes to Flux's own components.
    Requires Ed25519 signed approval.
    NOT YET IMPLEMENTED.
    """

    def propose_change(self, component: str, change_description: str) -> str:
        """
        Stub — logs the proposed change but does not act on it.
        Returns "deferred" until Layer 3 is implemented.
        """
        log.info(
            "Layer 3 (stub): architectural change proposed but not acted on "
            "(requires signed approval).\n  component: %s\n  change: %s",
            component, change_description[:200],
        )
        return "deferred"
