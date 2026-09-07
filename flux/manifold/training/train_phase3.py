"""
flux/manifold/training/train_phase3.py

Phase 3 training:
  Part A — Train ContextAwareRouter: (query, KG subgraph features) → operation
  Part B — Generate FLM training corpus: (query + serialized context) → response

Run: python3 -m flux.manifold.training.train_phase3

Part A trains fast (~1 min). Part B generates the data file that FLM fine-tuning reads.
"""
from __future__ import annotations
import json, logging, random, sys
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
log = logging.getLogger(__name__)

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import TensorDataset, DataLoader, WeightedRandomSampler

from flux.kg.store import get_store
from flux.manifold.operation_classifier import (
    OPERATIONS, OP_INDEX, char_ngram_ids, word_unigram_ids, structural_features,
)
from flux.manifold.kg_context_retriever import retrieve_for_concept
from flux.manifold.context_aware_router import (
    ContextAwareRouter, ROUTER_PATH, subgraph_to_feat, SUBGRAPH_DIM,
)
from flux.manifold.training.train_classifier import generate, _label

FLM_CORPUS_PATH = "logs/flux_phase3_corpus.jsonl"


# ── Part A: Router training ────────────────────────────────────────────────────

_SAME_DOMAIN_GAP = [
    "what conceptual bridge connects {a} and {b}",
    "what links {a} and {b}",
    "what is the intellectual connection between {a} and {b}",
    "how are {a} and {b} related",
    "what unifies {a} and {b}",
    "find the missing link between {a} and {b}",
    "what do {a} and {b} have in common at a deeper level",
    "what concept bridges {a} to {b}",
    "what intellectual bridge connects {a} and {b}",
    "where do {a} and {b} meet",
]


def build_router_dataset(store):
    """
    Extend the 3k classifier pairs with KG subgraph features + n_primary signal.

    n_primary is injected per operation type:
      - PERTURB / POINT_QUERY: n_primary=1  (single KG-grounded concept)
      - GAP / GEODESIC / CLUSTER: n_primary=2 (two KG-grounded concepts)

    Also adds same-domain GAP training pairs: two same-domain concepts with
    bridge/connection language — these teach the router that n_primary=2
    + same-domain + bridge vocabulary → GAP (not PERTURB).
    """
    import random as _rng
    pairs = generate(store, n_per_class=600)
    log.info("  %d pairs — extracting subgraph features...", len(pairs))

    from flux.manifold.operation_classifier import find_concepts_in_query
    import dataclasses

    # Map: which ops mean single-concept grounding vs two-concept grounding
    TWO_CONCEPT_OPS = {"GAP", "GEODESIC", "CLUSTER"}

    texts, ops, ctxs = [], [], []
    for text, op in pairs:
        found = find_concepts_in_query(text, store)
        if not found:
            continue
        src = found[0]
        ctx = retrieve_for_concept(src, store, k=6)
        # Inject n_primary signal based on op type
        n_prim = 2 if op in TWO_CONCEPT_OPS else 1
        ctx = dataclasses.replace(ctx, n_primary=n_prim)
        texts.append(text)
        ops.append(op)
        ctxs.append(ctx)

    # ── Same-domain GAP examples ──────────────────────────────────────────────
    # These pairs have two concepts from the SAME domain (so cross_domain=False)
    # but a "bridge/connection" query → GAP.  Without these, the router never
    # sees n_primary=2 + same-domain + GAP together and can't learn the distinction.
    rng = _rng.Random(42)
    all_ids = list(store.all_ids())
    # Group by domain
    domain_to_ids: dict = {}
    for cid in all_ids:
        dom = (store.get(cid) or {}).get("domain", "unknown")
        domain_to_ids.setdefault(dom, []).append(cid)

    for domain, members in domain_to_ids.items():
        if domain in ("constitutional", "unknown") or len(members) < 2:
            continue
        for _ in range(min(20, len(members))):
            a, b = rng.sample(members, 2)
            ctx_a = retrieve_for_concept(a, store, k=6)
            template = rng.choice(_SAME_DOMAIN_GAP)
            from flux.manifold.training.train_classifier import _label as _label2

            text = template.format(
                a=_label2(a).replace("_", " "),
                b=_label2(b).replace("_", " ")
            )
            # Inject n_primary=2, and use feature_ids that combine both concepts'
            # edges for a richer signal (cross_domain may still be False here)
            ctx_b = retrieve_for_concept(b, store, k=3)
            # Merge edge_hist (avg) and domain_vec (avg)
            merged_edge_hist = (ctx_a.edge_hist + ctx_b.edge_hist) / 2.0
            merged_domain_vec = (ctx_a.domain_vec + ctx_b.domain_vec) / 2.0
            merged_causal = (ctx_a.causal_density + ctx_b.causal_density) / 2.0
            merged_cross = ctx_a.cross_domain or ctx_b.cross_domain
            import dataclasses
            merged_ctx = dataclasses.replace(
                ctx_a,
                edge_hist      = merged_edge_hist,
                domain_vec     = merged_domain_vec,
                causal_density = merged_causal,
                cross_domain   = merged_cross,
                n_primary      = 2,
            )
            texts.append(text)
            ops.append("GAP")
            ctxs.append(merged_ctx)

    log.info("  %d usable pairs with subgraph context (incl. same-domain GAP)", len(texts))
    return texts, ops, ctxs




