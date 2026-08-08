"""
flux/sensors/consequence_writer.py

Stage-C: reads verified observations from flux_sensors.jsonl and writes
causal consequence nodes into the KG.

Flow:
  flux_sensors.jsonl → quality gate → KG.write(ACTION_CONSEQUENCE_RECORD)

Quality gate:
  - If observation_quality is set (by Akhil review): use it directly
  - If null: auto-assess from prediction_correct + diff pattern
  - Threshold: quality >= 0.7 to write

Watermark: logs/flux_sensors_watermark.json tracks last processed position
so records are never double-committed.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

SENSOR_LOG  = Path("logs/flux_sensors.jsonl")
WATERMARK   = Path("logs/flux_sensors_watermark.json")
QUALITY_THRESHOLD = 0.7


def _auto_quality(rec: dict) -> float:
    """
    Estimate observation quality when observation_quality is null.
    Based on prediction accuracy and diff pattern.
    """
    diff    = rec.get("diff", {})
    action  = rec.get("action", "QUERY")
    correct = rec.get("prediction_correct", False)

    # Self-diffs are always meaningful at the right timescale
    self_changed = any(
        abs(v) > 0 for k, v in diff.items()
        if k not in ("conf_mean", "conf_min", "conf_max")
    )

    if action == "QUERY":
        # Queries should produce zero KG diff — if they do, perfect signal
        return 0.9 if not self_changed else 0.5

    if action in ("TEACH", "COMMIT"):
        if self_changed and correct:
            return 0.9
        if self_changed and not correct:
            return 0.75   # unexpected change — still real, lower confidence
        if not self_changed:
            # Zero diff on TEACH via ask() — reveals routing truth (valid signal)
            return 0.8

    if action in ("WRONG", "CORRECT"):
        return 0.85 if correct else 0.6

    return 0.5   # PROPOSE and unknown


def _make_consequence_node(rec: dict, quality: float) -> tuple[dict, list[dict]]:
    """Build KG node + edges from a verified sensor record."""
    action      = rec.get("action", "QUERY")
    meta        = rec.get("action_meta", {})
    diff        = rec.get("diff", {})
    ts          = rec.get("timestamp", time.time())
    # Short unique ID from timestamp
    ts_short    = str(int(ts))[-8:]
    node_id     = f"consequence_{action.lower()}_{ts_short}"

    # Summarise what changed
    caused_parts = [f"{k}:{'+' if v>0 else ''}{v}" for k, v in diff.items() if v != 0]
    caused       = " | ".join(caused_parts) if caused_parts else "no_change"

    node = {
        "id":               node_id,
        "type":             "ACTION_CONSEQUENCE_RECORD",
        "action":           action,
        "action_target":    meta.get("input", "")[:60],
        "caused":           caused,
        "diff":             diff,
        "prediction":       rec.get("prediction", {}),
        "prediction_correct": rec.get("prediction_correct", False),
        "grounding_type":   "DIRECT_OBSERVATION",
        "observation_quality": quality,
        "source":           "stage_c",
        "confidence":       round(quality * 0.95, 3),
        "definition":       (
            f"Flux observed: action={action} caused [{caused}] "
            f"at t={ts_short}"
        ),
        "timestamp":        ts,
        "immutable":        False,
    }

    # Edge: consequence node → the action type it documents
    edges = [{
        "from":     node_id,
        "to":       action.lower(),
        "relation": "DOCUMENTS_CONSEQUENCE_OF",
        "weight":   quality,
    }]

    return node, edges


def _load_watermark() -> int:
    """Return last processed record index (0-based line count)."""
    try:
        if WATERMARK.exists():
            return json.loads(WATERMARK.read_text())["last_line"]
    except Exception:
        pass
    return 0


def _save_watermark(line: int) -> None:
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK.write_text(json.dumps({"last_line": line}))


def run(store) -> dict:
    """
    Process unread sensor records and write consequence nodes to KG.
    Returns summary: {written, skipped, low_quality, total_read}
    """
    if not SENSOR_LOG.exists():
        return {"written": 0, "skipped": 0, "low_quality": 0, "total_read": 0}

    lines = SENSOR_LOG.read_text(encoding="utf-8").strip().split("\n")
    lines = [l for l in lines if l.strip()]

    start    = _load_watermark()
    unread   = lines[start:]

    written = skipped = low_quality = 0

    for i, line in enumerate(unread):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue

        # Determine quality
        raw_q = rec.get("observation_quality")
        quality = float(raw_q) if raw_q is not None else _auto_quality(rec)

        if quality < QUALITY_THRESHOLD:
            low_quality += 1
            continue

        node, edges = _make_consequence_node(rec, quality)

        # Skip if already in KG (idempotent)
        if store.get(node["id"]):
            skipped += 1
            continue

        store.write(node, edges)
        written += 1

    _save_watermark(start + len(unread))

    log.info("Stage-C: written=%d  skipped=%d  low_quality=%d", written, skipped, low_quality)
    return {
        "written":      written,
        "skipped":      skipped,
        "low_quality":  low_quality,
        "total_read":   len(unread),
    }
