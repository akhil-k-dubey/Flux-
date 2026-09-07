"""
flux/training/book_trainer/dependency_mapper.py

THE LEARNING ENGINE — generates KG edges from extracted concepts.

This is where memorization becomes understanding.
A concept without edges is a dictionary entry.
A concept with edges is something Flux can REASON with.

Edge types generated:
  REQUIRES        → concept A needs concept B to make sense
  ENABLES         → knowing A makes B accessible
  ANALOGOUS_TO    → A and B are formally similar (cross-domain insight)
  PROVES          → A is a consequence of B
  APPLIES_TO      → A is used in context B
  GENERALIZES     → A is a special case of B
  CONTRADICTS     → A conflicts with B (rare but important)
  CONNECTS        → A and B are related (weaker, bidirectional)

The ANALOGOUS_TO edge is the most powerful: it fires when a concept
in one domain is formally similar to a concept in another.
Entropy ~~ANALOGOUS_TO~~ information_entropy is why Flux can reason
about Maxwell's demon using Shannon's framework.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .concept_extractor import ExtractedConcept

log = logging.getLogger(__name__)


@dataclass
class KGEdge:
    """A directed edge between two KG concepts."""
    from_id:   str
    to_id:     str
    edge_type: str   # REQUIRES, ENABLES, ANALOGOUS_TO, etc.
    reason:    str   # Why this edge was inferred (for Flux's self-explanation)
    weight:    float = 1.0
    source:    str   = "book_training"


# Cross-domain analogies: when a concept in domain A appears in domain B,
# create an ANALOGOUS_TO edge. These are the most intellectually valuable edges.
_CROSS_DOMAIN_ANALOGIES: list[tuple[str, str, str, str]] = [
    # (pattern_A, domain_A, pattern_B, domain_B)  → ANALOGOUS_TO
    ("entropy",              "thermodynamics",    "information_entropy",  "information_theory"),
    ("entropy",              "thermodynamics",    "kl_divergence",        "information_theory"),
    ("boltzmann_distribution","thermodynamics",   "softmax",              "machine_learning"),
    ("partition_function",   "thermodynamics",    "normalizing_constant", "probability_theory"),
    ("free_energy",          "thermodynamics",    "variational_inference", "machine_learning"),
    ("phase_transition",     "thermodynamics",    "loss_landscape",       "machine_learning"),
    ("eigenvalue",           "linear_algebra",    "singular_value",       "linear_algebra"),
    ("gradient",             "mathematics",       "gradient_descent",     "machine_learning"),
    ("fourier_transform",    "mathematics",       "attention_mechanism",  "machine_learning"),
    ("markov_chain",         "probability_theory","markov_decision_process","reinforcement_learning"),
    ("mutual_information",   "information_theory","feature_selection",    "machine_learning"),
    ("compression",          "information_theory","generalization",       "machine_learning"),
    ("hilbert_space",        "quantum_mechanics", "reproducing_kernel_hilbert_space","machine_learning"),
    ("density_matrix",       "quantum_mechanics", "probability_distribution","probability_theory"),
    ("least_action",         "physics",           "optimization",         "machine_learning"),
    ("manifold",             "mathematics",       "latent_space",         "machine_learning"),
    ("category",             "abstract_algebra",  "type",                 "type_theory"),
    ("functor",              "abstract_algebra",  "map",                  "functional_programming"),
]

# Prerequisite signals in definition text
_REQUIRES_SIGNALS = [
    "requires", "assumes", "recall that", "using the notion of",
    "we need", "presupposes", "is defined in terms of", "based on",
    "following from", "given", "assuming",
]

# Enablement signals
_ENABLES_SIGNALS = [
    "allows us to", "makes it possible", "enables", "we can then",
    "this leads to", "from which we derive", "yields", "implies",
]

# Generalisation signals
_GENERALIZES_SIGNALS = [
    "generalizes", "is a special case of", "is a generalization of",
    "extends", "in the general case",
]

# Proof/consequence signals
_PROVES_SIGNALS = [
    "proves", "follows from", "is a consequence of", "derived from",
    "entails", "implies", "we can show",
]


class DependencyMapper:
    """
    Generates KG edges for a set of extracted concepts.

    Three sources of edges:
    1. Explicit: from dependency signals in definition text
    2. Structural: from chapter ordering (later chapters require earlier)
    3. Cross-domain: from the analogy table above
    """

    def __init__(self, existing_kg_ids: set[str] | None = None):
        """
        Parameters
        ----------
        existing_kg_ids : set of concept IDs already in the KG
                          (used to link new concepts to existing ones)
        """
        self.existing_ids = existing_kg_ids or set()

    def map_edges(
        self,
        concepts: list[ExtractedConcept],
        book_domain: str,
    ) -> list[KGEdge]:
        """
        Generate all edges for a set of extracted concepts.

        Parameters
        ----------
        concepts    : concepts extracted from a book
        book_domain : primary domain of the book

        Returns
        -------
        list of KGEdge
        """
        edges: list[KGEdge] = []
        concept_ids = {c.concept_id for c in concepts}
        all_ids = concept_ids | self.existing_ids

        for concept in concepts:
            # 1. Explicit dependency edges from definition text
            edges.extend(self._explicit_edges(concept, all_ids))

            # 2. Edges from formula symbol links
            edges.extend(self._formula_edges(concept, all_ids))

            # 3. Chapter ordering: later = more complex = depends on earlier
            edges.extend(self._structural_edges(concept, concepts))

            # 4. Cross-domain analogy detection
            edges.extend(self._analogy_edges(concept, all_ids))

            # 5. Link to existing KG concepts by name similarity
            edges.extend(self._existing_kg_links(concept))

        # Deduplicate
        seen: set[tuple[str, str, str]] = set()
        unique: list[KGEdge] = []
        for e in edges:
            key = (e.from_id, e.to_id, e.edge_type)
            if key not in seen and e.from_id != e.to_id:
                seen.add(key)
                unique.append(e)

        log.info(
            "DependencyMapper: %d concepts → %d edges",
            len(concepts), len(unique)
        )
        return unique

    # ── Private ────────────────────────────────────────────────────────────────

    def _explicit_edges(self, concept: ExtractedConcept, all_ids: set[str]) -> list[KGEdge]:
        """Extract edges from explicit dependency signals in definition text."""
        edges: list[KGEdge] = []
        text = concept.definition.lower()

        # REQUIRES edges
        for signal in _REQUIRES_SIGNALS:
            if signal in text:
                # Find what's being required
                for dep_id in concept.depends_on:
                    if dep_id in all_ids:
                        edges.append(KGEdge(
                            from_id   = concept.concept_id,
                            to_id     = dep_id,
                            edge_type = "REQUIRES",
                            reason    = f"Definition of '{concept.name}' uses '{signal}' + {dep_id}",
                            weight    = 1.0,
                        ))

        # PROVES edges (theorem → theorem/lemma)
        if concept.concept_type in ("theorem", "corollary"):
            for signal in _PROVES_SIGNALS:
                if signal in text:
                    for dep_id in concept.depends_on:
                        if dep_id in all_ids:
                            edges.append(KGEdge(
                                from_id   = dep_id,
                                to_id     = concept.concept_id,
                                edge_type = "PROVES",
                                reason    = f"'{concept.name}' follows from {dep_id}",
                                weight    = 0.9,
                            ))

        # GENERALIZES edges
        for signal in _GENERALIZES_SIGNALS:
            if signal in text:
                m = re.search(
                    signal + r"\s+([a-z_\s]+?)(?:\.|\s+and|\s+for|\s+when)",
                    text
                )
                if m:
                    target = m.group(1).strip().replace(" ", "_")
                    if target in all_ids:
                        edges.append(KGEdge(
                            from_id   = concept.concept_id,
                            to_id     = target,
                            edge_type = "GENERALIZES",
                            reason    = f"'{concept.name}' generalizes {target}",
                            weight    = 0.85,
                        ))

        # ENABLES edges (from this concept)
        for signal in _ENABLES_SIGNALS:
            if signal in text:
                # Can't always identify what's enabled — add a soft CONNECTS edge
                # to concepts mentioned in the same paragraph
                for dep_id in concept.depends_on[:2]:
                    if dep_id in all_ids:
                        edges.append(KGEdge(
                            from_id   = concept.concept_id,
                            to_id     = dep_id,
                            edge_type = "ENABLES",
                            reason    = f"'{concept.name}' enables {dep_id} (via '{signal}')",
                            weight    = 0.75,
                        ))

        return edges

    def _formula_edges(self, concept: ExtractedConcept, all_ids: set[str]) -> list[KGEdge]:
        """Generate REQUIRES edges from formula symbol mappings."""
        edges: list[KGEdge] = []
        for formula in concept.formulas:
            for symbol, referenced_concept_id in formula.symbols.items():
                if referenced_concept_id in all_ids and referenced_concept_id != concept.concept_id:
                    edges.append(KGEdge(
                        from_id   = concept.concept_id,
                        to_id     = referenced_concept_id,
                        edge_type = "REQUIRES",
                        reason    = (
                            f"Formula for '{concept.name}' uses symbol '{symbol}' "
                            f"which maps to {referenced_concept_id}"
                        ),
                        weight    = 0.95,
                        source    = "formula_symbol",
                    ))
        return edges

    def _structural_edges(
        self,
        concept: ExtractedConcept,
        all_concepts: list[ExtractedConcept],
    ) -> list[KGEdge]:
        """
        Later concepts in the same chapter often build on earlier ones.
        Create weak CONNECTS edges for concepts in the same section.
        """
        edges: list[KGEdge] = []
        # Find concepts from the same chapter that appear BEFORE this one
        for other in all_concepts:
            if other.concept_id == concept.concept_id:
                break   # stop when we reach ourselves
            if (other.chapter == concept.chapter
                    and other.source_page < concept.source_page
                    and abs(concept.source_page - other.source_page) <= 5):
                edges.append(KGEdge(
                    from_id   = concept.concept_id,
                    to_id     = other.concept_id,
                    edge_type = "REQUIRES",
                    reason    = (
                        f"'{concept.name}' appears after '{other.name}' "
                        f"in chapter '{concept.chapter}' (structural)"
                    ),
                    weight    = 0.60,   # low weight — structural inference
                    source    = "structural",
                ))
        return edges

    def _analogy_edges(self, concept: ExtractedConcept, all_ids: set[str]) -> list[KGEdge]:
        """
        Detect cross-domain analogies using the pre-defined analogy table.
        Creates ANALOGOUS_TO edges — the most intellectually valuable edges.
        """
        edges: list[KGEdge] = []
        cid = concept.concept_id

        for pat_a, dom_a, pat_b, dom_b in _CROSS_DOMAIN_ANALOGIES:
            # Check if THIS concept matches pattern_A
            if pat_a in cid or pat_a.replace("_", " ") in concept.name.lower():
                if pat_b in all_ids:
                    edges.append(KGEdge(
                        from_id   = cid,
                        to_id     = pat_b,
                        edge_type = "ANALOGOUS_TO",
                        reason    = (
                            f"'{concept.name}' ({dom_a}) is formally analogous to "
                            f"'{pat_b}' ({dom_b}) — cross-domain bridge"
                        ),
                        weight    = 0.88,
                        source    = "analogy_table",
                    ))

            # Check if THIS concept matches pattern_B  
            if pat_b in cid or pat_b.replace("_", " ") in concept.name.lower():
                if pat_a in all_ids:
                    edges.append(KGEdge(
                        from_id   = cid,
                        to_id     = pat_a,
                        edge_type = "ANALOGOUS_TO",
                        reason    = (
                            f"'{concept.name}' ({dom_b}) is formally analogous to "
                            f"'{pat_a}' ({dom_a}) — cross-domain bridge"
                        ),
                        weight    = 0.88,
                        source    = "analogy_table",
                    ))

        return edges

    def _existing_kg_links(self, concept: ExtractedConcept) -> list[KGEdge]:
        """
        Check if any existing KG concept is referenced by name in this definition.
        Creates CONNECTS edges to bridge book knowledge with existing KG.
        """
        edges: list[KGEdge] = []
        text = concept.definition.lower()
        name_lower = concept.name.lower()

        for existing_id in self.existing_ids:
            # Convert ID to name for text matching
            existing_name = existing_id.replace("_", " ")
            if (len(existing_name) > 4
                    and existing_name in text
                    and existing_name not in name_lower):
                edges.append(KGEdge(
                    from_id   = concept.concept_id,
                    to_id     = existing_id,
                    edge_type = "CONNECTS",
                    reason    = (
                        f"Definition of '{concept.name}' mentions existing "
                        f"KG concept '{existing_name}'"
                    ),
                    weight    = 0.70,
                    source    = "existing_kg_reference",
                ))

        return edges
