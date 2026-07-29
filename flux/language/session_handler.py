"""
flux/language/session_handler.py

Manages multi-turn conversation state for Stage 5.

Responsibilities:
  - Maintain a context stack of the last 5 primary concept IDs
  - Resolve pronouns ("it", "that", "this") and domain nouns ("the equation")
    to the most recently discussed concept
  - Track pending clarification state (next turn resolves the clarification)
  - Count repeated gap queries per concept (Q8: TEACH suggestion threshold)
  - Assign session_id and turn_id
  - Persist gap_query_counts and synonym_gaps to flux_meta.json

Design decisions (Q5, Q7, Q8):
  - Pure pronouns resolve to last primary concept in context stack
  - Domain nouns (e.g., "the equation") resolve to most recent concept
    whose type matches the noun's type filter
  - Clarification requests extend context, not reset it (Q5)
  - TEACH_HINT suggestion triggered after 2+ gap queries for same concept (Q8)

Session IDs are UUID4 strings, generated fresh per FluxInterface instantiation.
Turn IDs increment within a session.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

META_PATH = Path("logs/flux_meta.json")

# ── Pronoun → resolution trigger ───────────────────────────────────────────────
_PURE_PRONOUNS = re.compile(
    r"\b(it|that|this|they|them|those|these|the concept|the idea|the topic)\b", re.I
)

# Domain nouns map phrase → list of concept types that match
# When pronoun is "the equation", find most recent concept with type in ["MATHEMATICAL"]
DOMAIN_NOUNS: dict[str, list[str]] = {
    "the equation":     ["MATHEMATICAL", "AXIOM", "LOGICAL"],
    "the function":     ["MATHEMATICAL"],
    "the law":          ["AXIOM", "PHYSICAL", "LOGICAL"],
    "the theorem":      ["MATHEMATICAL", "LOGICAL"],
    "the principle":    ["PHYSICAL", "LOGICAL", "AXIOM"],
    "the constant":     ["MATHEMATICAL"],
    "the model":        ["CONCEPT", "COMPUTATIONAL"],
    "the distribution": ["MATHEMATICAL", "PHYSICAL"],
    "the network":      ["COMPUTATIONAL", "BIOLOGICAL"],
    "the cycle":        ["PHYSICAL"],
    "the process":      ["PHYSICAL", "COMPUTATIONAL"],
    "the algorithm":    ["COMPUTATIONAL"],
    "the formula":      ["MATHEMATICAL", "PHYSICAL"],
}

# Domain phrase → anchor concept IDs (Q4a)
# Used for GAP queries about broad domains ("quantum mechanics", "thermodynamics")
DOMAIN_ANCHORS: dict[str, list[str]] = {
    "quantum mechanics":    ["schrodinger_equation", "wavefunction", "superposition",
                             "wave_function_collapse", "eigenstate"],
    "quantum":              ["schrodinger_equation", "wavefunction", "superposition"],
    "thermodynamics":       ["entropy", "enthalpy", "heat", "boltzmann_distribution",
                             "internal_energy", "carnot_cycle"],
    "thermo":               ["entropy", "enthalpy", "heat"],
    "information theory":   ["information_entropy", "kolmogorov_complexity",
                             "channel_capacity", "kl_divergence"],
    "information":          ["information_entropy", "kolmogorov_complexity"],
    "machine learning":     ["attention_mechanism", "transformer_architecture",
                             "gradient_descent", "loss_function"],
    "ml":                   ["attention_mechanism", "transformer_architecture"],
    "deep learning":        ["attention_mechanism", "transformer_architecture",
                             "recurrent_neural_network"],
    "neural":               ["hebbian_learning", "neural_plasticity", "synaptic_transmission"],
    "neuroscience":         ["hebbian_learning", "neural_plasticity",
                             "predictive_coding", "synaptic_transmission"],
    "electromagnetism":     ["maxwell_equations", "faradays_law",
                             "coulombs_law", "electric_field"],
    "statistical mechanics": ["boltzmann_distribution", "partition_function",
                              "fermi_dirac_statistics"],
    "statistics":           ["boltzmann_distribution", "partition_function"],
    "logic":                ["kg_true", "kg_false", "implies", "kg_and", "kg_or", "kg_not"],
    "calculus":             ["gradient_vector", "curl_and_divergence", "partial_derivative"],
    "lumen":                ["lumen_fsm", "lumen_latency_budget"],
}

TEACH_SUGGESTION_THRESHOLD = 2   # Q8: suggest TEACH after this many gap queries


class SessionHandler:
    """
    Manages multi-turn conversation context.
    One instance lives inside FluxInterface for the lifetime of a session.
    """

    def __init__(self, store=None) -> None:
        self.store               = store
        self.session_id: str     = str(uuid.uuid4())[:8]   # short 8-char ID
        self.turn_id:    int     = 0
        self.context_stack: deque[str] = deque(maxlen=5)   # last 5 concept IDs
        self.pending_clarification: bool       = False
        self.clarification_guesses: list[str]  = []
        self.gap_query_counts: dict[str, int]  = {}
        self._load_from_meta()

    # ── Context resolution ─────────────────────────────────────────────────────

    def resolve_context(self, text: str) -> str:
        """
        Replace pronouns and domain nouns in text with the most recently
        discussed concept ID from context_stack.
        Returns the resolved text (unchanged if no resolution applies).
        """
        if not self.context_stack:
            return text

        resolved = text

        # Check domain nouns first (more specific than pure pronouns)
        for phrase, types in DOMAIN_NOUNS.items():
            if phrase.lower() in text.lower():
                match = self._find_typed_concept(types)
                if match:
                    resolved = re.sub(
                        re.escape(phrase), match,
                        resolved, flags=re.I, count=1,
                    )
                    log.debug("Resolved domain noun %r -> %r", phrase, match)

        # Pure pronoun resolution → most recent primary concept
        if _PURE_PRONOUNS.search(resolved):
            last = self.context_stack[-1]
            resolved = _PURE_PRONOUNS.sub(last, resolved, count=1)
            log.debug("Resolved pronoun -> %r", last)

        return resolved

    def resolve_clarification(self, text: str) -> str:
        """
        When pending_clarification is True, treat the input as an answer
        to the clarification question. Returns the best-matching guess
        as a normalized concept query.
        """
        if not self.clarification_guesses:
            self.pending_clarification = False
            return text

        # Check if user's input matches one of the guesses
        text_low = text.lower().strip()
        for guess in self.clarification_guesses:
            if guess.lower() in text_low or text_low in guess.lower():
                self.pending_clarification = False
                self.clarification_guesses = []
                return f"tell me about {guess}"   # re-issue as clean EXPLAIN

        # User gave a new phrase — treat it as fresh input
        self.pending_clarification = False
        self.clarification_guesses = []
        return text

    # ── State updates ──────────────────────────────────────────────────────────

    def update(self, primary_concept_id: str | None, is_clarification: bool = False) -> None:
        """
        Update context stack after a successful query.
        Clarification requests extend context; they don't replace it.
        """
        self.turn_id += 1
        if primary_concept_id and not is_clarification:
            self.context_stack.append(primary_concept_id)

    def set_pending_clarification(self, guesses: list[str]) -> None:
        self.pending_clarification = True
        self.clarification_guesses = guesses

    def clear_clarification(self) -> None:
        self.pending_clarification = False
        self.clarification_guesses = []

    # ── Gap query tracking (Q8) ────────────────────────────────────────────────

    def record_gap_query(self, concept_id: str) -> int:
        """
        Increment gap query count for concept_id.
        Returns the new count — caller checks against TEACH_SUGGESTION_THRESHOLD.
        Persists to flux_meta.json.
        """
        self.gap_query_counts[concept_id] = self.gap_query_counts.get(concept_id, 0) + 1
        count = self.gap_query_counts[concept_id]
        self._save_gap_counts()
        return count

    def should_suggest_teach(self, concept_id: str) -> bool:
        """Return True if concept has been queried as unknown 2+ times."""
        return self.gap_query_counts.get(concept_id, 0) >= TEACH_SUGGESTION_THRESHOLD

    # ── Domain anchor lookup (Q4a) ─────────────────────────────────────────────

    @staticmethod
    def get_domain_anchors(domain_text: str) -> list[str]:
        """
        Map a domain phrase to its anchor concept IDs.
        'quantum mechanics' → ['schrodinger_equation', 'wavefunction', ...]
        Returns [] if no mapping known.
        """
        low = domain_text.lower().strip()
        # Try exact match first, then substring
        if low in DOMAIN_ANCHORS:
            return DOMAIN_ANCHORS[low]
        for key, anchors in DOMAIN_ANCHORS.items():
            if key in low or low in key:
                return anchors
        return []

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _find_typed_concept(self, types: list[str]) -> str | None:
        """
        Find the most recent concept in context_stack whose type matches
        any of the given type strings. Iterates stack from most recent.
        """
        if not self.store:
            # No store — fall back to most recent concept
            return self.context_stack[-1] if self.context_stack else None

        for concept_id in reversed(list(self.context_stack)):
            node = self.store.get(concept_id)
            if node and node.get("type", "").upper() in [t.upper() for t in types]:
                return concept_id
        # No typed match — fall back to most recent
        return self.context_stack[-1] if self.context_stack else None

    def _load_from_meta(self) -> None:
        """Load persisted gap query counts from flux_meta.json."""
        try:
            if META_PATH.exists():
                meta = json.loads(META_PATH.read_text(encoding="utf-8"))
                self.gap_query_counts = meta.get("gap_query_counts", {})
        except Exception as exc:
            log.warning("Could not load session state from flux_meta.json: %s", exc)

    def _save_gap_counts(self) -> None:
        """Persist gap query counts to flux_meta.json."""
        try:
            meta: dict = {}
            if META_PATH.exists():
                meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            meta["gap_query_counts"] = self.gap_query_counts
            META_PATH.parent.mkdir(parents=True, exist_ok=True)
            META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not save gap counts to flux_meta.json: %s", exc)
