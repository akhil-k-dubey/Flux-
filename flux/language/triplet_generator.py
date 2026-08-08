"""
flux/language/triplet_generator.py

Generates (anchor, positive, negative) triplets from Flux's KG.
Zero external contamination — every triplet derived from concepts
Akhil taught Flux.

Strategy
--------
For each concept C in the KG:
  anchor   = query_template(C)          e.g. "what is entropy"
  positive = different_template(C)      e.g. "tell me about entropy"
  negative = query_template(C') where C' ≠ C (random or hardest sample)

Paraphrase templates cover:
  • direct name queries
  • indirect / descriptive queries (first 5 tokens of definition)
  • structural queries (gap, connect)

Produces ~5000 triplets from 374 concepts — enough for a solid
projection head without any external data.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Triplet:
    anchor:   str
    positive: str
    negative: str


# ── Query templates ───────────────────────────────────────────────────────────

def _readable(concept_id: str) -> str:
    """Convert concept_id to readable form: gibbs_free_energy → gibbs free energy."""
    return concept_id.replace("_", " ")


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
    lambda n, _:  f"can you tell me what {n} is",
    lambda n, _:  f"what does {n} mean",
    lambda n, _:  f"flux what is {n}",
    lambda n, _:  f"i was wondering what {n} is",
    lambda n, d:  f"{d}",           # first N words of definition (no wrapper)
    lambda n, d:  f"{n}: {d}",      # concept: definition snippet
    lambda n, d:  f"{n} is {d}",    # concept is definition
]

# Core forms — concept substance without query templates
# These are the ANCHORS that train the head on pure concept signal
_CORE_FORMS = [
    lambda n, d: f"{n}",
    lambda n, d: f"{n} {d}",
    lambda n, d: f"{d}",
    lambda n, d: f"{n}: {d}",
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


def _definition_snippet(definition: str, max_words: int = 6) -> str:
    """Take first N non-trivial words from definition."""
    words = re.split(r"\s+", definition.strip())
    snippet = " ".join(w for w in words[:max_words] if len(w) > 2)
    return snippet if len(snippet) > 5 else definition[:40]


# ── Triplet generation ────────────────────────────────────────────────────────

def generate_triplets(
    nodes: dict,
    n_negatives_per_anchor: int = 2,
    seed: int = 42,
) -> list[Triplet]:
    """
    Generate contrastive training triplets from KG nodes dict.

    Key design decision:
    Anchors are CORE FORMS (concept name + definition snippet) — no query templates.
    Positives are either another core form OR a full query template.
    This prevents the head from ignoring concept content (templates like
    'what is ' are identical across ALL concepts and drown out the signal).

    Hard negatives: same-layer concepts are harder negatives than random ones.
    They force the head to learn fine-grained distinctions within a domain.

    nodes: { concept_id: { "definition": str, "layer": str, ... } }
    """
    rng = random.Random(seed)

    concept_ids   = list(nodes.keys())
    # Filter out internal/axiom nodes that have no meaningful definition
    concept_ids   = [
        c for c in concept_ids
        if not c.startswith("axiom_")
        and not c.startswith("__")
        and len(nodes[c].get("definition", "")) > 10
    ]

    if len(concept_ids) < 3:
        raise ValueError(f"Too few usable concepts ({len(concept_ids)}) to generate triplets.")

    # Build layer → concept_ids map for hard negatives
    layer_map: dict[str, list[str]] = {}
    for cid in concept_ids:
        layer = nodes[cid].get("layer", "unknown")
        layer_map.setdefault(layer, []).append(cid)

    triplets: list[Triplet] = []

    for cid in concept_ids:
        node    = nodes[cid]
        name    = _readable(cid)
        defn    = node.get("definition", "")
        snippet = _definition_snippet(defn)
        layer   = node.get("layer", "unknown")

        # Core forms for this concept (substance without template noise)
        core_forms = [t(name, snippet) for t in _CORE_FORMS]

        # Full query variants
        explain_variants = [t(name, snippet) for t in _EXPLAIN_TEMPLATES]

        # ── Core ↔ Core pairs (strongest signal: pure concept substance) ───
        for i in range(len(core_forms)):
            for j in range(i + 1, len(core_forms)):
                anchor   = core_forms[i]
                positive = core_forms[j]
                # Random negatives
                neg_pool = [c for c in concept_ids if c != cid]
                for neg_cid in rng.sample(neg_pool, min(n_negatives_per_anchor, len(neg_pool))):
                    neg_name = _readable(neg_cid)
                    neg_snip = _definition_snippet(nodes[neg_cid].get("definition", ""))
                    neg_t    = rng.choice(_CORE_FORMS)
                    triplets.append(Triplet(anchor=anchor, positive=positive,
                                           negative=neg_t(neg_name, neg_snip)))

        # ── Core ↔ Query pairs (bridges substance → natural language) ───
        for cf in core_forms:
            qv = rng.choice(explain_variants)
            # Random negative
            neg_cid  = rng.choice([c for c in concept_ids if c != cid])
            neg_name = _readable(neg_cid)
            neg_snip = _definition_snippet(nodes[neg_cid].get("definition", ""))
            neg_cf   = rng.choice(_CORE_FORMS)(neg_name, neg_snip)
            triplets.append(Triplet(anchor=cf, positive=qv, negative=neg_cf))

            # Hard negative: same-layer concept (harder to distinguish)
            same_layer = [c for c in layer_map.get(layer, []) if c != cid]
            if same_layer:
                hn_cid   = rng.choice(same_layer)
                hn_name  = _readable(hn_cid)
                hn_snip  = _definition_snippet(nodes[hn_cid].get("definition", ""))
                hn_neg   = rng.choice(_CORE_FORMS)(hn_name, hn_snip)
                triplets.append(Triplet(anchor=cf, positive=qv, negative=hn_neg))

        # ── Same-concept query pairs (natural language paraphrases) ───
        rng.shuffle(explain_variants)
        for i in range(0, len(explain_variants) - 1, 2):
            anchor   = explain_variants[i]
            positive = explain_variants[i + 1]
            neg_pool = [c for c in concept_ids if c != cid]
            for neg_cid in rng.sample(neg_pool, min(1, len(neg_pool))):
                neg_name = _readable(neg_cid)
                neg_snip = _definition_snippet(nodes[neg_cid].get("definition", ""))
                neg_t    = rng.choice(_CORE_FORMS)
                triplets.append(Triplet(anchor=anchor, positive=positive,
                                       negative=neg_t(neg_name, neg_snip)))

        # ── CONNECT pairs ─────────────────────────────────────────────────
        edges = node.get("relations", []) or []
        related = [e.get("target", "") for e in edges if e.get("target") in nodes]
        if not related:
            related = rng.sample([c for c in concept_ids if c != cid], min(2, len(concept_ids)-1))

        for rel_cid in related[:2]:
            rel_name = _readable(rel_cid)
            ct       = rng.choice(_CONNECT_TEMPLATES)
            anchor   = ct(name, rel_name)
            pos_t    = rng.choice([t for t in _CONNECT_TEMPLATES if t is not ct])
            positive = pos_t(name, rel_name)
            neg_cid  = rng.choice([c for c in concept_ids if c not in (cid, rel_cid)])
            neg_name = _readable(neg_cid)
            neg_snip = _definition_snippet(nodes[neg_cid].get("definition", ""))
            neg_cf   = rng.choice(_CORE_FORMS)(neg_name, neg_snip)
            triplets.append(Triplet(anchor=anchor, positive=positive, negative=neg_cf))

        # ── GAP pairs ──────────────────────────────────────────────────────
        gt       = rng.choice(_GAP_TEMPLATES)
        anchor   = gt(name, snippet)
        gt2      = rng.choice([t for t in _GAP_TEMPLATES if t is not gt])
        positive = gt2(name, snippet)
        neg_cid  = rng.choice([c for c in concept_ids if c != cid])
        neg_name = _readable(neg_cid)
        neg_snip = _definition_snippet(nodes[neg_cid].get("definition", ""))
        triplets.append(Triplet(
            anchor=anchor, positive=positive,
            negative=rng.choice(_CORE_FORMS)(neg_name, neg_snip)
        ))

    rng.shuffle(triplets)
    return triplets


def split(triplets: list[Triplet], val_frac: float = 0.1, seed: int = 42):
    """Split into train/val sets."""
    rng = random.Random(seed)
    data = list(triplets)
    rng.shuffle(data)
    n_val  = max(1, int(len(data) * val_frac))
    return data[n_val:], data[:n_val]
