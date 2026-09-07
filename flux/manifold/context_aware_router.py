"""
flux/manifold/context_aware_router.py

Phase 3 router: reads query text + actual KG subgraph structure → operation type.

What makes this Phase 3 (not Phase 1 or 2):
  Phase 1: query text features → op classification
  Phase 2: query text + manifold geometry → op refinement
  Phase 3: query text + KG causal structure of activated subgraph → op resolution

The KG subgraph tells the router things no surface pattern can:
  - "heat cannot flow — what fails" → heat has causal_out=3 (ENABLES entropy, ...)
    The causal density + constraint phrasing → unambiguously PERTURB
  - "entropy and information theory" → cross-domain subgraph → GAP
  - "carnot cycle to gibbs free energy" → two named concepts, both in subgraph → GEODESIC

The router trains on the 3k pairs used for Phase 1/2, extended with subgraph features.
Because subgraph features come from the KG (not from training data patterns),
the router generalizes to any phrasing — the KG structure is query-agnostic.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

from flux.manifold.operation_classifier import (
    OPERATIONS, OP_INDEX, NGRAM_BUCKETS, WORD_BUCKETS,
    EMBED_DIM_CHAR, EMBED_DIM_WORD, STRUCT_DIM,
    char_ngram_ids, word_unigram_ids, structural_features,
)
from flux.manifold.kg_context_retriever import (
    EDGE_TYPES, retrieve, SubgraphContext,
)

log = logging.getLogger(__name__)

ROUTER_PATH  = Path("logs/flux_context_router.pt")
# 5 structural features: edge_hist[12] + domain_vec[8] + causal_density[1] + cross_domain[1] + n_primary[1]
SUBGRAPH_DIM = len(EDGE_TYPES) + 8 + 3   # 23


@dataclass
class RouterResult:
    operation:  str
    confidence: float
    source_id:  Optional[str]
    target_id:  Optional[str]
    subgraph:   Optional[SubgraphContext]


class ContextAwareRouter(nn.Module):
    """
    Phase 3 router combining query text features and KG subgraph structural features.

    Inputs:
      char_ids  [max_c]   — character n-gram hashes of query
      word_ids  [max_w]   — word unigram hashes of query
      struct    [8]       — structural floats (Phase 1 features)
      subgraph  [23]      — KG subgraph structural features (5th: n_primary)

    Output: logits [5] over OPERATIONS
    """

    def __init__(self):
        super().__init__()
        self.char_emb  = nn.EmbeddingBag(NGRAM_BUCKETS, EMBED_DIM_CHAR, mode="mean")
        self.word_emb  = nn.EmbeddingBag(WORD_BUCKETS,  EMBED_DIM_WORD,  mode="mean")

        query_dim   = EMBED_DIM_CHAR + EMBED_DIM_WORD + STRUCT_DIM   # 200
        total_dim   = query_dim + SUBGRAPH_DIM                        # 223

        self.net = nn.Sequential(
            nn.Linear(total_dim, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(0.15),
            nn.Linear(256, 128),
            nn.GELU(),
            nn.Linear(128, len(OPERATIONS)),
        )

    def forward(self, char_ids: torch.Tensor, word_ids: torch.Tensor,
                struct: torch.Tensor, subgraph_feat: torch.Tensor) -> torch.Tensor:
        q = torch.cat([
            self.char_emb(char_ids.unsqueeze(0)).squeeze(0),
            self.word_emb(word_ids.unsqueeze(0)).squeeze(0),
            struct,
            subgraph_feat,
        ])
        return self.net(q.unsqueeze(0)).squeeze(0)   # [n_ops]


def subgraph_to_feat(ctx: SubgraphContext) -> torch.Tensor:
    """Converts SubgraphContext → 23-dim feature vector for the router.

    Features:
      [0:12]  edge_hist       — normalised edge type distribution
      [12:20] domain_vec      — normalised domain distribution (top 8)
      [20]    causal_density  — mean causal out-degree / 5.0 (clamped)
      [21]    cross_domain    — 1.0 if subgraph spans multiple domains
      [22]    n_primary       — normalised count of KG-grounded concepts (/ 3.0)
                                distinguishes: 1 concept (PERTURB) vs 2+ (GAP/GEODESIC)
    """
    causal   = torch.tensor([ctx.causal_density / 5.0], dtype=torch.float).clamp(0, 1)
    cross    = torch.tensor([float(ctx.cross_domain)],  dtype=torch.float)
    n_prim   = torch.tensor([min(ctx.n_primary, 3) / 3.0], dtype=torch.float)
    return torch.cat([ctx.edge_hist, ctx.domain_vec, causal, cross, n_prim])   # [23]


# ── Inference ──────────────────────────────────────────────────────────────────

_router: Optional[ContextAwareRouter] = None

def get_router() -> Optional[ContextAwareRouter]:
    global _router
    if _router is not None:
        return _router
    if ROUTER_PATH.exists():
        m = ContextAwareRouter()
        m.load_state_dict(torch.load(ROUTER_PATH, map_location="cpu", weights_only=True))
        m.eval()
        _router = m
        log.info("Phase 3 ContextAwareRouter loaded from %s", ROUTER_PATH)
    return _router


def route(text: str, store, manifold=None, encoder=None) -> Optional[RouterResult]:
    """
    Phase 3 routing: retrieve KG subgraph → router → operation.
    Includes a semantic consistency check: GAP between same-domain concepts is
    structurally impossible — corrects to PERTURB when causal density is high.
    """
    router = get_router()
    if router is None:
        return None

    # Retrieve subgraph
    ctx = retrieve(text, store, manifold=manifold, encoder=encoder)
    if not ctx.concept_ids:
        return None

    # Encode query
    c = torch.tensor(char_ngram_ids(text),   dtype=torch.long)
    w = torch.tensor(word_unigram_ids(text),  dtype=torch.long)
    s = structural_features(text)
    f = subgraph_to_feat(ctx)

    with torch.no_grad():
        logits = router(c, w, s, f)
        probs  = torch.softmax(logits, dim=-1)
        op_idx = probs.argmax().item()

    op   = OPERATIONS[op_idx]
    conf = probs[op_idx].item()

    # ── Semantic consistency override ─────────────────────────────────────────
    # GAP between same-domain concepts is structurally impossible UNLESS it's a
    # genuine bridge/connection query with two grounded concepts (n_primary >= 2).
    #
    # Override only fires when:
    #   - Router says GAP
    #   - Subgraph is single-domain (cross_domain=False)
    #   - Only ONE primary concept was grounded (n_primary < 2)
    #     → the GAP signal came from text features alone, not from two grounded concepts
    #   - That one concept has high causal density → it's a PERTURB target
    if (op == "GAP"
            and not ctx.cross_domain
            and ctx.n_primary < 2
            and ctx.causal_density >= 2.0):
        op   = "PERTURB"
        conf = max(conf, 0.70)
        log.debug("Semantic override: GAP→PERTURB (1 concept, single-domain, causal_density=%.1f)",
                  ctx.causal_density)

    source = ctx.concept_ids[0] if ctx.concept_ids else None
    target = ctx.concept_ids[1] if len(ctx.concept_ids) > 1 else None

    return RouterResult(
        operation  = op,
        confidence = conf,
        source_id  = source,
        target_id  = target,
        subgraph   = ctx,
    )

