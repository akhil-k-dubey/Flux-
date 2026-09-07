"""
flux/manifold/query_encoder.py

Phase 2: Maps query text directly onto the Lorentz manifold.

Phase 1 asks: "what operation does this query want?" — classification.
Phase 2 asks: "where in concept space does this query live?" — projection.

Once a query is projected to the manifold, two things become possible:
  1. Geometric operation selection: local curvature tells you what op makes sense.
     High causal density nearby → PERTURB is natural.
     Two distinct concept clusters found → GEODESIC is natural.
     Domain boundary nearby → GAP is natural.
  2. Operation mixing: run multiple ops weighted by geometric confidence.

Architecture:
  text features (200-dim from Phase 1 encoders)
  → Linear projection → R^dim (Euclidean)
  → project_to_manifold → H^dim (Lorentz point)

Training: minimize Lorentz distance between encoded query and source_concept embedding.
The source_concept embedding is frozen (from the trained GNN). Only the projection
weights are learned.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import torch
import torch.nn as nn

if TYPE_CHECKING:
    from flux.manifold.causal_manifold import CausalManifold
    from flux.kg.store import KGStore

from flux.manifold.lorentz import project_to_manifold, lorentz_distance
from flux.manifold.operation_classifier import (
    char_ngram_ids, word_unigram_ids, structural_features,
    NGRAM_BUCKETS, WORD_BUCKETS, EMBED_DIM_CHAR, EMBED_DIM_WORD, STRUCT_DIM,
    OPERATIONS, classify_operation,
)

log = logging.getLogger(__name__)

ENCODER_PATH  = "logs/flux_query_encoder.pt"
MANIFOLD_DIM  = 256   # must match trained manifold

# ── Model ──────────────────────────────────────────────────────────────────────

class QueryEncoder(nn.Module):
    """Maps query text → Lorentz manifold point."""

    def __init__(self, manifold_dim: int = MANIFOLD_DIM):
        super().__init__()
        self.char_emb  = nn.EmbeddingBag(NGRAM_BUCKETS, EMBED_DIM_CHAR, mode="mean")
        self.word_emb  = nn.EmbeddingBag(WORD_BUCKETS,  EMBED_DIM_WORD,  mode="mean")
        in_dim = EMBED_DIM_CHAR + EMBED_DIM_WORD + STRUCT_DIM
        self.proj = nn.Sequential(
            nn.Linear(in_dim, manifold_dim * 2),
            nn.LayerNorm(manifold_dim * 2),
            nn.GELU(),
            nn.Linear(manifold_dim * 2, manifold_dim),
        )
        self.manifold_dim = manifold_dim

    def forward(self, char_ids: torch.Tensor, word_ids: torch.Tensor,
                struct: torch.Tensor) -> torch.Tensor:
        """Returns [manifold_dim + 1] Lorentz point."""
        h = torch.cat([
            self.char_emb(char_ids.unsqueeze(0)),
            self.word_emb(word_ids.unsqueeze(0)),
            struct.unsqueeze(0),
        ], dim=-1)                                  # [1, in_dim]
        spatial = self.proj(h)                      # [1, manifold_dim]
        padded  = torch.cat([
            torch.zeros(1, 1, device=h.device), spatial
        ], dim=-1)                                  # [1, manifold_dim+1]
        return project_to_manifold(padded, kappa=1.0).squeeze(0)   # [manifold_dim+1]

    def encode_text(self, text: str) -> torch.Tensor:
        c = torch.tensor(char_ngram_ids(text),  dtype=torch.long)
        w = torch.tensor(word_unigram_ids(text), dtype=torch.long)
        s = structural_features(text)
        with torch.no_grad():
            return self.forward(c, w, s)

# ── Geometric operation selector ───────────────────────────────────────────────

@dataclass
class GeometricSignal:
    operation:   str
    confidence:  float
    source_id:   Optional[str]
    target_id:   Optional[str]
    notes:       str

def geometric_op_select(query_point: torch.Tensor,
                         manifold: "CausalManifold",
                         store: "KGStore",
                         k: int = 5) -> GeometricSignal:
    """
    Given a query's manifold location, use local geometry to determine
    what operation makes sense — without any text classification.

    Rules (pure geometry):
      - If k nearest concepts span two distinct domains → GAP signal
      - If nearest concept has many causal successors → PERTURB signal
      - If two concepts found AND they are far apart → GEODESIC signal
      - Otherwise → POINT_QUERY
    """
    nearest = manifold.nearest_concepts(query_point.unsqueeze(0), k=k, store=store)
    if not nearest:
        return GeometricSignal("POINT_QUERY", 0.3, None, None, "no nearby concepts")

    ids   = [cid for cid, _ in nearest]
    dists = [d   for _, d  in nearest]

    # Domain diversity — how many distinct domains in the k nearest?
    domains = set()
    for cid in ids:
        d = (store.get(cid) or {}).get("domain")
        if d:
            domains.add(d)
    domain_diversity = len(domains) / max(k, 1)

    # Causal density — does the top concept have many causal successors?
    top_id = ids[0]
    causal_out = sum(
        1 for e in (store.edges_from(top_id) or [])
        if e.get("type") in ("CAUSES", "ENABLES", "REQUIRES")
    )

    # Distance spread — are concepts clustered or spread?
    if len(dists) >= 2:
        dist_spread = max(dists) - min(dists)
    else:
        dist_spread = 0.0

    # Decision rules (order matters)
    if domain_diversity >= 0.5 and len(domains) >= 2:
        # Query lands between domain clusters → GAP
        domain_list = list(domains)
        src_id = next((c for c in ids if (store.get(c) or {}).get("domain") == domain_list[0]), ids[0])
        tgt_id = next((c for c in ids if (store.get(c) or {}).get("domain") == domain_list[1]), None)
        return GeometricSignal("GAP", domain_diversity, src_id, tgt_id,
                               f"domains={domains}, spread={dist_spread:.2f}")

    if causal_out >= 2:
        # High causal out-degree near query → perturbation is meaningful
        return GeometricSignal("PERTURB", min(causal_out / 5.0, 1.0), top_id, None,
                               f"causal_out={causal_out}")

    if dist_spread >= 3.0 and len(ids) >= 2:
        # Two distant concepts both near query → geodesic between them
        return GeometricSignal("GEODESIC", min(dist_spread / 6.0, 1.0),
                               ids[0], ids[1],
                               f"spread={dist_spread:.2f}")

    return GeometricSignal("POINT_QUERY", 0.7, top_id, None,
                           f"default, causal_out={causal_out}")

# ── Phase 2 result ─────────────────────────────────────────────────────────────

@dataclass
class Phase2Result:
    final_operation: str
    source_id:  Optional[str]
    target_id:  Optional[str]
    direction:  float
    domain_a:   Optional[str]
    domain_b:   Optional[str]
    phase1_op:  str
    geo_op:     str
    confidence: float

def resolve(text: str, manifold: "CausalManifold", store: "KGStore",
            encoder: QueryEncoder) -> Optional[Phase2Result]:
    """
    Phase 2 resolution — two cleanly separated concerns:

    1. OPERATION TYPE: combine Phase 1 classifier + geometric manifold signal.
       Phase 2's contribution is purely the operation selection.

    2. CONCEPT GROUNDING: always KG substring match (Phase 1).
       Reliable from day 1. Geometric nearest-neighbors only as fallback
       when substring match finds nothing.
    """
    if not manifold._built:
        return None

    # ── Operation selection ────────────────────────────────────────────────────
    p1      = classify_operation(text)
    p1_op   = p1.operation
    p1_conf = p1.confidence

    q_point  = encoder.encode_text(text)
    geo      = geometric_op_select(q_point, manifold, store)
    geo_op   = geo.operation
    geo_conf = geo.confidence

    if p1_op == geo_op:
        final_op = p1_op
        conf     = min((p1_conf + geo_conf) / 2 + 0.15, 1.0)
    elif p1_conf >= geo_conf:
        final_op = p1_op
        conf     = p1_conf * 0.8
    else:
        final_op = geo_op
        conf     = geo_conf * 0.8

    # ── Concept grounding — KG substring match, not geometric NN ──────────────
    from flux.manifold.operation_classifier import find_concepts_in_query, detect_direction
    concepts = find_concepts_in_query(text, store)
    concepts = [c for c in concepts if c in manifold._concept_index]

    source = concepts[0] if len(concepts) > 0 else None
    target = concepts[1] if len(concepts) > 1 else None

    # Geometric NN as last resort if substring match found nothing
    if source is None and geo.source_id and geo.source_id in manifold._concept_index:
        source = geo.source_id
        target = geo.target_id

    if source is None:
        return None

    direction = detect_direction(text) if final_op == "PERTURB" else -1.0
    domain_a = domain_b = None
    if final_op == "GAP":
        domain_a = (store.get(source) or {}).get("domain") or source
        domain_b = (store.get(target) or {}).get("domain") or target if target else None

    return Phase2Result(
        final_operation = final_op,
        source_id       = source,
        target_id       = target,
        direction       = direction,
        domain_a        = domain_a,
        domain_b        = domain_b,
        phase1_op       = p1_op,
        geo_op          = geo_op,
        confidence      = conf,
    )



# ── Singleton loader ───────────────────────────────────────────────────────────

_query_encoder: Optional[QueryEncoder] = None

def get_query_encoder() -> Optional[QueryEncoder]:
    global _query_encoder
    if _query_encoder is not None:
        return _query_encoder
    from pathlib import Path
    p = Path(ENCODER_PATH)
    if p.exists():
        m = QueryEncoder()
        m.load_state_dict(torch.load(p, map_location="cpu", weights_only=True))
        m.eval()
        _query_encoder = m
        log.info("QueryEncoder loaded from %s", p)
    return _query_encoder
