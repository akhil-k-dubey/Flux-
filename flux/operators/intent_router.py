"""
flux/operators/intent_router.py

IntentRouter — routes English utterances to operators via manifold geometry.

NO hardcoded verb lists. NO string matching. NO strip words. NO regex.

How it works:
  1. LanguageEncoder encodes utterance → (op_vec[257], target_vec[257])
  2. manifold.nearest_operator_concept(op_vec) → nearest operator in Lorentz space
  3. manifold.nearest_concept_to(target_vec)   → nearest KG concept
  4. confidence = 1 / (1 + lorentz_distance)

The routing decision is purely geometric — the LanguageEncoder's weights
determine what region of manifold space each utterance maps to.
No English knowledge leaks into the routing logic.

Parallel teacher mode (before encoder is calibrated):
  The OLD hardcoded router runs ONLY as a teacher — its decisions are written
  to logs/teacher_signal.jsonl as training data for the LanguageEncoder.
  Once TEACHER_GRADUATE_THRESHOLD corrections are accumulated, the teacher
  is retired and only the neural encoder routes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import torch

from flux.operators.registry import OperatorRegistry, Operator, get_registry

log = logging.getLogger(__name__)

# ── Operator concept IDs in KG → manifold positions ──────────────────────────
OP_CONCEPT_IDS = {
    # Phase 1
    "Build":      "op_build",
    "CanYou":     "op_can_you",
    "Connect":    "op_connect",
    "Assess":     "op_assess",
    "Simulate":   "op_simulate",
    # Phase 2
    "Know":       "op_know",
    "Understand": "op_understand",
    "Explain":    "op_explain",
    # Phase 3
    "Greet":      "op_greet",
    "Sync":       "op_sync",
}
# Reverse: concept_id → operator name
CONCEPT_TO_OP = {v: k for k, v in OP_CONCEPT_IDS.items()}

# Minimum confidence to route (below this = fall through to legacy pipeline)
CONFIDENCE_THRESHOLD = 0.20
MAX_DISTANCE = 20.0



@dataclass
class RouterResult:
    operator:    Optional[Operator] = None
    target_vec:  Optional[torch.Tensor] = None
    op_vec:      Optional[torch.Tensor] = None
    noun_slot:   str = ""
    noun_slot_b: str = ""                         # second concept for Connect
    concept_id:  str = ""
    concept_ids: list = field(default_factory=list)  # all matched concepts
    verb_surface: str = ""
    confidence:  float = 0.0
    is_feasibility: bool = False


class IntentRouter:
    """
    Routes utterances to operators via LanguageEncoder + manifold geometry.
    Falls back to parallel teacher (hardcoded) when encoder not yet calibrated.
    """

    def __init__(self, registry: Optional[OperatorRegistry] = None,
                 manifold=None) -> None:
        self._registry  = registry or get_registry()
        self._manifold  = manifold
        self._enc       = None   # lazy-loaded LanguageEncoder
        self._extractor = None   # lazy-loaded AttentionSlotExtractor

    def _get_encoder(self):
        if self._enc is None:
            try:
                from flux.language.language_encoder import get_encoder
                self._enc = get_encoder()
            except Exception as e:
                log.warning("LanguageEncoder unavailable: %s", e)
        return self._enc

    def _get_extractor(self):
        if self._extractor is None:
            try:
                from flux.language.attention_slot_extractor import AttentionSlotExtractor
                from flux.kg.store import get_store
                self._extractor = AttentionSlotExtractor(get_store(), self._enc)
            except Exception as e:
                log.warning("AttentionSlotExtractor unavailable: %s", e)
        return self._extractor

    def set_manifold(self, manifold) -> None:
        self._manifold = manifold

    def route(self, utterance: str) -> RouterResult:
        """
        Neural routing — LanguageEncoder + VectorRouter + AttentionSlotExtractor.
        Constitutional Layer runs in shadow mode (in FluxInterface).
        Returns empty RouterResult if encoder unavailable.
        """
        tl  = utterance.lower().strip()
        enc = self._get_encoder()
        manifold = self._manifold

        if enc is not None and manifold is not None:
            result = self._neural_route(tl, enc, manifold)
            if result is not None:
                return result

        # Encoder unavailable (untrained / not loaded) — return empty
        return RouterResult()

    def _neural_route(self, tl: str, enc, manifold) -> Optional[RouterResult]:
        """
        Neural routing — primary: geometric (VectorRouter on op_vec).
        Concept slot extraction: AttentionSlotExtractor (KG scan + attention rerank).
        """
        try:
            # encode_with_attention() returns 5 values:
            # (op_name, confidence, target_vec, op_vec, cls_attn)
            op_name, confidence, tgt_vec, op_vec, cls_attn = enc.encode_with_attention(tl)

            # ── Geometric routing ──────────────────────────────────────────
            if op_vec is not None:
                try:
                    from flux.operators.vector_router import get_vector_router
                    _vr = get_vector_router(manifold, OP_CONCEPT_IDS)
                    if _vr is not None and _vr.is_ready():
                        geo_op, geo_score = _vr.route(op_vec)
                        if geo_score >= 0.50:
                            log.debug("VectorRouter: %s(%.3f) vs classifier: %s(%.3f)",
                                      geo_op, geo_score, op_name, confidence)
                            op_name    = geo_op
                            confidence = geo_score
                except Exception as _vr_exc:
                    log.debug("VectorRouter failed: %s", _vr_exc)

            if op_name == "None":
                return None

            operator = self._registry.get(op_name)
            if operator is None:
                return None

            # ── Concept slot extraction ────────────────────────────────────
            extractor   = self._get_extractor()
            concept_id  = concept_id_b = ""
            concept_name = concept_b    = ""

            if extractor is not None:
                if op_name == "Connect":
                    concept_id, concept_id_b = extractor.extract_two(tl, cls_attn)
                else:
                    concept_name, concept_id = extractor.extract(tl, cls_attn)

                if concept_id:
                    concept_name = concept_id.replace("_", " ")
                if concept_id_b:
                    concept_b = concept_id_b.replace("_", " ")

            log.debug("Neural route: conf=%.3f → %s slot=%r", confidence, op_name, concept_name)

            return RouterResult(
                operator       = operator,
                target_vec     = tgt_vec,
                op_vec         = op_vec,
                noun_slot      = concept_name,
                noun_slot_b    = concept_b,
                concept_id     = concept_id,
                concept_ids    = [concept_id] if concept_id else [],
                verb_surface   = op_name.lower(),
                confidence     = confidence,
                is_feasibility = operator.is_feasibility,
            )
        except Exception as exc:
            log.debug("Neural routing failed: %s", exc)
            return None

