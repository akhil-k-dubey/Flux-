"""
flux/language/lsa_embedder.py

Latent Semantic Analysis (LSA) embedder for Flux's concept space.

Builds a 128-dim dense embedding from Flux's own TF-IDF vocabulary
via truncated SVD. These embeddings are:

  • Discriminative   — TF-IDF already separates concepts; SVD densifies that
  • Native           — built entirely from what Akhil taught Flux (zero contamination)
  • Fast             — SVD on ~400×2000 matrix takes <100ms; query encoding <1ms
  • Updatable        — rebuilt whenever KG changes

Architecture
------------
  TF-IDF matrix  (n_concepts × n_vocab)
       ↓  truncated SVD  (k=128 components)
  U  (n_concepts × 128)   ← concept embeddings in latent space
  Σ  (128,)               ← singular values
  Vt (128 × n_vocab)      ← vocabulary-to-latent projection matrix

  Concept embedding:  row_i of (U * Σ), L2-normalised  → 128-dim unit vector
  Query embedding:    tf-idf(query) @ Vt.T, L2-normalised → 128-dim unit vector

Why LSA over the LTC for embedding
-----------------------------------
  The LTC was trained on session QUALITY scoring (consolidation). Its hidden
  states encode "how well-taught is this session?" not "what topic is this?".
  LTC field-level embeddings give:
    transfer_learning ↔ overfitting:  0.9552  (should be low)
    entropy ↔ gibbs_free_energy:      0.7876  (reasonable)
    gibbs ↔ quantum_superposition:    0.6298  (better)

  LSA on the same KG's TF-IDF gives proper semantic distance because TF-IDF
  already encodes which tokens distinguish each concept.

  The LTC remains authoritative for:
    • Session consolidation  (its designed role)
    • Confidence scoring     (its trained objective)
    • Long-range reasoning   (future Layer 3)

Persistence
-----------
  logs/kg_lsa_embeddings.npz:  { matrix, ids, Vt, vocab_order }
  Rebuilt automatically when TF-IDF index is rebuilt.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

LSA_PATH = Path("logs/kg_lsa_embeddings.npz")
N_COMPONENTS = 128   # latent dimensionality


# ── Build ─────────────────────────────────────────────────────────────────────

def build(
    tfidf:       dict[str, dict[str, float]],   # concept_id → {token: tfidf}
    vocab:       dict[str, int],                # token → doc_frequency (for ordering)
    n_components: int = N_COMPONENTS,
) -> dict:
    """
    Build LSA from a TF-IDF index.

    Returns a dict with:
      matrix   (n_concepts, n_components) float32 — L2-normalised concept embeddings
      ids      list[str]                           — concept_id order matching matrix rows
      Vt       (n_components, n_vocab) float32     — for query projection
      vocab_order list[str]                        — token order matching Vt columns
    """
    concept_ids  = sorted(tfidf.keys())
    vocab_tokens = sorted(vocab.keys())
    token_to_idx = {t: i for i, t in enumerate(vocab_tokens)}
    n_c = len(concept_ids)
    n_v = len(vocab_tokens)

    if n_c < 2:
        raise ValueError(f"Need at least 2 concepts, got {n_c}")

    # Build dense TF-IDF matrix  (n_concepts × n_vocab)
    X = np.zeros((n_c, n_v), dtype=np.float32)
    for i, cid in enumerate(concept_ids):
        for tok, score in tfidf[cid].items():
            j = token_to_idx.get(tok)
            if j is not None:
                X[i, j] = score

    # Truncated SVD via numpy (fast for small matrices)
    k = min(n_components, n_c - 1, n_v)
    U, s, Vt = np.linalg.svd(X, full_matrices=False)
    U  = U[:, :k].astype(np.float32)      # (n_c, k)
    s  = s[:k].astype(np.float32)          # (k,)
    Vt = Vt[:k, :].astype(np.float32)     # (k, n_v)

    # Concept embeddings: U * Σ, then L2-normalise
    concept_matrix = U * s[np.newaxis, :]  # (n_c, k)
    norms = np.linalg.norm(concept_matrix, axis=1, keepdims=True)
    concept_matrix = concept_matrix / np.maximum(norms, 1e-8)

    log.info("LSAEmbedder: built %d × %d embedding matrix", n_c, k)
    return {
        "matrix":      concept_matrix,
        "ids":         concept_ids,
        "Vt":          Vt,
        "s":           s,
        "vocab_order": vocab_tokens,
    }


# ── Encode query ──────────────────────────────────────────────────────────────

def encode_query(
    text:        str,
    vocab_order: list[str],
    Vt:          np.ndarray,   # (k, n_vocab)
    s:           np.ndarray,   # (k,)
) -> np.ndarray:
    """
    Encode a query string into the LSA latent space.

    Computes a simple TF vector (no IDF — query is too short), then projects
    through Vt (the vocabulary-to-latent matrix from SVD).

    Returns: (k,) float32 L2-normalised embedding, or None if no token overlap.
    """
    import re
    tokens = re.split(r"[^a-z0-9]+", text.lower().strip())
    token_to_idx = {t: i for i, t in enumerate(vocab_order)}

    # TF vector for query
    qvec = np.zeros(len(vocab_order), dtype=np.float32)
    hits = 0
    for t in tokens:
        if t in token_to_idx:
            qvec[token_to_idx[t]] += 1.0
            hits += 1

    if hits == 0:
        return None

    qvec /= hits  # normalise TF

    # Project into LSA space: q_lsa = Vt @ qvec (then scale by s for LSA)
    # Use pseudo-inverse: q_lsa = (1/s) * Vt @ qvec (folding-in formula)
    proj = (Vt @ qvec) / np.maximum(s, 1e-8)   # (k,)
    norm = np.linalg.norm(proj)
    return proj / norm if norm > 1e-8 else proj


# ── Persistence ───────────────────────────────────────────────────────────────

def save(data: dict, path: Path = LSA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        str(path),
        matrix      = data["matrix"],
        ids         = np.array(data["ids"], dtype=object),
        Vt          = data["Vt"],
        s           = data["s"],
        vocab_order = np.array(data["vocab_order"], dtype=object),
    )
    log.debug("LSAEmbedder: saved to %s", path)


def load(path: Path = LSA_PATH) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = np.load(str(path), allow_pickle=True)
        data = {
            "matrix":      raw["matrix"],
            "ids":         list(raw["ids"]),
            "Vt":          raw["Vt"],
            "s":           raw["s"],
            "vocab_order": list(raw["vocab_order"]),
        }
        log.debug("LSAEmbedder: loaded %d × %d from %s",
                  *data["matrix"].shape, path)
        return data
    except Exception as exc:
        log.warning("LSAEmbedder: load failed (%s)", exc)
        return None
