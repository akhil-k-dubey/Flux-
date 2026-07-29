"""
flux/query/ranker.py

Ranks traversal result nodes by composite relevance score.

Score formula (weights sum to 1.0):
  0.40 × node confidence
  0.30 × edge type priority (normalised to [0, 1])
  0.20 × proximity  (1 / hop_distance, closer = more relevant)
  0.10 × layer weight (constitutional layers score higher)

Returns top-N nodes, each augmented with private fields:
  _score      — composite relevance score
  _edge_type  — highest-priority edge type used to reach this node
  _hop        — hop distance from target (1 = direct neighbour)
"""
from __future__ import annotations

from flux.query.traversal import TraversalResult, EDGE_PRIORITY, _MAX_PRIORITY


# Constitutional layer weight — earlier layers have higher epistemic priority
_LAYER_WEIGHT: dict[str, float] = {
    "LAYER_0": 1.00,
    "LAYER_S": 0.95,   # structural/schema
    "LAYER_1": 0.90,
    "LAYER_2": 0.80,
    "LAYER_3": 0.70,
    "LAYER_4": 0.60,
}
_DEFAULT_LAYER_WEIGHT = 0.50


def _score(node: dict, result: TraversalResult) -> float:
    node_id    = node.get("id", "")
    confidence = float(node.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    # Hop distance (path includes target as first element)
    path     = result.paths.get(node_id, [result.target, node_id])
    hop_dist = max(len(path) - 1, 1)
    proximity = 1.0 / hop_dist

    # Normalised edge priority
    etype      = result.edge_types_used.get(node_id, "RELATES_TO")
    edge_score = EDGE_PRIORITY.get(etype, 1) / _MAX_PRIORITY

    # Layer weight
    layer      = (node.get("layer") or "").upper()
    layer_wt   = _LAYER_WEIGHT.get(layer, _DEFAULT_LAYER_WEIGHT)

    return (
        0.40 * confidence +
        0.30 * edge_score +
        0.20 * proximity  +
        0.10 * layer_wt
    )


def rank(result: TraversalResult, top_n: int = 10) -> list[dict]:
    """
    Score and sort traversal result nodes.

    Returns at most top_n nodes, each augmented with _score, _edge_type,
    and _hop. These private fields are consumed by formatter.py and
    stripped from the final public response.
    """
    scored: list[dict] = []
    for node in result.nodes:
        node_id = node.get("id", "")
        s       = _score(node, result)
        hop     = len(result.paths.get(node_id, [result.target, node_id])) - 1
        scored.append({
            **node,
            "_score":     round(s, 4),
            "_edge_type": result.edge_types_used.get(node_id, "RELATES_TO"),
            "_hop":       hop,
        })

    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored[:top_n]
