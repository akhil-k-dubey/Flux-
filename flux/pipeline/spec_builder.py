"""
flux/pipeline/spec_builder.py

flux_kg_seed.json + flux_sessions.jsonl -> flux_spec_targets.jsonl
Training targets for spec generation: (session_text → formal_spec) pairs.
One record per (concept, language) pair. Currently: CLINGO + Z3 only.

Training mode: verified_by="origin" — specs come from FLUX_PRIMITIVES.md (constitutional).
Inference mode: consolidation_bridge runs full Clingo/Z3/Lean4 verification on novel proposals.

Trainer uses:
  session.char_sequence_b64  (input  — loaded by join on session_id)
  spec_target.spec_text      (target — encoded at train time via vocab.json)
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone
from flux.pipeline import schema as S

KG_SEED   = Path("logs/flux_kg_seed.json")
SESSIONS  = Path("logs/flux_sessions.jsonl")
SPEC_OUT  = Path("logs/flux_spec_targets.jsonl")

# -- Session Lookup ------------------------------------------------------------

def _session_index(path: Path) -> dict[str, str]:
    "concept_id -> session_id for all SYNTHETIC sessions."
    if not path.exists(): return {}
    idx = {}
    for l in path.read_text(encoding="utf-8").splitlines():
        if not l.strip(): continue
        r = json.loads(l)
        if r["source"] != "SYNTHETIC": continue
        # session_id = syn_{concept_id}_{rank:04d} -> strip prefix + suffix
        parts = r["session_id"].split("_")
        concept_id = "_".join(parts[1:-1])
        idx[concept_id] = r["session_id"]
    return idx

# -- Build --------------------------------------------------------------------

def build(delta: bool = True) -> dict:
    kg         = json.loads(KG_SEED.read_text(encoding="utf-8"))
    node_index = {n["id"]: n for n in kg["nodes"]}
    sessions   = _session_index(SESSIONS)

    # Group spec nodes by concept: concept_id -> [spec_node, ...]
    spec_by_concept: dict[str, list[dict]] = {}
    spec_map = {s["id"]: s for s in kg["spec_nodes"]}
    for edge in kg["spec_edges"]:
        if edge["type"] != "FORMALIZES": continue
        spec_by_concept.setdefault(edge["to"], []).append(spec_map[edge["from"]])

    existing_ids: set[str] = set()
    if delta and SPEC_OUT.exists():
        for l in SPEC_OUT.read_text(encoding="utf-8").splitlines():
            if l.strip(): existing_ids.add(json.loads(l)["spec_id"])

    SPEC_OUT.parent.mkdir(exist_ok=True)
    ts     = datetime.now(timezone.utc).isoformat()
    counts = {"written": 0, "skipped": 0, "orphaned": 0}
    missing_langs: set[str] = set()

    with SPEC_OUT.open("a" if delta and SPEC_OUT.exists() else "w", encoding="utf-8") as f:
        for concept_id, specs in sorted(spec_by_concept.items(),
                                         key=lambda kv: node_index.get(kv[0], {}).get("complexity_rank", 999)):
            node       = node_index.get(concept_id, {})
            session_id = sessions.get(concept_id)

            if not session_id:
                counts["orphaned"] += 1   # spec with no matching session -- flag but don't crash
                continue

            for spec in sorted(specs, key=lambda s: len(s["spec_text"])):
                sid = spec["id"]
                if delta and sid in existing_ids:
                    counts["skipped"] += 1
                    continue

                lang = spec["language"]
                if lang not in S.SPEC_LANGUAGES:
                    missing_langs.add(lang)

                f.write(json.dumps({
                    "spec_id":         sid,
                    "session_id":      session_id,
                    "concept_id":      concept_id,
                    "language":        lang,
                    "spec_text":       spec["spec_text"],
                    "layer":           node.get("layer", "LAYER_UNKNOWN"),
                    "complexity_rank": node.get("complexity_rank", 999),
                    "immutable":       node.get("immutable", False),
                    # Training mode: trusted by constitutional origin.
                    # Inference: consolidation_bridge verifies with Clingo/Z3/Lean4.
                    "verified":        True,
                    "verified_by":     "origin",
                    "verification_result": {
                        "passed":      True,
                        "trace":       "Pre-verified: source is FLUX_PRIMITIVES.md",
                        "language":    lang,
                        "duration_ms": 0.0,
                    },
                    "generated_at":    ts,
                }, ensure_ascii=False) + "\n")
                counts["written"] += 1

    # Language gap report — Lean4 will show up here until we add it to primitives
    lang_counts = {}
    for specs in spec_by_concept.values():
        for s in specs:
            lang_counts[s["language"]] = lang_counts.get(s["language"], 0) + 1
    missing_spec_langs = sorted(S.SPEC_LANGUAGES - set(lang_counts.keys()))

    return {
        "written":            counts["written"],
        "skipped":            counts["skipped"],
        "orphaned":           counts["orphaned"],
        "language_counts":    lang_counts,
        "missing_languages":  missing_spec_langs,  # LEAN4 gap shows here
    }


if __name__ == "__main__":
    S.clear_all_gaps()
    r = build()
    print(f"Spec targets -> {SPEC_OUT}")
    print(f"  written: {r['written']}  skipped: {r['skipped']}  orphaned: {r['orphaned']}")
    print(f"  languages: {r['language_counts']}")
    if r["missing_languages"]:
        print(f"  [GAP] no specs for: {r['missing_languages']} -- add lean4_map fields to primitives")
