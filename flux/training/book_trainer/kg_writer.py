"""
flux/training/book_trainer/kg_writer.py

Writes extracted concepts and edges into the KGStore.

The KG is the ground truth. This is the "knowledge acquisition" step.
After writing, every concept is immediately queryable by Flux.

Edge integrity:
  - Only writes edges where BOTH endpoints exist in the KG
  - Verifies concept IDs before writing
  - Logs every write for auditability
  - Skips concepts already in KG (no overwrites unless definition is longer)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flux.kg.store import KGStore
from .concept_extractor import ExtractedConcept
from .dependency_mapper  import KGEdge

log = logging.getLogger(__name__)


class KGWriter:
    """
    Writes extracted book concepts and edges to KGStore.

    Respects KG integrity:
      - No duplicate IDs (updates definition if new is longer)
      - No dangling edges (only writes if both endpoints exist)
      - All writes logged to logs/book_training.jsonl
    """

    def __init__(self, store: KGStore):
        self.store   = store
        self._log_path = Path("logs/book_training.jsonl")
        self._log_path.parent.mkdir(exist_ok=True)
        self._written_concepts = 0
        self._written_edges    = 0
        self._skipped          = 0

    def write_concepts(self, concepts: list[ExtractedConcept]) -> int:
        """
        Write a list of extracted concepts to the KGStore.
        Returns number of new concepts written.
        """
        written = 0
        for concept in concepts:
            try:
                if self._write_concept(concept):
                    written += 1
            except Exception as exc:
                log.warning("KGWriter: failed to write '%s': %s", concept.concept_id, exc)
        self._written_concepts += written
        return written

    def write_concepts_with_edges(
        self,
        concepts: list[ExtractedConcept],
        edges: list[KGEdge],
    ) -> tuple[int, int]:
        """
        Write concepts and their edges in a single batched pass.
        Groups edges by concept so each store.write() call includes edges.
        Returns (concepts_written, edges_written).
        """
        # Group edges by from_id
        from collections import defaultdict
        edges_by_from: dict[str, list[dict]] = defaultdict(list)
        all_ids_after = set(self.store.all_ids())

        # First pass: add concept IDs we're about to write
        for c in concepts:
            all_ids_after.add(c.concept_id)

        # Pre-filter edges to avoid dangling references
        for edge in edges:
            if edge.from_id in all_ids_after and edge.to_id in all_ids_after:
                edges_by_from[edge.from_id].append({
                    "from":   edge.from_id,
                    "to":     edge.to_id,
                    "type":   edge.edge_type,
                    "weight": edge.weight,
                    "reason": edge.reason,
                })

        concepts_written = 0
        edges_written    = 0

        for concept in concepts:
            try:
                existing = self.store.get(concept.concept_id)
                if existing:
                    old_def = existing.get("definition", "")
                    if len(concept.definition) <= len(old_def) + 50:
                        self._skipped += 1
                        # Still write any new edges for this concept
                        outgoing = edges_by_from.get(concept.concept_id, [])
                        if outgoing:
                            self.store.write(existing, outgoing)
                            edges_written += len(outgoing)
                        continue

                formula_strs = [f.cleaned for f in concept.formulas if f.cleaned]
                definition   = concept.definition.strip()
                if formula_strs:
                    definition += "\n\nFormulas: " + " | ".join(formula_strs[:3])

                node = {
                    "id":         concept.concept_id,
                    "type":       "CONCEPT",
                    "layer":      self._assign_layer(concept),
                    "definition": definition,
                    "confidence": concept.confidence,
                    "immutable":  concept.immutable,
                    "trainable":  True,
                    "source":     f"{concept.source_book}:p{concept.source_page}",
                    "domain":     concept.domain,
                    "chapter":    concept.chapter or "",
                    "specs":      {},
                    "examples":   [],
                    "non_examples": [],
                }

                outgoing = edges_by_from.get(concept.concept_id, [])
                self.store.write(node, outgoing)
                self._log_write("concept", concept.concept_id, concept.source_book)
                concepts_written += 1
                edges_written    += len(outgoing)

            except Exception as exc:
                log.warning("KGWriter: failed '%s': %s", concept.concept_id, exc)

        self._written_concepts += concepts_written
        self._written_edges    += edges_written
        return concepts_written, edges_written

    def write_concepts(self, concepts: list[ExtractedConcept]) -> int:
        """Write concepts without edges. Returns number written."""
        written, _ = self.write_concepts_with_edges(concepts, [])
        return written

    def write_edges(self, edges: list[KGEdge]) -> int:
        """Write edges for existing concepts. Returns number written."""
        all_ids  = set(self.store.all_ids())
        written  = 0
        for edge in edges:
            if edge.from_id not in all_ids or edge.to_id not in all_ids:
                continue
            try:
                node = self.store.get(edge.from_id)
                if not node:
                    continue
                edge_dict = {
                    "from":   edge.from_id,
                    "to":     edge.to_id,
                    "type":   edge.edge_type,
                    "weight": edge.weight,
                    "reason": edge.reason,
                }
                self.store.write(node, [edge_dict])
                written += 1
                self._log_write("edge", f"{edge.from_id}→{edge.to_id}", edge.edge_type)
            except Exception as exc:
                log.warning("KGWriter: edge %s→%s: %s", edge.from_id, edge.to_id, exc)
        self._written_edges += written
        return written

    # ── Private ────────────────────────────────────────────────────────────────

    def _assign_layer(self, concept: ExtractedConcept) -> str:
        """Assign KG layer based on concept type and domain."""
        if concept.immutable:
            return "LAYER_0"   # axioms and theorems are foundational
        if concept.domain in ("mathematics", "linear_algebra", "abstract_algebra", "type_theory"):
            return "LAYER_1"
        if concept.domain in ("information_theory", "probability_theory"):
            return "LAYER_2"
        if concept.domain in ("thermodynamics", "quantum_mechanics", "statistical_mechanics",
                              "general_relativity"):
            return "LAYER_2"
        if concept.domain in ("machine_learning", "reinforcement_learning", "probabilistic_ml"):
            return "LAYER_3"
        if concept.domain in ("algorithms", "compiler_theory", "operating_systems"):
            return "LAYER_3"
        return "LAYER_4"

    def _log_write(self, wtype: str, item_id: str, source: str) -> None:
        """Append a write record to the book training log."""
        record = {
            "type":      wtype,
            "id":        item_id,
            "source":    source,
            "timestamp": time.time(),
        }
        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass
