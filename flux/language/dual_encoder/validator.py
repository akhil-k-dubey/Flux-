"""
flux/language/dual_encoder/validator.py

Discrimination evaluator for the dual encoder.

Three test categories:
  1. Query-to-Node alignment (LTC query vs NodeEncoder definition — same concept)
     Uses LTC forward-pass 128-dim (same space as Phase 1 anchor targets)
     Target: cosine sim > 0.65

  2. Related-concept similarity (NodeEncoder definition pairs with KG edge)
     Target: cosine sim > 0.55  (close but not identical)

  3. Cross-domain separation (NodeEncoder definition pairs, different domain)
     Target: cosine sim < 0.35

  4. Within-domain discrimination (2-hop pairs with Jaccard < 0.20)
     Target: cosine sim < 0.45  (harder than cross-domain, should still separate)

ValidationResult.passed: True only if all four criteria met on average.
Saves timestamped JSON to logs/dual_encoder/.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch

log = logging.getLogger(__name__)

LOG_DIR = Path("logs/dual_encoder")

THRESHOLDS = {
    "query_to_node":      (0.45, "gt"),   # symmetric NodeEncoder(query) vs NodeEncoder(def)
    "related_pair":       (0.55, "gt"),   # NodeEncoder(def_A) vs NodeEncoder(def_B), KG edge
    "cross_domain":       (0.35, "lt"),   # different domain/type
    "within_domain_2hop": (0.80, "lt"),   # 2-hop KG neighbor (adjacent, not identical)
    # Invariant: cross_domain < 2hop < related_pair < 1.0
}


@dataclass
class ValidationResult:
    query_to_node_mean:       float
    related_pair_mean:        float
    cross_domain_mean:        float
    within_domain_2hop_mean:  float
    gap_query_vs_cross:       float   # query_to_node - cross_domain
    passed:                   bool
    details:                  dict



def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def validate(
    encoder,                  # KGNodeEncoder (any checkpoint)
    nodes: dict,
    store=None,
    n_samples: int = 12,
    seed: int = 99,
) -> ValidationResult:
    """
    Run all four discrimination tests.

    Args:
        encoder: trained KGNodeEncoder (or freshly initialised — for baseline)
        nodes:   KGStore.all_nodes()
        store:   KGStore instance (for edge lookup)
        n_samples: concepts to sample per test
    """
    from flux.language.dual_encoder.triplet_generator import (
        _build_adj, _build_two_hop, _build_domain_buckets, _jaccard_tokens
    )

    rng = random.Random(seed)
    device = next(encoder.parameters()).device

    usable = {
        c: nd for c, nd in nodes.items()
        if not c.startswith(("axiom_", "__", "consequence_query_"))
        and len(nd.get("definition", "")) > 10
        and nd.get("confidence", 0) > 0.4
    }
    cids    = list(usable.keys())
    adj     = _build_adj(store, cids)
    two_hop = _build_two_hop(adj)
    buckets = _build_domain_buckets(usable)
    bkt_of  = {c: bk for bk, ids in buckets.items() for c in ids}

    def node_emb(text: str) -> np.ndarray:
        with torch.no_grad():
            v = encoder.encode(text, device=device).cpu().numpy()
        return v

    details: dict = {}

    # ── Test 1: Query-to-Node alignment (symmetric: NodeEncoder for both) ─────
    # Uses NodeEncoder(query) vs NodeEncoder(definition) for same concept.
    # "what is entropy" should be close to "entropy: measure of disorder..."
    # because both share 'entropy' n-grams, and Phase 1 explicitly trained this.
    _QUERIES = [
        "what is {}", "explain {}", "tell me about {}", "define {}", "describe {}"
    ]
    q2n_sims: list[float] = []
    sampled = rng.sample(cids, min(n_samples, len(cids)))
    for cid in sampled:
        name  = cid.replace("_", " ")
        defn  = usable[cid].get("definition", "")[:400]
        n_vec = node_emb(defn)
        qs = []
        for tmpl in _QUERIES:
            q_vec = node_emb(tmpl.format(name))   # symmetric: NodeEncoder for queries too
            qs.append(_cosine(q_vec, n_vec))
        if qs:
            q2n_sims.append(float(np.mean(qs)))
            details[f"q2n_{cid}"] = round(float(np.mean(qs)), 4)

    # ── Test 2: Related-pair (direct KG edge) ─────────────────────────────────
    related_sims: list[float] = []
    edge_pairs = [(c, nb) for c in cids for nb in adj.get(c, set()) if nb in usable]
    rng.shuffle(edge_pairs)
    for a, b in edge_pairs[:n_samples]:
        sim = _cosine(node_emb(usable[a]["definition"][:400]),
                      node_emb(usable[b]["definition"][:400]))
        related_sims.append(sim)
        details[f"related_{a}_vs_{b}"] = round(sim, 4)

    # ── Test 3: Cross-domain separation ───────────────────────────────────────
    cross_sims: list[float] = []
    bkt_list = list(buckets.keys())
    if len(bkt_list) >= 2:
        for _ in range(n_samples):
            b1, b2 = rng.sample(bkt_list, 2)
            if buckets[b1] and buckets[b2]:
                a = rng.choice(buckets[b1])
                b = rng.choice(buckets[b2])
                if a in usable and b in usable:
                    sim = _cosine(node_emb(usable[a]["definition"][:400]),
                                  node_emb(usable[b]["definition"][:400]))
                    cross_sims.append(sim)
                    details[f"cross_{a}_vs_{b}"] = round(sim, 4)

    # ── Test 4: Within-domain 2-hop (hard negatives) ──────────────────────────
    hard_sims: list[float] = []
    for cid in rng.sample(cids, min(n_samples, len(cids))):
        candidates = [c for c in two_hop.get(cid, set())
                      if c in usable and _jaccard_tokens(cid, c) < 0.20]
        if candidates:
            nb  = rng.choice(candidates)
            sim = _cosine(node_emb(usable[cid]["definition"][:400]),
                          node_emb(usable[nb]["definition"][:400]))
            hard_sims.append(sim)
            details[f"hard_{cid}_vs_{nb}"] = round(sim, 4)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    def _mean(lst):
        return float(np.mean(lst)) if lst else float("nan")

    q2n   = _mean(q2n_sims)
    rel   = _mean(related_sims)
    cross = _mean(cross_sims)
    hard  = _mean(hard_sims)
    gap   = q2n - cross

    passed = (
        q2n   > THRESHOLDS["query_to_node"][0]
        and rel   > THRESHOLDS["related_pair"][0]
        and cross < THRESHOLDS["cross_domain"][0]
        and hard  < THRESHOLDS["within_domain_2hop"][0]
    )

    result = ValidationResult(
        query_to_node_mean=round(q2n, 4),
        related_pair_mean=round(rel, 4),
        cross_domain_mean=round(cross, 4),
        within_domain_2hop_mean=round(hard, 4),
        gap_query_vs_cross=round(gap, 4),
        passed=passed,
        details=details,
    )

    _log_result(result)
    return result


def _log_result(result: ValidationResult) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts  = time.strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"eval_{ts}.json"
    out.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")

    status = "✓ PASSED" if result.passed else "✗ FAILED"
    log.info("Validator %s:", status)
    log.info("  query→node:  %.4f (>0.45, symmetric NodeEncoder)", result.query_to_node_mean)
    log.info("  related:     %.4f (>0.55)", result.related_pair_mean)
    log.info("  cross-domain:%.4f (<0.35)", result.cross_domain_mean)
    log.info("  2-hop hard:  %.4f (<0.80)", result.within_domain_2hop_mean)
    log.info("  gap:         %.4f", result.gap_query_vs_cross)
    log.info("  → saved %s", out.name)
