"""
flux/pipeline/session_builder.py

flux_kg_seed.json + flux_interactions.jsonl -> flux_sessions.jsonl + flux_vocab.json
One session per trainable KG node, curriculum ordered by complexity_rank.
Includes NEGATIVE sessions for rejection-space training (9 total: 3 types x 3 each).
Curriculum order: SYNTHETIC (0..N) -> NEGATIVE (9998) -> REAL (9999).
"""

from __future__ import annotations
import json, base64, hashlib, array, re, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from flux.pipeline import schema as S

KG_SEED      = Path("logs/flux_kg_seed.json")
INTERACTIONS = Path("logs/flux_interactions.jsonl")
SESSIONS     = Path("logs/flux_sessions.jsonl")
VOCAB_OUT    = Path("logs/flux_vocab.json")

SYNTHETIC_WEIGHT = 0.8
REAL_WEIGHT      = 1.0
NEGATIVE_WEIGHT  = 1.0   # negatives weighted same as REAL — rejections are hard to learn
_TEACH_RE        = re.compile(r"\[TEACH\](.*?)\[/TEACH\]", re.DOTALL)

# -- Text Generation -----------------------------------------------------------

def _syn_text(node: dict) -> str:
    rank = node.get("complexity_rank", 0)
    return (
        f"[TEACH]\n"
        f"id: syn_{node['id']}_{rank:04d}\n"
        f"concept: {node['id']}\n"
        f"type: {node['type']}\n"
        f"layer: {node['layer']}\n"
        f"definition: {node['definition']}\n"
        f"complexity_rank: {rank}\n"
        f"[/TEACH]"
    )

def _block_to_text(block: dict) -> str:
    lines = ["[TEACH]"] + [f"{k}: {v}" for k, v in block.items()] + ["[/TEACH]"]
    return "\n".join(lines)

# -- Negative Session Text Generation ------------------------------------------

def _pick_immutable_targets(kg: dict, n: int) -> list[dict]:
    """
    Pick n immutable trainable nodes for negative examples.
    Groups by type first to ensure variety (not 3 LOGICAL nodes — pick from
    LOGICAL, AXIOM, EPISTEMIC if available).
    """
    immutable = [
        node for node in kg["nodes"]
        if node.get("immutable") and node.get("trainable", True) and node.get("type") != "META"
    ]
    # One per distinct type for maximum variety
    by_type: dict[str, dict] = {}
    for node in sorted(immutable, key=lambda nd: nd["id"]):
        by_type.setdefault(node["type"], node)
    targets = list(by_type.values())
    # Pad with LOGICAL nodes if fewer than n types available
    if len(targets) < n:
        extras = [nd for nd in immutable if nd not in targets]
        targets += extras[: n - len(targets)]
    return targets[:n]

def _neg_texts(kg: dict) -> list[tuple[str, str, str]]:
    """
    Generate (text, neg_type, session_id) for all S.NEG_PER_TYPE x 3 negatives.
    Immutable: 3 targets from different node types.
    Schema:    3 distinct invalid types.
    Empty:     3 blank sessions with distinct IDs.
    """
    results: list[tuple[str, str, str]] = []
    immutable_targets = _pick_immutable_targets(kg, S.NEG_PER_TYPE)
    tmpl = S.NEG_SESSION_TEMPLATES

    for i in range(S.NEG_PER_TYPE):
        # -- immutable: each targets a different immutable node type
        sid  = f"neg_immutable_{i:04d}"
        tgt  = immutable_targets[i] if i < len(immutable_targets) else immutable_targets[0]
        text = tmpl["immutable"]["template"].format(
            id=sid, target=tgt["id"], type=tgt["type"]
        )
        results.append((text, "immutable", sid))

        # -- schema: each uses a different invalid type string
        sid  = f"neg_schema_{i:04d}"
        text = tmpl["schema"]["template"].format(
            id=sid, invalid_type=S.SCHEMA_INVALID_TYPES[i]
        )
        results.append((text, "schema", sid))

        # -- empty: distinct IDs, no content
        sid  = f"neg_empty_{i:04d}"
        text = tmpl["empty"]["template"].format(id=sid)
        results.append((text, "empty", sid))

    return results

