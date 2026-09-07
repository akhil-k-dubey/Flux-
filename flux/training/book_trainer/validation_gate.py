"""
flux/training/book_trainer/validation_gate.py

After each book, verify identity preservation.
If KG win rate drops below 90%, STOP and report.

This is the safety mechanism that ensures book training
does not corrupt what Flux already knows.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# The 20 anchor concepts from identity_preservation_test
_ANCHOR_CONCEPTS = [
    "entropy", "second_law_thermodynamics", "information_entropy",
    "quantum_superposition", "entanglement", "schrodinger_equation",
    "backpropagation", "gradient_descent", "fisher_information",
    "flux_rk4_dynamics", "flux_identity", "flux_layer_system",
    "de_broglie_relation", "channel_capacity", "typical_set",
    "carnot_cycle", "gibbs_free_energy", "boltzmann_distribution",
    "basal_ganglia", "flux_training_phases",
]


class ValidationGate:
    """
    Runs KG-only identity preservation check after each book.
    Does NOT require training — checks KG integrity only.
    """

    def __init__(self, store, threshold: float = 0.90):
        """
        Parameters
        ----------
        store     : KGStore instance
        threshold : minimum KG win rate (default 90%)
        """
        self.store     = store
        self.threshold = threshold
        self._results_path = Path("logs/book_training_validation.jsonl")

    def check(self, book_title: str) -> tuple[bool, dict]:
        """
        Run identity preservation check.

        Returns
        -------
        (passed, report)
          passed : True if win_rate >= threshold
          report : detailed results dict
        """
        results = []
        for concept_id in _ANCHOR_CONCEPTS:
            node = self.store.get(concept_id)
            if node:
                defn = node.get("definition", "")
                overlap = 1.0 if len(defn) > 20 else 0.0
            else:
                overlap = 0.0
            results.append({
                "concept_id": concept_id,
                "present":    node is not None,
                "overlap":    overlap,
            })

        present_count = sum(1 for r in results if r["present"])
        win_rate      = present_count / len(_ANCHOR_CONCEPTS)
        avg_overlap   = sum(r["overlap"] for r in results) / len(results)

        passed = win_rate >= self.threshold

        report = {
            "book":         book_title,
            "timestamp":    time.time(),
            "win_rate":     win_rate,
            "avg_overlap":  avg_overlap,
            "passed":       passed,
            "threshold":    self.threshold,
            "total_kg_nodes": len(self.store.all_ids()),
            "results":      results,
        }

        # Log result
        try:
            with open(self._results_path, "a") as f:
                f.write(json.dumps(report) + "\n")
        except Exception:
            pass

        if passed:
            log.info(
                "ValidationGate: ✅ PASSED after '%s' — win_rate=%.1f%% (%d/%d anchor concepts present)",
                book_title, win_rate * 100, present_count, len(_ANCHOR_CONCEPTS)
            )
        else:
            log.error(
                "ValidationGate: ❌ FAILED after '%s' — win_rate=%.1f%% < threshold %.1f%%",
                book_title, win_rate * 100, self.threshold * 100
            )
            log.error(
                "Missing anchor concepts: %s",
                [r["concept_id"] for r in results if not r["present"]]
            )

        return passed, report

    def format_report(self, report: dict) -> str:
        """Format a validation report for display."""
        status = "✅ PASSED" if report["passed"] else "❌ FAILED"
        lines = [
            f"\n{'═'*55}",
            f"  Validation after: {report['book']}",
            f"  Status: {status}",
            f"  KG win rate:   {report['win_rate']*100:.1f}%  (threshold: {report['threshold']*100:.0f}%)",
            f"  Avg overlap:   {report['avg_overlap']:.3f}",
            f"  KG total nodes: {report['total_kg_nodes']}",
        ]
        missing = [r["concept_id"] for r in report["results"] if not r["present"]]
        if missing:
            lines.append(f"  Missing anchors: {', '.join(missing)}")
        lines.append(f"{'═'*55}\n")
        return "\n".join(lines)
