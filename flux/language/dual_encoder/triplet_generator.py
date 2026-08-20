"""
flux/language/dual_encoder/triplet_generator.py

Builds (anchor_def, positive_def, hard_neg_def) triplets from KG structure.

All three fields are DEFINITION strings (not query templates).
The node encoder trains on definitions — this is what it encodes at inference.

Negative strategy
-----------------
Priority 1: 2-hop KG neighbor + Jaccard token overlap < 0.20
  - Semantically adjacent (shares a KG neighbour)
  - Character-distinct (different concept name tokens)
  - Hard for the encoder but NOT contradictory for it (unlike word-overlap pairs)

Priority 2: same domain bucket (type::layer), no direct edge
  - Same domain but unconnected — medium difficulty

Priority 3: cross-domain fallback

3:1 hard:easy mix per anchor.

Triplet stats saved to logs/dual_encoder/triplet_stats.json.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

STATS_PATH = Path("logs/dual_encoder/triplet_stats.json")


@dataclass(frozen=True)
class Triplet:
    anchor:     str   # concept A definition
    positive:   str   # concept B definition (KG edge neighbour of A)
    negative:   str   # concept C definition (hard or easy negative)
    anchor_cid: str = ""  # concept A's KG ID — used by Phase 2 for LTC alignment target lookup


# ── Graph helpers ─────────────────────────────────────────────────────────────

def _build_adj(store, cids: list[str]) -> dict[str, set[str]]:
    cid_set = set(cids)
    adj: dict[str, set[str]] = {c: set() for c in cids}
    for cid in cids:
        for e in (store._edges_from.get(cid, []) if store else []):
            nb = e.get("to", "")
            if nb in cid_set:
                adj[cid].add(nb)
                adj[nb].add(cid)
    return adj


def _build_two_hop(adj: dict[str, set[str]]) -> dict[str, set[str]]:
    """For each concept: 2-hop neighbours (not self, not 1-hop)."""
    two_hop: dict[str, set[str]] = {}
    for cid, one in adj.items():
        two: set[str] = set()
        for nb in one:
            for nb2 in adj.get(nb, set()):
                if nb2 != cid and nb2 not in one:
                    two.add(nb2)
        two_hop[cid] = two
    return two_hop


def _jaccard_tokens(a: str, b: str) -> float:
    """Token Jaccard similarity on underscore-split concept IDs."""
    ta, tb = set(a.split("_")), set(b.split("_"))
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def _build_domain_buckets(nodes: dict) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for cid, nd in nodes.items():
        key = f"{nd.get('type','?')}::{nd.get('layer','?')}"
        buckets.setdefault(key, []).append(cid)
    return buckets


# ── Negative sampler ──────────────────────────────────────────────────────────

def _hard_neg(
    anchor: str,
    two_hop: dict[str, set[str]],
    adj: dict[str, set[str]],
    buckets: dict[str, list[str]],
    bkt_of: dict[str, str],
    all_cids: list[str],
    rng: random.Random,
    excl: set[str],
) -> str | None:
    blocked = excl | adj.get(anchor, set())

    # Priority 1: 2-hop + Jaccard < 0.20 (semantically adjacent, character-distinct)
    p1 = [c for c in two_hop.get(anchor, set())
          if c not in blocked and _jaccard_tokens(anchor, c) < 0.20]
    if p1:
        return rng.choice(p1)

    # Priority 2: same domain bucket, no direct edge
    p2 = [c for c in buckets.get(bkt_of.get(anchor, ""), [])
          if c not in blocked]
    if p2:
        return rng.choice(p2)

    # Priority 3: cross-domain
    my_bkt = bkt_of.get(anchor, "")
    other  = [k for k in buckets if k != my_bkt]
    rng.shuffle(other)
    for bk in other:
        pool = [c for c in buckets[bk] if c not in excl]
        if pool:
            return rng.choice(pool)
    return None


# ── Main generator ────────────────────────────────────────────────────────────

def generate_triplets(
    nodes: dict,
    store=None,
    seed: int = 42,
    hard_ratio: float = 0.75,
) -> list[Triplet]:
    """
    Generate definition-to-definition triplets from KG structure.

    Anchors and positives come from direct KG edges (A→B both present).
    Negatives are 2-hop + Jaccard-filtered (hard) or cross-domain (easy).
    """
    rng = random.Random(seed)

    usable_cids = [
        c for c in nodes
        if not c.startswith(("axiom_", "__", "consequence_query_"))
        and len(nodes[c].get("definition", "")) > 10
        and nodes[c].get("confidence", 0) > 0.4
    ]
    if len(usable_cids) < 3:
        raise ValueError(f"Too few usable concepts ({len(usable_cids)})")

    usable   = {c: nodes[c] for c in usable_cids}
    adj      = _build_adj(store, usable_cids)
    two_hop  = _build_two_hop(adj)
    buckets  = _build_domain_buckets(usable)
    bkt_of   = {c: bk for bk, ids in buckets.items() for c in ids}

    triplets: list[Triplet] = []
    hard_count = easy_count = p1_count = p2_count = p3_count = 0

    for cid in usable_cids:
        a_def = usable[cid].get("definition", "")[:400]
        if not a_def:
            continue

        # Positives: direct KG edge neighbours
        neighbours = [
            nb for nb in adj.get(cid, set())
            if nb in usable and usable[nb].get("definition", "")
        ]
        if not neighbours:
            continue

        excl = {cid}
        for nb in neighbours[:4]:  # at most 4 positive pairs per anchor
            p_def = usable[nb].get("definition", "")[:400]
            excl.add(nb)

            # Decide hard vs easy
            use_hard = rng.random() < hard_ratio
            neg_cid  = _hard_neg(cid, two_hop, adj, buckets, bkt_of, usable_cids, rng, excl)
            if neg_cid is None:
                continue

            n_def = usable[neg_cid].get("definition", "")[:400]
            triplets.append(Triplet(a_def, p_def, n_def, anchor_cid=cid))

            # Track stats
            if neg_cid in two_hop.get(cid, set()) and _jaccard_tokens(cid, neg_cid) < 0.20:
                p1_count += 1; hard_count += 1
            elif bkt_of.get(cid) == bkt_of.get(neg_cid):
                p2_count += 1; hard_count += 1
            else:
                p3_count += 1; easy_count += 1

    rng.shuffle(triplets)

    stats = {
        "total": len(triplets),
        "hard": hard_count,
        "easy": easy_count,
        "priority_1_two_hop": p1_count,
        "priority_2_same_domain": p2_count,
        "priority_3_cross_domain": p3_count,
    }
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    log.info("TripletGenerator: %d triplets (P1=%d P2=%d P3=%d)",
             len(triplets), p1_count, p2_count, p3_count)
    return triplets


def split(triplets: list[Triplet], val_frac: float = 0.10, seed: int = 42):
    rng  = random.Random(seed)
    data = list(triplets)
    rng.shuffle(data)
    n_val = max(1, int(len(data) * val_frac))
    return data[n_val:], data[:n_val]
