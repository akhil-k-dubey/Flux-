"""
flux/reasoning/intent_classifier.py

Classifies the FORM of a query to determine HOW Flux should respond.

Intents:
  EXPLAIN        — "what is X?" / "define X" / "tell me about X"
  CONNECT        — "how does X relate to Y?" / "link X and Y"
  REASON         — "given X, what follows?" / "what would happen if X?"
  COUNTERFACTUAL — "what if X were reversed/absent?" / "what would break if?"
  HYPOTHESIZE    — "how would I get from X to Y?" / "path from X toward Y"
  AUDIT          — "what do you know about X?" / "what are you missing?"
  SELF           — "what are you?" / "describe yourself" / "how do you work?"
  DECISION       — "why did you propose X?" / "why did you choose Y?"
  SYNTHESIS      — "what is the intersection of X, Y, Z?"  (multi-domain)
  ELABORATE      — "tell me more" / "go deeper" / "keep going"
  FEEDBACK       — "[WRONG]" / "[CORRECT]" / human feedback signals
  UNKNOWN        — cannot classify
"""
from __future__ import annotations
import re
from typing import NamedTuple

# ── Intent patterns ────────────────────────────────────────────────────────────

# DECISION — must be before REASON to catch "why did you"
_DECISION_RE = re.compile(
    r"\bwhy did you\b|\bwhy did flux\b|\bwhy (did|do) (you|flux|she) (propose|decide|choose|pick|implement|add|change|select)\b"
    r"|\bwhat made you\b|\bwhat was (your|the) reason(ing)? (for|behind)\b"
    r"|\bwhy (was|were|is|are) (the|this|that|your) (decision|proposal|change|choice)\b"
    r"|\bexplain your (decision|reasoning|rationale|proposal)\b"
    r"|\bwhy (propose|chose|selected)\b",
    re.I,
)

# COUNTERFACTUAL — "what if X were reversed / not true / absent"
_COUNTERFACTUAL_RE = re.compile(
    r"\bwhat (would|will|could|might) (break|happen|fail|change|result)\b.{0,60}\bif\b"
    r"|\bif .{1,60}\b(reversed?|violated?|absent|removed|disabled|negated|weren.t|wasn.t|didn.t)\b"
    r"|\bwhat (would break|would fail|would change)\b"
    r"|\b(reversed?|violated?|negated)\b.{0,40}\b(what|consequence|implication|effect)\b"
    r"|\bwithout .{1,40} (what|how)\b"
    r"|\bif we (remove|ignore|reverse|negate|violate)\b",
    re.I,
)

# SYNTHESIS — multi-domain unsolved / intersection queries
_SYNTHESIS_RE = re.compile(
    r"\bintersection of\b|\bunsolved\b.{0,40}\b(problem|question|connection|link)\b"
    r"|\bmost interesting\b.{0,60}\b(connection|intersection|link|bridge)\b"
    r"|\bwhat connects\b.{0,40}\band\b"
    r"|\bacross (all|both|these|the) (domain|field|area|concept|discipline)\b"
    r"|\bgiven everything you know about\b"
    r"|\bbased on everything\b.{0,60}\bwhat\b"
    r"|\bthree most important\b|\bmost important concept\b",
    re.I,
)

_REASON_RE = re.compile(
    r"\bgiven\b|\bif\b.{1,40}\bthen\b|\bwhat (would|will|happens?|follows?|results?)\b"
    r"|\bwhat is the (consequence|effect|result|outcome|implication)\b"
    r"|\bwhat does .{1,30} lead to\b|\bwhat follows from\b"
    r"|\bimplication of\b|\bconsequence of\b|\bderive\b|\bdeduce\b",
    re.I,
)

_HYPOTHESIZE_RE = re.compile(
    r"\bhow (would|do|can|could) (i|we|one)?\b.{0,30}\bget to\b"
    r"|\bhow (would|do|can|could) (i|we|one)?\b.{0,30}\breach\b"
    r"|\bget from\b.{1,60}\bto\b"                 # ← "get from X to Y"
    r"|\bfrom\b.{1,40}\bto\b.{0,30}\b(reach|get|go|arrive)\b"
    r"|\bpath (from|toward|to)\b|\bpathway (from|to)\b"
    r"|\bhow to (get to|achieve|reach|arrive at)\b"
    r"|\bhow would (i|we|one) (build|create|construct|derive|get)\b",
    re.I,
)

_AUDIT_RE = re.compile(
    r"\bwhat do you know\b|\bwhat (are you|is flux) (missing|lacking|uncertain about)\b"
    r"|\bwhat (don.t|do not|dont) you know\b|\bgaps?\b.{0,20}\bknowledge\b"
    r"|\bknowledge.{0,20}\bgap\b|\bwhat.{0,20}\bmissing\b"
    r"|\baudit\b|\bwhat have you learned\b|\bwhat.{0,30}\bknow about\b"
    r"|\bwhat.{0,30}\buncertain\b",
    re.I,
)

_SELF_RE = re.compile(
    r"\bwhat are you\b|\bwho are you\b|\bdescribe yourself\b"
    r"|\bhow do you work\b|\bhow (does|do) flux work\b"
    r"|\bwhat (can|do) you (do|know|remember|learn)\b"
    r"|\byour (architecture|design|structure|components?|memory|training)\b"
    r"|\bhow (are|were) you (built|trained|made|designed)\b"
    r"|\bwhat is flux[?!.\s]*$|\bwho is flux[?!.\s]*$"   # only at END of query
    r"|\btell me about (yourself|you)\b"                  # NOT "tell me about flux X"
    r"|\byour (capabilities|abilities|skills|purpose|goal)\b"
    # Creator / origin — "who built/created/made you/flux"
    r"|\bwho (built|created|made|designed|wrote|programmed) (you|flux)\b"
    r"|\bwho is your (creator|builder|maker|author|designer)\b"
    r"|\bwho (is|was) (your|flux.?s) (creator|builder|maker)\b",
    re.I,
)


