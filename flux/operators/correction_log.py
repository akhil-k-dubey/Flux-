"""
flux/operators/correction_log.py

Correction Pipeline — records Akhil's corrections as operator training signal.

When Akhil corrects Flux's operator routing, this module:
  1. Records the correction as a structured training example
  2. Updates the OperatorRegistry's verb weights immediately
  3. Persists to logs/operator_corrections.jsonl for future learning

Correction format in logs/operator_corrections.jsonl:
{
  "timestamp":        float,
  "session_id":       str,
  "utterance":        str,          # what Akhil said
  "verb_surface":     str,          # verb phrase that was extracted
  "noun_slot":        str,          # noun that was extracted
  "wrong_operator":   str | null,   # what Flux thought it was
  "correct_operator": str,          # what it actually was
  "correction_text":  str,          # Akhil's words when correcting
}

After 50 corrections on a verb form, the verb is considered calibrated.
Calibration status is tracked in logs/operator_calibration.json.

Usage:
  from flux.operators.correction_log import CorrectionPipeline
  pipeline = CorrectionPipeline()
  pipeline.record(
      utterance="can you build a holographic app",
      verb_surface="can you",
      noun_slot="holographic app",
      wrong_operator="Build",
      correct_operator="CanYou",
      correction_text="no, don't build it, just tell me if it's possible",
      session_id="abc123",
  )
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from flux.operators.registry import get_registry

log = logging.getLogger(__name__)

CORRECTIONS_LOG   = Path("logs/operator_corrections.jsonl")
CALIBRATION_FILE  = Path("logs/operator_calibration.json")

# Verb is "calibrated" after this many corrections
CALIBRATION_THRESHOLD = 50


class CorrectionPipeline:
    """
    Records operator corrections and applies them to the registry immediately.

    This is the RL reward signal for Phase 1. Every correction Akhil makes
    is a training example. The registry adjusts weights on the spot so the
    next query benefits from this correction immediately — no retraining cycle.
    """

    def __init__(self) -> None:
        self._registry    = get_registry()
        self._calibration = self._load_calibration()

    def record(self,
               utterance:        str,
               verb_surface:     str,
               noun_slot:        str,
               correct_operator: str,
               session_id:       str = "",
               wrong_operator:   Optional[str] = None,
               correction_text:  str = "") -> None:
        """
        Record one correction and immediately update verb weights.

        correct_operator: the name of the operator that SHOULD have been invoked.
        wrong_operator:   the name of the operator Flux invoked (None if unrouted).
        """
        ts = time.time()

        # 1. Update registry weights immediately
        if wrong_operator and wrong_operator != correct_operator:
            self._registry.record_correction(
                verb           = verb_surface,
                wrong_operator = wrong_operator,
                correct_operator = correct_operator,
            )
        elif not wrong_operator:
            # Flux was unrouted — teach it this verb → operator mapping
            weights = self._registry.verb_weights_for(verb_surface)
            if correct_operator not in weights:
                self._registry.record_correction(
                    verb             = verb_surface,
                    wrong_operator   = "NONE",
                    correct_operator = correct_operator,
                )

        # 2. Persist correction
        entry = {
            "timestamp":        ts,
            "session_id":       session_id,
            "utterance":        utterance,
            "verb_surface":     verb_surface,
            "noun_slot":        noun_slot,
            "wrong_operator":   wrong_operator,
            "correct_operator": correct_operator,
            "correction_text":  correction_text,
        }
        CORRECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CORRECTIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        # 3. Update calibration tracking
        self._update_calibration(verb_surface)

        log.info("Correction recorded: '%s' → %s (was %s)",
                 verb_surface, correct_operator, wrong_operator or "UNROUTED")

    def is_calibrated(self, verb: str) -> bool:
        """Return True if this verb has enough corrections to be trusted."""
        return self._calibration.get(verb.lower(), 0) >= CALIBRATION_THRESHOLD

    def correction_count(self, verb: str) -> int:
        """Return number of corrections recorded for this verb."""
        return self._calibration.get(verb.lower(), 0)

    def correction_summary(self) -> dict:
        """Return summary of correction counts per verb."""
        total = sum(self._calibration.values())
        calibrated = [v for v, c in self._calibration.items()
                      if c >= CALIBRATION_THRESHOLD]
        return {
            "total_corrections": total,
            "verbs_tracked":     len(self._calibration),
            "calibrated_verbs":  calibrated,
        }

    # ── Private ────────────────────────────────────────────────────────────────

    def _update_calibration(self, verb: str) -> None:
        verb = verb.lower()
        self._calibration[verb] = self._calibration.get(verb, 0) + 1
        try:
            CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CALIBRATION_FILE.open("w", encoding="utf-8") as f:
                json.dump(self._calibration, f, indent=2)
        except Exception as exc:
            log.warning("Could not save calibration: %s", exc)

    def _load_calibration(self) -> dict[str, int]:
        if not CALIBRATION_FILE.exists():
            return {}
        try:
            return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


_PIPELINE: Optional[CorrectionPipeline] = None


def get_pipeline() -> CorrectionPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = CorrectionPipeline()
    return _PIPELINE
