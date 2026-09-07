"""
flux/rsm/bottleneck_detector.py

L3A — Architecture Bottleneck Detector

Observes four signals every post-session:

  1. latency          — median elapsed_ms over the last SESSION_WINDOW EXPLAIN/
                        CONNECT/GAP queries (ignores TEACH, CORRECT_RESPONSE etc.)
                        Threshold: >500ms median  (baseline: 36ms median, p95=176ms)

  2. fisher_saturation — How "full" the LTC's Fisher diagonal is.
                        Saturation = mean(|F_i|) / (max(|F_i|) + ε)
                        When saturation → 1.0 the model has no capacity left for new
                        knowledge. Threshold: >0.85

  3. benchmark_plateau — Rolling mean of conf_variance from Layer 2 ProbeResults
                        over the last PLATEAU_WINDOW sessions, compared to the
                        window before it. If improvement < PLATEAU_MIN_DELTA for
                        CONSECUTIVE_REQUIRED sessions → plateau.
                        Threshold: mean conf_variance > 0.25 with delta < 0.01

  4. ewc_forgetting    — Weighted parameter drift from EWC optimal point:
                        rate = Σ F_i(θ_i − θ*_i)² / Σ F_i
                        High forgetting = model's old knowledge is being overwritten.
                        Threshold: >0.05 (5% weighted drift)

Fires BottleneckReport when ANY signal stays above threshold for
CONSECUTIVE_REQUIRED (3) consecutive sessions. Single-session spikes are
ignored — this is an architectural signal, not a noisy per-query signal.

Output files:
  logs/bottleneck_history.jsonl   — per-session metric snapshots
  logs/layer3_diagnoses.jsonl     — BottleneckReports that fired (severity ≥ threshold)

Standalone test:
  python -m flux.rsm.bottleneck_detector --inject-test
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SESSION_WINDOW        = 50    # last N conversation entries to measure latency over
PLATEAU_WINDOW        = 5     # sessions for rolling benchmark comparison
CONSECUTIVE_REQUIRED  = 3     # sessions a signal must stay triggered before report fires

THRESHOLD_LATENCY_MS  = 500.0  # median ms (baseline p95=176ms; 500ms = clear degradation)
THRESHOLD_FISHER_SAT  = 0.85   # Fisher saturation ratio
THRESHOLD_PLATEAU_VAR = 0.25   # conf_variance rolling mean
THRESHOLD_PLATEAU_DELTA = 0.01 # minimum improvement to NOT count as plateau
THRESHOLD_EWC_FORGET  = 0.05   # 5% weighted param drift

HISTORY_PATH     = Path("logs/bottleneck_history.jsonl")
L3_DIAGNOSE_PATH = Path("logs/layer3_diagnoses.jsonl")
CONV_LOG         = Path("logs/flux_conversations.jsonl")
L2_DIAGNOSE_LOG  = Path("logs/layer2_diagnoses.jsonl")
EWC_STATE_PATH   = Path("checkpoints/flux_ewc_state.pt")
LTC_CKPT         = Path("checkpoints/flux_grounded_best.pt")


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class SignalReading:
    name:      str
    value:     float
    threshold: float
    triggered: bool

    @property
    def severity(self) -> float:
        if self.threshold == 0:
            return 0.0
        return min(1.0, max(0.0, (self.value - self.threshold) / self.threshold))


@dataclass
class SessionSnapshot:
    session_id:   str
    timestamp:    float
    latency_ms:   float
    fisher_sat:   float
    plateau_var:  float
    ewc_forget:   float
    signals:      list = field(default_factory=list)


@dataclass
class BottleneckReport:
    report_id:          str
    timestamp:          float
    session_id:         str
    triggered_signals:  list
    signal_values:      dict
    consecutive_counts: dict
    severity:           float
    fires:              bool
    evidence:           list

    def to_dict(self) -> dict:
        return asdict(self)


# ── Core detector ─────────────────────────────────────────────────────────────

class BottleneckDetector:

    def __init__(self, history_path=HISTORY_PATH, l3_log=L3_DIAGNOSE_PATH):
        self.history_path = Path(history_path)
        self.l3_log       = Path(l3_log)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.l3_log.parent.mkdir(parents=True, exist_ok=True)

    def observe(self, session_id: str) -> Optional[BottleneckReport]:
        snap    = self._measure(session_id)
        self._append_history(snap)
        history = self._load_recent_history(n=CONSECUTIVE_REQUIRED + 5)
        consec  = self._consecutive_counts(history)

        triggered = [sig for sig, cnt in consec.items() if cnt >= CONSECUTIVE_REQUIRED]
        fires     = len(triggered) > 0
        severity  = self._composite_severity(snap, triggered)
        evidence  = self._build_evidence(snap, consec)

        report = BottleneckReport(
            report_id          = f"l3a_{int(snap.timestamp)}",
            timestamp          = snap.timestamp,
            session_id         = session_id,
            triggered_signals  = triggered,
            signal_values      = {
                "latency_ms":  snap.latency_ms,
                "fisher_sat":  snap.fisher_sat,
                "plateau_var": snap.plateau_var,
                "ewc_forget":  snap.ewc_forget,
            },
            consecutive_counts = consec,
            severity           = severity,
            fires              = fires,
            evidence           = evidence,
        )

        if fires:
            self._append_l3_log(report)
            log.warning(
                "L3A BottleneckReport FIRED: signals=%s severity=%.2f session=%s",
                triggered, severity, session_id,
            )
        else:
            log.info(
                "L3A observe: no bottleneck (max_consec=%d/%d) session=%s",
                max(consec.values(), default=0), CONSECUTIVE_REQUIRED, session_id,
            )
        return report if fires else None

    # ── Signal measurements ────────────────────────────────────────────────────

    def _measure(self, session_id: str) -> SessionSnapshot:
        lat  = self._measure_latency()
        fsat = self._measure_fisher_saturation()
        plat = self._measure_benchmark_plateau()
        ewc  = self._measure_ewc_forgetting()

        signals = [
            SignalReading("latency_ms",  lat,  THRESHOLD_LATENCY_MS,  lat  > THRESHOLD_LATENCY_MS),
            SignalReading("fisher_sat",  fsat, THRESHOLD_FISHER_SAT,  fsat > THRESHOLD_FISHER_SAT),
            SignalReading("plateau_var", plat, THRESHOLD_PLATEAU_VAR, plat > THRESHOLD_PLATEAU_VAR),
            SignalReading("ewc_forget",  ewc,  THRESHOLD_EWC_FORGET,  ewc  > THRESHOLD_EWC_FORGET),
        ]
        log.info(
            "L3A signals: latency=%.1fms fisher_sat=%.3f plateau_var=%.3f ewc_forget=%.4f",
            lat, fsat, plat, ewc,
        )
        return SessionSnapshot(
            session_id=session_id, timestamp=time.time(),
            latency_ms=lat, fisher_sat=fsat,
            plateau_var=plat, ewc_forget=ewc, signals=signals,
        )

    def _measure_latency(self) -> float:
        if not CONV_LOG.exists():
            return 0.0
        try:
            lines = CONV_LOG.read_text(encoding="utf-8").splitlines()
            vals: list[float] = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("intent") in ("EXPLAIN", "CONNECT", "GAP"):
                    ms = rec.get("elapsed_ms")
                    if ms is not None:
                        vals.append(float(ms))
                if len(vals) >= SESSION_WINDOW:
                    break
            return statistics.median(vals) if vals else 0.0
        except Exception as exc:
            log.debug("latency measure failed: %s", exc)
            return 0.0

    def _measure_fisher_saturation(self) -> float:
        if not EWC_STATE_PATH.exists():
            return 0.0
        try:
            import torch
            state = torch.load(str(EWC_STATE_PATH), map_location="cpu")
            all_f: list[float] = []
            for d in (state.get("fisher_model", {}), state.get("fisher_cons", {})):
                for t in d.values():
                    all_f.extend(t.abs().flatten().tolist())
            if not all_f:
                return 0.0
            return float(statistics.mean(all_f) / (max(all_f) + 1e-8))
        except Exception as exc:
            log.debug("fisher_sat measure failed: %s", exc)
            return 0.0

    def _measure_benchmark_plateau(self) -> float:
        if not L2_DIAGNOSE_LOG.exists():
            return 0.0
        try:
            lines = L2_DIAGNOSE_LOG.read_text(encoding="utf-8").splitlines()
            variances: list[float] = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for h in rec.get("hypotheses", []):
                    conf = h.get("confidence")
                    if conf is not None:
                        variances.append(float(conf))
                if len(variances) >= PLATEAU_WINDOW * 2:
                    break
            if len(variances) < 2:
                return 0.0
            return statistics.mean(variances[:PLATEAU_WINDOW])
        except Exception as exc:
            log.debug("plateau measure failed: %s", exc)
            return 0.0

    def _measure_ewc_forgetting(self) -> float:
        if not EWC_STATE_PATH.exists() or not LTC_CKPT.exists():
            return 0.0
        try:
            import torch
            ewc  = torch.load(str(EWC_STATE_PATH), map_location="cpu")
            ckpt = torch.load(str(LTC_CKPT),       map_location="cpu")

            if isinstance(ckpt, dict) and "model_state_dict" in ckpt:
                current = ckpt["model_state_dict"]
            elif isinstance(ckpt, dict) and "state_dict" in ckpt:
                current = ckpt["state_dict"]
            else:
                current = ckpt

            optimal = ewc.get("optimal_model", {})
            fisher  = ewc.get("fisher_model",  {})

            weighted_sq  = 0.0
            total_fisher = 0.0
            for name, f_val in fisher.items():
                if name not in current or name not in optimal:
                    continue
                theta     = current[name].float()
                theta_opt = optimal[name].float()
                f         = f_val.float()
                weighted_sq  += (f * (theta - theta_opt) ** 2).sum().item()
                total_fisher += f.sum().item()

            if total_fisher < 1e-10:
                return 0.0
            return float(weighted_sq / (total_fisher + 1e-10))
        except Exception as exc:
            log.debug("ewc_forget measure failed: %s", exc)
            return 0.0

    # ── History helpers ────────────────────────────────────────────────────────

    def _append_history(self, snap: SessionSnapshot) -> None:
        record = {
            "session_id":  snap.session_id,
            "timestamp":   snap.timestamp,
            "latency_ms":  snap.latency_ms,
            "fisher_sat":  snap.fisher_sat,
            "plateau_var": snap.plateau_var,
            "ewc_forget":  snap.ewc_forget,
            "triggered":   [s.name for s in snap.signals if s.triggered],
        }
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _load_recent_history(self, n: int) -> list:
        if not self.history_path.exists():
            return []
        lines = self.history_path.read_text(encoding="utf-8").splitlines()
        recent = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                recent.insert(0, json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(recent) >= n:
                break
        return recent

    def _consecutive_counts(self, history: list) -> dict:
        thresholds = {
            "latency_ms":  THRESHOLD_LATENCY_MS,
            "fisher_sat":  THRESHOLD_FISHER_SAT,
            "plateau_var": THRESHOLD_PLATEAU_VAR,
            "ewc_forget":  THRESHOLD_EWC_FORGET,
        }
        counts = {s: 0 for s in thresholds}
        for sig, thresh in thresholds.items():
            for snap in reversed(history):
                if snap.get(sig, 0.0) > thresh:
                    counts[sig] += 1
                else:
                    break
        return counts

    def _composite_severity(self, snap: SessionSnapshot, triggered: list) -> float:
        if not triggered:
            return 0.0
        readings = {s.name: s for s in snap.signals}
        sevs = [readings[t].severity for t in triggered if t in readings]
        return statistics.mean(sevs) if sevs else 0.0

    def _build_evidence(self, snap: SessionSnapshot, consec: dict) -> list:
        bullets = []
        meta = {
            "latency_ms":  (THRESHOLD_LATENCY_MS,  "ms median query latency",     ">{:.0f}ms"),
            "fisher_sat":  (THRESHOLD_FISHER_SAT,  "Fisher saturation",            ">{:.2f}"),
            "plateau_var": (THRESHOLD_PLATEAU_VAR, "benchmark conf_variance mean", ">{:.2f}"),
            "ewc_forget":  (THRESHOLD_EWC_FORGET,  "EWC weighted forgetting rate", ">{:.4f}"),
        }
        vals = {
            "latency_ms":  snap.latency_ms,
            "fisher_sat":  snap.fisher_sat,
            "plateau_var": snap.plateau_var,
            "ewc_forget":  snap.ewc_forget,
        }
        for sig, (thresh, label, fmt) in meta.items():
            val = vals[sig]
            cnt = consec.get(sig, 0)
            if cnt >= CONSECUTIVE_REQUIRED:
                bullets.append(
                    f"  [{sig}] {label} = {val:.4g} "
                    f"({fmt.format(thresh)}, fired {cnt} consecutive sessions)"
                )
            elif cnt > 0:
                bullets.append(
                    f"  [{sig}] {label} = {val:.4g} "
                    f"(triggered {cnt}/{CONSECUTIVE_REQUIRED} sessions, not yet firing)"
                )
        return bullets

    def _append_l3_log(self, report: BottleneckReport) -> None:
        self.l3_log.parent.mkdir(parents=True, exist_ok=True)
        with self.l3_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(report.to_dict()) + "\n")


# ── Standalone test ────────────────────────────────────────────────────────────

def _run_inject_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        hist  = Path(tmp) / "bh.jsonl"
        l3log = Path(tmp) / "l3.jsonl"
        det   = BottleneckDetector(history_path=hist, l3_log=l3log)

        print("=== L3A Bottleneck Detector — Inject Test ===\n")

        def make_snap(sid, lat, fsat, plat, ewc):
            s = SessionSnapshot(
                session_id=sid, timestamp=time.time(),
                latency_ms=lat, fisher_sat=fsat,
                plateau_var=plat, ewc_forget=ewc,
            )
            s.signals = [
                SignalReading("latency_ms",  lat,  THRESHOLD_LATENCY_MS,  lat  > THRESHOLD_LATENCY_MS),
                SignalReading("fisher_sat",  fsat, THRESHOLD_FISHER_SAT,  fsat > THRESHOLD_FISHER_SAT),
                SignalReading("plateau_var", plat, THRESHOLD_PLATEAU_VAR, plat > THRESHOLD_PLATEAU_VAR),
                SignalReading("ewc_forget",  ewc,  THRESHOLD_EWC_FORGET,  ewc  > THRESHOLD_EWC_FORGET),
            ]
            return s

        def inject(sid, lat, fsat, plat, ewc, expect):
            snap = make_snap(sid, lat, fsat, plat, ewc)
            det._append_history(snap)
            history  = det._load_recent_history(n=CONSECUTIVE_REQUIRED + 5)
            consec   = det._consecutive_counts(history)
            triggered = [s for s, c in consec.items() if c >= CONSECUTIVE_REQUIRED]
            fires    = bool(triggered)
            ev       = det._build_evidence(snap, consec)
            ok       = "✓" if fires == expect else "✗ MISMATCH"
            print(f"  {ok}  {sid:20s}  lat={lat:6.0f}ms fsat={fsat:.2f} plat={plat:.2f} "
                  f"ewc={ewc:.4f}  fired={fires}  triggered={triggered}")
            for e in ev:
                print(f"          {e}")
            if fires != expect:
                raise AssertionError(f"Expected fire={expect} but got {fires}")
            return fires

        print("Test 1: all below threshold → no fire (4 sessions)")
        for i in range(4):
            inject(f"ok_{i}", 36.0, 0.12, 0.08, 0.001, expect=False)
        print()

        print("Test 2: single latency spike → no fire (consecutive gate)")
        inject("spike_01", 1200.0, 0.12, 0.08, 0.001, expect=False)
        inject("spike_02", 36.0,   0.12, 0.08, 0.001, expect=False)
        print()

        print(f"Test 3: latency sustained {CONSECUTIVE_REQUIRED} sessions → FIRE")
        inject("lat_01", 600.0, 0.12, 0.08, 0.001, expect=False)
        inject("lat_02", 650.0, 0.12, 0.08, 0.001, expect=False)
        inject("lat_03", 700.0, 0.12, 0.08, 0.001, expect=True)
        print()

        # fresh detector for clean consecutive counts
        det2 = BottleneckDetector(
            history_path=Path(tmp) / "bh2.jsonl",
            l3_log=Path(tmp) / "l3b2.jsonl",
        )

        def inject2(sid, lat, fsat, plat, ewc, expect):
            snap = make_snap(sid, lat, fsat, plat, ewc)
            det2._append_history(snap)
            history  = det2._load_recent_history(n=CONSECUTIVE_REQUIRED + 5)
            consec   = det2._consecutive_counts(history)
            triggered = [s for s, c in consec.items() if c >= CONSECUTIVE_REQUIRED]
            fires    = bool(triggered)
            ok       = "✓" if fires == expect else "✗ MISMATCH"
            print(f"  {ok}  {sid:20s}  ewc={ewc:.4f}  fired={fires}  triggered={triggered}")
            if fires != expect:
                raise AssertionError(f"Expected fire={expect} but got {fires}")
            return fires

        print("Test 4: ewc_forget only → FIRE after 3 sessions")
        inject2("ewc_01", 36.0, 0.12, 0.08, 0.06, expect=False)
        inject2("ewc_02", 36.0, 0.12, 0.08, 0.07, expect=False)
        inject2("ewc_03", 36.0, 0.12, 0.08, 0.08, expect=True)
        print()

        print("Test 5: fisher_sat only → FIRE after 3 sessions")
        det3 = BottleneckDetector(
            history_path=Path(tmp) / "bh3.jsonl",
            l3_log=Path(tmp) / "l3b3.jsonl",
        )
        for sid, fsat, exp in [("f1", 0.90, False), ("f2", 0.91, False), ("f3", 0.92, True)]:
            snap = make_snap(sid, 36.0, fsat, 0.08, 0.001)
            det3._append_history(snap)
            history  = det3._load_recent_history(n=CONSECUTIVE_REQUIRED + 5)
            consec   = det3._consecutive_counts(history)
            triggered = [s for s, c in consec.items() if c >= CONSECUTIVE_REQUIRED]
            fires    = bool(triggered)
            ok       = "✓" if fires == exp else "✗ MISMATCH"
            print(f"  {ok}  {sid:20s}  fsat={fsat:.2f}  fired={fires}")
            if fires != exp:
                raise AssertionError(f"Expected fire={exp} but got {fires}")
        print()

        print("=== All 5 inject tests PASSED ===")


if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(name)s | %(message)s")
    sys.path.insert(0, ".")
    p = argparse.ArgumentParser()
    p.add_argument("--inject-test", action="store_true")
    p.add_argument("--session-id",  default=None)
    args = p.parse_args()

    if args.inject_test:
        _run_inject_test()
    elif args.session_id:
        det    = BottleneckDetector()
        report = det.observe(args.session_id)
        if report:
            print(f"\nBottleneckReport FIRED (severity={report.severity:.2f}):")
            for e in report.evidence:
                print(e)
        else:
            print("No bottleneck detected.")
    else:
        print("Usage: python -m flux.rsm.bottleneck_detector --inject-test")
        print("       python -m flux.rsm.bottleneck_detector --session-id <id>")
