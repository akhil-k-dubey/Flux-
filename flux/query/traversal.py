"""
flux/query/traversal.py

Breadth-first traversal from a target concept, following outgoing edges
weighted by type priority.

Edge type priority (higher = followed first):
  REQUIRES      6   foundational dependency — traverse first
  ENABLES       5
  IS_A          5
  PART_OF       4
  FORMALIZES    4
  RELATES_TO    3   generic relation
  SIMILAR_TO    2
  ANALOGOUS_TO  2
  CO_OCCURS     2
  LAYER_MEMBER  1
  INVERSE_OF    1
  CONTRADICTS   0   flagged separately — NOT followed (stops traversal)

Traversal stops at:
  - requested depth
  - already-visited nodes
  - nodes with confidence below min_confidence
  - dangling edges (target node not in KG)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from flux.kg.store import KGStore


EDGE_PRIORITY: dict[str, int] = {
    "REQUIRES":     6,
    "ENABLES":      5,
    "IS_A":         5,
    "PART_OF":      4,
    "FORMALIZES":   4,
    "VERIFIED_BY":  4,
    "RELATES_TO":   3,
    "DEFINES":      3,
    "IMPROVES":     3,
    "SIMILAR_TO":   2,
    "ANALOGOUS_TO": 2,
    "CO_OCCURS":    2,
    "HAS_A":        2,
    "CONSTRAINS":   2,
    "CAUSES":       2,
    "PRECEDES":     2,
    "LAYER_MEMBER": 1,
    "INVERSE_OF":   1,
    "INSTANCE_OF":  1,
    "DEGRADES":     1,
    "PREVENTS":     1,
    "CONTRADICTS":  0,   # flagged, never followed
}

_MAX_PRIORITY = max(EDGE_PRIORITY.values())


@dataclass
class TraversalResult:
    """Output of one BFS traversal run."""
    target:          str
    # Visited nodes excluding the target itself, in BFS order
    nodes:           list[dict]        = field(default_factory=list)
    # node_id → full path list from target (inclusive on both ends)
    paths:           dict[str, list[str]] = field(default_factory=dict)
    # node_id → highest-priority edge type used to reach it
    edge_types_used: dict[str, str]    = field(default_factory=dict)
    # CONTRADICTS edges encountered during traversal (not followed)
    contradictions:  list[dict]        = field(default_factory=list)


def traverse(
    target:         str,
    depth:          int,
    min_confidence: float,
    store:          KGStore,
) -> TraversalResult:
    """
    BFS from ``target`` up to ``depth`` hops.

    Returns a TraversalResult containing all reachable nodes above
    min_confidence, their traversal paths, and any CONTRADICTS edges seen.
    """
    result = TraversalResult(target=target)

    if depth == 0:
        return result

    visited: set[str] = {target}
    # queue entries: (node_id, current_depth, path_so_far)
    queue: deque[tuple[str, int, list[str]]] = deque(
        [(target, 0, [target])]
    )

    while queue:
        node_id, cur_depth, path = queue.popleft()

        if cur_depth >= depth:
            continue

        # Sort outgoing edges: highest priority first
        edges = sorted(
            store.edges_from(node_id),
            key=lambda e: EDGE_PRIORITY.get(e.get("type", ""), 1),
            reverse=True,
        )

        for edge in edges:
            etype = edge.get("type", "RELATES_TO")
            tgt   = edge.get("to",   "")

            # Contradiction: flag, do not follow
            if etype == "CONTRADICTS":
                result.contradictions.append(edge)
                continue

            if not tgt or tgt in visited:
                continue

            tgt_node = store.get(tgt)
            if tgt_node is None:
                continue   # dangling edge

            if tgt_node.get("confidence", 1.0) < min_confidence:
                continue   # below confidence threshold

            visited.add(tgt)
            new_path = path + [tgt]

            result.nodes.append(tgt_node)
            result.paths[tgt]           = new_path
            result.edge_types_used[tgt] = etype

            queue.append((tgt, cur_depth + 1, new_path))

    return result
