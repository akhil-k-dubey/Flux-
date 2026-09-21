"""
flux/cpm/__init__.py

Continuous Personal Model — the understanding layer that sits before any KG lookup.
"""
import re

from .utterance_parser  import UtteranceParser, UtteranceType, ParsedUtterance
from .temporal_state    import TemporalStateModel, TemporalState
from .vocabulary_model  import PersonalVocabularyModel
from .goal_inference    import GoalInferenceEngine, Goal
from .response_governor import ResponseGovernor, GovernorDecision
from .lexical_bridge    import LexicalBridge


class CPM:
    """
    One object to manage the full CPM pipeline.
    Initialized once at FluxInterface startup and reused for every query.
    """

    def __init__(self, world_model, store,
                 smalltalk_handler, self_handler,
                 domain_handler, concept_handler,
                 agent_name: str = "flux", user_name: str = "akhil") -> None:

        self.parser    = UtteranceParser(agent_name=agent_name, user_name=user_name)
        self.state_mdl = TemporalStateModel(world_model)
        self.vocab     = PersonalVocabularyModel(world_model)
        self.goal_eng  = GoalInferenceEngine()
        self.bridge    = LexicalBridge(store=store, world_model=world_model)
        self.governor  = ResponseGovernor(
            store=store,
            world_model=world_model,
            smalltalk_handler=smalltalk_handler,
            self_handler=self_handler,
            domain_handler=domain_handler,
            concept_handler=concept_handler,
            bridge=self.bridge,
        )
        self._wm = world_model

    def process(self,
                utterance: str,
                conversation_history: list[dict] | None = None,
                session_id: str = "") -> GovernorDecision:
        """
        Full CPM pipeline for one utterance.

        Returns a GovernorDecision:
          - handled=True  → use decision.response, skip ask() pipeline
          - handled=False → use decision.pipeline_slot + pipeline_intent as grounded
                            inputs to the manifold pipeline
        """
        history = conversation_history or []

        if history:
            last = history[-1]

            # ── Stage 2 → 3: "Should I save?" was asked last turn ────────────
            # Akhil is answering the confirmation question. Parse yes/no/modify.
            pending_confirm = last.get("pending_alias_confirm")
            if pending_confirm:
                term       = pending_confirm.get("term", "")
                cand_ids   = pending_confirm.get("candidates", [])
                cand_names = pending_confirm.get("candidate_names", [])
                intent     = self.bridge.parse_save_response(
                    utterance, term, cand_ids, cand_names
                )
                if intent.action == "save":
                    resp = self.bridge.confirm_alias(term, intent.resolved_ids, session_id)
                    return GovernorDecision(
                        handled  = True,
                        response = resp,
                        metadata = {"alias_confirmed": term, "confirmed_ids": intent.resolved_ids},
                    )
                if intent.action == "discard":
                    return GovernorDecision(
                        handled  = True,
                        response = f"Okay — I won't save that mapping for '{term}'.",
                    )
                # unclear → re-prompt
                return GovernorDecision(
                    handled       = True,
                    response      = self.bridge.interpretation_prompt(term, cand_ids),
                    pending_alias_confirm = pending_confirm,
                )

            # ── Stage 1 → 2: Clarifying question was asked last turn ─────────
            # Akhil is saying which concepts he meant. Build the interpretation
            # and ask for confirmation — do NOT write yet.
            pending_alias = last.get("pending_alias")
            if pending_alias:
                term       = pending_alias.get("term", "")
                candidates = pending_alias.get("candidates", [])
                names      = pending_alias.get("candidate_names", [])
                confirmed  = self.bridge.parse_confirmation(utterance, candidates, names)
                if confirmed:
                    # Show interpretation, ask to save — stage 2
                    prompt = self.bridge.interpretation_prompt(term, confirmed)
                    return GovernorDecision(
                        handled              = True,
                        response             = prompt,
                        pending_alias_confirm = {
                            "term":            term,
                            "candidates":      confirmed,
                            "candidate_names": [self.bridge._display_name(c) for c in confirmed],
                        },
                    )
                if re.search(r"\b(none|no|not really|something else|neither|wrong)\b",
                             utterance.lower()):
                    return GovernorDecision(
                        handled  = True,
                        response = (f"Got it — can you describe what you mean by '{term}'? "
                                    f"I'll use your description to look for the right concepts."),
                    )
                # Ambiguous — fall through to normal parsing

        # ── Stage 0: Parse utterance type from sentence structure ──────────────
        parsed = self.parser.parse(utterance, conversation_history=history)

        # ── Stage 1: Build temporal state snapshot ─────────────────────────────
        state = self.state_mdl.snapshot(current_utterance=utterance)

        # ── Stage 2: Infer goal ────────────────────────────────────────────────
        goal = self.goal_eng.infer(parsed, state, history)

        # ── Stage 3: Decide response or defer ─────────────────────────────────
        decision = self.governor.decide(goal, state, utterance)

        # ── Stage 4: Update models ─────────────────────────────────────────────
        self.state_mdl.update_from_utterance(
            utterance = utterance,
            utype     = parsed.utype.value,
            tone      = parsed.emotional_tone,
            domain    = self._wm.get("agents.akhil.current_focus_domain"),
        )
        self.vocab.learn_from_utterance(
            utterance      = utterance,
            utterance_type = parsed.utype.value,
            domain         = self._wm.get("agents.akhil.current_focus_domain") or "unknown",
        )

        return decision


__all__ = [
    "CPM",
    "UtteranceParser", "UtteranceType", "ParsedUtterance",
    "TemporalStateModel", "TemporalState",
    "PersonalVocabularyModel",
    "GoalInferenceEngine", "Goal",
    "ResponseGovernor", "GovernorDecision",
    "LexicalBridge",
]
