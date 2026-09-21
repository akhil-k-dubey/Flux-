"""
flux/cpm/temporal_state.py

Four-timescale view of Akhil's state — reads from the WorldModel.
No logic here, just a structured window into what the WorldModel already knows.

Timescales:
  immediate   — this utterance / last 5 minutes
  session     — what Akhil is working on today
  project     — what Akhil is building (weeks)
  longitudinal— how Akhil thinks and communicates (months)
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TemporalState:
    """A frozen snapshot of Akhil's state across all timescales."""
    # Immediate (last utterance / last 5 min)
    last_utterance:       str             = ""
    current_domain:       Optional[str]   = None
    emotional_signal:     str             = "neutral"
    cognitive_load:       str             = "medium"
    frustration_level:    float           = 0.0

    # Session (today)
    session_queries:      int             = 0
    session_domains:      list[str]       = field(default_factory=list)
    open_problems:        list[str]       = field(default_factory=list)

    # Project (weeks)
    active_projects:      list[str]       = field(default_factory=list)
    recent_decisions:     list[dict]      = field(default_factory=list)

    # Longitudinal (months — expertise + communication patterns)
    expertise_map:        dict[str, float] = field(default_factory=dict)
    vocabulary_patterns:  dict[str, Any]   = field(default_factory=dict)
    working_style:        str              = "exploratory"
    total_queries:        int              = 0
    time_since_last:      float            = 0.0   # seconds since last interaction

    @property
    def is_returning(self) -> bool:
        """True if Akhil is coming back after a gap (> 1 hour)."""
        return self.time_since_last > 3600

    @property
    def is_expert_in(self) -> list[str]:
        """Domains where Akhil's expertise score > 0.6."""
        return [d for d, s in self.expertise_map.items() if s > 0.6]

    @property
    def session_started_fresh(self) -> bool:
        """True if this is the first or second query of this session."""
        return self.session_queries <= 2


class TemporalStateModel:
    """
    Reads and updates Akhil's 4-timescale state in the WorldModel.

    The WorldModel is the source of truth. This class is a structured
    view + update layer over it.
    """

    def __init__(self, world_model) -> None:
        self._wm = world_model

    def snapshot(self, current_utterance: str = "") -> TemporalState:
        """Build a frozen state snapshot for the current moment."""
        wm = self._wm
        now = time.time()

        last_ts = wm.get("agents.akhil.last_interaction") or 0
        time_since = now - float(last_ts) if last_ts else 0.0

        return TemporalState(
            # Immediate
            last_utterance    = current_utterance,
            current_domain    = wm.get("agents.akhil.current_focus_domain"),
            emotional_signal  = wm.get("agents.akhil.emotional_signal", "neutral"),
            cognitive_load    = wm.get("agents.akhil.cognitive_load", "medium"),
            frustration_level = float(wm.get("agents.akhil.frustration_level", 0.0) or 0.0),
            # Session
            session_queries   = int(wm.get("agents.akhil.session_count_today", 0) or 0),
            session_domains   = list(wm.get("agents.akhil.session_domains", []) or []),
            open_problems     = list(wm.get("agents.akhil.open_problems", []) or []),
            # Project
            active_projects   = list(wm.get("agents.akhil.active_projects", []) or []),
            recent_decisions  = list(wm.get("agents.akhil.recent_decisions", []) or [])[:5],
            # Longitudinal
            expertise_map     = dict(wm.get("agents.akhil.expertise_map", {}) or {}),
            vocabulary_patterns = dict(wm.get("agents.akhil.vocabulary_patterns", {}) or {}),
            working_style     = wm.get("agents.akhil.working_style", "exploratory"),
            total_queries     = int(wm.get("agents.akhil.total_queries", 0) or 0),
            time_since_last   = time_since,
        )

    def update_from_utterance(self,
                               utterance: str,
                               utype: str,
                               tone: str,
                               domain: str | None) -> None:
        """Update the WorldModel after parsing an utterance."""
        wm = self._wm

        # Emotional signal
        wm.update("agents.akhil.emotional_signal", tone)

        # Frustration tracking — increases on frustrated tone, decays otherwise
        current_frustration = float(wm.get("agents.akhil.frustration_level", 0.0) or 0.0)
        if tone == "frustrated":
            new_frustration = min(1.0, current_frustration + 0.2)
        else:
            new_frustration = max(0.0, current_frustration - 0.05)
        wm.update("agents.akhil.frustration_level", new_frustration)

        # Session domain tracking
        if domain:
            domains = list(wm.get("agents.akhil.session_domains", []) or [])
            if not domains or domains[-1] != domain:
                domains.append(domain)
            if len(domains) > 20:
                domains = domains[-20:]
            wm.update("agents.akhil.session_domains", domains)

        # Update expertise map from domain access frequency
        if domain and domain not in ("unknown", "constitutional"):
            self._update_expertise(domain)

    def mark_open_problem(self, description: str) -> None:
        """Record that Akhil has an open problem."""
        problems = list(self._wm.get("agents.akhil.open_problems", []) or [])
        if description not in problems:
            problems.insert(0, description)
        if len(problems) > 5:
            problems = problems[:5]
        self._wm.update("agents.akhil.open_problems", problems)

    def resolve_problem(self, description: str) -> None:
        """Mark a problem as resolved."""
        problems = list(self._wm.get("agents.akhil.open_problems", []) or [])
        problems = [p for p in problems if description not in p]
        self._wm.update("agents.akhil.open_problems", problems)

    def record_decision(self, description: str) -> None:
        """Record a recent decision/change made to the system."""
        decisions = list(self._wm.get("agents.akhil.recent_decisions", []) or [])
        import time as _t
        decisions.insert(0, {"description": description, "ts": _t.time()})
        if len(decisions) > 10:
            decisions = decisions[:10]
        self._wm.update("agents.akhil.recent_decisions", decisions)

    def _update_expertise(self, domain: str) -> None:
        """
        Slowly build expertise scores per domain from access frequency.
        Score approaches 1.0 as Akhil consistently queries a domain.
        Decays slowly when a domain isn't visited.
        """
        expertise = dict(self._wm.get("agents.akhil.expertise_map", {}) or {})
        current = float(expertise.get(domain, 0.0))
        # Small increment each access, capped at 1.0
        new_score = min(1.0, current + 0.005)
        expertise[domain] = round(new_score, 4)
        self._wm.update("agents.akhil.expertise_map", expertise)
