"""
flux/manifold/training/train_embedding.py

Training loop for the GNN encoder on the Lorentz manifold.

Loss: Contrastive loss on manifold distance.
  Positive pairs (causal) → minimize distance
  Negative pairs (random) → maximize distance (up to a margin)

This is a Lorentz-space contrastive loss:
  L = positive_distance + max(0, margin - negative_distance)

After training, the geometry test should show:
  mean_causal_distance < mean_random_distance

Usage:
    python3 -m flux.manifold.training.train_embedding
    python3 -m flux.manifold.training.train_embedding --epochs 50 --lr 0.001
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.optim as optim

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

PAIRS_FILE = Path("logs/manifold_training_pairs.jsonl")
SENSORS    = Path("logs/flux_sensors.jsonl")


def lorentz_contrastive_loss(d_pos: torch.Tensor,
                              d_neg: torch.Tensor,
                              margin: float = 2.0) -> torch.Tensor:
    """
    Contrastive loss on Lorentz manifold distances.
    d_pos: [B] distances for positive (causal) pairs — we want these SMALL
    d_neg: [B] distances for negative (random) pairs — we want these LARGE

    L = mean(d_pos) + mean(max(0, margin - d_neg))
    """
    pos_loss = d_pos.mean()
    neg_loss = torch.clamp(margin - d_neg, min=0.0).mean()
    return pos_loss + neg_loss


def run_training(epochs: int = 30,
                 lr: float = 5e-4,
                 batch_size: int = 32,
                 margin: float = 2.0,
                 dim: int = 256,
                 kappa: float = 1.0,
                 save_every: int = 10) -> None:
    """
    Train the GNN encoder on the generated embedding pairs.

    Before running: generate pairs with
        python3 -m flux.manifold.training.pair_generator
    or:
        from flux.manifold.training.pair_generator import generate_and_save
        generate_and_save(store)
    """
    import sys; sys.path.insert(0, ".")
    from flux.kg.store import get_store
    from flux.manifold.causal_manifold import CausalManifold, MANIFOLD_CHECKPOINT
    from flux.manifold.lorentz import lorentz_distance

    if not PAIRS_FILE.exists():
        log.error("Training pairs not found at %s. Run pair_generator first.", PAIRS_FILE)
        return

    # Load pairs
    embedding_pairs = []
    with PAIRS_FILE.open() as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("type") == "embedding":
                embedding_pairs.append(rec)

    positive_pairs = [p for p in embedding_pairs if p["should_be_close"]]
    negative_pairs = [p for p in embedding_pairs if not p["should_be_close"]]
    log.info("Loaded %d positive + %d negative embedding pairs",
             len(positive_pairs), len(negative_pairs))

    # Build manifold (with hash features for now — retrain with LTC later)
    store = get_store()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    manifold = CausalManifold(dim=dim, kappa=kappa, device=str(device))
    n_concepts = manifold.build(store, text_encoder=None)
    log.info("Manifold built with %d concepts on %s", n_concepts, device)

    optimizer = optim.Adam(manifold.encoder.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    # Baseline geometry test BEFORE training
    log.info("\nBaseline geometry test (before training):")
    baseline = manifold.validate_geometry(store)
    for k, v in baseline.items():
        log.info("  %s: %s", k, v)

    best_ratio = 0.0
    for epoch in range(1, epochs + 1):
        manifold.encoder.train()

        # ── Single forward pass per epoch ──────────────────────────────────────
        # ONE full GNN pass computes all 793 embeddings with gradients.
        # Then we accumulate all contrastive losses and do ONE backward.
        # This is ~25× faster than doing a full forward pass per batch.
        all_features = manifold._node_features.to(device)
        all_embs = manifold.encoder(all_features, manifold._adj)  # [N, dim+1]

        # Shuffle pairs and take a sample (all pairs in one shot)
        random.shuffle(positive_pairs)
        random.shuffle(negative_pairs)
        n_sample   = min(len(positive_pairs), len(negative_pairs), 512)
        pos_sample = positive_pairs[:n_sample]
        neg_sample = negative_pairs[:n_sample]

        # Build index tensors for all pairs at once
        pos_src_idx = torch.tensor(
            [manifold._concept_index.get(p["concept_a"], 0) for p in pos_sample],
            device=device)
        pos_tgt_idx = torch.tensor(
            [manifold._concept_index.get(p["concept_b"], 0) for p in pos_sample],
            device=device)
        neg_src_idx = torch.tensor(
            [manifold._concept_index.get(p["concept_a"], 0) for p in neg_sample],
            device=device)
        neg_tgt_idx = torch.tensor(
            [manifold._concept_index.get(p["concept_b"], 0) for p in neg_sample],
            device=device)

        pos_src_embs = all_embs[pos_src_idx]                       # [S, dim+1]
        pos_tgt_embs = all_embs[pos_tgt_idx]
        neg_src_embs = all_embs[neg_src_idx]
        neg_tgt_embs = all_embs[neg_tgt_idx]

        d_pos = lorentz_distance(pos_src_embs, pos_tgt_embs, kappa) # [S]
        d_neg = lorentz_distance(neg_src_embs, neg_tgt_embs, kappa)

        loss = lorentz_contrastive_loss(d_pos, d_neg, margin)

        optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(manifold.encoder.parameters(), max_norm=1.0)
        optimizer.step()
        scheduler.step()

        avg_loss = loss.item()

        if epoch % 5 == 0 or epoch == 1:
            # Geometry test
            manifold.encoder.eval()
            with torch.no_grad():
                manifold._embeddings = manifold.encoder(
                    manifold._node_features.to(device), manifold._adj
                )
            val = manifold.validate_geometry(store)
            ratio = (val["mean_random_distance"] /
                     (val["mean_causal_distance"] + 1e-7))
            log.info("[Epoch %3d/%d] loss=%.4f | causal_dist=%.3f | "
                     "random_dist=%.3f | ratio=%.2f | passed=%s",
                     epoch, epochs, avg_loss,
                     val["mean_causal_distance"],
                     val["mean_random_distance"],
                     ratio,
                     "✓" if val["geometry_test_passed"] else "✗")

            if ratio > best_ratio:
                best_ratio = ratio
                manifold.save(MANIFOLD_CHECKPOINT)
                log.info("  → New best (ratio=%.2f), checkpoint saved.", ratio)

        elif epoch % save_every == 0:
            manifold.save(MANIFOLD_CHECKPOINT)

    # Final validation
    log.info("\nFinal geometry test:")
    manifold.encoder.eval()
    with torch.no_grad():
        manifold._embeddings = manifold.encoder(
            manifold._node_features.to(device), manifold._adj
        )
    final = manifold.validate_geometry(store)
    for k, v in final.items():
        log.info("  %s: %s", k, v)

    # Log training completion to sensors
    with SENSORS.open("a") as f:
        f.write(json.dumps({
            "event":     "manifold_training_complete",
            "timestamp": time.time(),
            "epochs":    epochs,
            "best_ratio": best_ratio,
            "final_geometry_passed": final["geometry_test_passed"],
            "concepts_embedded": n_concepts,
            "dim": dim,
            "kappa": kappa,
        }) + "\n")

    log.info("Training complete. Best ratio: %.2f. Checkpoint: %s",
             best_ratio, MANIFOLD_CHECKPOINT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs",     type=int,   default=30)
    parser.add_argument("--lr",         type=float, default=5e-4)
    parser.add_argument("--batch-size", type=int,   default=32)
    parser.add_argument("--margin",     type=float, default=2.0)
    parser.add_argument("--dim",        type=int,   default=256)
    parser.add_argument("--kappa",      type=float, default=1.0)
    args = parser.parse_args()

    run_training(
        epochs     = args.epochs,
        lr         = args.lr,
        batch_size = args.batch_size,
        margin     = args.margin,
        dim        = args.dim,
        kappa      = args.kappa,
    )
