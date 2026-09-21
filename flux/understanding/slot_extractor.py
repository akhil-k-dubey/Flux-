"""
flux/understanding/slot_extractor.py

Intent-specific slot extraction — BEFORE touching the KG.

This module parses the GRAMMATICAL STRUCTURE of Akhil's queries.
It doesn't know about KG concepts at all. It just answers:
  "Given this intent, what are the argument slots in this sentence?"

Examples:
  SYNTHESIS + "what connects entropy and information theory?"
    → slots: ["entropy", "information theory"]

  AUDIT + "what do you know about dynamic programming?"
    → slots: ["dynamic programming"]

  COUNTERFACTUAL + "what breaks if the second law were reversed?"
    → slots: ["second law"]

No n-gram matching against 2071 IDs. No regex over the full KG.
Just grammar-driven slot extraction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ExtractionResult:
    slots:        list[str]     # raw phrase slots (not yet KG IDs)
    confidence:   float
    template_hit: str           # which template matched
    modifiers:    list[str] = None   # transformation words stripped from slot fronts

    def __post_init__(self):
        if self.modifiers is None:
            self.modifiers = []


# Transformation modifiers — stripped from the front of slots so
# "inverse first law of thermodynamics" → slot="first law of thermodynamics", modifier="inverse"
_MODIFIERS = {
    "inverse", "invert", "reverse", "reversed", "negate", "negated",
    "violate", "violated", "remove", "removed", "eliminate", "eliminated",
    "double", "halve", "stop", "prevent", "maximize", "minimize",
    "increase", "decrease", "disable", "ignore",
}


# ── Intent-specific templates ──────────────────────────────────────────────────

# Each template is (name, regex, slot_groups)
# slot_groups: list of named groups to extract

_SYNTHESIS_TEMPLATES = [
    # "connection between X and Y" / "what connects X and Y"
    (r"(?:connection|link|relationship|bridge) between (?P<slot_a>.+?) and (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "connection_between"),
    # "what connects X and Y" / "how does X connect to Y"
    (r"(?:what (?:connects?|links?|bridges?|relates?)|how (?:does|do)) (?P<slot_a>.+?) (?:connect|relate|link) to (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "what_connects"),
    # "connects X and Y"
    (r"connects? (?P<slot_a>.+?) and (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "connects"),
    # "intersection of X, Y, and Z"
    (r"intersection of (?P<slots>.+?)(?:\?|$)",
     ["slots"], "intersection"),
    # "X and Y across domains" / "X, Y, Z"
    (r"(?:most interesting|unsolved|open question|synthesis|cross.domain).*?(?:between|of|across)?\s+(?P<slots>[a-z].*?)(?:\?|$)",
     ["slots"], "synthesis_multi"),
    # "what do X and Y have in common" / "shared between X and Y"
    (r"(?:what do|what's? common|shared between|common to) (?P<slot_a>.+?) and (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "common_between"),
]

_AUDIT_TEMPLATES = [
    # "what do you know about X"
    (r"what (?:do you know|is|are|can you tell me) about (?P<slot>.+?)(?:\?|$)",
     ["slot"], "know_about"),
    # "tell me about X" / "explain X" / "define X"
    (r"(?:tell me about|explain|define|describe|what is|what are) (?P<slot>.+?)(?:\?|$)",
     ["slot"], "tell_about"),
    # "X?" — bare concept query
    (r"^(?P<slot>[a-z][a-z0-9 _]{2,40})\??$",
     ["slot"], "bare"),
]

_COUNTERFACTUAL_TEMPLATES = [
    # "what would break if X were/was reversed/violated/absent"
    (r"(?:what (?:would|will)|how would things?) (?:break|change|fail|collapse) if (?P<slot>.+?) (?:were|was|is|had been) (?:reversed|violated|absent|removed|different|false|wrong)",
     ["slot"], "break_if"),
    # "if X were reversed, what would..."
    (r"if (?P<slot>.+?) (?:were|was) (?:reversed|violated|absent|removed|different|false|wrong)",
     ["slot"], "if_reversed"),
    # "what breaks if X"
    (r"what (?:breaks?|fails?|collapses?) if (?:the )? ?(?P<slot>.+?)(?:\?|$)",
     ["slot"], "breaks_if"),
]

_DECISION_TEMPLATES = [
    # "why did you decide X" / "why did you propose X"
    (r"why did you (?:decide|propose|choose|approve|reject|implement|change) (?P<slot>.+?)(?:\?|$)",
     ["slot"], "why_decide"),
    # "what was your reasoning for X"
    (r"(?:what was|explain) (?:your )?reasoning (?:for|behind|about) (?P<slot>.+?)(?:\?|$)",
     ["slot"], "reasoning_for"),
]

_REASON_TEMPLATES = [
    # "given X, what follows" / "from X, what can you derive"
    (r"(?:given|from|starting from|assuming) (?P<slot>.+?)[,;] (?:what (?:follows|can you|do we|does))",
     ["slot"], "given_follows"),
    # "what does X imply" / "what can you derive from X"
    (r"what (?:does|do) (?P<slot>.+?) (?:impl[yi]|suggest|tell us|entail)",
     ["slot"], "implies"),
]

_HYPOTHESIZE_TEMPLATES = [
    # "how do I get from X to Y"
    (r"how (?:do I|can (?:I|we|you)) (?:get|go|move) from (?P<slot_a>.+?) to (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "how_get_from"),
    # "path from X to Y"
    (r"(?:path|route|bridge|connection) from (?P<slot_a>.+?) to (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "path_from"),
]

_EXPLAIN_TEMPLATES = [
    # "how much do you understand X" / "how much do you know about X"
    (r"how (?:much )?(?:do you|does flux) (?:understand|know about?|know|remember|recall) (?P<slot>.+?)(?:\?|$)",
     ["slot"], "understand"),
    # "what is X" / "what are X" / "what's X"
    (r"what(?:'s| is| are) (?:(?:a|an|the) )?(?P<slot>[a-z].{2,60}?)(?:\?|$)",
     ["slot"], "what_is"),
    # "how does X work" / "how do X work" / "how X works"
    (r"how (?:does?|do) (?:(?:your|the|a|an|this) )?(?P<slot>.+?) works?(?:\?|$)",
     ["slot"], "how_works"),
    # "how do you use X" / "how does X help"
    (r"how (?:do you use|does) (?P<slot>.+?) (?:work|help|function|operate)(?:\?|$)",
     ["slot"], "how_use"),
    # "tell me about X" / "explain X" / "describe X" / "teach me X"
    (r"(?:tell me about|explain|describe|define|teach me (?:about)?)\s+(?P<slot>.+?)(?:\?|$)",
     ["slot"], "tell_about"),
    # "what does X mean" / "what is the meaning of X"
    (r"what (?:does|is the meaning of) (?P<slot>.+?) (?:mean|is|denote)(?:\?|$)",
     ["slot"], "meaning_of"),
    # "give me an example of X" / "show me X"
    (r"(?:give me (?:an )?example of|show me|demonstrate) (?P<slot>.+?)(?:\?|$)",
     ["slot"], "example_of"),
]

_SELF_TEMPLATES = [
    # "what is your X" / "how does your X work"
    (r"(?:what is|how does|how do|tell me about|explain) your (?P<slot>.+?)(?:\?|$)",
     ["slot"], "your_thing"),
    # "how do you X" / "can you X"
    (r"(?:how do you|can you|do you) (?P<slot>.+?)(?:\?|$)",
     ["slot"], "can_you"),
    # "what are your capabilities regarding X"
    (r"(?:what|which) (?:of your )?(?P<slot>.+?) (?:work|do you have|can you do)(?:\?|$)",
     ["slot"], "capability"),
]

_CONNECT_TEMPLATES = [
    # "connection between X and Y" / "relationship between X and Y"
    (r"(?:connection|relationship|link|bridge|difference|similarity) between (?P<slot_a>.+?) and (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "between"),
    # "how does X relate to Y" / "how are X and Y related"
    (r"how (?:does|do|are) (?P<slot_a>.+?) (?:relate|connect|compare|link) (?:to|with) (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "relate_to"),
    # "X vs Y" / "X versus Y"
    (r"(?P<slot_a>.+?)\s+(?:vs\.?|versus)\s+(?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "versus"),
    # "compare X and Y" / "difference between X and Y"
    (r"(?:compare|contrast|distinguish between) (?P<slot_a>.+?) and (?P<slot_b>.+?)(?:\?|$)",
     ["slot_a", "slot_b"], "compare"),
]

# Extended COUNTERFACTUAL — catches "if X decreased/increased/stopped/didn't exist"
_COUNTERFACTUAL_TEMPLATES = [
    # "what would break if X were/was reversed/violated/absent"
    (r"(?:what (?:would|will)|how would things?) (?:break|change|fail|collapse|happen|result) if (?P<slot>.+?) (?:were|was|is|had been) (?:reversed|violated|absent|removed|different|false|wrong)",
     ["slot"], "break_if"),
    # "if X were reversed, what would..."
    (r"if (?P<slot>.+?) (?:were|was) (?:reversed|violated|absent|removed|different|false|wrong)",
     ["slot"], "if_reversed"),
    # "what breaks if X"
    (r"what (?:breaks?|fails?|collapses?) if (?:the )? ?(?P<slot>.+?)(?:\?|$)",
     ["slot"], "breaks_if"),
    # NEW: "if X decreased/increased/stopped/became zero"
    (r"if (?P<slot>.+?) (?:decreas|increas|stopp|disappear|became|becomes|didn.t exist|didn.t happen|went to zero)",
     ["slot"], "if_changed"),
    # NEW: "what would happen if X"
    (r"what would happen if (?P<slot>.+?)(?:\?|$)",
     ["slot"], "what_happen_if"),
    # NEW: "what if X"
    (r"what if (?P<slot>.+?) (?:decreas|increas|stop|disappear|chang|didn.t|weren.t|wasn.t)(?:.{0,30})?(?:\?|$)",
     ["slot"], "what_if_changed"),
    # "what if i were to [modifier] X" / "if i were to [modifier] X"
    # modifier is stripped by _strip_modifier in _extract_groups
    (r"what if (?:i|we|you) (?:were|was) to (?P<slot>.+?)(?:\?|$)",
     ["slot"], "what_if_were_to"),
    (r"if (?:i|we|you) (?:were|was) to (?P<slot>.+?)(?:\?|$)",
     ["slot"], "if_were_to"),
    # "suppose X were reversed" / "imagine X was violated"
    (r"(?:suppose|imagine|assume|consider) (?P<slot>.+?) (?:were|was) (?:reversed|violated|absent|removed|false|wrong|inverted)",
     ["slot"], "suppose_reversed"),
]

_SMALLTALK_TEMPLATES = [
    # Greetings
    (r"^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening|night|day))[\s,!.]*(?:flux|there)?[\s,!.]*$",
     [], "greeting"),
    # How are you
    (r"how\s+(?:are\s+you|do\s+you\s+(?:feel|do)|is\s+it\s+going|you\s+doing)[\s?!.]*$",
     [], "how_are_you"),
    # Acknowledgments / filler
    (r"^(?:ok(?:ay)?|sure|alright|alr|cool|great|nice|thanks?|thank you|got it|"
     r"understood|makes sense|perfect|awesome|yep|yup|yeah|nope|no problem|np)[\s!.]*$",
     [], "ack"),
    # Goodbye
    (r"^(?:bye(?:bye)?|goodbye|see\s+you|later|take\s+care|cya)[\s!.]*$",
     [], "bye"),
    # "how are you" with name prefix stripped by _strip_agent_address
    (r"^(?:what(?:'s| is) up|wassup|sup)[\s?!.]*$", [], "whats_up"),
]

_ALL_TEMPLATES: dict[str, list] = {
    "SMALLTALK":      _SMALLTALK_TEMPLATES,       # ← checked FIRST in _phase1_handle
    "SYNTHESIS":      _SYNTHESIS_TEMPLATES,
    "AUDIT":          _AUDIT_TEMPLATES,
    "EXPLAIN":        _EXPLAIN_TEMPLATES,
    "SELF":           _SELF_TEMPLATES,
    "CONNECT":        _CONNECT_TEMPLATES,
    "COUNTERFACTUAL": _COUNTERFACTUAL_TEMPLATES,
    "DECISION":       _DECISION_TEMPLATES,
    "REASON":         _REASON_TEMPLATES,
    "HYPOTHESIZE":    _HYPOTHESIZE_TEMPLATES,
}

# Pre-compile
_COMPILED: dict[str, list] = {}
for _intent, _templates in _ALL_TEMPLATES.items():
    _COMPILED[_intent] = [
        (re.compile(pat, re.I | re.DOTALL), groups, name)
        for pat, groups, name in _templates
    ]


# ── Protected names — NOT hardcoded, but seeded as defaults ───────────────────
# The interface loads these from WorldModel at runtime and passes them in.
# This list is just the fallback if WorldModel isn't available yet.
_DEFAULT_PROTECTED = {"flux", "akhil", "jarvis", "tony"}


class SlotExtractor:
    """
    Extracts argument slots from Akhil's queries by intent.

    Does NOT touch the KG. Does NOT do string matching against concept IDs.
    Pure grammatical structure parsing.
    """

    def __init__(self, protected_names: set[str] | None = None):
        """
        Parameters
        ----------
        protected_names : names that are NEVER argument slots (agent names, user names)
                          Loaded from WorldModel at runtime — not hardcoded here.
        """
        self._protected = protected_names or _DEFAULT_PROTECTED

    def extract(self, text: str, intent: str) -> ExtractionResult:
        """
        Extract slots from text given the intent.

        Returns ExtractionResult with raw phrase slots.
        """
        # 1. Strip agent address "Flux, ..." or "Flux - ..."
        text = self._strip_agent_address(text)

        # 2. Try intent-specific templates
        templates = _COMPILED.get(intent, [])
        for pattern, groups, name in templates:
            m = pattern.search(text)
            if not m:
                continue
            slots, modifiers = self._extract_groups(m, groups, text)
            if slots:
                return ExtractionResult(
                    slots        = slots,
                    confidence   = 0.95,
                    template_hit = f"{intent}:{name}",
                    modifiers    = modifiers,
                )

        # 3. Fallback: generic noun phrase extraction
        slots = self._fallback_extract(text, intent)
        return ExtractionResult(
            slots        = slots,
            confidence   = 0.6,
            template_hit = f"{intent}:fallback",
        )

    def _strip_agent_address(self, text: str) -> str:
        """
        Remove agent address from start of query.
        "Flux, what is entropy?" → "what is entropy?"
        "Flux - tell me about X" → "tell me about X"

        The agent name comes from WorldModel protected_names, not hardcoded.
        """
        for name in self._protected:
            # "Name, ..." or "Name: ..." or "Name - ..."
            pattern = re.compile(
                rf"^{re.escape(name)}\s*[,:\-–]\s*",
                re.I
            )
            text = pattern.sub("", text.strip())
        return text.strip()

    def _extract_groups(self, match: re.Match, groups: list[str], full_text: str) -> list[str]:
        """Extract and clean named groups from a regex match.
        Returns (slots, modifiers) — modifiers are transformation words stripped
        from slot fronts (e.g. 'inverse', 'reverse', 'negate').
        """
        slots:     list[str] = []
        modifiers: list[str] = []

        for group in groups:
            try:
                val = match.group(group)
            except IndexError:
                continue
            if not val:
                continue
            val = val.strip().rstrip("?.,!;")
            if not val:
                continue
            # Handle "slots" group — may be "X, Y, and Z"
            if group == "slots":
                parts = re.split(r",\s*and\s+|,\s*|\s+and\s+", val)
                for part in parts:
                    part = part.strip().rstrip("?.,!")
                    if len(part) >= 2 and part.lower() not in self._protected:
                        cleaned, mod = self._strip_modifier(part)
                        if mod:
                            modifiers.append(mod)
                        slots.append(cleaned)
            else:
                if val.lower() not in self._protected and len(val) >= 2:
                    cleaned, mod = self._strip_modifier(val)
                    if mod:
                        modifiers.append(mod)
                    slots.append(cleaned)
        return slots, modifiers

    def _strip_modifier(self, slot: str) -> tuple[str, str | None]:
        """
        Strip a leading transformation modifier from a slot phrase.
        "inverse first law of thermodynamics" → ("first law of thermodynamics", "inverse")
        "entropy"                             → ("entropy", None)
        """
        words = slot.split()
        if words and words[0].lower() in _MODIFIERS:
            remaining = " ".join(words[1:]).strip()
            if remaining:           # don't strip if nothing left
                return remaining, words[0].lower()
        return slot, None


    def _fallback_extract(self, text: str, intent: str) -> list[str]:
        """
        Universal fallback: extract concept phrases from ANY query when
        intent-specific templates don't match.

        Works by removing question words, function words, and protected
        names, then returning the remaining content as candidate slots.
        This lets Pipeline A work on ANY natural language query.
        """
        # All English function words + question words + common verbs
        _FILLER = {
            "what","how","when","where","why","who","which","whose","whom",
            "do","does","did","can","could","would","will","shall","should",
            "may","might","must","need","dare","ought",
            "am","is","are","was","were","be","been","being",
            "have","has","had","having","do","does","did",
            "get","got","getting","let","make","go","going","come","coming",
            "much","many","often","ever","already","just","still","yet",
            "i","you","we","they","he","she","it","me","us","them","him","her",
            "my","your","our","their","its","his","her",
            "and","or","but","so","if","then","because","although","though",
            "however","therefore","hence","thus","moreover","furthermore",
            "a","an","the","this","that","these","those","there","here",
            "up","for","in","on","at","to","of","by","with","from","into",
            "about","against","between","during","after","before","through",
            "now","then","very","more","most","some","any","all","no","not",
            "want","wants","wanted","need","needs","needed",
            "think","thinks","thought","know","knows","known",
            "tell","tells","told","show","shows","showed","shown",
            "understand","understands","understood","use","uses","used",
            "work","works","worked","working","build","building","built",
            "like","likes","liked","see","sees","saw","seen","try","tries",
            "say","says","said","mean","means","meant",
            "run","runs","ran","start","stop","help","helps","helped",
            "really","actually","basically","simply","sure","please",
            "task","thing","things","something","anything","nothing",
            "also","too","as","well","only","even","just","right","okay",
            "yes","no","yeah","nope","ok","sure",
        }

        # Strip protected names
        cleaned = text
        for name in self._protected:
            cleaned = re.sub(rf"\b{re.escape(name)}\b", " ", cleaned, flags=re.I)

        # Remove punctuation and lowercase
        cleaned = re.sub(r"[?!.,;:\"'()\[\]{}]", " ", cleaned.lower()).strip()
        words = [w.strip() for w in cleaned.split() if w.strip()]

        # Filter to content words only
        content = [w for w in words if w not in _FILLER and len(w) >= 3]

        if not content:
            return []

        # Generate bigrams and unigrams in order
        # Prefer bigrams (multi-word concepts more specific)
        candidates: list[str] = []
        for i in range(len(content)):
            if i < len(content) - 1:
                candidates.append(f"{content[i]} {content[i+1]}")
            candidates.append(content[i])

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)

        return unique[:6]   # up to 6 candidate slots for the grounder

