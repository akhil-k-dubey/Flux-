"""
flux/manifold/training/train_classifier.py

Generates balanced training data from the KG and trains the OperationClassifier.

Run: python3 -m flux.manifold.training.train_classifier
"""
from __future__ import annotations
import logging, random, sys
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
log = logging.getLogger(__name__)

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset, WeightedRandomSampler


from flux.kg.store import get_store
from flux.manifold.operation_classifier import (
    OperationClassifier, OPERATIONS, OP_INDEX, CLASSIFIER_PATH,
    char_ngram_ids, word_unigram_ids, structural_features,
)

# ── Template banks ─────────────────────────────────────────────────────────────
_PERTURB = [
    "what would happen if {a} decreased",
    "what breaks if {a} fails",
    "consequences of removing {a}",
    "if {a} were violated what would change",
    "what fails without {a}",
    "what if {a} were absent",
    "effects of {a} being reversed",
    "what collapses if {a} is gone",
    "what would break if {a} were disabled",
    "if we remove {a} what happens",
    "what would happen if {a} increased",
    "what if {a} amplified beyond normal bounds",
    "what would change if {a} were negated",
    "downstream effects of {a} failing",
    "what depends on {a}",
]
_GEODESIC = [
    "how does {a} connect to {b}",
    "path from {a} to {b}",
    "trace from {a} toward {b}",
    "what links {a} and {b}",
    "reasoning chain from {a} to {b}",
    "how does {a} relate to {b}",
    "bridge between {a} and {b}",
    "connection from {a} to {b}",
    "link {a} and {b}",
    "how is {a} connected to {b}",
    "what is the path from {a} to {b}",
    "show me the connection between {a} and {b}",
    "how does {a} lead to {b}",
    "chain of reasoning from {a} to {b}",
    "what connects {a} to {b}",
]
_GAP = [
    "what concept would bridge {a} and {b}",
    "gap between {a} and {b}",
    "what lies between {a} and {b} conceptually",
    "hypothesis connecting {a} and {b}",
    "what would link {a} domain and {b} domain",
    "missing concept between {a} and {b}",
    "what would connect {a} and {b} domains",
    "is there a bridge concept from {a} to {b}",
    "what theoretical concept spans {a} and {b}",
    "invent a concept bridging {a} and {b}",
]
_CLUSTER = [
    "what concepts cluster around {a}",
    "what is related to {a} in my knowledge",
    "concepts similar to {a}",
    "neighbours of {a}",
    "what concepts are near {a}",
    "what is in the same family as {a}",
    "related concepts to {a}",
    "what else connects to {a}",
    "show me concepts near {a}",
    "what concepts surround {a}",
]
_POINT = [
    "what is {a}",
    "explain {a}",
    "tell me about {a}",
    "define {a}",
    "what do you know about {a}",
    "describe {a}",
    "what is the definition of {a}",
    "give me information on {a}",
    "how does {a} work",
    "what is {a} in simple terms",
    "summarise {a}",
    "audit {a}",
]

# ── Data generation ────────────────────────────────────────────────────────────

def _label(concept_id: str) -> str:
    return concept_id.replace("_", " ")

