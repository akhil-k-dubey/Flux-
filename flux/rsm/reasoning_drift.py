"""
flux/rsm/reasoning_drift.py

Stage 6 Layer 2 — continuous confidence drift tracker.

Records the delta between Flux's predicted confidence and the observed
query outcome (found_in_kg / confidence of returned node) per domain cluster.

Fires a FLAG when:
  - cluster has >= WARMUP_SESSIONS recorded (settles before flagging)
  - rolling mean delta > DRIFT_THRESHOLD for CONSECUTIVE_TRIGGER sessions

Cluster = the KG layer of the queried concept (e.g. "thermodynamics").
Written to logs/reasoning_drift.jsonl, one line per inference.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DRIFT_LOG       = Path("logs/reasoning_drift.jsonl")
WARMUP_SESSIONS = 15      # minimum observations before a cluster can flag
DRIFT_THRESHOLD = 0.20    # mean delta that triggers concern
CONSECUTIVE_TRIGGER = 3   # consecutive sessions above threshold before FLAG


def record(cluster: str, predicted_conf: float, observed_conf: float) -> None:
    """Append one drift observation. Called after every ask()."""
    DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":        time.time(),
        "cluster":   cluster or "unknown",
        "predicted": round(predicted_conf, 4),
        "observed":  round(observed_conf, 4),
        "delta":     round(abs(predicted_conf - observed_conf), 4),
    }
    with DRIFT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def flagged_clusters() -> list[dict]:
    """
    Return clusters that have crossed the drift threshold.
    Each entry: {cluster, mean_delta, session_count, consecutive_above}
    """
    if not DRIFT_LOG.exists():
        return []

    # Aggregate per cluster
    by_cluster: dict[str, list[float]] = {}
    for line in DRIFT_LOG.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        by_cluster.setdefault(e["cluster"], []).append(e["delta"])

    flags = []
    for cluster, deltas in by_cluster.items():
        n = len(deltas)
        if n < WARMUP_SESSIONS:
            continue   # still in warm-up — never flag

        mean_delta = sum(deltas) / n
        # Count trailing consecutive sessions above threshold
        consecutive = 0
        for d in reversed(deltas):
            if d > DRIFT_THRESHOLD:
                consecutive += 1
            else:
                break

        if consecutive >= CONSECUTIVE_TRIGGER:
            flags.append({
                "cluster":            cluster,
                "mean_delta":         round(mean_delta, 4),
                "session_count":      n,
                "consecutive_above":  consecutive,
            })

    return flags


def cluster_stats(cluster: str) -> dict:
    """Return raw stats for one cluster (used by diagnose.py)."""
    if not DRIFT_LOG.exists():
        return {}
    deltas = []
    for line in DRIFT_LOG.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e["cluster"] == cluster:
            deltas.append(e["delta"])
    if not deltas:
        return {}
    return {
        "session_count": len(deltas),
        "mean_delta":    round(sum(deltas) / len(deltas), 4),
        "max_delta":     round(max(deltas), 4),
        "recent_deltas": deltas[-10:],  # last 10 for trend analysis
    }
