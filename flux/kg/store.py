"""
flux/kg/store.py

In-memory singleton knowledge graph store.

Two data sources:
  flux_kg_seed.json    — constitutional seed, never modified at runtime
  flux_kg_live.jsonl   — append-only runtime writes (one JSON record per line)

Both are loaded at init and merged in-memory. Writes go only to the live file.

Record format (JSONL):
  {"record_type": "node", "timestamp": 1721234567.89, "data": {...}}
  {"record_type": "edge", "timestamp": 1721234567.89, "data": {...}}

Write semantics:
  New node        → write full node + edges, log COMMITTED
  Seed node (immutable)  → write new edges only, log AUGMENTED
  Seed node (mutable)    → write new edges + confidence update if higher, AUGMENTED
  Live node exists       → write new edges + confidence update if higher, UPDATED

Thread safety: not required — Flux is single-threaded at this stage.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flux.pipeline.schema import is_immutable as schema_is_immutable

log = logging.getLogger(__name__)

SEED_PATH = Path("logs/flux_kg_seed.json")
LIVE_PATH = Path("logs/flux_kg_live.jsonl")

# Module-level singleton
_store: "KGStore | None" = None


def get_store() -> "KGStore":
    """Return the shared singleton KGStore, creating it if necessary."""
    global _store
    if _store is None:
        _store = KGStore()
    return _store


class KGStore:
    """
    In-memory knowledge graph.

    Prefer get_store() over direct instantiation to ensure singleton behaviour.
    """

    def __init__(self) -> None:
        # id → node dict  (merged seed + live)
        self._nodes:       dict[str, dict]       = {}
        # source_id → [edge_dict, ...]
        self._edges_from:  dict[str, list[dict]] = {}
        # target_id → [edge_dict, ...]
        self._edges_to:    dict[str, list[dict]] = {}
        # spec_id → spec_node dict (seed only — bridge nodes embed specs on node)
        self._spec_nodes:  dict[str, dict]       = {}
        # IDs that came from the seed file — definition/type/layer are read-only
        self._seed_ids:    set[str]              = set()

        self._load_seed()
        self._load_live()

        s = self.stats()
        log.info(
            "KGStore ready: %d nodes (%d seed / %d live)  %d edges",
            s["nodes"], s["seed_nodes"], s["live_nodes"], s["edges"],
        )

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load_seed(self) -> None:
        if not SEED_PATH.exists():
            log.warning("Seed file not found: %s — starting with empty KG", SEED_PATH)
            return
        data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        for node in data.get("nodes", []):
            self._nodes[node["id"]] = node
            self._seed_ids.add(node["id"])
        for edge in data.get("edges", []):
            self._register_edge(edge)
        for spec in data.get("spec_nodes", []):
            self._spec_nodes[spec["id"]] = spec
        for edge in data.get("spec_edges", []):
            self._register_edge(edge)
        log.debug(
            "Loaded seed: %d nodes  %d spec_nodes", len(self._nodes), len(self._spec_nodes)
        )

    def _load_live(self) -> None:
        if not LIVE_PATH.exists():
            return
        loaded_nodes = loaded_edges = 0
        with LIVE_PATH.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    rtype  = record.get("record_type")
                    data   = record.get("data", {})
                    if rtype == "node":
                        self._nodes[data["id"]] = data
                        loaded_nodes += 1
                    elif rtype == "edge":
                        self._register_edge(data)
                        loaded_edges += 1
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning(
                        "Skipping malformed live record at line %d: %s", lineno, exc
                    )
        log.debug(
            "Loaded live file: %d nodes  %d edges", loaded_nodes, loaded_edges
        )

    def _register_edge(self, edge: dict) -> None:
        src = edge.get("from", "")
        tgt = edge.get("to",   "")
        if src:
            self._edges_from.setdefault(src, []).append(edge)
        if tgt:
            self._edges_to.setdefault(tgt, []).append(edge)

    # ── Read API ───────────────────────────────────────────────────────────────

    def get(self, concept_id: str) -> dict | None:
        """Return node dict for concept_id, or None if not found."""
        return self._nodes.get(concept_id)

    def edges_from(self, concept_id: str) -> list[dict]:
        """All outgoing edges from concept_id."""
        return list(self._edges_from.get(concept_id, []))

    def edges_to(self, concept_id: str) -> list[dict]:
        """All incoming edges to concept_id."""
        return list(self._edges_to.get(concept_id, []))

    def get_spec(self, concept_id: str, language: str) -> str | None:
        """
        Look up formal spec text for concept_id in language.

        Bridge-committed nodes embed specs directly on the node.
        Seed nodes store specs in spec_nodes with id "spec_{concept}_{lang}".
        Both cases handled transparently.
        """
        node = self._nodes.get(concept_id)
        if node:
            specs = node.get("specs", {})
            if isinstance(specs, dict):
                val = specs.get(language)
                if val is not None:
                    return val
        # Fall back to seed spec_nodes
        spec_id   = f"spec_{concept_id}_{language.lower()}"
        spec_node = self._spec_nodes.get(spec_id)
        if spec_node:
            return spec_node.get("spec_text")
        return None

    def all_nodes(self) -> dict[str, dict]:
        """Shallow copy of the id→node mapping."""
        return dict(self._nodes)

    def all_ids(self) -> list[str]:
        return list(self._nodes.keys())

    def is_seed_node(self, concept_id: str) -> bool:
        return concept_id in self._seed_ids

    # ── Write API ──────────────────────────────────────────────────────────────

    def write(self, node: dict, edges: list[dict]) -> str:
        """
        Commit a node and its outgoing edges to the KG.

        Returns the action taken: "COMMITTED" | "AUGMENTED" | "UPDATED"
        """
        node_id = node.get("id", "")
        if not node_id:
            raise ValueError("write() called with node missing 'id' field")

        ts = time.time()

        if node_id in self._seed_ids:
            return self._write_seed_augment(node_id, node, edges, ts)

        if node_id in self._nodes:
            return self._write_live_update(node_id, node, edges, ts)

        return self._write_new(node_id, node, edges, ts)

    # ── Write internals ────────────────────────────────────────────────────────

    def _write_seed_augment(
        self, node_id: str, node: dict, edges: list[dict], ts: float
    ) -> str:
        seed_node = self._nodes[node_id]
        immutable = seed_node.get("immutable", False) or schema_is_immutable(
            seed_node.get("type", ""), seed_node.get("layer", "")
        )
        if not immutable:
            # Mutable seed node: update confidence if the new value is higher
            if node.get("confidence", 0.0) > seed_node.get("confidence", 0.0):
                updated = {**seed_node, "confidence": node["confidence"]}
                self._nodes[node_id] = updated
                self._append("node", updated, ts)

        new_edges = self._filter_new_edges(edges)
        self._write_edges_batch(new_edges, ts)
        log.info("AUGMENTED seed node %r  (+%d new edges)", node_id, len(new_edges))
        return "AUGMENTED"

    def _write_live_update(
        self, node_id: str, node: dict, edges: list[dict], ts: float
    ) -> str:
        existing = self._nodes[node_id]
        if node.get("confidence", 0.0) > existing.get("confidence", 0.0):
            updated = {**existing, "confidence": node["confidence"]}
            self._nodes[node_id] = updated
            self._append("node", updated, ts)

        new_edges = self._filter_new_edges(edges)
        self._write_edges_batch(new_edges, ts)
        log.info("UPDATED live node %r  (+%d new edges)", node_id, len(new_edges))
        return "UPDATED"

    def _write_new(
        self, node_id: str, node: dict, edges: list[dict], ts: float
    ) -> str:
        self._nodes[node_id] = node
        self._append("node", node, ts)
        self._write_edges_batch(edges, ts)
        log.info("COMMITTED new node %r  (%d edges)", node_id, len(edges))
        return "COMMITTED"

    def _filter_new_edges(self, edges: list[dict]) -> list[dict]:
        """Exclude edges already present in _edges_from."""
        result = []
        for e in edges:
            existing = self._edges_from.get(e.get("from", ""), [])
            already  = any(
                x["to"] == e.get("to") and x["type"] == e.get("type")
                for x in existing
            )
            if not already:
                result.append(e)
        return result

    def _write_edges_batch(self, edges: list[dict], ts: float) -> None:
        for edge in edges:
            self._register_edge(edge)
            self._append("edge", edge, ts)

    def _append(self, record_type: str, data: dict, ts: float) -> None:
        record = {"record_type": record_type, "timestamp": ts, "data": data}
        with LIVE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── Stats / repr ───────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "nodes":      len(self._nodes),
            "seed_nodes": len(self._seed_ids),
            "live_nodes": len(self._nodes) - len(self._seed_ids),
            "edges":      sum(len(v) for v in self._edges_from.values()),
            "spec_nodes": len(self._spec_nodes),
        }

    def __repr__(self) -> str:
        s = self.stats()
        return (
            f"KGStore("
            f"nodes={s['nodes']} [{s['seed_nodes']} seed + {s['live_nodes']} live]  "
            f"edges={s['edges']})"
        )
