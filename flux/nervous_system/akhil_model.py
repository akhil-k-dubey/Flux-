"""
flux/nervous_system/akhil_model.py

Tracks Akhil's interaction patterns and updates the WorldModel agents.akhil.*
fields on every query.

Capabilities today:
  - last_interaction: timestamp of every query
  - session_count_today: daily counter (resets at calendar midnight)
  - current_focus_domain: inferred from query text
  - domain_history: last 20 domains for trend detection
  - total_queries: lifetime query count
  - predicted_next_query: after 100+ interactions, frequency-weighted prediction
  - cognitive_load: inferred from query complexity (word count, cross-domain flag)
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import datetime, date
from typing import Optional

log = logging.getLogger(__name__)


class AkhilModel:
    """Updates agents.akhil.* in the WorldModel on every Flux query."""

    def __init__(self, world_model, domain_inferrer_fn) -> None:
        """
        Parameters
        ----------
        world_model         : WorldModel singleton
        domain_inferrer_fn  : callable(query: str) → domain_str
                              Usually domain_inferrer.infer_query_domain
        """
        self._wm    = world_model
        self._infer = domain_inferrer_fn

        # In-memory query log for pattern detection (not persisted separately,
        # but WorldModel domain_history is persisted)
        self._recent_queries: list[str] = []
        self._domain_counts:  Counter   = Counter()

    def on_query(self, query: str, response_text: str = "") -> None:
        """
        Call this on every ask(). Updates all agents.akhil.* fields.
        Lightweight — designed to run synchronously inside ask().
        """
        now  = time.time()
        today = date.today().isoformat()

        # 1. Timestamp
        self._wm.update("agents.akhil.last_interaction", now)

        # 2. Total query count
        total = (self._wm.get("agents.akhil.total_queries") or 0) + 1
        self._wm.update("agents.akhil.total_queries", total)
        self._wm.update("self.total_queries_answered", total)

        # 3. Daily session counter — resets at midnight
        session_date = self._wm.get("agents.akhil.session_date")
        if session_date != today:
            self._wm.update("agents.akhil.session_count_today", 1)
            self._wm.update("agents.akhil.session_date",        today)
            # Record typical session start hour
            hour = datetime.now().hour
            self._wm.update("agents.akhil.typical_session_start", hour)
        else:
            count = (self._wm.get("agents.akhil.session_count_today") or 0) + 1
            self._wm.update("agents.akhil.session_count_today", count)

        # 4. Current focus domain
        domain = self._infer(query)
        self._wm.update("agents.akhil.current_focus_domain", domain)

        # 5. Domain history (last 20)
        history: list = self._wm.get("agents.akhil.domain_history") or []
        history.append(domain)
        if len(history) > 20:
            history = history[-20:]
        self._wm.update("agents.akhil.domain_history", history)

        # 6. Cognitive load — rough proxy from query complexity
        load = self._estimate_cognitive_load(query)
        self._wm.update("agents.akhil.cognitive_load", load)

        # 7. In-memory counts for prediction
        self._recent_queries.append(query)
        if len(self._recent_queries) > 200:
            self._recent_queries = self._recent_queries[-200:]
        self._domain_counts[domain] += 1

        # 8. Prediction (after 100+ queries)
        if total >= 100:
            predicted = self._predict_next(history, domain)
            self._wm.update("agents.akhil.predicted_next_query", predicted)

        # 9. Update JARVIS test status based on response
        if response_text:
            self._update_jarvis_status(query, response_text)

    def _estimate_cognitive_load(self, query: str) -> str:
        """
        Estimate cognitive load from query text.

        Returns: "low" | "medium" | "high"

        Heuristics:
          - word count > 20: at least medium
          - multiple clauses (and/or/if/then): higher
          - multiple named concepts: higher
          - question words: lower
        """
        words      = query.split()
        n_words    = len(words)
        n_commas   = query.count(",")
        multi_clause = len(re.findall(r"\b(and|or|if|then|but|however|given)\b", query, re.I))

        score = 0
        score += min(n_words // 5, 4)        # 0-4 from word count
        score += min(n_commas, 3)             # 0-3 from complexity
        score += min(multi_clause, 3)         # 0-3 from logical connectors

        if score <= 2:    return "low"
        elif score <= 5:  return "medium"
        else:             return "high"

    def _predict_next(self, history: list[str], current_domain: str) -> Optional[str]:
        """
        Simple frequency-weighted prediction of next query domain.
        After 100+ queries, predicts what Akhil will ask about next
        based on domain transition patterns.
        """
        if len(history) < 5:
            return None

        # Look at the last 5 domains and find what typically follows
        recent = history[-5:]
        # Count what came after current_domain in all history
        transitions: Counter = Counter()
        for i in range(len(history) - 1):
            if history[i] == current_domain:
                transitions[history[i + 1]] += 1

        if not transitions:
            # Fallback: most frequent domain overall
            most_common = self._domain_counts.most_common(3)
            if most_common:
                return f"likely: {most_common[0][0]} (most frequent topic)"
            return None

        next_domain = transitions.most_common(1)[0][0]
        return f"likely: {next_domain} (based on {len(self._recent_queries)} query history)"

    def _update_jarvis_status(self, query: str, response: str) -> None:
        """Update JARVIS test status based on query/response pair."""
        q_lower = query.lower()
        r_lower = response.lower()

        tests = {
            "test1_rsm_audit": (
                "rsm" in q_lower and "audit" in q_lower or "know about" in q_lower and "rsm" in q_lower,
                "knowledge audit for" in r_lower or "rsm" in r_lower,
            ),
            "test2_counterfactual_thermo": (
                "reversed" in q_lower or "counterfactual" in q_lower,
                "would break" in r_lower or "causal" in r_lower,
            ),
            "test5_decision_memory": (
                "why did you propose" in q_lower or "why did you" in q_lower,
                "my reasoning" in r_lower or "i proposed" in r_lower,
            ),
        }

        for test_key, (query_matches, response_ok) in tests.items():
            if query_matches:
                status = "PASSING" if response_ok else "FAILING"
                self._wm.update(f"processes.jarvis_tests.{test_key}", status)
