"""
flux/sensors/grounded_session_builder.py

Stage-B part 1: converts KG ACTION_CONSEQUENCE_RECORD nodes into
training sessions (same format as flux_sessions.jsonl, source="GROUNDED").

Key difference from normal sessions:
  - conf_target = observation_quality (not a commit confidence)
  - grounding_type field is preserved — LTC learns from OBSERVED evidence
  - grounding_source points back to the KG node for provenance tracing

Sessions are written to logs/flux_grounded_sessions.jsonl.
A watermark (logs/flux_grounded_watermark.json) prevents reprocessing.
Training triggers when accumulated sessions reach TRIGGER_THRESHOLD.
"""
from __future__ import annotations

import array, base64, hashlib, json, time, unicodedata
from pathlib import Path

GROUNDED_SESSIONS = Path("logs/flux_grounded_sessions.jsonl")
GROUNDED_WM       = Path("logs/flux_grounded_watermark.json")
VOCAB_PATH        = Path("logs/flux_vocab.json")
TRIGGER_THRESHOLD = 10     # queue up 10 grounded sessions before training fires


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _encode(text: str, vocab: dict[str, int]) -> str:
    text = _normalise(text)
    buf  = array.array("H", (vocab.get(c, 0) for c in text))
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _load_vocab() -> dict[str, int] | None:
    try:
        return json.loads(VOCAB_PATH.read_text())["char_to_idx"]
    except Exception:
        return None


def _load_watermark() -> set[str]:
    """Return set of already-processed node IDs."""
    try:
        if GROUNDED_WM.exists():
            return set(json.loads(GROUNDED_WM.read_text())["processed"])
    except Exception:
        pass
    return set()


def _save_watermark(processed: set[str]) -> None:
    GROUNDED_WM.parent.mkdir(parents=True, exist_ok=True)
    GROUNDED_WM.write_text(json.dumps({"processed": sorted(processed)}))


def _make_teach_text(node: dict) -> str:
    """Convert a consequence node into a [TEACH] block the LTC can train on."""
    return (
        f"[TEACH]\n"
        f"id: {node['id']}\n"
        f"concept: {node['id']}\n"
        f"type: ACTION_CONSEQUENCE_RECORD\n"
        f"action: {node.get('action', 'UNKNOWN')}\n"
        f"caused: {node.get('caused', 'no_change')}\n"
        f"definition: {node.get('definition', '')}\n"
        f"grounding_type: {node.get('grounding_type', 'DIRECT_OBSERVATION')}\n"
        f"grounding_source: {node['id']}\n"
        f"confidence: {node.get('confidence', 0.7)}\n"
        f"[/TEACH]"
    )


def run(store) -> dict:
    """
    Read new ACTION_CONSEQUENCE_RECORD nodes from KG and write grounded sessions.
    Returns {written, skipped, pending_total, trigger_ready}.
    """
    vocab = _load_vocab()
    if vocab is None:
        return {"written": 0, "skipped": 0, "pending_total": 0,
                "trigger_ready": False, "error": "vocab not found"}

    processed = _load_watermark()
    consequence_nodes = {
        nid: node for nid, node in store.all_nodes().items()
        if node.get("type") == "ACTION_CONSEQUENCE_RECORD"
    }

    written = skipped = 0
    GROUNDED_SESSIONS.parent.mkdir(parents=True, exist_ok=True)

    with GROUNDED_SESSIONS.open("a", encoding="utf-8") as f:
        for nid, node in consequence_nodes.items():
            if nid in processed:
                skipped += 1
                continue

            quality = node.get("observation_quality", 0.7)
            text    = _make_teach_text(node)
            b64     = _encode(text, vocab)
            sid     = f"grounded_{nid}"

            record = {
                "session_id":        sid,
                "source":            "GROUNDED",
                "complexity_rank":   9997,          # after SYNTHETIC, before NEGATIVE
                "weight":            float(quality),
                "active":            True,
                "char_sequence_b64": b64,
                "char_len":          len(_normalise(text)),
                "conf_target":       float(quality),
                "grounding_type":    node.get("grounding_type", "DIRECT_OBSERVATION"),
                "grounding_source":  nid,
                "timestamp":         time.time(),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            processed.add(nid)
            written += 1

    _save_watermark(processed)

    # Count total pending (written but not yet trained on)
    pending = _count_pending()
    trigger = pending >= TRIGGER_THRESHOLD

    return {
        "written":       written,
        "skipped":       skipped,
        "pending_total": pending,
        "trigger_ready": trigger,
    }


def _count_pending() -> int:
    """Count sessions in flux_grounded_sessions.jsonl."""
    if not GROUNDED_SESSIONS.exists():
        return 0
    return sum(1 for l in GROUNDED_SESSIONS.read_text().splitlines() if l.strip())