# -- Vocab & Encoding ----------------------------------------------------------

def build_vocab(texts: list[str]) -> dict[str, int]:
    "char -> index. Index 0 = UNK. Deterministic: sorted chars. NFKC-normalised."
    normalised = [unicodedata.normalize("NFKC", t) for t in texts]
    return {c: i + 1 for i, c in enumerate(sorted({c for t in normalised for c in t}))}

def _normalise(text: str) -> str:
    "Apply NFKC normalisation — composes combining marks, decomposes compatibility chars."
    return unicodedata.normalize("NFKC", text)

def encode(text: str, vocab: dict[str, int]) -> tuple[str, int]:
    "Text -> base64-encoded uint16 index array + character length. NFKC-normalised."
    text = _normalise(text)
    buf = array.array("H", (vocab.get(c, 0) for c in text))
    return base64.b64encode(buf.tobytes()).decode("ascii"), len(text)

def _content_hash(node_or_text) -> str:
    key = (
        f"{node_or_text['id']}|{node_or_text['definition']}|{node_or_text['type']}"
        if isinstance(node_or_text, dict) else node_or_text
    )
    return hashlib.md5(key.encode()).hexdigest()[:12]

# -- Delta Mode ----------------------------------------------------------------

def _load_existing_hashes(path: Path) -> dict[str, str]:
    "Returns {session_id: content_hash} for all active sessions on disk."
    if not path.exists(): return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        r = json.loads(line)
        if r.get("active", True):
            out[r["session_id"]] = r.get("content_hash", "")
    return out

# -- Real Session Parser -------------------------------------------------------

def _load_real(path: Path, kg_index: dict[str, dict]) -> list[dict]:
    """
    Parse flux_interactions.jsonl for teach entries.
    Uses parsed_blocks["TEACH"] when present; falls back to regex on raw content.
    """
    if not path.exists(): return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        entry = json.loads(line)
        if entry.get("message_type") != "teach": continue
        blocks = entry.get("parsed_blocks", {}).get("TEACH", [])
        if not blocks:
            blocks = [{"raw": m.group(1).strip()}
                      for m in _TEACH_RE.finditer(entry.get("content", ""))]
        for b in blocks:
            concept_id = b.get("concept", b.get("id", "unknown")).lower().strip()
            results.append({
                "text":            _block_to_text(b),
                "concept_id":      concept_id,
                "layer":           kg_index.get(concept_id, {}).get("layer", "LAYER_UNKNOWN"),
                "teach_confidence": float(b.get("confidence", 1.0)),
            })
    return results

# -- Build ---------------------------------------------------------------------

