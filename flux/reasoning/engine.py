"""
flux/reasoning/engine.py

Stage 4 reasoning engine. Single public entry point for all reasoning queries.

Four query types:

  REACHABLE — is concept B reachable from A within depth N?
              Uses Clingo reachability rules.

  PATH      — top-3 paths from A to B by propagated confidence.
              Uses Python BFS (traversal.py infrastructure) + inference.py scoring.
              Clingo is not used here — weighted path finding is Python-native.

  CONSISTENT — does the KG (or a proposed addition) violate any rules?
               Uses Clingo with both flux_kg_rules.lp + flux_axioms.lp.
               Also runs Python-side layer inversion and type checks.

  DERIVE    — what can Flux derive from concept X (transitive requires/enables)?
              Uses Clingo DERIVE query + inference.py scoring.

Tool routing:
  Clingo  → REACHABLE, DERIVE, CONSISTENT
  Python  → PATH (BFS), layer/type checks in CONSISTENT

Usage::

    from flux.reasoning.engine import ReasoningEngine

    engine = ReasoningEngine()

    # REACHABLE
    r = engine.query({"type": "REACHABLE", "source": "schrodinger_equation",
                      "target": "wave_function_collapse", "depth": 5})

    # PATH
    r = engine.query({"type": "PATH", "source": "entropy", "target": "boltzmann_distribution"})

    # CONSISTENT (check full KG)
    r = engine.query({"type": "CONSISTENT"})

    # CONSISTENT (check proposed addition)
    r = engine.query({"type": "CONSISTENT",
                      "node": {"id": "x", "type": "CONCEPT"},
                      "edge": {"from": "x", "to": "entropy", "type": "REQUIRES"}})

    # DERIVE
    r = engine.query({"type": "DERIVE", "source": "schrodinger_equation"})
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from flux.kg.store   import KGStore, get_store
from flux.reasoning.clingo_bridge import ClingoBridge
from flux.reasoning.inference     import (
    propagated_confidence, score_path, score_derivation,
    rank_derivations, ScoredPath, Derivation, EDGE_WEIGHTS,
)

log = logging.getLogger(__name__)

MAX_PATH_DEPTH   = 5
TOP_K_PATHS      = 3
DEFAULT_DEPTH    = 5

CONSISTENCY_SEVERITY: dict[str, str] = {
    "contradicts_and_requires": "ERROR",
    "circular_requires":        "ERROR",
    "type_violation":           "WARNING",
    "layer_inversion":          "WARNING",
}

# Layer ordering for inversion check
LAYER_ORDER: dict[str, int] = {
    "layer_0": 0,
    "layer_1": 1,
    "layer_2": 2,
    "layer_3": 3,
    "layer_4": 4,
    "layer_s": 1,    # structural — treat as peer of Layer 1
}


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ReasoningResult:
    """
    Unified result from any Stage 4 reasoning query.

    Fields populated depend on query_type:
      REACHABLE  → reachable, depth
      PATH       → paths (list of ScoredPath)
      CONSISTENT → consistent, violations
      DERIVE     → derivations (list of Derivation)
    """
    query_type:   str
    source:       str
    target:       str | None          = None
    # REACHABLE
    reachable:    bool | None         = None
    # PATH
    paths:        list[ScoredPath]    = field(default_factory=list)
    # CONSISTENT
    consistent:   bool | None         = None
    violations:   list[dict]          = field(default_factory=list)
    # DERIVE
    derivations:  list[Derivation]    = field(default_factory=list)
    # Meta
    clingo_ms:    float               = 0.0
    total_ms:     float               = 0.0
    error:        str | None          = None

    def __str__(self) -> str:
        if self.error:
            return f"[ERROR] {self.error}"
        if self.query_type == "REACHABLE":
            return (
                f"[REACHABLE] {self.source} -> {self.target}: "
                f"{'YES' if self.reachable else 'NO'}  ({self.total_ms:.1f}ms)"
            )
        if self.query_type == "PATH":
            n = len(self.paths)
            best = f"  best_conf={self.paths[0].propagated:.3f}" if self.paths else ""
            return f"[PATH] {self.source} -> {self.target}: {n} paths found{best}  ({self.total_ms:.1f}ms)"
        if self.query_type == "CONSISTENT":
            if self.consistent:
                return f"[CONSISTENT] KG is consistent  ({self.total_ms:.1f}ms)"
            errs = sum(1 for v in self.violations if CONSISTENCY_SEVERITY.get(v["type"]) == "ERROR")
            warns = len(self.violations) - errs
            return (
                f"[CONSISTENT] {errs} ERRORS  {warns} WARNINGS  ({self.total_ms:.1f}ms)"
            )
        if self.query_type == "DERIVE":
            return (
                f"[DERIVE] {self.source}: {len(self.derivations)} derivations  "
                f"({self.total_ms:.1f}ms)"
            )
        return f"[{self.query_type}]  ({self.total_ms:.1f}ms)"


# ── Engine ────────────────────────────────────────────────────────────────────

class ReasoningEngine:
    """
    Orchestrates Stage 4 reasoning over the live KG.

    Shares the same KGStore singleton as ConsolidationBridge and QueryEngine.
    """

    def __init__(self, store: KGStore | None = None) -> None:
        self.store  = store or get_store()
        self.bridge = ClingoBridge(self.store)

    def refresh(self) -> None:
        """
        Rebuild the Clingo fact base after KGStore writes.
        Call after bridge.process() commits new nodes.
        """
        self.bridge.refresh()

    def query(self, params: dict) -> ReasoningResult:
        """
        Dispatch a reasoning query by type.

        params keys:
          type    — "REACHABLE" | "PATH" | "CONSISTENT" | "DERIVE"
          source  — source concept ID
          target  — target concept ID (REACHABLE, PATH)
          depth   — max traversal depth (default 5)
          node    — proposed node dict (CONSISTENT, optional)
          edge    — proposed edge dict (CONSISTENT, optional)
        """
        t0          = time.perf_counter()
        qtype       = params.get("type", "").upper()
        source      = params.get("source", "")
        target      = params.get("target")
        depth       = int(params.get("depth", DEFAULT_DEPTH))

        try:
            if qtype == "REACHABLE":
                result = self._reachable(source, target or "", depth)
            elif qtype == "PATH":
                result = self._path(source, target or "", depth)
            elif qtype == "CONSISTENT":
                result = self._consistent(
                    params.get("node"),
                    params.get("edge"),
                )
            elif qtype == "DERIVE":
                result = self._derive(source)
            else:
                result = ReasoningResult(
                    query_type = qtype or "UNKNOWN",
                    source     = source,
                    error      = f"Unknown query type: {qtype!r}. "
                                 f"Valid types: REACHABLE, PATH, CONSISTENT, DERIVE",
                )
        except Exception as exc:
            log.exception("ReasoningEngine.query() raised: %s", exc)
            result = ReasoningResult(
                query_type = qtype,
                source     = source,
                error      = str(exc),
            )

        result.total_ms = round((time.perf_counter() - t0) * 1000, 2)
        return result

    # ── REACHABLE ──────────────────────────────────────────────────────────────

    def _reachable(self, source: str, target: str, depth: int) -> ReasoningResult:
        if not source or not target:
            return ReasoningResult(
                query_type = "REACHABLE",
                source     = source,
                target     = target,
                error      = "REACHABLE requires both source and target",
            )
        clingo_result = self.bridge.check_reachable(source, target)
        return ReasoningResult(
            query_type = "REACHABLE",
            source     = source,
            target     = target,
            reachable  = clingo_result["reachable"],
            clingo_ms  = clingo_result["time_ms"],
        )

    # ── PATH (Python BFS + propagation formula) ────────────────────────────────

    def _path(self, source: str, target: str, depth: int) -> ReasoningResult:
        if not source or not target:
            return ReasoningResult(
                query_type = "PATH",
                source     = source,
                target     = target,
                error      = "PATH requires both source and target",
            )
        all_paths = self._bfs_paths(source, target, max_depth=depth)
        scored    = []
        for node_ids in all_paths:
            sp = score_path(node_ids, self.store)
            if sp:
                scored.append(sp)

        scored.sort(key=lambda s: s.propagated, reverse=True)
        top_paths = scored[:TOP_K_PATHS]

        return ReasoningResult(
            query_type = "PATH",
            source     = source,
            target     = target,
            paths      = top_paths,
        )

    def _bfs_paths(self, source: str, target: str, max_depth: int) -> list[list[str]]:
        """
        BFS over KG edges to find all paths from source to target.
        Stops at max_depth hops. Avoids revisiting nodes on any single path.
        """
        found:  list[list[str]] = []
        # queue: (current_node, path_so_far)
        queue: deque[tuple[str, list[str]]] = deque([(source, [source])])

        while queue:
            node, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            for edge in self.store.edges_from(node):
                etype = edge.get("type", "")
                if etype == "CONTRADICTS":
                    continue
                nxt = edge.get("to", "")
                if not nxt or nxt in path:   # avoid cycles on this path
                    continue
                new_path = path + [nxt]
                if nxt == target:
                    found.append(new_path)
                    if len(found) >= TOP_K_PATHS * 4:  # prune early
                        return found
                else:
                    queue.append((nxt, new_path))
        return found

    # ── CONSISTENT ────────────────────────────────────────────────────────────

    def _consistent(
        self,
        extra_node: dict | None,
        extra_edge: dict | None,
    ) -> ReasoningResult:
        # 1. Clingo-based checks (CONTRADICTS+REQUIRES, circular REQUIRES)
        clingo_result = self.bridge.check_consistent(extra_node, extra_edge)
        violations    = list(clingo_result["violations"])

        # 2. Python-side checks (layer inversion, type violations)
        # Apply to proposed extra_node + all KG nodes if checking full KG
        check_nodes = [extra_node] if extra_node else []
        violations += self._python_consistency_checks(check_nodes, extra_edge)

        # Add severity to each violation
        for v in violations:
            v["severity"] = CONSISTENCY_SEVERITY.get(v["type"], "WARNING")

        # Sort: ERRORs first
        violations.sort(key=lambda v: (0 if v["severity"] == "ERROR" else 1))

        return ReasoningResult(
            query_type = "CONSISTENT",
            source     = extra_node.get("id", "KG") if extra_node else "KG",
            consistent = len(violations) == 0,
            violations = violations,
            clingo_ms  = clingo_result["time_ms"],
        )

    def _python_consistency_checks(
        self,
        check_nodes: list[dict],
        extra_edge: dict | None,
    ) -> list[dict]:
        """
        Python-side consistency checks not easily expressible in Clingo:
          - Layer inversion (lower-order node REQUIRES higher-order)
          - Immutable node being marked trainable (type violation)
        """
        violations: list[dict] = []

        # Layer inversion check on proposed edge
        if extra_edge:
            src_id = extra_edge.get("from", "")
            tgt_id = extra_edge.get("to",   "")
            etype  = extra_edge.get("type", "")
            if etype == "REQUIRES":
                src_node = self.store.get(src_id)
                tgt_node = self.store.get(tgt_id)
                if src_node and tgt_node:
                    src_layer = LAYER_ORDER.get(
                        (src_node.get("layer") or "").lower(), 99
                    )
                    tgt_layer = LAYER_ORDER.get(
                        (tgt_node.get("layer") or "").lower(), 99
                    )
                    if src_layer < tgt_layer:   # lower-order REQUIRES higher-order
                        violations.append({
                            "type":    "layer_inversion",
                            "source":  src_id,
                            "target":  tgt_id,
                            "detail":  (
                                f"Layer {src_node.get('layer')} concept requires "
                                f"Layer {tgt_node.get('layer')} concept"
                            ),
                        })

        # Type violation on proposed node
        for node in check_nodes:
            if node.get("immutable") and node.get("trainable"):
                violations.append({
                    "type":   "type_violation",
                    "source": node.get("id", "?"),
                    "target": node.get("id", "?"),
                    "detail": "Node is both immutable and trainable — contradictory flags",
                })

        return violations

    # ── DERIVE ────────────────────────────────────────────────────────────────

    def _derive(self, source: str) -> ReasoningResult:
        if not source:
            return ReasoningResult(
                query_type = "DERIVE",
                source     = source,
                error      = "DERIVE requires a source concept",
            )
        clingo_result = self.bridge.derive(source)
        derivations: list[Derivation] = []

        for raw in clingo_result["derivations"]:
            relation = raw["relation"]
            tgt      = raw["to"]
            # Estimate depth from relation type
            depth_map = {
                "requires":         1,
                "enables":          1,
                "indirect_enables": 2,
                "reachable":        1,
            }
            depth = depth_map.get(relation, 1)
            d = score_derivation(relation, source, tgt, self.store, depth=depth)
            derivations.append(d)

        ranked = rank_derivations(derivations)

        return ReasoningResult(
            query_type  = "DERIVE",
            source      = source,
            derivations = ranked,
            clingo_ms   = clingo_result["time_ms"],
        )
