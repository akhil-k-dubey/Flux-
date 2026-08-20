"""
flux/language/kg_index.py

KG concept resolver — 3-tier approach:

  Tier 1 — Exact/near-exact string matching
    "gibbs free energy" → normalise → "gibbs_free_energy" → direct KG hit
    Handles 90%+ of real queries. Zero cost.

  Tier 2 — Token overlap in concept names
    "gibbs energy" → tokens [gibbs, energy] → search all concept IDs
    for ones containing these tokens → "gibbs_free_energy" scores highest.
    Handles abbreviations, dropped words, informal naming.

  Tier 3 — TF-IDF over concept definitions
    "the energy that determines if reactions go forward spontaneously"
    → TF-IDF search over all definitions → "gibbs_free_energy" definition
    contains spontaneity, reactions, thermodynamic → scores highest.
    Handles descriptive / indirect references.

  Tier 4 (future slot) — LTC embedding similarity
    Activated when the LTC has been contrastively fine-tuned on
    (anchor, positive, negative) triplets from interaction logs.
    Currently disabled — ltc_embedder.encode() returns None until trained.

Joint scoring:
  When two candidates share a KG edge, both scores get a boost.
  KG structure validates the parse.

Persisted TF-IDF index to logs/kg_tfidf_index.json — fast startup after first build.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

TFIDF_PATH   = Path("logs/kg_tfidf_index.json")
LSA_PATH     = Path("logs/kg_lsa_embeddings.npz")
_EDGE_BOOST  = 0.12
_LSA_WEIGHT  = 0.45   # Tier 4 blend: 45% LSA cosine, 55% TF-IDF tiers

# Stopwords for TF-IDF (common words that add no signal)
_TFIDF_STOP = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "not", "that", "this", "it", "its", "as",
    "which", "where", "when", "how", "what", "who", "can", "will",
    "have", "has", "had", "been", "being", "do", "does", "did",
})


class KGIndex:
    def __init__(self) -> None:
        self._store        = None
        self._nodes:  dict = {}        # concept_id → node dict
        # TF-IDF state
        self._vocab:  dict[str, int] = {}        # token → doc frequency
        self._tfidf:  dict[str, dict[str, float]] = {}  # concept_id → {token: tfidf}
        self._n_docs: int = 0
        # Tier 4: LSA embeddings (built from same TF-IDF, zero extra training)
        self._lsa_data: dict | None = None
        # Tier 5: DualEncoderIndex — lazy-loaded on first uncertain query
        self._dual_enc  = None   # DualEncoderIndex | None
        self._dual_tried: bool = False   # don't retry if load failed

    # ── Build / Load ─────────────────────────────────────────────────────────

    def build(self, store) -> int:
        self._store = store
        t0 = time.perf_counter()
        self._nodes = {
            nid: nd for nid, nd in store.all_nodes().items()
            if nd.get("type") != "ACTION_CONSEQUENCE_RECORD"
        }
        self._build_tfidf()
        self._build_lsa()            # Tier 4 — instant, uses existing TF-IDF
        self._save()
        elapsed = round((time.perf_counter() - t0) * 1000)
        lsa_note = f" + LSA-{self._lsa_data['matrix'].shape[1]}d" if self._lsa_data else ""
        log.info("KGIndex: built TF-IDF%s over %d concepts in %dms",
                 lsa_note, len(self._nodes), elapsed)
        return len(self._nodes)

    def load(self, store) -> bool:
        if not TFIDF_PATH.exists():
            return False
        self._store = store
        self._nodes = {
            nid: nd for nid, nd in store.all_nodes().items()
            if nd.get("type") != "ACTION_CONSEQUENCE_RECORD"
        }
        try:
            data = json.loads(TFIDF_PATH.read_text(encoding="utf-8"))
            self._vocab  = data["vocab"]
            self._tfidf  = data["tfidf"]
            self._n_docs = data["n_docs"]
            # Load LSA embeddings
            from flux.language import lsa_embedder
            self._lsa_data = lsa_embedder.load(LSA_PATH)
            lsa_note = f" + LSA-{self._lsa_data['matrix'].shape[1]}d" if self._lsa_data else ""
            log.info("KGIndex: loaded TF-IDF%s cache (%d concepts)", lsa_note, self._n_docs)
            return True
        except Exception as exc:
            log.warning("KGIndex: cache load failed (%s) — rebuilding", exc)
            return False

    def rebuild(self, store) -> int:
        for p in (TFIDF_PATH, LSA_PATH):
            if p.exists(): p.unlink()
        return self.build(store)

    def add_concept(self, concept_id: str, node: dict) -> None:
        """Incrementally add one concept (called after TEACH)."""
        self._nodes[concept_id] = node
        self._build_tfidf()
        self._build_lsa()

    # ── Query ────────────────────────────────────────────────────────────────

    def query_one(self, span: str, top_k: int = 5) -> list[tuple[str, float]]:
        """
        Find top-k matching concepts for span.
        Tiers: Tier 1 (exact) → Tier 2 (token overlap) → Tier 3 (TF-IDF) → Tier 4 (LSA)
        """
        scores: dict[str, float] = {}

        # Tier 1 — exact / near-exact string match
        t1 = self._tier1(span)
        for cid, s in t1:
            scores[cid] = max(scores.get(cid, 0.0), s)

        # Tier 2 — token overlap in concept name
        t2 = self._tier2(span)
        for cid, s in t2:
            scores[cid] = max(scores.get(cid, 0.0), s * 0.9)

        # Tier 3 — TF-IDF over definitions (for descriptive queries)
        t3 = self._tier3(span)
        for cid, s in t3:
            scores[cid] = max(scores.get(cid, 0.0), s * 0.75)

        # Tier 4 — LSA semantic similarity
        # Only fires when Tiers 1-3 are uncertain (max score < 0.60).
        # High-confidence T1 hits (exact match = 1.0) must not be polluted.
        max_t123 = max(scores.values()) if scores else 0.0
        if max_t123 < 0.60:
            t4 = self._tier4_lsa(span)
            for cid, s in t4:
                if cid in scores:
                    # Gentle boost to existing candidates
                    scores[cid] = scores[cid] * 0.70 + s * 0.30
                else:
                    # Introduce LSA-only candidates at reduced weight
                    scores[cid] = s * _LSA_WEIGHT

        # Tier 5 — DualEncoder semantic search (trained NodeEncoder, 128-dim)
        # Only fires when Tiers 1-4 left scores uncertain (max < 0.40).
        # Higher precision than LSA for paraphrased or descriptive queries.
        max_t1234 = max(scores.values()) if scores else 0.0
        if max_t1234 < 0.40:
            t5 = self._tier5_dual_encoder(span)
            for cid, s in t5:
                if cid in scores:
                    scores[cid] = scores[cid] * 0.60 + s * 0.40
                else:
                    scores[cid] = s * 0.55

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(cid, s) for cid, s in ranked[:top_k] if s > 0.25]

    def query_joint(
        self,
        primary_span:   str,
        secondary_span: str,
        top_k: int = 5,
    ) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """Joint resolution with KG-edge boost."""
        p_hits = self.query_one(primary_span,   top_k)
        s_hits = self.query_one(secondary_span, top_k)

        if p_hits and s_hits:
            best_p, best_s = p_hits[0][0], s_hits[0][0]
            if self._store and _concepts_connected(self._store, best_p, best_s):
                p_hits = [(c, s + _EDGE_BOOST) for c, s in p_hits]
                s_hits = [(c, s + _EDGE_BOOST) for c, s in s_hits]
                log.debug("KGIndex: edge boost (%s ↔ %s)", best_p, best_s)

        return p_hits, s_hits

    # ── Tier implementations ─────────────────────────────────────────────────

    def _tier1(self, span: str) -> list[tuple[str, float]]:
        """Exact and near-exact string match against concept IDs."""
        normalised = _normalise(span)
        results = []

        # Exact
        if normalised in self._nodes:
            results.append((normalised, 1.0))
            return results

        # Check with common prefixes/suffixes stripped
        for cid in self._nodes:
            if cid == normalised:
                results.append((cid, 1.0))
            elif cid.startswith(normalised) or normalised.startswith(cid):
                overlap = min(len(cid), len(normalised)) / max(len(cid), len(normalised))
                if overlap > 0.75:
                    results.append((cid, 0.90 * overlap))

        return sorted(results, key=lambda x: x[1], reverse=True)[:5]

    def _tier2(self, span: str) -> list[tuple[str, float]]:
        """Token overlap between span tokens and concept ID tokens."""
        span_tokens = set(_tokenise(span)) - _TFIDF_STOP
        if not span_tokens:
            return []

        results = []
        for cid in self._nodes:
            cid_tokens = set(cid.replace("_", " ").split())
            overlap    = len(span_tokens & cid_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(span_tokens), len(cid_tokens))
            if score > 0.30:
                results.append((cid, score))

        return sorted(results, key=lambda x: x[1], reverse=True)[:8]

    def _tier3(self, span: str) -> list[tuple[str, float]]:
        """TF-IDF cosine similarity between span and concept definitions."""
        if not self._tfidf:
            return []

        span_tokens = [t for t in _tokenise(span) if t not in _TFIDF_STOP]
        if not span_tokens:
            return []

        # Build query TF vector
        query_tf: dict[str, float] = defaultdict(float)
        for t in span_tokens:
            query_tf[t] += 1.0
        for t in query_tf:
            query_tf[t] /= len(span_tokens)

        # Cosine similarity against each concept's TF-IDF vector
        results = []
        for cid, doc_vec in self._tfidf.items():
            score = sum(
                query_tf.get(t, 0.0) * doc_vec.get(t, 0.0)
                for t in query_tf
            )
            if score > 0.05:
                results.append((cid, score))

        return sorted(results, key=lambda x: x[1], reverse=True)[:8]

    # ── TF-IDF builder ───────────────────────────────────────────────────────

    def _build_tfidf(self) -> None:
        """Build TF-IDF index over concept definitions + names."""
        doc_texts: dict[str, list[str]] = {}
        for cid, nd in self._nodes.items():
            tokens = _tokenise(
                cid.replace("_", " ") + " " +
                str(nd.get("definition") or "") + " " +
                str(nd.get("layer") or "")
            )
            doc_texts[cid] = [t for t in tokens if t not in _TFIDF_STOP and len(t) > 1]

        self._n_docs = len(doc_texts)
        # Document frequency
        df: dict[str, int] = defaultdict(int)
        for tokens in doc_texts.values():
            for t in set(tokens):
                df[t] += 1

        # TF-IDF per concept
        self._tfidf = {}
        for cid, tokens in doc_texts.items():
            tf: dict[str, float] = defaultdict(float)
            for t in tokens:
                tf[t] += 1.0
            for t in tf:
                tf[t] /= len(tokens) if tokens else 1
                idf = math.log((self._n_docs + 1) / (df[t] + 1)) + 1.0
                tf[t] *= idf

            # Keep only top-20 tokens per concept to save memory
            top = sorted(tf.items(), key=lambda x: x[1], reverse=True)[:20]
            self._tfidf[cid] = dict(top)

        self._vocab = dict(df)

    def _save(self) -> None:
        TFIDF_PATH.parent.mkdir(parents=True, exist_ok=True)
        TFIDF_PATH.write_text(json.dumps({
            "vocab":  self._vocab,
            "tfidf":  self._tfidf,
            "n_docs": self._n_docs,
        }, ensure_ascii=False))
        if self._lsa_data is not None:
            from flux.language import lsa_embedder
            lsa_embedder.save(self._lsa_data, LSA_PATH)

    # ── Tier 4: LSA embedding index ───────────────────────────────────────────

    def _build_lsa(self) -> None:
        """Build LSA embedding matrix from the current TF-IDF index."""
        if not self._tfidf:
            return
        from flux.language import lsa_embedder
        try:
            self._lsa_data = lsa_embedder.build(self._tfidf, self._vocab)
        except Exception as exc:
            log.warning("KGIndex: LSA build failed (%s) — Tier 4 disabled", exc)
            self._lsa_data = None

    def _tier4_lsa(self, span: str) -> list[tuple[str, float]]:
        """LSA cosine similarity: encodes query into latent space, finds nearest concepts."""
        if self._lsa_data is None:
            return []

        from flux.language import lsa_embedder
        q_vec = lsa_embedder.encode_query(
            span,
            self._lsa_data["vocab_order"],
            self._lsa_data["Vt"],
            self._lsa_data["s"],
        )
        if q_vec is None:
            return []

        matrix = self._lsa_data["matrix"]   # (n_concepts, k) L2-normed
        ids    = self._lsa_data["ids"]
        sims   = matrix @ q_vec              # (n_concepts,) cosine similarity

        top_k   = min(5, len(ids))
        top_idx = np.argpartition(sims, -top_k)[-top_k:]
        top_idx = top_idx[np.argsort(sims[top_idx])[::-1]]

        results = []
        for idx in top_idx:
            s = float(sims[idx])
            if s > 0.20:   # lower threshold — LSA scores are softer than TF-IDF
                results.append((ids[idx], s))
        return results

    def _load_dual_enc(self) -> None:
        """Lazy-load DualEncoderIndex on first uncertain query. One-time attempt."""
        if self._dual_tried:
            return
        self._dual_tried = True
        try:
            from flux.language.dual_encoder.dual_encoder_index import DualEncoderIndex
            idx = DualEncoderIndex()
            if idx.load():
                self._dual_enc = idx
                log.info("KGIndex: Tier 5 DualEncoder loaded (%d nodes)", len(idx._ids))
            else:
                log.debug("KGIndex: DualEncoder cache not found — Tier 5 disabled")
        except Exception as exc:
            log.debug("KGIndex: DualEncoder unavailable (%s) — Tier 5 disabled", exc)

    def _tier5_dual_encoder(self, span: str) -> list[tuple[str, float]]:
        """
        DualEncoder semantic similarity (Tier 5).

        Uses the trained KGNodeEncoder to embed the query and find nearest
        KG node definitions. Only activated when Tiers 1–4 failed to reach
        score 0.40 — meaning exact/token/TF-IDF/LSA all uncertain.

        Typical triggers:
          - Paraphrased queries: "what increases with disorder?" → entropy
          - Descriptive queries: "the process that makes heat flow"  → thermodynamics
          - Cross-lingual synonyms not in TF-IDF vocabulary
        """
        self._load_dual_enc()
        if self._dual_enc is None:
            return []
        try:
            return self._dual_enc.query_one(span, top_k=5)
        except Exception as exc:
            log.debug("Tier5 DualEncoder query failed: %s", exc)
            return []

# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Normalise text to KG concept ID format."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower().strip()).strip("_")


def _tokenise(text: str) -> list[str]:
    return re.split(r"[^a-z0-9]+", text.lower().strip())


def _concepts_connected(store, a: str, b: str) -> bool:
    try:
        edges = store._edges_from.get(a, [])
        if any(e.get("to") == b for e in edges):
            return True
        edges = store._edges_from.get(b, [])
        return any(e.get("to") == a for e in edges)
    except Exception:
        return False


# ── Module-level singleton ────────────────────────────────────────────────────

_index: KGIndex | None = None


def get_index() -> KGIndex:
    global _index
    if _index is None:
        _index = KGIndex()
    return _index


def build(store) -> int:
    idx = get_index()
    if not idx.load(store):
        return idx.build(store)
    return len(idx._nodes)


def rebuild(store) -> int:
    return get_index().rebuild(store)
