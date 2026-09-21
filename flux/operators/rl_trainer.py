"""
flux/operators/rl_trainer.py

Online RL training loop — connects Akhil's corrections to immediate
weight updates in the LanguageEncoder.

Every correction from Akhil is one gradient step:
  correct_operator("Assess") on "what do you know about X"
  → MSE(predicted_op_vec, manifold.get_embedding("op_assess"))
  → backprop through LanguageEncoder
  → save updated checkpoint

Also writes teacher signal when the HARDCODED IntentRouter handles a query
before the LanguageEncoder is calibrated (parallel teacher mode).
After TEACHER_GRADUATE_THRESHOLD accepted interactions, the hardcoded
router stops being used and LanguageEncoder operates alone.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

log = logging.getLogger(__name__)

TEACHER_LOG                = Path("logs/teacher_signal.jsonl")
RL_LOG                     = Path("logs/rl_updates.jsonl")
TEACHER_GRADUATE_THRESHOLD = 50     # corrections before hardcoded router retires

OP_TO_CONCEPT_ID = {
    "Build":    "op_build",
    "CanYou":   "op_can_you",
    "Connect":  "op_connect",
    "Assess":   "op_assess",
    "Simulate": "op_simulate",
}

_lock       = threading.Lock()
_optimizer  = None
_interaction_count = 0


def _get_optimizer(enc) -> torch.optim.Optimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = torch.optim.AdamW(enc.parameters(), lr=1e-4, weight_decay=1e-4)
    return _optimizer


def teacher_signal(utterance: str, operator_name: str, concept_id: str) -> None:
    """
    Record one interaction handled by the hardcoded router.
    This becomes training data for the LanguageEncoder — the teacher is
    the hardcoded system, the student is the neural encoder.
    """
    TEACHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "timestamp":        time.time(),
        "utterance":        utterance,
        "correct_operator": operator_name,
        "noun_slot":        concept_id,
        "source":           "parallel_teacher",
    }
    with TEACHER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def correction_update(
    utterance:        str,
    correct_operator: str,
    concept_id:       str,
    manifold,
) -> Optional[float]:
    """
    One online RL gradient step on the LanguageEncoder.

    utterance:        the original English query
    correct_operator: the operator Akhil said should have been used
    concept_id:       the grounded concept ID (may be empty if unknown)
    manifold:         CausalManifold instance (must be built)
    Returns:          loss value, or None on failure
    """
    global _interaction_count
    try:
        from flux.language.language_encoder import get_encoder, save_encoder, OP_NAMES
        enc = get_encoder()
        enc.train()
        opt = _get_optimizer(enc)

        # ── Correct operator index ────────────────────────────────────────────
        if correct_operator not in OP_NAMES:
            log.warning("Unknown operator name: %s", correct_operator)
            return None
        correct_idx = OP_NAMES.index(correct_operator)

        # ── Forward pass ──────────────────────────────────────────────────────
        tokens    = enc.tokenize(utterance)
        op_logits, tgt_vec = enc(tokens)                              # [1,5], [1,257]

        # ── Classification loss (same objective as training) ──────────────────
        label = torch.tensor([correct_idx])
        loss  = nn.CrossEntropyLoss()(op_logits, label)

        # ── Optional concept MSE if manifold available ────────────────────────
        if concept_id and manifold is not None:
            try:
                true_tgt = manifold.get_embedding(concept_id)
                if true_tgt is not None:
                    loss = loss + 0.1 * nn.MSELoss()(tgt_vec, true_tgt.unsqueeze(0))
            except Exception:
                pass


        # ── Backward pass ─────────────────────────────────────────────────────
        with _lock:
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(enc.parameters(), max_norm=1.0)
            opt.step()
            _interaction_count += 1

        enc.eval()
        loss_val = float(loss.item())

        # Persist after each correction
        save_encoder(enc)

        # Log the RL update
        RL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp":        time.time(),
                "utterance":        utterance,
                "correct_operator": correct_operator,
                "concept_id":       concept_id,
                "loss":             loss_val,
                "n_updates":        _interaction_count,
            }) + "\n")

        log.info("RL update: %s→%s  loss=%.4f  total_updates=%d",
                 utterance[:40], correct_operator, loss_val, _interaction_count)
        return loss_val

    except Exception as exc:
        log.error("RL update failed: %s", exc)
        return None


def interactions_until_graduation() -> int:
    return max(0, TEACHER_GRADUATE_THRESHOLD - _interaction_count)


def is_graduated() -> bool:
    return _interaction_count >= TEACHER_GRADUATE_THRESHOLD


def positive_signal(utterance: str, operator_name: str, concept_id: str = "") -> None:
    """
    Record that the encoder routed correctly (GREEN constitutional tier).
    Reward signal — written to teacher_signal.jsonl for next retrain cycle.
    """
    TEACHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "timestamp":        time.time(),
        "utterance":        utterance,
        "correct_operator": operator_name,
        "noun_slot":        concept_id,
        "source":           "constitutional_green",
        "reward":           1.0,
    }
    with TEACHER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    log.debug("RL +1.0 (green): %s → %s", utterance[:40], operator_name)


def penalty_signal(utterance: str, neural_operator: str,
                   constitutional_operator: str) -> None:
    """
    Record a RED constitutional divergence — neural operator strongly
    disagrees with the constitutional invariant at high confidence.
    Penalty signal — written with reward=-1.0 and the constitutional
    operator as the correct label for the next retrain cycle.
    """
    TEACHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "timestamp":           time.time(),
        "utterance":           utterance,
        "correct_operator":    constitutional_operator,
        "wrong_operator":      neural_operator,
        "source":              "constitutional_red",
        "reward":              -1.0,
    }
    with TEACHER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    log.warning("RL -1.0 (red): %s → neural=%s but constitutional=%s",
                utterance[:40], neural_operator, constitutional_operator)
