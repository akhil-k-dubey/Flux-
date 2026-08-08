"""
flux/language/intent_parser.py

Converts free-form natural language text into structured ParsedQuery objects.

No trained model — rule-based pattern matching with fuzzy concept resolution.
Stage 5 design principle: handle 80% of queries correctly; return a
CLARIFICATION_NEEDED response for the 20% that don't match rather than
guessing silently.

Intent types:
  EXPLAIN            — "what is X", "tell me about X", "explain X"
  CONNECT            — "how does X relate to Y", "what connects X and Y"
  GAP                — "what don't you know about X", "what's missing"
  TEACH_HINT         — "you should know about X", "you're missing X"
  CLARIFICATION_NEEDED — parser confidence too low to commit
  SELF               — "tell me about yourself", "what are you", "who are you"
  CAPABILITY         — "what can you do", "help", "your capabilities"

Multi-intent: "tell me about X and how does it connect to Y"
  → ParsedQuery(intent=EXPLAIN, ..., secondary_intent=ParsedQuery(intent=CONNECT, ...))

Concept resolution pipeline (Q1):
  1. Extract raw concept text from regex group
  2. Normalize: lowercase, remove stopwords, join with underscores
  3. KGStore.get(normalized) → found? done.
  4. QueryEngine.suggest_concepts(normalized) → fuzzy match → best candidate
  5. If still not found: log synonym gap to flux_meta.json

Synonym gaps logged to flux_meta.json under "synonym_gaps" key.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

META_PATH = Path("logs/flux_meta.json")

# ── Intent patterns ────────────────────────────────────────────────────────────
# Each pattern: (compiled regex, group_indices)
# group_indices: int for single concept, (int, int) for two concepts

_EXPLAIN_PATS = [
    re.compile(r"what is (?:a |an |the )?(.+?)[\?\.!]*$",      re.I),
    re.compile(r"what are (?:a |an |the )?(.+?)[\?\.!]*$",     re.I),
    re.compile(r"tell me about (.+?)[\?\.!]*$",                 re.I),
    re.compile(r"explain (?:to me )?(.+?)[\?\.!]*$",            re.I),
    re.compile(r"describe (.+?)[\?\.!]*$",                      re.I),
    re.compile(r"define (.+?)[\?\.!]*$",                        re.I),
    re.compile(r"what do you know about (.+?)[\?\.!]*$",        re.I),
    re.compile(r"give me (?:info|information) (?:on|about) (.+?)[\?\.!]*$", re.I),
]

_CONNECT_PATS = [
    re.compile(r"how does (.+?) (?:relate|connect|link) to (.+?)[\?\.!]*$",           re.I),
    re.compile(r"what connects (.+?) (?:and|to) (.+?)[\?\.!]*$",                      re.I),
    re.compile(r"how (?:is|are) (.+?) (?:related|connected|linked) to (.+?)[\?\.!]*$", re.I),
    re.compile(r"what (?:is the |are the )?(?:connection|relation|link)s? between (.+?) and (.+?)[\?\.!]*$", re.I),
    re.compile(r"how does (.+?) lead to (.+?)[\?\.!]*$",                              re.I),
    re.compile(r"what is the (?:path|route|chain) from (.+?) to (.+?)[\?\.!]*$",      re.I),
    re.compile(r"(?:show|find) (?:the )?(?:path|connection|link) between (.+?) and (.+?)[\?\.!]*$", re.I),
    # Bug 4 fix: "what is the relationship between X and Y"
    re.compile(r"what is (?:the )?relationship between (.+?) and (.+?)[\?\.!]*$",     re.I),
    re.compile(r"what(?:'s| is) (?:the )?difference between (.+?) and (.+?)[\?\.!]*$", re.I),
]

_GAP_PATS = [
    re.compile(r"what don'?t you know(?: about (.+?))?[\?\.!]*$",          re.I),
    re.compile(r"what do you not know(?: about (.+?))?[\?\.!]*$",          re.I),
    re.compile(r"what(?:'s| is) missing(?: (?:from )?(.+?))?[\?\.!]*$",   re.I),
    re.compile(r"what are (?:the )?gaps?(?: in (?:your )?(.+?))?[\?\.!]*$", re.I),
    re.compile(r"what(?:'s| is) unknown(?: about (.+?))?[\?\.!]*$",        re.I),
    re.compile(r"what (?:haven'?t|have not) you (?:learned|been taught)(?: about (.+?))?[\?\.!]*$", re.I),
    # Bug 3 fix: "what are your gaps" / "show me your gaps"
    re.compile(r"(?:what are|show me) your gaps?[\?\.!]*$",                re.I),
    re.compile(r"(?:your|the) gaps?[\?\.!]*$",                             re.I),
]

_TEACH_HINT_PATS = [
    re.compile(r"^you should (?:know|learn) about (.+?)[\?\.!]*$",     re.I),
    re.compile(r"^you(?:'re| are) missing (.+?)[\?\.!]*$",             re.I),
    re.compile(r"^(?:please )?(?:learn|add|know) (?:about )?(.+?)[\?\.!]*$", re.I),
    re.compile(r"^teach yourself (?:about )?(.+?)[\?\.!]*$",           re.I),
    re.compile(r"^you need to know (?:about )?(.+?)[\?\.!]*$",         re.I),
]

# Detect "... and how does it/that connect to Y" appended to an EXPLAIN sentence.
# Bug 2 fix: the suffix can appear anywhere after the primary concept, not just
# at the end of a clean group match. Pattern is now applied on the full raw query.
_CONNECT_SUFFIX_PAT = re.compile(
    r"and how does (?:it|that|this) (?:relate|connect|link) to (.+?)[\?\.!]*$", re.I
)
# Also catches "... and how does it connect to Y" mid-sentence
_CONNECT_SUFFIX_SPLIT = re.compile(
    r"^(.+?)\s+and how does (?:it|that|this) (?:relate|connect|link) to (.+?)[\?\.!]*$", re.I
)

# ── Stopwords (removed during normalization) ───────────────────────────────────
_STOPWORDS = frozenset({
    "the", "a", "an", "its", "their", "of", "about", "for",
    "to", "from", "in", "on", "at", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "my", "your", "his", "her", "our",
})

_PUNCT = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s+")

# ── Fuzzy-matching guard (Fix 1) ───────────────────────────────────────────────
# Tokens in this set are only ever matched via EXACT lookup — never fuzzy.
# Short logical/Boolean primitives would otherwise match every query that
# contains the word as a substring (e.g. "quantum consciousness AND microtubules"
# → resolves to the KG node "and").
_FUZZY_BLOCKLIST: frozenset[str] = frozenset({
    # Logical primitives
    "and", "or", "not", "nor", "xor",
    # Boolean values
    "true", "false",
    # Common short words that happen to exist as KG nodes
    "is", "be", "to", "in", "on", "at",
})

# Normalized queries shorter than this are never sent to fuzzy matching.
_MIN_FUZZY_LEN: int = 4


# Pure pronouns — never valid concept names in a CONNECT pattern
_PRONOUN_SET: frozenset[str] = frozenset({
    "it", "that", "this", "they", "them", "those", "these",
    "the concept", "the idea", "the topic",
})


@dataclass
class ConceptResolution:
    """Result of resolving a raw text snippet to a KG concept ID."""
    raw_text:       str
    normalized:     str          # after normalize_concept()
    concept_id:     str          # best KG ID (may equal normalized if found directly)
    found_in_kg:    bool
    closest:        list[str]    # fuzzy alternatives (may be empty)


@dataclass
class ParsedQuery:
    """
    Structured intent extracted from free-form user text.

    For multi-intent queries (EXPLAIN + CONNECT), primary intent is stored
    in this object and the secondary intent in secondary_intent.
    """
    intent:             str                    # EXPLAIN|CONNECT|GAP|TEACH_HINT|CLARIFICATION_NEEDED
    primary:            ConceptResolution | None = None
    secondary:          ConceptResolution | None = None   # CONNECT only
    raw_text:           str                    = ""
    parser_confidence:  float                  = 1.0
    is_multi_intent:    bool                   = False
    secondary_intent:   "ParsedQuery | None"   = None


# ── IntentParser ───────────────────────────────────────────────────────────────

class IntentParser:
    """
    Rule-based intent parser. No trained model.
    Requires KGStore for concept lookup and QueryEngine for fuzzy matching.
    """

    def __init__(self, store, query_engine) -> None:
        self.store        = store
        self.qengine      = query_engine

    # ── Public ─────────────────────────────────────────────────────────────────

    def parse(self, text: str) -> ParsedQuery:
        """
        Parse free-form text into a ParsedQuery.
        Never raises — returns CLARIFICATION_NEEDED on failure.
        """
        text = text.strip()
        low  = text.lower()

        # Priority order: CONNECT > GAP > TEACH_HINT > EXPLAIN
        # CONNECT is most specific (two concepts), try first

        result = (
            self._try_multi_suffix(text, low)    # Bug 2 fix: check full-sentence split first
            or self._try_connect(text, low)
            or self._try_gap(text, low)
            or self._try_teach_hint(text, low)
            or self._try_explain(text, low)
            or self._try_bare_concept(text, low) # Bug 1 fix: bare noun phrase fallback
        )

        if result is None:
            return ParsedQuery(
                intent            = "CLARIFICATION_NEEDED",
                raw_text          = text,
                parser_confidence = 0.0,
            )

        return result

    def resolve_concept(self, raw_text: str) -> ConceptResolution:
        """
        Resolve raw text to a KG concept ID.

        Semantics:
          found_in_kg=True  ONLY when the normalized form EXACTLY matches a KG node.
          closest            is a list of fuzzy suggestions — for the caller to use.
          concept_id         is ALWAYS the normalized form of what the user said,
                             never substituted with a fuzzy match.

        This keeps TEACH_HINT honest: if the user says "hamiltonian" and it's not
        in the KG, we say "I don't know hamiltonian" — not "I know phase_transition".
        Callers that want fuzzy resolution (EXPLAIN/CONNECT) use closest[0] explicitly.
        """
        normalized = self._normalize(raw_text)
        if not normalized:
            return ConceptResolution(
                raw_text="", normalized="", concept_id="",
                found_in_kg=False, closest=[],
            )

        # Direct exact lookup
        if self.store.get(normalized):
            return ConceptResolution(
                raw_text    = raw_text,
                normalized  = normalized,
                concept_id  = normalized,
                found_in_kg = True,
                closest     = [],
            )

        # Fix 1: Guard fuzzy matching — skip for blocklisted tokens or very short
        # normalized forms.  Short words like "and", "or", "true", "false" exist as
        # KG nodes but must never match a user query via substring/token overlap.
        if normalized in _FUZZY_BLOCKLIST or len(normalized) < _MIN_FUZZY_LEN:
            self._log_synonym_gap(raw_text, normalized, None, resolved=False)
            return ConceptResolution(
                raw_text    = raw_text,
                normalized  = normalized,
                concept_id  = normalized,
                found_in_kg = False,
                closest     = [],
            )

        # Fuzzy suggestions — returned as hints, NOT used as concept_id
        closest = self.qengine.suggest_concepts(normalized)
        # Log only if completely unresolved (no exact match, no fuzzy hit)
        if not closest:
            self._log_synonym_gap(raw_text, normalized, None, resolved=False)
        else:
            self._log_synonym_gap(raw_text, normalized, closest[0], resolved=False)

        return ConceptResolution(
            raw_text    = raw_text,
            normalized  = normalized,
            concept_id  = normalized,   # always the user's own normalized text
            found_in_kg = False,
            closest     = closest,
        )

    # ── Pattern matching helpers ────────────────────────────────────────────────

    def _try_connect(self, text: str, low: str) -> ParsedQuery | None:
        for pat in _CONNECT_PATS:
            m = pat.search(low)
            if m:
                g1 = m.group(1).strip()
                g2 = m.group(2).strip()
                if g1.lower() in _PRONOUN_SET:
                    continue
                p = self.resolve_concept(g1)
                s = self.resolve_concept(g2)
                # Bug 4: reject if primary resolved to a blocklisted/empty concept
                if not p.normalized or p.normalized in _FUZZY_BLOCKLIST:
                    continue
                return ParsedQuery(
                    intent            = "CONNECT",
                    primary           = p,
                    secondary         = s,
                    raw_text          = text,
                    parser_confidence = 0.95,
                )
        return None

    def _try_multi_suffix(self, text: str, low: str) -> ParsedQuery | None:
        """Bug 2 fix: EXPLAIN(X) + CONNECT(X, Y) from a single sentence.

        Matches: '<trigger> <primary> [clause] and how does it <verb> <secondary>'
        After stripping the trigger, uses a secondary CONNECT pattern to pull
        the first concept out of the remaining clause if it's multi-word.
        """
        m = _CONNECT_SUFFIX_SPLIT.search(low)
        if not m:
            return None
        raw_clause    = m.group(1).strip()
        raw_secondary = m.group(2).strip()

        # Strip leading trigger to get the clause containing primary concept
        raw_primary = raw_clause
        for trigger in ("what is ", "what are ", "tell me about ", "explain ",
                        "describe ", "define ", "how does ", "how is ", "how are ",
                        "what do you know about "):
            if raw_primary.startswith(trigger):
                raw_primary = raw_primary[len(trigger):].strip()
                break

        # If clause is still multi-word (e.g. "entropy connect to thermodynamics"),
        # isolate just the leading concept by cutting at a clause verb
        _CLAUSE_VERBS = {"connect", "connects", "relate", "relates", "link",
                         "links", "lead", "leads", "goes", "maps"}
        tokens = raw_primary.split()
        if len(tokens) > 1 and tokens[1].lower() in _CLAUSE_VERBS:
            raw_primary = tokens[0]

        p   = self.resolve_concept(raw_primary)
        s   = self.resolve_concept(raw_secondary)
        sec = ParsedQuery(intent="CONNECT", primary=p, secondary=s,
                          raw_text=text, parser_confidence=0.80)
        return ParsedQuery(
            intent           = "EXPLAIN",
            primary          = p,
            raw_text         = text,
            parser_confidence= 0.80,
            is_multi_intent  = True,
            secondary_intent = sec,
        )

    def _try_gap(self, text: str, low: str) -> ParsedQuery | None:
        for pat in _GAP_PATS:
            m = pat.search(low)
            if m:
                # Some gap patterns have no domain capture group (e.g. "your gaps")
                try:
                    domain_text = (m.group(1) or "").strip()
                except IndexError:
                    domain_text = ""
                p = self.resolve_concept(domain_text) if domain_text else None
                return ParsedQuery(
                    intent            = "GAP",
                    primary           = p,
                    raw_text          = text,
                    parser_confidence = 0.90,
                )
        return None

    def _try_teach_hint(self, text: str, low: str) -> ParsedQuery | None:
        for pat in _TEACH_HINT_PATS:
            m = pat.search(low)
            if m:
                p = self.resolve_concept(m.group(1))
                return ParsedQuery(
                    intent            = "TEACH_HINT",
                    primary           = p,
                    raw_text          = text,
                    parser_confidence = 0.85,
                )
        return None

    def _try_explain(self, text: str, low: str) -> ParsedQuery | None:
        for pat in _EXPLAIN_PATS:
            m = pat.search(low)
            if m:
                raw_primary = m.group(1).strip()

                # Check for appended CONNECT suffix: "... and how does it connect to Y"
                suffix_m   = _CONNECT_SUFFIX_PAT.search(raw_primary)
                secondary_intent = None
                is_multi   = False

                if suffix_m:
                    # Split: primary concept is everything before the suffix
                    raw_primary    = raw_primary[:suffix_m.start()].strip()
                    raw_secondary  = suffix_m.group(1).strip()
                    sec_concept    = self.resolve_concept(raw_secondary)
                    # Will set up secondary_intent below after resolving primary
                    is_multi       = True
                else:
                    raw_secondary  = None
                    sec_concept    = None

                p = self.resolve_concept(raw_primary)

                # Bug 5 fix: if normalized primary is blocklisted, skip this match
                if p.normalized in _FUZZY_BLOCKLIST:
                    continue

                if is_multi and sec_concept:
                    secondary_intent = ParsedQuery(
                        intent            = "CONNECT",
                        primary           = p,
                        secondary         = sec_concept,
                        raw_text          = text,
                        parser_confidence = 0.80,
                    )

                return ParsedQuery(
                    intent            = "EXPLAIN",
                    primary           = p,
                    raw_text          = text,
                    parser_confidence = 0.90 if not is_multi else 0.80,
                    is_multi_intent   = is_multi,
                    secondary_intent  = secondary_intent,
                )
        return None

    def _try_bare_concept(self, text: str, low: str) -> ParsedQuery | None:
        """Bug 1 fix: bare noun phrases (no pattern match) → EXPLAIN."""
        # Only trigger if text looks like a concept name (no verbs/question words)
        skip_starts = ("what", "how", "why", "when", "where", "who", "which",
                       "tell", "show", "explain", "describe", "define", "give",
                       "learn", "add", "please", "you", "teach",)
        stripped = low.strip().rstrip("?.!")
        if any(stripped.startswith(s) for s in skip_starts):
            return None
        # Must be at least 2 chars and not a pure stopword
        if len(stripped) < 2:
            return None
        p = self.resolve_concept(stripped)
        if not p.normalized or p.normalized in _FUZZY_BLOCKLIST:
            return None
        return ParsedQuery(
            intent            = "EXPLAIN",
            primary           = p,
            raw_text          = text,
            parser_confidence = 0.70,  # lower confidence — no explicit pattern
        )

    # ── Concept normalization ───────────────────────────────────────────────────

    @staticmethod
    def _normalize(raw: str) -> str:
        """
        Convert raw concept text to a candidate KG ID.
        'wave function collapse' -> 'wave_function_collapse'
        'the Boltzmann Distribution' -> 'boltzmann_distribution'
        """
        s = raw.lower()
        s = _PUNCT.sub(" ", s)
        s = _MULTI_SPACE.sub(" ", s).strip()
        tokens = [t for t in s.split() if t not in _STOPWORDS]
        return "_".join(tokens) if tokens else s.replace(" ", "_")

    # ── Synonym gap logging ────────────────────────────────────────────────────

    @staticmethod
    def _log_synonym_gap(raw: str, normalized: str, resolved_to: str | None, resolved: bool) -> None:
        """
        Append or update synonym gap in flux_meta.json.
        Keys: raw_text → {count, first_seen, normalized, resolved_to}
        """
        try:
            meta: dict = {}
            if META_PATH.exists():
                meta = json.loads(META_PATH.read_text(encoding="utf-8"))

            gaps: dict = meta.setdefault("synonym_gaps", {})
            entry = gaps.setdefault(raw.lower(), {
                "count":       0,
                "first_seen":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "normalized":  normalized,
                "resolved_to": resolved_to,
                "resolved":    resolved,
            })
            entry["count"] += 1
            if resolved_to and not entry.get("resolved_to"):
                entry["resolved_to"] = resolved_to
                entry["resolved"]    = resolved

            META_PATH.parent.mkdir(parents=True, exist_ok=True)
            META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        except Exception as exc:
            log.warning("Could not write synonym gap to flux_meta.json: %s", exc)
