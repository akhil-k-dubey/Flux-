#!/usr/bin/env python3
"""
flux/boot.py
Bootstrap runner — takes Flux from zero to a live, verified knowledge graph.

This is the single entry point that orchestrates the full boot sequence:
  1. Constitutional integrity verification (SHA256 hash)
  2. Knowledge graph construction (two-pass parser)
  3. Clingo safety axiom compilation
  4. Operator identity verification (Ed25519 public key)
  5. Interaction logger initialization
  6. Readiness assessment and score

Usage:
    python -m flux.boot                          # Standard boot
    python -m flux.boot --primitives path/to/md  # Custom primitives path
    python -m flux.boot --verify-only            # Integrity check only, no full boot
"""

import os
import sys
import time
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve project root and ensure importability
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PRIMITIVES_DEFAULT = PROJECT_ROOT / "FLUX_PRIMITIVES.md"
AXIOMS_DEFAULT = PROJECT_ROOT / "flux_axioms.lp"
LOG_DIR_DEFAULT = PROJECT_ROOT / "logs"
KEYS_DIR = Path.home() / ".flux_keys"


class BootError(Exception):
    """Fatal error during the boot sequence."""
    pass


class BootResult:
    """Encapsulates the result of a boot attempt."""
    def __init__(self):
        self.graph = None
        self.parser = None
        self.logger = None
        self.axioms_path = None
        self.metadata = {}
        self.stats = {}
        self.readiness_score = 0.0
        self.checks = {}       # name -> (passed: bool, detail: str)
        self.boot_time_ms = 0

    @property
    def ready(self):
        return self.readiness_score >= 1.0

    def summary(self):
        lines = []
        lines.append("=" * 64)
        lines.append("  FLUX BOOTSTRAP REPORT")
        lines.append("=" * 64)
        lines.append("")
        for name, (passed, detail) in self.checks.items():
            icon = "[PASS]" if passed else "[FAIL]"
            lines.append(f"  {icon} {name}: {detail}")
        lines.append("")
        lines.append(f"  Readiness Score: {self.readiness_score:.2f} / 1.00")
        lines.append(f"  Boot Time:       {self.boot_time_ms:.0f} ms")
        lines.append("")
        if self.ready:
            lines.append("  STATUS: OPERATIONAL")
        else:
            lines.append("  STATUS: DEGRADED — see failed checks above")
        lines.append("=" * 64)
        return "\n".join(lines)


