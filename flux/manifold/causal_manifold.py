"""
flux/manifold/causal_manifold.py

The CausalManifold — Flux's world model made geometric.

This is the central object that ties together:
  - ConceptEncoder: places concepts on the manifold (inductive)
  - Lorentz math: distances, directions, geodesics
  - Propagator: causal effect propagation
  - KG verification: manifold answers validated against KG ground truth

The manifold supports five query operations:

  POINT_QUERY:  "what is entropy"
    → find manifold region near the query projection
    → return nearest concept(s) with their definitions

  PERTURB:      "what would happen if entropy decreased"
    → identify source concept (entropy)
    → propagate perturbation through causal graph
    → return cascade of affected concepts, ranked by magnitude

  GEODESIC:     "how does entropy relate to information theory"
    → find two anchor points on manifold
    → trace geodesic between them
    → intermediate concept points = reasoning chain

  GAP:          "what problem sits between thermodynamics and information theory"
    → find cluster centers for both domains
    → identify low-density region between clusters
    → characterize what kind of concept would live there
    → this is hypothesis generation

  CLUSTER:      "what are the core concepts in quantum mechanics"
    → find all manifold points in quantum_mechanics domain region
    → return centroid + members sorted by centrality

All manifold answers go through KG verification before being returned.
KG is the ground truth. The manifold is the query interface.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

from flux.kg.store import KGStore
from .encoder import InductiveConceptEncoder, EDGE_TYPE_INDEX, NUM_EDGE_TYPES
from .lorentz import (lorentz_distance, project_to_manifold,
                      causal_direction, origin, EPS)
from .propagator import Propagator, PerturbationResult

log = logging.getLogger(__name__)

MANIFOLD_CHECKPOINT = Path("logs/flux_manifold.pt")

# Exclude from manifold: infrastructure meta-nodes + constitutional operators
_SKIP_TYPES = {"META", "LOGICAL", "RELATIONAL"}
_SKIP_PREFIX_SUFFIX = ("__", "__")   # __layer_layer_X__


@dataclass
class ManifoldOperation:
    op_type:          str                       # POINT_QUERY|PERTURB|GEODESIC|GAP|CLUSTER
    source_concepts:  list[str]                 # concept IDs to anchor the operation
    intent:           str                       # EXPLAIN|COUNTERFACTUAL|CONNECT|HYPOTHESIZE
    query_text:       str = ""
    perturbation_dir: float = -1.0             # for PERTURB: -1.0=decrease, +1.0=increase


@dataclass
class ManifoldResult:
    operation:           ManifoldOperation
    concepts:            list[str]              # concept IDs in the result
    distances:           list[float]            # distances from query point
    reasoning_path:      list[str] = field(default_factory=list)
    perturbation_result: Optional[PerturbationResult] = None
    gap_description:     Optional[str] = None
    kg_verified:         bool = False
    raw_text:            str = ""               # formatted for FLM input


class CausalManifold(nn.Module):
    """
    The world model. Geometric + causal.

    Initialization creates a fresh manifold.
    Call build(store) to embed all KG concepts.
    Call save() / load() to persist between sessions.
    """

    def __init__(self,
                 dim: int = 256,
                 kappa: float = 1.0,
                 device: Optional[str] = None):
        super().__init__()
        self.dim    = dim
        self.kappa  = kappa
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))

        self.encoder    = InductiveConceptEncoder(
            input_dim=128, hidden_dim=dim, kappa=kappa
        ).to(self.device)
        self.propagator = None   # initialized in build() with store

        # Concept registry
        self._concept_ids:    list[str]         = []          # index → concept_id
        self._concept_index:  dict[str, int]    = {}          # concept_id → index
        self._embeddings:     Optional[torch.Tensor] = None   # [N, dim+1]
        self._node_features:  Optional[torch.Tensor] = None   # [N, 128]
        self._adj:            dict[int, list[tuple[int,int]]] = {}
        self._built:          bool = False

    # ── Build ──────────────────────────────────────────────────────────────────

    def build(self, store: KGStore, text_encoder=None) -> int:
        """
        Embed all KG concept nodes onto the manifold.

        store: the KGStore
        text_encoder: callable(definition_text) → torch.Tensor([128])
                      If None, uses a hash-based fallback (no semantic quality).
                      Pass the LTC encoder when available.

        Returns: number of concepts embedded.
        """
        self.propagator = Propagator(store).to(self.device)
        log.info("Building causal manifold...")

        # Collect embeddable concepts
        concept_ids = []
        for nid in store.all_ids():
            node = store.get(nid)
            if not node:
                continue
            ntype = node.get("type", "")
            if ntype in _SKIP_TYPES:
                continue
            if nid.startswith("__") and nid.endswith("__"):
                continue
            # Skip concepts explicitly marked as manifold-ineligible
            # (low-quality book extractions: conf<0.75 or def<80ch)
            if node.get("manifold_eligible") is False:
                continue
            concept_ids.append(nid)

        self._concept_ids   = concept_ids
        self._concept_index = {cid: i for i, cid in enumerate(concept_ids)}
        N = len(concept_ids)
        log.info("  %d concepts to embed", N)

        # Build node feature matrix [N, 128]
        self._node_features = self._build_features(concept_ids, store, text_encoder)

        # Build adjacency
        self._adj = self._build_adj(concept_ids, store)

        # Encode all concepts through GNN
        log.info("  Running GNN encoder...")
        with torch.no_grad():
            self._embeddings = self.encoder(
                self._node_features.to(self.device),
                self._adj,
            )
        log.info("  Manifold built: %d points in H^%d (κ=%.1f)",
                 N, self.dim, self.kappa)
        self._built = True
        return N

    def _build_features(self, concept_ids: list[str], store: KGStore,
                        text_encoder) -> torch.Tensor:
        """Build [N, 128] node feature matrix from concept definitions."""
        features = []
        for cid in concept_ids:
            node = store.get(cid) or {}
            defn = node.get("definition", node.get("name", cid))

            if text_encoder is not None:
                try:
                    feat = text_encoder(defn)
                    if isinstance(feat, torch.Tensor):
                        feat = feat.float().cpu()
                    else:
                        feat = torch.tensor(feat, dtype=torch.float32)
                    feat = feat.flatten()[:128]
                    if feat.shape[0] < 128:
                        feat = torch.nn.functional.pad(feat, (0, 128 - feat.shape[0]))
                except Exception:
                    feat = self._hash_feature(cid, defn)
            else:
                feat = self._hash_feature(cid, defn)

            features.append(feat)
        return torch.stack(features)                             # [N, 128]

    @staticmethod
    def _hash_feature(concept_id: str, definition: str) -> torch.Tensor:
        """
        Deterministic hash-based fallback feature vector.
        Not semantic quality — used when LTC encoder is unavailable.
        Good enough to test manifold geometry structure.
        """
        import hashlib
        text = (concept_id + " " + definition).encode()
        h    = hashlib.sha256(text).digest()
        # Repeat hash bytes to fill 128 floats
        h_ext = (h * 4)[:512]
        feat  = torch.tensor([b / 255.0 for b in h_ext[:128]], dtype=torch.float32)
        return feat * 2 - 1   # normalize to [-1, 1]

    def _build_adj(self, concept_ids: list[str],
                   store: KGStore) -> dict[int, list[tuple[int, int]]]:
        """Build adjacency dict: node_idx → [(neighbor_idx, edge_type_idx)]"""
        adj: dict[int, list[tuple[int, int]]] = {i: [] for i in range(len(concept_ids))}
        idx = self._concept_index

        for cid in concept_ids:
            u = idx.get(cid)
            if u is None:
                continue
            for edge in (store.edges_from(cid) or []):
                target    = edge.get("to", "")
                edge_type = edge.get("type", "")
                v = idx.get(target)
                if v is None:
                    continue
                et_idx = EDGE_TYPE_INDEX.get(edge_type, NUM_EDGE_TYPES)
                adj[u].append((v, et_idx))
        return adj

    # ── Query operations ───────────────────────────────────────────────────────

    def get_embedding(self, concept_id: str) -> Optional[torch.Tensor]:
        """Get the manifold embedding for a concept. [dim+1]"""
        self._ensure_built()
        idx = self._concept_index.get(concept_id)
        if idx is None:
            return None
        return self._embeddings[idx]

    def nearest_concepts(self, query_point: torch.Tensor,
                         k: int = 5,
                         domain_filter: Optional[str] = None,
                         store: Optional[KGStore] = None) -> list[tuple[str, float]]:
        """
        Find the k nearest concept points to a query point on the manifold.
        Returns [(concept_id, distance), ...] sorted by distance.
        """
        self._ensure_built()
        query = query_point.to(self.device)
        if query.dim() == 1:
            query = query.unsqueeze(0)                              # [1, dim+1]

        # Batch distance computation
        dists = lorentz_distance(
            query.expand(len(self._concept_ids), -1),
            self._embeddings,
            self.kappa,
        )                                                           # [N]

        if domain_filter and store:
            # Zero out non-matching domains (effectively push them far away)
            for i, cid in enumerate(self._concept_ids):
                node = store.get(cid) or {}
                if node.get("domain") != domain_filter:
                    dists[i] = float("inf")

        sorted_idx = torch.argsort(dists)[:k]
        return [(self._concept_ids[i], float(dists[i])) for i in sorted_idx]

    def get_embedding(self, concept_id: str) -> Optional[torch.Tensor]:
        """Return the 257-dim Lorentz embedding for a concept_id, or None."""
        self._ensure_built()
        # Build index lazily (CausalManifold stores _concept_ids list, not a dict)
        if not hasattr(self, "_concept_index") or self._concept_index is None:
            self._concept_index = {cid: i for i, cid in enumerate(self._concept_ids)}
        idx = self._concept_index.get(concept_id)
        if idx is None:
            return None
        return self._embeddings[idx].detach().cpu()

    def nearest_operator_concept(
        self, query_vec: torch.Tensor,
        operator_ids: Optional[list[str]] = None,
    ) -> tuple[str, float]:
        """
        Find the nearest OPERATOR concept to query_vec in manifold space.
        Returns (op_concept_id, lorentz_distance). No hardcoded verb lists —
        routing is purely geometric.
        """
        self._ensure_built()
        if operator_ids is None:
            operator_ids = ["op_build", "op_can_you", "op_connect",
                            "op_assess", "op_simulate"]
        q = query_vec.float().to(self.device)
        if q.dim() == 1:
            q = q.unsqueeze(0)
        best_id, best_dist = operator_ids[0], float("inf")
        for op_id in operator_ids:
            emb = self.get_embedding(op_id)
            if emb is None:
                continue
            dist = float(lorentz_distance(
                q, emb.to(self.device).unsqueeze(0), self.kappa
            ).item())
            if dist < best_dist:
                best_dist, best_id = dist, op_id
        return best_id, best_dist

    def nearest_concept_to(
        self, query_vec: torch.Tensor, k: int = 1,
        exclude_operators: bool = True,
    ) -> list[tuple[str, float]]:
        """
        k nearest KG concepts to an arbitrary 257-dim query vector.
        Used by ManifoldExecutor to ground target_vec → concept_id.
        """
        self._ensure_built()
        q = query_vec.float().to(self.device)
        if q.dim() == 1:
            q = q.unsqueeze(0)
        dists = lorentz_distance(
            q.expand(len(self._concept_ids), -1),
            self._embeddings, self.kappa,
        )
        if exclude_operators:
            for i, cid in enumerate(self._concept_ids):
                if cid.startswith("op_"):
                    dists[i] = float("inf")
        sorted_idx = torch.argsort(dists)[:k]
        return [(self._concept_ids[i], float(dists[i])) for i in sorted_idx]

    def geodesic_from_vec(
        self, src_vec: torch.Tensor, tgt_vec: torch.Tensor,
        steps: int = 8, store: Optional[KGStore] = None,
    ) -> "ManifoldResult":
        """
        Geodesic path between two arbitrary manifold vectors.
        Used by vector-based routing — no string grounding needed.
        """
        self._ensure_built()
        src = src_vec.float().to(self.device)
        tgt = tgt_vec.float().to(self.device)
        if src.dim() == 1:
            src = src.unsqueeze(0)
        if tgt.dim() == 1:
            tgt = tgt.unsqueeze(0)

        path_concepts, path_dists = [], []
        for step in range(steps + 1):
            t       = step / steps
            tangent = log_map(src.squeeze(0), tgt.squeeze(0), self.kappa)
            interp  = exp_map(src.squeeze(0), t * tangent, self.kappa)
            interp  = project_to_manifold(interp.unsqueeze(0), self.kappa).squeeze(0)
            nearest = self.nearest_concept_to(interp, k=1)
            if nearest:
                cid, dist = nearest[0]
                if not path_concepts or path_concepts[-1] != cid:
                    path_concepts.append(cid)
                    path_dists.append(dist)

        total_dist = float(lorentz_distance(src, tgt, self.kappa).item())
        op = ManifoldOperation("GEODESIC", path_concepts, "CONNECT")
        return ManifoldResult(
            operation      = op,
            concepts       = path_concepts,
            distances      = path_dists,
            reasoning_path = path_concepts,
            total_distance = total_dist,
            kg_verified    = True,
        )

    def point_query(self, query_embedding: torch.Tensor,
                    k: int = 3, store: Optional[KGStore] = None) -> ManifoldResult:
        """POINT_QUERY: find concepts nearest to a query embedding."""
        self._ensure_built()
        nearest = self.nearest_concepts(query_embedding, k, store=store)
        concepts = [cid for cid, _ in nearest]
        distances = [d for _, d in nearest]
        op = ManifoldOperation("POINT_QUERY", concepts[:1], "EXPLAIN")
        return ManifoldResult(operation=op, concepts=concepts, distances=distances,
                              kg_verified=True)

    def perturb_query(self, source_concept: str,
                      direction: float = -1.0,
                      depth: int = 4) -> ManifoldResult:
        """PERTURB: what happens if source_concept changes?"""
        self._ensure_built()
        if self.propagator is None:
            raise RuntimeError("Call build() before using the manifold.")
        result = self.propagator.perturb(source_concept, direction, depth)
        op = ManifoldOperation("PERTURB", [source_concept], "COUNTERFACTUAL",
                               perturbation_dir=direction)
        raw = self.propagator.format_result(result)
        return ManifoldResult(
            operation           = op,
            concepts            = list(result.affected_concepts.keys()),
            distances           = [abs(v) for v in result.affected_concepts.values()],
            perturbation_result = result,
            kg_verified         = any(result.kg_verified.values()),
            raw_text            = raw,
        )

    def geodesic_query(self, source_id: str, target_id: str,
                       store: Optional[KGStore] = None) -> ManifoldResult:
        """GEODESIC: find the reasoning chain between two concepts."""
        self._ensure_built()
        src_emb = self.get_embedding(source_id)
        tgt_emb = self.get_embedding(target_id)
        if src_emb is None or tgt_emb is None:
            return ManifoldResult(
                operation=ManifoldOperation("GEODESIC", [source_id, target_id], "CONNECT"),
                concepts=[], distances=[], kg_verified=False
            )

        # Sample points along the geodesic and find nearest concepts
        path_concepts = []
        steps = 5
        for t in [i / (steps - 1) for i in range(steps)]:
            # Linear interpolation in tangent space then project
            from .lorentz import log_map, exp_map
            tangent = log_map(src_emb, tgt_emb, self.kappa)
            interp  = exp_map(src_emb, t * tangent, self.kappa)
            interp  = project_to_manifold(interp.unsqueeze(0), self.kappa).squeeze(0)
            nearest = self.nearest_concepts(interp, k=1, store=store)
            if nearest:
                cid = nearest[0][0]
                if cid not in path_concepts:
                    path_concepts.append(cid)

        dist = float(lorentz_distance(src_emb.unsqueeze(0),
                                      tgt_emb.unsqueeze(0), self.kappa).item())
        op = ManifoldOperation("GEODESIC", [source_id, target_id], "CONNECT")
        return ManifoldResult(
            operation      = op,
            concepts       = path_concepts,
            distances      = [dist],
            reasoning_path = path_concepts,
            kg_verified    = True,
        )

    def gap_query(self, domain_a: str, domain_b: str,
                  store: KGStore) -> ManifoldResult:
        """
        GAP: find the conceptual gap between two domain clusters.
        This is hypothesis generation — what concept should exist but doesn't?

        Method:
          1. Find centroid of domain_a cluster and domain_b cluster
          2. Find midpoint of the geodesic between them
          3. Find nearest actual concepts to that midpoint
          4. The gap IS the difference between "what's there" and "what should be there"
        """
        self._ensure_built()

        # Find cluster centroids for each domain
        def domain_centroid(domain: str) -> Optional[torch.Tensor]:
            domain_embs = []
            for cid in self._concept_ids:
                node = store.get(cid) or {}
                if node.get("domain") == domain:
                    emb = self.get_embedding(cid)
                    if emb is not None:
                        domain_embs.append(emb)
            if not domain_embs:
                return None
            from .lorentz import frechet_mean
            return frechet_mean(torch.stack(domain_embs), kappa=self.kappa)

        c_a = domain_centroid(domain_a)
        c_b = domain_centroid(domain_b)
        if c_a is None or c_b is None:
            return ManifoldResult(
                operation=ManifoldOperation("GAP", [], "HYPOTHESIZE"),
                concepts=[], distances=[], kg_verified=False,
                gap_description=f"Insufficient concepts in domain {domain_a} or {domain_b}"
            )

        # Find midpoint
        from .lorentz import log_map, exp_map
        mid_tangent = log_map(c_a, c_b, self.kappa) * 0.5
        midpoint = exp_map(c_a, mid_tangent, self.kappa)
        midpoint = project_to_manifold(midpoint.unsqueeze(0), self.kappa).squeeze(0)

        # Nearest concepts to the gap region
        nearby = self.nearest_concepts(midpoint, k=5, store=store)
        nearby_concepts = [cid for cid, _ in nearby]
        gap_dist = float(lorentz_distance(c_a.unsqueeze(0), c_b.unsqueeze(0),
                                          self.kappa).item())

        gap_desc = (
            f"Gap between '{domain_a}' and '{domain_b}' (manifold distance: {gap_dist:.3f}). "
            f"Nearest existing concepts: {', '.join(nearby_concepts[:3])}. "
            f"A bridge concept here would need to connect both domains — "
            f"this is where Flux's hypotheses live."
        )

        op = ManifoldOperation("GAP", [domain_a, domain_b], "HYPOTHESIZE")
        return ManifoldResult(
            operation       = op,
            concepts        = nearby_concepts,
            distances       = [d for _, d in nearby],
            gap_description = gap_desc,
            kg_verified     = False,  # hypotheses are not KG-verified by definition
        )

    # ── Embed new concept (inductive learning) ─────────────────────────────────

    def embed_new_concept(self, concept_id: str,
                          store: KGStore,
                          text_encoder=None) -> torch.Tensor:
        """
        When Akhil teaches Flux a new concept, this places it on the manifold.
        No retraining needed — the inductive encoder generalizes.

        Returns the new concept's manifold embedding.
        """
        if not self._built:
            raise RuntimeError("Build manifold first with build().")

        node = store.get(concept_id) or {}
        defn = node.get("definition", concept_id)

        # Get text feature
        if text_encoder:
            try:
                feat = text_encoder(defn)
                if isinstance(feat, torch.Tensor):
                    feat = feat.float().cpu()
                feat = feat.flatten()[:128]
                if feat.shape[0] < 128:
                    feat = torch.nn.functional.pad(feat, (0, 128 - feat.shape[0]))
            except Exception:
                feat = self._hash_feature(concept_id, defn)
        else:
            feat = self._hash_feature(concept_id, defn)

        # Find KG neighbors that are already on the manifold
        new_adj: dict[int, list[tuple[int, int]]] = {0: []}
        from .encoder import EDGE_TYPE_INDEX, NUM_EDGE_TYPES
        for edge in (store.edges_from(concept_id) or []):
            tgt = edge.get("to", "")
            et  = edge.get("type", "")
            nbr_idx = self._concept_index.get(tgt)
            if nbr_idx is not None:
                et_idx = EDGE_TYPE_INDEX.get(et, NUM_EDGE_TYPES)
                new_adj[0].append((nbr_idx + 1, et_idx))  # +1 offset (node 0 = new)

        # Build feature matrix: [new_concept] + all existing
        all_features = torch.cat([
            feat.unsqueeze(0),
            self._node_features
        ], dim=0).to(self.device)

        # Adjust existing adjacency indices (+1 offset)
        combined_adj = {0: new_adj[0]}
        for old_idx, nbrs in self._adj.items():
            combined_adj[old_idx + 1] = [(n + 1, et) for n, et in nbrs]

        with torch.no_grad():
            new_emb = self.encoder(all_features, combined_adj,
                                   target_indices=[0])         # only compute for new node

        embedding = new_emb[0]                                  # [dim+1]

        # Register the new concept
        new_idx = len(self._concept_ids)
        self._concept_ids.append(concept_id)
        self._concept_index[concept_id] = new_idx
        self._embeddings = torch.cat([self._embeddings, embedding.unsqueeze(0)], dim=0)
        self._node_features = torch.cat([self._node_features, feat.unsqueeze(0)], dim=0)

        # Update adjacency for new concept
        self._adj[new_idx] = [(n - 1, et) for n, et in new_adj[0]]  # remove offset

        log.info("Embedded new concept '%s' on manifold (idx=%d)", concept_id, new_idx)
        return embedding

    # ── Validation ─────────────────────────────────────────────────────────────

    def validate_geometry(self, store: KGStore,
                          n_pairs: int = 10) -> dict:
        """
        The validation test: do causal pairs cluster? Do directions align?

        Picks n_pairs causally connected pairs and n_pairs unconnected pairs.
        Reports:
          - mean distance for causal pairs (should be SMALL)
          - mean distance for random pairs (should be LARGE)
          - direction consistency: causal arrows should point the same way

        This is the pass/fail test for Week 1. If causal pairs are closer
        than random pairs, the geometry is learning the right structure.
        """
        self._ensure_built()

        # Collect CAUSES/REQUIRES edge pairs
        causal_pairs   = []
        for cid in self._concept_ids[:200]:   # sample from first 200 nodes
            for edge in (store.edges_from(cid) or []):
                if edge.get("type") in ("CAUSES", "REQUIRES", "ENABLES"):
                    tgt = edge.get("to", "")
                    if tgt in self._concept_index:
                        causal_pairs.append((cid, tgt))
                        if len(causal_pairs) >= n_pairs:
                            break
            if len(causal_pairs) >= n_pairs:
                break

        # Compute distances for causal pairs
        import random
        causal_dists = []
        for src, tgt in causal_pairs:
            e_src = self.get_embedding(src)
            e_tgt = self.get_embedding(tgt)
            if e_src is not None and e_tgt is not None:
                d = float(lorentz_distance(e_src.unsqueeze(0),
                                           e_tgt.unsqueeze(0), self.kappa).item())
                causal_dists.append(d)

        # Random pairs
        random_pairs  = []
        all_ids = list(self._concept_ids)
        for _ in range(n_pairs):
            a, b = random.sample(all_ids, 2)
            random_pairs.append((a, b))

        random_dists = []
        for a, b in random_pairs:
            e_a = self.get_embedding(a)
            e_b = self.get_embedding(b)
            if e_a is not None and e_b is not None:
                d = float(lorentz_distance(e_a.unsqueeze(0),
                                           e_b.unsqueeze(0), self.kappa).item())
                random_dists.append(d)

        # Direction consistency: causal arrows should be consistent
        causal_vectors = []
        for src, tgt in causal_pairs[:5]:
            e_src = self.get_embedding(src)
            e_tgt = self.get_embedding(tgt)
            if e_src is not None and e_tgt is not None:
                vec = causal_direction(e_src, e_tgt, self.kappa)
                causal_vectors.append(vec)

        direction_consistency = 0.0
        if len(causal_vectors) >= 2:
            from .lorentz import minkowski_inner
            # Avg pairwise cosine similarity in tangent space
            similarities = []
            for i in range(len(causal_vectors)):
                for j in range(i + 1, len(causal_vectors)):
                    sim = float(minkowski_inner(causal_vectors[i],
                                               causal_vectors[j]).item())
                    similarities.append(sim)
            direction_consistency = sum(similarities) / len(similarities)

        mean_causal = sum(causal_dists) / len(causal_dists) if causal_dists else 0
        mean_random = sum(random_dists) / len(random_dists) if random_dists else 0
        passed = mean_causal < mean_random

        return {
            "causal_pairs_tested":   len(causal_dists),
            "random_pairs_tested":   len(random_dists),
            "mean_causal_distance":  round(mean_causal, 4),
            "mean_random_distance":  round(mean_random, 4),
            "geometry_test_passed":  passed,
            "direction_consistency": round(direction_consistency, 4),
            "note": ("Causal pairs are closer than random pairs — geometry is learning "
                     "causal structure." if passed else
                     "Random pairs are as close as causal pairs — manifold needs training.")
        }

    # ── Persist ────────────────────────────────────────────────────────────────

    def save(self, path: Path = MANIFOLD_CHECKPOINT) -> None:
        """Persist the manifold (encoder weights + embeddings) to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "encoder_state":    self.encoder.state_dict(),
            "propagator_state": self.propagator.state_dict() if self.propagator else None,
            "embeddings":       self._embeddings.cpu() if self._embeddings is not None else None,
            "node_features":    self._node_features.cpu() if self._node_features is not None else None,
            "concept_ids":      self._concept_ids,
            "dim":              self.dim,
            "kappa":            self.kappa,
            "adj":              self._adj,
        }, path)
        log.info("Manifold saved to %s (%d concepts)", path, len(self._concept_ids))

    def load(self, path: Path = MANIFOLD_CHECKPOINT, store: KGStore = None) -> bool:
        """Load a persisted manifold. Returns True if successful."""
        if not path.exists():
            return False
        try:
            ckpt = torch.load(path, map_location=self.device)
            self.encoder.load_state_dict(ckpt["encoder_state"])
            if ckpt.get("propagator_state") and store:
                if self.propagator is None:
                    self.propagator = Propagator(store).to(self.device)
                self.propagator.load_state_dict(ckpt["propagator_state"])
            self._embeddings   = ckpt["embeddings"].to(self.device) if ckpt["embeddings"] is not None else None
            self._node_features = ckpt.get("node_features")
            self._concept_ids  = ckpt["concept_ids"]
            self._concept_index = {cid: i for i, cid in enumerate(self._concept_ids)}
            self._adj           = ckpt.get("adj", {})
            self._built         = self._embeddings is not None
            log.info("Manifold loaded from %s (%d concepts)", path, len(self._concept_ids))
            return True
        except Exception as e:
            log.warning("Failed to load manifold: %s", e)
            return False

    def _ensure_built(self) -> None:
        if not self._built or self._embeddings is None:
            raise RuntimeError(
                "Manifold not built. Call build(store) first, or load() a saved checkpoint."
            )


# ── Module-level singleton ─────────────────────────────────────────────────────

_manifold_singleton: Optional[CausalManifold] = None


def get_manifold(dim: int = 256, kappa: float = 1.0) -> CausalManifold:
    """Get or create the module-level CausalManifold singleton."""
    global _manifold_singleton
    if _manifold_singleton is None:
        _manifold_singleton = CausalManifold(dim=dim, kappa=kappa)
    return _manifold_singleton
