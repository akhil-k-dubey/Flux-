"""
flux/training/domain_reclassifier.py

Assigns correct domains to ALL domain=unknown nodes using pure topology.
Handles both live JSONL nodes and seed-only nodes.

Two-pass strategy:
  Pass 1 — Declarative constitutional assignment (architectural facts, not patches):
    Layer 0–3 (LOGICAL, EPISTEMIC, RELATIONAL, ACTION)  → constitutional
    Layer S   (AXIOM)                                   → constitutional
    Layer 4   (DOMAIN type)                             → flux_architecture
    META sentinels (__layer_X__)                        → skip
    ENGINE type (lumen subsystems)                      → flux_architecture

  Pass 2 — Topology inference (majority vote from domain-tagged neighbors):
    Any remaining domain=unknown node — vote from what it REQUIRES/ENABLES.
    Require >= 2 neighbors agreeing on the same domain.

Write-back:
  - Live JSONL nodes  → rewritten in place
  - Seed-only nodes   → appended to live JSONL as override records
    (KGStore loads live AFTER seed and overwrites, so these take effect)
  - All corrections logged to flux_sensors.jsonl

Usage:
    python3 -m flux.training.domain_reclassifier            # fix all unknown
    python3 -m flux.training.domain_reclassifier --dry-run  # preview only
    python3 -m flux.training.domain_reclassifier --source-domain python  # original mode
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

LIVE_KG  = Path("logs/flux_kg_live.jsonl")
SEED_KG  = Path("logs/flux_kg_seed.json")
SENSORS  = Path("logs/flux_sensors.jsonl")

# Edges that carry semantic domain signal
_SEMANTIC_EDGES = {"REQUIRES", "ENABLES", "ANALOGOUS_TO", "GENERALIZES",
                   "SPECIFIES", "USES", "CONTRASTS", "CONNECTS"}

# Pass 1 — Constitutional assignment rules (architectural facts, not keyword heuristics)
# These are STRUCTURAL properties of the node, not content-based guesses.
_CONSTITUTIONAL_LAYERS  = {"LAYER_0", "LAYER_1", "LAYER_2", "LAYER_3", "LAYER_S"}
_CONSTITUTIONAL_TYPES   = {"LOGICAL", "EPISTEMIC", "RELATIONAL", "ACTION", "AXIOM"}
_FLUX_ARCH_LAYER4_TYPES = {"DOMAIN"}
_FLUX_ARCH_TYPES        = {"ENGINE"}  # Lumen subsystem nodes, etc.


def _load_live_records(path: Path) -> tuple[dict[str, dict], list[dict]]:
    """Load node and edge records from JSONL. Returns (nodes, edges)."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        data     = rec.get("data", {})
        rec_type = rec.get("record_type", "")
        if rec_type == "node":
            nid = data.get("id")
            if nid:
                nodes[nid] = data
        elif rec_type == "edge":
            frm = data.get("from", "")
            to  = data.get("to", "")
            if frm and to:
                edges.append({"from": frm, "to": to, "type": data.get("type", "")})
    return nodes, edges


def _load_seed_nodes(path: Path) -> dict[str, dict]:
    """Load all nodes from the constitutional seed JSON."""
    if not path.exists():
        return {}
    seed = json.loads(path.read_text())
    nodes = seed.get("nodes", [])
    return {n["id"]: n for n in nodes if "id" in n}


def _build_neighbors(all_nodes: dict, edges: list[dict]) -> dict[str, list[str]]:
    """Build bidirectional adjacency via semantic edges only."""
    adj: dict[str, list[str]] = {nid: [] for nid in all_nodes}
    for e in edges:
        frm, to, typ = e["from"], e["to"], e.get("type", "")
        if typ not in _SEMANTIC_EDGES:
            continue
        if frm in adj:
            adj[frm].append(to)
        if to in adj:
            adj[to].append(frm)
    return adj


def _topology_vote(nid: str, all_nodes: dict, adj: dict) -> str | None:
    """Majority vote domain from neighbors. Returns domain or None."""
    counts: Counter = Counter()
    for nbr in adj.get(nid, []):
        nbr_node = all_nodes.get(nbr)
        if not nbr_node:
            continue
        d = nbr_node.get("domain", "unknown")
        if d and d not in ("unknown", "constitutional"):
            counts[d] += 1
    if not counts:
        return None
    top, top_count = counts.most_common(1)[0]
    total = sum(counts.values())
    if top_count >= 2 and (top_count / total) >= 0.50:
        return top
    return None


def _constitutional_domain(node: dict) -> str | None:
    """
    Pass 1: assign domain based purely on layer/type structural facts.
    Returns the domain string, or None if no rule applies.
    """
    nid  = node.get("id", "")
    typ  = node.get("type", "")
    lyr  = node.get("layer", "")

    # Infrastructure sentinels — skip entirely
    if typ == "META":
        return None

    # Layer 0–3 logical/epistemic/relational/action primitives → constitutional
    if lyr in _CONSTITUTIONAL_LAYERS and typ in _CONSTITUTIONAL_TYPES:
        return "constitutional"

    # Layer 4 DOMAIN type nodes → flux_architecture
    if lyr == "LAYER_4" and typ in _FLUX_ARCH_LAYER4_TYPES:
        return "flux_architecture"

    # ENGINE type (Lumen subsystem nodes) → flux_architecture
    if typ in _FLUX_ARCH_TYPES:
        return "flux_architecture"

    return None


