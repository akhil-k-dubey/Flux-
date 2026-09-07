"""
flux/understanding/domain_resolver.py

Infers the domain context of a query from WorldModel + KGSheaf topology.

NO hardcoded domain keyword lists.
Domain inference is driven entirely by KG structure:
  - Which domains does the sheaf's word index associate with the slot words?
  - Which domains are most active in Akhil's recent sessions?
  - What do the slot words' sections say about domain?

JARVIS doesn't have a hardcoded list of "physics words" — he knows what domain
a question is about because he has a rich model of what each domain IS.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

log = logging.getLogger(__name__)


class DomainResolver:
    """
    Infers query domain context from KGSheaf topology + Akhil's session history.

    No hardcoded keyword→domain mappings.
    Derives domain signal from:
      1. Which sections does the sheaf find for the slot words?  (structural)
      2. Which domains appear most in those sections?            (vote-by-topology)
      3. Akhil's recent domain activity                         (personalization)
    """

    def __init__(self, sheaf, world_model=None):
        """
        Parameters
        ----------
        sheaf       : KGSheaf instance
        world_model : WorldModel instance (for Akhil's session context)
        """
        self.sheaf  = sheaf
        self._wm    = world_model

    def infer(
        self,
        slots: list[str],
        akhil_domain_history: Optional[list[str]] = None,
    ) -> Optional[str]:
        """
        Infer the most likely domain context for this query.

        Parameters
        ----------
        slots               : raw phrase slots (not yet grounded)
        akhil_domain_history: Akhil's recent domain activity

        Returns the most likely domain string, or None.
        """
        domain_votes: Counter = Counter()

        for slot in slots:
            slot_clean = slot.lower().strip()
            sections   = self.sheaf.find_sections_for_word(slot_clean)
            if not sections:
                # Try underscore form
                sections = self.sheaf.find_sections_for_word(
                    slot_clean.replace(" ", "_")
                )
            for sec in sections[:3]:           # top 3 sections for this slot
                if sec.domain and sec.domain != "unknown":
                    # Weight by section confidence
                    domain_votes[sec.domain] += sec.confidence

        # Boost domains Akhil has been exploring recently
        if akhil_domain_history:
            for i, domain in enumerate(reversed(akhil_domain_history[-5:])):
                # More recent = higher boost
                boost = 0.3 * (i + 1) / 5
                domain_votes[domain] += boost

        if not domain_votes:
            return None

        winner, score = domain_votes.most_common(1)[0]
        log.debug(
            "DomainResolver: inferred '%s' (score=%.2f) from slots=%s",
            winner, score, slots
        )
        return winner

    def infer_multi(self, slots: list[str]) -> list[tuple[str, float]]:
        """
        Return ordered list of (domain, score) for all domains with signal.
        Useful when a query spans multiple domains (synthesis queries).
        """
        domain_votes: Counter = Counter()

        for slot in slots:
            slot_clean = slot.lower().strip()
            for form in [slot_clean, slot_clean.replace(" ", "_")]:
                for sec in self.sheaf.find_sections_for_word(form)[:3]:
                    if sec.domain and sec.domain != "unknown":
                        domain_votes[sec.domain] += sec.confidence

        return domain_votes.most_common()
