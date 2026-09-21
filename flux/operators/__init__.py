"""
flux/operators/

Phase 1 operator system — replaces regex intent classification.

Operators are learned functional units that map utterances to manifold operations.
Each operator has:
  - A name (Build, CanYou, Connect, Assess, Simulate)
  - Input/output type specification
  - Preconditions (what must be true for it to apply)
  - A manifold query function (how it queries the Reality Engine)
  - A feasibility check (for CanYou-type operators)

Operators are NOT hardcoded pattern matchers.
They start as seed specifications and are refined through Akhil's corrections.
"""
from flux.operators.registry import OperatorRegistry, get_registry
from flux.operators.intent_router import IntentRouter

__all__ = ["OperatorRegistry", "get_registry", "IntentRouter"]
