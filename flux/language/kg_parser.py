"""
flux/language/kg_parser.py

KG-Aware Intent Parser — Flux understands language through her own knowledge.

Pipeline per query:
  1. Pre-dispatch   — structural intents (TEACH/WRONG/CORRECT/SELF/CAPABILITY)
                      handled by rules — unambiguous, zero embedding cost
  2. Span extraction — tokenise + n-grams (1..4-gram), ranked by specificity
  3. KG resolution   — cosine similarity over LTC-embedded concept definitions
  4. Intent inference — structural patterns + resolved concepts + KG edge evidence
  5. Fallback        — IntentParser handles anything unresolvable

Intent path is designed to be KG-native in the future:
  _infer_intent() stores the raw structural signal in ParsedQuery so that
  a future classifier trained on flux_conversations.jsonl can replace the
  rule layer without any interface change.

Returns identical ParsedQuery objects — drop-in for IntentParser.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_EDGE_BOOST = 0.12   # score bonus when two resolved concepts share a KG edge

# ── Stopwords (spans made entirely of these are skipped) ────────────────────
_STOP = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "i", "you", "me", "my", "we", "our", "it", "its", "this", "that",
    "how", "what", "when", "where", "why", "which", "who",
    "do", "does", "did", "have", "has", "had", "can", "could",
    "would", "should", "will", "shall", "may", "might", "must",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "not", "but", "if", "then", "so", "just",
    "tell", "explain", "describe", "show", "give", "get",
    "about", "between", "there", "here", "please", "hmm",
    "like", "think", "thought", "know", "wonder", "wondering",
    "relate", "relates", "related", "relationship", "connection",
    "connect", "connects", "something", "anything", "nothing",
    "flux", "hey", "hi", "okay", "alright", "yeah", "yes", "no",
})

# Words that signal a boundary BETWEEN concepts — always split here
_SPLIT_WORDS = frozenset({
    "and", "or", "to", "with", "from", "of", "the", "a", "an",
    "in", "on", "at", "for", "by", "between", "versus", "vs",
})

# ── Intent signal patterns ───────────────────────────────────────────────────
_CONNECT_RE = re.compile(
    r"relat|connect|link|between|associat|interact|depend|correlat|"
    r"have to do with|what.*to do|compared to|versus|vs\b|"
    r"difference between|similar|same as|like .+ but|"
    r"how does .+ affect|how does .+ work with",
    re.I
)
_GAP_RE = re.compile(
    r"don'?t\s+(?:you\s+)?know|do not know|not know|what.*gap|missing|"
    r"what.*lack|haven'?t\s+(?:yet\s+)?learned|uncertain about|unknown",
    re.I
)
_EXPLAIN_RE = re.compile(
    r"what is|what are|tell me|explain|describe|define|"
    r"how does|how do|how is|how are|what does|what do|"
    r"wonder.*about|curious about|more about",
    re.I
)


class KGParser:
    """
    KG-aware parser. Primary resolver for all Flux queries.
    Falls back to IntentParser on complete resolution failure.
    """

    def __init__(self, store, query_engine=None) -> None:
        from flux.language.intent_parser import IntentParser
        from flux.language import kg_index

        self._store    = store
        self._fallback = IntentParser(store, query_engine)
        self._index    = kg_index.get_index()

        # Ensure index is built (loads cache if available, else builds)
        if not self._index._nodes:
            kg_index.build(store)

    # ── Public interface (identical to IntentParser.parse) ────────────────────

    def parse(self, text: str):
        """Parse free-form query. Returns ParsedQuery (same type as IntentParser)."""
        from flux.language.intent_parser import ParsedQuery, ConceptResolution

        clean = text.strip()
        if not clean:
            return self._fallback.parse(clean)

        # 1. Pre-dispatch: structural intents — no embedding needed
        pre = self._pre_dispatch(clean)
        if pre is not None:
            return pre

        # 2. Intent signal (structural rules — KG-native path ready for future)
        intent_signal = _classify_intent_signal(clean)

        # 3. Extract candidate spans
        spans = _extract_spans(clean)
        if not spans:
            return self._fallback.parse(clean)

        # 4. Resolve concepts — two-pass to prevent composite junk from blocking short spans
        # Pass 1: exact tier-1 matches only (score ≥ 0.99) — lock these concepts first
        exact_resolved: list[tuple[str, str, float]] = []
        exact_seen:     set[str] = set()
        for span in spans:
            hits = self._index.query_one(span, 1)
            if hits and hits[0][1] >= 0.99:
                cid, score = hits[0]
                if cid not in exact_seen:
                    exact_resolved.append((span, cid, score))
                    exact_seen.add(cid)

        # Pass 2: near-matches (≥ 0.35) — only for concepts not already locked
        near_resolved: list[tuple[str, str, float]] = []
        near_seen = set(exact_seen)
        for span in spans:
            hits = self._index.query_one(span, 1)
            if hits:
                cid, score = hits[0]
                if score >= 0.35 and cid not in near_seen:
                    near_resolved.append((span, cid, score))
                    near_seen.add(cid)

        resolved = exact_resolved + near_resolved
        resolved.sort(key=lambda x: x[2], reverse=True)

        if not resolved:
            log.debug("KGParser: no KG hits for spans %s — fallback", spans[:4])
            return self._fallback.parse(clean)


        # 5. Build ConceptResolution objects
        primary_span, primary_id, primary_score = resolved[0]
        primary_res = ConceptResolution(
            raw_text    = primary_span,
            normalized  = primary_id,
            concept_id  = primary_id,
            found_in_kg = True,
            closest     = [],
        )

        secondary_res = None
        if intent_signal == "CONNECT" and len(resolved) >= 2:
            sec_span, sec_id, sec_score = resolved[1]
            # Optionally apply edge boost
            if self._store and _concepts_connected(self._index._store, primary_id, sec_id):
                primary_score  += _EDGE_BOOST
                sec_score      += _EDGE_BOOST
            secondary_res = ConceptResolution(
                raw_text    = sec_span,
                normalized  = sec_id,
                concept_id  = sec_id,
                found_in_kg = True,
                closest     = [],
            )

        # 7. Map signal to final intent
        if intent_signal == "CONNECT" and secondary_res:
            intent = "CONNECT"
        elif intent_signal == "GAP":
            intent = "GAP"
        else:
            intent = "EXPLAIN"

        log.debug("KGParser: %s primary=%s secondary=%s",
                  intent, primary_id,
                  secondary_res.concept_id if secondary_res else "None")

        return ParsedQuery(
            intent    = intent,
            primary   = primary_res,
            secondary = secondary_res,
        )

    # ── Pre-dispatch (rules — identical to IntentParser) ─────────────────────

    def _pre_dispatch(self, text: str):
        """Return ParsedQuery for unambiguous structural intents, else None."""
        upper = text.upper()
        # Delegate all structural intents to the fallback parser's pre-dispatch
        if any(tag in upper for tag in ("[TEACH]", "[WRONG]", "[CORRECT]",
                                        "[PROPOSE", "[DIAGNOSE]")):
            return self._fallback.parse(text)
        return None


# ── Span extraction ──────────────────────────────────────────────────────────

def _extract_spans(text: str) -> list[str]:
    """
    Extract candidate spans for KG resolution.
    Returns spans ranked by: length (longer = more specific), then position.
    Filters stopword-only spans.
    """
    tokens = re.split(r"[\s,;:!?.()\[\]\"']+", text.lower())
    tokens = [t for t in tokens if t]

    candidates: list[tuple[int, str]] = []  # (score, span_text)

    # n-grams from 4 down to 1
    for n in range(4, 0, -1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            # Skip if all stopwords
            if all(g in _STOP for g in gram):
                continue
            if n > 1:
                # Skip if starts or ends with a stopword
                if gram[0] in _STOP or gram[-1] in _STOP:
                    continue
                # Skip if any MIDDLE token is a boundary/connector word
                # e.g. "gibbs free energy AND entropy" → split at AND
                if n > 2 and any(g in _SPLIT_WORDS for g in gram[1:-1]):
                    continue
            span = "_".join(gram)
            score = n * 10 - i  # longer = higher score, earlier = higher score
            candidates.append((score, span))

    # De-duplicate, preserve order by score
    seen:  set[str] = set()
    result: list[str] = []
    for _, span in sorted(candidates, reverse=True):
        if span not in seen:
            seen.add(span)
            result.append(span)

    return result[:14]   # cap candidates for speed


# ── Intent signal classifier ─────────────────────────────────────────────────

def _classify_intent_signal(text: str) -> str:
    """
    Classify the structural intent signal from the query.
    Returns: CONNECT | GAP | EXPLAIN

    Designed for future replacement by a trained classifier —
    the same signal categories will be preserved.
    """
    if _GAP_RE.search(text):
        return "GAP"
    if _CONNECT_RE.search(text):
        return "CONNECT"
    return "EXPLAIN"


def _concepts_connected(store, a: str, b: str) -> bool:
    """Check if two KG concepts share any edge (either direction)."""
    try:
        edges = store._edges_from.get(a, [])
        if any(e.get("target") == b for e in edges):
            return True
        edges = store._edges_from.get(b, [])
        return any(e.get("target") == a for e in edges)
    except Exception:
        return False
