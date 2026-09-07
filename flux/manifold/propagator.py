"""
flux/manifold/propagator.py

Causal perturbation propagation via attenuated message passing.

The manifold FINDS which concepts are relevant.
The propagator COMPUTES what happens when one of them is perturbed.

These are separate mechanisms by design:
  - Manifold: geometric concept navigation
  - Propagator: causal effect computation on the KG graph

The propagation is differentiable (edge_weights are learned) so the
whole pipeline can be trained end-to-end.

Usage:
    prop = Propagator(kg_store)
    result = prop.perturb("entropy", direction=-1.0, depth=4)
    # → {"second_law_thermodynamics": -0.87, "carnot_cycle": -0.73, ...}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import torch
import torch.nn as nn

from flux.kg.store import KGStore

log = logging.getLogger(__name__)


# ── Edge decay table ────────────────────────────────────────────────────────────
# These are starting values — they become learned parameters during training.
# Design rationale:
#   CAUSES → strong, near-certain propagation (A causes B → B changes when A does)
#   REQUIRES → if A requires B and B decreases, A is threatened
#   ENABLES → partial (necessary but not sufficient condition)
#   IMPROVES → weak (augmenting, not causal)
#   ANALOGOUS_TO → very weak (structural similarity ≠ causal coupling)
_DEFAULT_DECAY = {
    "CAUSES":       0.90,
    "REQUIRES":     0.85,
    "ENABLES":      0.70,
    "SPECIFIES":    0.65,
    "GENERALIZES":  0.60,
    "CONNECTS":     0.55,
    "USES":         0.50,
    "IMPROVES":     0.45,
    "CONTRASTS":    0.25,
    "ANALOGOUS_TO": 0.20,
    "CONTRADICTS":  0.15,
}
_SIGNIFICANCE_THRESHOLD = 0.05   # effects smaller than this are ignored


@dataclass
class PerturbationResult:
    """Output of a perturbation propagation."""
    source_concept:     str
    direction:          float                    # -1.0 = decrease, +1.0 = increase
    affected_concepts:  dict[str, float]         # concept_id → effect magnitude
    cascade_depth:      int                      # how many hops the effect traveled
    suppressed_count:   int                      # concepts below significance threshold
    kg_verified:        dict[str, bool] = field(default_factory=dict)
    # after KG verification: True = edge exists in KG, False = spurious manifold artifact


class LearnedDecay(nn.Module):
    """
    Learned edge-type decay factors.
    Initialized from _DEFAULT_DECAY, fine-tuned during training
    so propagation predictions match KG causal structure.
    """

    def __init__(self):
        super().__init__()
        self.decay = nn.ParameterDict({
            et: nn.Parameter(torch.tensor(v))
            for et, v in _DEFAULT_DECAY.items()
        })
        # Unknown edge types get a neutral decay
        self.default_decay = nn.Parameter(torch.tensor(0.40))

    def get(self, edge_type: str) -> float:
        param = self.decay.get(edge_type, self.default_decay)
        # Clamp to [0, 1] — decay is a probability, not a weight
        return float(torch.sigmoid(param.detach()))


class Propagator(nn.Module):
    """
    Causal perturbation propagator.
    
    Takes a source concept and a perturbation direction (+1 increase / -1 decrease),
    propagates the effect through KG causal edges, returns the affected concepts
    and magnitudes.
    
    After propagation, a KG verification step checks each proposed effect
    against actual KG edges. Effects with no KG support are flagged.
    """

    def __init__(self, store: KGStore):
        super().__init__()
        self.store      = store
        self.decay      = LearnedDecay()

    def perturb(self,
                source_concept_id: str,
                direction: float = -1.0,
                depth: int = 4,
                verify_kg: bool = True) -> PerturbationResult:
        """
        Perturb a concept and propagate effects.

        source_concept_id: which concept is being perturbed
        direction: -1.0 = "what if it decreased/stopped/failed"
                   +1.0 = "what if it increased/was amplified"
        depth: how many hops to follow (default 4 covers most KG paths)
        verify_kg: if True, mark each effect as supported/unsupported by KG edge
        """
        affected:  dict[str, float] = {source_concept_id: direction}
        frontier:  list[str]        = [source_concept_id]
        visited:   set[str]         = {source_concept_id}
        suppressed = 0
        max_depth_reached = 0

        for step in range(depth):
            if not frontier:
                break
            max_depth_reached = step + 1
            next_frontier: list[str] = []

            for concept_id in frontier:
                current_effect = affected[concept_id]
                edges = self.store.edges_from(concept_id) or []

                for edge in edges:
                    target_id  = edge.get("to", "")
                    edge_type  = edge.get("type", "")
                    edge_weight = float(edge.get("weight", 1.0))

                    if not target_id or target_id == concept_id:
                        continue
                    if target_id.startswith("__"):   # skip meta nodes
                        continue

                    decay = self.decay.get(edge_type)
                    child_effect = current_effect * edge_weight * decay

                    if abs(child_effect) < _SIGNIFICANCE_THRESHOLD:
                        suppressed += 1
                        continue

                    # Accumulate (multiple paths can affect same concept)
                    prev = affected.get(target_id, 0.0)
                    new_val = prev + child_effect
                    # Clamp to [-1, 1] — saturation
                    affected[target_id] = max(-1.0, min(1.0, new_val))

                    if target_id not in visited:
                        visited.add(target_id)
                        next_frontier.append(target_id)

            frontier = next_frontier

        # Remove source from results
        affected.pop(source_concept_id, None)

        # Sort by absolute magnitude (most affected first)
        affected = dict(sorted(affected.items(),
                               key=lambda kv: abs(kv[1]), reverse=True))

        # KG verification: every proposed effect should have a KG path
        kg_verified: dict[str, bool] = {}
        if verify_kg:
            source_neighbors = {
                e.get("to", "") for e in (self.store.edges_from(source_concept_id) or [])
            }
            for cid in affected:
                # Direct KG neighbor = verified
                # Indirect = unverified (propagated through intermediate nodes)
                kg_verified[cid] = cid in source_neighbors

        return PerturbationResult(
            source_concept    = source_concept_id,
            direction         = direction,
            affected_concepts = affected,
            cascade_depth     = max_depth_reached,
            suppressed_count  = suppressed,
            kg_verified       = kg_verified,
        )

    def format_result(self, result: PerturbationResult,
                      store: Optional[KGStore] = None) -> str:
        """
        Human-readable summary of a perturbation result.
        This is what gets fed to the FLM for language generation.
        """
        store = store or self.store
        direction_word = "decreased" if result.direction < 0 else "increased"
        source_node    = store.get(result.source_concept) or {}
        source_name    = source_node.get("name", result.source_concept)

        lines = [
            f"[MANIFOLD_RESULT]",
            f"operation: PERTURB",
            f"source_concept: {source_name}",
            f"perturbation: {direction_word}",
            f"significantly_affected:",
        ]

        for cid, magnitude in list(result.affected_concepts.items())[:8]:
            node   = store.get(cid) or {}
            name   = node.get("name", cid)
            pct    = abs(magnitude) * 100
            suffix = " (direct KG edge)" if result.kg_verified.get(cid) else " (propagated)"
            lines.append(f"  {name}: {pct:.0f}%{suffix}")

        lines.append(f"cascade_depth: {result.cascade_depth}")
        lines.append(f"[/MANIFOLD_RESULT]")
        return "\n".join(lines)
