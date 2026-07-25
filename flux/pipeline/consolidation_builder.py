"""
flux/pipeline/consolidation_builder.py

flux_kg_seed.json + flux_sessions.jsonl -> flux_kg_commits.jsonl
Ground truth commit targets for consolidation_head.py training.
Format mirrors consolidation_head inference output exactly.

commit_confidence ground truth:
  SYNTHETIC : 0.95  (generated from KG -- correct but not directly taught)
  REAL      : 1.0   (directly taught by Akhil -- gold standard)
  NEGATIVE  : 0.0   (corrupted/rejected -- model must abstain, with rejection_reason)
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone
from flux.pipeline import schema as S

KG_SEED  = Path("logs/flux_kg_seed.json")
SESSIONS = Path("logs/flux_sessions.jsonl")
COMMITS  = Path("logs/flux_kg_commits.jsonl")

# -- Commit Assembly -----------------------------------------------------------

def _edges_from(node_id: str, edges: list[dict]) -> list[dict]:
    "Outgoing edges from node, excluding pipeline infrastructure (LAYER_MEMBER, META targets)."
    return [{"from": e["from"], "to": e["to"], "type": e["type"]}
            for e in edges
            if e["from"] == node_id
            and e["type"] != "LAYER_MEMBER"
            and not e["to"].startswith("__")]

def _specs_for(node_id: str, spec_nodes: list[dict], spec_edges: list[dict]) -> list[dict]:
    "SPEC nodes that FORMALIZES -> node_id."
    spec_ids = {e["from"] for e in spec_edges if e["to"] == node_id}
    return [{"node_id": node_id, "language": s["language"], "spec": s["spec_text"]}
            for s in spec_nodes if s["id"] in spec_ids]

def _commit(nodes: list[dict], edges: list[dict], specs: list[dict],
            conf: float, rejection_reason: str | None = None) -> dict:
    c: dict = {
        "nodes":                 nodes,
        "edges":                 edges,
        "spec_proposals":        specs,
        "commit_confidence":     round(conf, 4),
        "requires_akhil_review": conf < S.CONSOLIDATION_THRESHOLD,
    }
    if rejection_reason:
        c["rejection_reason"] = rejection_reason
    return c

def _node_entry(node: dict) -> dict:
    return {k: node[k] for k in ("id", "type", "definition", "confidence", "immutable")}

# -- Session ID Parsing --------------------------------------------------------

def _parse_node_id(session_id: str) -> str:
    "Extract concept id from syn_{node_id}_{rank:04d} or real_{concept_id}_{hash}."
    parts = session_id.split("_")
    return "_".join(parts[1:-1]) if len(parts) > 2 else parts[-1]

# -- Build ---------------------------------------------------------------------

def build(delta: bool = True) -> dict:
    kg         = json.loads(KG_SEED.read_text(encoding="utf-8"))
    node_index = {n["id"]: n for n in kg["nodes"]}
    edges      = kg["edges"]
    spec_nodes = kg["spec_nodes"]
    spec_edges = kg["spec_edges"]

    sessions = [
        json.loads(l) for l in
        SESSIONS.read_text(encoding="utf-8").splitlines() if l.strip()
    ]

    existing_ids: set[str] = set()
    if delta and COMMITS.exists():
        for l in COMMITS.read_text(encoding="utf-8").splitlines():
            if l.strip(): existing_ids.add(json.loads(l)["session_id"])

    COMMITS.parent.mkdir(exist_ok=True)
    ts     = datetime.now(timezone.utc).isoformat()
    counts = {"written": 0, "skipped": 0}

    with COMMITS.open("a" if delta and COMMITS.exists() else "w", encoding="utf-8") as f:

        def _write(sid: str, src: str, layer: str, rank: int,
                   commit: dict, extra: dict | None = None) -> None:
            record: dict = {
                "session_id":          sid,
                "source":              src,
                "layer":               layer,
                "complexity_rank":     rank,
                "active":              True,
                "generated_at":        ts,
                "ground_truth_commit": commit,
            }
            if extra:
                record.update(extra)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["written"] += 1

        for session in sessions:
            sid  = session["session_id"]
            src  = session["source"]
            rank = session["complexity_rank"]

            if delta and sid in existing_ids:
                counts["skipped"] += 1
                continue

            if src == "SYNTHETIC":
                node_id = _parse_node_id(sid)
                node    = node_index.get(node_id)
                commit  = (
                    _commit(
                        nodes=[_node_entry(node)],
                        edges=_edges_from(node_id, edges),
                        specs=_specs_for(node_id, spec_nodes, spec_edges),
                        conf=S.CONFIDENCE_RULES["synthetic_session"],  # 0.95
                    )
                    if node and node.get("trainable", True) and node.get("type") != "META"
                    else _commit([], [], [], 0.0)
                )
                _write(sid, src, session["layer"], rank, commit)

            elif src == "REAL":
                node_id    = _parse_node_id(sid)
                node       = node_index.get(node_id)
                # teach_confidence from [TEACH] block overrides defaults.
                # Enables full gradient: 0.20 (speculative) -> 1.0 (gold standard).
                teach_conf = session.get("teach_confidence")
                if teach_conf is not None:
                    conf = teach_conf
                elif node and node.get("trainable", True):
                    conf = S.CONFIDENCE_RULES["directly_taught"]  # 1.0
                else:
                    conf = 0.3   # novel concept, not yet in KG -- routes to review
                commit = _commit(
                    nodes=[_node_entry(node)] if node else [],
                    edges=_edges_from(node_id, edges) if node else [],
                    specs=_specs_for(node_id, spec_nodes, spec_edges) if node else [],
                    conf=conf,
                )
                _write(sid, src, session["layer"], rank, commit)

            elif src == "NEGATIVE":
                # Rejection ground truth -- model must output empty commit + rejection reason.
                # rejection_reason distinguishes the three failure modes:
                #   IMMUTABLE_NODE   -> constitutional node cannot be rewritten
                #   SCHEMA_VIOLATION -> proposed type/edge not in schema
                #   EMPTY_COMMIT     -> no parseable content, nothing to commit
                neg_type         = session.get("neg_type", "empty")
                rejection_reason = S.NEG_SESSION_TEMPLATES[neg_type]["expected_rejection"]
                commit = _commit([], [], [], 0.0, rejection_reason=rejection_reason)
                _write(sid, src, session["layer"], rank, commit,
                       extra={"neg_type": neg_type})

    return {"written": counts["written"], "skipped": counts["skipped"]}


if __name__ == "__main__":
    S.clear_all_gaps()
    r = build()
    print(f"Commits written -> {COMMITS}")
    print(f"  written: {r['written']}  skipped: {r['skipped']}")
