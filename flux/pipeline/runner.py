"""
flux/pipeline/runner.py

Orchestrates all five pipeline stages in dependency order:
  kg_seed -> session_builder -> consolidation_builder -> spec_builder -> meta_builder

Usage:
  python -m flux.pipeline.runner                        # delta run (default)
  python -m flux.pipeline.runner --force                # full rebuild from scratch
  python -m flux.pipeline.runner --from session_builder # rebuild from stage N onwards
  python -m flux.pipeline.runner --only meta_builder    # run exactly one stage

Exit codes: 0 = pipeline VALID, 1 = stage failure, 2 = pipeline INVALID (validation errors)
"""

from __future__ import annotations
import argparse, sys, time
from flux.pipeline import (
    kg_seed,
    session_builder,
    consolidation_builder,
    spec_builder,
    meta_builder,
    schema as S,
)

# -- Stage Registry ------------------------------------------------------------
# Order matters: each stage depends on the output of all stages before it.
# supports_delta: whether build() accepts a delta kwarg.

STAGE_NAMES = [
    "kg_seed",
    "session_builder",
    "consolidation_builder",
    "spec_builder",
    "meta_builder",
]

_MODULES = {
    "kg_seed":               kg_seed,
    "session_builder":       session_builder,
    "consolidation_builder": consolidation_builder,
    "spec_builder":          spec_builder,
    "meta_builder":          meta_builder,
}

# Stages that accept build(delta=...) — kg_seed and meta_builder always overwrite.
_DELTA_CAPABLE = {"session_builder", "consolidation_builder", "spec_builder"}

# -- Result Formatting ---------------------------------------------------------

def _fmt(name: str, r: dict) -> str:
    if name == "kg_seed":
        return (f"nodes={r['node_count']}  edges={r['edge_count']}  "
                f"specs={r['spec_count']}  gaps={len(r.get('gaps', {}))} known")
    if name == "session_builder":
        return (f"syn={r['synthetic']}  neg={r['negative']}  "
                f"real={r['real']}  skip={r['skipped_delta']}  "
                f"vocab={r['vocab_size']} tokens")
    if name in ("consolidation_builder", "spec_builder"):
        orphan = f"  orphaned={r['orphaned']}" if "orphaned" in r else ""
        return f"written={r['written']}  skip={r['skipped']}{orphan}"
    if name == "meta_builder":
        drift = "\u26a0 VOCAB DRIFT — reinit embeddings" if r["vocab"]["vocab_drifted"] else "vocab stable"
        return f"{r['pipeline_status']}  {drift}"
    return repr(r)

# -- Runner --------------------------------------------------------------------

def run(stages: list[str], delta: bool) -> int:
    """
    Run the specified stages in order.
    Returns 0 on success, 1 on exception, 2 on pipeline INVALID.
    """
    S.clear_all_gaps()
    total = len(stages)
    all_results: dict[str, dict] = {}

    for i, name in enumerate(stages, 1):
        mod = _MODULES[name]
        capable = name in _DELTA_CAPABLE

        print(f"  [{i}/{total}] {name:<25}", end="", flush=True)
        t0 = time.monotonic()
        try:
            result = mod.build(delta=delta) if capable else mod.build()
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f"  FAILED  ({elapsed:.1f}s)")
            print(f"          {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            return 1

        elapsed = time.monotonic() - t0
        print(f"  done  ({elapsed:.2f}s)  {_fmt(name, result)}")
        all_results[name] = result

    # Final pipeline health from meta
    if "meta_builder" in all_results:
        status = all_results["meta_builder"]["pipeline_status"]
        errors = all_results["meta_builder"]["validation"]["errors"]
        if status != "VALID":
            print(f"\n[runner] Pipeline status: {status}")
            for e in errors:
                print(f"         ERROR: {e}")
            return 2

    return 0

# -- CLI -----------------------------------------------------------------------

def _parse() -> tuple[list[str], bool]:
    p = argparse.ArgumentParser(
        prog="python -m flux.pipeline.runner",
        description="FLUX knowledge graph training pipeline orchestrator",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--force",
        action="store_true",
        help="Full rebuild — ignore existing outputs, rewrite all files",
    )
    mode.add_argument(
        "--from",
        dest="from_stage",
        metavar="STAGE",
        choices=STAGE_NAMES,
        help="Run from STAGE onwards (full rebuild for that stage and all downstream)",
    )
    mode.add_argument(
        "--only",
        metavar="STAGE",
        choices=STAGE_NAMES,
        help="Run exactly one stage (delta mode — skips if no change)",
    )
    args = p.parse_args()

    if args.force:
        return STAGE_NAMES, False   # all stages, no delta
    if args.from_stage:
        idx = STAGE_NAMES.index(args.from_stage)
        return STAGE_NAMES[idx:], False   # from stage N onwards, no delta
    if args.only:
        return [args.only], True    # single stage, delta=True
    return STAGE_NAMES, True        # default: all stages, delta=True


if __name__ == "__main__":
    stages, delta = _parse()
    mode_label = "full rebuild" if not delta else "delta"
    print(f"[runner] FLUX pipeline — {mode_label}  ({len(stages)} stage(s))")
    sys.exit(run(stages, delta))
