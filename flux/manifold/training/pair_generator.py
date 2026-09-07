"""
flux/manifold/training/pair_generator.py

Generates training pairs from KG edges for manifold embedding.

Zero labeled data needed — every KG edge is a free training signal.
With 1724 edges × ~3 query templates each = ~5,000 training pairs.

Two types of training pairs:
  1. Embedding pairs: (concept_a, concept_b, should_be_close)
     Causal pairs (CAUSES/REQUIRES) → close on manifold
     Random non-connected pairs → far on manifold
     Used to train the GNN encoder via contrastive loss on Lorentz distance.

  2. Operation pairs: (query_text, intent, operation_type, source_concepts)
     Used to train the QueryProjector (Week 3).
     Generated from edge templates.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from flux.kg.store import KGStore

log = logging.getLogger(__name__)

PAIRS_OUTPUT = Path("logs/manifold_training_pairs.jsonl")

# ── Operation pair templates ────────────────────────────────────────────────────
# For each edge type → (operation_type, query_templates)
# {source} and {target} are filled with concept names from the KG.

_EDGE_TEMPLATES = {
    "CAUSES": ("PERTURB", [
        "what would happen if {source} changed",
        "what breaks if {source} fails",
        "what does {source} cause",
        "if {source} decreased, what would break",
        "what depends on {source}",
    ]),
    "REQUIRES": ("GEODESIC", [
        "what does {target} need from {source}",
        "how does {source} enable {target}",
        "what is the relationship between {source} and {target}",
        "why does {target} depend on {source}",
    ]),
    "ENABLES": ("GEODESIC", [
        "how does {source} lead to {target}",
        "what does {source} make possible",
        "how is {source} connected to {target}",
    ]),
    "ANALOGOUS_TO": ("CLUSTER", [
        "how is {source} similar to {target}",
        "what do {source} and {target} have in common",
        "what is the analogy between {source} and {target}",
    ]),
    "CONTRADICTS": ("GAP", [
        "how does {source} conflict with {target}",
        "what is the tension between {source} and {target}",
        "why are {source} and {target} in conflict",
    ]),
    "CONNECTS": ("GEODESIC", [
        "what connects {source} and {target}",
        "how is {source} related to {target}",
    ]),
    "GENERALIZES": ("GEODESIC", [
        "how does {source} generalize {target}",
        "what is the relationship between {source} and {target}",
    ]),
    "SPECIFIES": ("POINT_QUERY", [
        "explain {source} in the context of {target}",
        "what is {source}",
    ]),
    "CONTRASTS": ("GAP", [
        "what is the difference between {source} and {target}",
        "how do {source} and {target} differ",
    ]),
    "USES": ("GEODESIC", [
        "how does {source} use {target}",
        "what is {target}'s role in {source}",
    ]),
}

_INTENT_FOR_OP = {
    "PERTURB":     "COUNTERFACTUAL",
    "GEODESIC":    "CONNECT",
    "CLUSTER":     "EXPLAIN",
    "GAP":         "HYPOTHESIZE",
    "POINT_QUERY": "EXPLAIN",
}


@dataclass
class EmbeddingPair:
    """Training pair for the GNN encoder."""
    concept_a: str
    concept_b: str
    should_be_close: bool      # True = causal pair, False = negative sample
    edge_type: str = ""
    weight: float = 1.0        # importance weight for training


@dataclass
class OperationPair:
    """Training pair for the QueryProjector."""
    query_text:      str
    intent:          str
    operation_type:  str
    source_concept:  str
    target_concept:  str = ""


def generate_embedding_pairs(store: KGStore,
                              neg_ratio: int = 2) -> list[EmbeddingPair]:
    """
    Generate positive (causal) and negative (random) embedding pairs.

    neg_ratio: negative samples per positive pair (default 2)
    Higher ratio = harder training = better separation.
    """
    pairs: list[EmbeddingPair] = []
    all_ids = [nid for nid in store.all_ids()
               if not (nid.startswith("__") and nid.endswith("__"))]

    # Strong positive pairs — CAUSES/REQUIRES (strongest causal signal)
    strong_types = {"CAUSES", "REQUIRES", "ENABLES"}
    # Weak positive pairs — CONNECTS/USES/ANALOGOUS_TO (semantic but not causal)
    weak_types   = {"CONNECTS", "ANALOGOUS_TO", "USES", "GENERALIZES", "SPECIFIES"}

    connected_set: set[tuple[str, str]] = set()

    for cid in all_ids:
        for edge in (store.edges_from(cid) or []):
            tgt   = edge.get("to", "")
            etype = edge.get("type", "")
            if not tgt or tgt.startswith("__"):
                continue

            weight = 1.0 if etype in strong_types else 0.6
            pairs.append(EmbeddingPair(cid, tgt, True, etype, weight))
            connected_set.add((cid, tgt))
            connected_set.add((tgt, cid))

    log.info("  Positive pairs: %d", len(pairs))

    # Negative pairs — random non-connected concept pairs
    n_negatives = len(pairs) * neg_ratio
    attempts    = 0
    negatives   = []
    while len(negatives) < n_negatives and attempts < n_negatives * 10:
        a, b = random.sample(all_ids, 2)
        if (a, b) not in connected_set and a != b:
            negatives.append(EmbeddingPair(a, b, False, "", 1.0))
        attempts += 1

    log.info("  Negative pairs: %d", len(negatives))
    pairs.extend(negatives)
    random.shuffle(pairs)
    return pairs


def generate_operation_pairs(store: KGStore) -> list[OperationPair]:
    """
    Generate query→operation training pairs from KG edges.
    Approximately: len(edges) × avg_templates_per_edge ≈ 5,000 pairs.
    """
    pairs: list[OperationPair] = []
    all_ids = [nid for nid in store.all_ids()
               if not (nid.startswith("__") and nid.endswith("__"))]

    for cid in all_ids:
        src_node = store.get(cid) or {}
        src_name = src_node.get("name", cid).replace("_", " ")

        for edge in (store.edges_from(cid) or []):
            tgt   = edge.get("to", "")
            etype = edge.get("type", "")
            if not tgt or tgt.startswith("__"):
                continue
            if etype not in _EDGE_TEMPLATES:
                continue

            tgt_node = store.get(tgt) or {}
            tgt_name = tgt_node.get("name", tgt).replace("_", " ")

            op_type, templates = _EDGE_TEMPLATES[etype]
            intent = _INTENT_FOR_OP[op_type]

            for template in templates:
                query = template.format(source=src_name, target=tgt_name)
                pairs.append(OperationPair(
                    query_text     = query,
                    intent         = intent,
                    operation_type = op_type,
                    source_concept = cid,
                    target_concept = tgt,
                ))

    log.info("  Operation pairs: %d", len(pairs))
    random.shuffle(pairs)
    return pairs


def generate_and_save(store: KGStore,
                      output: Path = PAIRS_OUTPUT) -> dict:
    """Generate all training pairs and save to JSONL."""
    log.info("Generating manifold training pairs...")

    embedding_pairs  = generate_embedding_pairs(store)
    operation_pairs  = generate_operation_pairs(store)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as f:
        for p in embedding_pairs:
            f.write(json.dumps({
                "type": "embedding",
                "concept_a": p.concept_a,
                "concept_b": p.concept_b,
                "should_be_close": p.should_be_close,
                "edge_type": p.edge_type,
                "weight": p.weight,
            }) + "\n")
        for p in operation_pairs:
            f.write(json.dumps({
                "type": "operation",
                "query_text": p.query_text,
                "intent": p.intent,
                "operation_type": p.operation_type,
                "source_concept": p.source_concept,
                "target_concept": p.target_concept,
            }) + "\n")

    log.info("Saved %d embedding + %d operation pairs to %s",
             len(embedding_pairs), len(operation_pairs), output)
    return {
        "embedding_pairs": len(embedding_pairs),
        "operation_pairs": len(operation_pairs),
        "total":           len(embedding_pairs) + len(operation_pairs),
        "output":          str(output),
    }
