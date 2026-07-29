"""
flux/rsm/gap_analyser.py

Scans the live KG for concepts that are referenced but missing (dangling edges),
scores them by impact, applies domain balance, and returns Top-K candidates
for RSM proposal generation.

Gap scoring formula (per design spec):
    gap_score = (
        dangling_reference_count * 3 +   # how many nodes are waiting for this
        reasoning_chain_unlock_count * 5 + # new derivations enabled when filled
        recent_query_relevance * 2          # queried as unknown recently
    )

Thresholds (per design spec):
    MIN_DANGLING_REFS = 2
    MIN_GAP_SCORE     = 10

Domain balance (per design spec):
    MAX_PROPOSALS     = 5
    MAX_PER_DOMAIN    = 2

Triggers:
    - Post-COMMIT (called by ConsolidationBridge._run_post_commit_rsm)
    - Post-session (called by FluxInterface.end_session)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────
MIN_DANGLING_REFS = 2
MIN_GAP_SCORE     = 10
MAX_PROPOSALS     = 5
MAX_PER_DOMAIN    = 2

META_PATH = Path("logs/flux_meta.json")


@dataclass
class GapScore:
    """
    One scored gap concept.

    Attributes:
        concept_id:           normalized KG ID of the missing concept
        gap_score:            composite score (higher = more impactful to fill)
        domain:               inferred domain from referencing nodes' layers
        dangling_ref_count:   # of known nodes that directly reference this concept
        chain_unlock_count:   estimated # of new derivations unlocked if filled
        recent_query_rel:     # of recent user queries about this concept (capped at 5)
        referencing_nodes:    list of known node IDs that reference this concept
    """
    concept_id:         str
    gap_score:          float
    domain:             str
    dangling_ref_count: int
    chain_unlock_count: int
    recent_query_rel:   int
    referencing_nodes:  list[str] = field(default_factory=list)

    def justification(self) -> str:
        refs = ", ".join(self.referencing_nodes[:3])
        return (
            f"Referenced by {self.dangling_ref_count} known nodes ({refs}...). "
            f"Unlocks ~{self.chain_unlock_count} reasoning chains. "
            f"Queried {self.recent_query_rel}× as unknown. "
            f"gap_score={self.gap_score:.1f}"
        )


class GapAnalyser:
    """
    Scans the KG and returns scored gap concepts ready for RSM proposal.

    Usage:
        ga = GapAnalyser()
        gaps = ga.analyse()       # returns list[GapScore], Top-K by gap_score
    """

    def __init__(self) -> None:
        from flux.kg.store import get_store
        self.store = get_store()

    # ── Public API ───────────────────────────────────────────────────────────

    def analyse(
        self,
        max_proposals: int = MAX_PROPOSALS,
        max_per_domain: int = MAX_PER_DOMAIN,
    ) -> list[GapScore]:
        """
        Run gap analysis. Returns scored and domain-balanced list of gaps.
        Only concepts not already pending/committed/rejected in the queue are returned.
        """
        from flux.rsm.proposal_queue import ProposalQueue
        queue = ProposalQueue()

        dangling    = self._find_dangling()
        query_counts = self._load_query_counts()

        scored: list[GapScore] = []
        for concept_id, referencing_nodes in dangling.items():
            # Already in KG? skip (belt+suspenders on top of queue check)
            if self.store.get(concept_id):
                continue

            # Already pending, committed, or rejected?
            if queue.is_blocked(concept_id):
                continue

            dangling_ref_count = len(referencing_nodes)
            if dangling_ref_count < MIN_DANGLING_REFS:
                continue

            chain_unlock_count = self._estimate_chain_unlocks(concept_id, referencing_nodes)
            recent_query_rel   = min(query_counts.get(concept_id, 0), 5)

            gap_score = (
                dangling_ref_count * 3
                + chain_unlock_count * 5
                + recent_query_rel   * 2
            )

            if gap_score < MIN_GAP_SCORE:
                continue

            domain = self._classify_domain(referencing_nodes)
            scored.append(GapScore(
                concept_id         = concept_id,
                gap_score          = gap_score,
                domain             = domain,
                dangling_ref_count = dangling_ref_count,
                chain_unlock_count = chain_unlock_count,
                recent_query_rel   = recent_query_rel,
                referencing_nodes  = referencing_nodes,
            ))

        scored.sort(key=lambda x: -x.gap_score)
        selected = self._apply_domain_balance(scored, max_proposals, max_per_domain)

        log.info(
            "GapAnalyser: %d dangling, %d qualified, %d selected (top-%d)",
            len(dangling), len(scored), len(selected), max_proposals,
        )
        return selected

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _find_dangling(self) -> dict[str, list[str]]:
        """
        Find all concept IDs referenced in KG edges or REQUIRES fields
        that don't exist as KG nodes.

        Returns: {missing_concept_id: [list_of_source_node_ids_that_reference_it]}
        """
        known_ids = set(self.store._nodes.keys())
        dangling: dict[str, list[str]] = {}

        # 1. Outgoing edges: edge["to"] might not exist in known_ids
        for source_id, edges in self.store._edges_from.items():
            for edge in edges:
                target = edge.get("to", "")
                if target and target not in known_ids:
                    dangling.setdefault(target, [])
                    if source_id not in dangling[target]:
                        dangling[target].append(source_id)

        # 2. REQUIRES fields on nodes (parsed from list or pipe-separated string)
        for node_id, node in self.store._nodes.items():
            requires = node.get("requires", [])
            if isinstance(requires, str):
                requires = [r.strip() for r in requires.split("|") if r.strip()]
            for req in requires:
                req = req.lower().replace(" ", "_")
                if req and req not in known_ids:
                    dangling.setdefault(req, [])
                    if node_id not in dangling[req]:
                        dangling[req].append(node_id)

        # Filter out clearly invalid IDs: stopwords, very short strings
        _INVALID = frozenset({"none", "n/a", "unknown", "tbd", "pending", ""})
        return {
            cid: refs for cid, refs in dangling.items()
            if cid not in _INVALID and len(cid) >= 3
        }

    def _estimate_chain_unlocks(
        self,
        concept_id:       str,
        referencing_nodes: list[str],
    ) -> int:
        """
        Heuristic: count how many KNOWN nodes transitively REQUIRE this gap concept.

        For each referencing node that REQUIRES the gap (not just ENABLES/RELATES_TO):
          - count that node itself (+1)
          - count nodes that REQUIRE that node (+N)
        This estimates how many reasoning chains are currently blocked.
        """
        known_ids = set(self.store._nodes.keys())
        count = 0
        requirers = set()

        for node_id in referencing_nodes:
            node = self.store.get(node_id)
            if not node:
                continue
            reqs = node.get("requires", [])
            if isinstance(reqs, str):
                reqs = [r.strip() for r in reqs.split("|") if r.strip()]
            if concept_id in reqs:
                requirers.add(node_id)
                count += 1

        # Second hop: nodes that require the requirers
        for node_id, node in self.store._nodes.items():
            reqs = node.get("requires", [])
            if isinstance(reqs, str):
                reqs = [r.strip() for r in reqs.split("|") if r.strip()]
            if any(r in requirers for r in reqs):
                count += 1

        return count

    def _classify_domain(self, referencing_nodes: list[str]) -> str:
        """
        Classify the gap concept's domain from referencing nodes' layers.
        Returns most-common layer, or "unknown".
        """
        from collections import Counter
        layer_counts: Counter = Counter()
        for node_id in referencing_nodes:
            node = self.store.get(node_id)
            if node:
                layer = node.get("layer") or "unknown"
                layer_counts[str(layer)] += 1
        if not layer_counts:
            return "unknown"
        return layer_counts.most_common(1)[0][0]

    def _apply_domain_balance(
        self,
        scored:        list[GapScore],
        max_proposals: int,
        max_per_domain: int,
    ) -> list[GapScore]:
        """
        Select up to max_proposals with at most max_per_domain from the same domain.
        Iterates sorted list (highest gap_score first) and applies the cap.
        """
        result: list[GapScore] = []
        domain_counts: dict[str, int] = {}

        for gap in scored:
            if len(result) >= max_proposals:
                break
            count = domain_counts.get(gap.domain, 0)
            if count >= max_per_domain:
                continue
            result.append(gap)
            domain_counts[gap.domain] = count + 1

        return result

    def _load_query_counts(self) -> dict[str, int]:
        """
        Load gap_query_counts from flux_meta.json.
        These are the # of times the user asked about a concept that wasn't in the KG.
        Higher count → higher recent_query_relevance → higher gap_score.
        """
        if not META_PATH.exists():
            return {}
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            return meta.get("gap_query_counts", {})
        except Exception:
            return {}

    # ── Introspection ────────────────────────────────────────────────────────

    def dangling_summary(self) -> list[dict]:
        """
        Return all dangling concepts with ref counts, sorted by count DESC.
        Useful for inspection without running the full scored analysis.
        """
        dangling = self._find_dangling()
        result = [
            {"concept": cid, "ref_count": len(refs), "referenced_by": refs}
            for cid, refs in dangling.items()
        ]
        result.sort(key=lambda x: -x["ref_count"])
        return result
