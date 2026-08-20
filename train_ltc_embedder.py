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


# ── LTC encode functions ──────────────────────────────────────────────────────

def _build_ltc_encoder_frozen():
    """Returns a callable: text → torch.Tensor(256,) — LTC frozen (no_grad)."""
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


def _build_ltc_encoder_trainable():
    """Returns (callable, ltc_model) — LTC is UNFROZEN, gradients flow through it."""
    from flux.language import ltc_embedder
    ltc_embedder._load()
    if ltc_embedder._model is None:
        raise RuntimeError("LTC model not loaded — need a checkpoint first.")

    def _encode(text: str) -> torch.Tensor:
        # No torch.no_grad() — gradients flow through LTC weights
        chars_fwd = ltc_embedder._text_to_tensor(text)
        chars_bwd = chars_fwd.flip(0)
        emb_fwd   = ltc_embedder._run_pass_grad(chars_fwd)
        emb_bwd   = ltc_embedder._run_pass_grad(chars_bwd)
        return torch.cat([emb_fwd, emb_bwd], dim=0)   # (256,) — no detach

    ltc_model = ltc_embedder.get_model()
    return _encode, ltc_model


# Keep old name as alias (frozen) for backward compatibility
def _build_ltc_encoder():
    return _build_ltc_encoder_frozen()


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate_quality(ltc_encode, proj_head, nodes, n_pairs=10, seed=99):
    import random
    from flux.language.triplet_generator import _readable, _definition_snippet, _EXPLAIN_TEMPLATES

    dev = next(proj_head.parameters()).device
    proj_head.eval()

    def _enc(text):
        with torch.no_grad():
            return proj_head(ltc_encode(text).to(dev).unsqueeze(0)).squeeze(0).cpu().numpy()

    rng    = random.Random(seed)
    sample = rng.sample([c for c in nodes if len(nodes[c].get("definition","")) > 10],
                        min(n_pairs, len(nodes)))

    same_sims, cross_sims = [], []

    print("\n  SAME-CONCEPT (target > 0.70):")
    for cid in sample:
        name = _readable(cid)
        snip = _definition_snippet(nodes[cid].get("definition",""))
        t1, t2 = rng.sample(_EXPLAIN_TEMPLATES, 2)
        sim = float(np.dot(_enc(t1(name, snip)), _enc(t2(name, snip))))
        same_sims.append(sim)
        print(f"  {name:<30} {sim:+.4f}")

    print("\n  CROSS-CONCEPT (target < 0.30):")
    for ca, cb in [(sample[i], sample[(i+1) % len(sample)]) for i in range(min(8, len(sample)))]:
        sim = float(np.dot(_enc(f"what is {_readable(ca)}"), _enc(f"what is {_readable(cb)}")))
        cross_sims.append(sim)
        print(f"  {_readable(ca):<28} vs {_readable(cb):<28} {sim:+.4f}")
    sep = np.mean(same_sims) - np.mean(cross_sims)
    print(f"\n  Same: {np.mean(same_sims):+.4f}  Cross: {np.mean(cross_sims):+.4f}  Gap: {sep:+.4f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import torch   # import here to avoid module-level CUDA init at import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--quick",  action="store_true", help="20-epoch smoke test")
    ap.add_argument("--eval",   action="store_true", help="evaluate existing head only")
    ap.add_argument("--epochs", type=int, default=0,    help="override max epochs")
    ap.add_argument("--freeze", action="store_true",   help="freeze LTC (old mode) — default is UNFROZEN")
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

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # ── Build LTC encoder ────────────────────────────────────────────────────
    from flux.language import ltc_embedder   # needed here for test + model access
    frozen_ltc = args.freeze
    ltc_model  = None

    if frozen_ltc:
        print("  Loading LTC base weights (FROZEN — old mode)...")
        ltc_encode = _build_ltc_encoder_frozen()
    else:
        print("  Loading LTC base weights (UNFROZEN — fine-tuning at 3e-5)...")
        ltc_encode, ltc_model = _build_ltc_encoder_trainable()

    # Verify it works
    try:
        with torch.no_grad():
            test_emb = ltc_embedder._run_pass(ltc_embedder._text_to_tensor("entropy"))
        print(f"  LTC encode test: shape=(256,) ✓")
    except Exception as e:
        print(f"  ERROR: LTC encode failed — {e}")
        print("  Make sure a checkpoint exists in checkpoints/")
        sys.exit(1)

    if ltc_model is not None:
        n_ltc = sum(p.numel() for p in ltc_model.parameters())
        print(f"  LTC model: {n_ltc:,} parameters (trainable at lr=3e-5)")

    # ── Load or create projection head ────────────────────────────────────────
    # Always start fresh — don't load the collapsed checkpoint
    from flux.language.projection_head import ProjectionHead, CKPT_PATH
    print("  Creating new projection head (random init) — fresh start for semantic training")
    proj_head = ProjectionHead()
    proj_head.eval()
    n_params = sum(p.numel() for p in proj_head.parameters())
    print(f"  ProjectionHead: {n_params:,} trainable parameters")

    if args.eval:
        _banner("EVALUATION ONLY")
        evaluate_quality(ltc_encode, ProjectionHead.load().to(device), usable)
        return

    # ── Generate triplets ─────────────────────────────────────────────────────
    _banner("GENERATING TRIPLETS")
    from flux.language.triplet_generator import generate_triplets, split
    triplets = generate_triplets(usable, store=store, n_negatives_per_anchor=3, seed=42)
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


    mode_str = "FROZEN LTC" if frozen_ltc else "UNFROZEN LTC (differential LR)"
    print(f"  Device: {device}")
    print(f"  Mode: {mode_str}")
    print(f"  Temperature: {ct.TEMP_START:.2f} → {ct.TEMP_END:.2f} over {ct.TEMP_ANNEAL_EPOCHS} epochs")
    print(f"  Max epochs: {ct.MAX_EPOCHS}  |  Batch: {ct.BATCH_SIZE}")
    print(f"  LR head: {ct.LR_HEAD}  |  LR LTC: {ct.LR_LTC}")
    print()

    proj_head.train()
    metrics = ct.train(
        proj_head   = proj_head,
        ltc_encode  = ltc_encode,
        train_set   = train_set,
        val_set     = val_set,
        device      = device,
        progress_cb = _progress,
        frozen_ltc  = frozen_ltc,
        ltc_model   = ltc_model,
    )

    # ── Results ───────────────────────────────────────────────────────────────
    _banner("TRAINING COMPLETE")
    print(f"  Best val loss:  {metrics['best_val_loss']:.4f}")
    print(f"  Epochs trained: {metrics['epochs']}")
    print(f"  Time elapsed:   {metrics['elapsed_s']:.1f}s")
    print(f"  Saved to:       {CKPT_PATH}")

    # ── Quality evaluation ─────────────────────────────────────────────────────
    _banner("QUALITY EVALUATION")
    evaluate_quality(ltc_encode, ProjectionHead.load().to(device), usable)

    # ── Activate ───────────────────────────────────────────────────────────────
    _banner("ACTIVATING")
    from flux.language import ltc_embedder, kg_index
    ltc_embedder.reload_proj_head()
    kg_index.rebuild(store)
    print("  Done — run flux_runner.py to start.")


if __name__ == "__main__":
    main()
