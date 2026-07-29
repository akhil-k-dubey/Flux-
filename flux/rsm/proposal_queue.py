"""
flux/rsm/proposal_queue.py

Manages pending, committed, deferred, and rejected RSM proposals.
Persists to logs/review_queue.jsonl.

Proposal lifecycle:
    pending   → constitutional check → committed  (auto-commit, KG updated)
                                     → deferred   (gap score fell below threshold this run)
    pending   → manual rejection     → rejected   (never re-propose)

The queue is the primary deduplication mechanism: before proposing a concept,
gap_analyser checks whether it's already pending/committed/rejected.
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

QUEUE_PATH = Path("logs/review_queue.jsonl")


class ProposalState(str, Enum):
    PENDING   = "pending"    # proposed, constitutional check ongoing or passed, awaiting RSM commit
    COMMITTED = "committed"  # auto-committed to KG
    DEFERRED  = "deferred"   # gap score below threshold this run, retry next cycle
    REJECTED  = "rejected"   # manually rejected — never re-propose


@dataclass
class ProposalRecord:
    """One entry in review_queue.jsonl."""
    concept_id:           str
    gap_score:            float
    state:                str          # ProposalState value
    teach_text:           str          # generated TEACH block
    synthesis_confidence: float        # 0.65 | 0.75 | 0.80
    mc_mean:              float        # MC Dropout mean confidence (0.0 if not run)
    mc_std:               float        # MC Dropout std (0.0 if not run)
    bridge_confidence:    float        # deterministic bridge confidence (0.0 if not committed)
    kg_action:            str          # "COMMITTED" | "AUGMENTED" | "" (empty if not committed)
    justification:        str          # gap analysis rationale
    created_at:           float        # unix timestamp
    updated_at:           float        # unix timestamp


class ProposalQueue:
    """
    Manages the review queue for RSM Layer 1 proposals.

    Load from disk on init (if file exists). Write individual records
    immediately on append/update — no batching, no data loss on crash.
    """

    def __init__(self, path: Path = QUEUE_PATH) -> None:
        self.path     = path
        self._records: dict[str, ProposalRecord] = {}  # concept_id → ProposalRecord
        self._load()

    # ── Load / Save ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                rec = ProposalRecord(**d)
                # Later records with same concept_id overwrite earlier (most recent wins)
                self._records[rec.concept_id] = rec
            except Exception as exc:
                log.warning("review_queue: skipping malformed line: %s", exc)

    def _append(self, record: ProposalRecord) -> None:
        """Append one record to the JSONL file (idempotent on crash)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    # ── Query ────────────────────────────────────────────────────────────────

    def state_of(self, concept_id: str) -> ProposalState | None:
        """Return current proposal state, or None if never proposed."""
        rec = self._records.get(concept_id)
        return ProposalState(rec.state) if rec else None

    def is_blocked(self, concept_id: str) -> bool:
        """
        True if the concept should NOT be proposed again:
          - committed  → already in KG (caller should check KG first, but belt+suspenders)
          - rejected   → manually rejected, never re-propose
          - pending    → already in queue, don't duplicate
        """
        s = self.state_of(concept_id)
        return s in (ProposalState.COMMITTED, ProposalState.REJECTED, ProposalState.PENDING)

    def all_pending(self) -> list[ProposalRecord]:
        return [r for r in self._records.values() if r.state == ProposalState.PENDING]

    def all_records(self) -> list[ProposalRecord]:
        return list(self._records.values())

    # ── Mutations ────────────────────────────────────────────────────────────

    def add(
        self,
        concept_id:           str,
        gap_score:            float,
        teach_text:           str,
        synthesis_confidence: float,
        justification:        str,
        state:                ProposalState = ProposalState.PENDING,
        mc_mean:              float = 0.0,
        mc_std:               float = 0.0,
        bridge_confidence:    float = 0.0,
        kg_action:            str   = "",
    ) -> ProposalRecord:
        """Add or overwrite a proposal record. Returns the new record."""
        now = time.time()
        rec = ProposalRecord(
            concept_id           = concept_id,
            gap_score            = gap_score,
            state                = state.value,
            teach_text           = teach_text,
            synthesis_confidence = synthesis_confidence,
            mc_mean              = mc_mean,
            mc_std               = mc_std,
            bridge_confidence    = bridge_confidence,
            kg_action            = kg_action,
            justification        = justification,
            created_at           = self._records[concept_id].created_at
                                   if concept_id in self._records else now,
            updated_at           = now,
        )
        self._records[concept_id] = rec
        self._append(rec)
        log.info("RSM queue: %s → %s (gap_score=%.1f)", concept_id, state.value, gap_score)
        return rec

    def update_state(
        self,
        concept_id:        str,
        new_state:         ProposalState,
        mc_mean:           float | None = None,
        mc_std:            float | None = None,
        bridge_confidence: float | None = None,
        kg_action:         str   | None = None,
    ) -> None:
        """Transition a pending record to a new state."""
        rec = self._records.get(concept_id)
        if rec is None:
            log.warning("RSM queue: update_state called for unknown concept %s", concept_id)
            return

        now = time.time()
        updated = ProposalRecord(
            concept_id           = rec.concept_id,
            gap_score            = rec.gap_score,
            state                = new_state.value,
            teach_text           = rec.teach_text,
            synthesis_confidence = rec.synthesis_confidence,
            mc_mean              = mc_mean  if mc_mean  is not None else rec.mc_mean,
            mc_std               = mc_std   if mc_std   is not None else rec.mc_std,
            bridge_confidence    = bridge_confidence if bridge_confidence is not None
                                   else rec.bridge_confidence,
            kg_action            = kg_action if kg_action is not None else rec.kg_action,
            justification        = rec.justification,
            created_at           = rec.created_at,
            updated_at           = now,
        )
        self._records[concept_id] = updated
        self._append(updated)
        log.info("RSM queue: %s → %s", concept_id, new_state.value)

    def reject(self, concept_id: str) -> None:
        """Manually reject a proposal. Never re-proposed."""
        self.update_state(concept_id, ProposalState.REJECTED)

    # ── Summary ──────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        from collections import Counter
        counts = Counter(r.state for r in self._records.values())
        return {
            "total":     len(self._records),
            "pending":   counts.get(ProposalState.PENDING.value, 0),
            "committed": counts.get(ProposalState.COMMITTED.value, 0),
            "deferred":  counts.get(ProposalState.DEFERRED.value, 0),
            "rejected":  counts.get(ProposalState.REJECTED.value, 0),
        }

    def __repr__(self) -> str:
        s = self.summary()
        return (
            f"ProposalQueue(total={s['total']} "
            f"pending={s['pending']} "
            f"committed={s['committed']} "
            f"rejected={s['rejected']})"
        )