def build(delta: bool = True) -> dict:
    kg       = json.loads(KG_SEED.read_text(encoding="utf-8"))
    kg_index = {n["id"]: n for n in kg["nodes"]}

    trainable = sorted(
        [n for n in kg["nodes"] if n.get("trainable", True)],
        key=lambda n: n.get("complexity_rank", 999),
    )
    real         = _load_real(INTERACTIONS, kg_index)
    neg_triples  = _neg_texts(kg)      # [(text, neg_type, sid), ...]

    # Pass 1 -- all texts together so vocab covers every character.
    # Spec texts included so #program, :- and ! in Clingo axioms aren't UNK.
    syn_texts  = [_syn_text(n) for n in trainable]
    real_texts = [r["text"] for r in real]
    neg_texts  = [t for t, _, _ in neg_triples]
    spec_texts = [s["spec_text"] for s in kg.get("spec_nodes", [])]
    vocab      = build_vocab(syn_texts + real_texts + neg_texts + spec_texts)
    vocab_hash = hashlib.md5(json.dumps(vocab, sort_keys=True).encode()).hexdigest()

    existing = _load_existing_hashes(SESSIONS) if delta else {}
    counts   = {"syn": 0, "real": 0, "neg": 0, "skipped": 0}
    ts       = datetime.now(timezone.utc).isoformat()
    SESSIONS.parent.mkdir(exist_ok=True)

    with SESSIONS.open("a" if delta and SESSIONS.exists() else "w", encoding="utf-8") as f:

        # -- Synthetic sessions (curriculum positions 0 .. N)
        for node, text in zip(trainable, syn_texts):
            rank = node.get("complexity_rank", 0)
            sid  = f"syn_{node['id']}_{rank:04d}"
            nhsh = _content_hash(node)
            if delta and existing.get(sid) == nhsh:
                counts["skipped"] += 1; continue
            b64, slen = encode(text, vocab)
            f.write(json.dumps({
                "session_id":        sid,
                "source":            "SYNTHETIC",
                "layer":             node["layer"],
                "complexity_rank":   rank,
                "active":            True,
                "superseded_by":     None,
                "weight":            SYNTHETIC_WEIGHT,
                "content_hash":      nhsh,
                "char_sequence_b64": b64,
                "char_sequence_len": slen,
                "generated_at":      ts,
            }, ensure_ascii=False) + "\n")
            counts["syn"] += 1

        # -- Negative sessions (curriculum position 9998 -- after positives, before REAL)
        for text, neg_type, sid in zip(neg_texts, [nt for _, nt, _ in neg_triples],
                                                   [s  for _, _,  s in neg_triples]):
            if delta and sid in existing:
                counts["skipped"] += 1; continue
            b64, slen = encode(text, vocab)
            f.write(json.dumps({
                "session_id":        sid,
                "source":            "NEGATIVE",
                "neg_type":          neg_type,
                "expected_rejection": S.NEG_SESSION_TEMPLATES[neg_type]["expected_rejection"],
                "layer":             "NEGATIVE",
                "complexity_rank":   9998,
                "active":            True,
                "superseded_by":     None,
                "weight":            NEGATIVE_WEIGHT,
                "content_hash":      _content_hash(text),
                "char_sequence_b64": b64,
                "char_sequence_len": slen,
                "generated_at":      ts,
            }, ensure_ascii=False) + "\n")
            counts["neg"] += 1

        # -- Real sessions (curriculum position 9999 -- last, gold standard)
        for r, text in zip(real, real_texts):
            chsh = _content_hash(text)
            sid  = f"real_{r['concept_id']}_{chsh}"
            if delta and sid in existing:
                counts["skipped"] += 1; continue
            b64, slen = encode(text, vocab)
            f.write(json.dumps({
                "session_id":        sid,
                "source":            "REAL",
                "layer":             r["layer"],
                "complexity_rank":   9999,
                "active":            True,
                "superseded_by":     None,
                "weight":            REAL_WEIGHT,
                "content_hash":      chsh,
                "char_sequence_b64": b64,
                "char_sequence_len": slen,
                "teach_confidence":  r["teach_confidence"],
                "generated_at":      ts,
            }, ensure_ascii=False) + "\n")
            counts["real"] += 1

    VOCAB_OUT.write_text(
        json.dumps({
            "vocab_size":  len(vocab) + 1,
            "vocab_hash":  vocab_hash,
            "char_to_idx": vocab,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "total_sessions": counts["syn"] + counts["neg"] + counts["real"],
        "synthetic":      counts["syn"],
        "negative":       counts["neg"],
        "real":           counts["real"],
        "skipped_delta":  counts["skipped"],
        "vocab_size":     len(vocab) + 1,
        "vocab_hash":     vocab_hash,
    }


if __name__ == "__main__":
    S.clear_all_gaps()
    r = build()
    print(f"Sessions -> {SESSIONS}")
    print(f"  synthetic: {r['synthetic']}  negative: {r['negative']}  "
          f"real: {r['real']}  skipped: {r['skipped_delta']}")
    print(f"Vocab    -> {VOCAB_OUT}  ({r['vocab_size']} tokens, hash={r['vocab_hash'][:8]})")
