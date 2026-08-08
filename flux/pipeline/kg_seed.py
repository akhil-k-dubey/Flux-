"""
flux/pipeline/kg_seed.py

Converts FLUX_PRIMITIVES.md + flux_axioms.lp -> flux_kg_seed.json.
Two-pass: Pass 1 builds node registry, Pass 2 resolves edges.
Adds: Layer 0 definitional edges, layer meta-nodes for topo sort ordering.
Outputs: nodes, edges, SPEC nodes, AXIOM nodes, topo order, gap report.
"""

from __future__ import annotations
import json, re, hashlib
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque
from flux.pipeline import schema as S

PRIMITIVES = Path("FLUX_PRIMITIVES.md")
AXIOMS     = Path("flux_axioms.lp")
OUTPUT     = Path("logs/flux_kg_seed.json")

_LAYER_RE = re.compile(r"##\s+(LAYER\s+\S+)", re.IGNORECASE)
_FIELD_RE = re.compile(r"^([\w][\w_]*):\s*(.+)$", re.MULTILINE)

# -- Layer 0 Definitional Edges ------------------------------------------------
# Mathematical definitions, not discovered relationships.
# NOT -> TRUE is a definition. EXISTS INVERSE_OF FORALL is a definition.
# Everything above Layer 0 is discovered by Flux through training.
# Kept here (not in FLUX_PRIMITIVES.md) to preserve the constitutional doc.
LAYER_0_EDGES: list[tuple[str, str, str]] = [
    # Truth duality - one canonical direction (true is the positive pole).
    # CONTRADICTS is symmetric by type; both directions aren't needed in a DAG.
    ("true",       "false",       "CONTRADICTS"),
    # Logical operators operate over TRUE / FALSE as their domain
    ("not",        "true",        "RELATES_TO"),
    ("not",        "false",       "RELATES_TO"),
    ("and",        "true",        "RELATES_TO"),
    ("and",        "false",       "RELATES_TO"),
    ("or",         "true",        "RELATES_TO"),
    ("or",         "false",       "RELATES_TO"),
    ("implies",    "true",        "RELATES_TO"),
    ("implies",    "false",       "RELATES_TO"),
    ("equivalent", "true",        "RELATES_TO"),
    ("equivalent", "false",       "RELATES_TO"),
    # De Morgan duals - one direction; INVERSE_OF encodes symmetry
    ("and",        "or",          "INVERSE_OF"),
    # Quantifier duals - one direction
    ("exists",     "forall",      "INVERSE_OF"),
    # Implication / equivalence - one direction
    ("implies",    "equivalent",  "RELATES_TO"),
]

# Layer curriculum priority — lower number = taught first
LAYER_PRIORITY = {
    "LAYER_0": 0, "LAYER_1": 1, "LAYER_2": 2,
    "LAYER_3": 3, "LAYER_4": 4, "LAYER_S": 5, "LAYER_UNKNOWN": 6,
}

# -- Pass 1 — Node Extraction --------------------------------------------------

def _extract_layer(header: str) -> str:
    for key, val in S.LAYER_MAP.items():
        if key in header.upper():
            return val
    return "LAYER_UNKNOWN"

def _parse_block(raw: str, layer: str) -> dict | None:
    """Parse one primitive block into a node dict with raw edge fields."""
    fields = dict(_FIELD_RE.findall(raw))
    if "name" not in fields:
        return None
    ntype = S.validate_node_type(fields.get("type", "CONCEPT"))
    nid   = fields["name"].strip().lower().replace(" ", "_")
    return {
        "id": nid, "type": ntype, "layer": layer,
        "definition":  fields.get("definition", "").strip(),
        "confidence":  S.DEFAULT_CONFIDENCE,
        "immutable":   S.is_immutable(ntype, layer),
        "trainable":   True,
        "_relates_to": fields.get("relates_to", ""),
        "_requires":   fields.get("requires", ""),
        "_is_a":       fields.get("is_a", ""),
        "_clingo":     fields.get("clingo_map", ""),
        "_z3":         fields.get("z3_map", ""),
        "_lean4":      fields.get("lean4_map", ""),
    }