def boot(primitives_path=None, axioms_path=None, log_dir=None, verify_only=False):
    """
    Executes the full Flux boot sequence.

    Returns a BootResult with the live knowledge graph, compiled axioms,
    and a readiness score between 0.0 and 1.0.
    """
    t_start = time.perf_counter()
    result = BootResult()

    primitives_path = Path(primitives_path or PRIMITIVES_DEFAULT)
    axioms_path = Path(axioms_path or AXIOMS_DEFAULT)
    log_dir = Path(log_dir or LOG_DIR_DEFAULT)

    # ------------------------------------------------------------------
    # CHECK 1: Constitution file exists
    # ------------------------------------------------------------------
    if not primitives_path.exists():
        result.checks["constitution_exists"] = (False, f"File not found: {primitives_path}")
        result.boot_time_ms = (time.perf_counter() - t_start) * 1000
        raise BootError(f"Constitution not found: {primitives_path}")

    result.checks["constitution_exists"] = (True, str(primitives_path))

    # ------------------------------------------------------------------
    # CHECK 2: Integrity verification (SHA256)
    # ------------------------------------------------------------------
    from flux.bootstrap.parser import BootstrapParser, BootstrapError, IntegrityError

    parser = BootstrapParser(str(primitives_path))

    try:
        # The parser's load() method verifies hash integrity internally
        graph = parser.load()
        result.checks["integrity_hash"] = (True, f"SHA256 verified — {parser.metadata.get('hash', '?')[:16]}...")
    except IntegrityError as e:
        result.checks["integrity_hash"] = (False, str(e))
        result.boot_time_ms = (time.perf_counter() - t_start) * 1000
        raise BootError(f"Constitutional integrity check FAILED: {e}")
    except BootstrapError as e:
        result.checks["integrity_hash"] = (False, str(e))
        result.boot_time_ms = (time.perf_counter() - t_start) * 1000
        raise BootError(f"Parser error: {e}")

    result.graph = graph
    result.parser = parser
    result.metadata = parser.metadata
    result.stats = parser.stats()

    # ------------------------------------------------------------------
    # CHECK 3: Graph structural minimums
    # ------------------------------------------------------------------
    stats = result.stats
    prim_ok = stats['primitives'] >= 80
    concept_ok = stats['concept_nodes'] >= 500
    edge_ok = stats['edges'] >= 3000
    grammar_ok = stats['grammar_blocks'] == 10
    bench_ok = stats['benchmarks'] >= 50

    result.checks["primitives"] = (prim_ok, f"{stats['primitives']} primitives (min 80)")
    result.checks["concepts"] = (concept_ok, f"{stats['concept_nodes']} concepts (min 500)")
    result.checks["edges"] = (edge_ok, f"{stats['edges']} edges (min 3000)")
    result.checks["grammar_blocks"] = (grammar_ok, f"{stats['grammar_blocks']} grammar blocks (exactly 10)")
    result.checks["benchmarks"] = (bench_ok, f"{stats['benchmarks']} benchmarks (min 50)")

    if verify_only:
        passed = sum(1 for ok, _ in result.checks.values() if ok)
        result.readiness_score = passed / len(result.checks)
        result.boot_time_ms = (time.perf_counter() - t_start) * 1000
        return result

    # ------------------------------------------------------------------
    # CHECK 4: Operator identity — Ed25519 public key present
    # ------------------------------------------------------------------
    akhil_node = graph.nodes.get("AKHIL")
    if akhil_node is None:
        result.checks["operator_identity"] = (False, "AKHIL primitive not found in graph")
    else:
        vk = akhil_node.properties.get("verification_key", "")
        if vk.startswith("ed25519:") and len(vk.split(":")[1]) == 64:
            result.checks["operator_identity"] = (True, f"Ed25519 key anchored — {vk[:24]}...")
        else:
            result.checks["operator_identity"] = (False, f"Invalid or missing verification_key: {vk}")

    # Check sole_operator
    sole = akhil_node.properties.get("sole_operator", "") if akhil_node else ""
    is_sole = sole is True or str(sole).upper() == "YES"
    result.checks["sole_operator"] = (is_sole, f"sole_operator={sole}")

    # ------------------------------------------------------------------
    # CHECK 5: Private key accessible (warning only — not required to boot)
    # ------------------------------------------------------------------
    private_key_path = KEYS_DIR / "akhil_ed25519.pem"
    if private_key_path.exists():
        result.checks["private_key"] = (True, str(private_key_path))
    else:
        # Not fatal — Flux can boot read-only, just can't sign approvals
        result.checks["private_key"] = (False, f"Not found: {private_key_path} (signing disabled)")

    # ------------------------------------------------------------------
    # CHECK 6: Clingo axiom compilation
    # ------------------------------------------------------------------
    try:
        parser.compile_clingo_axioms(str(axioms_path))
        with open(axioms_path, 'r', encoding='utf-8') as f:
            axiom_content = f.read()
        # Verify critical constraints are present
        critical = [
            ":- self_modify(A), not approved(A, akhil).",
            ":- approved(A, akhil), not audit_verified(A, akhil).",
            ":- operator(X), X != akhil.",
            ":- user_command(C), modifies_state(C).",
        ]
        missing = [c for c in critical if c not in axiom_content]
        if missing:
            result.checks["clingo_axioms"] = (False, f"Missing {len(missing)} critical constraints")
        else:
            result.checks["clingo_axioms"] = (True, f"Compiled — {len(axiom_content)} bytes, all critical constraints present")
        result.axioms_path = str(axioms_path)
    except Exception as e:
        result.checks["clingo_axioms"] = (False, f"Compilation failed: {e}")

    # ------------------------------------------------------------------
    # CHECK 7: Interaction logger
    # ------------------------------------------------------------------
    try:
        from flux.teaching.interaction_logger import InteractionLogger
        log_path = log_dir / "flux_interactions.jsonl"
        os.makedirs(log_dir, exist_ok=True)
        logger = InteractionLogger(str(log_path))
        result.logger = logger
        result.checks["interaction_logger"] = (True, str(log_path))
    except Exception as e:
        result.checks["interaction_logger"] = (False, f"Logger init failed: {e}")

    # ------------------------------------------------------------------
    # CHECK 8: TC_001 is 'akhil' (boot order invariant)
    # ------------------------------------------------------------------
    akhil_concept = graph.nodes.get("akhil")
    if akhil_concept and akhil_concept.type == "CONCEPT":
        is_a = akhil_concept.properties.get("is_a", "")
        result.checks["tc_001_akhil"] = (is_a == "AKHIL", f"TC_001 concept='akhil', is_a={is_a}")
    else:
        result.checks["tc_001_akhil"] = (False, "akhil concept node not found")

    # ------------------------------------------------------------------
    # READINESS SCORE
    # ------------------------------------------------------------------
    # Weight the checks: integrity and operator identity are critical (2x weight)
    weights = {
        "constitution_exists": 1,
        "integrity_hash": 2,
        "primitives": 1,
        "concepts": 1,
        "edges": 1,
        "grammar_blocks": 1,
        "benchmarks": 1,
        "operator_identity": 2,
        "sole_operator": 2,
        "private_key": 0.5,      # Not required to boot
        "clingo_axioms": 2,
        "interaction_logger": 1,
        "tc_001_akhil": 1,
    }
    total_weight = sum(weights.get(k, 1) for k in result.checks)
    earned = sum(weights.get(k, 1) for k, (ok, _) in result.checks.items() if ok)
    result.readiness_score = earned / total_weight if total_weight > 0 else 0.0

    result.boot_time_ms = (time.perf_counter() - t_start) * 1000
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    arg_parser = argparse.ArgumentParser(
        description="FLUX Bootstrap Runner — boots the cognitive architecture from zero to live."
    )
    arg_parser.add_argument(
        "--primitives", type=str, default=None,
        help=f"Path to FLUX_PRIMITIVES.md (default: {PRIMITIVES_DEFAULT})"
    )
    arg_parser.add_argument(
        "--axioms", type=str, default=None,
        help=f"Path for compiled Clingo axioms (default: {AXIOMS_DEFAULT})"
    )
    arg_parser.add_argument(
        "--log-dir", type=str, default=None,
        help=f"Directory for interaction logs (default: {LOG_DIR_DEFAULT})"
    )
    arg_parser.add_argument(
        "--verify-only", action="store_true",
        help="Only verify constitutional integrity, don't full-boot"
    )
    args = arg_parser.parse_args()

    try:
        result = boot(
            primitives_path=args.primitives,
            axioms_path=args.axioms,
            log_dir=args.log_dir,
            verify_only=args.verify_only,
        )
        print(result.summary())

        if not result.ready:
            sys.exit(1)

    except BootError as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
