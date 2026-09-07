"""
flux/understanding/query_understanding.py

The QueryUnderstanding layer — Flux's linguistic intelligence.

This is the first thing that touches every query from Akhil.
It sits between raw text and the KG, answering:
  "What is Akhil actually asking me?"

Pipeline:
  1. SlotExtractor  → parse query grammar → raw phrase slots
  2. DomainResolver → infer domain context from sheaf topology
  3. ConceptGrounder → map slots → KG concept IDs via sheaf
  4. Attach Akhil's current context (personalization)
  → StructuredQuery

A StructuredQuery replaces raw text in all intent handlers.
The KG never sees the full query string again after this point.

JARVIS reads Tony's intent from tone, context, history.
QueryUnderstanding reads Akhil's intent from grammar, domain topology,
session history. No hardcoded patches. Everything is structural.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class StructuredQuery:
    """
    The output of QueryUnderstanding.parse().

    Everything a reasoning component needs, pre-computed:
      - grounded_concepts: KG IDs to reason over (no garbage from string matching)
      - gap_slots: phrases Flux doesn't know yet (honest reporting)
      - domain_context: which part of Flux's knowledge is relevant
      - akhil_context: who is Akhil, what's he working on right now

    The raw_text is preserved for display / logging only.
    """
    raw_text:           str
    intent:             str
    # Slot extraction
    slots:              list[str]              # raw phrase slots from grammar
    template_hit:       str                    # which template matched
    slot_confidence:    float                  # how confident slot extraction was
    # Domain inference
    domain_context:     Optional[str]          # primary inferred domain
    domain_scores:      list[tuple[str, float]] = field(default_factory=list)
    # Concept grounding
    grounded_concepts:  list[str]              = field(default_factory=list)  # KG IDs
    gap_slots:          list[str]              = field(default_factory=list)  # no KG match
    protected_slots:    list[str]              = field(default_factory=list)  # agent/user names
    # Personalization
    akhil_context:      dict                   = field(default_factory=dict)

    # ── Convenience accessors ──────────────────────────────────────────────────

    @property
    def has_concepts(self) -> bool:
        return len(self.grounded_concepts) > 0

    @property
    def primary_concept(self) -> Optional[str]:
        return self.grounded_concepts[0] if self.grounded_concepts else None

    @property
    def secondary_concept(self) -> Optional[str]:
        return self.grounded_concepts[1] if len(self.grounded_concepts) > 1 else None

    @property
    def is_cross_domain(self) -> bool:
        """True if grounded concepts span multiple domains."""
        if len(self.domain_scores) >= 2:
            top_two = self.domain_scores[:2]
            # Both domains have meaningful signal
            return top_two[1][1] > 0.4 * top_two[0][1]
        return False

    def summary(self) -> str:
        """Human-readable summary for debugging."""
        lines = [
            f"Intent: {self.intent}",
            f"Template: {self.template_hit}  (confidence={self.slot_confidence:.2f})",
            f"Slots: {self.slots}",
            f"Domain: {self.domain_context}",
            f"Grounded: {self.grounded_concepts}",
        ]
        if self.gap_slots:
            lines.append(f"Gaps: {self.gap_slots}")
        if self.protected_slots:
            lines.append(f"Protected (stripped): {self.protected_slots}")
        return "\n".join(lines)


class QueryUnderstanding:
    """
    Akhil's words → StructuredQuery

    Initialized once, shared across all queries.
    Holds references to sheaf, WorldModel, AkhilModel — all live.

    Protected names and other identity facts come from WorldModel,
    not from Python constants. If Akhil tells Flux to add a protected name,
    it persists in WorldModel and takes effect immediately.
    """

    def __init__(self, sheaf, world_model=None, akhil_model=None):
        """
        Parameters
        ----------
        sheaf       : KGSheaf (built at startup)
        world_model : WorldModel singleton
        akhil_model : AkhilModel (session + domain tracking)
        """
        from .slot_extractor    import SlotExtractor
        from .domain_resolver   import DomainResolver
        from .concept_grounder  import ConceptGrounder

        # Load protected names from WorldModel (living, not hardcoded)
        protected = self._load_protected_names(world_model)

        self._slot_ex  = SlotExtractor(protected_names=protected)
        self._domain   = DomainResolver(sheaf, world_model)
        self._grounder = ConceptGrounder(sheaf, protected_names=protected)
        self._wm       = world_model
        self._akhil    = akhil_model

        log.info("QueryUnderstanding ready (protected names: %s)", protected)

    def parse(self, raw_text: str, intent: str) -> StructuredQuery:
        """
        Full understanding pipeline for one query.

        Step 1: Extract slots (grammar-driven, no KG)
        Step 2: Infer domain context (sheaf topology + Akhil history)
        Step 3: Ground slots → KG concept IDs (sheaf sections)
        Step 4: Attach Akhil's context (personalization)
        """
        akhil_history = self._get_akhil_domain_history()

        # ── Step 1: Slot extraction ────────────────────────────────────────────
        extraction = self._slot_ex.extract(raw_text, intent)
        slots      = extraction.slots

        # ── Step 2: Domain inference ───────────────────────────────────────────
        domain_scores = self._domain.infer_multi(slots)
        domain_ctx    = domain_scores[0][0] if domain_scores else None

        # ── Step 3: Concept grounding ──────────────────────────────────────────
        grounded_concepts: list[str] = []
        gap_slots:         list[str] = []
        protected_slots:   list[str] = []

        grounding_results = self._grounder.ground_all(
            slots,
            domain_context=domain_ctx,
            akhil_domain_history=akhil_history,
        )
        for result in grounding_results:
            if result.is_protected:
                protected_slots.append(result.slot)
            elif result.is_gap:
                gap_slots.append(result.slot)
            elif result.concept_id:
                grounded_concepts.append(result.concept_id)

        # ── Step 4: Akhil's context ────────────────────────────────────────────
        akhil_ctx = self._build_akhil_context()

        sq = StructuredQuery(
            raw_text          = raw_text,
            intent            = intent,
            slots             = slots,
            template_hit      = extraction.template_hit,
            slot_confidence   = extraction.confidence,
            domain_context    = domain_ctx,
            domain_scores     = domain_scores,
            grounded_concepts = grounded_concepts,
            gap_slots         = gap_slots,
            protected_slots   = protected_slots,
            akhil_context     = akhil_ctx,
        )

        log.debug("QueryUnderstanding:\n%s", sq.summary())
        return sq

    # ── Private ────────────────────────────────────────────────────────────────

    def _load_protected_names(self, world_model) -> set[str]:
        """
        Load protected names from WorldModel.
        These are: Flux (agent name), Akhil (user name), any others stored.

        Living: Flux can update WorldModel.self.identity.protected_names at runtime.
        """
        defaults = {"flux", "akhil"}
        if not world_model:
            return defaults
        try:
            # Agent name
            agent_name = world_model.get("self.identity.name", "flux")
            # User name
            user_name  = world_model.get("akhil.name", "akhil")
            # Any additional protected names
            extra      = world_model.get("self.identity.protected_names", [])
            names      = {agent_name.lower(), user_name.lower()} | {n.lower() for n in extra}
            return names | defaults
        except Exception:
            return defaults

    def _get_akhil_domain_history(self) -> list[str]:
        """Get Akhil's recent domain activity.

        Reads from WorldModel directly — domain_history is stored as a list there.
        Falls back to AkhilModel in-memory counts if WorldModel unavailable.
        """
        if self._wm:
            try:
                history = self._wm.get("agents.akhil.domain_history", [])
                if isinstance(history, list):
                    return [d for d in history[-10:] if d and d != "unknown"]
            except Exception:
                pass
        return []

    def _build_akhil_context(self) -> dict:
        """
        Build Akhil's current context for response personalization.
        JARVIS knows Tony's current project — Flux knows Akhil's.

        All data comes from WorldModel (persisted, live) so it accumulates
        across sessions. This is genuine learning from interaction.
        """
        ctx: dict = {
            "name":            "Akhil",
            "is_builder":      True,    # Fundamental: Akhil is building Flux
            "current_domain":  None,
            "session_queries": 0,
            "recent_concepts": [],
            "kg_size":         0,
            "books_learned":   0,
        }
        if self._wm:
            try:
                ctx["kg_size"]       = self._wm.get("knowledge.kg_nodes", 0)
                ctx["books_learned"] = len(self._wm.get("knowledge.books_processed", []) or [])
                ctx["current_domain"] = self._wm.get("agents.akhil.current_focus_domain")
                ctx["session_queries"] = self._wm.get("agents.akhil.session_count_today", 0) or 0
                total = self._wm.get("agents.akhil.total_queries", 0) or 0
                ctx["total_queries"] = total
                predicted = self._wm.get("agents.akhil.predicted_next_query")
                if predicted:
                    ctx["predicted_next"] = predicted
            except Exception:
                pass
        return ctx
