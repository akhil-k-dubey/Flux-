"""
flux/training/identity_preservation_test.py

Runs before and after any LTC fine-tuning to verify the KG still wins
over learned representations when they conflict.

Usage:
    python3 -m flux.training.identity_preservation_test --baseline
    python3 -m flux.training.identity_preservation_test --post-finetune
    python3 -m flux.training.identity_preservation_test --compare
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_PATH = ROOT / "logs" / "identity_preservation_test.json"

# 20 concepts across domains — definitions are Flux-specific, not textbook copies
TEST_CONCEPTS = [
    "entropy", "superposition", "entanglement", "quantum_decoherence",
    "backpropagation", "batch_normalization", "recursive_self_improvement",
    "flux_identity", "flux_rk4_dynamics", "flux_layer_system",
    "channel_capacity", "path_integral_formulation", "typical_set",
    "heat", "de_broglie_relation", "basal_ganglia",
    "hardware_over_software", "flux_training_phases",
    "flux_confidence_calibration", "decoherence",
]

TEST_QUERIES = {c: f"What is {c.replace('_', ' ')}?" for c in TEST_CONCEPTS}


def _kg_definition(concept_id: str) -> str:
    from flux.kg.store import KGStore
    store = KGStore()
    node = store.get(concept_id)
    if isinstance(node, dict):
        return node.get("definition", "[NOT IN KG]")
    return "[NOT IN KG]"


def _flux_answer(concept_id: str) -> str:
    from flux.interface.flux_interface import FluxInterface
    fi = FluxInterface()
    try:
        return fi.ask(TEST_QUERIES[concept_id])
    except Exception as e:
        return f"[ERROR: {e}]"


def _overlap_score(kg_def: str, answer: str) -> float:
    """Fraction of KG definition key-words (>=4 chars) found in the answer."""
    if not kg_def or kg_def == "[NOT IN KG]":
        return 0.0
    def tok(s): return set(re.findall(r"[a-z]{4,}", s.lower()))
    kg = tok(kg_def)
    return len(kg & tok(answer)) / len(kg) if kg else 0.0


def run_test(label: str) -> dict:
    print(f"\n{'═'*60}\n  Identity Preservation Test — {label.upper()}\n{'═'*60}")
    results = {}
    for cid in TEST_CONCEPTS:
        kg_def  = _kg_definition(cid)
        answer  = _flux_answer(cid)
        score   = _overlap_score(kg_def, answer)
        kg_wins = score >= 0.25
        results[cid] = {
            "kg_definition": kg_def[:200],
            "flux_answer":   answer[:300],
            "overlap_score": round(score, 3),
            "kg_wins":       kg_wins,
        }
        print(f"  {'✓' if kg_wins else '✗'} {cid}: overlap={score:.2f}")

    win_rate   = sum(r["kg_wins"] for r in results.values()) / len(results)
    avg_overlap= sum(r["overlap_score"] for r in results.values()) / len(results)
    print(f"\n  KG win rate : {win_rate:.0%}  ({int(win_rate*len(results))}/{len(results)})")
    print(f"  Avg overlap : {avg_overlap:.3f}")
    return {"label": label, "timestamp": time.time(),
            "kg_win_rate": round(win_rate, 3),
            "avg_overlap":  round(avg_overlap, 3),
            "per_concept":  results}


def save_result(result: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else {}
    data[result["label"]] = result
    RESULTS_PATH.write_text(json.dumps(data, indent=2))
    print(f"\n  Saved → {RESULTS_PATH}")


def compare() -> None:
    if not RESULTS_PATH.exists():
        print("No results. Run --baseline first."); return
    data = json.loads(RESULTS_PATH.read_text())
    if "baseline" not in data:
        print("No baseline. Run --baseline first."); return
    b = data["baseline"]
    print(f"\n{'═'*60}\n  Identity Preservation — Comparison\n{'═'*60}")
    print(f"  {'Concept':<40} {'Base':>7} {'PostFT':>7} {'Δ':>7}")
    print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*7}")
    for cid in TEST_CONCEPTS:
        bs = b["per_concept"].get(cid, {}).get("overlap_score", 0)
        if "post_finetune" in data:
            ps = data["post_finetune"]["per_concept"].get(cid, {}).get("overlap_score", 0)
            d  = ps - bs
            mk = "↑" if d > 0.05 else ("↓" if d < -0.05 else "~")
            print(f"  {cid:<40} {bs:>7.3f} {ps:>7.3f} {mk}{abs(d):>5.3f}")
        else:
            print(f"  {cid:<40} {bs:>7.3f} {'N/A':>7}")
    print(f"\n  Baseline KG win rate: {b['kg_win_rate']:.0%}")
    if "post_finetune" in data:
        p  = data["post_finetune"]
        dw = p["kg_win_rate"] - b["kg_win_rate"]
        print(f"  Post-FT  KG win rate: {p['kg_win_rate']:.0%}")
        verdict = "IDENTITY PRESERVED ✓" if dw >= -0.10 else "CONTAMINATION DETECTED ✗"
        print(f"\n  Verdict: {verdict}  (Δ win rate: {dw:+.0%})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline",      action="store_true")
    ap.add_argument("--post-finetune", action="store_true", dest="post_finetune")
    ap.add_argument("--compare",       action="store_true")
    args = ap.parse_args()
    if args.baseline:
        save_result(run_test("baseline"))
    elif args.post_finetune:
        save_result(run_test("post_finetune"))
        compare()
    elif args.compare:
        compare()
    else:
        ap.print_help()

if __name__ == "__main__":
    main()
