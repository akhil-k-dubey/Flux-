"""
flux/cpm/utterance_parser.py

Stage 0 of the CPM: classify the utterance type from sentence structure,
NOT from keyword presence.

The distinction:
  - Keyword matching: "does the sentence contain 'flux'?" → wrong
  - Structural parsing: "is 'flux' the grammatical subject here, or a vocative
    address used while speaking to someone?" → right

Eight utterance types that cover all real input:
  SOCIAL         — greeting, farewell, how-are-you, emotional expression
  SELF_QUERY     — asking about Flux herself (state, capability, identity)
  KNOWLEDGE_QUERY — asking to understand a domain concept
  TASK           — asking Flux to DO something (build, write, run, check)
  COUNTERFACTUAL — if X then what / what would happen if
  TEACHING       — depositing knowledge ("[TEACH] X: def")
  EXPLORATION    — thinking out loud, wondering, musing
  CONTINUATION   — following up on prior exchange (ambiguous without history)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class UtteranceType(str, Enum):
    SOCIAL          = "SOCIAL"
    SELF_QUERY      = "SELF_QUERY"
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    TASK            = "TASK"
    COUNTERFACTUAL  = "COUNTERFACTUAL"
    TEACHING        = "TEACHING"
    EXPLORATION     = "EXPLORATION"
    CONTINUATION    = "CONTINUATION"
    UNKNOWN         = "UNKNOWN"


@dataclass
class ParsedUtterance:
    raw:            str
    utype:          UtteranceType
    confidence:     float           # 0-1 how certain we are of the type
    subject_is_you: bool            # is Flux ("you") the grammatical subject?
    slot_text:      str             # the content the utterance is *about*
    agent_as_name:  bool            # is "flux" used as a name/address, not a concept subject?
    emotional_tone: str             # neutral / curious / frustrated / excited
    is_continuation: bool           # references prior exchange

    @property
    def needs_kg(self) -> bool:
        """Only KNOWLEDGE_QUERY and COUNTERFACTUAL need the KG."""
        return self.utype in (
            UtteranceType.KNOWLEDGE_QUERY,
            UtteranceType.COUNTERFACTUAL,
            UtteranceType.EXPLORATION,
        )


# ── Compiled patterns (sentence-level structure) ──────────────────────────────

# Greeting signals at sentence start
_GREETING_START = re.compile(
    r"^(hi+|hello+|hey+|heyy+|howdy|yo\b|good\s+(morning|afternoon|evening|night|day|time))",
    re.I
)

# Social verbs: questions about Flux's state, not domain knowledge
_SOCIAL_VERB = re.compile(
    r"\b(how\s+(are|have|is)\s+(you|it)|"
    r"how('?s|\s+is)\s+it\s+going|"
    r"how\s+are\s+things|"
    r"you\s+(doing|been|feeling|holding\s+up)|"
    r"(talk|chat|catch\s+up)\s+(to|with)\s+you|"
    r"miss\s+you|glad\s+(to|you))",
    re.I
)

# Farewells
_FAREWELL = re.compile(
    r"^(bye|goodbye|see\s+you|later|good\s+night|take\s+care|"
    r"till\s+(next\s+)?time|cya|gotta\s+go|talk\s+(to\s+you\s+)?later)",
    re.I
)

# Acknowledgment-only utterances (short responses, no real content)
_ACK_ONLY = re.compile(
    r"^(ok|okay|got\s+it|understood|alright|alr|sure|yep|yup|yeah|"
    r"cool|nice|great|perfect|noted|thanks|thank\s+you|"
    r"makes\s+sense|i\s+see|i\s+get\s+it|fair\s+enough|"
    r"sounds\s+good|sounds\s+right)[\s.!]*$",
    re.I
)

# "You" as subject/object of main verb → SELF_QUERY
# Catches: "how much do YOU understand X", "do YOU know", "can YOU tell me",
#          "what are YOUR capabilities", "how many edges do YOU have",
#          "who built you", "who are you", "who created you"
_YOU_SUBJECT = re.compile(
    r"\b(do\s+you|can\s+you|could\s+you|are\s+you|were\s+you|"
    r"have\s+you|did\s+you|will\s+you|would\s+you|"
    r"how\s+(much|many|well|do)\s+(do\s+)?you|"
    r"what\s+(do|can|are|is)\s+you(r)?|"
    r"tell\s+me\s+about\s+your|"
    r"what\s+is\s+your|how\s+is\s+your|"
    r"who\s+(built|made|created|designed|trained|developed|coded|wrote|programmed)\s+you|"
    r"who\s+are\s+you|what\s+are\s+you|"
    r"your\s+(capabilities|state|knowledge|understanding|memory|KG|graph|architecture|name|creator|builder))",
    re.I
)

# Task delegation: imperative + action verb + object
# "build X", "write X", "run X", "create X", "check X", "fix X", "help me X"
# Also: "I want to build X", "I'd like to create X"
_TASK_VERB = re.compile(
    r"^(build|make|create|write|generate|run|execute|fix|debug|help\s+(me\s+)?"
    r"|implement|design|give\s+me|show\s+me|can\s+you\s+(build|make|create|write|"
    r"generate|run|fix|debug|implement|design|help)|"
    r"i\s+(want|would\s+like|'d\s+like)\s+to\s+(build|make|create|write|implement|design|develop)|"
    r"are\s+you\s+(up\s+for|able\s+to|capable\s+of))",
    re.I
)

# Counterfactual: if-then structure
_COUNTERFACTUAL = re.compile(
    r"\b(if\s+.{2,40}\s+(were\s+to|would|could|should|becomes?|disappears?|"
    r"increases?|decreases?|fails?|breaks?|changes?|removes?|stops?)|"
    r"what\s+(would|will|happens?)\s+(happen|change|break|result|occur)"
    r"\s+if|"
    r"what\s+if\b|"
    r"suppose\s+.{2,30}\s+(were|is|becomes))",
    re.I
)

# Definitional / knowledge queries: "what is X", "explain X", "tell me about X"
_KNOWLEDGE_QUERY = re.compile(
    r"^(what\s+(is|are|was|were|does)\s+|"
    r"explain\s+|define\s+|"
    r"tell\s+me\s+(about|how|why|what)\s+|"
    r"describe\s+|"
    r"how\s+does\s+.{2,40}\s+work|"
    r"why\s+(is|does|did|do)\s+|"
    r"what\s+do\s+you\s+know\s+about\s+)",        # last one: about domain, not about Flux
    re.I
)

# Teaching deposit
_TEACHING = re.compile(r"\[TEACH\]", re.I)

# Exploration / thinking out loud
_EXPLORATION = re.compile(
    r"\b(i\s+(wonder|think|feel|suspect|imagine|hypothesize|bet)\s+|"
    r"isn'?t\s+it\s+(interesting|strange|odd|cool|fascinating)\s+|"
    r"what\s+if\s+we\s+could|"
    r"do\s+you\s+think\s+|"
    r"could\s+it\s+be\s+that|"
    r"maybe\s+|perhaps\s+)",
    re.I
)

# Continuation markers
_CONTINUATION = re.compile(
    r"^(so|and|but|anyway|also|"
    r"tell\s+me\s+more|go\s+(on|ahead|deeper)|"
    r"continue|elaborate|expand|"
    r"wait|actually|i\s+meant|"
    r"what\s+about|how\s+about|"
    r"and\s+(what|how|why)\s+about)(\s+|$)",
    re.I
)

# Emotional frustration markers
_FRUSTRATED = re.compile(
    r"\b(broken|wrong|doesn'?t\s+work|not\s+working|still\s+failing|"
    r"what\s+the|why\s+(is|does|isn'?t|doesn'?t)|"
    r"this\s+is\s+(terrible|bad|wrong|broken|useless)|"
    r"not\s+understanding|you\s+(keep|always|never)|"
    r"seriously|again\?)",
    re.I
)

# Excited/positive tone
_EXCITED = re.compile(
    r"\b(amazing|great|awesome|perfect|excellent|sick|brilliant|"
    r"love\s+(it|this|that)|that'?s\s+(cool|nice|great|perfect|sick)|"
    r"this\s+is\s+(cool|nice|great|perfect)|nailed|!{2,})",
    re.I
)


class UtteranceParser:
    """
    Classifies what kind of sentence Akhil sent.

    Runs BEFORE any KG lookup. Output tells the system whether to touch
    the KG at all, and if so, with what slot.

    Key design: reads sentence STRUCTURE, not keyword presence.
    "flux" appearing in a sentence does NOT trigger a KG lookup unless
    the sentence structure shows "flux" as the grammatical subject of
    a knowledge question.
    """

    def __init__(self, agent_name: str = "flux", user_name: str = "akhil"):
        self._agent  = agent_name.lower()
        self._user   = user_name.lower()
        # Vocative pattern: agent name used as address (mid-sentence or after comma)
        self._vocative = re.compile(
            rf"(^|,\s*|hey\s+){re.escape(self._agent)}\b", re.I
        )

    def parse(self,
              text: str,
              conversation_history: list[dict] | None = None) -> ParsedUtterance:
        """
        Parse an utterance into its type and slot.

        Parameters
        ----------
        text                 : raw utterance from Akhil
        conversation_history : list of prior {query, response, intent} dicts
        """
        t = text.strip()
        tl = t.lower()
        history = conversation_history or []

        # ── Signals ────────────────────────────────────────────────────────────

        has_greeting_start  = bool(_GREETING_START.match(tl))
        has_social_verb     = bool(_SOCIAL_VERB.search(tl))
        has_farewell        = bool(_FAREWELL.match(tl))
        is_ack_only         = bool(_ACK_ONLY.match(tl))
        you_is_subject      = bool(_YOU_SUBJECT.search(tl))
        is_task             = bool(_TASK_VERB.match(tl))
        is_counterfactual   = bool(_COUNTERFACTUAL.search(tl))
        is_knowledge        = bool(_KNOWLEDGE_QUERY.match(tl))
        is_teaching         = bool(_TEACHING.search(t))
        is_exploration      = bool(_EXPLORATION.search(tl))
        is_continuation     = bool(_CONTINUATION.match(tl))

        # Vocative: agent name used as address, NOT as subject
        # e.g. "hii flux how have you been", "hey flux", "good morning flux"
        agent_as_name = bool(self._vocative.search(tl)) and (
            has_greeting_start or has_social_verb or has_farewell or is_ack_only
        )

        # Emotional tone
        tone = "neutral"
        if _FRUSTRATED.search(tl):
            tone = "frustrated"
        elif _EXCITED.search(tl):
            tone = "excited"
        elif has_greeting_start or has_social_verb:
            tone = "social"

        # ── Classification (priority order) ────────────────────────────────────

        utype, confidence = self._classify(
            tl, has_greeting_start, has_social_verb, has_farewell,
            is_ack_only, you_is_subject, is_task, is_counterfactual,
            is_knowledge, is_teaching, is_exploration, is_continuation,
            agent_as_name, history
        )

        # ── Slot extraction (what the utterance is *about*) ────────────────────
        slot = self._extract_slot(t, tl, utype, agent_as_name)

        return ParsedUtterance(
            raw            = t,
            utype          = utype,
            confidence     = confidence,
            subject_is_you = you_is_subject,
            slot_text      = slot,
            agent_as_name  = agent_as_name,
            emotional_tone = tone,
            is_continuation= is_continuation,
        )

    def _classify(self,
                  tl: str,
                  has_greeting_start: bool,
                  has_social_verb: bool,
                  has_farewell: bool,
                  is_ack_only: bool,
                  you_is_subject: bool,
                  is_task: bool,
                  is_counterfactual: bool,
                  is_knowledge: bool,
                  is_teaching: bool,
                  is_exploration: bool,
                  is_continuation: bool,
                  agent_as_name: bool,
                  history: list) -> tuple[UtteranceType, float]:

        # 1. Teaching — unambiguous marker
        if is_teaching:
            return UtteranceType.TEACHING, 0.99

        # 2. Farewell
        if has_farewell:
            return UtteranceType.SOCIAL, 0.98

        # 3. Social: greeting + (vocative name OR social verb OR short ack)
        if has_greeting_start or is_ack_only:
            return UtteranceType.SOCIAL, 0.95

        if has_social_verb:
            return UtteranceType.SOCIAL, 0.92

        # 4. SELF_QUERY — "you" is the grammatical subject/object
        # Must check before KNOWLEDGE_QUERY because "what do you know about X"
        # is a SELF_QUERY (asking about Flux's knowledge), not KNOWLEDGE_QUERY
        if you_is_subject:
            return UtteranceType.SELF_QUERY, 0.90

        # 5. Counterfactual — if-then structure
        if is_counterfactual:
            return UtteranceType.COUNTERFACTUAL, 0.88

        # 6. Task delegation
        if is_task:
            return UtteranceType.TASK, 0.87

        # 7. Pure exploration — thinking out loud
        if is_exploration and not is_knowledge:
            return UtteranceType.EXPLORATION, 0.75

        # 8. Knowledge query — definitional / explanatory
        if is_knowledge:
            return UtteranceType.KNOWLEDGE_QUERY, 0.85

        # 9. Continuation — following prior exchange
        if is_continuation and history:
            return UtteranceType.CONTINUATION, 0.80

        # 10. Short utterance with history → probably continuation
        if len(tl.split()) <= 4 and history:
            return UtteranceType.CONTINUATION, 0.60

        return UtteranceType.UNKNOWN, 0.40

    def _extract_slot(self,
                      raw: str,
                      tl: str,
                      utype: UtteranceType,
                      agent_as_name: bool) -> str:
        """
        Extract what the utterance is *about* — the content slot.

        For KNOWLEDGE_QUERY: strip the question frame, keep the concept phrase.
        For SELF_QUERY:      the topic Akhil is asking about w.r.t. Flux.
        For COUNTERFACTUAL:  the concept being perturbed.
        For TASK:            what Flux is being asked to do.
        For SOCIAL:          empty (no content slot needed).
        """
        if utype == UtteranceType.SOCIAL:
            return ""

        # Strip agent name if used as vocative (not subject)
        working = raw
        if agent_as_name:
            working = re.sub(
                rf"(,?\s*{re.escape(self._agent)}\s*,?)", " ", working, flags=re.I
            ).strip()

        wl = working.lower()

        if utype == UtteranceType.KNOWLEDGE_QUERY:
            # Remove the question frame, keep what's being asked about
            slot = re.sub(
                r"^(what\s+(is|are|was|were|does)\s+|"
                r"explain\s+|define\s+|"
                r"tell\s+me\s+(about|how|why|what)\s+|"
                r"describe\s+|"
                r"why\s+(is|does|did|do)\s+|"
                r"what\s+do\s+you\s+know\s+about\s+)",
                "", wl, flags=re.I
            ).strip()
            # Remove leading articles
            slot = re.sub(r"^(a|an|the)\s+", "", slot).strip()
            return slot or working

        if utype == UtteranceType.SELF_QUERY:
            # What topic is Akhil asking Flux about?
            # "how much do you understand python" → slot = "python"
            # "what are your capabilities" → slot = "capabilities"
            # "how many edges do you have" → slot = "edges"
            slot = re.sub(
                r"^(how\s+(much|many|well|do)\s+do\s+you\s+(understand|know(\s+about)?)\s*|"
                r"what\s+(are|is)\s+your\s*|"
                r"do\s+you\s+(know|understand)\s+(about\s+)?|"
                r"can\s+you\s+(tell\s+me\s+about\s+)?|"
                r"tell\s+me\s+about\s+your\s*)",
                "", wl, flags=re.I
            ).strip().rstrip("?.!")
            return slot or wl.rstrip("?.!")

        if utype == UtteranceType.COUNTERFACTUAL:
            # Extract the concept being conditionally changed
            m = re.search(
                r"if\s+(?:the\s+)?(?P<concept>[\w\s]+?)\s+"
                r"(?:were\s+to|would|becomes?|disappears?|increases?|decreases?|"
                r"fails?|breaks?|changes?|removes?|stops?)",
                wl, re.I
            )
            if m:
                return m.group("concept").strip()
            return wl

        if utype == UtteranceType.TASK:
            # Strip the task verb frame
            slot = re.sub(
                r"^(build|make|create|write|generate|run|fix|debug|implement|"
                r"design|help\s+(me\s+)?|can\s+you\s+(build|make|create|write|"
                r"generate|run|fix|debug|implement|design|help)\s*|"
                r"are\s+you\s+(up\s+for|able\s+to|capable\s+of)\s*)",
                "", wl, flags=re.I
            ).strip()
            return slot or wl

        return working.lower().strip()
