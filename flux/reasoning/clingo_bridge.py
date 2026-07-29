"""
flux/reasoning/clingo_bridge.py

Serializes the live KG as Clingo facts and executes Clingo programs.

Responsibilities:
  1. Serialize every KG node and edge to Clingo fact strings
  2. Combine with flux_kg_rules.lp (and optionally flux_axioms.lp)
  3. Inject query-specific atoms (query_source, query_target)
  4. Run Clingo via Python API and parse the answer set
  5. Return raw atom list — inference.py / engine.py handle interpretation

Fact schema (mirrors the proposed schema from design spec):

  concept(id).
  has_type(id, type).
  has_layer(id, layer).
  has_confidence(id, conf_int).    -- confidence × 100 as integer
  immutable_concept(id).           -- immutable nodes only
  relates(source, target, etype).  -- all edges, lowercase edge type

Concept IDs and edge types are sanitized to valid Clingo atom names
(lowercase, underscores only, leading letter guaranteed).
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

import clingo

from flux.kg.store import KGStore

log = logging.getLogger(__name__)

KG_RULES_PATH  = Path("flux_kg_rules.lp")
AXIOMS_PATH    = Path("flux_axioms.lp")


def _clingo_logger(code: clingo.MessageCode, msg: str) -> None:
    """
    Clingo message callback. Suppresses benign AtomUndefined info messages
    (e.g., safety-axiom atoms like 'self_modify', 'sandboxed' that have no
    head — harmless, they fire on empty input for those predicates).
    Only logs real errors.
    """
    if code in (clingo.MessageCode.AtomUndefined,
                clingo.MessageCode.VariableUnbounded):
        return   # info-level: ignore
    log.warning("Clingo [%s]: %s", code, msg)


# ── Atom sanitization ─────────────────────────────────────────────────────────

_NON_ATOM = re.compile(r"[^a-z0-9_]")

# Clingo reserved keywords that cannot appear as atoms in fact position.
# These are all Layer 0 logical primitives in the KG seed.
_CLINGO_RESERVED: frozenset[str] = frozenset({
    "not", "and", "or", "if", "else",
    "true", "false",
    "mod", "xor", "div",
    "count", "sum", "min", "max", "minimize", "maximize",
})

def _atom(s: str) -> str:
    """
    Convert a string to a valid Clingo lowercase atom.
      - Replace non-alphanumeric/underscore chars with '_'.
      - Prefix with 'c_' if the result starts with a digit.
      - Prefix with 'kg_' if the result is a Clingo reserved keyword
        (e.g., concept 'not' → atom 'kg_not', 'true' → 'kg_true').
    """
    s = s.lower()
    s = _NON_ATOM.sub("_", s)
    if s and s[0].isdigit():
        s = "c_" + s
    if s in _CLINGO_RESERVED:
        s = "kg_" + s
    return s or "unknown"


# ── Fact serializer ───────────────────────────────────────────────────────────

def serialize_kg(store: KGStore) -> str:
    """
    Serialize all KG nodes and edges to a Clingo facts string.
    Returns a multi-line string of Clingo facts ready to load.
    """
    lines: list[str] = ["% === KG Facts (auto-generated) ===\n"]

    for node_id, node in store.all_nodes().items():
        nid  = _atom(node_id)
        ntyp = _atom(str(node.get("type")  or "unknown"))
        nlay = _atom(str(node.get("layer") or "unknown"))
        conf = int(round(float(node.get("confidence", 1.0)) * 100))

        lines.append(f"concept({nid}).")
        lines.append(f"has_type({nid}, {ntyp}).")
        lines.append(f"has_layer({nid}, {nlay}).")
        lines.append(f"has_confidence({nid}, {conf}).")

        if node.get("immutable"):
            lines.append(f"immutable_concept({nid}).")

    lines.append("")   # blank separator

    for src_id, edges in store._edges_from.items():
        for edge in edges:
            src   = _atom(src_id)
            tgt   = _atom(edge.get("to", ""))
            etype = _atom(edge.get("type", "relates_to"))
            if src and tgt:
                lines.append(f"relates({src}, {tgt}, {etype}).")

    return "\n".join(lines)


# ── Clingo runner ─────────────────────────────────────────────────────────────

class ClingoBridge:
    """
    Loads KG facts + inference rules and runs Clingo queries.

    One instance per reasoning session — holds the serialized facts
    in memory so queries within a session don't re-serialize the KG.
    Rebuild by calling refresh() after KGStore writes.
    """

    def __init__(self, store: KGStore, load_axioms: bool = False) -> None:
        self.store       = store
        self.load_axioms = load_axioms
        self._facts: str = serialize_kg(store)
        log.debug(
            "ClingoBridge: serialized %d nodes  %d edge facts",
            len(store.all_nodes()),
            self._facts.count("relates("),
        )

    def refresh(self) -> None:
        """Re-serialize KG after new writes."""
        self._facts = serialize_kg(self.store)

    def _base_program(self) -> str:
        """Load flux_kg_rules.lp (and optionally flux_axioms.lp) as strings."""
        parts: list[str] = []
        if KG_RULES_PATH.exists():
            parts.append(KG_RULES_PATH.read_text(encoding="utf-8"))
        else:
            log.warning("flux_kg_rules.lp not found at %s", KG_RULES_PATH)
        if self.load_axioms and AXIOMS_PATH.exists():
            parts.append(AXIOMS_PATH.read_text(encoding="utf-8"))
        return "\n".join(parts)

    def run(
        self,
        extra_facts: str = "",
        collect_atoms: list[str] | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """
        Run Clingo with: base_rules + KG_facts + extra_facts.

        Args:
            extra_facts:   Clingo source to inject (query_source, query_target, etc.)
            collect_atoms: atom names to collect from the answer set, e.g.
                           ["derived", "inconsistency", "reachable"].
                           If None, returns all atoms.
            timeout_seconds: hard wall-clock limit.

        Returns dict:
            {
              "satisfiable": bool,
              "atoms":       list[tuple[str, list[str]]],  # (name, args) pairs
              "models_found": int,
              "time_ms":     float,
            }
        """
        import time
        t0 = time.perf_counter()

        ctl = clingo.Control(["--models=1"], logger=_clingo_logger)

        full_program = "\n".join([
            self._base_program(),
            self._facts,
            extra_facts,
        ])

        ctl.add("base", [], full_program)
        ctl.ground([("base", [])])

        atoms: list[tuple[str, list[str]]] = []
        models_found = 0

        with ctl.solve(yield_=True) as handle:
            for model in handle:
                models_found += 1
                for atom in model.symbols(shown=True):
                    name = atom.name
                    if collect_atoms is None or name in collect_atoms:
                        args = [str(a) for a in atom.arguments]
                        atoms.append((name, args))
                break  # only need first model

        elapsed_ms = (time.perf_counter() - t0) * 1000
        satisfiable = models_found > 0

        log.debug(
            "Clingo: sat=%s  atoms=%d  %.1fms",
            satisfiable, len(atoms), elapsed_ms,
        )

        return {
            "satisfiable":  satisfiable,
            "atoms":        atoms,
            "models_found": models_found,
            "time_ms":      round(elapsed_ms, 2),
        }

    def check_reachable(self, source: str, target: str) -> dict[str, Any]:
        """
        REACHABLE query: is target reachable from source (any non-CONTRADICTS path)?
        """
        src = _atom(source)
        tgt = _atom(target)
        extra = (
            f"query_source({src}).\n"
            f"query_target({tgt}).\n"
            f":- not reachable({src}, {tgt}).\n"  # integrity constraint: fail if not reachable
        )
        result = self.run(extra_facts=extra, collect_atoms=["reachable"])
        return {
            "reachable":  result["satisfiable"],
            "time_ms":    result["time_ms"],
        }

    def derive(self, source: str) -> dict[str, Any]:
        """
        DERIVE query: what can Clingo derive from source (trans_requires,
        trans_enables, indirect_enables)?
        """
        src = _atom(source)
        extra = f"query_source({src}).\n"
        result = self.run(
            extra_facts   = extra,
            collect_atoms = ["derived"],
        )
        # Parse derived/3 atoms: (relation_type, source, target)
        derivations: list[dict] = []
        for name, args in result["atoms"]:
            if name == "derived" and len(args) == 3:
                derivations.append({
                    "relation": args[0],
                    "from":     args[1],
                    "to":       args[2],
                })
        return {
            "source":       source,
            "derivations":  derivations,
            "time_ms":      result["time_ms"],
        }

    def check_consistent(
        self,
        extra_node: dict | None = None,
        extra_edge: dict | None = None,
    ) -> dict[str, Any]:
        """
        CONSISTENT query: check for inconsistencies in the KG.
        Optionally inject a proposed node or edge to check before committing.

        Returns list of violations with name + involved concepts.
        """
        # Re-instantiate with axioms loaded
        ctl_with_axioms = ClingoBridge(
            self.store, load_axioms=True
        )

        extra_facts = ""
        if extra_node:
            nid = _atom(extra_node.get("id", ""))
            if nid:
                extra_facts += f"concept({nid}).\n"
                extra_facts += f"has_type({nid}, {_atom(extra_node.get('type','unknown'))}).\n"
        if extra_edge:
            src   = _atom(extra_edge.get("from", ""))
            tgt   = _atom(extra_edge.get("to",   ""))
            etype = _atom(extra_edge.get("type", "relates_to"))
            if src and tgt:
                extra_facts += f"relates({src}, {tgt}, {etype}).\n"

        result = ctl_with_axioms.run(
            extra_facts   = extra_facts,
            collect_atoms = ["inconsistency"],
        )

        violations: list[dict] = []
        for name, args in result["atoms"]:
            if name == "inconsistency" and len(args) == 3:
                violations.append({
                    "type":    args[0],
                    "source":  args[1],
                    "target":  args[2],
                })

        return {
            "consistent":  len(violations) == 0,
            "violations":  violations,
            "time_ms":     result["time_ms"],
        }
