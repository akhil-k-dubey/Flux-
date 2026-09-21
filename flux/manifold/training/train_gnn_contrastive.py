"""
flux/manifold/training/train_gnn_contrastive.py

Trains the GNN encoder with domain-based triplet loss to fix embedding collapse.

Problem: The GNN is currently run with torch.no_grad() — zero training signal.
All 845 concept embeddings collapse to the same direction because the GNN
weights were never updated by any loss function.

Fix: Triplet loss using KG 'domain' attribute as supervision:
  - Anchor, Positive: two concepts from the SAME domain → minimize distance
  - Anchor, Negative: concept from DIFFERENT domain → maximize distance
  loss = max(0, d_pos - d_neg + margin)

After training:
  - Same-domain concepts cluster together on the Lorentz manifold
  - target_head (in LanguageEncoder) can distinguish "entropy" from "neural_network"
  - Slot extraction becomes geometry-based instead of attention-heuristic-based

Usage:
    python -m flux.manifold.training.train_gnn_contrastive
"""
import random
import logging
import sys
from pathlib import Path

import torch
import torch.nn.functional as F

_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from flux.manifold.causal_manifold import CausalManifold, MANIFOLD_CHECKPOINT
from flux.manifold.lorentz import lorentz_distance
from flux.kg.store import KGStore

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")

_CACHE = Path("logs/manifold_emb_cache.pt")

# ── Hyperparameters ────────────────────────────────────────────────────────────
EPOCHS     = 60
MARGIN     = 1.5     # Lorentz geodesic distance margin
LR         = 3e-4
BATCH_SIZE = 256
MIN_DOMAIN_SIZE = 2  # need ≥2 concepts per domain for positive sampling


def _build_domain_groups(manifold, store):
    """concept_index → domain; domain → [concept_indices]"""
    groups = {}
    for i, cid in enumerate(manifold._concept_ids):
        node = store.get(cid)
        if node and node.get("domain"):
            groups.setdefault(node["domain"], []).append(i)
    return {d: idxs for d, idxs in groups.items() if len(idxs) >= MIN_DOMAIN_SIZE}


def _sample_triplets(groups, n):
    domain_list = list(groups.keys())
    triplets = []
    for _ in range(n):
        # anchor+positive domain
        d_pos = random.choice(domain_list)
        anchor, pos = random.sample(groups[d_pos], 2)
        # negative from different domain
        d_neg = random.choice([d for d in domain_list if d != d_pos])
        neg   = random.choice(groups[d_neg])
        triplets.append((anchor, pos, neg))
    return triplets


def _lorentz_probe(embs, groups, n=30):
    """Returns (mean_same_dist, mean_diff_dist) on random pairs."""
    dl = list(groups.items())
    same, diff = [], []
    for _ in range(n):
        d, idxs = random.choice(dl)
        if len(idxs) < 2: continue
        i, j = random.sample(idxs, 2)
        d2 = random.choice([d2 for d2, _ in dl if d2 != d])
        k  = random.choice(groups[d2])
        same.append(lorentz_distance(embs[i:i+1], embs[j:j+1]).item())
        diff.append(lorentz_distance(embs[i:i+1], embs[k:k+1]).item())
    return (sum(same)/len(same) if same else 0), (sum(diff)/len(diff) if diff else 0)


