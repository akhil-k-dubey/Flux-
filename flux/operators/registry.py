"""
flux/operators/registry.py

Operator Registry — physical operator definitions.

Each Operator is a functional specification:
  name          — identifier ("Build", "CanYou", "Connect", "Assess", "Simulate")
  input_type    — what kind of slot this operator expects ("concept", "domain", "pair")
  output_type   — what this operator produces ("path", "feasibility", "coverage", "gap")
  manifold_op   — which manifold operation to run ("GEODESIC", "PERTURB", "POINT_QUERY",
                  "GAP", "CLUSTER")
  is_feasibility — True for CanYou: run simulation but do not execute, return bool
  preconditions  — list of conditions that must hold (checked against KG/manifold)

Operator routing is entirely neural (LanguageEncoder + VectorRouter).
Verb-form string matching has been removed — the encoder handles all disambiguation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

OPERATOR_WEIGHTS_FILE = Path("logs/operator_weights.jsonl")


@dataclass
class Operator:
    """
    A learned functional unit mapping utterance intent to manifold operation.

    input_type    : "concept"  — single KG concept expected in slot
                    "domain"   — domain-level concept (broader)
                    "pair"     — two concepts expected (source + target)
                    "any"      — no constraint on slot type
    output_type   : "path"         — causal reasoning chain
                    "feasibility"  — yes/no + nearest feasible endpoint
                    "coverage"     — how much of this concept Flux knows
                    "gap"          — missing concept between two regions
                    "perturbation" — cascade effect of a change
    manifold_op   : one of GEODESIC, PERTURB, POINT_QUERY, GAP, CLUSTER
    is_feasibility: if True, manifold runs the simulation but returns bool,
                    not execution output. (The CanYou operator.)
    preconditions : strings checked before invoking this operator.
                    e.g. ["slot_in_kg", "manifold_loaded"]
    """
    name:           str
    input_type:     str
    output_type:    str
    manifold_op:    str
    is_feasibility: bool = False
    preconditions:  list[str] = field(default_factory=list)
    description:    str = ""


# ── Operators ──────────────────────────────────────────────────────────────────

_SEED_OPERATORS: list[Operator] = [

    Operator(
        name        = "Build",
        input_type  = "concept",
        output_type = "path",
        manifold_op = "GEODESIC",
        preconditions = ["manifold_loaded"],
        description = (
            "Akhil wants a construction path: how to go from current state to "
            "a target concept existing. The manifold finds the causal chain."
        ),
    ),

    Operator(
        name        = "CanYou",
        input_type  = "concept",
        output_type = "feasibility",
        manifold_op = "POINT_QUERY",
        is_feasibility = True,
        preconditions  = ["manifold_loaded"],
        description = (
            "Akhil is asking if a construction or operation is feasible — "
            "not requesting execution. Returns feasible/infeasible + "
            "nearest feasible endpoint if infeasible."
        ),
    ),

    Operator(
        name        = "Connect",
        input_type  = "pair",
        output_type = "path",
        manifold_op = "GEODESIC",
        preconditions = ["manifold_loaded", "two_concepts"],
        description = (
            "Akhil wants the causal/conceptual path between two known concepts. "
            "The manifold traces the geodesic between them."
        ),
    ),

    Operator(
        name        = "Assess",
        input_type  = "concept",
        output_type = "coverage",
        manifold_op = "CLUSTER",
        preconditions = [],
        description = (
            "Akhil is asking about Flux's knowledge depth for a concept or domain. "
            "Reports density (coverage) — structural assessment of KG contents."
        ),
    ),

    Operator(
        name        = "Simulate",
        input_type  = "concept",
        output_type = "perturbation",
        manifold_op = "PERTURB",
        preconditions = ["manifold_loaded"],
        description = (
            "Akhil wants a causal simulation: given a perturbation to a concept, "
            "what cascades through the causal graph?"
        ),
    ),

    Operator(
        name        = "Know",
        input_type  = "concept",
        output_type = "existence",
        manifold_op = "POINT_QUERY",
        preconditions = [],
        description = (
            "Binary familiarity check: does Flux have concept X in its KG? "
            "Returns yes/no + nearest known concept."
        ),
    ),

    Operator(
        name        = "Understand",
        input_type  = "concept",
        output_type = "comprehension",
        manifold_op = "CLUSTER",
        preconditions = [],
        description = (
            "Comprehension quality check: how well does Flux grasp concept X? "
            "Measures edge density + neighbourhood richness."
        ),
    ),

    Operator(
        name        = "Explain",
        input_type  = "concept",
        output_type = "explanation",
        manifold_op = "GEODESIC",
        preconditions = ["manifold_loaded"],
        description = (
            "User wants Flux to OUTPUT structured knowledge about X. "
            "Generates a causal explanation from KG + manifold."
        ),
    ),

    Operator(
        name        = "Greet",
        input_type  = "none",
        output_type = "social",
        manifold_op = "NONE",
        preconditions = [],
        description = (
            "Social openings, closings, phatic expressions. "
            "No concept grounding needed."
        ),
    ),

    Operator(
        name        = "Sync",
        input_type  = "none",
        output_type = "acknowledgment",
        manifold_op = "NONE",
        preconditions = [],
        description = (
            "Mid-conversation state sync — confirming, acknowledging, "
            "correcting, or adjusting Flux's direction."
        ),
    ),

]


class OperatorRegistry:
    """
    Stores operators and correction-weight bookkeeping.
    Operator selection is entirely neural (LanguageEncoder + VectorRouter).
    """

    def __init__(self) -> None:
        self._operators: dict[str, Operator] = {
            op.name: op for op in _SEED_OPERATORS
        }
        # verb → {operator_name → weight} — populated from corrections only
        self._verb_weights: dict[str, dict[str, float]] = {}
        self._load_learned_weights()

    def _load_learned_weights(self) -> None:
        """Load correction-adjusted weights from the operator weights log."""
        if not OPERATOR_WEIGHTS_FILE.exists():
            return
        try:
            with OPERATOR_WEIGHTS_FILE.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    verb  = entry.get("verb", "")
                    op    = entry.get("operator", "")
                    w     = float(entry.get("weight", 1.0))
                    if verb and op:
                        if verb not in self._verb_weights:
                            self._verb_weights[verb] = {}
                        self._verb_weights[verb][op] = w
        except Exception as exc:
            log.warning("OperatorRegistry: could not load weights (%s)", exc)

    def get(self, name: str) -> Optional[Operator]:
        return self._operators.get(name)

    def all_operators(self) -> list[Operator]:
        return list(self._operators.values())

    def verb_weights_for(self, verb: str) -> dict[str, float]:
        return dict(self._verb_weights.get(verb.lower(), {}))

    def best_operator_for_verb(self, verb: str) -> Optional[str]:
        weights = self.verb_weights_for(verb)
        if not weights:
            return None
        return max(weights, key=lambda k: weights[k])

    def record_correction(self,
                          verb: str,
                          wrong_operator: str,
                          correct_operator: str) -> None:
        """Record a verb→operator correction and persist to weights log."""
        verb = verb.lower()
        if verb not in self._verb_weights:
            self._verb_weights[verb] = {}
        old_wrong = self._verb_weights[verb].get(wrong_operator, 0.5)
        self._verb_weights[verb][wrong_operator] = max(0.0, old_wrong - 0.3)
        old_correct = self._verb_weights[verb].get(correct_operator, 0.5)
        self._verb_weights[verb][correct_operator] = min(2.0, old_correct + 0.5)

        OPERATOR_WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "verb":     verb,
            "operator": correct_operator,
            "weight":   self._verb_weights[verb][correct_operator],
        }
        with OPERATOR_WEIGHTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log.info("Operator correction: '%s' → %s (was %s)",
                 verb, correct_operator, wrong_operator)


_REGISTRY: Optional[OperatorRegistry] = None


def get_registry() -> OperatorRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = OperatorRegistry()
    return _REGISTRY
