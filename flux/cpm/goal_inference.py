"""
flux/cpm/goal_inference.py

Maps a parsed utterance + temporal state → what Akhil actually wants.

Goals are distinct from utterance types:
  - Type: what kind of sentence is this?
  - Goal: what should Flux DO in response?

The same utterance type can have different goals depending on context:
  SOCIAL early in session       → acknowledge + offer to continue work
  SOCIAL late in session        → warm farewell, summarize if useful
  SELF_QUERY about capabilities → list capabilities honestly
  SELF_QUERY about KG state     → report actual numbers
  KNOWLEDGE_QUERY in expert domain  → technical, skip motivation
  KNOWLEDGE_QUERY in new domain     → intuition first, formalism second
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from .utterance_parser import UtteranceType, ParsedUtterance
from .temporal_state import TemporalState


@dataclass
class Goal:
    """What Flux should do in response to Akhil's utterance."""
    action:             str             # primary action class
    slot:               str             # what the action is about
    needs_kg:           bool            = False
    kg_role:            str             = "lookup"  # lookup | context | none
    calibrate_to:       list[str]       = field(default_factory=list)  # expert domains
    urgency:            str             = "normal"  # normal | high | low
    ask_clarification:  bool            = False
    clarification_hint: str             = ""
    metadata:           dict            = field(default_factory=dict)


