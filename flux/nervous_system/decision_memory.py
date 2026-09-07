"""
flux/nervous_system/decision_memory.py

Flux's decision memory — reads the RSM L3 log files and lets Flux explain
her own architectural decisions in her own reasoning.

The key insight: layer3_approvals.jsonl contains a `reason` field with the
full text Flux generated when she proposed a change. That IS her own thinking.
This module surfaces that thinking when asked "why did you propose X".

Query:
    from flux.nervous_system.decision_memory import DecisionMemory
    dm = DecisionMemory()
    result = dm.query_why("cons_bottleneck")
    # → Flux's own reasoning text from the L3 approval log
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_L3_APPROVALS   = Path("logs/layer3_approvals.jsonl")
_L3_REJECTIONS  = Path("logs/layer3_rejections.jsonl")
_L3_QUEUE       = Path("logs/layer3_queue.jsonl")
_L3_DIAGNOSES   = Path("logs/layer3_diagnoses.jsonl")


@dataclass
class Decision:
    """A single RSM decision record."""
    proposal_id:    str
    timestamp:      float
    event:          str                 # APPROVED | REJECTED | CREATED
    change_type:    str                 # DEEPEN | SHRINK | GROW | REGULARIZE
    hypothesis:     str                 # Flux's original proposal hypothesis
    reason:         str                 # Flux's full reasoning (if approved)
    changes:        dict = field(default_factory=dict)   # what actually changed
    architecture:   dict = field(default_factory=dict)   # resulting architecture
    benchmark_score: float = 0.0


class DecisionMemory:
    """
    Reads L3 decision logs and answers questions about past RSM decisions.
    Lazy-loaded — reads on first query, then cached in memory.
    """

    def __init__(self) -> None:
        self._approvals:  list[Decision] = []
        self._rejections: list[Decision] = []
        self._pending:    list[Decision] = []
        self._loaded:     bool           = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._approvals  = self._read_file(_L3_APPROVALS,  "APPROVED")
        self._rejections = self._read_file(_L3_REJECTIONS, "REJECTED")
        self._pending    = self._read_file(_L3_QUEUE,      "CREATED")
        self._loaded     = True
        log.info(
            "DecisionMemory: loaded %d approvals, %d rejections, %d pending",
            len(self._approvals), len(self._rejections), len(self._pending),
        )

    def _read_file(self, path: Path, expected_event: str) -> list[Decision]:
        if not path.exists():
            return []
        decisions: list[Decision] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                d = Decision(
                    proposal_id    = rec.get("proposal_id", ""),
                    timestamp      = float(rec.get("timestamp", 0)),
                    event          = rec.get("event", expected_event),
                    change_type    = rec.get("change_type", ""),
                    hypothesis     = rec.get("hypothesis", ""),
                    reason         = rec.get("reason", ""),
                    changes        = rec.get("changes", {}),
                    architecture   = rec.get("architecture", {}),
                    benchmark_score = float(rec.get("benchmark_score", 0)),
                )
                decisions.append(d)
            except Exception as exc:
                log.debug("DecisionMemory: skipping malformed line: %s", exc)
        return decisions

    # ── Public API ────────────────────────────────────────────────────────────

    def query_why(self, query: str) -> str:
        """
        Answer "why did you propose/decide X?"

        Searches approvals first (these have the full reasoning text), then pending.
        Returns Flux's own reasoning — NOT a KG lookup.
        """
        self._ensure_loaded()

        # Search approvals first — these have the full `reason` field (Flux's actual thinking)
        best_approved = self._find_best_match(query, self._approvals)
        if best_approved:
            return self._format_decision(best_approved, query)

        # Fall back to pending proposals (shorter, no full reasoning)
        best_pending = self._find_best_match(query, self._pending)
        if best_pending:
            return self._format_decision(best_pending, query)

        return (
            "I don't have a recorded decision matching that. "
            "My RSM decisions are logged in the Layer 3 queue — if this was "
            "a recent proposal, it may still be pending review."
        )

    def query_history(self) -> str:
        """Return all past approved decisions chronologically."""
        self._ensure_loaded()
        if not self._approvals:
            return "No approved RSM decisions recorded yet."

        parts = ["My architectural decision history:\n"]
        for d in sorted(self._approvals, key=lambda x: x.timestamp):
            arch_str = ", ".join(f"{k}={v}" for k, v in d.changes.items())
            parts.append(f"  [{d.change_type}] {d.hypothesis[:80]}...")
            parts.append(f"    → Changes: {arch_str}")
            parts.append(f"    → Benchmark after: {d.benchmark_score:.2f}")
        return "\n".join(parts)

    def query_pending(self) -> str:
        """Return all proposals still pending decision."""
        self._ensure_loaded()
        pending = [d for d in self._pending if d.event == "CREATED"]
        if not pending:
            return "No pending RSM proposals in the queue."

        parts = ["Pending architectural proposals:\n"]
        for d in pending:
            arch_str = ", ".join(f"{k}={v}" for k, v in d.changes.items())
            parts.append(f"  [{d.change_type}] {d.hypothesis[:100]}")
            parts.append(f"    → Would change: {arch_str}")
        return "\n".join(parts)

    def query_status(self) -> str:
        """Quick status of the RSM decision system."""
        self._ensure_loaded()
        return (
            f"RSM Decision Memory: "
            f"{len(self._approvals)} approved, "
            f"{len(self._rejections)} rejected, "
            f"{len([d for d in self._pending if d.event == 'CREATED'])} pending."
        )

    # ── Matching ─────────────────────────────────────────────────────────────

    def _find_best_match(self, query: str, candidates: list[Decision]) -> Optional[Decision]:
        """Score each decision against the query and return the best match."""
        q_lower  = query.lower()
        q_tokens = set(re.findall(r"[a-z0-9]+", q_lower))

        # Remove very common words from the query tokens
        stop = {"why", "did", "you", "propose", "implement", "add", "change",
                 "the", "a", "an", "to", "for", "what", "was", "reason",
                 "behind", "your", "decision", "about", "how"}
        q_tokens -= stop

        if not q_tokens:
            # If nothing specific, return the most recent approved decision
            if candidates:
                return sorted(candidates, key=lambda d: d.timestamp)[-1]
            return None

        best_score   = 0.0
        best_decision: Optional[Decision] = None

        for d in candidates:
            # Score = overlap between query tokens and decision text tokens
            text_tokens = set(re.findall(r"[a-z0-9]+",
                (d.hypothesis + " " + d.reason + " " + d.change_type + " " +
                 " ".join(f"{k} {v}" for k, v in d.changes.items())).lower()))

            overlap = len(q_tokens & text_tokens)
            # Bonus: if change_type appears in query
            if d.change_type.lower() in q_lower:
                overlap += 2
            # Bonus: if changed keys appear in query
            for changed_key in d.changes:
                if changed_key.replace("_", " ") in q_lower or changed_key in q_lower:
                    overlap += 3

            if overlap > best_score:
                best_score    = overlap
                best_decision = d

        return best_decision if best_score > 0 else None

    def _format_decision(self, d: Decision, query: str) -> str:
        """Format a decision into Flux's own voice."""
        arch_str = ", ".join(f"{k}: {v}" for k, v in d.changes.items())
        status   = "approved and implemented" if d.event == "APPROVED" else "pending"

        parts: list[str] = []

        # Header
        parts.append(
            f"Here is my reasoning for the [{d.change_type}] proposal ({status}):\n"
        )

        # Hypothesis — what she proposed
        if d.hypothesis:
            parts.append(f"What I proposed: {d.hypothesis}\n")

        # The reason — Flux's own thinking, verbatim from L3 log
        if d.reason:
            # Clean up the reason text a bit
            reason_clean = d.reason.replace("\\u2014", "—").replace("\\u2190", "→")
            parts.append("My reasoning:\n")
            # Split into paragraphs if it's long
            if len(reason_clean) > 500:
                # Find natural break points
                sentences = re.split(r'\. (?=[A-Z])', reason_clean)
                # Take first 5 sentences for readability
                excerpt = ". ".join(sentences[:6]).strip()
                if not excerpt.endswith("."):
                    excerpt += "."
                parts.append(excerpt)
                parts.append(f"\n[Full reasoning is {len(reason_clean)} chars — "
                             f"ask me to 'elaborate on the {d.change_type} decision' "
                             f"for the complete text]")
            else:
                parts.append(reason_clean)

        # Outcome
        if d.event == "APPROVED" and d.architecture:
            arch_after = ", ".join(f"{k}={v}" for k, v in d.architecture.items()
                                   if k in d.changes)
            parts.append(f"\nResulting architecture change: {arch_str}")
            if d.benchmark_score > 0:
                parts.append(f"Benchmark score after change: {d.benchmark_score:.2f}")

        return "\n".join(parts)
