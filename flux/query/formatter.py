"""
flux/query/formatter.py

Converts ranked traversal results into the clean public response dict.

Handles two node formats transparently:
  Seed nodes   — spec lives in KGStore._spec_nodes (id: "spec_{id}_{lang}")
  Bridge nodes — spec lives in node["specs"] dict

The formatter strips internal private fields (_score, _edge_type, _hop)
before returning — callers never see them.
"""
from __future__ import annotations

from flux.kg.store import KGStore
from flux.query.traversal import TraversalResult


def _get_spec(node: dict, store: KGStore) -> dict[str, str | None]:
    """
    Return {"CLINGO": text|None, "Z3": text|None, "LEAN4": text|None}
    for a concept node. Checks bridge-format and seed-format transparently.
    """
    node_id = node.get("id", "")
    return {lang: store.get_spec(node_id, lang) for lang in ("CLINGO", "Z3", "LEAN4")}


def _strip_private(node: dict) -> dict:
    """Remove _score, _edge_type, _hop from a ranked node dict."""
    return {k: v for k, v in node.items() if not k.startswith("_")}


def format_response(
    req_id:        str,
    target:        str,
    target_node:   dict | None,
    ranked:        list[dict],
    traversal:     TraversalResult | None,
    store:         KGStore,
    mode:          str,
    elapsed_ms:    float,
    closest_known: list[str] | None = None,
) -> dict:
    """
    Build the unified public response dict for any query mode.

    Unknown target → found=False with closest_known list.
    Known target   → full node data + optional traversal results.
    """
    # ── Unknown concept ────────────────────────────────────────────────────────
    if target_node is None:
        return {
            "query_id":        req_id,
            "target":          target,
            "found":           False,
            "closest_known":   closest_known or [],
            "response_time_ms": round(elapsed_ms, 2),
        }

    # ── Spec lookup ────────────────────────────────────────────────────────────
    spec         = _get_spec(target_node, store)
    has_spec     = any(v for v in spec.values())
    gaps: list[str] = []
    if not spec.get("CLINGO"):
        gaps.append("no CLINGO spec")
    if not spec.get("Z3"):
        gaps.append("no Z3 spec")
    if not spec.get("LEAN4"):
        gaps.append("no LEAN4 spec")

    # ── Related nodes ──────────────────────────────────────────────────────────
    related: list[dict] = []
    for n in ranked:
        related.append({
            "concept":    n.get("id"),
            "edge":       n.get("_edge_type", "RELATES_TO"),
            "confidence": round(float(n.get("confidence", 0.0)), 4),
            "hop":        n.get("_hop", 1),
        })

    # ── Contradictions found during traversal ──────────────────────────────────
    contradictions: list[str] = []
    if traversal:
        for e in traversal.contradictions:
            contradictions.append(f"{e.get('from')} CONTRADICTS {e.get('to')}")

    # ── Core response ──────────────────────────────────────────────────────────
    response: dict = {
        "query_id":        req_id,
        "target":          target,
        "found":           True,
        "confidence":      round(float(target_node.get("confidence", 0.0)), 4),
        "definition":      target_node.get("definition", ""),
        "type":            target_node.get("type",       ""),
        "layer":           target_node.get("layer",      ""),
        "source":          target_node.get("source",     "SEED"),
        "immutable":       target_node.get("immutable",  False),
        "related":         related,
        "spec":            spec if has_spec else None,
        "gaps":            gaps,
        "response_time_ms": round(elapsed_ms, 2),
    }

    # ── EXPLAIN-only extras ────────────────────────────────────────────────────
    if mode == "EXPLAIN":
        examples     = target_node.get("examples",     [])
        non_examples = target_node.get("non_examples", [])
        if examples:
            response["examples"]     = examples
        if non_examples:
            response["non_examples"] = non_examples

    if contradictions:
        response["contradictions"] = contradictions

    return response