def generate(store, n_per_class: int = 600) -> list[tuple[str, str]]:
    """Returns [(query_text, operation_type), ...]"""
    pairs = []
    ids   = list(store.all_ids())
    edges = store.all_edges() if hasattr(store, "all_edges") else []

    # Cross-domain pairs for GEODESIC and GAP
    cross_domain: list[tuple[str, str]] = []
    same_domain:  list[tuple[str, str]] = []
    for e in edges:
        src, tgt = e.get("from", ""), e.get("to", "")
        if not src or not tgt:
            continue
        sn = (store.get(src) or {}).get("domain")
        tn = (store.get(tgt) or {}).get("domain")
        if sn and tn and sn != tn:
            cross_domain.append((src, tgt))
        elif src and tgt:
            same_domain.append((src, tgt))

    causal_ids = [
        e.get("from", "") for e in edges
        if e.get("type") in ("CAUSES", "ENABLES", "REQUIRES") and e.get("from")
    ]

    rng = random.Random(42)

    def sample_concept():
        return _label(rng.choice(ids))

    def sample_pair(pool, fallback):
        p = rng.choice(pool) if pool else (rng.choice(fallback), rng.choice(fallback))
        return _label(p[0]), _label(p[1])

    # PERTURB
    for _ in range(n_per_class):
        a = _label(rng.choice(causal_ids) if causal_ids else rng.choice(ids))
        pairs.append((rng.choice(_PERTURB).format(a=a), "PERTURB"))

    # GEODESIC
    for _ in range(n_per_class):
        a, b = sample_pair(same_domain + cross_domain, ids)
        if a == b:
            b = sample_concept()
        pairs.append((rng.choice(_GEODESIC).format(a=a, b=b), "GEODESIC"))

    # GAP
    for _ in range(n_per_class):
        a, b = sample_pair(cross_domain, ids)
        if a == b:
            b = sample_concept()
        pairs.append((rng.choice(_GAP).format(a=a, b=b), "GAP"))

    # CLUSTER
    for _ in range(n_per_class):
        pairs.append((rng.choice(_CLUSTER).format(a=sample_concept()), "CLUSTER"))

    # POINT_QUERY
    for _ in range(n_per_class):
        pairs.append((rng.choice(_POINT).format(a=sample_concept()), "POINT_QUERY"))

    rng.shuffle(pairs)
    return pairs

# ── Dataset encoding ───────────────────────────────────────────────────────────

def encode_dataset(pairs: list[tuple[str, str]]):
    max_char = max(len(char_ngram_ids(t)) for t, _ in pairs)
    max_word = max(len(word_unigram_ids(t)) for t, _ in pairs)

    C = torch.zeros(len(pairs), max_char, dtype=torch.long)
    W = torch.zeros(len(pairs), max_word, dtype=torch.long)
    S = torch.zeros(len(pairs), 8,         dtype=torch.float)
    Y = torch.zeros(len(pairs),             dtype=torch.long)

    for i, (text, op) in enumerate(pairs):
        ci = char_ngram_ids(text)
        wi = word_unigram_ids(text)
        C[i, :len(ci)] = torch.tensor(ci)
        W[i, :len(wi)] = torch.tensor(wi)
        S[i]           = structural_features(text)
        Y[i]           = OP_INDEX[op]

    return C, W, S, Y

# ── Training ───────────────────────────────────────────────────────────────────

def train(epochs: int = 60, lr: float = 3e-3, batch: int = 64):
    store = get_store()
    log.info("Generating training data...")
    pairs = generate(store, n_per_class=600)
    log.info("  %d pairs across %d classes", len(pairs), len(OPERATIONS))

    C, W, S, Y = encode_dataset(pairs)

    # Weighted sampler for class balance
    class_counts  = torch.bincount(Y, minlength=len(OPERATIONS)).float()
    sample_weights = (1.0 / class_counts)[Y]
    sampler = WeightedRandomSampler(sample_weights, len(pairs), replacement=True)

    ds     = TensorDataset(C, W, S, Y)
    loader = DataLoader(ds, batch_size=batch, sampler=sampler)

    model = OperationClassifier()
    opt   = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    best_acc = 0.0
    for ep in range(1, epochs + 1):
        model.train()
        total_loss = correct = total = 0
        for bc, bw, bs, by in loader:
            opt.zero_grad()
            logits = torch.stack([
                model(bc[i], bw[i], bs[i]) for i in range(len(bc))
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
            torch.save(model.state_dict(), CLASSIFIER_PATH)

        if ep % 10 == 0 or ep == 1:
            log.info("[Epoch %3d/%d] loss=%.4f acc=%.1f%% best=%.1f%%",
                     ep, epochs, total_loss / len(loader), acc * 100, best_acc * 100)

    log.info("Training done. Best accuracy: %.1f%%. Saved to %s", best_acc * 100, CLASSIFIER_PATH)
    return best_acc

if __name__ == "__main__":
    train()
