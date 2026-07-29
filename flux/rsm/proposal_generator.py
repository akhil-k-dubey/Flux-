"""
flux/rsm/proposal_generator.py

Given a GapScore, generates a TEACH block, verifies it, and either
auto-commits it to the KG or routes it to the review queue.

Definition synthesis confidence levels (per design spec):
    structural_only      (no text found):                          0.65
    template_extraction  (concept name found in existing defs):    0.75
    pattern_induction    (pattern filled from corpus templates):   0.80

Constitutional auto-commit gate:
    mc_mean >= 0.75 AND mc_std <= 0.15 → auto-commit
    else                               → pending in review queue

All proposals (committed or pending) are logged to logs/review_queue.jsonl.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Type name inference table ────────────────────────────────────────────────
# If a referencing node is of type X and references the gap via REQUIRES,
# the gap concept is likely a Y.
_EDGE_TYPE_TO_IS_A: dict[str, str] = {
    "MATHEMATICAL_OPERATOR": "MATHEMATICAL_OPERATOR",
    "EQUATION":              "EQUATION",
    "THEOREM":               "THEOREM",
    "ALGORITHM":             "ALGORITHM",
    "PROCESS":               "PROCESS",
    "PHYSICAL_LAW":          "PHYSICAL_LAW",
    "EXPERIMENTAL_RESULT":   "EXPERIMENTAL_RESULT",
}

# Fallback is_a type by layer
_LAYER_TO_IS_A: dict[str, str] = {
    "quantum_physics": "CONCEPT",
    "thermodynamics":  "CONCEPT",
    "machine_learning":"CONCEPT",
    "neuroscience":    "CONCEPT",
    "mathematics":     "MATHEMATICAL_CONCEPT",
    "None":            "CONCEPT",
    "unknown":         "CONCEPT",
}


@dataclass
class Proposal:
    """Generated proposal before queue insertion."""
    concept_id:           str
    gap_score:            float
    teach_text:           str
    synthesis_confidence: float
    is_a:                 str
    definition:           str
    justification:        str
    source:               str = "AUTO_RSM"


class ProposalGenerator:
    """
    Generates TEACH blocks from GapScore objects and routes them through:
      1. Definition synthesis (PatternInducer)
      2. MC Dropout uncertainty check
      3. Constitutional Clingo consistency check (via QueryEngine / ReasoningEngine)
      4. Auto-commit if gates pass, else review queue

    Usage:
        pg = ProposalGenerator()
        pg.process_gap(gap_score)   # analyse + synthesise + commit/queue one gap
    """

    def __init__(self) -> None:
        from flux.kg.store import get_store
        from flux.language.pattern_inducer import PatternInducer
        from flux.rsm.proposal_queue import ProposalQueue

        self.store    = get_store()
        self.queue    = ProposalQueue()
        self.inducer  = PatternInducer(kg_store=self.store)
        self.inducer.build()

    # ── Public API ───────────────────────────────────────────────────────────

    def process_gap(self, gap) -> str:
        """
        Full pipeline for one gap: synthesise → verify → commit or queue.

        Returns: "committed" | "pending" | "skipped" | "error"
        """
        concept_id = gap.concept_id

        # Already in KG or queue?
        if self.store.get(concept_id):
            log.debug("RSM: %s already in KG, skipping", concept_id)
            return "skipped"
        if self.queue.is_blocked(concept_id):
            log.debug("RSM: %s already in queue (%s), skipping",
                      concept_id, self.queue.state_of(concept_id))
            return "skipped"

        try:
            proposal = self._synthesise(gap)
            return self._commit_or_queue(proposal)
        except Exception as exc:
            log.warning("RSM: proposal failed for %s: %s", concept_id, exc)
            return "error"

    # ── Synthesis ────────────────────────────────────────────────────────────

    def _synthesise(self, gap) -> Proposal:
        """Build a TEACH block for the gap concept."""
        concept_id = gap.concept_id

        # 1. Find existing text that mentions this concept
        mentions = self.inducer.find_mentions(concept_id)

        # 2. Synthesise definition
        definition, def_confidence = self.inducer.synthesise(concept_id, mentions)

        # 3. Infer is_a type
        is_a = self._infer_is_a(gap)

        # 4. Build relates_to edges (gap → referencing nodes where appropriate)
        relates_to_entries = self._build_relates_to(gap)

        # 5. Build requires entries
        requires_entries = self._build_requires(gap)

        # 6. Assemble TEACH block text
        teach_text = self._build_teach_block(
            concept_id, is_a, definition,
            requires_entries, relates_to_entries,
        )

        return Proposal(
            concept_id           = concept_id,
            gap_score            = gap.gap_score,
            teach_text           = teach_text,
            synthesis_confidence = def_confidence,
            is_a                 = is_a,
            definition           = definition,
            justification        = gap.justification(),
        )

    def _infer_is_a(self, gap) -> str:
        """
        Infer the type of the gap concept from its referencing nodes' types.
        Most common type in referencing nodes → apply domain mapping → is_a.
        """
        from collections import Counter
        type_counts: Counter = Counter()
        for node_id in gap.referencing_nodes:
            node = self.store.get(node_id)
            if node:
                t = node.get("type", "CONCEPT")
                type_counts[str(t)] += 1
        if not type_counts:
            return "CONCEPT"

        most_common_type = type_counts.most_common(1)[0][0]
        # If the referencing nodes are of one type, gap is likely the same
        return _EDGE_TYPE_TO_IS_A.get(most_common_type,
               _LAYER_TO_IS_A.get(gap.domain, "CONCEPT"))

    def _build_relates_to(self, gap) -> list[str]:
        """
        Build relates_to entries: the gap concept ENABLES or is REQUIRED_BY
        its referencing nodes. Format: "node_id:EDGE_TYPE"
        """
        entries = []
        for node_id in gap.referencing_nodes[:3]:
            node = self.store.get(node_id)
            if not node:
                continue
            # Check if the referencing node REQUIRES the gap
            reqs = node.get("requires", [])
            if isinstance(reqs, str):
                reqs = [r.strip() for r in reqs.split("|") if r.strip()]
            if gap.concept_id in reqs:
                entries.append(f"{node_id}:ENABLES")
            else:
                entries.append(f"{node_id}:RELATES_TO")
        return entries

    def _build_requires(self, gap) -> list[str]:
        """
        The gap concept REQUIRES its referencing nodes that are its prerequisites.
        Limit to top-3 direct referencing nodes.
        """
        # Conservative: don't infer requires from referencing nodes
        # (we don't know if referencing→gap means gap→referencing)
        # Only add requires if the referencing node type clearly implies it
        return []

    def _build_teach_block(
        self,
        concept_id:      str,
        is_a:            str,
        definition:      str,
        requires:        list[str],
        relates_to:      list[str],
    ) -> str:
        lines = [
            "[TEACH]",
            f"id: RSM_{concept_id}",
            f"concept: {concept_id.replace('_', ' ')}",
            f"is_a: {is_a}",
            f"definition: {definition}",
            "examples: (auto-proposed — review recommended)",
            "non_examples: (auto-proposed — review recommended)",
        ]
        if requires:
            lines.append(f"requires: {' | '.join(requires)}")
        if relates_to:
            lines.append(f"relates_to: {' | '.join(relates_to)}")
        lines += [
            "confidence: 0.85",
            "note: auto-proposed by Flux RSM — verify before relying on this",
            "[/TEACH]",
        ]
        return "\n".join(lines)

    # ── Constitutional gate ───────────────────────────────────────────────────

    def _commit_or_queue(self, proposal: Proposal) -> str:
        """
        MC Dropout check → constitutional Clingo check → auto-commit or queue.

        Auto-commit gate:
            mc_mean >= 0.75 AND mc_std <= 0.15 → commit
            else                               → pending
        """
        from flux.model.consolidation_bridge import ConsolidationBridge

        try:
            bridge = ConsolidationBridge()
        except Exception as exc:
            log.warning("RSM: bridge unavailable (%s) — queuing %s", exc, proposal.concept_id)
            self._queue_pending(proposal, 0.0, 0.0, 0.0)
            return "pending"

        # 1. MC Dropout uncertainty
        mc_mean, mc_std = bridge.mc_confidence(proposal.teach_text, n=20)
        log.info(
            "RSM: MC Dropout for %s — mean=%.3f std=%.3f",
            proposal.concept_id, mc_mean, mc_std,
        )

        # High uncertainty → queue for review regardless of mean confidence
        if mc_std > 0.15:
            log.info(
                "RSM: %s uncertainty too high (std=%.3f > 0.15) → review queue",
                proposal.concept_id, mc_std,
            )
            self._queue_pending(proposal, mc_mean, mc_std, 0.0)
            return "pending"

        # Low mean confidence → also queue
        if mc_mean < 0.75:
            log.info(
                "RSM: %s confidence too low (mean=%.3f < 0.75) → review queue",
                proposal.concept_id, mc_mean,
            )
            self._queue_pending(proposal, mc_mean, mc_std, 0.0)
            return "pending"

        # 2. Deterministic bridge process (applies Clingo check via KGStore.write)
        result = bridge.process(proposal.teach_text)
        log.info(
            "RSM: bridge result for %s — %s conf=%.3f action=%s",
            proposal.concept_id, result.decision, result.confidence, result.kg_action,
        )

        if result.decision == "COMMIT":
            from flux.rsm.proposal_queue import ProposalState
            self.queue.add(
                concept_id           = proposal.concept_id,
                gap_score            = proposal.gap_score,
                teach_text           = proposal.teach_text,
                synthesis_confidence = proposal.synthesis_confidence,
                justification        = proposal.justification,
                state                = ProposalState.COMMITTED,
                mc_mean              = mc_mean,
                mc_std               = mc_std,
                bridge_confidence    = result.confidence,
                kg_action            = result.kg_action,
            )
            log.info("RSM: AUTO-COMMITTED %s (gap_score=%.1f)", proposal.concept_id, proposal.gap_score)
            return "committed"
        else:
            self._queue_pending(proposal, mc_mean, mc_std, result.confidence)
            return "pending"

    def _queue_pending(
        self,
        proposal:   Proposal,
        mc_mean:    float,
        mc_std:     float,
        bridge_conf: float,
    ) -> None:
        from flux.rsm.proposal_queue import ProposalState
        self.queue.add(
            concept_id           = proposal.concept_id,
            gap_score            = proposal.gap_score,
            teach_text           = proposal.teach_text,
            synthesis_confidence = proposal.synthesis_confidence,
            justification        = proposal.justification,
            state                = ProposalState.PENDING,
            mc_mean              = mc_mean,
            mc_std               = mc_std,
            bridge_confidence    = bridge_conf,
            kg_action            = "",
        )

    # ── Batch API ─────────────────────────────────────────────────────────────

    def run_cycle(self, max_proposals: int = 5) -> dict:
        """
        Run a full RSM cycle: analyse gaps → generate proposals → commit or queue.

        Returns summary: {committed: N, pending: N, skipped: N, error: N}
        """
        from flux.rsm.gap_analyser import GapAnalyser
        gaps = GapAnalyser().analyse(max_proposals=max_proposals)

        results: dict[str, int] = {"committed": 0, "pending": 0, "skipped": 0, "error": 0}
        for gap in gaps:
            outcome = self.process_gap(gap)
            results[outcome] = results.get(outcome, 0) + 1

        log.info(
            "RSM cycle complete: %d committed, %d pending, %d skipped, %d errors",
            results["committed"], results["pending"], results["skipped"], results["error"],
        )
        return results
