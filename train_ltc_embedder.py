#!/usr/bin/env python3
"""
train_ltc_embedder.py

Train Flux's LTC ProjectionHead using contrastive (InfoNCE) learning.

Usage
-----
  python3 train_ltc_embedder.py              # full training
  python3 train_ltc_embedder.py --quick      # 20-epoch smoke test
  python3 train_ltc_embedder.py --eval       # evaluate existing head only

What this does
--------------
  1. Load Flux's KG (374+ concepts, all Akhil-defined)
  2. Generate synthetic (anchor, positive, negative) triplets from KG vocab
  3. Freeze LTC base weights (EWC protection by construction)
  4. Train ProjectionHead (256 → 128, L2-norm) with InfoNCE
  5. Save best head to checkpoints/flux_proj_head.pt
  6. Run quality evaluation — show cosine similarity matrix for sample concepts
  7. Activate head in ltc_embedder — encode() now returns 128-dim instead of 256-dim

After training
--------------
  flux_runner.py will automatically use the trained head via ltc_embedder.encode()
  kg_index.py will add a Tier-4 LTC path for highest-quality concept resolution.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import torch

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("train_ltc_embedder")


def _banner(msg: str) -> None:
    print(f"\n{'='*62}")
    print(f"  {msg}")
    print(f"{'='*62}")


def _progress(epoch: int, train_loss: float, val_loss: float) -> None:
    bar_len = 20
    # Simple ASCII progress indicator every 5 epochs
    if epoch % 5 == 0 or epoch == 1:
        print(f"  Epoch {epoch:3d}  |  train={train_loss:.4f}  val={val_loss:.4f}")


# ── LTC encode function (frozen, no_grad) ─────────────────────────────────────

def _build_ltc_encoder():
    """Returns a callable: text → torch.Tensor(256,) no_grad."""
    from flux.language import ltc_embedder

    def _encode(text: str) -> torch.Tensor:
        with torch.no_grad():
            ltc_embedder._load()
            if ltc_embedder._model is None:
                raise RuntimeError("LTC model not loaded — need a checkpoint first.")
            chars_fwd = ltc_embedder._text_to_tensor(text)
            chars_bwd = chars_fwd.flip(0)
            emb_fwd   = ltc_embedder._run_pass(chars_fwd)
            emb_bwd   = ltc_embedder._run_pass(chars_bwd)
            return torch.cat([emb_fwd, emb_bwd], dim=0).detach()   # (256,)
    return _encode


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate_quality(
    ltc_encode,
    proj_head,
    nodes:    dict,
    n_pairs:  int = 10,
    seed:     int = 99,
) -> None:
    """
    Print cosine similarity matrix for:
    - Same-concept paraphrases (should be HIGH ~0.8-1.0)
    - Cross-concept queries   (should be LOW  ~0.0-0.3)
    """
    import random
    rng = random.Random(seed)
    from flux.language.triplet_generator import _readable, _definition_snippet, _EXPLAIN_TEMPLATES

    usable = [
        cid for cid in nodes
        if not cid.startswith("axiom_")
        and not cid.startswith("__")
        and len(nodes[cid].get("definition", "")) > 10
    ]
    sample = rng.sample(usable, min(n_pairs, len(usable)))

    print("\n  SAME-CONCEPT SIMILARITY (should be HIGH > 0.70):")
    print(f"  {'Concept':<30} {'Query A':<32} {'Query B':<32} CosSim")
    print("  " + "-" * 102)

    proj_head.eval()
    same_sims, cross_sims = [], []

    def _proj_encode(text: str) -> np.ndarray:
        raw = ltc_encode(text)
        with torch.no_grad():
            proj = proj_head(raw.unsqueeze(0)).squeeze(0)
        return proj.cpu().numpy()

    for cid in sample:
        name  = _readable(cid)
        defn  = nodes[cid].get("definition", "")
        snip  = _definition_snippet(defn)
        t1, t2 = rng.sample(_EXPLAIN_TEMPLATES, 2)
        q1, q2 = t1(name, snip), t2(name, snip)
        e1, e2 = _proj_encode(q1), _proj_encode(q2)
        sim    = float(np.dot(e1, e2))
        same_sims.append(sim)
        q1_s = q1[:30] if len(q1) > 30 else q1
        q2_s = q2[:30] if len(q2) > 30 else q2
        print(f"  {cid[:28]:<30} {q1_s:<32} {q2_s:<32} {sim:+.4f}")

    print(f"\n  Mean same-concept sim: {np.mean(same_sims):+.4f}")

    # Cross-concept pairs
    print("\n  CROSS-CONCEPT SIMILARITY (should be LOW < 0.30):")
    print(f"  {'Concept A':<28} {'Concept B':<28} CosSim")
    print("  " + "-" * 64)

    pairs = [(sample[i], sample[(i+1) % len(sample)]) for i in range(min(8, len(sample)))]
    for ca, cb in pairs:
        na, nb = _readable(ca), _readable(cb)
        da = nodes[ca].get("definition", "")
        db = nodes[cb].get("definition", "")
        q1 = f"what is {na}"
        q2 = f"what is {nb}"
        e1, e2 = _proj_encode(q1), _proj_encode(q2)
        sim = float(np.dot(e1, e2))
        cross_sims.append(sim)
        print(f"  {ca[:26]:<28} {cb[:26]:<28} {sim:+.4f}")

    print(f"\n  Mean cross-concept sim: {np.mean(cross_sims):+.4f}")
    sep = np.mean(same_sims) - np.mean(cross_sims)
    print(f"  Separation (higher = better): {sep:+.4f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick",  action="store_true", help="20-epoch smoke test")
    ap.add_argument("--eval",   action="store_true", help="evaluate existing head only")
    ap.add_argument("--epochs", type=int, default=0,    help="override max epochs")
    ap.add_argument("--temp",   type=float, default=0.05, help="InfoNCE temperature")
    args = ap.parse_args()

    _banner("FLUX LTC CONTRASTIVE TRAINING")

    # ── Load KG ──────────────────────────────────────────────────────────────
    from flux.kg.store import get_store
    store = get_store()
    nodes = store.all_nodes()
    usable = {
        cid: nd for cid, nd in nodes.items()
        if not cid.startswith("axiom_")
        and not cid.startswith("__")
        and len(nd.get("definition", "")) > 10
    }
    print(f"  KG: {len(nodes)} total nodes, {len(usable)} usable for training")

    # ── Build LTC encoder (frozen) ────────────────────────────────────────────
    print("  Loading LTC base weights (frozen)...")
    ltc_encode = _build_ltc_encoder()
    # Verify it works
    try:
        test_emb = ltc_encode("entropy")
        print(f"  LTC encode test: shape={tuple(test_emb.shape)} ✓")
    except Exception as e:
        print(f"  ERROR: LTC encode failed — {e}")
        print("  Make sure a checkpoint exists in checkpoints/")
        sys.exit(1)

    # ── Load or create projection head ────────────────────────────────────────
    from flux.language.projection_head import ProjectionHead, CKPT_PATH
    if ProjectionHead.is_trained() and not args.eval is False:
        print(f"  Loading existing projection head from {CKPT_PATH}")
        proj_head = ProjectionHead.load()
    else:
        print("  Creating new projection head (random init)")
        proj_head = ProjectionHead()
    proj_head.eval()
    n_params = sum(p.numel() for p in proj_head.parameters())
    print(f"  ProjectionHead: {n_params:,} trainable parameters")

    if args.eval:
        _banner("EVALUATION ONLY")
        evaluate_quality(ltc_encode, proj_head, usable)
        return

    # ── Generate triplets ─────────────────────────────────────────────────────
    _banner("GENERATING TRIPLETS")
    from flux.language.triplet_generator import generate_triplets, split
    triplets = generate_triplets(usable, n_negatives_per_anchor=3, seed=42)
    train_set, val_set = split(triplets, val_frac=0.10)
    print(f"  Generated: {len(triplets)} triplets")
    print(f"  Train: {len(train_set)}  Val: {len(val_set)}")
    print(f"  Sample anchor:   '{train_set[0].anchor}'")
    print(f"  Sample positive: '{train_set[0].positive}'")
    print(f"  Sample negative: '{train_set[0].negative}'")

    # ── Training ──────────────────────────────────────────────────────────────
    _banner("TRAINING")
    from flux.language import contrastive_trainer as ct
    if args.quick:
        ct.MAX_EPOCHS = 20
        ct.PATIENCE   = 5
        print("  [QUICK MODE — 20 epochs]")
    if args.epochs:
        ct.MAX_EPOCHS = args.epochs
    if args.temp:
        ct.TEMPERATURE = args.temp

    print(f"  InfoNCE temperature: {ct.TEMPERATURE}")
    print(f"  Max epochs: {ct.MAX_EPOCHS}  |  Batch: {ct.BATCH_SIZE}  |  LR: {ct.LR}")
    print()

    proj_head.train()
    metrics = ct.train(
        proj_head   = proj_head,
        ltc_encode  = ltc_encode,
        train_set   = train_set,
        val_set     = val_set,
        progress_cb = _progress,
    )

    # ── Results ───────────────────────────────────────────────────────────────
    _banner("TRAINING COMPLETE")
    print(f"  Best val loss:  {metrics['best_val_loss']:.4f}")
    print(f"  Epochs trained: {metrics['epochs']}")
    print(f"  Time elapsed:   {metrics['elapsed_s']:.1f}s")
    print(f"  Saved to:       {CKPT_PATH}")

    # ── Quality evaluation ────────────────────────────────────────────────────
    _banner("QUALITY EVALUATION")
    # Reload best saved head into singleton before evaluation
    from flux.language import ltc_embedder
    ltc_embedder.reload_proj_head()
    proj_head = ProjectionHead.load()   # load best saved
    evaluate_quality(ltc_encode, proj_head, usable)

    # ── Activate in embedder ──────────────────────────────────────────────────
    _banner("ACTIVATING IN LTCEMBEDDER")
    print("  Reloading projection head into singleton...")
    ltc_embedder.reload_proj_head()
    print(f"  Projection head active: {ltc_embedder.has_proj_head()}")
    print("  Rebuilding KG embedding index with trained head...")
    from flux.language import kg_index
    kg_index.rebuild(store)
    print("  Done. Flux's LTC embedding engine is now active.")
    print()
    print("  Run  python3 flux_runner.py  to start her with the trained embeddings.")


if __name__ == "__main__":
    main()