class GoalInferenceEngine:
    """
    Infers what Akhil actually wants from a parsed utterance + temporal state.

    The rules here encode knowledge about how Akhil works — they are not
    generic intent rules. They get more accurate as more interaction data
    accumulates in the TemporalState.
    """

    def infer(self,
              parsed: ParsedUtterance,
              state: TemporalState,
              conversation_history: list[dict] | None = None) -> Goal:
        """
        Map parsed utterance + current state → Goal.
        """
        history = conversation_history or []
        utype   = parsed.utype
        slot    = parsed.slot_text
        tone    = parsed.emotional_tone

        if utype == UtteranceType.SOCIAL:
            return self._social_goal(parsed, state, history)

        if utype == UtteranceType.SELF_QUERY:
            return self._self_goal(parsed, state, slot)

        if utype == UtteranceType.TASK:
            return self._task_goal(parsed, state, slot)

        if utype == UtteranceType.COUNTERFACTUAL:
            return Goal(
                action    = "perturb_causal_chain",
                slot      = slot,
                needs_kg  = True,
                kg_role   = "causal_traversal",
            )

        if utype == UtteranceType.KNOWLEDGE_QUERY:
            return self._knowledge_goal(parsed, state, slot)

        if utype == UtteranceType.TEACHING:
            return Goal(
                action   = "learn_concept",
                slot     = slot,
                needs_kg = True,
                kg_role  = "write",
            )

        if utype == UtteranceType.EXPLORATION:
            return Goal(
                action   = "extend_thought",
                slot     = slot,
                needs_kg = True,
                kg_role  = "context",
            )

        if utype == UtteranceType.CONTINUATION:
            return self._continuation_goal(parsed, state, history)

        # UNKNOWN with non-empty slot — defer to pipeline rather than clarify.
        # Catches GEODESIC ("path from X to Y"), GAP, and other structured
        # queries that have content but don't match our structural patterns.
        if parsed.slot_text:
            return Goal(
                action          = "pipeline_fallback",
                slot            = parsed.slot_text,
                needs_kg        = True,
                kg_role         = "lookup",
            )

        # UNKNOWN with no slot — ask for clarification
        return Goal(
            action             = "clarify",
            slot               = slot,
            needs_kg           = False,
            ask_clarification  = True,
            clarification_hint = self._clarification_hint(parsed, state),
        )

    # ── Goal builders ─────────────────────────────────────────────────────────

    def _social_goal(self,
                     parsed: ParsedUtterance,
                     state: TemporalState,
                     history: list) -> Goal:
        """
        Social utterances have three sub-goals depending on context:
          - Session opener: acknowledge + what was open last time
          - Mid-session check-in: brief acknowledgment + continue
          - Farewell: warm close + optionally summarize session
        """
        tl = parsed.raw.lower()

        # Farewell markers
        if any(w in tl for w in ["bye", "goodbye", "see you", "later", "good night"]):
            return Goal(
                action   = "farewell",
                slot     = "",
                needs_kg = False,
                metadata = {"open_problems": state.open_problems[:2]},
            )

        # Opener: first/second query of session, or after a long break
        if state.session_started_fresh or state.is_returning:
            return Goal(
                action   = "session_opener",
                slot     = "",
                needs_kg = False,
                metadata = {
                    "time_away_s":    state.time_since_last,
                    "open_problems":  state.open_problems[:2],
                    "recent_changes": state.recent_decisions[:1],
                    "active_projects": state.active_projects[:1],
                },
            )

        # Mid-session: brief acknowledgment
        return Goal(
            action   = "brief_acknowledgment",
            slot     = "",
            needs_kg = False,
        )

    def _self_goal(self,
                   parsed: ParsedUtterance,
                   state: TemporalState,
                   slot: str) -> Goal:
        """
        SELF_QUERY: Akhil asking about Flux's state, knowledge, or capabilities.

        Distinguish:
          - Identity queries: "who are you", "who built you", "what are you"
          - State queries: "how many edges do you have", "what's your KG size"
          - Knowledge queries: "how much do you understand python"
          - Capability queries: "what can you do", "can you build X"
        """
        sl = slot.lower()
        raw_lower = parsed.raw.lower()

        # Identity queries — slot will be "who built you" or empty
        if any(p in raw_lower for p in ["who built", "who made", "who created",
                                         "who designed", "who are you", "what are you",
                                         "who developed", "who coded", "who wrote"]):
            return Goal(action="self_identity", slot=slot, needs_kg=False)

        # State / stats queries — whole-word match only
        # (substring check would hit "graph" inside "holographic", etc.)
        _STATS_WORDS = {"edges", "nodes", "concepts", "kg", "graph",
                        "memory", "size", "connections", "queries", "uptime"}
        sl_words = set(sl.split())
        if sl_words & _STATS_WORDS:
            return Goal(
                action   = "report_self_state",
                slot     = slot,
                needs_kg = False,
                metadata = {"state": "stats"},
            )


        # Domain knowledge queries — "how much do you understand X"
        if sl and len(sl) > 2:
            return Goal(
                action   = "report_domain_knowledge",
                slot     = slot,
                needs_kg = True,
                kg_role  = "lookup",
                metadata = {"domain_query": slot},
            )

        # Fallback to identity
        return Goal(action="self_identity", slot=slot, needs_kg=False)


    def _task_goal(self,
                   parsed: ParsedUtterance,
                   state: TemporalState,
                   slot: str) -> Goal:
        """
        TASK: Akhil asking Flux to DO something.
        Determine if Flux is capable and what the task is.
        """
        sl = slot.lower()

        # Code/build tasks
        if any(w in sl for w in ["app", "code", "program", "script", "function",
                                   "class", "api", "system", "tool", "algorithm"]):
            return Goal(
                action   = "task_code",
                slot     = slot,
                needs_kg = True,
                kg_role  = "context",
                metadata = {"task_type": "code"},
            )

        # Analysis tasks
        if any(w in sl for w in ["analyze", "analysis", "check", "verify",
                                   "review", "debug", "diagnose"]):
            return Goal(
                action   = "task_analysis",
                slot     = slot,
                needs_kg = True,
                kg_role  = "context",
                metadata = {"task_type": "analysis"},
            )

        return Goal(
            action   = "task_generic",
            slot     = slot,
            needs_kg = True,
            kg_role  = "context",
        )

    def _knowledge_goal(self,
                        parsed: ParsedUtterance,
                        state: TemporalState,
                        slot: str) -> Goal:
        """
        KNOWLEDGE_QUERY: Akhil wants to understand something.
        Calibrate response depth to Akhil's expertise in that domain.
        """
        expert_domains = state.is_expert_in

        return Goal(
            action        = "explain_concept",
            slot          = slot,
            needs_kg      = True,
            kg_role       = "lookup",
            calibrate_to  = expert_domains,
            metadata      = {
                "prefer_technical": len(expert_domains) > 0,
            },
        )

    def _continuation_goal(self,
                            parsed: ParsedUtterance,
                            state: TemporalState,
                            history: list) -> Goal:
        """
        CONTINUATION: Akhil is following up on the prior exchange.
        Infer what they're continuing from history.
        """
        if not history:
            return Goal(
                action            = "clarify",
                slot              = parsed.slot_text,
                ask_clarification = True,
                clarification_hint = "What are you following up on?",
            )

        last = history[-1]
        last_intent  = last.get("intent", "")
        last_concept = last.get("response", {})
        if isinstance(last_concept, dict):
            last_concept = last.get("primary_concept", "")

        return Goal(
            action   = "continue_" + (last_intent.lower() or "explain"),
            slot     = last.get("query", parsed.slot_text),
            needs_kg = True,
            kg_role  = "context",
            metadata = {"continuing_from": last_intent},
        )

    def _clarification_hint(self, parsed: ParsedUtterance, state: TemporalState) -> str:
        """Generate a useful clarification prompt."""
        if parsed.emotional_tone == "frustrated":
            return ("I want to help — can you tell me what specifically isn't working? "
                    "I'll focus on that.")
        if state.open_problems:
            return f"Are you asking about {state.open_problems[0]}?"
        return "Can you give me a bit more context about what you're after?"
