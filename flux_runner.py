#!/usr/bin/env python3
"""
flux_runner.py

Flux autonomous runner — keeps Flux alive and self-processing indefinitely.

What it does every cycle:
  1. Self-inquiry: picks concepts from her own KG and asks herself questions
     (EXPLAIN, CONNECT, GAP) — this generates real sensor observations,
     drift measurements, and consequence nodes just like a human session would.
  2. Every SESSION_CYCLES cycles: calls end_session() which triggers
     Stage C → Stage B → RSM (gap proposals) → Layer 2 (reasoning RSM)

Usage:
    python flux_runner.py                   # default: 30s cycle, session every 5 cycles
    python flux_runner.py --cycle 10        # faster: 10s per cycle
    python flux_runner.py --session 3       # end_session every 3 cycles
    python flux_runner.py --once            # run exactly one cycle then exit
    python flux_runner.py --verbose         # show full response text

Stop anytime with Ctrl+C — safe, won't corrupt state.
"""
from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── Inquiry templates ────────────────────────────────────────────────────────
# Flux cycles through these to generate varied sensor observations

_EXPLAIN_TMPL    = ["what is {c}?", "tell me about {c}", "{c}"]
_CONNECT_TMPL    = ["how does {a} relate to {b}?", "how does {a} connect to {b}?",
                    "what is the relationship between {a} and {b}?"]
_GAP_TMPL        = ["what don't you know about {c}?"]


def _pick_concepts(store, n: int = 2) -> list[str]:
    """Pick n random trainable concept IDs from the KG, filtering stale/noisy nodes."""
    nodes = store.all_nodes()
    # Only pick properly taught concepts — exclude auto-generated, LUMEN-internal,
    # and low-quality nodes that produce nonsensical CONNECT queries
    SKIP_TYPES = frozenset({
        "ACTION_CONSEQUENCE_RECORD", "META", "AKHIL",
        "RSM_PROPOSAL", "SYSTEM",
    })
    SKIP_PREFIXES = (
        "consequence_query_", "lumen_", "flux_rk4",
        "unknown_unknown", "the_unknown",
    )
    trainable = [
        nid for nid, nd in nodes.items()
        if nd.get("trainable", True)
        and nd.get("type") not in SKIP_TYPES
        and nd.get("source") == "TAUGHT"   # only what Akhil explicitly taught
        and nd.get("confidence", 0) > 0.6  # raised threshold for cleaner queries
        and not any(nid.startswith(pfx) for pfx in SKIP_PREFIXES)
    ]
    return random.sample(trainable, min(n, len(trainable))) if trainable else []


def _build_query(store) -> str:
    """Construct one self-inquiry query from available concepts."""
    concepts = _pick_concepts(store, 2)
    if len(concepts) == 0:
        return "what is entropy?"
    if len(concepts) == 1 or random.random() < 0.4:
        c = concepts[0].replace("_", " ")
        tmpl = random.choice(_EXPLAIN_TMPL + _GAP_TMPL)
        return tmpl.format(c=c)
    a, b = concepts[0].replace("_", " "), concepts[1].replace("_", " ")
    return random.choice(_CONNECT_TMPL).format(a=a, b=b)


def run(cycle_secs: float = 30.0,
        session_every: int = 5,
        verbose: bool = False,
        once: bool = False) -> None:

    from flux.interface import FluxInterface
    flux = FluxInterface()

    print(f"\n{'='*60}")
    print(f"  FLUX AUTONOMOUS RUNNER")
    print(f"  Cycle: {cycle_secs}s  |  Session flush every {session_every} cycles")
    print(f"  KG nodes loaded: {len(flux.store.all_nodes())}")
    print(f"  Stop with Ctrl+C")
    print(f"{'='*60}\n")

    # Graceful shutdown on Ctrl+C
    running   = True
    def _stop(sig, frame):
        nonlocal running
        print("\n\n[Runner] Ctrl+C received — finishing current cycle then stopping...")
        running = False
    signal.signal(signal.SIGINT, _stop)

    cycle_num  = 0
    session_num = 0

    while running:
        cycle_num += 1
        t0 = time.perf_counter()

        # ── Self-inquiry ──────────────────────────────────────────────────
        query = _build_query(flux.store)


        try:
            response = flux.ask(query)
            elapsed  = round((time.perf_counter() - t0) * 1000)
            # Header: cycle + query + timing (one atomic line, no truncation mid-word)
            print(f"\n[Cycle {cycle_num:4d}] [{elapsed}ms]  Q: {query}")
            # Full response — wrap at 100 chars so definitions aren't cut off
            for line in response.splitlines():
                # word-wrap long lines
                while len(line) > 100:
                    print(f"  {line[:100]}")
                    line = line[100:]
                if line.strip():
                    print(f"  {line}")
            if verbose:
                print()   # blank line separator in verbose mode
        except Exception as exc:
            print(f"[Cycle {cycle_num:4d}] ERROR: {exc}")

        # ── Session flush ─────────────────────────────────────────────────
        if cycle_num % session_every == 0:
            session_num += 1
            print(f"\n[Session {session_num}] Flushing — Stage C → B → RSM → Layer 2...")
            t_s = time.perf_counter()
            try:
                summary = flux.end_session()
                elapsed_s = round(time.perf_counter() - t_s, 1)
                _print_session_summary(summary, elapsed_s)
            except Exception as exc:
                print(f"  [Session] ERROR: {exc}")
            print()

        if once:
            break

        # ── Wait for next cycle ───────────────────────────────────────────
        if running:
            time.sleep(cycle_secs)

    # Final flush on exit
    if not once and cycle_num % session_every != 0:
        print(f"\n[Runner] Final session flush before exit...")
        try:
            summary = flux.end_session()
            _print_session_summary(summary, 0)
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print(f"\n[Runner] Stopped after {cycle_num} cycles, {session_num} sessions.")


def _print_session_summary(summary: dict, elapsed: float) -> None:
    stage_c = summary.get("stage_c", {})
    rsm     = summary.get("rsm", {})
    l2      = summary.get("layer2_rsm", {})
    b_train = summary.get("stage_b_train", {})
    loss_str = f"loss={b_train['loss_final']:.4f}" if b_train.get("loss_final") else b_train.get("skipped_reason", "triggered")

    print(f"  Stage-C:  {stage_c}")
    if b_train:
        print(f"  Stage-B:  {loss_str}  ({b_train.get('trained_on', '?')} sessions)")
    if rsm:
        print(f"  RSM L1:   committed={rsm.get('committed',0)}  pending={rsm.get('pending',0)}")
    if l2:
        print(f"  RSM L2:   flagged={l2.get('flagged',0)}  "
              f"pending_approval={l2.get('pending_approval',0)}  "
              f"skipped={l2.get('skipped',{})}")
    print(f"  [{elapsed}s]")


def main():
    p = argparse.ArgumentParser(description="Flux autonomous runner")
    p.add_argument("--cycle",   type=float, default=8.0,
                   help="Seconds between self-inquiry cycles (default: 8)")
    p.add_argument("--session", type=int,   default=5,
                   help="End session every N cycles (default: 5)")
    p.add_argument("--verbose", action="store_true",
                   help="Print full response text")
    p.add_argument("--once",    action="store_true",
                   help="Run exactly one cycle then exit")
    args = p.parse_args()
    run(cycle_secs=args.cycle, session_every=args.session,
        verbose=args.verbose, once=args.once)


if __name__ == "__main__":
    main()
