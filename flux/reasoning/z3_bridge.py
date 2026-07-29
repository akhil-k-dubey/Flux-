"""
flux/reasoning/z3_bridge.py

Z3 formal verification bridge — stub implementation.

Z3 verification requires actual formal spec text on nodes.
Currently all node specs are either null or auto-predicted placeholders.

Implement at Stage 5 when:
  1. spec_head is retrained to produce real Z3/Clingo/LEAN4 spec text
  2. At least some nodes have non-placeholder spec content
  3. z3-solver is installed: pip install z3-solver

Interface is defined here so Stage 5 can fill it in without changing callers.
"""
from __future__ import annotations


class Z3Bridge:
    """
    Formal verification via Z3 SMT solver.
    Currently a stub — raises NotImplementedError for all queries.
    """

    def __init__(self) -> None:
        pass

    def verify_spec(self, spec_text: str, query: str) -> dict:
        """
        Verify a formal Z3 spec against a query.

        Args:
            spec_text: Z3 spec from node['specs']['Z3']
            query:     Z3 assertion to check

        Returns:
            {"result": "SAT" | "UNSAT" | "UNKNOWN", "model": dict | None}

        Raises:
            NotImplementedError: always, until Stage 5 implementation.
        """
        raise NotImplementedError(
            "Z3 verification requires formal spec text on nodes. "
            "All current node Z3 specs are auto-predicted placeholders. "
            "Implement at Stage 5 when spec_head produces real Z3 output "
            "and z3-solver is installed (pip install z3-solver)."
        )

    def check_satisfiable(self, constraints: list[str]) -> dict:
        """
        Check whether a set of Z3 constraint strings is satisfiable.

        Raises:
            NotImplementedError: always, until Stage 5 implementation.
        """
        raise NotImplementedError(
            "Z3Bridge.check_satisfiable() not yet implemented. "
            "See class docstring for conditions."
        )

    def is_available(self) -> bool:
        """Return True if z3-solver is installed and usable."""
        try:
            import z3  # noqa: F401
            return True
        except ImportError:
            return False
