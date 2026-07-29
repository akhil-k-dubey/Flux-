"""
flux/kg/index.py

Fast O(1) lookup indices over KGStore, built at engine startup.
Provides type-based and layer-based node filtering without scanning all nodes.

Call rebuild() after a batch of KGStore writes to keep indices current.
For single writes the engine calls rebuild() itself.
"""
from __future__ import annotations

from collections import defaultdict
from flux.kg.store import KGStore


class KGIndex:
    """
    Secondary lookup indices for type and layer filtering.

    The primary id→node lookup is already O(1) inside KGStore._nodes.
    This adds O(1) type-group and layer-group lookups.
    """

    def __init__(self, store: KGStore) -> None:
        self._store     = store
        self._by_type:  dict[str, list[str]] = defaultdict(list)
        self._by_layer: dict[str, list[str]] = defaultdict(list)
        self._build()

    # ── Build / refresh ────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._by_type.clear()
        self._by_layer.clear()
        for node_id, node in self._store.all_nodes().items():
            t = (node.get("type",  "") or "").upper()
            l = (node.get("layer", "") or "").upper()
            if t:
                self._by_type[t].append(node_id)
            if l:
                self._by_layer[l].append(node_id)

    def rebuild(self) -> None:
        """Rebuild after KGStore writes. Call after write() to stay current."""
        self._build()

    # ── Query ──────────────────────────────────────────────────────────────────

    def by_type(self, node_type: str) -> list[str]:
        """Return all node IDs of the given type."""
        return list(self._by_type.get(node_type.upper(), []))

    def by_layer(self, layer: str) -> list[str]:
        """Return all node IDs in the given layer."""
        return list(self._by_layer.get(layer.upper(), []))

    def all_ids(self) -> list[str]:
        return self._store.all_ids()

    # ── Utility ────────────────────────────────────────────────────────────────

    def type_counts(self) -> dict[str, int]:
        return {t: len(ids) for t, ids in self._by_type.items()}

    def layer_counts(self) -> dict[str, int]:
        return {l: len(ids) for l, ids in self._by_layer.items()}

    def __repr__(self) -> str:
        return (
            f"KGIndex(types={len(self._by_type)}  "
            f"layers={len(self._by_layer)}  "
            f"total={len(self.all_ids())})"
        )
