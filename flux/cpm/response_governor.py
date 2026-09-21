"""
flux/cpm/response_governor.py

The CPM's response layer — converts a Goal into a response string,
using the KG and WorldModel as sources.

This replaces the patchwork of fast-paths in ask():
  - SMALLTALK fast-path → Goal(action="session_opener") / Goal(action="farewell")
  - DOMAIN fast-path   → Goal(action="explain_concept") with KG lookup
  - EXPLAIN fast-path  → Goal(action="explain_concept")
  - SELF fast-path     → Goal(action="report_self_state") / Goal(action="self_identity")

For goals that still need manifold routing (PERTURB, GEODESIC, GAP),
the governor signals that the existing pipeline should handle them.
The key change: it decides FIRST, routes SECOND.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from .goal_inference import Goal
from .temporal_state import TemporalState

if TYPE_CHECKING:
    from flux.kg.store import KGStore

log = logging.getLogger(__name__)


@dataclass
class GovernorDecision:
    """
    Output of the response governor.

    handled=True  → governor produced the response, skip rest of pipeline.
    handled=False → governor defers to the existing pipeline (PERTURB/GEODESIC/GAP).
    """
    handled:  bool
    response: str  = ""
    # Signals to the main pipeline for un-handled cases
    pipeline_slot:   str            = ""
    pipeline_intent: str            = ""
    # Alias state — stored in conversation history so next turn can detect
    pending_alias:         Optional[dict] = None   # Stage 1: clarifying question asked
    pending_alias_confirm: Optional[dict] = None   # Stage 2: interpretation shown, awaiting save
    metadata:              Optional[dict] = None


class ResponseGovernor:
    """
    Converts a Goal into an action: either a direct response string,
    or a signal to pass control to the manifold pipeline.

    The governor knows about the KG and WorldModel but does NOT parse
    sentences — that's the UtteranceParser's job.
    """

    def __init__(self, store: "KGStore", world_model, smalltalk_handler: Callable,
                 self_handler: Callable, domain_handler: Callable,
                 concept_handler: Callable, bridge=None) -> None:
        self._store     = store
        self._wm        = world_model
        self._smalltalk = smalltalk_handler
        self._self      = self_handler
        self._domain    = domain_handler
        self._concept   = concept_handler
        self._bridge    = bridge   # LexicalBridge — set by CPM.__init__

    def decide(self, goal: Goal, state: TemporalState,
               raw_utterance: str) -> GovernorDecision:
        """
        Map a Goal to a response or a pipeline deferral.
        """
        action = goal.action

        # ── Social responses ───────────────────────────────────────────────────

        if action == "session_opener":
            return GovernorDecision(
                handled=True,
                response=self._build_session_opener(state, goal.metadata)
            )

        if action == "farewell":
            return GovernorDecision(
                handled=True,
                response=self._build_farewell(state, goal.metadata)
            )

        if action == "brief_acknowledgment":
            return GovernorDecision(
                handled=True,
                response=self._build_ack(state)
            )

        # ── Self / identity responses ──────────────────────────────────────────

        if action == "self_identity":
            resp = self._self(raw_utterance)
            return GovernorDecision(
                handled=True,
                response=resp.text if hasattr(resp, "text") else str(resp)
            )

        if action == "report_self_state":
            return GovernorDecision(
                handled=True,
                response=self._build_self_state_report(state, goal.slot)
            )

        if action == "report_domain_knowledge":
            report = self._build_domain_knowledge_report(goal.slot, state)
            if report is None:
                # Gap — delegate to bridge
                return self._bridge_gap(goal.slot, state)
            return GovernorDecision(handled=True, response=report)


        # ── Knowledge responses ────────────────────────────────────────────────

        if action == "explain_concept":
            return self._route_knowledge(goal, state)

        # ── Causal / counterfactual ────────────────────────────────────────────

        if action == "perturb_causal_chain":
            # Defer to manifold pipeline with clean slot
            return GovernorDecision(
                handled=False,
                pipeline_slot   = goal.slot,
                pipeline_intent = "PERTURB",
            )

        # ── Continuation ───────────────────────────────────────────────────────

        if action.startswith("continue_"):
            # Defer to pipeline with original slot from history
            return GovernorDecision(
                handled=False,
                pipeline_slot   = goal.slot,
                pipeline_intent = goal.metadata.get("continuing_from", "EXPLAIN"),
            )

        # ── Pipeline fallback (GEODESIC / GAP / unclassified structured) ────────
        if action == "pipeline_fallback":
            return GovernorDecision(
                handled=False,
                pipeline_slot   = goal.slot,
                pipeline_intent = "",   # let Phase 3 decide
            )

        # ── Task ───────────────────────────────────────────────────────────────

        if action.startswith("task_"):
            return GovernorDecision(
                handled=True,
                response=self._build_task_response(goal, state)
            )

        # ── Teaching ───────────────────────────────────────────────────────────

        if action == "learn_concept":
            # Teaching is handled downstream by the [TEACH] parser
            return GovernorDecision(
                handled=False,
                pipeline_slot   = goal.slot,
                pipeline_intent = "TEACH",
            )

        # ── Clarification ──────────────────────────────────────────────────────

        if action == "clarify":
            return GovernorDecision(
                handled=True,
                response=goal.clarification_hint or
                         "I'm not sure what you're asking — could you say more?"
            )

        # ── Exploration / extend thought ───────────────────────────────────────

        if action == "extend_thought":
            return GovernorDecision(
                handled=False,
                pipeline_slot   = goal.slot,
                pipeline_intent = "REASON",
            )

        # Anything else: defer to existing pipeline
        return GovernorDecision(
            handled=False,
            pipeline_slot   = goal.slot,
            pipeline_intent = "",
        )

    # ── Response builders ─────────────────────────────────────────────────────

    def _build_session_opener(self, state: TemporalState, meta: dict) -> str:
        """Warm opener that acknowledges time away and context."""
        parts = []

        time_away = float(meta.get("time_away_s", 0))
        if time_away > 86400:  # more than a day
            days = int(time_away / 86400)
            parts.append(f"Good to have you back, Akhil — it's been {days} day{'s' if days > 1 else ''}.")
        elif time_away > 3600:  # more than an hour
            hrs = int(time_away / 3600)
            parts.append(f"Hey Akhil — {hrs} hour{'s' if hrs > 1 else ''} since we last talked.")
        else:
            parts.append("Hey Akhil.")

        # Mention recent changes
        recent = meta.get("recent_changes", [])
        if recent:
            desc = recent[0].get("description", "") if isinstance(recent[0], dict) else str(recent[0])
            if desc:
                parts.append(f"Since we last spoke: {desc.lower().rstrip('.')}.")

        # Mention open problems
        open_p = meta.get("open_problems", [])
        if open_p:
            parts.append(f"Still open from before: {open_p[0]}.")

        # Offer
        kg_nodes = self._wm.get("knowledge.kg_nodes", 0)
        parts.append(f"KG is at {kg_nodes} concepts. What are we working on?")

        return " ".join(parts)

    def _build_farewell(self, state: TemporalState, meta: dict) -> str:
        parts = ["See you, Akhil."]
        open_p = meta.get("open_problems", [])
        if open_p:
            parts.append(f"Leaving open: {open_p[0]}.")
        parts.append("I'll be here.")
        return " ".join(parts)

    def _build_ack(self, state: TemporalState) -> str:
        """Brief mid-session acknowledgment."""
        kg_nodes = self._wm.get("knowledge.kg_nodes", 0)
        kg_edges = self._wm.get("knowledge.kg_edges", 0)
        return (f"Running well, Akhil. KG has {kg_nodes} concepts and {kg_edges} edges. "
                f"Manifold geometry is stable. What do you want to think about?")

    def _build_self_state_report(self, state: TemporalState, slot: str) -> str:
        """Report Flux's actual state for stat queries."""
        sl = slot.lower()
        wm = self._wm

        if any(w in sl for w in ["edge", "connect"]):
            n = wm.get("knowledge.kg_edges", 0)
            return f"I have {n} edges in my knowledge graph right now."

        if any(w in sl for w in ["node", "concept", "knowledge"]):
            n = wm.get("knowledge.kg_nodes", 0)
            return f"I have {n} concept nodes in my knowledge graph."

        if any(w in sl for w in ["query", "question", "asked"]):
            n = wm.get("agents.akhil.total_queries", 0)
            return f"You've asked me {n} questions total."

        if any(w in sl for w in ["uptime", "running", "alive"]):
            start = wm.get("self.start_time")
            if start:
                return f"I've been running since {start}."
            return "I'm running — no exact start time recorded for this session."

        # General state
        n_nodes = wm.get("knowledge.kg_nodes", 0)
        n_edges = wm.get("knowledge.kg_edges", 0)
        n_q     = wm.get("agents.akhil.total_queries", 0)
        return (f"KG: {n_nodes} concepts, {n_edges} edges. "
                f"Total queries answered: {n_q}.")

    def _build_domain_knowledge_report(self, slot: str, state: TemporalState) -> str:
        """
        Report what Flux knows about a domain/topic.
        e.g. "how much do you understand python" → list python domain concepts.
        """
        from flux.manifold.operation_classifier import find_concepts_in_query

        # Try to find the domain in the KG
        matching = find_concepts_in_query(slot, self._store, max_results=3)

        if not matching:
            # Direct ID lookup
            direct = slot.lower().replace(" ", "_")
            if self._store.get(direct):
                matching = [direct]

        if not matching:
            # Check if slot is a domain name
            for nid in self._store.all_ids():
                node = self._store.get(nid) or {}
                if (node.get("type") == "DOMAIN" and
                        slot.lower() in nid.lower()):
                    matching = [nid]
                    break

        if not matching:
            return None  # signal to decide() to run _bridge_gap()


        node = self._store.get(matching[0]) or {}
        ntype = node.get("type", "CONCEPT")

        # Alias node — expand to its targets
        if ntype == "ALIAS_OF":
            targets = node.get("alias_targets", [])
            if not targets:
                return None
            lines = [f"I know '{slot}' as an alias for:"]
            for tid in targets:
                tnode = self._store.get(tid) or {}
                tname = tid.replace("_", " ").title()
                defn  = (tnode.get("definition") or "")[:80]
                lines.append(f"  • {tname}" + (f" — {defn}..." if defn else ""))
            return "\n".join(lines)

        if ntype == "DOMAIN":
            return self._domain(matching[0])

        # Find all concepts in that domain
        domain_name = node.get("domain", slot)
        domain_concepts = [
            nid for nid in self._store.all_ids()
            if (self._store.get(nid) or {}).get("domain") == domain_name
        ]
        n = len(domain_concepts)
        examples = [c.replace("_", " ").title() for c in domain_concepts[:5]]

        expert_score = state.expertise_map.get(domain_name, 0.0)
        lines = [f"What I know about {slot}:"]
        lines.append(f"  {n} concepts in the {domain_name} domain.")
        if examples:
            lines.append(f"  Includes: {', '.join(examples)}{'...' if n > 5 else ''}.")
        if expert_score > 0.3:
            lines.append(f"  You've explored this domain extensively across our sessions.")
        return "\n".join(lines)

    def _route_knowledge(self, goal: Goal, state: TemporalState) -> GovernorDecision:
        """
        For KNOWLEDGE_QUERY goals: try to resolve via KG directly.
        If found → return response. If not → defer to manifold pipeline.
        """
        from flux.manifold.operation_classifier import find_concepts_in_query
        slot = goal.slot

        # 1. Find concept in KG
        matching = find_concepts_in_query(slot, self._store, max_results=2)
        if not matching:
            direct = slot.lower().replace(" ", "_").replace("-", "_")
            if self._store.get(direct):
                matching = [direct]

        if not matching:
            # Try bridge before falling back to manifold pipeline
            if self._bridge is not None:
                bridge_result = self._bridge.resolve(slot)
                if bridge_result.candidates:
                    return GovernorDecision(
                        handled      = True,
                        response     = bridge_result.question,
                        pending_alias = {
                            "term":            slot,
                            "candidates":      bridge_result.candidates,
                            "candidate_names": bridge_result.candidate_names,
                        },
                    )
            # No bridge or no candidates → defer to pipeline
            return GovernorDecision(
                handled=False,
                pipeline_slot   = slot,
                pipeline_intent = "EXPLAIN",
            )

        if len(matching) == 1:
            node = self._store.get(matching[0]) or {}
            ntype = node.get("type", "CONCEPT")

            if ntype == "DOMAIN":
                return GovernorDecision(
                    handled=True,
                    response=self._domain(matching[0])
                )
            if ntype == "CONCEPT" and node.get("definition"):
                return GovernorDecision(
                    handled=True,
                    response=self._concept(matching[0])
                )

        # Multiple or ambiguous → defer
        return GovernorDecision(
            handled=False,
            pipeline_slot   = slot,
            pipeline_intent = "EXPLAIN",
        )

    def _bridge_gap(self, slot: str, state: "TemporalState") -> GovernorDecision:
        """
        When a term has no KG match, run the LexicalBridge.
        Returns a GovernorDecision with:
          - handled=True + question + pending_alias  → ask clarifying question
          - handled=True + [TEACH] prompt            → complete gap, no candidates
        """
        if self._bridge is not None:
            result = self._bridge.resolve(slot)
            if result.candidates:
                return GovernorDecision(
                    handled      = True,
                    response     = result.question,
                    pending_alias = {
                        "term":            slot,
                        "candidates":      result.candidates,
                        "candidate_names": result.candidate_names,
                    },
                )
        # No candidates at all
        expertise = state.expertise_map.get(slot, 0.0)
        if expertise > 0:
            return GovernorDecision(
                handled  = True,
                response = (f"I have some familiarity with '{slot}' from our conversations, "
                            f"but I don't have it formally defined yet. "
                            f"You can teach me: [TEACH] {slot}: <definition>"),
            )
        return GovernorDecision(
            handled  = True,
            response = (f"I don't have '{slot}' in my knowledge graph. "
                        f"You can teach me: [TEACH] {slot}: <definition>"),
        )

    def _build_task_response(self, goal: Goal, state: TemporalState) -> str:
        """
        Honest response to task requests.
        Flux is a reasoning/knowledge system — not a code executor (yet).
        """
        slot = goal.slot
        metadata = goal.metadata or {}
        task_type = metadata.get("task_type", "generic")

        if task_type == "code":
            return (f"I can reason about the architecture and design of {slot}, "
                    f"and I know relevant concepts from my KG. But I don't have "
                    f"a code execution environment yet — that's a planned capability. "
                    f"Want me to think through the design?")

        if task_type == "analysis":
            return (f"I can analyze {slot} from a causal reasoning perspective — "
                    f"tracing what depends on what and where gaps are. "
                    f"What specifically do you want me to look at?")

        return (f"I understand you want me to work on: {slot}. "
                f"Tell me more about what you need and I'll see what I can do.")
