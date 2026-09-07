"""
flux/manifold/training/train_query_encoder.py

Trains QueryEncoder: minimize Lorentz distance between encoded query and
the source concept's frozen GNN embedding.

The supervision signal: for each (query_text, source_concept_id) pair,
push the encoded query point toward source_concept's manifold embedding.
The GNN embeddings are FROZEN — only the query encoder weights are learned.

Run: python3 -m flux.manifold.training.train_query_encoder
"""
from __future__ import annotations
import logging, sys
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
log = logging.getLogger(__name__)

import random
import torch
import torch.nn as nn
import torch.nn.functional as F

from flux.kg.store import get_store
from flux.manifold.causal_manifold import CausalManifold, MANIFOLD_CHECKPOINT
from flux.manifold.lorentz import lorentz_distance
from flux.manifold.operation_classifier import (
    char_ngram_ids, word_unigram_ids, structural_features, OPERATIONS,
)
from flux.manifold.query_encoder import QueryEncoder, ENCODER_PATH, MANIFOLD_DIM
from flux.manifold.training.train_classifier import generate, _label

# ── Build dataset: (query_text, source_concept_id) ────────────────────────────

def build_dataset(store, manifold):
    pairs = generate(store, n_per_class=600)   # reuse balanced generator
    result = []
    for text, op in pairs:
        # Ground the first concept mentioned back to its concept_id
        from flux.manifold.operation_classifier import find_concepts_in_query
        found = find_concepts_in_query(text, store)
        found = [c for c in found if c in manifold._concept_index]
        if not found:
            continue
        result.append((text, found[0]))  # (query_text, source_concept_id)
    log.info("  %d usable query→concept pairs", len(result))
    return result

# ── Training ───────────────────────────────────────────────────────────────────

def train(epochs: int = 80, lr: float = 1e-3, batch: int = 64):
    store = get_store()

    manifold = CausalManifold(dim=MANIFOLD_DIM, kappa=1.0)
    if not manifold.load(MANIFOLD_CHECKPOINT, store=store):
        log.error("No manifold checkpoint. Run train_embedding.py first.")
        return

    log.info("Generating query→concept dataset...")
    dataset = build_dataset(store, manifold)
    if not dataset:
        log.error("Empty dataset — check concept grounding.")
        return

    # Freeze manifold embeddings
    concept_embs = manifold._embeddings.detach()  # [N, dim+1]

    # Encode all text features upfront (faster training)
    log.info("Encoding text features...")
    texts, concept_ids = zip(*dataset)
    concept_indices = [manifold._concept_index[cid] for cid in concept_ids]

    max_c = max(len(char_ngram_ids(t)) for t in texts)
    max_w = max(len(word_unigram_ids(t)) for t in texts)
    C = torch.zeros(len(texts), max_c, dtype=torch.long)
    W = torch.zeros(len(texts), max_w, dtype=torch.long)
    S = torch.zeros(len(texts), 8,     dtype=torch.float)
    for i, t in enumerate(texts):
        ci = char_ngram_ids(t);  C[i, :len(ci)] = torch.tensor(ci)
        wi = word_unigram_ids(t); W[i, :len(wi)] = torch.tensor(wi)
        S[i] = structural_features(t)
    CI = torch.tensor(concept_indices, dtype=torch.long)

    encoder = QueryEncoder(manifold_dim=MANIFOLD_DIM)
    # Query encoder is lightweight — train on CPU for simplicity
    device  = torch.device("cpu")
    concept_embs = concept_embs.to(device)
    encoder = encoder.to(device)
    C, W, S = C.to(device), W.to(device), S.to(device)

    opt     = torch.optim.AdamW(encoder.parameters(), lr=lr, weight_decay=1e-4)
    sched   = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    n = len(dataset)
    best_loss = float("inf")

    for ep in range(1, epochs + 1):
        encoder.train()
        idx    = torch.randperm(n)
        total  = 0.0
        steps  = 0

        for start in range(0, n, batch):
            b = idx[start:start + batch]
            bc, bw, bs = C[b], W[b], S[b]
            targets = concept_embs[CI[b]]          # [B, dim+1] — frozen

            # Encode queries → manifold points
            q_points = torch.stack([
                encoder(bc[i], bw[i], bs[i]) for i in range(len(b))
            ])                                     # [B, dim+1]

            # Loss: mean Lorentz distance to target concept
            dists = lorentz_distance(q_points, targets, kappa=1.0)
            loss  = dists.mean()

            if loss.isnan():
                log.warning("NaN loss at epoch %d — skipping batch", ep)
                continue

            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(encoder.parameters(), 1.0)
            opt.step()
            total += loss.item()
            steps += 1

        sched.step()
        avg_loss = total / max(steps, 1)

        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(encoder.state_dict(), ENCODER_PATH)

        if ep % 10 == 0 or ep == 1:
            log.info("[Epoch %3d/%d] avg_dist=%.4f  best=%.4f", ep, epochs, avg_loss, best_loss)

    log.info("Done. Best avg distance: %.4f  Saved to %s", best_loss, ENCODER_PATH)

if __name__ == "__main__":
    train()
