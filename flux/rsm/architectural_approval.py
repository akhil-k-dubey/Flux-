"""
flux/rsm/architectural_approval.py

L3C — Architectural Approval Gate

Receives a CandidateReport from isolation_runner and drives the approval loop:

  1. FluxEvaluation  — Flux reads the numbers and produces a structured
                       verdict (APPROVE / REJECT / UNCERTAIN) with reasoning.
                       Deterministic threshold logic — not a free-form LLM call.

  2. CLI surface     — Proposal summary + Flux's verdict displayed in the
                       terminal. Human types approve/reject/skip + reason.

  3. Action          — Approve: archive live checkpoint, promote candidate,
                                write flux_architecture.json, archive EWC state.
                       Reject:  archive proposal workdir, log training signal.
                       Skip:    log SKIPPED to queue, no action.

Output files:
  logs/layer3_approvals.jsonl     — approved decisions
  logs/layer3_rejections.jsonl    — rejected decisions with reasons
  checkpoints/archive/            — archived live checkpoints (never deleted)
  checkpoints/flux_architecture.json  — active architecture (read by grounded_trainer)

Standalone:
  python -m flux.rsm.architectural_approval --inject-test
  python -m flux.rsm.architectural_approval --from-report <CandidateReport.json>
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

FLUX_ROOT      = Path(__file__).resolve().parents[2]
CKPT_DIR       = FLUX_ROOT / "checkpoints"
ARCHIVE_DIR    = CKPT_DIR  / "archive"
LIVE_LTC_CKPT  = CKPT_DIR  / "flux_grounded_best.pt"
LIVE_EWC_STATE = CKPT_DIR  / "flux_ewc_state.pt"
ARCH_CONFIG    = CKPT_DIR  / "flux_architecture.json"

APPROVALS_LOG  = FLUX_ROOT / "logs" / "layer3_approvals.jsonl"
REJECTIONS_LOG = FLUX_ROOT / "logs" / "layer3_rejections.jsonl"
L3_QUEUE_PATH  = FLUX_ROOT / "logs" / "layer3_queue.jsonl"

# ── Evaluation thresholds ──────────────────────────────────────────────────────
# Calibrated from Stage-B production outcomes:
#   - val_loss converges to ~0.33 after 8-10 epochs on 134 sessions.
#   - Candidate trained from scratch for 20-50 epochs expected: 0.30-0.45.
#   - > 0.60 means the model did not learn the task at all.

THRESHOLD_BENCHMARK_APPROVE    = 0.70
THRESHOLD_BENCHMARK_REJECT     = 0.50
THRESHOLD_VAL_LOSS_MAX         = 0.60
THRESHOLD_REGRESSION_APPROVE   = 1.00   # all 20/20 must pass
THRESHOLD_REGRESSION_UNCERTAIN = 0.80   # >= 80% pass -> uncertain (not reject)


# ── Current architecture defaults (for delta display) ─────────────────────────

_CURRENT = {
    "hidden_dim":      128,
    "cons_bottleneck": 32,
    "dropout":         0.1,
    "lr_schedule":     "constant",
    "embed_dim":       32,
}

def _is_changed(param, val):
    return val != _CURRENT.get(param)


# ── FluxEvaluation ─────────────────────────────────────────────────────────────

@dataclass
class FluxEvaluation:
    verdict:        str          # "APPROVE" | "REJECT" | "UNCERTAIN"
    confidence:     float        # 0.0-1.0
    reasoning:      list
    concerns:       list
    recommendation: str

    def to_dict(self):
        return asdict(self)

    @property
    def should_approve(self):
        return self.verdict == "APPROVE"

    @property
    def should_reject(self):
        return self.verdict == "REJECT"


def evaluate(report) -> FluxEvaluation:
    """
    Deterministic threshold logic producing FluxEvaluation.
    Priority: REJECT hard > REJECT soft > APPROVE > UNCERTAIN
    """
    reasoning, concerns = [], []
    bm  = report.benchmark_score
    reg = report.regression_passed / max(report.regression_total, 1)
    vl  = report.final_val_loss
    tl  = report.final_train_loss
    err = report.error

    # Hard reject: training error
    if err:
        return FluxEvaluation(
            verdict="REJECT", confidence=1.0, reasoning=[],
            concerns=[f"Training failed: {err}"],
            recommendation="Fix the error before re-proposing this architecture.",
        )

    # Hard reject: benchmark too low
    if bm < THRESHOLD_BENCHMARK_REJECT:
        return FluxEvaluation(
            verdict="REJECT", confidence=0.95, reasoning=[],
            concerns=[
                f"benchmark_score={bm:.3f} < reject threshold {THRESHOLD_BENCHMARK_REJECT:.2f}",
                f"Only {report.regression_passed}/{report.regression_total} regression concepts pass",
                "Architectural change destroys encoding capacity.",
            ],
            recommendation="Reject. Architecture loses too much encoding capacity.",
        )

    # Hard reject: model did not converge
    if vl > THRESHOLD_VAL_LOSS_MAX:
        return FluxEvaluation(
            verdict="REJECT", confidence=0.90, reasoning=[],
            concerns=[
                f"val_loss={vl:.4f} > max threshold {THRESHOLD_VAL_LOSS_MAX:.2f}",
                "Model did not converge on this corpus.",
            ],
            recommendation="Reject. Increase train_epochs or reconsider architecture size.",
        )

    # Hard reject: too many concept regressions
    if reg < THRESHOLD_REGRESSION_UNCERTAIN:
        return FluxEvaluation(
            verdict="REJECT", confidence=0.85, reasoning=[],
            concerns=[
                f"Regression rate {reg:.1%} < {THRESHOLD_REGRESSION_UNCERTAIN:.0%} minimum",
                f"{report.regression_total - report.regression_passed} concepts lost confidence",
                "Too many regressions — architectural change degrades existing knowledge.",
            ],
            recommendation="Reject. Architecture loses too many existing concepts.",
        )

    # Build positive evidence
    if bm >= THRESHOLD_BENCHMARK_APPROVE:
        reasoning.append(f"benchmark_score={bm:.3f} >= {THRESHOLD_BENCHMARK_APPROVE:.2f}")
    if reg >= THRESHOLD_REGRESSION_APPROVE:
        reasoning.append(
            f"Full regression suite passed: {report.regression_passed}/{report.regression_total}"
        )
    if vl <= 0.40:
        reasoning.append(f"val_loss={vl:.4f} — strong convergence")
    elif vl <= THRESHOLD_VAL_LOSS_MAX:
        reasoning.append(f"val_loss={vl:.4f} — acceptable convergence (< {THRESHOLD_VAL_LOSS_MAX:.2f})")
    if tl > 0 and vl > 0 and vl < tl * 1.20:
        reasoning.append(f"train/val gap minimal ({tl:.4f}/{vl:.4f}) — no overfitting detected")

    # Architecture-specific evidence
    arch    = report.architecture
    changes = {k: v for k, v in arch.items() if _is_changed(k, v)}
    for param, val in changes.items():
        cur = _CURRENT.get(param)
        if param == "hidden_dim" and val < cur:
            reasoning.append(
                f"SHRINK hidden_dim {cur}->={val}: "
                f"latency_p95={report.latency_p95_ms:.1f}ms (faster inference)"
            )
        elif param == "hidden_dim" and val > cur:
            reasoning.append(
                f"GROW hidden_dim {cur}->{val}: more capacity, converged at val_loss={vl:.4f}"
            )
        elif param == "dropout":
            reasoning.append(f"dropout {cur}->{val}: regularisation adjusted")
        elif param == "lr_schedule":
            reasoning.append(f"lr_schedule ->{val}: improved convergence profile")
        elif param == "cons_bottleneck":
            reasoning.append(f"cons_bottleneck {cur}->{val}: consolidation path adjusted")

    # Soft concerns
    if bm < 1.0:
        concerns.append(
            f"benchmark_score={bm:.3f} < 1.0: "
            f"{report.regression_total - report.regression_passed} concept(s) below threshold"
        )
    if tl > 0 and vl > tl * 1.20:
        concerns.append(
            f"val_loss ({vl:.4f}) is {(vl/tl-1)*100:.0f}% above train_loss — overfitting risk"
        )

    # Verdict
    if (bm >= THRESHOLD_BENCHMARK_APPROVE
            and reg >= THRESHOLD_REGRESSION_APPROVE
            and vl <= THRESHOLD_VAL_LOSS_MAX):
        conf = min(0.95, 0.60 + 0.20 * bm + 0.15 * (1 - vl / THRESHOLD_VAL_LOSS_MAX))
        change_str = ", ".join(
            f"{k} {_CURRENT.get(k)}->{v}" for k, v in changes.items()
        ) or "no change"
        return FluxEvaluation(
            verdict="APPROVE", confidence=conf,
            reasoning=reasoning, concerns=concerns,
            recommendation=(
                f"Promote candidate ({report.proposal.change_type}: {change_str}). "
                "Archive live checkpoint first."
            ),
        )

    return FluxEvaluation(
        verdict="UNCERTAIN", confidence=0.50,
        reasoning=reasoning, concerns=concerns,
        recommendation="Run more training epochs on candidate before deciding.",
    )


# ── CLI surface ────────────────────────────────────────────────────────────────

def _box(lines, width=66):
    W = width
    top = "╔" + "═" * W + "╗"
    sep = "╠" + "═" * W + "╣"
    bot = "╚" + "═" * W + "╝"
    rows = [top]
    for line in lines:
        if line == "---":
            rows.append(sep)
        else:
            content = (" " + line)[:W]
            rows.append("║" + content.ljust(W) + "║")
    rows.append(bot)
    return "\n".join(rows)


def display_report(report, evaluation: FluxEvaluation) -> None:
    prop  = report.proposal
    arch  = report.architecture
    changes = {k: v for k, v in arch.items() if _is_changed(k, v)}
    change_str = "  ".join(
        f"{k}: {_CURRENT.get(k)}->{v}" for k, v in changes.items()
    ) or "(baseline unchanged)"
    icon  = {"APPROVE": "APPROVE", "REJECT": "REJECT", "UNCERTAIN": "UNCERTAIN?"}.get(
        evaluation.verdict, evaluation.verdict
    )

    lines = [
        f"L3C: Architectural Candidate Ready for Review",
        "---",
        f"Proposal:   {prop.proposal_id}",
        f"Change:     {prop.change_type}  --  {change_str}",
        f"Triggered:  {', '.join(prop.triggered_signals)}",
        f"Reason:     {prop.hypothesis[:58]}",
        "---",
        "Training Results:",
        f"  benchmark_score   = {report.benchmark_score:.3f}",
        f"  regression        = {report.regression_passed}/{report.regression_total}",
        f"  train_loss        = {report.final_train_loss:.4f}  val_loss = {report.final_val_loss:.4f}",
        f"  latency_p95       = {report.latency_p95_ms:.1f}ms",
        f"  training_duration = {report.training_duration_s:.1f}s",
        "---",
        f"Flux's Evaluation:  [{icon}]  confidence={evaluation.confidence:.2f}",
    ]
    for r in evaluation.reasoning:
        lines.append(f"  [+] {r[:60]}")
    for c in evaluation.concerns:
        lines.append(f"  [!] {c[:60]}")
    lines.append(f"Recommendation: {evaluation.recommendation[:57]}")
    lines.append("---")
    lines.append("Decision:")
    lines.append("  [A] approve  +  reason")
    lines.append("  [R] reject   +  reason")
    lines.append("  [S] skip  (decide later)")
    lines.append("")

    print("\n" + _box(lines) + "\n")


def prompt_decision():
    import sys
    if not sys.stdin.isatty():
        log.warning("stdin is not a TTY -- defaulting to 'skip'")
        return "skip", "non-interactive mode"
    while True:
        try:
            choice = input("  Choice [A/R/S]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "skip", "interrupted"
        if choice in ("a", "approve"):
            reason = input("  Approve reason: ").strip() or "approved"
            return "approve", reason
        if choice in ("r", "reject"):
            reason = input("  Reject reason: ").strip() or "rejected"
            return "reject", reason
        if choice in ("s", "skip", ""):
            return "skip", "skipped by user"
        print("  Please enter A, R, or S.")


# ── Promotion mechanics ────────────────────────────────────────────────────────

def promote_candidate(report, reason: str) -> dict:
    """
    1. Archive live checkpoint  -> checkpoints/archive/flux_grounded_{ts}.pt
    2. Convert candidate weights -> live format, save to flux_grounded_best.pt
    3. Write checkpoints/flux_architecture.json
    4. Archive EWC state (shape incompatible with new architecture)
    5. Log to layer3_approvals.jsonl
    """
    import torch
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    # 1. Archive live checkpoint
    arch_path = None
    if LIVE_LTC_CKPT.exists():
        arch_path = ARCHIVE_DIR / f"flux_grounded_{ts}.pt"
        shutil.copy2(LIVE_LTC_CKPT, arch_path)
        log.info("Archived live checkpoint -> %s", arch_path)

    # 2. Load candidate and save in live format
    cand_weights = Path(report.workdir) / "output" / "candidate_weights.pt"
    if not cand_weights.exists():
        raise FileNotFoundError(f"Candidate weights not found: {cand_weights}")

    cand = torch.load(str(cand_weights), map_location="cpu")
    live = {
        "liquid_transformer": cand["model"],
        "consolidation_head": cand["head"],
        "epoch":              0,
        "grounded_sessions":  0,
        "promoted_from":      report.proposal_id,
        "promoted_at":        ts,
        "architecture":       report.architecture,
    }
    torch.save(live, str(LIVE_LTC_CKPT))
    log.info("Promoted candidate -> %s", LIVE_LTC_CKPT)

    # 3. Write architecture config so grounded_trainer picks up new dims
    arch_cfg = dict(report.architecture)
    arch_cfg.update({"promoted_from": report.proposal_id, "promoted_at": ts})
    ARCH_CONFIG.write_text(json.dumps(arch_cfg, indent=2), encoding="utf-8")
    log.info("Architecture config -> %s", ARCH_CONFIG)

    # 4. Archive EWC state — no longer valid for new architecture dims
    ewc_arch = None
    if LIVE_EWC_STATE.exists():
        ewc_arch = ARCHIVE_DIR / f"flux_ewc_state_{ts}.pt"
        shutil.move(str(LIVE_EWC_STATE), str(ewc_arch))
        log.info("EWC state archived -> %s  (will recompute on next Stage-B)", ewc_arch)

    # 5. Log
    record = {
        "event":              "APPROVED",
        "timestamp":          ts,
        "proposal_id":        report.proposal_id,
        "change_type":        report.proposal.change_type,
        "reason":             reason,
        "architecture":       report.architecture,
        "benchmark_score":    report.benchmark_score,
        "regression":         f"{report.regression_passed}/{report.regression_total}",
        "final_val_loss":     report.final_val_loss,
        "archived_live_ckpt": str(arch_path) if arch_path else None,
        "archived_ewc":       str(ewc_arch)  if ewc_arch  else None,
    }
    _append_log(APPROVALS_LOG, record)
    _append_log(L3_QUEUE_PATH, {
        "event": "APPROVED", "timestamp": float(ts),
        "proposal_id": report.proposal_id, "reason": reason,
    })
    log.warning(
        "PROMOTED %s  architecture=%s  "
        "NOTE: grounded_trainer now reads flux_architecture.json for dims.",
        report.proposal_id, report.architecture,
    )
    return record


def reject_candidate(report, reason: str) -> dict:
    """
    Move candidate workdir -> logs/candidates/rejected/{proposal_id}
    Log rejection as training signal.
    """
    rejected_dir = FLUX_ROOT / "logs" / "candidates" / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)

    src = Path(report.workdir)
    dst = rejected_dir / src.name
    if src.exists() and not dst.exists():
        shutil.move(str(src), str(dst))
        log.info("Rejected workdir archived -> %s", dst)

    record = {
        "event":              "REJECTED",
        "timestamp":          int(time.time()),
        "proposal_id":        report.proposal_id,
        "change_type":        report.proposal.change_type,
        "triggered_signals":  report.proposal.triggered_signals,
        "reason":             reason,
        "architecture":       report.architecture,
        "benchmark_score":    report.benchmark_score,
        "regression":         f"{report.regression_passed}/{report.regression_total}",
        "final_val_loss":     report.final_val_loss,
        "workdir_archived":   str(dst),
    }
    _append_log(REJECTIONS_LOG, record)
    _append_log(L3_QUEUE_PATH, {
        "event": "REJECTED", "timestamp": time.time(),
        "proposal_id": report.proposal_id, "reason": reason,
    })
    log.info("Rejected %s -- %s", report.proposal_id, reason)
    return record


# ── ArchitecturalApproval (main class) ────────────────────────────────────────

class ArchitecturalApproval:
    """
    Drives the full L3C loop.

    result = ArchitecturalApproval().run(candidate_report)
    # result["decision"] in ("approve", "reject", "skip")
    """

    def run(self, report, auto_decision: str = None, auto_reason: str = None) -> dict:
        evaluation = evaluate(report)

        log.info(
            "FluxEvaluation: verdict=%s  confidence=%.2f  "
            "benchmark=%.3f  regression=%d/%d  val_loss=%.4f",
            evaluation.verdict, evaluation.confidence,
            report.benchmark_score, report.regression_passed,
            report.regression_total, report.final_val_loss,
        )

        display_report(report, evaluation)

        if auto_decision:
            decision = auto_decision
            reason   = auto_reason or f"auto-{auto_decision}"
            log.info("Auto-decision: %s -- %s", decision, reason)
        else:
            decision, reason = prompt_decision()

        result = {
            "decision":        decision,
            "reason":          reason,
            "proposal_id":     report.proposal_id,
            "flux_verdict":    evaluation.verdict,
            "flux_confidence": evaluation.confidence,
            "timestamp":       time.time(),
        }

        if decision == "approve":
            try:
                summary = promote_candidate(report, reason)
                result.update({"action": "PROMOTED", "summary": summary})
                print("\n  [OK] Promoted. Live model updated. EWC state archived.")
                print("       NOTE: grounded_trainer reads flux_architecture.json on next run.")
                print(f"       Archive: checkpoints/archive/\n")
            except Exception as exc:
                result.update({"action": "PROMOTION_FAILED", "error": str(exc)})
                log.error("Promotion failed: %s", exc)
                print(f"\n  [ERR] Promotion error: {exc}\n")

        elif decision == "reject":
            summary = reject_candidate(report, reason)
            result.update({"action": "REJECTED", "summary": summary})
            print("\n  [REJECTED] Candidate archived. Reason logged as training signal.\n")

        else:
            _append_log(L3_QUEUE_PATH, {
                "event": "SKIPPED", "timestamp": time.time(),
                "proposal_id": report.proposal_id, "reason": reason,
            })
            result.update({"action": "SKIPPED"})
            print("\n  [SKIP] Proposal remains pending in layer3_queue.jsonl.\n")

        return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _append_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ── Inject test ────────────────────────────────────────────────────────────────

def _run_inject_test() -> None:
    import sys, tempfile
    sys.path.insert(0, str(FLUX_ROOT))
    from flux.rsm.isolation_runner  import CandidateReport
    from flux.rsm.candidate_builder import ArchitecturalProposal

    print("=== L3C ArchitecturalApproval -- Inject Test ===\n")

    def _fake_prop():
        return ArchitecturalProposal(
            proposal_id         = f"inject_{int(time.time())}",
            timestamp           = time.time(),
            trigger_report_id   = "l3a_test",
            triggered_signals   = ["latency_ms"],
            change_type         = "SHRINK",
            hypothesis          = "SHRINK: hidden_dim 128->64 to cut latency",
            changes             = {"hidden_dim": 64, "cons_bottleneck": 32,
                                   "dropout": 0.1, "lr_schedule": "constant", "embed_dim": 32},
            rationale           = ["  [latency_ms] 650ms > 500ms, 3 consecutive sessions"],
            regression_concepts = [f"c_{i}" for i in range(20)],
            baseline_responses  = {f"c_{i}": f"response {i}" for i in range(20)},
        )

    def _fake_report(bm, reg_pass, reg_total, vl, error=None):
        wd = tempfile.mkdtemp(prefix="l3c_inject_")
        # Create fake output dir so reject_candidate move works cleanly
        (Path(wd) / "output").mkdir()
        prop = _fake_prop()
        return CandidateReport(
            proposal_id=prop.proposal_id, proposal=prop,
            benchmark_score=bm, regression_passed=reg_pass, regression_total=reg_total,
            regression_details={f"c_{i}": {"passed": i < reg_pass, "baseline_len": 10,
                                            "candidate_len": 10, "key_terms_found": 2,
                                            "key_terms_total": 5} for i in range(reg_total)},
            latency_p95_ms=1.8, training_duration_s=19.0,
            final_train_loss=0.305, final_val_loss=vl,
            architecture=prop.changes, mode="architecture",
            status="EVALUATED" if not error else "ERROR",
            error=error, workdir=wd, timestamp=time.time(),
        )

    approval = ArchitecturalApproval()
    all_ok   = True

    # Scenario A: APPROVE verdict, auto-skip (don't overwrite live ckpt in test)
    print("Scenario A: Strong candidate (bm=1.0, 20/20, vl=0.33) -- expect APPROVE")
    rpt_a  = _fake_report(1.0, 20, 20, 0.33)
    eval_a = evaluate(rpt_a)
    ok_a   = eval_a.verdict == "APPROVE"
    print(f"  {'[OK]' if ok_a else '[FAIL]'} verdict={eval_a.verdict}  confidence={eval_a.confidence:.2f}")
    for r in eval_a.reasoning: print(f"    + {r}")
    for c in eval_a.concerns:  print(f"    ! {c}")
    res_a  = approval.run(rpt_a, auto_decision="skip", auto_reason="inject-test skip")
    ok_a2  = res_a["action"] == "SKIPPED"
    print(f"  {'[OK]' if ok_a2 else '[FAIL]'} action=SKIPPED (skipped to preserve live ckpt)")
    all_ok = all_ok and ok_a and ok_a2
    print()

    # Scenario B: REJECT verdict (training error), auto-reject
    print("Scenario B: Training error -- expect REJECT")
    rpt_b  = _fake_report(0.0, 0, 20, 0.0, error="CUDA OOM")
    eval_b = evaluate(rpt_b)
    ok_b   = eval_b.verdict == "REJECT"
    print(f"  {'[OK]' if ok_b else '[FAIL]'} verdict={eval_b.verdict}  confidence={eval_b.confidence:.2f}")
    for c in eval_b.concerns: print(f"    ! {c}")
    res_b  = approval.run(rpt_b, auto_decision="reject", auto_reason="inject: training failed")
    ok_b2  = res_b["action"] == "REJECTED"
    print(f"  {'[OK]' if ok_b2 else '[FAIL]'} action=REJECTED")
    # Verify rejection log
    if REJECTIONS_LOG.exists():
        last = json.loads(REJECTIONS_LOG.read_text().splitlines()[-1])
        ok_b3 = last["event"] == "REJECTED"
        print(f"  {'[OK]' if ok_b3 else '[FAIL]'} layer3_rejections.jsonl entry written")
        all_ok = all_ok and ok_b3
    all_ok = all_ok and ok_b and ok_b2
    print()

    # Scenario C: UNCERTAIN (bm=0.75, 18/20), auto-skip
    print("Scenario C: Borderline candidate (bm=0.75, 18/20, vl=0.45) -- expect UNCERTAIN or REJECT")
    rpt_c  = _fake_report(0.75, 18, 20, 0.45)
    eval_c = evaluate(rpt_c)
    ok_c   = eval_c.verdict in ("UNCERTAIN", "REJECT")
    print(f"  {'[OK]' if ok_c else '[FAIL]'} verdict={eval_c.verdict} (accepted: UNCERTAIN or REJECT)")
    res_c  = approval.run(rpt_c, auto_decision="skip", auto_reason="inject-test skip")
    ok_c2  = res_c["action"] == "SKIPPED"
    print(f"  {'[OK]' if ok_c2 else '[FAIL]'} action=SKIPPED")
    all_ok = all_ok and ok_c and ok_c2
    print()

    # Scenario D: Low benchmark -- expect REJECT
    print("Scenario D: Low benchmark (bm=0.30) -- expect REJECT")
    rpt_d  = _fake_report(0.30, 6, 20, 0.35)
    eval_d = evaluate(rpt_d)
    ok_d   = eval_d.verdict == "REJECT"
    print(f"  {'[OK]' if ok_d else '[FAIL]'} verdict={eval_d.verdict}")
    all_ok = all_ok and ok_d
    print()

    # Queue log
    print("Queue events (last 8):")
    if L3_QUEUE_PATH.exists():
        events = [json.loads(l)["event"] for l in L3_QUEUE_PATH.read_text().splitlines() if l.strip()]
        for e in events[-8:]: print(f"  {e}")
    print()
    print(f"=== {'All checks PASSED' if all_ok else 'SOME CHECKS FAILED -- see above'} ===\n")


if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(name)s | %(message)s")
    sys.path.insert(0, str(FLUX_ROOT))

    p = argparse.ArgumentParser()
    p.add_argument("--inject-test",  action="store_true")
    p.add_argument("--from-report",  default=None,
                   help="JSON file containing a serialised CandidateReport")
    p.add_argument("--auto-approve", action="store_true")
    p.add_argument("--auto-reject",  action="store_true")
    p.add_argument("--reason",       default=None)
    args = p.parse_args()

    if args.inject_test:
        _run_inject_test()

    elif args.from_report:
        from flux.rsm.isolation_runner  import CandidateReport
        from flux.rsm.candidate_builder import ArchitecturalProposal

        raw  = json.loads(Path(args.from_report).read_text())
        prop = ArchitecturalProposal(**raw["proposal"])
        raw["proposal"] = prop
        report = CandidateReport(**raw)

        auto = ("approve" if args.auto_approve
                else "reject" if args.auto_reject
                else None)

        result = ArchitecturalApproval().run(report, auto_decision=auto, auto_reason=args.reason)
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Usage: python -m flux.rsm.architectural_approval --inject-test")
        print("       python -m flux.rsm.architectural_approval --from-report <path>")
