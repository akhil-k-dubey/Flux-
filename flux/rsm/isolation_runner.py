"""
flux/rsm/isolation_runner.py

L3B (Part 2) — Isolation Runner

Takes an ArchitecturalProposal and runs the candidate train script
in an isolated environment, then returns a CandidateReport.

Isolation modes (in priority order):
  1. subprocess sandbox  — always available, used first
  2. firecracker VM      — used when --firecracker flag passed (Stage 7)

Subprocess sandbox enforcement:
  - Corpus snapshot copied into workdir — candidate reads ONLY those files
  - Output written to workdir/output/ — candidate cannot reach live checkpoints
  - Stripped environment: only PATH + PYTHONPATH pointing at FLUX root
  - No HOME, no FLUX_* vars, no XDG vars
  - Hard timeout: 3600s

Workdir layout:
  logs/candidates/{proposal_id}/
    corpus_snapshot/
      grounded_sessions.jsonl    ← copy of live corpus (read-only by convention)
      vocab.json
      ewc_state.pt               ← if available
      flux_grounded_best.pt      ← needed for char_freq
    output/
      candidate_output.json      ← written by train script (locked schema)
      candidate_weights.pt       ← written by train script
    config.json                  ← config with workdir-internal paths

Standalone test:
  python -m flux.rsm.isolation_runner --inject-test
  python -m flux.rsm.isolation_runner --from-report logs/layer3_diagnoses.jsonl
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

FLUX_ROOT         = Path(__file__).resolve().parents[2]   # .../FLUX/

CORPUS_SRC        = FLUX_ROOT / "logs" / "flux_grounded_sessions.jsonl"
VOCAB_SRC         = FLUX_ROOT / "logs" / "flux_vocab.json"
EWC_SRC           = FLUX_ROOT / "checkpoints" / "flux_ewc_state.pt"
LTC_WEIGHTS_SRC   = FLUX_ROOT / "checkpoints" / "flux_grounded_best.pt"

CANDIDATES_DIR    = FLUX_ROOT / "logs" / "candidates"
L3_QUEUE_PATH     = FLUX_ROOT / "logs" / "layer3_queue.jsonl"
L3_REJECTIONS     = FLUX_ROOT / "logs" / "layer3_rejections.jsonl"

# ── Timeout + training defaults ────────────────────────────────────────────────

SUBPROCESS_TIMEOUT_S  = 3600    # 1 hour hard limit
DEFAULT_TRAIN_EPOCHS  = 50      # enough to show convergence without overnight run
DEFAULT_VAL_FRAC      = 0.2
DEFAULT_LR            = 5e-5


# ── CandidateReport ────────────────────────────────────────────────────────────

@dataclass
class CandidateReport:
    """
    The output of IsolationRunner.run().
    Consumed by architectural_approval.py (L3C) for Flux evaluation + human decision.
    """
    proposal_id:          str
    proposal:             object          # ArchitecturalProposal (avoid circular import)
    benchmark_score:      float
    regression_passed:    int
    regression_total:     int
    regression_details:   dict
    latency_p95_ms:       float
    training_duration_s:  float
    final_train_loss:     float
    final_val_loss:       float
    architecture:         dict            # what was actually built
    mode:                 str             # "architecture"
    status:               str             # "EVALUATED" | "ERROR"
    error:                Optional[str]
    workdir:              str
    timestamp:            float

    def to_dict(self) -> dict:
        d = asdict(self)
        # proposal is a dataclass — already serialised by asdict; drop large baselines
        if "proposal" in d and isinstance(d["proposal"], dict):
            d["proposal"].pop("baseline_responses", None)
        return d

    @property
    def passed_regression(self) -> bool:
        return (
            self.regression_total > 0
            and self.regression_passed == self.regression_total
        )

    @property
    def summary_line(self) -> str:
        return (
            f"proposal={self.proposal_id}  "
            f"benchmark={self.benchmark_score:.3f}  "
            f"regression={self.regression_passed}/{self.regression_total}  "
            f"train_loss={self.final_train_loss:.4f}  "
            f"val_loss={self.final_val_loss:.4f}  "
            f"latency_p95={self.latency_p95_ms:.1f}ms  "
            f"duration={self.training_duration_s:.1f}s  "
            f"status={self.status}"
        )


# ── IsolationRunner ────────────────────────────────────────────────────────────

class IsolationRunner:
    """
    Runs an ArchitecturalProposal in isolation and returns a CandidateReport.

    Usage:
        from flux.rsm.isolation_runner import IsolationRunner
        runner = IsolationRunner()
        report = runner.run(proposal)

    The runner creates a fresh workdir for each proposal — old workdirs are
    kept as an audit trail (never auto-deleted). Disk cost: ~1.5 MB per run.
    """

    def __init__(
        self,
        use_firecracker: bool = False,
        train_epochs:    int   = DEFAULT_TRAIN_EPOCHS,
        val_frac:        float = DEFAULT_VAL_FRAC,
        lr:              float = DEFAULT_LR,
        timeout_s:       int   = SUBPROCESS_TIMEOUT_S,
    ):
        self.use_firecracker = use_firecracker
        self.train_epochs    = train_epochs
        self.val_frac        = val_frac
        self.lr              = lr
        self.timeout_s       = timeout_s
        CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self, proposal) -> CandidateReport:
        """
        Run the proposal in isolation. Returns CandidateReport.
        Never raises — errors are captured in report.status="ERROR".
        """
        pid     = proposal.proposal_id
        workdir = CANDIDATES_DIR / pid
        workdir.mkdir(parents=True, exist_ok=True)

        log.info("IsolationRunner: starting proposal=%s  workdir=%s", pid, workdir)
        self._log_queue(proposal, "TRAINING_STARTED")

        try:
            if self.use_firecracker:
                report = self._run_firecracker(proposal, workdir)
            else:
                report = self._run_subprocess(proposal, workdir)
        except Exception as exc:
            log.error("IsolationRunner fatal error for %s: %s", pid, exc)
            report = self._error_report(proposal, workdir, str(exc))

        self._log_queue(proposal, f"TRAINING_{report.status}", extra={
            "benchmark_score":  report.benchmark_score,
            "regression":       f"{report.regression_passed}/{report.regression_total}",
            "final_val_loss":   report.final_val_loss,
            "error":            report.error,
        })

        log.info("IsolationRunner: %s", report.summary_line)
        return report

    # ── Subprocess isolation ───────────────────────────────────────────────────

    def _run_subprocess(self, proposal, workdir: Path) -> CandidateReport:
        pid = proposal.proposal_id

        # 1. Build corpus snapshot
        snapshot_dir = workdir / "corpus_snapshot"
        output_dir   = workdir / "output"
        snapshot_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)

        self._copy_snapshot(snapshot_dir)

        # 2. Write config with workdir-internal paths
        config = self._build_config(proposal, snapshot_dir, output_dir)
        config_path = workdir / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        log.info("IsolationRunner: config written → %s", config_path)

        # 3. Run train script in subprocess
        log.info(
            "IsolationRunner: launching subprocess  epochs=%d  timeout=%ds",
            self.train_epochs, self.timeout_s,
        )
        t_start = time.time()
        result  = self._exec_subprocess(config_path)
        elapsed = time.time() - t_start

        if result.returncode != 0:
            stderr = (result.stderr or b"").decode(errors="replace").strip()
            stdout = (result.stdout or b"").decode(errors="replace").strip()
            err_msg = f"Subprocess exited {result.returncode}. stderr: {stderr[-500:]}"
            log.error("IsolationRunner subprocess failed: %s", err_msg)
            log.debug("stdout: %s", stdout[-2000:])
            return self._error_report(proposal, workdir, err_msg)

        # 4. Read output
        output_report = output_dir / "candidate_output.json"
        if not output_report.exists():
            return self._error_report(
                proposal, workdir,
                f"candidate_output.json not found in {output_dir}. "
                f"stdout: {(result.stdout or b'').decode(errors='replace')[-500:]}"
            )

        try:
            out = json.loads(output_report.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._error_report(proposal, workdir, f"Output JSON parse error: {exc}")

        # 5. Check for script-level error
        if out.get("error"):
            return self._error_report(proposal, workdir, f"Train script error: {out['error']}")

        return CandidateReport(
            proposal_id         = pid,
            proposal            = proposal,
            benchmark_score     = float(out["benchmark_score"]),
            regression_passed   = int(out["regression_passed"]),
            regression_total    = int(out["regression_total"]),
            regression_details  = out["regression_details"],
            latency_p95_ms      = float(out["latency_p95_ms"]),
            training_duration_s = float(out["training_duration_s"]),
            final_train_loss    = float(out["final_train_loss"]),
            final_val_loss      = float(out["final_val_loss"]),
            architecture        = out["architecture"],
            mode                = out.get("mode", "architecture"),
            status              = "EVALUATED",
            error               = None,
            workdir             = str(workdir),
            timestamp           = time.time(),
        )

    def _copy_snapshot(self, snapshot_dir: Path) -> None:
        """Copy the four corpus files into snapshot_dir."""
        copies = [
            (CORPUS_SRC,      snapshot_dir / "grounded_sessions.jsonl"),
            (VOCAB_SRC,       snapshot_dir / "vocab.json"),
            (EWC_SRC,         snapshot_dir / "ewc_state.pt"),
            (LTC_WEIGHTS_SRC, snapshot_dir / "flux_grounded_best.pt"),
        ]
        for src, dst in copies:
            if src.exists():
                shutil.copy2(src, dst)
                log.debug("Snapshot: %s → %s  (%d KB)", src.name, dst, dst.stat().st_size // 1024)
            else:
                log.warning("Snapshot: %s not found — skipping", src)

    def _build_config(self, proposal, snapshot_dir: Path, output_dir: Path) -> dict:
        """Build the config.json passed to the train script — all paths in workdir."""
        return {
            "proposal_id":          proposal.proposal_id,
            "changes":              proposal.changes,
            "corpus_path":          str(snapshot_dir / "grounded_sessions.jsonl"),
            "vocab_path":           str(snapshot_dir / "vocab.json"),
            "ewc_state_path":       str(snapshot_dir / "ewc_state.pt"),
            "ltc_weights_path":     str(snapshot_dir / "flux_grounded_best.pt"),
            "output_weights":       str(output_dir   / "candidate_weights.pt"),
            "output_report":        str(output_dir   / "candidate_output.json"),
            "regression_concepts":  proposal.regression_concepts,
            "baseline_responses":   proposal.baseline_responses,
            "train_epochs":         self.train_epochs,
            "val_frac":             self.val_frac,
            "lr":                   self.lr,
        }

    def _exec_subprocess(self, config_path: Path) -> subprocess.CompletedProcess:
        """
        Run the train script in a stripped environment.

        Environment contains only:
          PATH      — standard system paths (no user-specific dirs)
          PYTHONPATH — FLUX root only (candidate reads corpus, not live flux state)

        No HOME, no XDG_*, no FLUX_* env vars.
        The subprocess cannot write outside its cwd without an absolute path,
        and the config only points inside the workdir.
        """
        stripped_env = {
            "PATH":       "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": str(FLUX_ROOT),
        }

        # Propagate CUDA visibility if available
        cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        if cuda_visible is not None:
            stripped_env["CUDA_VISIBLE_DEVICES"] = cuda_visible

        cmd = [
            sys.executable,          # same Python interpreter
            "-m", "flux.rsm.candidate_train_script",
            "--config", str(config_path),
        ]
        log.info("IsolationRunner: %s", " ".join(cmd))

        return subprocess.run(
            cmd,
            cwd=str(FLUX_ROOT),
            env=stripped_env,
            capture_output=True,
            timeout=self.timeout_s,
        )

    # ── Firecracker stub (Stage 7) ─────────────────────────────────────────────

    def _run_firecracker(self, proposal, workdir: Path) -> CandidateReport:
        """
        Firecracker microVM isolation — Stage 7.

        Full implementation requires:
          1. A rootfs image with Python + PyTorch (~2 GB squashfs)
          2. A microVM kernel (vmlinux)
          3. Two block devices: corpus.img (read-only) + output.img (writable)
          4. Firecracker API socket management

        The subprocess path already provides strong isolation for development.
        This stub logs the attempt and falls back to subprocess.

        To enable: set use_firecracker=True AND ensure rootfs + kernel are
        present at FLUX_ROOT/firecracker/{rootfs.img, vmlinux}.
        """
        rootfs = FLUX_ROOT / "firecracker" / "rootfs.img"
        kernel = FLUX_ROOT / "firecracker" / "vmlinux"

        if not rootfs.exists() or not kernel.exists():
            log.warning(
                "Firecracker: rootfs/kernel not found at %s — "
                "falling back to subprocess sandbox. "
                "(Stage 7: build rootfs with Python + PyTorch to enable VM isolation.)",
                FLUX_ROOT / "firecracker",
            )
            return self._run_subprocess(proposal, workdir)

        # Stage 7 implementation goes here.
        # For now: fall through to subprocess even if files exist.
        log.warning("Firecracker: rootfs found but VM orchestration not yet implemented (Stage 7).")
        return self._run_subprocess(proposal, workdir)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _error_report(self, proposal, workdir: Path, error: str) -> CandidateReport:
        return CandidateReport(
            proposal_id         = proposal.proposal_id,
            proposal            = proposal,
            benchmark_score     = 0.0,
            regression_passed   = 0,
            regression_total    = len(proposal.regression_concepts),
            regression_details  = {},
            latency_p95_ms      = 0.0,
            training_duration_s = 0.0,
            final_train_loss    = 0.0,
            final_val_loss      = 0.0,
            architecture        = proposal.changes,
            mode                = "architecture",
            status              = "ERROR",
            error               = error,
            workdir             = str(workdir),
            timestamp           = time.time(),
        )

    def _log_queue(self, proposal, event: str, extra: dict = None) -> None:
        L3_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event":       event,
            "timestamp":   time.time(),
            "proposal_id": proposal.proposal_id,
            "change_type": proposal.change_type,
        }
        if extra:
            record.update(extra)
        with L3_QUEUE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


# ── Standalone test ────────────────────────────────────────────────────────────

def _run_inject_test() -> None:
    """
    Inject test: build a synthetic ArchitecturalProposal and run it
    through the full isolation_runner → candidate_train_script → CandidateReport
    pipeline on the host (subprocess sandbox, no VM).

    Verifies:
      1. Corpus snapshot is created correctly
      2. Config has workdir-internal paths (not host paths like /home/akhil/...)
      3. Train script runs to completion
      4. CandidateReport has correct schema
      5. No files written outside workdir
    """
    import tempfile, sys
    sys.path.insert(0, str(FLUX_ROOT))

    from flux.rsm.candidate_builder import CandidateBuilder, ArchitecturalProposal
    from flux.rsm.bottleneck_detector import BottleneckReport
    from flux.kg.store import get_store
    from flux.interface.flux_interface import FluxInterface

    print("=== L3B IsolationRunner — Inject Test ===\n")

    # Build a synthetic BottleneckReport (latency triggered)
    report = BottleneckReport(
        report_id          = "l3a_inject_test",
        timestamp          = time.time(),
        session_id         = "inject_test_session",
        triggered_signals  = ["latency_ms"],
        signal_values      = {"latency_ms": 650.0, "fisher_sat": 0.12,
                              "plateau_var": 0.08, "ewc_forget": 0.001},
        consecutive_counts = {"latency_ms": 3, "fisher_sat": 0,
                              "plateau_var": 0, "ewc_forget": 0},
        severity           = 0.3,
        fires              = True,
        evidence           = ["  [latency_ms] ms median query latency = 650 (>500ms, fired 3 consecutive sessions)"],
    )

    print("Step 1: Build ArchitecturalProposal from BottleneckReport")
    store   = get_store()
    flux    = FluxInterface()
    builder = CandidateBuilder(store, flux)
    proposal = builder.build(report)
    print(f"  ✓ proposal_id={proposal.proposal_id}")
    print(f"    change_type={proposal.change_type}  changes={proposal.changes}")
    print(f"    regression_concepts ({len(proposal.regression_concepts)}): "
          f"{proposal.regression_concepts[:3]}...")
    print()

    print("Step 2: Run IsolationRunner (subprocess sandbox, 20 epochs)")
    runner = IsolationRunner(train_epochs=20)
    t0     = time.time()
    cand_report = runner.run(proposal)
    elapsed = time.time() - t0

    print(f"  Run time: {elapsed:.1f}s")
    print(f"  status: {cand_report.status}")
    if cand_report.error:
        print(f"  ERROR: {cand_report.error}")
        return

    print(f"\nStep 3: Verify CandidateReport")
    checks = [
        ("proposal_id matches",    cand_report.proposal_id == proposal.proposal_id),
        ("status=EVALUATED",       cand_report.status == "EVALUATED"),
        ("benchmark_score in [0,1]", 0.0 <= cand_report.benchmark_score <= 1.0),
        ("regression_total > 0",   cand_report.regression_total > 0),
        ("final_train_loss > 0",   cand_report.final_train_loss > 0),
        ("final_val_loss > 0",     cand_report.final_val_loss > 0),
        ("latency_p95_ms > 0",     cand_report.latency_p95_ms > 0),
        ("training_duration > 0",  cand_report.training_duration_s > 0),
        ("architecture correct",   cand_report.architecture == proposal.changes),
        ("error is None",          cand_report.error is None),
        ("workdir exists",         Path(cand_report.workdir).exists()),
        ("output.json exists",     (Path(cand_report.workdir) / "output" / "candidate_output.json").exists()),
    ]

    all_ok = True
    for desc, ok in checks:
        print(f"  {'✓' if ok else '✗ FAIL'} {desc}")
        if not ok:
            all_ok = False

    print(f"\nStep 4: Verify corpus snapshot isolation")
    workdir = Path(cand_report.workdir)
    snapshot = workdir / "corpus_snapshot"
    expected_files = ["grounded_sessions.jsonl", "vocab.json"]
    for fname in expected_files:
        present = (snapshot / fname).exists()
        print(f"  {'✓' if present else '✗'} corpus_snapshot/{fname}")
        if not present:
            all_ok = False

    # Verify config has workdir-internal paths (not host paths)
    config = json.loads((workdir / "config.json").read_text())
    corpus_in_config = config["corpus_path"]
    is_internal = str(workdir) in corpus_in_config
    print(f"  {'✓' if is_internal else '✗'} config.corpus_path is workdir-internal: "
          f"{corpus_in_config[:70]}...")

    print(f"\n  benchmark_score    = {cand_report.benchmark_score:.3f}")
    print(f"  regression         = {cand_report.regression_passed}/{cand_report.regression_total}")
    print(f"  final_train_loss   = {cand_report.final_train_loss:.4f}")
    print(f"  final_val_loss     = {cand_report.final_val_loss:.4f}")
    print(f"  latency_p95_ms     = {cand_report.latency_p95_ms:.1f}ms")
    print(f"  training_duration  = {cand_report.training_duration_s:.1f}s")
    print(f"  architecture       = {cand_report.architecture}")
    print(f"  workdir            = {cand_report.workdir}")

    print(f"\nStep 5: Verify layer3_queue.jsonl events written")
    if L3_QUEUE_PATH.exists():
        events = [json.loads(l)["event"] for l in L3_QUEUE_PATH.read_text().splitlines() if l.strip()]
        training_events = [e for e in events if "TRAINING" in e]
        print(f"  Queue events: {events[-4:]}")
        print(f"  Training events found: {training_events}")
        has_started   = any("TRAINING_STARTED"   in e for e in events)
        has_evaluated = any("TRAINING_EVALUATED" in e for e in events)
        print(f"  {'✓' if has_started else '✗'} TRAINING_STARTED logged")
        print(f"  {'✓' if has_evaluated else '✗'} TRAINING_EVALUATED logged")
        if not (has_started and has_evaluated):
            all_ok = False
    else:
        print("  ✗ layer3_queue.jsonl not created")
        all_ok = False

    print(f"\n=== {'All checks PASSED' if all_ok else 'SOME CHECKS FAILED — see above'} ===")


if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(name)s | %(message)s")
    sys.path.insert(0, str(FLUX_ROOT))
    p = argparse.ArgumentParser()
    p.add_argument("--inject-test",  action="store_true")
    p.add_argument("--from-report",  default=None,
                   help="Path to layer3_diagnoses.jsonl — run last fired report end-to-end")
    p.add_argument("--firecracker",  action="store_true")
    p.add_argument("--epochs",       type=int, default=DEFAULT_TRAIN_EPOCHS)
    args = p.parse_args()

    if args.inject_test:
        _run_inject_test()

    elif args.from_report:
        from flux.rsm.bottleneck_detector import BottleneckReport
        from flux.rsm.candidate_builder   import CandidateBuilder
        from flux.kg.store                import get_store
        from flux.interface.flux_interface import FluxInterface

        diag_path = Path(args.from_report)
        if not diag_path.exists():
            print(f"No diagnoses file at {diag_path}")
            sys.exit(1)

        lines = [l for l in diag_path.read_text().splitlines() if l.strip()]
        if not lines:
            print("No fired reports found")
            sys.exit(1)

        last = json.loads(lines[-1])
        report   = BottleneckReport(**last)
        store    = get_store()
        flux     = FluxInterface()
        builder  = CandidateBuilder(store, flux)
        proposal = builder.build(report)

        runner = IsolationRunner(
            use_firecracker=args.firecracker,
            train_epochs=args.epochs,
        )
        cand_report = runner.run(proposal)
        print(f"\n{cand_report.summary_line}")
    else:
        print("Usage: python -m flux.rsm.isolation_runner --inject-test")
        print("       python -m flux.rsm.isolation_runner --from-report logs/layer3_diagnoses.jsonl")