def run(source_domain: str | None = None, dry_run: bool = False) -> None:
    """
    Main reclassification pass.
    source_domain: if set, only reclassify nodes with that specific domain.
    If None, reclassify all domain=unknown nodes.
    """
    log.info("Loading seed nodes from %s ...", SEED_KG)
    seed_nodes = _load_seed_nodes(SEED_KG)
    log.info("  %d seed nodes loaded", len(seed_nodes))

    log.info("Loading live records from %s ...", LIVE_KG)
    live_nodes, edges = _load_live_records(LIVE_KG)
    log.info("  %d live nodes, %d edges loaded", len(live_nodes), len(edges))

    # Merge: live overrides seed (matches KGStore load order)
    all_nodes: dict[str, dict] = {**seed_nodes, **live_nodes}
    log.info("  %d total nodes in merged view", len(all_nodes))

    adj = _build_neighbors(all_nodes, edges)

    # Which nodes are candidates?
    if source_domain:
        candidates = {nid: n for nid, n in all_nodes.items()
                      if n.get("domain", "unknown") == source_domain}
        log.info("  %d nodes with domain=%s", len(candidates), source_domain)
    else:
        candidates = {nid: n for nid, n in all_nodes.items()
                      if n.get("domain", "unknown") in ("unknown", None, "")}
        log.info("  %d nodes with domain=unknown", len(candidates))

    corrections: dict[str, str] = {}   # nid → new_domain
    log_events:  list[dict]     = []

    for nid, node in candidates.items():
        # Pass 1: constitutional structural rules
        new_domain = _constitutional_domain(node)
        method     = "constitutional_layer_type"

        # Pass 2: topology vote for remaining
        if new_domain is None:
            new_domain = _topology_vote(nid, all_nodes, adj)
            method     = "kg_neighbor_majority_vote"

        if new_domain is None:
            log.debug("  UNRESOLVABLE  %s — no rule matched, no neighbor consensus", nid)
            continue

        old_domain = node.get("domain", "unknown")
        if new_domain == old_domain:
            continue

        corrections[nid] = new_domain
        log_events.append({
            "node_id":    nid,
            "old_domain": old_domain,
            "new_domain": new_domain,
            "method":     method,
        })
        log.info("  %-10s  %-45s  %s -> %s",
                 method[:10], nid, old_domain or "unknown", new_domain)

    log.info("\n  Total to reclassify: %d / %d candidates", len(corrections), len(candidates))

    if dry_run:
        log.info("DRY RUN — no changes written.")
        return

    if not corrections:
        log.info("Nothing to reclassify.")
        return

    # Which corrected nodes are in live JSONL vs seed-only?
    in_live    = {nid for nid in corrections if nid in live_nodes}
    seed_only  = {nid for nid in corrections if nid not in live_nodes}
    log.info("  %d corrections to live JSONL, %d new override records for seed nodes",
             len(in_live), len(seed_only))

    # ── Rewrite live JSONL (update existing live node records) ──────────────
    out_lines: list[str] = []
    live_changed = 0
    for line in LIVE_KG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec  = json.loads(line)
            nid  = rec.get("data", {}).get("id")
            if rec.get("record_type") == "node" and nid in in_live:
                rec["data"]["domain"] = corrections[nid]
                line = json.dumps(rec)
                live_changed += 1
        except json.JSONDecodeError:
            pass
        out_lines.append(line)

    # ── Append seed-only override records ───────────────────────────────────
    # KGStore's _load_live does: self._nodes[data["id"]] = data
    # So appending a full node record here overrides the seed entry.
    seed_appended = 0
    for nid in seed_only:
        node = dict(seed_nodes[nid])   # copy seed node data
        node["domain"] = corrections[nid]
        record = {"record_type": "node", "data": node}
        out_lines.append(json.dumps(record))
        seed_appended += 1

    LIVE_KG.write_text("\n".join(out_lines) + "\n")
    log.info("  Wrote %d updated live records + %d seed override records.",
             live_changed, seed_appended)

    # ── Log correction events ────────────────────────────────────────────────
    ts = time.time()
    with SENSORS.open("a") as f:
        for ev in log_events:
            f.write(json.dumps({
                "event":     "domain_reclassification",
                "timestamp": ts,
                "source":    ev["method"],
                **{k: v for k, v in ev.items() if k != "method"},
            }) + "\n")

    log.info("Done. %d nodes reclassified.", len(corrections))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Assign domains to unknown-domain KG nodes.")
    parser.add_argument("--source-domain", default=None,
                        help="If set, only reclassify nodes with this domain (e.g. 'python'). "
                             "Default: reclassify all domain=unknown nodes.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing anything.")
    args = parser.parse_args()
    run(source_domain=args.source_domain, dry_run=args.dry_run)
