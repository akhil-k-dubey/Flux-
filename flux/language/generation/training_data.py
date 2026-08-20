"""
flux/language/generation/training_data.py

Auto-generate the INPUT side of training pairs from all KG nodes.

For each node, produces a structured text block that captures:
  - Node name + type
  - Definition
  - Examples / non-examples
  - Top connections (relation + target)
  - Dependencies (requires)

Output: logs/generation/train_inputs.jsonl
Each line: {"concept_id": "entropy", "input": "<structured text>", "output": ""}

The "output" field is intentionally blank — you fill it in for 20-30 concepts
by reading the TemplateFormatter's current output and editing it toward fluency.
Those corrections become the gold training data for FluxLanguageModel.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flux.kg.store import KGStore

log = logging.getLogger(__name__)

OUTPUT_PATH = Path("logs/generation/train_inputs.jsonl")
GOLD_PATH   = Path("logs/generation/train_gold.jsonl")   # you fill this


# ── Field formatters ─────────────────────────────────────────────────────────

def _examples_str(examples) -> str:
    if not examples:
        return ""
    if isinstance(examples, list):
        return " | ".join(str(e).strip() for e in examples[:3] if e)
    return str(examples).strip()


def _connections_str(edges: list[dict], store: "KGStore", max_edges: int = 5) -> str:
    """Format edges as 'target (RELATION)' strings, top by edge order."""
    parts = []
    for edge in edges[:max_edges]:
        tgt  = edge.get("to", "")
        rel  = edge.get("type", "RELATES_TO")
        if tgt:
            parts.append(f"{tgt.replace('_', ' ')} ({rel.lower()})")
    return ", ".join(parts)


def build_input_text(concept_id: str, node: dict, store: "KGStore") -> str:
    """
    Build the structured INPUT text for a single KG node.

    Format:
        concept: entropy
        type: CONCEPT
        definition: A state function measuring...
        examples: gas expanding into vacuum | ice melting at 0°C
        non_examples: Entropy is not simply 'disorder'...
        connects_to: gibbs_free_energy (enables), carnot_cycle (enables)
        requires: second_law_thermodynamics, enthalpy
    """
    lines = [f"concept: {concept_id.replace('_', ' ')}"]

    node_type = node.get("type", "")
    if node_type:
        lines.append(f"type: {node_type}")

    defn = (node.get("definition") or "").strip()
    if defn:
        lines.append(f"definition: {defn}")

    ex = _examples_str(node.get("examples"))
    if ex:
        lines.append(f"examples: {ex}")

    ne = _examples_str(node.get("non_examples"))
    if ne:
        lines.append(f"non_examples: {ne}")

    # Edges from the store
    edges = store.edges_from(concept_id)
    requires = [e for e in edges if e.get("type") == "REQUIRES"]
    connects = [e for e in edges if e.get("type") not in ("REQUIRES", "IS_A")]

    if connects:
        lines.append(f"connects_to: {_connections_str(connects, store, 5)}")

    if requires:
        req_names = [e.get("to", "").replace("_", " ") for e in requires[:4]]
        lines.append(f"requires: {', '.join(req_names)}")

    conf = node.get("confidence")
    if conf is not None:
        lines.append(f"confidence: {float(conf):.3f}")

    return "\n".join(lines)


def generate_all_inputs(store: "KGStore") -> list[dict]:
    """
    Generate structured input texts for all KG nodes (excluding internal types).
    Returns list of dicts: {concept_id, input, output (blank)}
    """
    nodes  = store.all_nodes()
    skip   = {"ACTION_CONSEQUENCE_RECORD", "CONSEQUENCE", "QUERY_CONSEQUENCE"}
    result = []

    for cid, node in sorted(nodes.items()):
        if node.get("type") in skip:
            continue
        if cid.startswith(("axiom_", "__", "consequence_query_")):
            continue
        defn = node.get("definition", "")
        if not defn:
            continue   # skip nodes with no definition

        input_text = build_input_text(cid, node, store)
        result.append({
            "concept_id": cid,
            "input":      input_text,
            "output":     "",   # ← you write this for the 20-30 gold examples
        })

    log.info("Generated %d training inputs from %d total nodes", len(result), len(nodes))
    return result


def save_inputs(records: list[dict], path: Path = OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.info("Saved %d training inputs → %s", len(records), path)


def load_gold(path: Path = GOLD_PATH) -> list[dict]:
    """Load gold training pairs (input + hand-written output)."""
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if rec.get("input") and rec.get("output"):
                    records.append(rec)
    log.info("Loaded %d gold training pairs from %s", len(records), path)
    return records


def merge_corrections_from_log(
    log_path:  Path = Path("logs/flux_interactions.jsonl"),
    gold_path: Path = GOLD_PATH,
    store=None,
) -> dict:
    """
    Scan the InteractionLogger JSONL for [CORRECT_RESPONSE] blocks that are
    not yet in train_gold.jsonl, and merge them in.

    Called automatically at the start of every trainer run so corrections
    captured during normal Flux usage appear as training pairs without any
    manual copy-paste step.

    Returns {"merged": int, "skipped_dup": int, "skipped_no_output": int}
    """
    from flux.teaching.interaction_logger import InteractionLogger

    if not log_path.exists():
        return {"merged": 0, "skipped_dup": 0, "skipped_no_output": 0}

    il = InteractionLogger(str(log_path))

    # Load existing gold to know which concept_ids are already covered
    existing      = load_gold(gold_path)
    existing_ids  = {r["concept_id"] for r in existing}

    merged              = 0
    skipped_dup         = 0
    skipped_no_output   = 0

    for corr in il.extract_corrections():
        better     = corr.get("better", "").strip()
        concept_id = corr.get("concept_id", "").strip()

        if not better:
            skipped_no_output += 1
            continue

        if concept_id in existing_ids:
            skipped_dup += 1
            continue

        # Build structured input from KG if store is available
        input_text = corr.get("query", "")
        if store is not None:
            try:
                node = store.get(concept_id)
                if node:
                    input_text = build_input_text(concept_id, node, store)
            except Exception:
                pass

        if not input_text:
            input_text = f"concept: {concept_id}\nquery: {corr.get('query', '')}"

        new_pair = {
            "concept_id": concept_id,
            "input":      input_text,
            "output":     better,
        }

        gold_path.parent.mkdir(parents=True, exist_ok=True)
        with gold_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_pair, ensure_ascii=False) + "\n")

        existing_ids.add(concept_id)
        merged += 1
        log.info("merge_corrections: added pair for '%s'", concept_id)

    if merged:
        log.info(
            "merge_corrections_from_log: +%d new pairs → %s  (skipped: %d dup, %d no-output)",
            merged, gold_path, skipped_dup, skipped_no_output,
        )
    return {"merged": merged, "skipped_dup": skipped_dup, "skipped_no_output": skipped_no_output}



if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    sys.path.insert(0, ".")
    from flux.kg.store import get_store

    store   = get_store()
    records = generate_all_inputs(store)
    save_inputs(records)
    print(f"\nGenerated {len(records)} input texts → {OUTPUT_PATH}")
    print("\nSample (entropy):")
    for r in records:
        if r["concept_id"] == "entropy":
            print(r["input"])
            break
