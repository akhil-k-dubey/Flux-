"""
flux/rsm/reasoning_proposal.py

Stage 6 Layer 2 — [PROPOSE_MODIFICATION] block generator.

Given a Diagnosis, generates a concrete, formally-expressible proposal
targeting one of the three valid Layer 2 targets:
  - inference_rules          → new Clingo rule in flux_kg_rules.lp
  - edge_confidence_weights  → KG edge weight adjustment for cluster
  (query_type proposals are generated when RULE_DEPTH diagnosis indicates
   the existing query set is structurally insufficient)

All templates are cluster-parameterised — no arbitrary Clingo generation.
Each hypothesis type maps to a finite set of templates Flux fills in.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

PROPOSAL_LOG = Path("logs/layer2_queue.jsonl")

# Clingo rule templates per hypothesis type
_RULE_TEMPLATES = {
    "RULE_DEPTH": (
        # Depth-decay rule: transitive enables chains of depth >= 3 get reduced confidence weight
        "%% RSM Layer 2: depth-decay on ENABLES chains (proposed for cluster: {cluster})\n"
        "enables_depth(A, B, 1) :- relates(A, B, enables).\n"
        "enables_depth(A, C, D+1) :- enables_depth(A, B, D), relates(B, C, enables), D < 5.\n"
        "deep_enables(A, C) :- enables_depth(A, C, D), D >= 3.\n"
        "%% Confidence penalty for deep chains — downstream rules should prefer direct enables\n"
        "penalised_enables(A, C) :- deep_enables(A, C), not relates(A, C, enables).\n"
    ),
}

_WEIGHT_TEMPLATES = {
    "WEIGHTS_OFF": {
        "description": "Reduce REQUIRES edge confidence weight for cluster {cluster} from {old} to {new}",
        "edge_type":   "REQUIRES",
        "direction":   "reduce",
        "magnitude":   0.08,   # conservative 8% reduction
    },
}


@dataclass
class ReasoningProposal:
    proposal_id:    str
    cluster:        str
    target:         str         # inference_rules | edge_confidence_weights
    hypothesis:     str         # RULE_DEPTH | WEIGHTS_OFF
    rule_text:      str | None  # new Clingo rule (inference_rules only)
    weight_delta:   dict | None # {edge_type, direction, magnitude} (weights only)
    justification:  str
    diagnosis_block: str
    raw_block:      str         # [PROPOSE_MODIFICATION] block text


def generate(diagnosis, drift_stats: dict) -> ReasoningProposal | None:
    """Generate a proposal from a confirmed Diagnosis."""
    target     = diagnosis.proposal_target
    hypothesis = diagnosis.primary.name
    cluster    = diagnosis.cluster

    if target == "inference_rules":
        return _inference_rule_proposal(cluster, hypothesis, drift_stats, diagnosis)
    elif target == "edge_confidence_weights":
        return _weight_proposal(cluster, hypothesis, drift_stats, diagnosis)
    return None


def _inference_rule_proposal(cluster, hypothesis, stats, diagnosis) -> ReasoningProposal:
    template  = _RULE_TEMPLATES.get(hypothesis, "")
    rule_text = template.format(cluster=cluster)

    pid   = _make_id(cluster, hypothesis)
    block = (
        f"[PROPOSE_MODIFICATION]\n"
        f"proposal_id:    {pid}\n"
        f"target:         flux_kg_rules.lp\n"
        f"hypothesis:     {hypothesis}\n"
        f"cluster:        {cluster}\n"
        f"rule_text:      |\n"
        + "\n".join("  " + l for l in rule_text.splitlines()) + "\n"
        f"justification:  {diagnosis.primary.rationale}\n"
        f"drift_mean:     {stats.get('mean_delta', 0):.3f}\n"
        f"[/PROPOSE_MODIFICATION]"
    )

    proposal = ReasoningProposal(
        proposal_id     = pid,
        cluster         = cluster,
        target          = "inference_rules",
        hypothesis      = hypothesis,
        rule_text       = rule_text,
        weight_delta    = None,
        justification   = diagnosis.primary.rationale,
        diagnosis_block = diagnosis.raw_block,
        raw_block       = block,
    )
    _log(proposal)
    return proposal


def _weight_proposal(cluster, hypothesis, stats, diagnosis) -> ReasoningProposal:
    tmpl = _WEIGHT_TEMPLATES.get(hypothesis, {})
    old_weight = 0.95
    new_weight = round(old_weight - tmpl.get("magnitude", 0.08), 3)

    pid   = _make_id(cluster, hypothesis)
    block = (
        f"[PROPOSE_MODIFICATION]\n"
        f"proposal_id:    {pid}\n"
        f"target:         edge_confidence_weights\n"
        f"hypothesis:     {hypothesis}\n"
        f"cluster:        {cluster}\n"
        f"edge_type:      {tmpl.get('edge_type', 'REQUIRES')}\n"
        f"old_weight:     {old_weight}\n"
        f"new_weight:     {new_weight}\n"
        f"justification:  {diagnosis.primary.rationale}\n"
        f"drift_mean:     {stats.get('mean_delta', 0):.3f}\n"
        f"[/PROPOSE_MODIFICATION]"
    )

    proposal = ReasoningProposal(
        proposal_id     = pid,
        cluster         = cluster,
        target          = "edge_confidence_weights",
        hypothesis      = hypothesis,
        rule_text       = None,
        weight_delta    = {
            "edge_type":  tmpl.get("edge_type", "REQUIRES"),
            "cluster":    cluster,
            "old_weight": old_weight,
            "new_weight": new_weight,
        },
        justification   = diagnosis.primary.rationale,
        diagnosis_block = diagnosis.raw_block,
        raw_block       = block,
    )
    _log(proposal)
    return proposal


def _make_id(cluster: str, hypothesis: str) -> str:
    key = f"{cluster}_{hypothesis}_{time.time():.0f}"
    return "L2_" + hashlib.md5(key.encode()).hexdigest()[:10]


def _log(proposal: ReasoningProposal) -> None:
    PROPOSAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":             time.time(),
        "proposal_id":    proposal.proposal_id,
        "cluster":        proposal.cluster,
        "target":         proposal.target,
        "hypothesis":     proposal.hypothesis,
        "state":          "PENDING_VERIFICATION",
        "rule_text":      proposal.rule_text,
        "weight_delta":   proposal.weight_delta,
        "justification":  proposal.justification,
        "diagnosis_block":proposal.diagnosis_block,
        "raw_block":      proposal.raw_block,
    }
    with PROPOSAL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
