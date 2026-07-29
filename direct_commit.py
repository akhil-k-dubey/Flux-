"""
direct_commit.py

Directly commits manually-verified sessions that scored conf=0.000 in
bulk_commit.py (late additions not seen in Phase 1 training).

These sessions are structurally perfect — all required fields present,
rich definitions and examples. The bridge underscores them because the
model never trained on them, not because the content is wrong.

Direct write to KGStore with conf=1.0 (manually verified ground truth).

Run from project root:
  python direct_commit.py
"""
import sys, json, base64, struct
from pathlib import Path

sys.path.insert(0, ".")

from flux.kg.store import get_store
from flux.model.field_encoder import parse_field_spans, EXCLUDE_FIELDS
from flux.pipeline.schema import parse_relates_to, validate_node_type
import unicodedata

VOCAB_PATH    = Path("logs/flux_vocab.json")
SESSIONS_PATH = Path("logs/flux_sessions.jsonl")

# Sessions to directly commit (verified correct, missed by Phase 1 training)
DIRECT_COMMIT_IDS = [
    "real_enthalpy_0a2b175d70cf",
    "real_schrodinger_equation_c081f327ce32",
    "real_gradient_vector_c2363dd80576",
    "real_density_matrix_15cfdd26be89",
    "real_lumen_fsm_1982229f1824",
    "real_curl_and_divergence_3d50b5b7fa87",
]

MANUAL_CONFIDENCE = 1.0   # manually verified ground truth


def decode(b64: str, idx_to_char: dict) -> str:
    raw     = base64.b64decode(b64)
    n       = len(raw) // 2
    indices = struct.unpack(f"<{n}H", raw[:n * 2])
    return "".join(idx_to_char.get(i, "") for i in indices)


def build_node_from_text(text: str, confidence: float) -> tuple[dict, list[dict]]:
    """
    Parse a TEACH block text into a KG node + edge list.
    Mirrors ConsolidationBridge._build_node() exactly.
    """
    text   = unicodedata.normalize("NFKC", text)
    spans  = parse_field_spans(text)

    def extract(fname: str) -> str:
        span = spans.get(fname)
        if not span or span[1] <= span[0]:
            return ""
        return text[span[0]:span[1]].strip()

    def split_pipe(raw: str) -> list[str]:
        return [p.strip() for p in raw.split("|") if p.strip()]

    # Concept ID from 'concept' field (not session 'id')
    concept_id = extract("concept").lower().replace(" ", "_")
    if not concept_id:
        concept_id = extract("id").lower().replace(" ", "_") or "unknown"

    definition   = extract("definition")
    is_a_raw     = extract("is_a")
    layer_raw    = extract("layer") or None
    node_type    = validate_node_type(is_a_raw) if is_a_raw else "CONCEPT"
    examples     = split_pipe(extract("examples"))
    non_examples = split_pipe(extract("non_examples"))

    edges: list[dict] = []
    for tgt, etype in parse_relates_to(extract("relates_to")):
        if tgt and tgt != concept_id:
            edges.append({"from": concept_id, "to": tgt, "type": etype})
    for req in split_pipe(extract("requires")):
        req = req.lower()
        if req and req != concept_id:
            edges.append({"from": concept_id, "to": req, "type": "REQUIRES"})

    node = {
        "id":              concept_id,
        "type":            node_type,
        "layer":           layer_raw,
        "definition":      definition,
        "confidence":      round(confidence, 6),
        "immutable":       False,
        "trainable":       True,
        "complexity_rank": None,
        "source":          "TAUGHT",
        "specs":           {"CLINGO": None, "Z3": None, "LEAN4": None},
        "examples":        examples,
        "non_examples":    non_examples,
    }
    return node, edges


def main() -> None:
    import os
    os.chdir(Path(__file__).parent)

    vocab       = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    idx_to_char = {v: k for k, v in vocab["char_to_idx"].items()}

    sessions = []
    with SESSIONS_PATH.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                sessions.append(json.loads(line))
    sess_map = {s["session_id"]: s for s in sessions}

    store = get_store()
    print(f"KG before: {store}")
    print()

    results = []
    for sid in DIRECT_COMMIT_IDS:
        s = sess_map.get(sid)
        if not s:
            print(f"  NOT FOUND: {sid}")
            continue

        text = decode(s["char_sequence_b64"], idx_to_char)
        node, edges = build_node_from_text(text, MANUAL_CONFIDENCE)

        action = store.write(node, edges)
        results.append((sid, node["id"], action, len(edges)))
        print(f"  {action:<12} {node['id']:<35} ({len(edges)} edges)  [{sid}]")

    print()
    print(f"KG after:  {store}")
    print()

    # Show which dangling edges just resolved
    from bulk_commit import dangling_report
    remaining = dangling_report(store, top_n=15)
    print("Remaining dangling edges after direct commit:")
    if remaining:
        for concept, count in remaining:
            print(f"  {concept:<35} {count} references")
    else:
        print("  None — all edges resolved.")

    print("\nDone.")


if __name__ == "__main__":
    main()
