"""
flux/operators/constitutional_layer.py

Constitutional Layer — shadow safety inspector.

At 98.6%+ accuracy the neural encoder drives execution.
The constitutional layer watches in shadow mode using the SAME geometric
signal as the router — cosine similarity on op_vec against operator centroids.

Three tiers:
  GREEN  conf >= 0.85, geometric constitutional agrees
         -> RL reward +1.0, execute silently
  AMBER  conf < 0.85 OR constitutional uncertain (score < GEO_THRESHOLD)
         -> RL reward  0.0, execute, log for review
  RED    conf >= 0.85, constitutional strongly disagrees
         -> RL reward -1.0, log + penalise
         -> if hard_mode=True also blocks execution (guardrail)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import torch

log = logging.getLogger(__name__)

_LOG_PATH        = Path("logs/constitutional_log.jsonl")
_AMBER_THRESHOLD = 0.85   # neural confidence threshold
_GEO_THRESHOLD   = 0.70   # VectorRouter cosine score to be constitutionally certain

# Operators where constitutional check is meaningful.
# Social ops (Greet/Sync) are intent-based — high surface variability makes
# geometric constitutional check unreliable for them.
_STRICT_OPS = frozenset({
    "Build", "CanYou", "Connect", "Assess", "Simulate",
    "Know", "Understand", "Explain",
})

_TIER_REWARD: dict[str, float] = {"green": +1.0, "amber": 0.0, "red": -1.0}


@dataclass
class InspectionResult:
    tier:              str
    constitutional_op: str
    divergence:        float
    rl_reward:         float
    should_block:      bool = False
    detail:            str  = ""


class ConstitutionalLayer:
    """
    Shadow safety inspector — instantiated once inside FluxInterface.
    Call inspect() after every successful neural route.
    Never drives execution. Only watches, measures, and signals.
    """

    def __init__(self, registry, hard_mode: bool = False) -> None:
        self._registry  = registry
        self._hard_mode = hard_mode

    def inspect(
        self,
        utterance:  str,
        neural_op:  str,
        confidence: float,
        op_vec:     Optional[torch.Tensor] = None,
    ) -> InspectionResult:
        """
        Run constitutional check in shadow mode.
        Returns tier, constitutional prediction, divergence, RL reward.
        Never raises.
        """
        try:
            tl         = utterance.lower().strip()
            const_op   = self._geometric_check(op_vec, neural_op)
            divergence = self._compute_divergence(neural_op, const_op, confidence)
            tier       = self._classify_tier(neural_op, confidence, divergence)
            reward     = _TIER_REWARD[tier]
            block      = self._hard_mode and tier == "red"
            detail     = (
                f"neural={neural_op}(conf={confidence:.3f}) "
                f"constitutional={const_op or 'uncertain'} "
                f"div={divergence:.2f} tier={tier}"
            )
            self._log(tl, neural_op, confidence, const_op, tier, divergence)
            return InspectionResult(
                tier=tier, constitutional_op=const_op,
                divergence=divergence, rl_reward=reward,
                should_block=block, detail=detail,
            )
        except Exception as exc:
            log.debug("ConstitutionalLayer.inspect error: %s", exc)
            return InspectionResult(
                tier="amber", constitutional_op="", divergence=0.5,
                rl_reward=0.0, detail=f"error: {exc}",
            )

    def _geometric_check(
        self, op_vec: Optional[torch.Tensor], neural_op: str
    ) -> str:
        """
        Geometric constitutional verification using VectorRouter cosine similarity.

        The constitutional layer uses the SAME geometric signal as the router:
          - If VectorRouter's top operator agrees with neural_op → constitutional agrees
          - If VectorRouter is confident about a DIFFERENT operator → RED divergence
          - If VectorRouter score < _GEO_THRESHOLD → uncertain → AMBER

        Returns: predicted operator name (geometric opinion) or "" if uncertain.
        """
        if neural_op not in _STRICT_OPS:
            return ""
        if op_vec is None:
            return ""

        try:
            from flux.operators.vector_router import get_vector_router
            _vr = get_vector_router()   # returns already-loaded singleton, no args needed
            if _vr is None or not _vr.is_ready():
                return ""

            geo_op, geo_score = _vr.route(op_vec)

            if geo_score < _GEO_THRESHOLD or geo_op == "None":
                return ""   # geometric signal too weak → uncertain

            return geo_op
        except Exception as exc:
            log.debug("Geometric constitutional check failed: %s", exc)
            return ""

    def _compute_divergence(self, neural_op: str, const_op: str, confidence: float) -> float:
        if not const_op:
            return 0.5
        return 0.0 if neural_op == const_op else confidence

    def _classify_tier(self, neural_op: str, confidence: float, divergence: float) -> str:
        if neural_op not in _STRICT_OPS:
            return "green" if confidence >= _AMBER_THRESHOLD else "amber"
        if divergence == 0.0 and confidence >= _AMBER_THRESHOLD:
            return "green"
        if divergence >= _AMBER_THRESHOLD and confidence >= _AMBER_THRESHOLD:
            return "red"
        return "amber"

    def _log(self, utterance: str, neural_op: str, confidence: float,
             const_op: str, tier: str, divergence: float) -> None:
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.time(), "utterance": utterance,
                "neural_op": neural_op, "confidence": round(confidence, 4),
                "constitutional_op": const_op or "uncertain",
                "divergence": round(divergence, 4), "tier": tier,
            }
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def stats(self) -> dict:
        """Return tier distribution from the log."""
        counts = {"green": 0, "amber": 0, "red": 0, "total": 0}
        try:
            if _LOG_PATH.exists():
                for line in _LOG_PATH.read_text().splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    t = row.get("tier", "amber")
                    counts[t] = counts.get(t, 0) + 1
                    counts["total"] += 1
        except Exception:
            pass
        return counts


_instance: Optional[ConstitutionalLayer] = None


def get_constitutional_layer(hard_mode: bool = False) -> ConstitutionalLayer:
    global _instance
    if _instance is None:
        from flux.operators.registry import get_registry
        _instance = ConstitutionalLayer(get_registry(), hard_mode=hard_mode)
    return _instance
