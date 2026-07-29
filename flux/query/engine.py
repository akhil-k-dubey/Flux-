"""
flux/query/engine.py

Single public entry point for all query operations.

Pipeline per query:
  parse → store.get → [traverse → rank → check_consistency] → format → log

Usage::

    from flux.query.engine import QueryEngine

    engine = QueryEngine()

    # Direct lookup
    r = engine.query("[QUERY]\\ntarget: entropy\\ndepth: 0\\n[/QUERY]")

    # Multi-hop EXPLAIN
    r = engine.query("[QUERY]\\ntarget: entropy\\ndepth: 2\\n[/QUERY]")

    # Unknown concept (no tags required)
    r = engine.query("target: dark_energy")

    # After bridge.process() commits a new concept — immediately queryable:
    r = engine.query("[QUERY]\\ntarget: wave_function_collapse\\n[/QUERY]")
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flux.kg.store  import KGStore, get_store
from flux.kg.index  import KGIndex
from flux.kg.consistency import ConsistencyChecker
from flux.query.parser    import parse, QueryRequest, QueryParseError
from flux.query.traversal import traverse
from flux.query.ranker    import rank
from flux.query.formatter import format_response

log = logging.getLogger(__name__)

INTERACTION_LOG = Path("logs/flux_interactions.jsonl")


class QueryEngine:
    """
    Orchestrates the full query pipeline.

    Shares the same KGStore singleton as ConsolidationBridge, so nodes
    committed by bridge.process() are immediately visible to engine.query().
    """

    def __init__(self, store: KGStore | None = None) -> None:
        self.store   = store or get_store()
        self.index   = KGIndex(self.store)
        self.checker = ConsistencyChecker(self.store)

    # ── Public API ─────────────────────────────────────────────────────────────

    def query(self, query_text: str) -> dict:
        """
        Process a [QUERY] block (or bare field lines) and return a response dict.

        Never raises — errors are returned as {"error": ..., "found": False}.
        """
        t0 = time.time()

        # 1. Parse
        try:
            req = parse(query_text)
        except QueryParseError as exc:
            return {"error": str(exc), "found": False, "target": None}

        # 2. Lookup target
        target_node = self.store.get(req.target)

        # 3. Unknown target or explicit UNKNOWN mode
        if target_node is None or req.mode == "UNKNOWN":
            closest  = self._find_closest(req.target)
            elapsed  = (time.time() - t0) * 1000
            response = format_response(
                req_id        = req.id,
                target        = req.target,
                target_node   = None,
                ranked        = [],
                traversal     = None,
                store         = self.store,
                mode          = req.mode,
                elapsed_ms    = elapsed,
                closest_known = closest,
            )
            self._log(req, response)
            return response

        # 4. LOOKUP (depth=0): return node data, no traversal
        if req.mode == "LOOKUP" or req.depth == 0:
            elapsed  = (time.time() - t0) * 1000
            response = format_response(
                req_id        = req.id,
                target        = req.target,
                target_node   = target_node,
                ranked        = [],
                traversal     = None,
                store         = self.store,
                mode          = req.mode,
                elapsed_ms    = elapsed,
            )
            self._log(req, response)
            return response

        # 5. EXPLAIN: BFS traversal → rank → consistency check → format
        result  = traverse(req.target, req.depth, req.min_confidence, self.store)
        ranked  = rank(result)

        for path in result.paths.values():
            ok, reason = self.checker.check_path(path)
            if not ok:
                log.warning("Inconsistent traversal path: %s", reason)

        elapsed  = (time.time() - t0) * 1000
        response = format_response(
            req_id        = req.id,
            target        = req.target,
            target_node   = target_node,
            ranked        = ranked,
            traversal     = result,
            store         = self.store,
            mode          = req.mode,
            elapsed_ms    = elapsed,
        )
        self._log(req, response)
        return response

    def suggest_concepts(self, text: str) -> list[str]:
        """
        Public alias for _find_closest. Used by Stage 5 intent parser
        to resolve natural-language concept references to KG IDs.
        Returns up to 3 best-matching concept IDs (empty list if none score > 0.5).
        """
        return self._find_closest(text)

    def refresh_index(self) -> None:
        """Rebuild type/layer indices after a batch of KGStore writes."""
        self.index.rebuild()

    # ── Closest-known (UNKNOWN mode) ───────────────────────────────────────────

    def _find_closest(self, target: str) -> list[str]:
        """
        Three-step string similarity over all known concept IDs.

        Step 1 — Substring match  (weight 3.0)
        Step 2 — Edit distance    (weight 2.0, normalised to [0,1])
        Step 3 — Token overlap    (weight 1.5 per overlapping token)

        Returns up to 3 best-matching concept IDs, or [] if none score > 0.5.
        """
        all_ids       = self.store.all_ids()
        target_lower  = target.lower()
        target_tokens = set(_tokenise(target_lower))

        scores: dict[str, float] = {}

        for cid in all_ids:
            cid_lower  = cid.lower()
            cid_tokens = set(_tokenise(cid_lower))
            s          = 0.0

            # Step 1: substring
            if target_lower in cid_lower or cid_lower in target_lower:
                s += 3.0

            # Step 2: edit distance (normalised, inverted so 0=far, 1=identical)
            dist     = _levenshtein(target_lower, cid_lower)
            max_len  = max(len(target_lower), len(cid_lower), 1)
            s       += (1.0 - dist / max_len) * 2.0

            # Step 3: token overlap
            overlap  = len(target_tokens & cid_tokens)
            s       += overlap * 1.5

            if s > 0.5:
                scores[cid] = s

        return sorted(scores, key=scores.__getitem__, reverse=True)[:3]

    # ── Logging ────────────────────────────────────────────────────────────────

    def _log(self, req: QueryRequest, response: dict) -> None:
        record = {
            "query_id":   req.id,
            "target":     req.target,
            "mode":       req.mode,
            "depth":      req.depth,
            "found":      response.get("found", False),
            "confidence": response.get("confidence"),
            "n_related":  len(response.get("related", [])),
            "elapsed_ms": response.get("response_time_ms"),
            "timestamp":  time.time(),
        }
        try:
            with INTERACTION_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Failed to write interaction log: %s", exc)


# ── String similarity helpers ──────────────────────────────────────────────────

def _tokenise(s: str) -> list[str]:
    """Split on underscores and hyphens."""
    import re
    return [t for t in re.split(r"[_\-]", s) if t]


def _levenshtein(a: str, b: str) -> int:
    """Standard DP Levenshtein distance."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                prev[j]     + 1,                          # deletion
                curr[j - 1] + 1,                          # insertion
                prev[j - 1] + (0 if ca == cb else 1),    # substitution
            ))
        prev = curr
    return prev[-1]
