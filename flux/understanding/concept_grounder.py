"""
flux/understanding/concept_grounder.py

Maps raw phrase slots → KG concept IDs.

Grounding rule: a slot "X" grounds to concept C if C's ID or label matches X
as a whole word or substring — NOT if X appears in C's definition text.

Uses find_concepts_in_query (Phases 1/2/3 matcher) for consistency everywhere.
If a slot doesn't match any concept ID/label → honest gap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class GroundingResult:
    slot:        str
    concept_id:  str | None
    domain:      str | None
    confidence:  float
    is_gap:      bool
    is_protected: bool
    alternatives: list[str]


class ConceptGrounder:
    """
    Maps raw phrase slots → KG concept IDs via KG-ID boundary matching.

    Strategy:
      1. Protected-name check (from WorldModel)
      2. find_concepts_in_query over the slot text:
           - Multi-word: substring match on normalised form
           - Single-word: exact word-boundary match (≥4 chars, non-constitutional)
           - ALIAS_OF resolution: "shannon entropy" → "information_entropy"
      3. Domain-aware disambiguation when multiple matches exist
      4. No match → gap (honest)
    """

    def __init__(self, sheaf, protected_names: set[str]):
        self._store     = sheaf.store
        self._protected = {n.lower() for n in protected_names}

    def ground(
        self,
        slot: str,
        domain_context: Optional[str] = None,
        akhil_domain_history: Optional[list[str]] = None,
    ) -> GroundingResult:
        from flux.manifold.operation_classifier import find_concepts_in_query

        slot_clean = slot.strip().lower()

        # 1. Protected name check
        if slot_clean in self._protected:
            return GroundingResult(
                slot=slot, concept_id=None, domain=None,
                confidence=1.0, is_gap=False, is_protected=True,
                alternatives=[],
            )

        # 2. KG-ID boundary match — same function as Phases 1/2/3
        matched = find_concepts_in_query(slot, self._store, max_results=3)

        if not matched:
            log.debug("ConceptGrounder: '%s' → gap (no KG ID match)", slot)
            return GroundingResult(
                slot=slot, concept_id=None, domain=None,
                confidence=0.0, is_gap=True, is_protected=False,
                alternatives=[],
            )

        # 3. Domain-aware disambiguation
        best = matched[0]
        if len(matched) > 1 and domain_context:
            for cid in matched:
                if (self._store.get(cid) or {}).get("domain") == domain_context:
                    best = cid
                    break

        node   = self._store.get(best) or {}
        domain = node.get("domain")

        log.debug("ConceptGrounder: '%s' → %s (domain=%s)", slot, best, domain)

        return GroundingResult(
            slot=slot, concept_id=best, domain=domain,
            confidence=1.0, is_gap=False, is_protected=False,
            alternatives=matched[1:],
        )

    def ground_all(
        self,
        slots: list[str],
        domain_context: Optional[str] = None,
        akhil_domain_history: Optional[list[str]] = None,
    ) -> list[GroundingResult]:
        return [
            self.ground(slot, domain_context, akhil_domain_history)
            for slot in slots
        ]
