"""
flux/language/dual_encoder/dual_encoder_index.py

Drop-in replacement for kg_index.KGIndex concept matching.

Query encoding:  LTC forward-pass 128-dim (no proj head, no bidirectional)
Node encoding:   KGNodeEncoder (trained, 128-dim)
Matching:        cosine similarity

"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import torch

log = logging.getLogger(__name__)

CACHE_NPY  = Path("checkpoints/dual_encoder/node_embeddings_cache.npy")
CACHE_IDS  = Path("checkpoints/dual_encoder/node_embeddings_ids.json")


class DualEncoderIndex:
    """
    Cosine-similarity concept index using asymmetric dual encoder.

    Query side: frozen LTC forward-pass (character-level sequential processing)
    Node  side: KGNodeEncoder (semantic discrimination, trained on KG structure)
    """

    def __init__(self) -> None:
        self._encoder = None          # KGNodeEncoder
        self._matrix: np.ndarray | None = None   # (N, 128) float32, L2-normed rows
        self._ids:    list[str] = []
        self._ready = False

    # ── Build / Load ─────────────────────────────────────────────────────────

    def build(self, nodes: dict, encoder=None) -> int:
        """
        Pre-compute node embeddings for all usable KG nodes.

        Args:
            nodes:   KGStore.all_nodes()
            encoder: trained KGNodeEncoder (loads best checkpoint if None)
        """
        from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder, PHASE2_BEST, PHASE1_CKPT

        if encoder is None:
            ckpt = PHASE2_BEST if PHASE2_BEST.exists() else PHASE1_CKPT
            if not ckpt.exists():
                raise FileNotFoundError(
                    f"No trained node encoder found. Run: "
                    f"python -m flux.language.dual_encoder.runner --phase 1"
                )
            encoder = KGNodeEncoder.load(ckpt)
            log.info("DualEncoderIndex: loaded encoder from %s", ckpt.name)

        self._encoder = encoder
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        encoder.to(device).eval()

        usable = {
            cid: nd for cid, nd in nodes.items()
            if not cid.startswith(("axiom_", "__", "consequence_query_"))
            and nd.get("type") != "ACTION_CONSEQUENCE_RECORD"
            and nd.get("definition", "")
        }

        ids, vecs = [], []
        with torch.no_grad():
            for cid, nd in usable.items():
                defn = nd.get("definition", "")[:400]
                vec  = encoder.encode(defn, device=device).cpu().numpy()
                ids.append(cid)
                vecs.append(vec)

        self._ids    = ids
        self._matrix = np.stack(vecs).astype(np.float32)  # (N, 128)
        self._ready  = True

        CACHE_NPY.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(CACHE_NPY), self._matrix)
        CACHE_IDS.write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
        log.info("DualEncoderIndex: built %d node embeddings → %s", len(ids), CACHE_NPY)
        return len(ids)

    def load(self) -> bool:
        """Load pre-computed embeddings from disk. Returns True if successful."""
        if not (CACHE_NPY.exists() and CACHE_IDS.exists()):
            return False
        try:
            self._matrix = np.load(str(CACHE_NPY)).astype(np.float32)
            self._ids    = json.loads(CACHE_IDS.read_text(encoding="utf-8"))
            # Also load the encoder for query-side encoding
            from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder, PHASE2_BEST, PHASE1_CKPT
            ckpt = PHASE2_BEST if PHASE2_BEST.exists() else PHASE1_CKPT
            if ckpt.exists():
                self._encoder = KGNodeEncoder.load(ckpt)
                self._encoder.eval()
                log.info("DualEncoderIndex: loaded %d node embeddings + encoder from cache",
                         len(self._ids))
            else:
                log.warning("DualEncoderIndex: no encoder checkpoint found — query encoding unavailable")
            self._ready = self._encoder is not None
            return self._ready
        except Exception as exc:
            log.warning("DualEncoderIndex: cache load failed (%s)", exc)
            return False

    # ── Query ────────────────────────────────────────────────────────────────

    def query_one(self, span: str, top_k: int = 5) -> list[tuple[str, float]]:
        """
        Find top-k concepts matching the query span.
        Uses NodeEncoder for query encoding (symmetric with node encoding).
        Returns [(concept_id, cosine_sim), ...] sorted descending.
        Compatible with KGIndex.query_one() interface.
        """
        if not self._ready or self._encoder is None:
            return []

        device = next(self._encoder.parameters()).device
        q_vec  = self._encoder.encode(span, device=device).cpu().numpy().astype(np.float32)

        sims    = self._matrix @ q_vec.astype(np.float32)  # (N,)
        top_k_n = min(top_k, len(self._ids))
        idx     = np.argpartition(sims, -top_k_n)[-top_k_n:]
        idx     = idx[np.argsort(sims[idx])[::-1]]

        return [
            (self._ids[i], float(sims[i]))
            for i in idx
            if float(sims[i]) > 0.20
        ]

    def query_joint(
        self, primary: str, secondary: str, top_k: int = 5
    ) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """Compatible with KGIndex.query_joint() interface."""
        return self.query_one(primary, top_k), self.query_one(secondary, top_k)

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def n_nodes(self) -> int:
        return len(self._ids)


# ── Module-level singleton ────────────────────────────────────────────────────

_index: DualEncoderIndex | None = None


def get_index() -> DualEncoderIndex:
    global _index
    if _index is None:
        _index = DualEncoderIndex()
    return _index


def build(nodes: dict, encoder=None) -> int:
    return get_index().build(nodes, encoder)


def load() -> bool:
    return get_index().load()
