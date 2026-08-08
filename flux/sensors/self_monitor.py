"""
flux/sensors/self_monitor.py

Flux observing herself — KG state, RSM queue, confidence distribution.
Snapshot → action → snapshot → diff: every action produces a causal record.
"""
from __future__ import annotations
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flux.kg.store import KGStore


class SelfMonitor:
    def __init__(self, store: "KGStore") -> None:
        self.store = store

    def snapshot(self) -> dict:
        nodes      = self.store.all_nodes()
        edge_count = sum(len(v) for v in self.store._edges_from.values())
        live_count = len(self.store._nodes) - len(self.store._seed_ids)
        confs      = [n.get("confidence", 0.0) for n in nodes.values()
                      if isinstance(n.get("confidence"), (int, float))]

        return {
            "timestamp":       time.time(),
            "kg_node_count":   len(nodes),
            "kg_edge_count":   edge_count,
            "kg_live_nodes":   live_count,
            "conf_mean":       round(sum(confs) / len(confs), 4) if confs else 0.0,
            "conf_min":        round(min(confs), 4) if confs else 0.0,
            "conf_max":        round(max(confs), 4) if confs else 0.0,
        }

    def diff(self, before: dict, after: dict) -> dict:
        """Numeric delta between two snapshots."""
        skip = {"timestamp"}
        return {
            k: round(after[k] - before[k], 4)
            for k in before
            if k not in skip and isinstance(before[k], (int, float))
        }

    def predict(self, action: str) -> dict:
        """
        Stage-D: predict expected diff before acting.
        Starts as heuristic estimates; replaced by learned values in Stage B.
        """
        heuristics = {
            "TEACH":            {"kg_node_count": 1,  "kg_edge_count": 4,  "kg_live_nodes": 1},
            "QUERY":            {"kg_node_count": 0,  "kg_edge_count": 0,  "kg_live_nodes": 0},
            "COMMIT":           {"kg_node_count": 1,  "kg_edge_count": 3,  "kg_live_nodes": 1},
            "PROPOSE":          {"kg_node_count": 0,  "kg_edge_count": 0,  "kg_live_nodes": 0},
            "WRONG":            {"kg_node_count": 0,  "kg_edge_count": 0,  "conf_mean": -0.05},
            "CORRECT":          {"kg_node_count": 0,  "kg_edge_count": 0,  "conf_mean":  0.02},
        }
        return heuristics.get(action, {"kg_node_count": 0, "kg_edge_count": 0})
