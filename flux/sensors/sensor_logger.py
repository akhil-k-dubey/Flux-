"""
flux/sensors/sensor_logger.py

Stage-A: append-only audit trail. Every action → before/after/diff/prediction.
This is the ground truth record. Never deleted. Verified by Akhil before
Stage-C (KG consequence records) or Stage-B (LTC training) consume it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

SENSOR_LOG = Path("logs/flux_sensors.jsonl")


class SensorLogger:
    """Append-only sensor log. One JSON record per action."""

    def log(
        self,
        *,
        action:      str,
        action_meta: dict,
        before_self: dict,
        after_self:  dict,
        diff_self:   dict,
        prediction:  dict,
        before_sys:  dict | None = None,
        after_sys:   dict | None = None,
    ) -> dict:
        """
        Write one sensor record. Returns the record dict.
        observation_quality is left null for Akhil to fill during Stage-A review.
        """
        prediction_error = {
            k: round(diff_self.get(k, 0) - prediction.get(k, 0), 4)
            for k in set(prediction) | set(diff_self)
        }
        correct = all(
            abs(prediction_error.get(k, 0)) <= 1
            for k in prediction
        )

        record = {
            "timestamp":          time.time(),
            "action":             action,
            "action_meta":        action_meta,
            "prediction":         prediction,
            "snapshot_before":    before_self,
            "snapshot_after":     after_self,
            "diff":               diff_self,
            "prediction_error":   prediction_error,
            "prediction_correct": correct,
            "sys_before":         before_sys,
            "sys_after":          after_sys,
            "observation_quality": None,   # Akhil fills during Stage-A review
            "grounding_type":     "DIRECT_OBSERVATION",
        }

        try:
            SENSOR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with SENSOR_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # never crash the main loop over a log write

        return record