def pass1(text: str) -> tuple[dict[str, dict], list[str]]:
    """
    Extract all nodes from FLUX_PRIMITIVES.md.
    Splits on ``` markers: even chunks are outside code blocks (scan for layer
    headers), odd chunks starting with 'primitive' are primitive blocks.
    Returns (registry, parse_order) — parse_order used to classify forward refs.
    """
    registry, order = {}, []
    current_layer = "LAYER_UNKNOWN"
    for i, chunk in enumerate(text.split("```")):
        if i % 2 == 0:
            if m := _LAYER_RE.search(chunk):
                current_layer = _extract_layer(m.group(1))
        elif chunk.startswith("primitive\n"):
            node = _parse_block(chunk[len("primitive\n"):], current_layer)
            if node and node["id"] not in registry:
                registry[node["id"]] = node
                order.append(node["id"])
    return registry, order

# -- Axiom Parser --------------------------------------------------------------

def _make_axiom(label: str, rules: list[str]) -> dict:
    spec = "\n".join(rules)
    nid  = "axiom_" + hashlib.md5(spec.encode()).hexdigest()[:8]
    return {
        "id": nid, "type": "AXIOM", "layer": "LAYER_S",
        "definition": label or "Constitutional Clingo constraint",
        "confidence": 1.0, "immutable": True, "trainable": True,
        "_clingo": spec, "_z3": "", "_lean4": "",
        "_relates_to": "", "_requires": "", "_is_a": "",
    }

def parse_axioms(text: str) -> list[dict]:
    """Group Clingo rules by comment headers into AXIOM nodes."""
    nodes, buf, label = [], [], ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("%"):
            if buf:
                nodes.append(_make_axiom(label, buf)); buf = []
            label = line.lstrip("% ").strip()
        elif line:
            buf.append(line)
    if buf:
        nodes.append(_make_axiom(label, buf))
    return nodes

# -- Pass 2 — Edge Resolution --------------------------------------------------

def _parse_list(raw: str) -> list[str]:
    """Pipe- or comma-separated concept IDs -> list of lowercase ids."""
    if not raw.strip():
        return []
    sep = "|" if "|" in raw else ","
    return [p.strip().lower() for p in raw.split(sep) if p.strip()]

def pass2(
    registry: dict[str, dict],
    parse_index: dict[str, int],
) -> tuple[list[dict], dict]:
    """
    Resolve relates_to / requires / is_a into typed edges.
    Classifies unresolvable targets as true_gaps (never defined anywhere).
    Classifies targets defined later in the file as resolved_forward_refs.
    """
    edges, true_gaps, resolved_refs = [], [], []
    all_ids = set(registry)
    for nid, node in registry.items():
        candidates = (
            S.parse_relates_to(node["_relates_to"])
            + [(t, "REQUIRES") for t in _parse_list(node["_requires"])]
            + ([(node["_is_a"].strip().lower(), "IS_A")] if node["_is_a"].strip() else [])
        )
        for target, etype in candidates:
            if target in all_ids:
                edges.append({"from": nid, "to": target, "type": etype})
                if parse_index.get(target, 0) > parse_index.get(nid, 0):
                    resolved_refs.append(f"{nid} -> {target}")
            else:
                true_gaps.append({"from": nid, "to": target, "type": etype})
    return edges, {"resolved_forward_refs": resolved_refs, "true_gaps": true_gaps}

# -- SPEC Nodes ----------------------------------------------------------------

