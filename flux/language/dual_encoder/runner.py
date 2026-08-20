"""
flux/language/dual_encoder/runner.py

Single CLI entry point for the full dual encoder pipeline.

Usage:
  python -m flux.language.dual_encoder.runner --phase 1
  python -m flux.language.dual_encoder.runner --phase 2
  python -m flux.language.dual_encoder.runner --validate
  python -m flux.language.dual_encoder.runner --index
  python -m flux.language.dual_encoder.runner --all        # runs 1 → 2 → validate → index

Run from FLUX project root:
  cd /home/akhil/Documents/FLUX && python -m flux.language.dual_encoder.runner --all
"""
from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dual_encoder.runner")


def _get_store():
    from flux.kg.store import get_store
    return get_store()


def run_phase1(store) -> None:
    from flux.language.dual_encoder import anchor_target_generator as atg
    from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder
    from flux.language.dual_encoder import phase1_trainer

    nodes = store.all_nodes()

    log.info("=== Phase 1: Generate (query, definition) pairs ===")
    pairs = atg.generate(nodes, force=True)   # force=True to regenerate with new format
    log.info("Generated %d (query, def) pairs", len(pairs))

    log.info("=== Phase 1: InfoNCE query-definition alignment ===")
    encoder = KGNodeEncoder()
    result  = phase1_trainer.train(encoder, pairs)
    log.info("Phase 1 complete: %s", result)


def run_phase2(store) -> None:
    from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder, PHASE1_CKPT
    from flux.language.dual_encoder import anchor_target_generator as atg
    from flux.language.dual_encoder import triplet_generator as tg
    from flux.language.dual_encoder import phase2_trainer

    if not PHASE1_CKPT.exists():
        log.error("Phase 1 checkpoint not found. Run --phase 1 first.")
        sys.exit(1)

    nodes = store.all_nodes()

    # Load cached anchor targets (generated in Phase 1) for joint loss
    anchor_targets = atg.load()
    if anchor_targets is None:
        log.warning("No cached anchor targets — Phase 2 will run InfoNCE only (no alignment term).")

    log.info("=== Phase 2: Generate triplets ===")
    triplets = tg.generate_triplets(nodes, store=store)
    train_set, val_set = tg.split(triplets)
    log.info("Triplets: %d train / %d val", len(train_set), len(val_set))

    log.info("=== Phase 2: InfoNCE + alignment training ===")
    encoder = KGNodeEncoder.load(PHASE1_CKPT)
    result  = phase2_trainer.train(encoder, train_set, val_set, anchor_targets=anchor_targets)
    log.info("Phase 2 complete: %s", result)


def run_validate(store) -> None:
    from flux.language.dual_encoder.kg_node_encoder import KGNodeEncoder, PHASE2_BEST, PHASE1_CKPT
    from flux.language.dual_encoder import validator

    ckpt = PHASE2_BEST if PHASE2_BEST.exists() else PHASE1_CKPT
    if not ckpt.exists():
        log.error("No trained checkpoint found. Run --phase 1 and/or --phase 2 first.")
        sys.exit(1)

    log.info("=== Validation: loading %s ===", ckpt.name)
    encoder = KGNodeEncoder.load(ckpt)
    result  = validator.validate(encoder, store.all_nodes(), store=store)

    print("\n── Validation Results ──────────────────────────────")
    print(f"  Query→Node alignment:   {result.query_to_node_mean:.4f}  (target >0.65)")
    print(f"  Related pair (KG edge): {result.related_pair_mean:.4f}  (target >0.55)")
    print(f"  Cross-domain:           {result.cross_domain_mean:.4f}  (target <0.35)")
    print(f"  2-hop hard negatives:   {result.within_domain_2hop_mean:.4f}  (target <0.45)")
    print(f"  Gap (q→n vs cross):     {result.gap_query_vs_cross:.4f}")
    print(f"  {'✓ PASSED' if result.passed else '✗ FAILED'}")
    print()


def run_index(store) -> None:
    from flux.language.dual_encoder import dual_encoder_index

    log.info("=== Building node embedding cache ===")
    n = dual_encoder_index.build(store.all_nodes())
    log.info("Index built: %d nodes", n)


def run_eval(store) -> None:
    """Quick eval: run validate + print sample query results."""
    run_validate(store)

    from flux.language.dual_encoder import dual_encoder_index
    if not dual_encoder_index.load():
        run_index(store)

    idx = dual_encoder_index.get_index()
    test_queries = [
        "what is entropy",
        "how does information theory work",
        "explain neural networks",
        "what is quantum superposition",
        "tell me about thermodynamics",
    ]
    print("── Sample Query Results ─────────────────────────────")
    for q in test_queries:
        hits = idx.query_one(q, top_k=3)
        print(f"  '{q}'")
        for cid, score in hits:
            print(f"    → {cid:<40} {score:.4f}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dual encoder training pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--phase",    type=int, choices=[1, 2], help="Run training phase 1 or 2")
    parser.add_argument("--validate", action="store_true",      help="Run validation on best checkpoint")
    parser.add_argument("--eval",     action="store_true",      help="Validate + sample query results")
    parser.add_argument("--index",    action="store_true",      help="Build node embedding cache")
    parser.add_argument("--all",      action="store_true",      help="Run phases 1→2→validate→index")
    args = parser.parse_args()

    if not any([args.phase, args.validate, args.eval, args.index, args.all]):
        parser.print_help()
        sys.exit(0)

    store = _get_store()

    if args.phase == 1 or args.all:
        run_phase1(store)
    if args.phase == 2 or args.all:
        run_phase2(store)
    if args.validate or args.all:
        run_validate(store)
    if args.eval:
        run_eval(store)
    if args.index or args.all:
        run_index(store)


if __name__ == "__main__":
    main()
