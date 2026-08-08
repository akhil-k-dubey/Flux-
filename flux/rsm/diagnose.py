"""
flux/rsm/diagnose.py

Stage 6 Layer 2 — [DIAGNOSE] block generator.

Given a flagged cluster + probe result, generates three competing hypotheses
for what's causing the reasoning weakness, scores each, selects the primary,
and returns a Diagnosis object (or None if confidence is too low).

Hypothesis types (exhaustive for Layer 2 targets):
  WEIGHTS_OFF   — REQUIRES/ENABLES edge weights miscalibrated for this cluster
  RULE_DEPTH    — Transitive chain depth decay absent (deep chains over-confident)
  DATA_THIN     — Insufficient training sessions in cluster (not a rule problem)

Confidence scoring uses signals from drift history + KG structure:
  WEIGHTS_OFF: edge traversal frequency × weight vs drift magnitude correlation
  RULE_DEPTH:  recent_deltas trend (does delta grow with chain depth?)
  DATA_THIN:   session_count inverse — fewer sessions → higher data_thin confidence

MIN_DIAGNOSIS_CONFIDENCE = 0.60: if primary < 0.60, log LOW_CONFIDENCE_DIAGNOSIS
and return None (no proposal generated, drift accumulates until signal is clearer).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIN_DIAGNOSIS_CONFIDENCE = 0.60
DIAGNOSE_LOG = Path("logs/layer2_diagnoses.jsonl")

# Hypothesis → proposal target mapping
_HYPOTHESIS_TO_TARGET = {
    "WEIGHTS_OFF":  "edge_confidence_weights",
    "RULE_DEPTH":   "inference_rules",
    "DATA_THIN":    None,   # not a Layer 2 target — suggests teaching instead
}


@dataclass
class Hypothesis:
    name:       str    # WEIGHTS_OFF | RULE_DEPTH | DATA_THIN
    confidence: float
    rationale:  str


@dataclass
class Diagnosis:
    cluster:        str
    primary:        Hypothesis
    alternatives:   list[Hypothesis]
    proposal_target: str   # edge_confidence_weights | inference_rules
    fallback_target: str | None
    raw_block:      str    # the [DIAGNOSE] block text for logging/display


def generate(cluster: str, drift_stats: dict, probe_result) -> Diagnosis | None:
    """
    Generate a Diagnosis from drift stats and probe result.
    Returns None if primary hypothesis confidence < MIN_DIAGNOSIS_CONFIDENCE.
    """
    hypotheses = _score_hypotheses(cluster, drift_stats, probe_result)
    primary    = max(hypotheses, key=lambda h: h.confidence)
    others     = [h for h in hypotheses if h.name != primary.name]

    # Confidence floor guard
    if primary.confidence < MIN_DIAGNOSIS_CONFIDENCE:
        log.info(
            "LOW_CONFIDENCE_DIAGNOSIS cluster=%s  primary=%s conf=%.2f < %.2f — no proposal",
            cluster, primary.name, primary.confidence, MIN_DIAGNOSIS_CONFIDENCE,
        )
        _log_diagnosis(cluster, hypotheses, primary, outcome="LOW_CONFIDENCE")
        return None

    target   = _HYPOTHESIS_TO_TARGET.get(primary.name)
    fallback = _HYPOTHESIS_TO_TARGET.get(others[0].name if others else "") if target else None

    if target is None:
        # DATA_THIN — not a rule problem, don't generate Layer 2 proposal
        log.info("DIAGNOSE[%s]: primary=DATA_THIN — teaching hint needed, not Layer 2", cluster)
        _log_diagnosis(cluster, hypotheses, primary, outcome="TEACHING_HINT")
        return None

    block = _build_block(cluster, drift_stats, hypotheses, primary, target, fallback)
    _log_diagnosis(cluster, hypotheses, primary, outcome="PROPOSAL", block=block)

    return Diagnosis(
        cluster         = cluster,
        primary         = primary,
        alternatives    = others,
        proposal_target = target,
        fallback_target = fallback,
        raw_block       = block,
    )


def _score_hypotheses(cluster: str, stats: dict, probe) -> list[Hypothesis]:
    session_count  = stats.get("session_count", 0)
    mean_delta     = stats.get("mean_delta", 0.0)
    recent_deltas  = stats.get("recent_deltas", [])
    chain_errors   = getattr(probe, "chain_errors", 0)

    # ── WEIGHTS_OFF ─────────────────────────────────────────────────────────
    # High confidence if: chain_errors high AND mean_delta moderate (not extreme)
    weights_conf = min(0.95, (chain_errors / max(1, len(probe.probe_concepts))) * 1.5
                       + (0.2 if 0.15 < mean_delta < 0.35 else 0.0))

    # ── RULE_DEPTH ───────────────────────────────────────────────────────────
    # High confidence if: recent deltas are trending upward (depth compounds error)
    depth_trend = 0.0
    if len(recent_deltas) >= 4:
        mid = len(recent_deltas) // 2
        early_mean = sum(recent_deltas[:mid]) / mid
        late_mean  = sum(recent_deltas[mid:]) / (len(recent_deltas) - mid)
        depth_trend = max(0.0, late_mean - early_mean)  # positive = getting worse
    rule_depth_conf = min(0.95, 0.4 + depth_trend * 2.0)

    # ── DATA_THIN ────────────────────────────────────────────────────────────
    # Confidence inversely proportional to session count (fewer = more likely thin)
    data_thin_conf = max(0.0, min(0.90, 1.0 - (session_count / 50.0)))

    return [
        Hypothesis("WEIGHTS_OFF", round(weights_conf, 3),
                   f"{chain_errors} chain confidence errors in {len(probe.probe_concepts)} probes"),
        Hypothesis("RULE_DEPTH", round(rule_depth_conf, 3),
                   f"delta trend: early={sum(recent_deltas[:len(recent_deltas)//2 or 1]):.2f} → "
                   f"late={sum(recent_deltas[len(recent_deltas)//2:] or [0]):.2f}"),
        Hypothesis("DATA_THIN", round(data_thin_conf, 3),
                   f"only {session_count} sessions recorded for cluster"),
    ]


def _build_block(cluster, stats, hypotheses, primary, target, fallback) -> str:
    lines = [
        "[DIAGNOSE]",
        f"cluster:          {cluster}",
        f"drift_observed:   {stats.get('mean_delta', 0):.3f} mean over {stats.get('session_count', 0)} sessions",
    ]
    for i, h in enumerate(hypotheses, 1):
        lines.append(f"hypothesis_{i}:    {h.name} — confidence: {h.confidence:.2f} ({h.rationale})")
    lines += [
        f"primary_diagnosis: {primary.name} (confidence: {primary.confidence:.2f})",
        f"proposed_target:   {target}",
        f"alternative_if_wrong: {fallback or 'none'}",
        "[/DIAGNOSE]",
    ]
    return "\n".join(lines)


def _log_diagnosis(cluster, hypotheses, primary, outcome, block=""):
    DIAGNOSE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":         time.time(),
        "cluster":    cluster,
        "outcome":    outcome,
        "primary":    {"name": primary.name, "confidence": primary.confidence},
        "hypotheses": [{"name": h.name, "confidence": h.confidence} for h in hypotheses],
        "block":      block,
    }
    with DIAGNOSE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
