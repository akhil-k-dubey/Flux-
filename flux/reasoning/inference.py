"""
flux/reasoning/inference.py

Confidence propagation over reasoning chains.

The bridge returns paths and derivations as raw concept-ID lists.
This module scores them using the edge-type weight table × target
node confidence × hop-decay formula from the design spec.

Formula (per design spec):
  hop_confidence(edge) = EDGE_WEIGHTS[edge.type] × target_node.confidence
  propagated_confidence(path) = min(hop_confs) × 0.9^(hops - 1)

EDGE_WEIGHTS:
  REQUIRES     1.00   — hard dependency, strongest evidence
  IS_A         0.95   — definitional subsumption
  ENABLES      0.90   — causal enablement
  PART_OF      0.85   — compositional
  DEFINES      0.85   — definitional
  FORMALIZES   0.82   — formal correspondence
  VERIFIED_BY  0.80   — empirical/formal verification
  RELATES_TO   0.70   — generic association
  SIMILAR_TO   0.65   — soft analogy
  ANALOGOUS_TO 0.60   — structural analogy
  CO_OCCURS    0.55   — statistical co-occurrence
  LAYER_MEMBER 0.50   — structural membership
  INVERSE_OF   0.50   — definitional inverse
  INSTANCE_OF  0.60   — instantiation
  CONTRADICTS  0.00   — never traversed

Paths are scored end-to-end and ranked by propagated_confidence descending.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from flux.kg.store import KGStore

# ── Edge type weight table ────────────────────────────────────────────────────

EDGE_WEIGHTS: dict[str, float] = {
    "REQUIRES":     1.00,
    "IS_A":         0.95,
    "ENABLES":      0.90,
    "PART_OF":      0.85,
    "DEFINES":      0.85,
    "FORMALIZES":   0.82,
    "VERIFIED_BY":  0.80,
    "IMPROVES":     0.75,
    "CONSTRAINS":   0.75,
    "CAUSES":       0.75,
    "PRECEDES":     0.72,
    "RELATES_TO":   0.70,
    "DEFINES":      0.70,
    "HAS_A":        0.65,
    "SIMILAR_TO":   0.65,
    "INSTANCE_OF":  0.60,
    "ANALOGOUS_TO": 0.60,
    "CO_OCCURS":    0.55,
    "DEGRADES":     0.55,
    "PREVENTS":     0.55,
    "LAYER_MEMBER": 0.50,
    "INVERSE_OF":   0.50,
    "CONTRADICTS":  0.00,   # never traversed
}

DECAY = 0.9


# ── Scored path dataclass ─────────────────────────────────────────────────────

@dataclass
class ScoredPath:
    """A traversal path with per-hop details and a final propagated confidence."""
    nodes:         list[str]             # concept IDs in order
    edges:         list[dict]            # edge dicts {from, to, type}
    hop_scores:    list[float]           # per-hop confidence contribution
    propagated:    float                 # final propagated confidence
    depth:         int                   # number of hops


@dataclass
class Derivation:
    """A single derived fact with its propagated confidence."""
    relation:   str    # "requires", "enables", "indirect_enables", "reachable"
    source:     str
    target:     str
    confidence: float


# ── Confidence computation ────────────────────────────────────────────────────

def hop_confidence(edge: dict, store: KGStore) -> float:
    """
    Confidence of a single hop = EDGE_WEIGHTS[type] × target_node.confidence.
    If target node is missing from store, uses 0.5 as a conservative default.
    """
    etype        = edge.get("type", "RELATES_TO").upper()
    target_id    = edge.get("to", "")
    target_node  = store.get(target_id)
    target_conf  = float(target_node.get("confidence", 0.5)) if target_node else 0.5
    edge_weight  = EDGE_WEIGHTS.get(etype, 0.60)
    return edge_weight * target_conf


def propagated_confidence(path_edges: list[dict], store: KGStore) -> tuple[float, list[float]]:
    """
    Compute propagated confidence across a list of edge dicts.

    Returns (propagated_conf, hop_scores).
    """
    if not path_edges:
        return 1.0, []

    hop_scores = [hop_confidence(e, store) for e in path_edges]
    base       = min(hop_scores)                         # weakest link
    decay      = DECAY ** (len(path_edges) - 1)         # 0.9^(hops-1)
    return round(base * decay, 4), [round(s, 4) for s in hop_scores]


# ── Path scoring ──────────────────────────────────────────────────────────────

def score_path(node_ids: list[str], store: KGStore) -> ScoredPath | None:
    """
    Given an ordered list of concept IDs (a traversal path), look up the
    connecting edges in the KG and compute propagated confidence.

    Returns None if any hop has no connecting edge (broken path).
    """
    if len(node_ids) < 2:
        return None

    edges: list[dict] = []
    for i in range(len(node_ids) - 1):
        src, tgt = node_ids[i], node_ids[i + 1]
        # Find the edge between src and tgt
        edge = _find_edge(src, tgt, store)
        if edge is None:
            return None
        edges.append(edge)

    prop_conf, hop_scores = propagated_confidence(edges, store)
    return ScoredPath(
        nodes      = node_ids,
        edges      = edges,
        hop_scores = hop_scores,
        propagated = prop_conf,
        depth      = len(edges),
    )


def _find_edge(src: str, tgt: str, store: KGStore) -> dict | None:
    """Find the highest-weight edge from src to tgt."""
    candidates = [
        e for e in store.edges_from(src)
        if e.get("to") == tgt
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: EDGE_WEIGHTS.get(e.get("type","").upper(), 0.0))


# ── Derivation scoring ────────────────────────────────────────────────────────

def score_derivation(
    relation: str,
    source:   str,
    target:   str,
    store:    KGStore,
    depth:    int = 1,
) -> Derivation:
    """
    Score a derived fact (from Clingo output) using the edge weight formula.
    For multi-hop derivations, depth is estimated from the relation type:
      trans_requires / trans_enables: depth 2 (at least 2 hops to be "transitive")
      indirect_enables:               depth 3 (requires + enables = 2 hops min)
      reachable:                      depth 1 (conservative)
    """
    # Find the direct edge if it exists — use hop_confidence directly
    direct_edge = _find_edge(source, target, store)
    if direct_edge:
        base_conf = hop_confidence(direct_edge, store)
        dec       = DECAY ** 0
    else:
        # Estimated: use target node confidence × estimated edge weight
        target_node = store.get(target)
        target_conf = float(target_node.get("confidence", 0.5)) if target_node else 0.5
        etype_map   = {
            "requires":        EDGE_WEIGHTS["REQUIRES"],
            "enables":         EDGE_WEIGHTS["ENABLES"],
            "indirect_enables": EDGE_WEIGHTS["ENABLES"] * EDGE_WEIGHTS["REQUIRES"],
            "reachable":       EDGE_WEIGHTS["RELATES_TO"],
        }
        base_conf = etype_map.get(relation, 0.60) * target_conf
        dec       = DECAY ** max(depth - 1, 1)

    conf = round(base_conf * dec, 4)
    return Derivation(
        relation   = relation,
        source     = source,
        target     = target,
        confidence = conf,
    )


def rank_derivations(derivations: list[Derivation], top_n: int = 20) -> list[Derivation]:
    """Sort derivations by confidence descending, return top_n."""
    return sorted(derivations, key=lambda d: d.confidence, reverse=True)[:top_n]
