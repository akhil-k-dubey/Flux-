"""
flux/manifold/kg_context_retriever.py

Phase 3: Retrieves and encodes the KG subgraph activated by a query.

Two outputs:
  1. Serialized text  — for FLM cross-attention memory encoding
  2. Structural features — for ContextAwareRouter (edge types, domain distribution)

The subgraph is: KG substring-matched concepts + their immediate neighbors.
The structural features are what make Phase 3 different from Phase 1 and 2:
  Phase 1 reads query text.
  Phase 2 reads query's manifold geometry.
  Phase 3 reads the actual KG structure that the query activates.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import torch

if TYPE_CHECKING:
    from flux.manifold.causal_manifold import CausalManifold
    from flux.manifold.query_encoder import QueryEncoder
    from flux.kg.store import KGStore

log = logging.getLogger(__name__)

# All edge types in the KG (must match KGStore schema)
EDGE_TYPES = [
    "CAUSES", "ENABLES", "REQUIRES", "SPECIFIES", "CONNECTS",
    "CONTRADICTS", "ANALOGOUS_TO", "PART_OF", "IMPLEMENTS",
    "PRECEDES", "PRODUCES", "UNKNOWN",
]
EDGE_INDEX = {et: i for i, et in enumerate(EDGE_TYPES)}


@dataclass
class SubgraphContext:
    concept_ids:    list[str]
    serialized:     str          # text for FLM memory
    edge_hist:      torch.Tensor  # [12] normalized edge type counts (primary concepts only)
    domain_vec:     torch.Tensor  # [8] normalized domain distribution (primary concepts only)
    causal_density: float         # mean causal out-degree of primary concepts
    cross_domain:   bool          # spans multiple domains? (primary concepts only)
    n_concepts:     int
    n_primary:      int           # how many concepts came from KG substring match


def retrieve(query: str,
             store: "KGStore",
             manifold: Optional["CausalManifold"] = None,
             encoder:  Optional["QueryEncoder"]   = None,
             k: int = 6) -> SubgraphContext:
    """
    Returns SubgraphContext for the query.
    Requires at least one KG-grounded (substring-matched) concept to be useful.
    Returns empty context if no primary concept found — caller should fall through.
    """
    from flux.manifold.operation_classifier import find_concepts_in_query

    # Primary: KG substring match — the anchor for structural features
    primary = find_concepts_in_query(query, store)

    # Gate: if no KG substring match, return empty context — no Phase 3 routing
    if not primary:
        return _build_context([], [], store)

    # Expansion: manifold nearest neighbors for richer FLM context (NOT used for features)
    expansion: list[str] = []
    if manifold is not None and encoder is not None and manifold._built:
        try:
            q_pt = encoder.encode_text(query)
            geo  = manifold.nearest_concepts(q_pt.unsqueeze(0), k=k, store=store)
            expansion = [cid for cid, _ in geo if cid not in primary]
        except Exception:
            pass

    # 1-hop neighbors of primary (structural context, not expansion)
    neighbors: list[str] = []
    for cid in primary[:2]:
        for e in (store.edges_from(cid) or []):
            tgt = e.get("to", "")
            if tgt and tgt not in primary and tgt not in neighbors:
                neighbors.append(tgt)

    # Feature ids: primary + immediate neighbors ONLY (keeps cross_domain signal clean)
    feature_ids = list(dict.fromkeys(primary + neighbors[:2]))[:4]
    # All ids: primary + neighbors + manifold expansion (for richer FLM context)
    all_ids     = list(dict.fromkeys(primary + neighbors + expansion))[:k]

    ctx = _build_context(all_ids, feature_ids, store)
    # n_primary = query match count (1 or 2), NOT len(feature_ids).
    # This matches the injected values used during training.
    import dataclasses
    return dataclasses.replace(ctx, n_primary=len(primary))



def retrieve_for_concept(concept_id: str, store: "KGStore", k: int = 6) -> SubgraphContext:
    """Build subgraph context from a known concept id — used for training data generation."""
    neighbors = [concept_id]
    for e in (store.edges_from(concept_id) or []):
        tgt = e.get("to", "")
        if tgt and tgt not in neighbors:
            neighbors.append(tgt)
        if len(neighbors) >= k:
            break
    ids = neighbors[:k]
    return _build_context(ids, ids, store)


def _build_context(concept_ids: list[str], feature_ids: list[str],
                   store: "KGStore") -> SubgraphContext:
    """
    concept_ids  — all concepts (for FLM serialized text — richer context)
    feature_ids  — primary + immediate neighbors only (for router structural features)
    """
    # ── Structural features (from feature_ids only) ───────────────────────────
    feat_edge_counts:  list[int] = [0] * len(EDGE_TYPES)
    feat_domain_counts: dict[str, int] = {}
    feat_causal_total  = 0

    for cid in feature_ids:
        node   = store.get(cid) or {}
        domain = node.get("domain") or "unknown"
        feat_domain_counts[domain] = feat_domain_counts.get(domain, 0) + 1
        for e in (store.edges_from(cid) or [])[:6]:
            et  = (e.get("type") or "UNKNOWN").upper()
            tgt = e.get("to", "")
            if not tgt:
                continue
            idx = EDGE_INDEX.get(et, EDGE_INDEX["UNKNOWN"])
            feat_edge_counts[idx] += 1
            if et in ("CAUSES", "ENABLES", "REQUIRES"):
                feat_causal_total += 1

    fn = max(len(feature_ids), 1)
    total_feat_edges = max(sum(feat_edge_counts), 1)
    edge_hist = torch.tensor(
        [c / total_feat_edges for c in feat_edge_counts], dtype=torch.float)
    sorted_domains = sorted(feat_domain_counts.items(), key=lambda x: -x[1])
    d_vec = torch.zeros(8, dtype=torch.float)
    for i, (_, cnt) in enumerate(sorted_domains[:8]):
        d_vec[i] = cnt / fn

    # ── Serialized text (from all concept_ids — richer for FLM) ──────────────
    lines: list[str] = []
    for cid in concept_ids:
        node   = store.get(cid) or {}
        domain = node.get("domain") or "unknown"
        defn   = (node.get("definition") or "")[:80].replace("\n", " ")
        edges  = store.edges_from(cid) or []
        edge_parts = []
        for e in edges[:6]:
            et  = (e.get("type") or "UNKNOWN").upper()
            tgt = e.get("to", "")
            if tgt:
                edge_parts.append(f"{et} {tgt.replace('_', ' ')}")
        edge_str = " | ".join(edge_parts) if edge_parts else "no edges"
        lines.append(f"{cid.replace('_',' ')} [{domain}]: {defn} >> {edge_str}")

    return SubgraphContext(
        concept_ids    = concept_ids,
        serialized     = "\n".join(lines),
        edge_hist      = edge_hist,
        domain_vec     = d_vec,
        causal_density = feat_causal_total / fn,
        cross_domain   = len(feat_domain_counts) > 1,
        n_concepts     = max(len(concept_ids), 1),
        n_primary      = len(feature_ids),
    )