def create_spec_nodes(registry: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """
    For each node with clingo_map / z3_map / lean4_map:
    create a typed SPEC node and a FORMALIZES edge pointing to the concept.
    """
    nodes, edges = [], []
    for nid, node in registry.items():
        for lang, field in [("CLINGO", "_clingo"), ("Z3", "_z3"), ("LEAN4", "_lean4")]:
            if spec := node.get(field, "").strip():
                sid = f"spec_{nid}_{lang.lower()}"
                nodes.append({
                    "id": sid, "type": "SPEC", "layer": node["layer"],
                    "definition": spec, "confidence": 1.0,
                    "immutable": node["immutable"], "trainable": True,
                    "language": lang, "spec_text": spec,
                })
                edges.append({"from": sid, "to": nid, "type": "FORMALIZES"})
    return nodes, edges

# -- Layer Meta-Nodes ----------------------------------------------------------

def create_layer_meta_nodes(registry: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """
    Create one META node per layer seen in the registry.
    META nodes are pipeline infrastructure — not real KG concepts.
    trainable: False ensures session_builder skips them entirely.
    They exist solely to give topo sort a structural ordering anchor:
    every concept node gets a LAYER_MEMBER edge to its layer meta-node,
    forcing Layer 0 before Layer 1 before Layer 2 etc. in topo order.
    """
    layers_seen = {n["layer"] for n in registry.values()}
    meta_nodes, layer_edges = [], []
    for layer in layers_seen:
        mid = f"__layer_{layer.lower()}__"
        meta_nodes.append({
            "id": mid, "type": "META", "layer": layer,
            "definition": f"Layer ordering anchor for {layer}",
            "confidence": 1.0, "immutable": True, "trainable": False,
        })
        for nid, node in registry.items():
            if node["layer"] == layer:
                layer_edges.append({"from": nid, "to": mid, "type": "LAYER_MEMBER"})
    return meta_nodes, layer_edges

# -- Cycle Detection & Topo Sort -----------------------------------------------

def detect_cycles(node_ids: set[str], edges: list[dict]) -> list[str]:
    """
    DFS cycle detection. Returns list of back-edge strings 'from->to'.
    These edges are removed before topo sort and logged in the gap report.
    Breaking the back edge is the minimum intervention — history preserved,
    cycle flagged for Akhil review.
    """
    graph = defaultdict(list)
    for e in edges:
        graph[e["from"]].append(e["to"])
    visited, rec_stack, back_edges = set(), set(), []
    def dfs(v: str) -> None:
        visited.add(v); rec_stack.add(v)
        for nb in graph[v]:
            if nb not in visited:   dfs(nb)
            elif nb in rec_stack:   back_edges.append(f"{v}->{nb}")
        rec_stack.discard(v)
    for n in node_ids:
        if n not in visited: dfs(n)
    return back_edges

def topo_sort(node_ids: set[str], edges: list[dict]) -> list[str]:
    """Kahn's algorithm. Assumes cycles have been removed."""
    graph, in_deg = defaultdict(list), defaultdict(int)
    for e in edges:
        graph[e["from"]].append(e["to"]); in_deg[e["to"]] += 1
    for n in node_ids:
        in_deg.setdefault(n, 0)
    q = deque(n for n in node_ids if in_deg[n] == 0)
    order = []
    while q:
        n = q.popleft(); order.append(n)
        for nb in graph[n]:
            in_deg[nb] -= 1
            if in_deg[nb] == 0: q.append(nb)
    return order

# -- Build ---------------------------------------------------------------------

def build() -> dict:
    prim_text  = PRIMITIVES.read_text(encoding="utf-8")
    axiom_text = AXIOMS.read_text(encoding="utf-8") if AXIOMS.exists() else ""

    # Pass 1 — primitives
    registry, parse_order = pass1(prim_text)
    parse_index = {nid: i for i, nid in enumerate(parse_order)}

    # Merge axiom nodes
    for an in parse_axioms(axiom_text):
        registry[an["id"]] = an
        parse_index[an["id"]] = len(parse_index)

    # Pass 2 — explicit edges from source fields
    edges, gaps = pass2(registry, parse_index)

    # Layer 0 definitional edges (mathematical definitions, not discoveries)
    l0_edges_added = 0
    for src, tgt, etype in LAYER_0_EDGES:
        if src in registry and tgt in registry:
            edges.append({"from": src, "to": tgt, "type": etype})
            l0_edges_added += 1

    # Layer meta-nodes (topo sort anchors, trainable: False)
    meta_nodes, layer_edges = create_layer_meta_nodes(registry)
    for mn in meta_nodes:
        registry[mn["id"]] = mn
    edges.extend(layer_edges)

    # SPEC nodes (clingo / z3 / lean4 maps)
    spec_nodes, spec_edges = create_spec_nodes(registry)

    # Cycle detection — remove back edges, log them
    back_edges = detect_cycles(set(registry), edges)
    if back_edges:
        broken = set(back_edges)
        edges  = [e for e in edges if f"{e['from']}->{e['to']}" not in broken]
    gaps["cycles"] = back_edges

    # Topo sort -> then post-sort by layer priority to enforce curriculum order.
    # Kahn's alone can't guarantee Layer 0 before Layer 1 when LAYER_MEMBER
    # edges run concept->meta (meta ends up last in Kahn's, not first).
    # Post-sort: stable sort by (layer_priority, kahn_rank) gives correct order.
    kahn_order = topo_sort(set(registry), edges)
    kahn_rank  = {nid: i for i, nid in enumerate(kahn_order)}
    topo = sorted(
        kahn_order,
        key=lambda nid: (
            LAYER_PRIORITY.get(registry[nid]["layer"], 99),
            kahn_rank.get(nid, 0),
        ),
    )
    for rank, nid in enumerate(topo):
        if nid in registry:
            registry[nid]["complexity_rank"] = rank

    # Isolated nodes (exclude META nodes — they're anchors, not concepts)
    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    gaps["isolated_nodes"] = [
        n for n in registry
        if n not in connected and registry[n].get("type") != "META"
    ]

    # Edge training gap — explicit flag for meta_builder
    has_explicit_edges = any(
        n.get("type") not in ("META",) for e in edges
        for n in [registry.get(e["from"]), registry.get(e["to"])]
        if n
    )
    source_edge_count = len(edges) - layer_edges.__len__() - l0_edges_added
    gaps["edge_training_data"] = {
        "source_declared_edges": source_edge_count,
        "layer0_definitional_edges": l0_edges_added,
        "layer_member_edges": len(layer_edges),
        "isolated_concept_count": len(gaps["isolated_nodes"]),
        "reason": "No relates_to fields in FLUX_PRIMITIVES.md above Layer 0 — synthetic sessions produce empty edge lists",
        "impact": "Edge prediction capability requires real [TEACH] sessions with explicit relates_to fields",
        "recommendation": "Add relates_to fields to higher-layer primitives or generate [TEACH] sessions with typed edges",
    } if source_edge_count == 0 else {}

    # Strip private _ fields before serialisation
    clean_nodes = [
        {k: v for k, v in n.items() if not k.startswith("_")}
        for n in registry.values()
    ]

    return {
        "version":      S.VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "node_count":   len(clean_nodes),
        "edge_count":   len(edges),
        "spec_count":   len(spec_nodes),
        "nodes":        clean_nodes,
        "edges":        edges,
        "spec_nodes":   spec_nodes,
        "spec_edges":   spec_edges,
        "topo_order":   topo,
        "gaps":         gaps,
    }

if __name__ == "__main__":
    S.clear_all_gaps()
    OUTPUT.parent.mkdir(exist_ok=True)
    result = build()
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    g = result["gaps"]
    print(f"KG seed written -> {OUTPUT}")
    print(f"  nodes: {result['node_count']}  edges: {result['edge_count']}  specs: {result['spec_count']}")
    print(f"  true_gaps: {len(g['true_gaps'])}  resolved_refs: {len(g['resolved_forward_refs'])}  "
          f"cycles: {len(g['cycles'])}  isolated: {len(g['isolated_nodes'])}")
    etd = g.get("edge_training_data", {})
    if etd:
        print(f"  [WARNING] edge_training_data gap: {etd['reason']}")
        print(f"    -> layer0 edges: {etd['layer0_definitional_edges']}  "
              f"source edges: {etd['source_declared_edges']}")
    node_gaps = S.get_node_gaps()
    edge_gaps = S.get_edge_gaps()
    if node_gaps: print(f"  unknown node types: {node_gaps}")
    if edge_gaps: print(f"  unknown edge types: {edge_gaps}")
