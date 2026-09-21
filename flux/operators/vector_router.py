from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import torch
import torch.nn.functional as F

log = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.30
_EMB_CACHE = Path("logs/manifold_emb_cache.pt")


class VectorRouter:
    """
    Geometric operator router.
    Loads pre-normalised centroid vectors from the manifold embedding cache.
    Thread-safe (read-only after __init__).
    """

    def __init__(self, emb_source, op_concept_ids: dict[str, str]) -> None:
        """
        emb_source: either a dict {concept_id → Tensor} or any object with
                    .get_embedding(concept_id) → Tensor|None
        op_concept_ids: {op_name → concept_id}  e.g. {"Build": "op_build", ...}
        """
        self._centroids: dict[str, torch.Tensor] = {}
        missing = []

        for op_name, concept_id in op_concept_ids.items():
            try:
                if isinstance(emb_source, dict):
                    emb = emb_source.get(concept_id)
                else:
                    emb = emb_source.get_embedding(concept_id)

                if emb is not None:
                    emb_t = emb if isinstance(emb, torch.Tensor) else torch.tensor(emb, dtype=torch.float32)
                    self._centroids[op_name] = F.normalize(emb_t.float(), dim=0)
                else:
                    missing.append(concept_id)
            except Exception:
                missing.append(concept_id)

        if missing:
            log.debug("VectorRouter: no centroid for %s", missing)
        log.info(
            "VectorRouter: %d/%d operator centroids loaded",
            len(self._centroids), len(op_concept_ids),
        )

    def route(self, op_vec: torch.Tensor) -> tuple[str, float]:
        if not self._centroids:
            return "None", 0.0
        vec_norm = F.normalize(op_vec.float(), dim=0)
        best_name, best_score = "None", -1.0
        for op_name, centroid in self._centroids.items():
            score = float(torch.dot(vec_norm, centroid).item())
            if score > best_score:
                best_score = score
                best_name  = op_name
        if best_score < _MIN_CONFIDENCE:
            return "None", best_score
        return best_name, best_score

    def all_scores(self, op_vec: torch.Tensor) -> dict[str, float]:
        if not self._centroids:
            return {}
        vec_norm = F.normalize(op_vec.float(), dim=0)
        return {op: float(torch.dot(vec_norm, c).item()) for op, c in self._centroids.items()}

    def is_ready(self) -> bool:
        return len(self._centroids) > 0


# ── Singleton factory ─────────────────────────────────────────────────────────

_instance: Optional[VectorRouter] = None


def get_vector_router(manifold=None, op_concept_ids: dict | None = None) -> Optional[VectorRouter]:
    """
    Load the singleton VectorRouter.

    Priority:
      1. manifold_emb_cache.pt — loaded directly (no manifold.build() needed)
      2. manifold.get_embedding() — fallback if no cache
    Returns None if neither source is available.
    """
    global _instance
    if _instance is not None:
        return _instance

    if op_concept_ids is None:
        return None

    try:
        if _EMB_CACHE.exists():
            embs = torch.load(_EMB_CACHE, map_location="cpu", weights_only=False)
            _instance = VectorRouter(embs, op_concept_ids)
            return _instance

        if manifold is not None:
            _instance = VectorRouter(manifold, op_concept_ids)
            return _instance
    except Exception as exc:
        log.warning("VectorRouter init failed: %s", exc)

    return None
