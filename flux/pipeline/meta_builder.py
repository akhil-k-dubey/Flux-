"""
flux/pipeline/meta_builder.py

Aggregates all pipeline stage outputs into flux_meta.json.
This is the single manifest that runner.py reads to orchestrate training
and trainer.py reads to load data paths, vocab, and curriculum order.

Responsibilities:
  - Cross-stage validation (sessions == commits, specs reference valid sessions)
  - Vocab drift detection (hash comparison across delta runs)
  - Gap consolidation from all upstream stages
  - Curriculum summary (counts + rank ranges per source type)
  - Pipeline health: VALID / INVALID status
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from flux.pipeline import schema as S

INPUTS = {
    "kg_seed":      Path("logs/flux_kg_seed.json"),
    "sessions":     Path("logs/flux_sessions.jsonl"),
    "commits":      Path("logs/flux_kg_commits.jsonl"),
    "spec_targets": Path("logs/flux_spec_targets.jsonl"),
    "vocab":        Path("logs/flux_vocab.json"),
}
OUTPUT = Path("logs/flux_meta.json")

# -- Loaders -------------------------------------------------------------------

def _jsonl(path: Path) -> list[dict]:
    if not path.exists(): return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

# -- Validation ----------------------------------------------------------------

def _validate(sessions: list[dict], commits: list[dict], specs: list[dict],
               missing_files: list[str]) -> list[str]:
    """Return list of validation errors. Empty list = pipeline is clean."""
    errors = list(missing_files)   # missing output files are always errors

    sess_ids = {s["session_id"] for s in sessions}
    comm_ids = {c["session_id"] for c in commits}

    # Session / commit 1:1 alignment
    unmatched_sess = sess_ids - comm_ids
    unmatched_comm = comm_ids - sess_ids
    if unmatched_sess:
        errors.append(f"Sessions without commits ({len(unmatched_sess)}): "
                       f"{sorted(unmatched_sess)[:3]}")
    if unmatched_comm:
        errors.append(f"Commits without sessions ({len(unmatched_comm)}): "
                       f"{sorted(unmatched_comm)[:3]}")

    # Spec targets reference valid session IDs
    orphaned = [s["spec_id"] for s in specs if s["session_id"] not in sess_ids]
    if orphaned:
        errors.append(f"Orphaned spec targets ({len(orphaned)}): {orphaned[:3]}")

    # Duplicate session IDs
    dupes = [sid for sid, n in Counter(s["session_id"] for s in sessions).items() if n > 1]
    if dupes:
        errors.append(f"Duplicate session IDs: {dupes}")

    return errors

# -- Gap Consolidation ---------------------------------------------------------

def _gaps(kg: dict, spec_langs_missing: list[str]) -> dict:
    gaps: dict = {}
    kg_gaps = kg.get("gaps", {})

    # From kg_seed: edge training data gap
    etd = kg_gaps.get("edge_training_data", {})
    if etd:
        gaps["edge_training_data"] = etd

    # From kg_seed: unresolved references
    if kg_gaps.get("true_gaps"):
        gaps["unresolved_references"] = kg_gaps["true_gaps"]

    # From spec_builder: missing formal languages
    if spec_langs_missing:
        gaps["missing_spec_languages"] = {
            "missing":        spec_langs_missing,
            "reason":         "No lean4_map fields in FLUX_PRIMITIVES.md",
            "impact":         "Flux cannot learn Lean4 spec generation from primitive data",
            "recommendation": "Add lean4_map: fields to Layer 0 primitives in FLUX_PRIMITIVES.md",
        }

    return gaps

# -- Curriculum Summary --------------------------------------------------------

def _curriculum(sessions: list[dict]) -> dict:
    by_source: dict[str, list[int]] = {}
    for s in sessions:
        by_source.setdefault(s["source"], []).append(s["complexity_rank"])
    return {
        src: {"count": len(ranks), "rank_range": [min(ranks), max(ranks)]}
        for src, ranks in sorted(by_source.items())
    }

# -- Build ---------------------------------------------------------------------

def build() -> dict:
    # Check which pipeline outputs exist
    missing_files = [f"Missing: {p}" for k, p in INPUTS.items() if not p.exists()]

    kg       = _json(INPUTS["kg_seed"])
    sessions = _jsonl(INPUTS["sessions"])
    commits  = _jsonl(INPUTS["commits"])
    specs    = _jsonl(INPUTS["spec_targets"])
    vocab    = _json(INPUTS["vocab"])
    prev     = _json(OUTPUT)          # previous meta for vocab drift check

    ts = datetime.now(timezone.utc).isoformat()

    # Vocab drift
    cur_hash  = vocab.get("vocab_hash", "")
    prev_hash = prev.get("vocab", {}).get("vocab_hash", "")
    drifted   = bool(prev_hash and cur_hash != prev_hash)

    # Spec language gap
    spec_lang_counts  = Counter(s["language"] for s in specs)
    spec_langs_missing = sorted(S.SPEC_LANGUAGES - set(spec_lang_counts.keys()))

    # Validation
    errors = _validate(sessions, commits, specs, missing_files)

    # Commit confidence distribution
    conf_dist = dict(sorted(Counter(
        round(c["ground_truth_commit"]["commit_confidence"], 2) for c in commits
    ).items()))

    meta = {
        "version":         S.VERSION,
        "generated_at":    ts,
        "pipeline_status": "VALID" if not errors else "INVALID",

        "data_paths": {k: str(p) for k, p in INPUTS.items()},

        "statistics": {
            "kg_nodes":         kg.get("node_count", 0),
            "kg_edges":         kg.get("edge_count", 0),
            "kg_spec_nodes":    kg.get("spec_count", 0),
            "sessions":         len(sessions),
            "commits":          len(commits),
            "spec_targets":     len(specs),
            "commit_conf_dist": conf_dist,
        },

        "vocab": {
            "vocab_size":    vocab.get("vocab_size", 0),
            "vocab_hash":    cur_hash,
            "vocab_drifted": drifted,
            "drift_note": (
                f"Vocab changed {prev_hash[:8]} -> {cur_hash[:8]}. "
                "Embedding layer must be reinitialized before fine-tuning."
                if drifted else "Stable"
            ),
        },

        "curriculum": _curriculum(sessions),

        "spec_languages": {
            "present": dict(spec_lang_counts),
            "missing": spec_langs_missing,
        },

        "gaps": _gaps(kg, spec_langs_missing),

        "validation": {
            "passed":     not errors,
            "errors":     errors,
            "checked_at": ts,
        },
    }

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


if __name__ == "__main__":
    S.clear_all_gaps()
    m = build()
    st = m["statistics"]
    print(f"Meta written -> {OUTPUT}")
    print(f"  status:     {m['pipeline_status']}")
    print(f"  sessions:   {st['sessions']}  commits: {st['commits']}  specs: {st['spec_targets']}")
    print(f"  kg_nodes:   {st['kg_nodes']}  kg_edges: {st['kg_edges']}")
    print(f"  vocab:      {m['vocab']['vocab_size']} tokens  "
          f"hash={m['vocab']['vocab_hash'][:8]}  drift={m['vocab']['vocab_drifted']}")
    print(f"  curriculum: { {k: v['count'] for k, v in m['curriculum'].items()} }")
    if m["gaps"]:
        print(f"  gaps:       {list(m['gaps'].keys())}")
    if m["spec_languages"]["missing"]:
        print(f"  [GAP] no specs for: {m['spec_languages']['missing']}")
    if not m["validation"]["passed"]:
        for e in m["validation"]["errors"]:
            print(f"  [ERROR] {e}")
    else:
        print(f"  validation: PASSED")
