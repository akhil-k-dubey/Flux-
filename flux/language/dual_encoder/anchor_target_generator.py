"""
flux/language/dual_encoder/anchor_target_generator.py

Generates (query_text, definition_text) pairs for Phase 1 InfoNCE training.

NO LTC dependency. No network calls. Pure KG-based.

For each concept C with a definition, generate N query phrasings:
  ("what is entropy", "entropy: measure of disorder...")
  ("explain entropy",  "entropy: measure of disorder...")
  ...

These are POSITIVE pairs (query and definition of the same concept).
Phase 1 trains the NodeEncoder to map them close together using InfoNCE.

Saves:
  logs/dual_encoder/query_def_pairs.json   [{"query": ..., "definition": ...}, ...]
  logs/dual_encoder/anchor_meta.json       {n_pairs, n_concepts, ...}
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

PAIRS_PATH = Path("logs/dual_encoder/query_def_pairs.json")
META_PATH  = Path("logs/dual_encoder/anchor_meta.json")

_QUERY_TEMPLATES = [
    lambda n: f"what is {n}",
    lambda n: f"explain {n}",
    lambda n: f"tell me about {n}",
    lambda n: f"define {n}",
    lambda n: f"describe {n}",
    lambda n: n,
    lambda n: f"what does {n} mean",
    lambda n: f"i want to understand {n}",
]


def generate(nodes: dict, force: bool = False) -> list[dict]:
    """
    Generate (query, definition) pairs for all usable KG concepts.

    Args:
        nodes: KGStore.all_nodes()
        force: regenerate even if cached file exists

    Returns:
        list of {"query": str, "definition": str, "concept_id": str}
    """
    if not force and PAIRS_PATH.exists():
        log.info("AnchorPairGen: loading cached pairs from %s", PAIRS_PATH)
        return json.loads(PAIRS_PATH.read_text(encoding="utf-8"))

    PAIRS_PATH.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    usable = [
        cid for cid, nd in nodes.items()
        if not cid.startswith(("axiom_", "__", "consequence_query_"))
        and len(nd.get("definition", "")) > 10
        and nd.get("confidence", 0) > 0.4
    ]

    pairs: list[dict] = []
    for cid in usable:
        name = cid.replace("_", " ")
        defn = nodes[cid].get("definition", "")[:400]
        for tmpl in _QUERY_TEMPLATES:
            pairs.append({"query": tmpl(name), "definition": defn, "concept_id": cid})

    elapsed = time.time() - t0
    log.info("AnchorPairGen: %d pairs from %d concepts in %.2fs", len(pairs), len(usable), elapsed)

    PAIRS_PATH.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    META_PATH.write_text(json.dumps({
        "timestamp":  time.time(),
        "n_pairs":    len(pairs),
        "n_concepts": len(usable),
        "n_templates": len(_QUERY_TEMPLATES),
        "elapsed_s":  round(elapsed, 4),
        "source":     "kg_only_no_ltc",
    }, ensure_ascii=False), encoding="utf-8")

    return pairs


def load() -> list[dict] | None:
    """Load cached pairs, or None if not generated yet."""
    if not PAIRS_PATH.exists():
        return None
    return json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