_ELABORATE_RE = re.compile(
    r"\btell me more\b|\belaborate\b|\bgo deeper\b|\bexpand on\b"
    r"|\bmore detail\b|\bin more detail\b|\bdig deeper\b|\bkeep going\b"
    r"|\bgo on\b|\bsay more\b|\bcontinue\b|\bwhat else\b"
    r"|\bexplain (that|more|further|in detail)\b"
    r"|\bmore about (that|this|it)\b|\bcan you clarify\b",
    re.I,
)

_CONNECT_RE = re.compile(
    r"\brelat(e|es|ion(ship)?)\b|\bconnect(ion|s)?\b|\blink(ed|s)?\b"
    r"|\bbetween\b.{1,50}\band\b|\bdifference between\b|\bcompare\b"
    r"|\bvs\.?\b|\bversus\b|\bhow.{1,20}relate\b|\bsimilar to\b",
    re.I,
)

_EXPLAIN_RE = re.compile(
    r"\bwhat is\b|\bwhat are\b|\bdefin(e|ition of)\b|\btell me about\b"
    r"|\bexplain\b|\bdescribe\b|\bhow does\b|\bhow do\b"
    r"|\bwhat does.{1,20}mean\b|\bmeaning of\b",
    re.I,
)

_FEEDBACK_RE = re.compile(
    r"^\s*\[(WRONG|CORRECT|PARTIAL|FEEDBACK)\]"
    r"|\byou (are|were) wrong\b|\bincorrect\b|\bthat.{0,10}\bwrong\b"
    r"|\bthat.{0,10}\bright\b|\bcorrect\b|\bgood answer\b|\bbad answer\b",
    re.I,
)


class ClassifiedIntent(NamedTuple):
    intent:     str    # EXPLAIN | CONNECT | REASON | HYPOTHESIZE | AUDIT | SELF | ELABORATE | FEEDBACK | UNKNOWN
    confidence: float  # 0.0 – 1.0
    signals:    list[str]   # debug: which patterns fired


def classify(text: str) -> ClassifiedIntent:
    """
    Classify the intent of a free-form query.

    Priority order (first match wins):
      1. FEEDBACK        — structural signals
      2. DECISION        — "why did you propose/decide X?" (before AUDIT)
      3. ELABORATE       — follow-up / elaboration phrases
      4. AUDIT           — knowledge gap / what-do-you-know queries
      5. SELF            — questions about Flux herself (architecture, identity)
      6. SYNTHESIS       — multi-domain intersection / unsolved problem queries
      7. COUNTERFACTUAL  — "what if X were reversed/absent?" (before REASON)
      8. REASON          — causal / conditional queries
      9. HYPOTHESIZE     — path-finding / how-to-reach queries
      10. CONNECT        — relation / comparison queries
      11. EXPLAIN        — default information retrieval
      12. UNKNOWN        — nothing matched
    """
    t = text.strip()
    signals: list[str] = []

    if _FEEDBACK_RE.search(t):
        signals.append("feedback_pattern")
        return ClassifiedIntent("FEEDBACK", 0.95, signals)

    # DECISION before ELABORATE/AUDIT — "why did you propose" is a decision query
    if _DECISION_RE.search(t):
        signals.append("decision_pattern")
        return ClassifiedIntent("DECISION", 0.95, signals)

    if _ELABORATE_RE.search(t):
        signals.append("elaborate_pattern")
        return ClassifiedIntent("ELABORATE", 0.90, signals)

    # AUDIT before SELF — "what do you know" is an audit, not a self-description
    if _AUDIT_RE.search(t):
        signals.append("audit_pattern")
        return ClassifiedIntent("AUDIT", 0.85, signals)

    if _SELF_RE.search(t):
        signals.append("self_pattern")
        return ClassifiedIntent("SELF", 0.90, signals)

    # SYNTHESIS before REASON — multi-domain intersection is not a causal query
    if _SYNTHESIS_RE.search(t):
        signals.append("synthesis_pattern")
        return ClassifiedIntent("SYNTHESIS", 0.85, signals)

    # COUNTERFACTUAL before REASON — "what would break if reversed" ≠ forward chain
    if _COUNTERFACTUAL_RE.search(t):
        signals.append("counterfactual_pattern")
        return ClassifiedIntent("COUNTERFACTUAL", 0.90, signals)

    if _REASON_RE.search(t):
        signals.append("reason_pattern")
        return ClassifiedIntent("REASON", 0.85, signals)

    if _HYPOTHESIZE_RE.search(t):
        signals.append("hypothesize_pattern")
        return ClassifiedIntent("HYPOTHESIZE", 0.85, signals)

    if _CONNECT_RE.search(t) and _EXPLAIN_RE.search(t):
        signals.append("connect+explain_patterns")
        return ClassifiedIntent("CONNECT", 0.75, signals)

    if _CONNECT_RE.search(t):
        signals.append("connect_pattern")
        return ClassifiedIntent("CONNECT", 0.80, signals)

    if _EXPLAIN_RE.search(t):
        signals.append("explain_pattern")
        return ClassifiedIntent("EXPLAIN", 0.80, signals)

    return ClassifiedIntent("UNKNOWN", 0.3, ["no_pattern_matched"])

