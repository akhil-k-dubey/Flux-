"""
flux/nervous_system/world_model.py

Flux's living world model — a unified, durable state of everything Flux knows
about herself, her environment, and Akhil.

This is not a snapshot. It is the nervous system. Every part of Flux reads from
and writes to this. It stays alive for as long as Flux is running.

Architecture:
  - In-memory dict with dotted-path get/update API
  - Background writer thread: flushes to disk every 5s IF dirty
  - Atomic write (temp file → rename) — no partial writes
  - atexit handler for clean shutdown persistence
  - Thread-safe via RLock

Usage:
  from flux.nervous_system.world_model import WorldModel
  wm = WorldModel.get()                         # singleton
  wm.update("agents.akhil.last_interaction", t)
  wm.get("knowledge.kg_nodes")                  # → 437
  wm.get("self.architecture.hidden_dim")        # → 128
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_WORLD_MODEL_PATH = Path("logs/world_model.json")
_FLUSH_INTERVAL   = 5.0   # seconds between disk flushes when dirty

# ── Initial state (schema + defaults) ─────────────────────────────────────────
def _make_initial_state() -> dict:
    return {
        "physical": {
            "laptop": {
                "hostname":         os.uname().nodename,
                "cpu_temp_c":       None,
                "ram_used_mb":      None,
                "ram_total_mb":     None,
                "battery_pct":      None,
                "battery_charging": None,
                "uptime_hours":     None,
                "last_seen":        None,
            },
            "sensors": {
                "active":       False,
                "co2":          None,
                "presence":     None,
                "lux":          None,
                "power_watts":  None,
            },
        },
        "agents": {
            "akhil": {
                "last_interaction":       None,
                "session_count_today":    0,
                "session_date":           None,       # date of current session_count
                "typical_session_start":  None,       # learned over time
                "current_focus_domain":   None,       # inferred from queries
                "predicted_next_query":   None,       # after 100+ interactions
                "cognitive_load":         None,       # inferred from query complexity
                "total_queries":          0,
                "domain_history":         [],         # last 20 domains queried
            },
        },
        "knowledge": {
            "kg_nodes":         0,
            "kg_edges":         0,
            "domains":          {},                   # domain → count
            "last_stage_b":     None,
        },
        "processes": {
            "active_rsm_proposals":    [],
            "open_failures":           [],
            "jarvis_tests": {
                "test1_rsm_audit":              "UNKNOWN",
                "test2_counterfactual_thermo":  "UNKNOWN",
                "test3_holographic_gaps":       "UNKNOWN",
                "test4_unsolved_intersection":  "UNKNOWN",
                "test5_decision_memory":        "UNKNOWN",
            },
        },
        "predictions": {
            "next_akhil_session":  None,
            "next_rsm_trigger":    None,
            "next_stage_b":        None,
        },
        "self": {
            "architecture": {
                "hidden_dim":       128,
                "cons_bottleneck":  64,
                "dropout":          0.1,
                "lr_schedule":      "cosine",
            },
            "capabilities": {
                "confirmed": ["EXPLAIN", "CONNECT", "AUDIT", "SELF", "REASON"],
                "partial":   ["COUNTERFACTUAL", "HYPOTHESIZE"],
                "missing":   ["SYNTHESIS", "DECISION"],
            },
            "last_shutdown":          None,
            "total_uptime_hours":     0.0,
            "total_queries_answered": 0,
            "total_failures":         0,
            "start_time":             None,
        },
    }


class WorldModel:
    """Flux's living world model. Singleton — use WorldModel.get()."""

    _instance: "WorldModel | None" = None
    _lock = threading.RLock()

    def __init__(self) -> None:
        self._data:    dict        = {}
        self._dirty:   bool        = False
        self._rlock:   threading.RLock = threading.RLock()
        self._writer:  threading.Thread | None = None
        self._running: bool        = False

    @classmethod
    def instance(cls) -> "WorldModel":
        """Return the singleton WorldModel, creating and starting it if needed."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
                cls._instance._load()
                cls._instance._start()
            return cls._instance

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, path: str, default: Any = None) -> Any:           # noqa: A003
        """Read a value by dotted path. e.g. 'agents.akhil.last_interaction'"""
        with self._rlock:
            cur = self._data
            for key in path.split("."):
                if not isinstance(cur, dict) or key not in cur:
                    return default
                cur = cur[key]
            return cur

    def update(self, path: str, value: Any) -> None:
        """Write a value at a dotted path. Creates intermediate dicts as needed."""
        with self._rlock:
            parts = path.split(".")
            cur = self._data
            for key in parts[:-1]:
                if key not in cur or not isinstance(cur[key], dict):
                    cur[key] = {}
                cur = cur[key]
            cur[parts[-1]] = value
            self._dirty = True

    def patch(self, path: str, patch_dict: dict) -> None:
        """Merge patch_dict into the dict at path."""
        with self._rlock:
            existing = self.get(path) or {}
            if isinstance(existing, dict):
                existing.update(patch_dict)
                self.update(path, existing)

    def snapshot(self) -> dict:
        """Return a deep copy of the full state."""
        import copy
        with self._rlock:
            return copy.deepcopy(self._data)

    def report(self) -> str:
        """Human-readable status line for Flux's self-report."""
        q   = self.get("self.total_queries_answered", 0)
        up  = self.get("self.total_uptime_hours", 0.0)
        nodes = self.get("knowledge.kg_nodes", 0)
        edges = self.get("knowledge.kg_edges", 0)
        domain = self.get("agents.akhil.current_focus_domain", "unknown")
        arch = self.get("self.architecture", {})
        return (
            f"Uptime: {up:.1f}h | Queries answered: {q} | "
            f"KG: {nodes} nodes / {edges} edges | "
            f"Akhil's focus: {domain} | "
            f"Architecture: hidden={arch.get('hidden_dim')}, "
            f"bottleneck={arch.get('cons_bottleneck')}"
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load from disk or initialise fresh state."""
        initial = _make_initial_state()
        if _WORLD_MODEL_PATH.exists():
            try:
                loaded = json.loads(_WORLD_MODEL_PATH.read_text())
                # Deep-merge: initial fills in missing keys, loaded overwrites
                self._data = _deep_merge(initial, loaded)
                log.info("WorldModel: loaded from %s", _WORLD_MODEL_PATH)
            except Exception as exc:
                log.warning("WorldModel: load failed (%s) — using fresh state", exc)
                self._data = initial
        else:
            self._data = initial
        # Always stamp start time
        self.update("self.start_time", datetime.now().isoformat())

    def flush(self) -> None:
        """Write current state to disk atomically."""
        _WORLD_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _WORLD_MODEL_PATH.with_suffix(".tmp")
        with self._rlock:
            payload = json.dumps(self._data, indent=2, default=str)
            self._dirty = False
        tmp.write_text(payload)
        tmp.rename(_WORLD_MODEL_PATH)

    def _start(self) -> None:
        """Start the background flush thread."""
        self._running = True
        self._writer = threading.Thread(
            target=self._flush_loop, daemon=True, name="WorldModel-flush"
        )
        self._writer.start()
        atexit.register(self._shutdown)
        log.info("WorldModel: started (flush every %.0fs)", _FLUSH_INTERVAL)

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(_FLUSH_INTERVAL)
            if self._dirty:
                try:
                    self.flush()
                except Exception as exc:
                    log.warning("WorldModel: flush error: %s", exc)

    def _shutdown(self) -> None:
        """Called on process exit — flush everything."""
        self._running = False
        try:
            self.flush()
            log.info("WorldModel: shutdown flush complete → %s", _WORLD_MODEL_PATH)
        except Exception as exc:
            log.warning("WorldModel: shutdown flush failed: %s", exc)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. base provides default keys."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
