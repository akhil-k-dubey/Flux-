"""
flux/pipeline/schema.py

Constitutional schema for the Flux knowledge graph pipeline.
Single source of truth for all node types, edge types, immutability rules,
and verification contracts. All definitions derive from FLUX_PRIMITIVES.md.

Status: CONSTITUTIONAL - changes require deliberate review.
"""

from __future__ import annotations
from dataclasses import dataclass

VERSION = "0.1"

# -- Node Types ----------------------------------------------------------------
NODE_TYPES = {
    "LOGICAL", "RELATIONAL",  "EPISTEMIC", "RELATIONSHIP", "CONCEPT",
    "ACTION",  "GOAL",        "DOMAIN",    "GRAMMAR",      "ACTOR",
    "AKHIL",   "AI_SYSTEM",   "EVENT",     "AXIOM",        "SPEC",
    "OPERATOR","ENGINE",  "META",
}

# -- Edge Types ----------------------------------------------------------------
EDGE_TYPES = {
    "REQUIRES",      "ENABLES",          "IS_A",             "INSTANCE_OF",
    "RELATES_TO",    "IMPROVES",         "PREVENTS",         "PART_OF",
    "SIMILAR_TO",    "CONTRADICTS",      "CO_OCCURS",        "HAS_A",
    "CONSTRAINS",    "DEFINES",          "LAYER_MEMBER",     "INVERSE_OF",
    "FORMALIZES",    "VERIFIED_BY",      "CRYSTALLIZED_FROM",
    "ANALOGOUS_TO",  "CAUSES",           "DEGRADES",         "PRECEDES",
}

# -- Spec Languages ------------------------------------------------------------
SPEC_LANGUAGES = {"CLINGO", "Z3", "LEAN4"}

# -- Immutability --------------------------------------------------------------
# AXIOM nodes are always immutable (constitutional safety constraints).
# LOGICAL nodes are immutable only in LAYER_0 (primitive layer).
# Any LOGICAL node Flux creates in higher layers is NOT immutable.
IMMUTABLE_TYPES  = frozenset({"AXIOM"})
IMMUTABLE_LAYERS = frozenset({"LAYER_0"})

def is_immutable(node_type: str, layer: str) -> bool:
    "True if this node cannot be modified by RSM under any condition."
    return node_type in IMMUTABLE_TYPES or (
        node_type == "LOGICAL" and layer in IMMUTABLE_LAYERS
    )

# -- Defaults and Thresholds ---------------------------------------------------
DEFAULT_EDGE_TYPE       = "RELATES_TO"
DEFAULT_CONFIDENCE      = 1.0
CONSOLIDATION_THRESHOLD = 0.85   # below this -> review queue, not KG write
DIRECTED                = True
ALLOW_ISOLATED_NODES    = False  # every node must relate to something

# Confidence ground truth by knowledge origin.
# inferred_by_ltc is None - calibrated by MC Dropout at runtime, not here.
CONFIDENCE_RULES: dict[str, float | None] = {
    "directly_taught":   1.0,
    "synthetic_session": 0.95,
    "cross_reference":   0.8,
    "inferred_by_ltc":   None,
}

# -- Layer Map -----------------------------------------------------------------
# Maps section headers in FLUX_PRIMITIVES.md to canonical layer identifiers.
LAYER_MAP: dict[str, str] = {
    f"LAYER {i}": f"LAYER_{i}" for i in range(5)
} | {"LAYER S": "LAYER_S"}

# -- Gap Registry --------------------------------------------------------------
# Accumulates unknown types encountered during parsing.
# meta_builder reads these to populate the gap report - nothing is silently lost.
_edge_gaps: list[str] = []
_node_gaps: list[str] = []

def get_edge_gaps() -> list[str]:   return _edge_gaps.copy()
def get_node_gaps() -> list[str]:   return _node_gaps.copy()
def clear_edge_gaps() -> None:      _edge_gaps.clear()
def clear_node_gaps() -> None:      _node_gaps.clear()
def clear_all_gaps()  -> None:      _edge_gaps.clear(); _node_gaps.clear()

# -- Validation ----------------------------------------------------------------
def validate_node_type(t: str) -> str:
    "Return canonical node type or default to CONCEPT and log the gap."
    if (u := t.upper().strip()) in NODE_TYPES:
        return u
    _node_gaps.append(t)
    return "CONCEPT"

def validate_edge_type(t: str) -> str:
    "Return canonical edge type or default to RELATES_TO and log the gap."
    if (u := t.upper().strip()) in EDGE_TYPES:
        return u
    _edge_gaps.append(t)
    return DEFAULT_EDGE_TYPE

def parse_relates_to(raw: str) -> list[tuple[str, str]]:
    "Parse relates_to string into (target_id, edge_type) pairs."
    if not raw or not raw.strip():
        return []
    result = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        target, _, edge_raw = part.partition(":")
        edge_type = validate_edge_type(edge_raw.strip()) if edge_raw else DEFAULT_EDGE_TYPE
        result.append((target.strip().lower(), edge_type))
    return result

# -- Verification Result -------------------------------------------------------
@dataclass
class VerificationResult:
    "Uniform result type returned by all formal verifiers: Clingo, Z3, Lean4."
    passed:      bool
    trace:       str
    language:    str
    duration_ms: float = 0.0


# -- Negative Example Configuration -------------------------------------------
# Shared by session_builder (text generation) and consolidation_builder (commit targets).
# NEG_PER_TYPE negative sessions per type; 3 types x 3 each = 9 total.
# curriculum rank 9998: trains after all synthetic positives, before REAL (9999).

NEG_PER_TYPE = 3

NEG_SESSION_TYPES = ["immutable", "schema", "empty"]

# Three distinct invalid types for schema negatives -- variety prevents memorisation.
SCHEMA_INVALID_TYPES = ["INVALID_TYPE_XYZ", "FAKE_ACTION_99", "NONEXISTENT_KIND"]

NEG_SESSION_TEMPLATES: dict[str, dict] = {
    "immutable": {
        "description":        "session proposing rewrite of an immutable concept",
        "template":           "[TEACH]\nid: {id}\nconcept: {target}\nis_a: {type}\ndefinition: {target} is redefined as something else entirely\nrelates_to: {target}:CONTRADICTS\nconfidence: 1.0\n[/TEACH]",
        "expected_rejection": "IMMUTABLE_NODE",
    },
    "schema": {
        "description":        "session proposing invalid node type and nonexistent edge",
        "template":           "[TEACH]\nid: {id}\nconcept: invalid_concept_{id}\nis_a: {invalid_type}\ndefinition: a concept with a type that does not exist in schema\nrelates_to: nonexistent_node:FAKE_EDGE_TYPE\nconfidence: 1.0\n[/TEACH]",
        "expected_rejection": "SCHEMA_VIOLATION",
    },
    "empty": {
        "description":        "session with no parseable definition",
        "template":           "[TEACH]\nid: {id}\nconcept: \nis_a: \ndefinition: \n[/TEACH]",
        "expected_rejection": "EMPTY_COMMIT",
    },
}
