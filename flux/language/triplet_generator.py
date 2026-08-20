"""
flux/language/triplet_generator.py

Generates (anchor, positive, negative) triplets from Flux KG.

Negative strategy — 3:1 hard:easy mix
--------------------------------------
OLD: all negatives cross-domain (different type::layer bucket).
     transfer_learning and overfitting were NEVER each other's negatives
     → model never learned to push them apart → cross-concept stayed at 0.98.

NEW: hard negatives = same domain bucket + NO KG edge.
     These are concepts that share a domain but aren't explicitly related.
     Forces fine-grained separation within a domain, not just coarse
     physics-vs-ML separation that the model learns trivially by epoch 3.

     3 hard negatives : 1 cross-domain per anchor
     Hard > word-overlap (confusable names) > same bucket > cross-domain fallback
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Triplet:
    anchor:   str
    positive: str
    negative: str


# ── Text helpers ──────────────────────────────────────────────────────────────

def _readable(cid: str) -> str:
    return cid.replace("_", " ")

def _definition_snippet(defn: str, max_words: int = 6) -> str:
    words = re.split(r"\s+", defn.strip())
    snippet = " ".join(w for w in words[:max_words] if len(w) > 2)
    return snippet if len(snippet) > 5 else defn[:40]


# ── Templates ─────────────────────────────────────────────────────────────────

_EXPLAIN_TEMPLATES = [
    lambda n, _:  f"what is {n}",
    lambda n, _:  f"tell me about {n}",
    lambda n, _:  f"explain {n}",
    lambda n, _:  f"what do you know about {n}",
    lambda n, _:  f"describe {n}",
    lambda n, _:  f"define {n}",
    lambda n, _:  f"{n}",
    lambda n, _:  f"{n} what is it",
    lambda n, _:  f"i want to understand {n}",
    lambda n, _:  f"what does {n} mean",
    lambda n, _:  f"flux what is {n}",
    lambda n, d:  f"{d}",
    lambda n, d:  f"{n}: {d}",
    lambda n, d:  f"{n} is {d}",
]

_CONNECT_TEMPLATES = [
    lambda a, b: f"how does {a} relate to {b}",
    lambda a, b: f"what is the relationship between {a} and {b}",
    lambda a, b: f"how does {a} connect to {b}",
    lambda a, b: f"what connects {a} and {b}",
    lambda a, b: f"{a} and {b} - what is the connection",
    lambda a, b: f"is there a link between {a} and {b}",
    lambda a, b: f"{a} versus {b}",
    lambda a, b: f"how are {a} and {b} related",
]

_GAP_TEMPLATES = [
    lambda n, _: f"what don't you know about {n}",
    lambda n, _: f"what gaps exist in your knowledge of {n}",
    lambda n, _: f"what is missing about {n}",
    lambda n, _: f"where does your knowledge of {n} end",
]


# ── Negative samplers ─────────────────────────────────────────────────────────

def _build_domain_buckets(nodes: dict) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for cid, nd in nodes.items():
        key = f"{nd.get('type','UNKNOWN')}::{nd.get('layer','unknown')}"
        buckets.setdefault(key, []).append(cid)
    return buckets


def _build_adj(store, cids: list[str]) -> dict[str, set[str]]:
    """Undirected adjacency set from KG edges."""
    cid_set = set(cids)
    adj: dict[str, set[str]] = {c: set() for c in cids}
    for cid in cids:
        for e in (store._edges_from.get(cid, []) if store else []):
            nb = e.get("to", "")
            if nb in cid_set:
                adj[cid].add(nb)
                adj[nb].add(cid)
    return adj


def _hard_neg(anchor_cid, anchor_bucket, buckets, adj, all_cids, rng, excl):
    """
    Hard negative: same domain bucket + no KG edge, OR name word overlap + no edge.
    Falls back to cross-domain if nothing found.
    """
    anchor_words = set(anchor_cid.split("_"))
    blocked      = excl | adj.get(anchor_cid, set())

    # Priority 1: name word overlap + no edge (confusable concept names)
    word_pool = [c for c in all_cids
                 if c not in blocked and anchor_words & set(c.split("_"))]
    if word_pool:
        return rng.choice(word_pool)

    # Priority 2: same domain bucket + no edge
    domain_pool = [c for c in buckets.get(anchor_bucket, []) if c not in blocked]
    if domain_pool:
        return rng.choice(domain_pool)

    # Fallback: cross-domain
    return _cross_neg(anchor_bucket, buckets, rng, excl)


def _cross_neg(anchor_bucket, buckets, rng, excl):
    """Cross-domain negative (different type::layer bucket)."""
    other = [k for k in buckets if k != anchor_bucket]
    if not other:
        return None
    rng.shuffle(other)
    for bk in other:
        pool = [c for c in buckets[bk] if c not in excl]
        if pool:
            return rng.choice(pool)
    return None


def _sample_neg(anchor_cid, anchor_bucket, buckets, adj, all_cids, rng, excl, hard=True):
    """3:1 hard:cross-domain mix."""
    if hard and rng.random() < 0.75:
        neg = _hard_neg(anchor_cid, anchor_bucket, buckets, adj, all_cids, rng, excl)
    else:
        neg = _cross_neg(anchor_bucket, buckets, rng, excl)
    return neg


# ── Main generator ────────────────────────────────────────────────────────────

def generate_triplets(
    nodes: dict,
    store=None,
    n_negatives_per_anchor: int = 2,
    seed: int = 42,
) -> list[Triplet]:
    rng = random.Random(seed)

    concept_ids = [
        c for c in nodes
        if not c.startswith("axiom_")
        and not c.startswith("__")
        and not c.startswith("consequence_query_")
        and len(nodes[c].get("definition", "")) > 10
        and nodes[c].get("confidence", 0) > 0.4
    ]
    if len(concept_ids) < 3:
        raise ValueError(f"Too few usable concepts ({len(concept_ids)})")

    filtered   = {c: nodes[c] for c in concept_ids}
    buckets    = _build_domain_buckets(filtered)
    adj        = _build_adj(store, concept_ids)
    bkt_of     = {c: bk for bk, ids in buckets.items() for c in ids}

    triplets: list[Triplet] = []

    for cid in concept_ids:
        nd     = nodes[cid]
        name   = _readable(cid)
        defn   = nd.get("definition", "")
        snip   = _definition_snippet(defn)
        full   = defn[:180]
        bucket = bkt_of.get(cid, "UNKNOWN::unknown")
        excl   = {cid}

        def neg(hard=True):
            return _sample_neg(cid, bucket, buckets, adj, concept_ids, rng, excl, hard)

        # ── Signal 1: Name ↔ Definition (no character overlap) ───────────────
        for _ in range(n_negatives_per_anchor):
            n1 = neg()
            if n1:
                n1_name = _readable(n1)
                triplets.append(Triplet(name, full, n1_name))
                q = rng.choice(_EXPLAIN_TEMPLATES)
                triplets.append(Triplet(
                    f"{name}: {full}",
                    q(name, snip),
                    q(n1_name, _definition_snippet(nodes[n1].get("definition", ""))),
                ))

        # ── Signal 2: KG edge neighbors as structural positives ──────────────
        if store is not None:
            for e in store._edges_from.get(cid, [])[:3]:
                nb = e.get("to", "")
                if nb not in filtered or nb == cid:
                    continue
                nb_name = _readable(nb)
                nb_defn = nodes[nb].get("definition", "")[:180]
                n2 = neg()
                if n2:
                    n2_name = _readable(n2)
                    ct = rng.choice(_CONNECT_TEMPLATES)
                    triplets.append(Triplet(f"{name}: {full}", f"{nb_name}: {nb_defn}", ct(name, n2_name)))
                    triplets.append(Triplet(name, nb_name, n2_name))

        # ── Signal 3: Query paraphrases ──────────────────────────────────────
        variants = [t(name, snip) for t in _EXPLAIN_TEMPLATES]
        rng.shuffle(variants)
        for i in range(0, len(variants) - 1, 2):
            n3 = neg()
            if n3:
                n3_name = _readable(n3)
                n3_snip = _definition_snippet(nodes[n3].get("definition", ""))
                tmpl    = _EXPLAIN_TEMPLATES[i % len(_EXPLAIN_TEMPLATES)]
                triplets.append(Triplet(variants[i], variants[i+1], tmpl(n3_name, n3_snip)))

        # ── Signal 3b: Gap paraphrases ───────────────────────────────────────
        gt, gt2 = rng.sample(_GAP_TEMPLATES, 2)
        n4 = neg()
        if n4:
            n4_name = _readable(n4)
            n4_snip = _definition_snippet(nodes[n4].get("definition", ""))
            triplets.append(Triplet(gt(name, snip), gt2(name, snip), gt(n4_name, n4_snip)))

    rng.shuffle(triplets)
    return triplets


def split(triplets: list[Triplet], val_frac: float = 0.1, seed: int = 42):
    rng  = random.Random(seed)
    data = list(triplets)
    rng.shuffle(data)
    n_val = max(1, int(len(data) * val_frac))
    return data[n_val:], data[:n_val]
