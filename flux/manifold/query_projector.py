"""
flux/manifold/query_projector.py

QueryProjector: maps a natural language query → ManifoldQuery.

Uses the trained OperationClassifier for operation routing and
KG substring matching for concept grounding. No regex.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from flux.manifold.causal_manifold import CausalManifold
    from flux.kg.sheaf import KGSheaf
    from flux.kg.store import KGStore

from flux.manifold.operation_classifier import (
    classify_operation, find_concepts_in_query, detect_direction,
)

log = logging.getLogger(__name__)


@dataclass
class ManifoldQuery:
    operation:  str
    source_id:  Optional[str]
    target_id:  Optional[str] = None
    direction:  float = -1.0
    domain_a:   Optional[str] = None
    domain_b:   Optional[str] = None
    confidence: float = 1.0
    notes:      list[str] = field(default_factory=list)


class QueryProjector:
    def __init__(self, manifold: "CausalManifold", sheaf: "KGSheaf", store: "KGStore"):
        self.manifold = manifold
        self.sheaf    = sheaf
        self.store    = store

    def project(self, query_text: str,
                intent_hint: Optional[str] = None) -> Optional[ManifoldQuery]:
        if not self.manifold._built:
            return None

        # Operation classification
        op_result = classify_operation(query_text)
        op        = op_result.operation
        conf      = op_result.confidence

        # Concept grounding — KG substring match, manifold-verified
        concepts = find_concepts_in_query(query_text, self.store)
        concepts = [c for c in concepts if c in self.manifold._concept_index]

        source_id = concepts[0] if len(concepts) > 0 else None
        target_id = concepts[1] if len(concepts) > 1 else None

        if source_id is None:
            return None  # can't do anything without a grounded source

        direction = detect_direction(query_text) if op == "PERTURB" else -1.0

        # Infer domains for GAP
        domain_a = domain_b = None
        if op == "GAP":
            domain_a = (self.store.get(source_id) or {}).get("domain") or source_id
            domain_b = (self.store.get(target_id) or {}).get("domain") or target_id if target_id else None

        return ManifoldQuery(
            operation  = op,
            source_id  = source_id,
            target_id  = target_id,
            direction  = direction,
            domain_a   = domain_a,
            domain_b   = domain_b,
            confidence = conf,
        )
