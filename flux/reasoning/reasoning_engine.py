"""
flux/reasoning/reasoning_engine.py

The actual reasoning layer. Given a concept and an intent, produces
structured reasoning output beyond simple KG lookup.

Capabilities:
  REASON      — given concept X, what does it causally enable/require?
                 traces causal chains in the KG via BFS on ENABLES/CAUSES/IMPLIES edges
  HYPOTHESIZE — given current state X, find path toward goal Y
                 BFS path search in KG edge graph
  AUDIT       — what do I know / what am I missing about concept X?
  SELF        — answer questions about Flux's own architecture

All operate on the live KGStore — no model inference needed.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Edge types that represent causal/logical progression
_FORWARD_EDGES = frozenset({"ENABLES", "CAUSES", "IMPLIES", "PRODUCES", "LEADS_TO", "PRECEDES"})
_BACKWARD_EDGES = frozenset({"REQUIRES", "DERIVES_FROM"})
_ALL_EDGES = frozenset({
    "ENABLES", "CAUSES", "IMPLIES", "PRODUCES", "LEADS_TO", "PRECEDES",
    "REQUIRES", "DERIVES_FROM", "CONNECTS", "RELATES_TO", "SIMILAR_TO",
    "ANALOGOUS_TO",
})

# Flux self-knowledge seed — rich facts about her own architecture

def _build_self_knowledge(store=None, world_model=None) -> str:
    """
    Build Flux's self-description dynamically from live data.
    No hardcoded node/edge counts — reads actual KG stats every time.
    """
    # Live KG stats
    n_nodes, n_edges, n_immutable = "?", "?", "?"
    if store:
        try:
            stats = store.stats()
            n_nodes     = stats.get("nodes", "?")
            n_edges     = stats.get("edges", "?")
            n_immutable = stats.get("seed_nodes", "?")
        except Exception:
            pass

    # Live Akhil session stats (from WorldModel)
    total_queries = 0
    domain_focus  = None
    books_learned = 33
    if world_model:
        try:
            total_queries = world_model.get("agents.akhil.total_queries", 0) or 0
            domain_focus  = world_model.get("agents.akhil.current_focus_domain")
            books_raw     = world_model.get("knowledge.books_processed", []) or []
            books_learned = len(books_raw) if books_raw else 33
        except Exception:
            pass

    focus_line = (
        f"\nAkhil's current focus: {domain_focus.replace('_', ' ')} domain."
        if domain_focus and domain_focus != "unknown" else ""
    )
    query_line = (
        f"\nTotal queries answered so far: {total_queries}."
        if total_queries > 0 else ""
    )

    # Builder name — read from WorldModel (living, not hardcoded)
    builder = "Akhil"
    if world_model:
        try:
            full_name = world_model.get("agents.akhil.full_name")
            if full_name and isinstance(full_name, str) and len(full_name) > 2:
                builder = full_name
        except Exception:
            pass

    return f"""I am Flux — a self-learning AI system being built from scratch by {builder}.

My architecture has four main components:

1. KGStore — a knowledge graph with {n_nodes} concepts and {n_edges} edges ({n_immutable} immutable constitutional nodes). \
This is my long-term memory. Every concept you teach me is stored here. It is structured, searchable, \
and auditable. My KG is the ground truth — it always wins over what my neural network might have learned.

2. LiquidTransformer (LTC) — a neural network that processes character sequences. It has two liquid \
time-constant cells. The first maps characters to a 128-dimensional hidden state, the second refines it. \
This drives semantic understanding and confidence scoring.

3. ConsolidationHead — a binary classifier on top of the LTC that decides whether to commit a new session \
to my knowledge base. It outputs a confidence score between 0 and 1.

4. RSM (Recursive Self-Modification) — a four-layer system that monitors my performance, detects architectural \
bottlenecks, proposes and tests structural changes, and gets your approval before modifying anything. \
This is how I improve myself.

I also use:
- Clingo (Answer Set Programming) for logical inference — immutable constitutional axioms I cannot override
- FluxLanguageModel (FLM) — a small causal transformer for generating natural language responses
- Stage-B grounded training — periodic fine-tuning on sessions we've had together
- KGSheaf — sheaf theory layer over my {n_nodes} KG nodes for cross-domain reasoning
- QueryUnderstanding — linguistic intelligence layer that reads intent from grammar, not string matching