def train():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("Device: %s | epochs=%d | margin=%.1f | lr=%.0e | batch=%d",
             device, EPOCHS, MARGIN, LR, BATCH_SIZE)

    store    = KGStore()
    manifold = CausalManifold(device=str(device))

    if not manifold.load(MANIFOLD_CHECKPOINT, store=store):
        log.error("No manifold checkpoint at %s — run 'build manifold' first.", MANIFOLD_CHECKPOINT)
        return

    if manifold._node_features is None:
        log.error("node_features missing from checkpoint — rebuild manifold first.")
        return

    nf      = manifold._node_features.to(device)   # [N, 128]
    adj     = manifold._adj
    enc     = manifold.encoder.to(device)
    kappa   = manifold.kappa

    groups = _build_domain_groups(manifold, store)
    log.info("Domains for triplet training: %d", len(groups))
    for d, idxs in sorted(groups.items(), key=lambda x: -len(x[1]))[:6]:
        log.info("  %-32s %3d concepts", d, len(idxs))

    # ── Baseline distances before training ────────────────────────────────────
    enc.eval()
    with torch.no_grad():
        embs0 = enc(nf, adj)
    s0, d0 = _lorentz_probe(embs0.cpu(), groups)
    log.info("Before: same-domain=%.3f  cross-domain=%.3f  ratio=%.2f", s0, d0, d0/max(s0, 1e-6))

    # ── Triplet training ───────────────────────────────────────────────────────
    opt   = torch.optim.Adam(enc.parameters(), lr=LR)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS, eta_min=LR * 0.05)

    for ep in range(EPOCHS):
        enc.train()
        opt.zero_grad()

        all_embs = enc(nf, adj)   # [N, 257] — full forward with gradients

        triplets = _sample_triplets(groups, BATCH_SIZE)
        ai = torch.tensor([t[0] for t in triplets], device=device)
        pi = torch.tensor([t[1] for t in triplets], device=device)
        ni = torch.tensor([t[2] for t in triplets], device=device)

        d_pos = lorentz_distance(all_embs[ai], all_embs[pi], kappa)   # [B]
        d_neg = lorentz_distance(all_embs[ai], all_embs[ni], kappa)   # [B]

        loss = F.relu(d_pos - d_neg + MARGIN).mean()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(enc.parameters(), 1.0)
        opt.step()
        sched.step()

        if ep % 10 == 0 or ep == EPOCHS - 1:
            viols = (d_pos >= d_neg).float().mean().item()
            log.info("Ep %3d | loss=%.4f | d_pos=%.3f d_neg=%.3f | viols=%.0f%%",
                     ep, loss.item(), d_pos.mean().item(), d_neg.mean().item(), viols * 100)

    # ── Rebuild embeddings with trained GNN ────────────────────────────────────
    enc.eval()
    with torch.no_grad():
        manifold._embeddings = enc(nf, adj)

    # Post-training distances
    s1, d1 = _lorentz_probe(manifold._embeddings.cpu(), groups)
    log.info("After:  same-domain=%.3f  cross-domain=%.3f  ratio=%.2f", s1, d1, d1/max(s1, 1e-6))

    if d1 <= s1:
        log.warning("Training may not have converged — cross-domain distance still < same-domain!")

    # ── Spot check individual concept pairs ───────────────────────────────────
    embs = manifold._embeddings.cpu()
    idx  = {cid: i for i, cid in enumerate(manifold._concept_ids)}
    pairs = [
        ("entropy",     "thermodynamics", "same"),
        ("entropy",     "neural_network", "diff"),
        ("eigenvalue",  "linear_independence", "same"),
        ("eigenvalue",  "entropy",        "diff"),
        ("gradient",    "neural_network", "same"),
    ]
    log.info("Spot-check distances:")
    for a, b, kind in pairs:
        if a in idx and b in idx:
            d = lorentz_distance(embs[idx[a]:idx[a]+1], embs[idx[b]:idx[b]+1]).item()
            log.info("  d(%-20s, %-25s) = %.3f  [%s]", a, b, d, kind)

    # ── Save full manifold checkpoint ──────────────────────────────────────────
    manifold.save(MANIFOLD_CHECKPOINT)
    log.info("Manifold checkpoint saved → %s", MANIFOLD_CHECKPOINT)

    # ── Update manifold_emb_cache.pt (used by VectorRouter + encoder_trainer) ──
    # Merge: keep existing cache entries for concepts NOT in this manifold,
    # overwrite entries for all concepts that ARE in this manifold.
    existing = {}
    if _CACHE.exists():
        existing = torch.load(_CACHE, map_location="cpu", weights_only=True)

    updated = dict(existing)
    for i, cid in enumerate(manifold._concept_ids):
        updated[cid] = manifold._embeddings[i].cpu()

    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    torch.save(updated, _CACHE)
    log.info("Cache updated → %s (%d entries)", _CACHE, len(updated))

    log.info("\n=== Next step ===")
    log.info("Retrain LanguageEncoder to recalibrate op_head against new operator centroids:")
    log.info("  python -m flux.language.encoder_trainer")


if __name__ == "__main__":
    train()