def encode_router_dataset(texts, ops, ctxs):
    max_c = max(len(char_ngram_ids(t))  for t in texts)
    max_w = max(len(word_unigram_ids(t)) for t in texts)

    C = torch.zeros(len(texts), max_c, dtype=torch.long)
    W = torch.zeros(len(texts), max_w, dtype=torch.long)
    S = torch.zeros(len(texts), 8,     dtype=torch.float)
    G = torch.zeros(len(texts), SUBGRAPH_DIM, dtype=torch.float)
    Y = torch.zeros(len(texts),               dtype=torch.long)

    for i, (t, op, ctx) in enumerate(zip(texts, ops, ctxs)):
        ci = char_ngram_ids(t);   C[i, :len(ci)] = torch.tensor(ci)
        wi = word_unigram_ids(t); W[i, :len(wi)] = torch.tensor(wi)
        S[i] = structural_features(t)
        G[i] = subgraph_to_feat(ctx)
        Y[i] = OP_INDEX[op]

    return C, W, S, G, Y


def train_router(epochs=60, lr=3e-3, batch=64):
    store = get_store()
    log.info("=== Part A: Training ContextAwareRouter ===")
    texts, ops, ctxs = build_router_dataset(store)
    log.info("Encoding dataset...")
    C, W, S, G, Y = encode_router_dataset(texts, ops, ctxs)

    counts  = torch.bincount(Y, minlength=len(OPERATIONS)).float()
    weights = (1.0 / counts)[Y]
    sampler = WeightedRandomSampler(weights, len(Y), replacement=True)

    ds     = TensorDataset(C, W, S, G, Y)
    loader = DataLoader(ds, batch_size=batch, sampler=sampler)

    model = ContextAwareRouter()
    opt   = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    best_acc = 0.0
    for ep in range(1, epochs + 1):
        model.train()
        total_loss = correct = total = 0
        for bc, bw, bs, bg, by in loader:
            opt.zero_grad()
            logits = torch.stack([
                model(bc[i], bw[i], bs[i], bg[i]) for i in range(len(bc))
            ])
            loss = F.cross_entropy(logits, by)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total_loss += loss.item()
            correct    += (logits.argmax(1) == by).sum().item()
            total      += len(by)
        sched.step()

        acc = correct / total
        if acc > best_acc:
            best_acc = acc
            torch.save(model.state_dict(), ROUTER_PATH)

        if ep % 10 == 0 or ep == 1:
            log.info("[Epoch %3d/%d] loss=%.4f acc=%.1f%% best=%.1f%%",
                     ep, epochs, total_loss / len(loader), acc * 100, best_acc * 100)

    log.info("Router training done. Best accuracy: %.1f%%", best_acc * 100)
    return best_acc


# ── Part B: FLM training corpus generation ─────────────────────────────────────