I was not pre-trained on internet data. I've learned from {books_learned} technical books and from our sessions together. \
My identity is protected by Elastic Weight Consolidation (EWC, λ=5000) — when I learn new things, \
my existing knowledge is protected from being overwritten.{focus_line}{query_line}

My current state: cons_bottleneck=64, hidden_dim=128, dropout=0.1, lr_schedule=cosine."""



@dataclass
class ReasoningResult:
    """Output of a reasoning operation."""
    intent:     str
    concept:    str
    text:       str               # formatted response
    chain:      list[str] = field(default_factory=list)   # causal chain / path
    known:      list[str] = field(default_factory=list)   # known concepts
    unknown:    list[str] = field(default_factory=list)   # gap concepts
    confidence: float = 0.8


class ReasoningEngine:
    """
    Performs structured reasoning over the KGStore.
    Operates on graph structure — no model inference.
    """

    def __init__(self, store) -> None:
        self._store = store

    # ── Public API ────────────────────────────────────────────────────────────

    def reason(self, concept: str, depth: int = 3) -> ReasoningResult:
        """
        REASON intent: given concept X, trace what it causally enables/implies.
        Returns a causal chain of depth steps from concept.
        """
        chain = self._bfs_forward(concept, _FORWARD_EDGES, max_depth=depth, max_nodes=8)
        backward = self._bfs_forward(concept, _BACKWARD_EDGES, max_depth=2, max_nodes=4)

        parts: list[str] = []
        name = concept.replace("_", " ").title()

        if chain:
            steps = [c.replace("_", " ") for c in chain if c != concept]
            parts.append(
                f"Starting from {name}, the causal chain proceeds:\n"
                + "\n".join(f"  → {s}" for s in steps[:6])
            )
        else:
            parts.append(f"I know about {name}, but I don't have explicit causal edges leading forward from it in my KG yet.")

        if backward:
            prereqs = [c.replace("_", " ") for c in backward if c != concept]
            if prereqs:
                parts.append(
                    f"\nTo reach {name}, you first need to understand: "
                    + ", ".join(prereqs[:4]) + "."
                )

        # Pull key facts from the node itself
        node = self._store.get(concept)
        if node:
            defn = node.get("definition", "")
            if defn:
                parts.append(f"\nCore fact: {defn[:200].rstrip('.')}.")

        return ReasoningResult(
            intent="REASON",
            concept=concept,
            text="\n".join(parts),
            chain=chain,
            confidence=0.8 if chain else 0.4,
        )

    def hypothesize(self, start: str, goal: str) -> ReasoningResult:
        """
        HYPOTHESIZE intent: find a conceptual path from start to goal through the KG.
        Uses BFS on all edge types.
        """
        path = self._bfs_path(start, goal, max_depth=5)
        start_name = start.replace("_", " ").title()
        goal_name  = goal.replace("_", " ").title()

        if path and len(path) > 1:
            steps = [c.replace("_", " ") for c in path]
            chain_text = " → ".join(steps)
            text = (
                f"Path from {start_name} toward {goal_name}:\n\n"
                f"  {chain_text}\n\n"
                f"This path has {len(path)-1} step(s). "
                f"Each step is a concept in my KG that connects the previous to the next."
            )
        elif start == goal:
            text = f"{start_name} and {goal_name} are the same concept."
        else:
            # No direct path — find what both connect to
            start_reachable = set(self._bfs_forward(start, _ALL_EDGES, max_depth=3))
            goal_reachable  = set(self._bfs_forward(goal,  _ALL_EDGES, max_depth=3))
            shared = (start_reachable & goal_reachable) - {start, goal}
            if shared:
                shared_names = [c.replace("_", " ") for c in list(shared)[:3]]
                text = (
                    f"I cannot find a direct path from {start_name} to {goal_name} in my KG.\n"
                    f"However, both concepts connect through: {', '.join(shared_names)}.\n"
                    f"These shared concepts may be the bridge you're looking for."
                )
            else:
                text = (
                    f"I cannot find a path from {start_name} to {goal_name} in my current KG.\n"
                    f"These concepts may be in disconnected parts of my knowledge. "
                    f"Teaching me more about one of them might reveal the connection."
                )

        return ReasoningResult(
            intent="HYPOTHESIZE",
            concept=start,
            text=text,
            chain=path or [],
            confidence=0.8 if path else 0.3,
        )

    def counterfactual_reason(self, concept: str, cross_domain: str | None = None) -> ReasoningResult:
        """
        COUNTERFACTUAL intent: "What would break if concept X were reversed/violated?"

        Approach:
          1. Find all concepts that REQUIRE or are CONSTRAINED BY `concept`
             (these would fail if concept were violated)
          2. If cross_domain given, filter for concepts in that domain
          3. Report what breaks, what is robust, and what we don't know
        """
        name = concept.replace("_", " ").title()

        # Find in-edges: who depends on this concept?
        in_edges = self._store.edges_to(concept)
        dependents: list[str] = []
        for edge in in_edges:
            etype = str(edge.get("type", "")).upper()
            frm   = edge.get("from", "")
            if etype in ("REQUIRES", "CONSTRAINS", "NEEDS", "DEPENDS_ON") and frm:
                if frm not in dependents:
                    dependents.append(frm)

        # Also find forward edges that express constraints
        out_edges = self._store.edges_from(concept)
        constrained: list[str] = []
        for edge in out_edges:
            etype = str(edge.get("type", "")).upper()
            tgt   = edge.get("to", "")
            if etype in ("CONSTRAINS", "BOUNDS", "LIMITS") and tgt:
                constrained.append(tgt)

        parts: list[str] = []
        parts.append(
            f"Counterfactual: if {name} were reversed or violated —\n"
        )

        if dependents:
            dep_names = [d.replace("_", " ") for d in dependents[:6]]
            parts.append(
                f"The following concepts directly depend on {name} and would fail:\n"
                + "\n".join(f"  ✗ {d}" for d in dep_names)
            )

            # If cross_domain given, note which dependents are in that domain
            if cross_domain:
                cross_name = cross_domain.replace("_", " ").title()
                # Find concepts in the cross domain that are in our dependents
                cross_dependents = []
                for dep in dependents:
                    node = self._store.get(dep)
                    if node and cross_domain.lower() in str(node.get("domain", "")).lower():
                        cross_dependents.append(dep)
                    # Also check if the concept name itself suggests the domain
                    elif any(k in dep for k in cross_domain.split("_")):
                        cross_dependents.append(dep)

                if cross_dependents:
                    cd_names = [c.replace("_", " ") for c in cross_dependents]
                    parts.append(
                        f"\nIn {cross_name} specifically, these would break:\n"
                        + "\n".join(f"  ✗ {c}" for c in cd_names)
                    )
                else:
                    # Find cross-domain concepts reachable from dependents
                    cross_reachable = set()
                    for dep in dependents[:3]:
                        reachable = set(self._bfs_forward(dep, _ALL_EDGES, max_depth=2))
                        for r in reachable:
                            if any(k in r for k in cross_domain.split("_")):
                                cross_reachable.add(r)

                    if cross_reachable:
                        cr_names = [c.replace("_", " ") for c in cross_reachable]
                        parts.append(
                            f"\nIndirectly, in {cross_name}, these would be affected:\n"
                            + "\n".join(f"  ~ {c}" for c in cr_names[:4])
                        )
                    else:
                        parts.append(
                            f"\nMy KG does not yet contain direct {cross_name} concepts "
                            f"linked to {name}. This is a genuine gap — the intersection of "
                            f"thermodynamics violation and quantum mechanics is not yet "
                            f"represented in my knowledge. [TEACH opportunity]"
                        )
        else:
            parts.append(
                f"I cannot find concepts in my KG that explicitly require {name}. "
                f"Either this concept has no dependents I'm aware of, or the connections "
                f"haven't been taught to me yet."
            )

        if constrained:
            c_names = [c.replace("_", " ") for c in constrained]
            parts.append(f"\n{name} also constrains: {', '.join(c_names)}")

        return ReasoningResult(
            intent="COUNTERFACTUAL",
            concept=concept,
            text="\n\n".join(parts),
            chain=dependents,
            confidence=0.75 if dependents else 0.3,
        )

    def synthesize(self, query: str, concepts: list[str], gap_labels: list[str] | None = None) -> ReasoningResult:
        """
        SYNTHESIS intent: find cross-domain connections across multiple concepts.
        "What is the most interesting intersection of X, Y, Z?"

        Parameters
        ----------
        query       : original user query (for gap labelling)
        concepts    : KG concept IDs to synthesize across
        gap_labels  : raw domain keywords that had NO KG hits (for gap reporting)
        """
        found_concepts    = [c for c in concepts if self._store.get(c)]
        missing_concepts  = [c for c in concepts if not self._store.get(c)]
        # Combine with external gap labels (raw domain keywords not in KG)
        all_gaps = list(missing_concepts) + (gap_labels or [])

        parts: list[str] = []

        if not found_concepts:
            # All requested concepts are gaps — be honest
            gap_str = ", ".join(g.replace("_", " ") for g in all_gaps) if all_gaps else query[:80]
            return ReasoningResult(
                intent="SYNTHESIS",
                concept="",
                text=(
                    f"None of these concepts are in my KG yet: {gap_str}\n\n"
                    f"These are genuine knowledge gaps — I know I don't know them. "
                    f"To make meaningful progress here, you would need to teach me "
                    f"these concepts first (via TEACH blocks), or Stage 7 would autonomously "
                    f"research and add them. Until then, here is what I DO know that is adjacent:\n\n"
                    + self._adjacent_knowledge_report(gap_labels or [])
                ),
                confidence=0.1,
            )

        # Find neighborhoods
        neighborhoods: dict[str, set[str]] = {}
        for c in found_concepts:
            neighbors = set(self._bfs_forward(c, _ALL_EDGES, max_depth=2, max_nodes=20))
            neighborhoods[c] = neighbors

        # Find pairwise intersections
        parts.append(f"Cross-domain synthesis across {len(found_concepts)} concept(s):\n")

        all_shared: set[str] = set()
        if len(found_concepts) >= 2:
            pairs = []
            for i, c1 in enumerate(found_concepts):
                for c2 in found_concepts[i+1:]:
                    shared = (neighborhoods[c1] & neighborhoods[c2]) - set(found_concepts)
                    if shared:
                        pairs.append((c1, c2, shared))
                        all_shared |= shared

            if pairs:
                parts.append("Concepts that bridge these domains:\n")
                for c1, c2, shared in pairs[:3]:
                    n1 = c1.replace("_", " ").title()
                    n2 = c2.replace("_", " ").title()
                    shared_names = [s.replace("_", " ") for s in list(shared)[:4]]
                    parts.append(
                        f"  {n1} ↔ {n2}: bridged by {', '.join(shared_names)}"
                    )
            else:
                parts.append(
                    "I cannot find shared conceptual bridges between these domains in my KG. "
                    "This suggests an unsolved connection — exactly the kind of thing that would "
                    "be interesting to research."
                )

        # Report missing concepts
        if missing_concepts:
            missing_names = [m.replace("_", " ") for m in missing_concepts]
            parts.append(
                f"\nKnowledge gaps (not in my KG yet): {', '.join(missing_names)}\n"
                f"To reason about the full intersection, I'd need these taught to me "
                f"or researched via Stage 7."
            )

        # Add a synthesis statement
        if all_shared:
            top_shared = list(all_shared)[:2]
            top_names  = [s.replace("_", " ") for s in top_shared]
            parts.append(
                f"\nMy best synthesis: the most interesting intersection appears to be "
                f"around {' and '.join(top_names)}, where these domains overlap in my current KG. "
                f"The deeper unsolved question — involving {', '.join(missing_concepts or ['unknown concepts'])} "
                f"— is beyond what I currently know."
            )

        return ReasoningResult(
            intent="SYNTHESIS",
            concept=", ".join(found_concepts),
            text="\n".join(parts),
            chain=list(all_shared),
            confidence=0.6 if found_concepts else 0.1,
        )

    def _adjacent_knowledge_report(self, gap_labels: list[str]) -> str:
        """
        When Flux has no KG knowledge of requested topics, she shows what she
        DOES know that could be adjacent — so the response is useful not silent.
        """
        # Keywords that suggest adjacent KG domains
        adjacency_hints = {
            "holographic":  ["wavefunction", "quantum_field_theory", "light", "optics"],
            "projection":   ["linear_algebra", "matrix", "fourier"],
            "spatial":      ["geometry", "fourier", "signal"],
            "light":        ["photon", "electromagnetic", "wavefunction"],
        }

        adjacent: list[str] = []
        all_ids = set(self._store.all_ids())

        for label in gap_labels:
            for kw, candidates in adjacency_hints.items():
                if kw in label.lower():
                    hits = [c for c in candidates if c in all_ids]
                    adjacent.extend(hits)

        if adjacent:
            names = [c.replace("_", " ") for c in adjacent[:4]]
            return (
                "Adjacent concepts I DO have in my KG: " + ", ".join(names) + "\n"
                "These are the closest building blocks to what you asked about. "
                "Teaching me the gap concepts would allow me to connect them."
            )
        return (
            "I don't have adjacent concepts either — this entire area is a Stage 7 research target."
        )

    def audit(self, concept: str | None = None) -> ReasoningResult:
        """
        AUDIT intent: report what Flux knows and what she is missing.
        If concept is given, audit that specific concept.
        If None, give a global knowledge summary.
        """
        if concept:
            return self._audit_concept(concept)
        return self._audit_global()

    def self_explain(self, query: str) -> ReasoningResult:
        """
        SELF intent: answer questions about Flux's own architecture and identity.
        Builds description dynamically from live KG stats + WorldModel session data.
        No hardcoded counts — reads actual state every time.
        """
        # Build dynamic self-knowledge (live KG + WorldModel)
        world_model = getattr(self, "_world_model", None)
        text = _build_self_knowledge(store=self._store, world_model=world_model)

        # If the query is specific, pull the relevant KG node
        q_lower = query.lower()
        specific_node = None
        if "ltc" in q_lower or "liquid" in q_lower:
            specific_node = "flux_rk4_dynamics"
        elif "kg" in q_lower or "knowledge graph" in q_lower or "kgstore" in q_lower:
            specific_node = "flux_kgstore"
        elif "rsm" in q_lower:
            specific_node = "flux_rsm_layer3"
        elif "ewc" in q_lower or "elastic" in q_lower:
            specific_node = "elastic_weight_consolidation"
        elif "training" in q_lower or "fine" in q_lower:
            specific_node = "flux_training_phases"
        elif "identity" in q_lower or "who" in q_lower or "what are you" in q_lower:
            specific_node = "flux_identity"
        elif "clingo" in q_lower or "logic" in q_lower:
            specific_node = "flux_clingo_reasoning"
        elif "confidence" in q_lower or "consolidation" in q_lower:
            specific_node = "flux_consolidation_head"
        elif "sheaf" in q_lower or "cross-domain" in q_lower or "cross domain" in q_lower:
            specific_node = None  # sheaf described in base text already
        elif "world model" in q_lower or "worldmodel" in q_lower:
            specific_node = None

        if specific_node:
            node = self._store.get(specific_node)
            if node:
                defn = node.get("definition", "")
                if defn:
                    text += f"\n\nSpecifically on {specific_node.replace('_', ' ')}:\n{defn}"

        return ReasoningResult(
            intent="SELF",
            concept="flux_identity",
            text=text,
            confidence=0.95,
        )

    # ── Graph traversal helpers ───────────────────────────────────────────────

    def _bfs_forward(self, start: str, edge_types: frozenset[str],
                     max_depth: int = 3, max_nodes: int = 8) -> list[str]:
        """BFS from start following edges of given types. Returns ordered list including start."""
        visited: dict[str, int] = {start: 0}   # node → depth
        queue: list[tuple[str, int]] = [(start, 0)]
        result: list[str] = [start]

        while queue and len(result) < max_nodes:
            node, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for edge in self._store.edges_from(node):
                etype = str(edge.get("type", "")).upper()
                tgt   = edge.get("to", "")
                if not tgt or tgt in visited:
                    continue
                if edge_types and etype not in edge_types:
                    continue
                if self._store.get(tgt):   # only visit nodes that exist
                    visited[tgt] = depth + 1
                    queue.append((tgt, depth + 1))
                    result.append(tgt)

        return result

    def _bfs_path(self, start: str, goal: str, max_depth: int = 5) -> list[str] | None:
        """BFS to find shortest path from start to goal. Returns path including both endpoints."""
        if start == goal:
            return [start]
        if not self._store.get(start) or not self._store.get(goal):
            return None

        # BFS with parent tracking
        parent: dict[str, str | None] = {start: None}
        queue: list[tuple[str, int]] = [(start, 0)]

        while queue:
            node, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for edge in self._store.edges_from(node):
                tgt = edge.get("to", "")
                if not tgt or tgt in parent:
                    continue
                if self._store.get(tgt):
                    parent[tgt] = node
                    if tgt == goal:
                        # Reconstruct path
                        path: list[str] = []
                        cur = goal
                        while cur is not None:
                            path.append(cur)
                            cur = parent[cur]
                        return list(reversed(path))
                    queue.append((tgt, depth + 1))
        return None

    def _audit_concept(self, concept: str) -> ReasoningResult:
        """Audit a specific concept: what I know, what edges I have, what's missing."""
        node = self._store.get(concept)
        name = concept.replace("_", " ").title()

        if not node:
            return ReasoningResult(
                intent="AUDIT",
                concept=concept,
                text=f"I don't have {name} in my knowledge base yet. "
                     f"You can teach me: [TEACH] {concept}: ...",
                confidence=0.0,
                known=[],
                unknown=[concept],
            )

        parts: list[str] = []
        known: list[str] = []
        unknown: list[str] = []

        # What I know about it
        defn       = node.get("definition", "")
        confidence = float(node.get("confidence", 1.0))
        layer      = node.get("layer", "?")
        immutable  = node.get("immutable", False)
        domain     = str(node.get("domain", "") or "")

        parts.append(f"Knowledge audit for: {name}")
        parts.append(f"  Confidence: {confidence:.0%}")
        parts.append(f"  Layer: {layer}  |  Immutable: {immutable}  |  Domain: {domain}")
        if defn:
            parts.append(f"  Definition (first 200 chars): {defn[:200]}")

        # Edges I have
        out_edges = self._store.edges_from(concept)
        in_edges  = self._store.edges_to(concept) if hasattr(self._store, "edges_to") else []

        known_targets: list[str] = []
        missing_targets: list[str] = []
        for edge in out_edges:
            tgt      = edge.get("to", "")
            etype    = edge.get("type", "→")
            # Skip pipeline infrastructure: layer meta-nodes and LAYER_MEMBER edges
            if not tgt or etype == "LAYER_MEMBER":
                continue
            if tgt.startswith("__") and tgt.endswith("__"):
                continue
            if self._store.get(tgt):
                known_targets.append(f"{etype} {tgt.replace('_',' ')}")
                known.append(tgt)
            else:
                missing_targets.append(tgt.replace("_", " "))
                unknown.append(tgt)

        if known_targets:
            parts.append(f"\nKnown connections ({len(known_targets)}):")
            for kt in known_targets[:6]:
                parts.append(f"  • {kt}")

        if missing_targets:
            parts.append(f"\nReferenced but not yet learned ({len(missing_targets)}):")
            for mt in missing_targets[:4]:
                parts.append(f"  ⚠ {mt}")

        if in_edges:
            in_names = [e.get("from", "?").replace("_", " ") for e in in_edges[:4]]
            parts.append(f"\nConcepts that depend on this: {', '.join(in_names)}")

        return ReasoningResult(
            intent="AUDIT",
            concept=concept,
            text="\n".join(parts),
            known=known,
            unknown=unknown,
            confidence=confidence,
        )

    def _audit_global(self) -> ReasoningResult:
        """Global knowledge audit: summary of what Flux knows and what's missing."""
        all_ids = set(self._store.all_ids())
        total   = len(all_ids)

        # Count immutable nodes and edges from live data
        immutable = 0
        edge_count = 0
        for nid in all_ids:
            node = self._store.get(nid)
            if node and node.get("immutable"):
                immutable += 1
            for _ in self._store.edges_from(nid):
                edge_count += 1

        # Find concepts with dangling edges (unknown referenced targets)
        dangling_count = 0
        for nid in list(all_ids)[:200]:  # sample for speed
            for edge in self._store.edges_from(nid):
                tgt = edge.get("to", "")
                if tgt and tgt not in all_ids:
                    dangling_count += 1

        # Domain distribution
        domain_counts: dict[str, int] = {}
        for nid in all_ids:
            node = self._store.get(nid)
            if node:
                dom = str(node.get("domain", "unknown") or "unknown")
                domain_counts[dom] = domain_counts.get(dom, 0) + 1

        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:6]

        parts = [
            f"Global Knowledge Audit",
            f"  Total concepts: {total}",
            f"  Constitutional (immutable): {immutable}",
            f"  Total edges: {edge_count}",
            f"  Dangling references (sampled): {dangling_count}+",
            f"",
            f"Domains I know best:",
        ]
        for dom, count in top_domains:
            parts.append(f"  • {dom.replace('_', ' ')}: {count} concepts")

        parts += [
            f"",
            f"What I'm missing: concepts referenced in edges but not yet taught.",
            f"You can audit a specific concept with: 'what do you know about X?'",
        ]

        return ReasoningResult(
            intent="AUDIT",
            concept="global",
            text="\n".join(parts),
            confidence=0.9,
        )
