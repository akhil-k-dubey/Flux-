"""
bulk_commit.py

Decodes every REAL and SYNTHETIC training session from flux_sessions.jsonl,
runs each through ConsolidationBridge, and commits passing sessions to the
live KG (logs/flux_kg_live.jsonl).

Output:
  Per-session one-liner (action + confidence + concept_id)
  Final summary: COMMITTED / AUGMENTED / REVIEW / SKIPPED counts
  Dangling edge report: concepts referenced by edges but not yet in KG

Run from project root:
  python bulk_commit.py

The live KG file is append-only — safe to re-run. Concepts already in the KG
will be AUGMENTED (new edges added, confidence updated if higher) rather than
duplicated.
"""
import base64, json, struct, sys, time
from collections import Counter, defaultdict
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).parent
SESSIONS_PATH = ROOT / "logs" / "flux_sessions.jsonl"
VOCAB_PATH    = ROOT / "logs" / "flux_vocab.json"


def decode_session(char_sequence_b64: str, idx_to_char: dict[int, str]) -> str:
    """
    Decode a base64 uint16 char-index sequence back to the original TEACH text.

    Encoding path (session_builder):
        text → NFKC normalize → char_to_idx → uint16 little-endian → base64

    Decoding path (here):
        base64 → uint16 little-endian → idx_to_char → text
    """
    raw     = base64.b64decode(char_sequence_b64)
    n       = len(raw) // 2
    indices = struct.unpack(f"<{n}H", raw[:n * 2])
    return "".join(idx_to_char.get(i, "") for i in indices)


def dangling_report(store, top_n: int = 15) -> list[tuple[str, int]]:
    """
    Find concept IDs referenced by edges in the live KG that don't
    have a corresponding node. Sort by reference count descending.
    """
    tally: Counter = Counter()
    for edges in store._edges_from.values():
        for edge in edges:
            tgt = edge.get("to", "")
            if tgt and store.get(tgt) is None:
                tally[tgt] += 1
    return tally.most_common(top_n)


def main() -> None:
    import os
    os.chdir(ROOT)

    # ── Load vocab ─────────────────────────────────────────────────────────────
    vocab_data  = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    char_to_idx = vocab_data["char_to_idx"]
    idx_to_char = {v: k for k, v in char_to_idx.items()}
    print(f"Vocab: {len(char_to_idx)} chars")

    # ── Load sessions ──────────────────────────────────────────────────────────
    sessions = []
    with SESSIONS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                sessions.append(json.loads(line))

    real_syn  = [s for s in sessions if s.get("source") in ("REAL", "SYNTHETIC")]
    negatives = [s for s in sessions if s.get("source") == "NEGATIVE"]
    print(f"Sessions: {len(sessions)} total  "
          f"({len(real_syn)} REAL+SYNTHETIC to process, "
          f"{len(negatives)} NEGATIVE skipped)\n")

    # Sort: SYNTHETIC first (lower complexity_rank = foundational),
    # then REAL (complexity_rank=9999). Ensures dependencies exist before
    # higher-layer concepts reference them.
    real_syn.sort(key=lambda s: (
        int(s.get("complexity_rank", 9999)),
        s.get("source", ""),
    ))

    # ── Init bridge and store (singleton — bridge writes to store internally) ──
    from flux.model.consolidation_bridge import ConsolidationBridge
    from flux.kg.store import get_store

    bridge = ConsolidationBridge()
    store  = get_store()
    print(f"{bridge}")
    print(f"{store}\n")

    # ── Process sessions ───────────────────────────────────────────────────────
    results: list[dict] = []
    action_counts: Counter = Counter()
    errors: list[str] = []

    print(f"{'#':>4}  {'Source':<10} {'Action':<10} {'Conf':>6}  {'Concept'}")
    print("-" * 70)

    for i, sess in enumerate(real_syn, 1):
        sess_id = sess.get("session_id", f"sess_{i}")
        source  = sess.get("source", "?")
        b64     = sess.get("char_sequence_b64", "")

        if not b64:
            action_counts["SKIPPED"] += 1
            errors.append(f"{sess_id}: no char_sequence_b64")
            continue

        try:
            text   = decode_session(b64, idx_to_char)
            result = bridge.process(text)
            action = result.kg_action if result.decision == "COMMIT" else "REVIEW"
            action_counts[action] += 1

            concept = result.concept_id or "(none)"
            print(f"{i:>4}  {source:<10} {action:<10} {result.confidence:>6.3f}  {concept}")

            results.append({
                "session_id": sess_id,
                "source":     source,
                "action":     action,
                "confidence": result.confidence,
                "concept_id": result.concept_id,
            })

        except Exception as exc:
            action_counts["ERROR"] += 1
            errors.append(f"{sess_id}: {exc}")
            print(f"{i:>4}  {source:<10} ERROR      -       {exc!s:.60}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for action in ("COMMITTED", "AUGMENTED", "REVIEW", "SKIPPED", "ERROR"):
        n = action_counts.get(action, 0)
        if n:
            print(f"  {action:<12} {n:>4} sessions")

    total_committed = action_counts.get("COMMITTED", 0) + action_counts.get("AUGMENTED", 0)
    print(f"\n  KG nodes now:   {store.stats()['nodes']}")
    print(f"  Live nodes:     {store.stats()['live_nodes']}")
    print(f"  Total edges:    {store.stats()['edges']}")

    # ── Dangling edge report ───────────────────────────────────────────────────
    dangling = dangling_report(store, top_n=15)
    if dangling:
        print("\n" + "=" * 70)
        print("DANGLING EDGES (concepts referenced but not yet in KG)")
        print("=" * 70)
        print(f"  {'Concept':<35} {'References':>10}")
        print(f"  {'-'*35}  {'-'*10}")
        for concept, count in dangling:
            print(f"  {concept:<35} {count:>10}")

        print(f"\n  --> Write curated [TEACH] sessions for these concepts first.")
        print(f"      They are referenced by {sum(c for _,c in dangling)} edges "
              f"waiting to resolve.\n")
    else:
        print("\n  No dangling edges — all referenced concepts are in the KG.")

    # ── REVIEW list ───────────────────────────────────────────────────────────
    reviews = [r for r in results if r["action"] == "REVIEW"]
    if reviews:
        print("=" * 70)
        print(f"REVIEW QUEUE ({len(reviews)} sessions below 0.85 threshold)")
        print("=" * 70)
        for r in reviews:
            print(f"  conf={r['confidence']:.3f}  {r['session_id']}")

    if errors:
        print("\nERRORS:")
        for e in errors:
            print(f"  {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