def generate_flm_corpus(store, manifold, n_per_class=200):
    """
    Generate (query, context, response) triples for FLM fine-tuning.

    Uses the existing manifold operations as the supervision signal:
    - PERTURB: run perturb_query → format cascade as response text
    - GEODESIC: run geodesic_query → format path as response text
    - GAP:      run gap_query → use gap_description
    - CLUSTER:  nearest_concepts → list concepts
    - POINT_QUERY: KG node definition as response

    Stores to FLM_CORPUS_PATH as JSONL: {"query":..., "context":..., "response":...}
    """
    from flux.manifold.training.train_classifier import (
        _PERTURB, _GEODESIC, _GAP, _CLUSTER, _POINT, _label,
    )
    from flux.manifold.operation_classifier import find_concepts_in_query

    log.info("=== Part B: Generating FLM training corpus ===")
    rng    = random.Random(99)
    ids    = list(store.all_ids())

    # Collect all edges from KGStore via edges_from per concept
    edges: list[dict] = []
    for cid in ids:
        for e in (store.edges_from(cid) or []):
            edges.append({"from": cid, "to": e.get("to", ""), "type": e.get("type", "")})

    causal = [e["from"] for e in edges
              if e["type"] in ("CAUSES", "ENABLES", "REQUIRES") and e["from"]]
    cross  = [(e["from"], e["to"]) for e in edges
              if e["from"] and e["to"] and
              (store.get(e["from"]) or {}).get("domain") !=
              (store.get(e["to"]) or {}).get("domain")]


    records = []

    def _ctx(cid):
        return retrieve_for_concept(cid, store, k=5).serialized

    def _label_cap(cid):
        return _label(cid).replace("_", " ").title()

    # PERTURB — construct from direct KG causal edges (no manifold propagation needed)
    pool_c = list(set(causal))
    rng.shuffle(pool_c)
    count = 0
    for src in pool_c * 3:  # loop pool multiple times to reach n_per_class
        if count >= n_per_class:
            break
        out_edges = [e for e in edges if e["from"] == src
                     and e["type"] in ("CAUSES", "ENABLES", "REQUIRES")]
        if not out_edges:
            continue
        text = rng.choice(_PERTURB).format(a=_label(src))
        direct = [f"  • {_label_cap(e['to'])} (via {e['type'].lower()})"
                  for e in out_edges[:4]]
        # 2-hop propagation from direct targets via more edges
        propagated = []
        for e in out_edges[:2]:
            for e2 in [x for x in edges if x["from"] == e["to"]
                       and x["type"] in ("CAUSES", "ENABLES", "REQUIRES")][:2]:
                propagated.append(f"  • {_label_cap(e2['to'])} (propagated)")
        response = (f"If {_label_cap(src)} decreased:\n\nDirectly affected:\n" +
                    "\n".join(direct) +
                    ("\n\nPropagated effects:\n" + "\n".join(propagated) if propagated else ""))
        records.append({"query": text, "context": _ctx(src), "response": response, "op": "PERTURB"})
        count += 1

    # GEODESIC — use direct KG edges to construct 1–2 hop paths
    pool_e = [e for e in edges if e["from"] and e["to"]]
    rng.shuffle(pool_e)
    count = 0
    for e in pool_e * 3:
        if count >= n_per_class:
            break
        src, tgt = e["from"], e["to"]
        if not src or not tgt or src == tgt:
            continue
        # Try 2-hop path through the edge
        hop2 = [x["to"] for x in edges if x["from"] == tgt and x["to"] and x["to"] != src]
        text = rng.choice(_GEODESIC).format(a=_label(src), b=_label(tgt))
        if hop2:
            path = f"{_label(src)} → {_label(tgt)} → {_label(hop2[0])}"
            endpoint = _label_cap(hop2[0])
        else:
            path = f"{_label(src)} → {_label(tgt)}"
            endpoint = _label_cap(tgt)
        response = (f"Reasoning chain from {_label_cap(src)} to {_label_cap(tgt)}:\n\n"
                    f"  {path}\n\nThis path has "
                    f"{len(path.split('→'))} step(s), each connected by a KG edge.")
        ctx = retrieve_for_concept(src, store, k=5).serialized
        records.append({"query": text, "context": ctx, "response": response, "op": "GEODESIC"})
        count += 1

    # GAP — construct bridge description from cross-domain pairs
    pool_x = list(cross)
    rng.shuffle(pool_x)
    count = 0
    for src, tgt in pool_x * 5:
        if count >= n_per_class:
            break
        if not src or not tgt:
            continue
        dn_a = (store.get(src) or {}).get("domain") or src
        dn_b = (store.get(tgt) or {}).get("domain") or tgt
        if not dn_a or not dn_b or dn_a == dn_b:
            continue
        text = rng.choice(_GAP).format(a=_label(src), b=_label(tgt))
        response = (f"Gap between '{dn_a}' and '{dn_b}' domains. "
                    f"Nearest cross-domain concepts: {_label_cap(src)}, {_label_cap(tgt)}. "
                    f"A bridge concept here would need to unify both domains — "
                    f"this is where Flux's hypotheses live.")
        ctx = retrieve_for_concept(src, store, k=5).serialized
        records.append({"query": text, "context": ctx, "response": response, "op": "GAP"})
        count += 1

    # POINT_QUERY
    rng.shuffle(ids)
    count = 0
    for src in ids * 2:
        if count >= n_per_class:
            break
        node = store.get(src) or {}
        defn = node.get("definition") or ""
        if not defn:
            continue
        text     = rng.choice(_POINT).format(a=_label(src))
        response = defn[:400]
        records.append({"query": text, "context": _ctx(src), "response": response, "op": "POINT_QUERY"})
        count += 1

    rng.shuffle(records)
    with open(FLM_CORPUS_PATH, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    by_op = {}
    for r in records:
        by_op[r["op"]] = by_op.get(r["op"], 0) + 1
    log.info("FLM corpus: %d records — %s", len(records), by_op)
    log.info("Saved to %s", FLM_CORPUS_PATH)



if __name__ == "__main__":
    # Part A: router
    train_router()

    # Part B: FLM corpus (needs manifold)
    log.info("Loading manifold for FLM corpus generation...")
    from flux.manifold.causal_manifold import CausalManifold, MANIFOLD_CHECKPOINT
    store    = get_store()
    manifold = CausalManifold(dim=256, kappa=1.0)
    if manifold.load(MANIFOLD_CHECKPOINT, store=store):
        generate_flm_corpus(store, manifold, n_per_class=200)
    else:
        log.warning("Manifold not loaded — skipping FLM corpus generation")
