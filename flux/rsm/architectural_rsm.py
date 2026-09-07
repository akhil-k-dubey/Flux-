"""
flux/rsm/architectural_rsm.py

L3D — Architectural RSM Orchestrator

Connects L3A → L3B → L3C into a single pipeline and provides the
CLI entry point for architectural review.

Architecture:
  L3A (bottleneck_detector)  — runs in every end_session(), writes fired
                                reports to logs/layer3_diagnoses.jsonl
  L3B (candidate_builder     — builds ArchitecturalProposal from the report
       + isolation_runner)   — trains candidate in subprocess, returns CandidateReport
  L3C (architectural_approval) — FluxEvaluation + human decision (A/R/S)

The orchestrator runs L3B+L3C on demand (not in end_session) because
L3C requires interactive input — it cannot run headlessly during a session.

Usage:
  python -m flux.rsm.architectural_rsm              # interactive review loop
  python -m flux.rsm.architectural_rsm --status     # show pending reports + queue
  python -m flux.rsm.architectural_rsm --run-one    # process oldest pending report
  python -m flux.rsm.architectural_rsm --inject-test

end_session() integration:
  The L3A observe() call is already wired into flux_interface.py end_session().
  When L3A fires (3 consecutive trigger sessions), the BottleneckReport is:
    - Appended to logs/layer3_diagnoses.jsonl
    - Included in the end_session() summary as summary["l3a_bottleneck"]
  The user then runs 'python -m flux.rsm.architectural_rsm' to review.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

FLUX_ROOT        = Path(__file__).resolve().parents[2]
L3_DIAGNOSE_PATH = FLUX_ROOT / "logs" / "layer3_diagnoses.jsonl"
L3_QUEUE_PATH    = FLUX_ROOT / "logs" / "layer3_queue.jsonl"
APPROVALS_LOG    = FLUX_ROOT / "logs" / "layer3_approvals.jsonl"
REJECTIONS_LOG   = FLUX_ROOT / "logs" / "layer3_rejections.jsonl"
ARCH_CONFIG      = FLUX_ROOT / "checkpoints" / "flux_architecture.json"
BOTTLENECK_HIST  = FLUX_ROOT / "logs" / "bottleneck_history.jsonl"

# Training config for isolation runner (tunable per run)
DEFAULT_TRAIN_EPOCHS = 50
DEFAULT_LR           = 5e-5


# ── Report state helpers ───────────────────────────────────────────────────────

def _processed_report_ids() -> set[str]:
    """
    Return the set of report_ids that have already been acted on
    (APPROVED, REJECTED, or currently TRAINING_STARTED in the queue).
    Skipped reports are NOT considered processed — they get re-surfaced.
    """
    ids: set[str] = set()
    if not L3_QUEUE_PATH.exists():
        return ids
    terminal = {"APPROVED", "REJECTED", "TRAINING_STARTED", "TRAINING_EVALUATED"}
    for line in L3_QUEUE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("event") in terminal:
            rid = rec.get("trigger_report_id") or rec.get("proposal_id", "")
            # trigger_report_id is on proposal records; for queue entries from
            # isolation_runner the proposal_id links back to the report via naming
            if rid:
                ids.add(rid)
    return ids


def _pending_reports() -> list[dict]:
    """
    Return fired BottleneckReports from layer3_diagnoses.jsonl that have
    not yet been fully processed (no APPROVED/REJECTED event in the queue).
    Ordered oldest-first.
    """
    if not L3_DIAGNOSE_PATH.exists():
        return []
    processed = _processed_report_ids()
    pending   = []
    for line in L3_DIAGNOSE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("fires") and rec.get("report_id") not in processed:
            pending.append(rec)
    return pending


def _queue_events(last_n: int = 10) -> list[dict]:
    if not L3_QUEUE_PATH.exists():
        return []
    lines = [l for l in L3_QUEUE_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines[-last_n:]]


def _last_approval() -> Optional[dict]:
    if not APPROVALS_LOG.exists():
        return None
    lines = [l for l in APPROVALS_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def _last_rejection() -> Optional[dict]:
    if not REJECTIONS_LOG.exists():
        return None
    lines = [l for l in REJECTIONS_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def _last_bottleneck_snapshot() -> Optional[dict]:
    if not BOTTLENECK_HIST.exists():
        return None
    lines = [l for l in BOTTLENECK_HIST.read_text(encoding="utf-8").splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


# ── Status display ─────────────────────────────────────────────────────────────

def print_status() -> None:
    """Print a compact dashboard of the L3 RSM state."""
    print("\n" + "═" * 66)
    print("  L3 Architectural RSM — Status")
    print("═" * 66)

    # Active architecture
    if ARCH_CONFIG.exists():
        arch = json.loads(ARCH_CONFIG.read_text())
        promoted = arch.get("promoted_from", "baseline")
        print(f"\n  Active architecture (promoted from {promoted}):")
        print(f"    hidden_dim={arch['hidden_dim']}  "
              f"cons_bottleneck={arch['cons_bottleneck']}  "
              f"dropout={arch['dropout']}  "
              f"lr_schedule={arch['lr_schedule']}")
    else:
        print("\n  Active architecture: baseline defaults (hidden_dim=128, cons_bottleneck=32)")

    # Latest bottleneck signal snapshot
    snap = _last_bottleneck_snapshot()
    if snap:
        fired = snap.get("triggered", [])
        print(f"\n  Last L3A snapshot (session={snap.get('session_id', '?')[:12]}):")
        print(f"    latency={snap.get('latency_ms', 0):.1f}ms  "
              f"fisher_sat={snap.get('fisher_sat', 0):.3f}  "
              f"plateau_var={snap.get('plateau_var', 0):.3f}  "
              f"ewc_forget={snap.get('ewc_forget', 0):.4f}")
        print(f"    triggered={fired or 'none'}")
    else:
        print("\n  Last L3A snapshot: none yet")

    # Pending reports
    pending = _pending_reports()
    print(f"\n  Pending bottleneck reports: {len(pending)}")
    for r in pending[:3]:
        print(f"    {r['report_id']}  signals={r['triggered_signals']}  "
              f"severity={r.get('severity', 0):.2f}")

    # Recent queue events
    events = _queue_events(8)
    if events:
        print(f"\n  Recent queue events:")
        for e in events:
            ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("timestamp", 0)))
            print(f"    [{ts}]  {e.get('event'):25s}  {e.get('proposal_id', '')[:30]}")

    # Last approval / rejection
    appr = _last_approval()
    rej  = _last_rejection()
    if appr:
        print(f"\n  Last approval:  {appr.get('proposal_id')}  "
              f"benchmark={appr.get('benchmark_score', 0):.3f}  "
              f"reason={appr.get('reason', '')[:30]}")
    if rej:
        print(f"  Last rejection: {rej.get('proposal_id')}  "
              f"reason={rej.get('reason', '')[:40]}")

    print("\n" + "═" * 66 + "\n")


# ── Core pipeline ──────────────────────────────────────────────────────────────

def run_one(
    report_dict:  dict,
    train_epochs: int   = DEFAULT_TRAIN_EPOCHS,
    lr:           float = DEFAULT_LR,
    auto_decision: Optional[str] = None,
    auto_reason:   Optional[str] = None,
) -> dict:
    """
    Run the full L3B + L3C pipeline for a single fired BottleneckReport.

    Steps:
      1. CandidateBuilder.build(report) → ArchitecturalProposal
      2. IsolationRunner.run(proposal)  → CandidateReport
      3. ArchitecturalApproval.run(report) → decision

    Returns result dict with {decision, action, proposal_id, flux_verdict, ...}.
    """
    import sys
    sys.path.insert(0, str(FLUX_ROOT))

    from flux.rsm.bottleneck_detector   import BottleneckReport
    from flux.rsm.candidate_builder     import CandidateBuilder
    from flux.rsm.isolation_runner      import IsolationRunner
    from flux.rsm.architectural_approval import ArchitecturalApproval
    from flux.kg.store                  import get_store
    from flux.interface.flux_interface  import FluxInterface

    report = BottleneckReport(**report_dict)

    log.info(
        "L3 pipeline: report=%s  signals=%s  severity=%.2f",
        report.report_id, report.triggered_signals, report.severity,
    )

    # L3B-1: build proposal
    print(f"\n  [L3B] Building candidate proposal for {report.report_id}...")
    store    = get_store()
    flux     = FluxInterface()
    builder  = CandidateBuilder(store, flux)
    proposal = builder.build(report)
    print(f"  [L3B] Proposal: {proposal.proposal_id}  change={proposal.change_type}")
    print(f"         delta: "
          + ", ".join(f"{k}: {_current(k)}->{v}"
                      for k, v in proposal.changes.items()
                      if v != _current(k))
          or "  (no delta)")

    # L3B-2: run in isolation
    print(f"\n  [L3B] Running isolation_runner (epochs={train_epochs})...")
    print(f"         This trains a candidate from scratch — takes ~20–60s on CUDA.\n")
    runner = IsolationRunner(train_epochs=train_epochs, lr=lr)
    cand   = runner.run(proposal)

    if cand.status == "ERROR":
        print(f"\n  [L3B] ERROR: {cand.error}")
        print(f"         Proposal archived. Try again after fixing the issue.\n")
        return {"decision": "error", "error": cand.error, "proposal_id": proposal.proposal_id}

    print(f"\n  [L3B] Candidate evaluated:")
    print(f"         {cand.summary_line}\n")

    # L3C: approval
    approval = ArchitecturalApproval()
    result   = approval.run(cand, auto_decision=auto_decision, auto_reason=auto_reason)
    return result


def _current(param: str):
    return {"hidden_dim": 128, "cons_bottleneck": 32, "dropout": 0.1,
            "lr_schedule": "constant", "embed_dim": 32}.get(param)


# ── Interactive review loop ────────────────────────────────────────────────────

def interactive_review(
    train_epochs: int   = DEFAULT_TRAIN_EPOCHS,
    lr:           float = DEFAULT_LR,
) -> None:
    """
    Present each pending bottleneck report for review, one at a time.
    """
    pending = _pending_reports()

    if not pending:
        print("\n  No pending bottleneck reports — Flux is healthy.")
        print("  (L3A fires after 3 consecutive triggering sessions.)\n")
        print_status()
        return

    print(f"\n  {len(pending)} bottleneck report(s) pending review.")
    print_status()

    for i, rep in enumerate(pending):
        print(f"\n  Report {i+1}/{len(pending)}: {rep['report_id']}")
        result = run_one(rep, train_epochs=train_epochs, lr=lr)
        print(f"  Decision: {result.get('decision')}  action={result.get('action')}")

        # After an approve, stop — the architecture just changed
        if result.get("action") == "PROMOTED":
            print("\n  Architecture promoted. Stopping review loop.")
            print("  Run 'python -m flux.rsm.architectural_rsm --status' to verify.\n")
            break


# ── Inject test ────────────────────────────────────────────────────────────────

def _run_inject_test() -> None:
    """
    End-to-end inject test: synthesise a BottleneckReport, write it to
    layer3_diagnoses.jsonl, then run the full orchestrator pipeline.
    Verifies the entire L3A→L3B→L3C chain runs without errors.
    Auto-skips the approval (no live checkpoint modified).
    """
    import sys
    sys.path.insert(0, str(FLUX_ROOT))

    from flux.rsm.bottleneck_detector import BottleneckReport
    from dataclasses import asdict

    print("=== L3D ArchitecturalRSM — Inject Test ===\n")

    # Write a synthetic fired BottleneckReport to diagnoses log
    report = BottleneckReport(
        report_id          = f"l3a_inject_{int(time.time())}",
        timestamp          = time.time(),
        session_id         = "inject_test_orchestrator",
        triggered_signals  = ["plateau_var"],
        signal_values      = {"latency_ms": 64.0, "fisher_sat": 0.000,
                              "plateau_var": 0.280, "ewc_forget": 0.000},
        consecutive_counts = {"latency_ms": 0, "fisher_sat": 0,
                              "plateau_var": 3, "ewc_forget": 0},
        severity           = 0.28,
        fires              = True,
        evidence           = [
            "  [plateau_var] benchmark reasoning variance = 0.280 "
            "(>0.250, fired 3 consecutive sessions)"
        ],
    )

    # Write to diagnoses log so _pending_reports() picks it up
    L3_DIAGNOSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with L3_DIAGNOSE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(report)) + "\n")
    print(f"Step 1: Wrote synthetic BottleneckReport → {L3_DIAGNOSE_PATH}")
    print(f"  report_id={report.report_id}  signals={report.triggered_signals}")

    # Confirm it shows up as pending
    pending = _pending_reports()
    our_pending = [r for r in pending if r["report_id"] == report.report_id]
    ok1 = len(our_pending) == 1
    print(f"\nStep 2: _pending_reports() sees it: {'[OK]' if ok1 else '[FAIL]'}  "
          f"({len(pending)} total pending)")

    # Run the full pipeline with auto_decision="skip" (no live checkpoint touched)
    print(f"\nStep 3: run_one() — full L3B+L3C pipeline (auto-skip at approval)")
    print(f"  plateau_var triggered -> DEEPEN (cons_bottleneck 32->64 + cosine LR)")
    print(f"  Training 20 epochs on isolated corpus snapshot...\n")

    result = run_one(
        asdict(report),
        train_epochs  = 20,
        auto_decision = "skip",
        auto_reason   = "inject-test: not modifying live checkpoint",
    )

    ok2 = result.get("action") == "SKIPPED"
    ok3 = result.get("flux_verdict") in ("APPROVE", "UNCERTAIN", "REJECT")
    ok4 = "proposal_id" in result

    print(f"\nStep 4: Verify result")
    print(f"  {'[OK]' if ok2 else '[FAIL]'} action=SKIPPED")
    print(f"  {'[OK]' if ok3 else '[FAIL]'} flux_verdict={result.get('flux_verdict')}")
    print(f"  {'[OK]' if ok4 else '[FAIL]'} proposal_id present: {result.get('proposal_id', '')[:40]}")

    # Status display
    print(f"\nStep 5: print_status()")
    print_status()

    all_ok = ok1 and ok2 and ok3 and ok4
    print(f"=== {'All checks PASSED' if all_ok else 'SOME CHECKS FAILED'} ===\n")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(levelname)-7s | %(name)s | %(message)s",
    )
    sys.path.insert(0, str(FLUX_ROOT))

    p = argparse.ArgumentParser(
        description="L3 Architectural RSM — review and approve/reject architectural candidates"
    )
    p.add_argument("--status",       action="store_true",
                   help="Show L3 RSM status dashboard and exit")
    p.add_argument("--run-one",      action="store_true",
                   help="Process the oldest pending bottleneck report and exit")
    p.add_argument("--inject-test",  action="store_true",
                   help="Run full E2E inject test (no live checkpoint modified)")
    p.add_argument("--auto-approve", action="store_true",
                   help="Auto-approve the candidate (USE WITH CARE)")
    p.add_argument("--auto-reject",  action="store_true",
                   help="Auto-reject the candidate")
    p.add_argument("--reason",       default=None,
                   help="Reason string for auto-approve/reject")
    p.add_argument("--epochs",       type=int, default=DEFAULT_TRAIN_EPOCHS,
                   help=f"Training epochs for isolation_runner (default={DEFAULT_TRAIN_EPOCHS})")
    p.add_argument("--lr",           type=float, default=DEFAULT_LR,
                   help=f"Learning rate (default={DEFAULT_LR})")
    args = p.parse_args()

    auto = ("approve" if args.auto_approve
            else "reject" if args.auto_reject
            else None)

    if args.inject_test:
        _run_inject_test()

    elif args.status:
        print_status()

    elif args.run_one:
        pending = _pending_reports()
        if not pending:
            print("\n  No pending bottleneck reports. Flux is healthy.\n")
            print_status()
        else:
            result = run_one(
                pending[0],
                train_epochs   = args.epochs,
                lr             = args.lr,
                auto_decision  = auto,
                auto_reason    = args.reason,
            )
            print(json.dumps(result, indent=2, default=str))

    else:
        # Default: interactive review loop
        interactive_review(train_epochs=args.epochs, lr=args.lr)
