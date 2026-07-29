"""
flux/kg/consistency.py

Checks whether multi-hop traversal paths are logically consistent.

Current implementation: graph-level contradiction detection.
  A path is inconsistent if any two nodes on it are connected by a
  CONTRADICTS edge in the KG.

Planned (Stage 4): full Clingo axiom verification for paths involving
  formal specs. The check_path() interface is stable — the internals
  can be upgraded without touching the engine or query layer.
"""
from __future__ import annotations

from flux.kg.store import KGStore


class ConsistencyChecker:
    """
    Stateless consistency checker.  All data read from store at call time.
    """

    def __init__(self, store: KGStore) -> None:
        self._store = store

    def check_path(self, path: list[str]) -> tuple[bool, str | None]:
        """
        Check a list of concept IDs (a traversal path) for contradictions.

        A path is inconsistent if any node on it has a CONTRADICTS edge
        pointing to another node also on the path.

        Args:
            path: concept IDs in traversal order, e.g. ["entropy", "thermodynamics"]

        Returns:
            (True, None)          — path is consistent
            (False, reason_str)   — contradiction found with explanation
        """
        path_set = set(path)
        for node_id in path:
            for edge in self._store.edges_from(node_id):
                if edge.get("type") == "CONTRADICTS" and edge.get("to") in path_set:
                    return False, f"{node_id} CONTRADICTS {edge['to']}"
        return True, None

    def find_contradictions(self, concept_id: str) -> list[dict]:
        """
        Return all CONTRADICTS edges involving concept_id
        (as source OR target) — useful for query result flagging.
        """
        contradictions = [
            e for e in self._store.edges_from(concept_id)
            if e.get("type") == "CONTRADICTS"
        ]
        contradictions += [
            e for e in self._store.edges_to(concept_id)
            if e.get("type") == "CONTRADICTS"
        ]
        return contradictions

    def has_contradiction(self, a: str, b: str) -> bool:
        """Quick check: is there a CONTRADICTS edge between a and b?"""
        for edge in self._store.edges_from(a):
            if edge.get("type") == "CONTRADICTS" and edge.get("to") == b:
                return True
        for edge in self._store.edges_from(b):
            if edge.get("type") == "CONTRADICTS" and edge.get("to") == a:
                return True
        return False
