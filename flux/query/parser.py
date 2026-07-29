"""
flux/query/parser.py

Parses [QUERY] ... [/QUERY] blocks into QueryRequest dataclasses.

Supported fields (all optional except target):
  id             — unique query ID  (auto-generated UUID fragment if absent)
  target         — concept to look up  (REQUIRED)
  depth          — traversal depth 0-3  (default 1)
  mode           — LOOKUP | EXPLAIN | UNKNOWN  (auto-detected if absent)
  min_confidence — float ∈ [0, 1], traversal stops below this  (default 0.3)

Auto-detection rules:
  depth == 0          → mode = LOOKUP
  mode not specified  → mode = EXPLAIN  (depth 1 BFS by default)

Usage::

    from flux.query.parser import parse, QueryParseError

    req = parse("[QUERY]\\ntarget: entropy\\ndepth: 2\\n[/QUERY]")
    # QueryRequest(id='Q_...', target='entropy', depth=2, mode='EXPLAIN', ...)
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass


QUERY_MODES            = frozenset({"LOOKUP", "EXPLAIN", "UNKNOWN"})
DEFAULT_DEPTH          = 1
DEFAULT_MIN_CONFIDENCE = 0.3
MAX_DEPTH              = 3


@dataclass(frozen=True)
class QueryRequest:
    id:             str
    target:         str
    depth:          int
    mode:           str
    min_confidence: float


class QueryParseError(ValueError):
    """Raised when a [QUERY] block is missing required fields or is malformed."""


def parse(query_text: str) -> QueryRequest:
    """
    Parse a [QUERY] block into a QueryRequest.

    Accepts blocks with or without [QUERY] / [/QUERY] tags.
    Field parsing is case-insensitive on field names, values are stripped.

    Raises:
        QueryParseError: if the 'target' field is missing or empty.
    """
    # Extract inner block (between tags if present)
    m = re.search(r"\[QUERY\](.*?)\[/QUERY\]", query_text, re.DOTALL | re.IGNORECASE)
    block = m.group(1) if m else query_text

    fields: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            name, _, value = line.partition(":")
            fields[name.strip().lower()] = value.strip()

    # target is the only required field
    target = fields.get("target", "").strip().lower()
    if not target:
        raise QueryParseError(
            "QUERY block is missing required field: target\n"
            f"Parsed fields: {list(fields.keys())}"
        )

    # depth
    try:
        depth = max(0, min(MAX_DEPTH, int(fields.get("depth", DEFAULT_DEPTH))))
    except ValueError:
        depth = DEFAULT_DEPTH

    # mode (depth=0 forces LOOKUP regardless of what mode says)
    raw_mode = fields.get("mode", "").upper()
    if depth == 0:
        mode = "LOOKUP"
    elif raw_mode in QUERY_MODES:
        mode = raw_mode
    else:
        mode = "EXPLAIN"

    # min_confidence
    try:
        min_conf = float(fields.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
        min_conf = max(0.0, min(1.0, min_conf))
    except ValueError:
        min_conf = DEFAULT_MIN_CONFIDENCE

    # query id
    query_id = fields.get("id") or f"Q_{uuid.uuid4().hex[:8]}"

    return QueryRequest(
        id             = query_id,
        target         = target,
        depth          = depth,
        mode           = mode,
        min_confidence = min_conf,
    )
