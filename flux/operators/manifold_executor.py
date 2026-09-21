"""
flux/operators/manifold_executor.py

Manifold Executor — runs the manifold operation specified by a routed Operator.

This is the bridge between the Intent Router and the existing manifold/KG
infrastructure. It takes a RouterResult (operator + noun_slot) and:

  1. Grounds the noun_slot to a KG concept ID (using KGStore + KGIndex)
  2. Runs the operator's specified manifold operation
  3. Returns a structured OperatorResult that the Decoder (response_assembler)
     can format into language

The KG's role here:
  - Ground truth: KG confirms whether the concept EXISTS
  - Memory map: KG provides concept location (domain, type, edges)
  - NOT answer source: KG definitions are not returned directly

The manifold's role:
  - Compute what Akhil asked (path, coverage, perturbation, feasibility)
  - Generate the hypothesis / reasoning chain
  - Identify the boundary edge (nearest feasible endpoint when infeasible)

OperatorResult:
  operator_name    — which operator ran
  noun_slot        — what concept was asked about
  concept_id       — grounded KG concept ID (or None if not found)
  manifold_result  — raw manifold output (ManifoldResult or similar)
  feasible         — bool for CanYou operator
  nearest_feasible — concept_id of nearest feasible endpoint (if infeasible)
  boundary_edge    — description of what's missing for feasibility
  coverage_score   — 0.0-1.0 for Assess operator
  status           — "ok" | "no_concept" | "no_manifold" | "gap"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from flux.kg.store import KGStore
    from flux.manifold.causal_manifold import CausalManifold

from flux.operators.registry import Operator

log = logging.getLogger(__name__)


@dataclass
class OperatorResult:
    operator_name:    str
    noun_slot:        str
    concept_id:       Optional[str]        = None
    concept_id_b:     Optional[str]        = None   # for Connect (pair)
    manifold_result:  object               = None   # ManifoldResult | PerturbationResult | etc.
    feasible:         Optional[bool]       = None   # for CanYou
    nearest_feasible: Optional[str]        = None   # concept_id nearest to goal
    boundary_edge:    str                  = ""     # what's missing
    coverage_score:   float                = 0.0    # for Assess
    coverage_concepts: list[str]           = field(default_factory=list)
    status:           str                  = "ok"   # ok|no_concept|no_manifold|gap
    raw_text:         str                  = ""     # fallback text if manifold unavailable


class ManifoldExecutor:
    """
    Executes manifold operations for routed operators.

    Injected with KGStore and CausalManifold by FluxInterface at startup.
    Operates independently — can degrade gracefully when manifold is unavailable.
    """

    def __init__(self,
                 store:    "KGStore",
                 manifold: Optional["CausalManifold"] = None) -> None:
        self._store    = store
        self._manifold = manifold
        self._index    = None   # lazy-load KGIndex for concept grounding

    def execute(self,
                operator:    Operator,
                noun_slot:   str          = "",
                noun_slot_b: str          = "",
                direction:   float        = -1.0,
                target_vec:  object       = None,   # Optional[torch.Tensor] — from LanguageEncoder
                ) -> OperatorResult:
        """
        Run the manifold operation for the given operator.

        Two grounding modes:
          1. Vector mode (preferred): target_vec is a 257-dim Lorentz tensor from
             the LanguageEncoder. Grounding uses manifold.nearest_concept_to(target_vec).
             No string matching at all.
          2. String mode (teacher fallback): noun_slot is an English string.
             Grounding uses KGIndex TF-IDF → string match → KG lookup.

        noun_slot   — primary concept string (or "slotA|||slotB" for Connect)
        target_vec  — 257-dim manifold vector (overrides string grounding if provided)
        direction   — perturbation direction (-1.0=decrease, +1.0=increase)
        """
        # ── Split pair slot for Connect operator (string mode) ────────────────
        if "|||" in noun_slot:
            parts       = noun_slot.split("|||", 1)
            noun_slot   = parts[0].strip()
            noun_slot_b = noun_slot_b or parts[1].strip()

        # ── Ground concept: router already ran KG string match ───────────────────
        # target_vec (from LanguageEncoder target_head) is NOT used for grounding here —
        # the regression is unreliable. The router's _match_all_concepts_in_text()
        # already grounded noun_slot from the utterance text against the live KG.
        # We just confirm the concept exists in the KGStore.
        concept_id  = self._ground_concept(noun_slot)
        concept_id_b = self._ground_concept(noun_slot_b) if noun_slot_b else None

        if operator.name in ("Build", "CanYou", "Simulate", "Know", "Understand") \
                and not concept_id:
            teach_slot = noun_slot or "this concept"
            return OperatorResult(
                operator_name = operator.name,
                noun_slot     = noun_slot,
                status        = "no_concept",
                raw_text      = (
                    f"I don't have '{teach_slot}' in my knowledge graph yet. "
                    f"You can teach me: [TEACH] {teach_slot}: <definition>"
                ),
            )

        # ── Dispatch ──────────────────────────────────────────────────────────
        if operator.name == "Build":
            return self._run_build(operator, noun_slot, concept_id)

        if operator.name == "CanYou":
            return self._run_can_you(operator, noun_slot, concept_id)

        if operator.name == "Connect":
            return self._run_connect(operator, noun_slot, noun_slot_b,
                                     concept_id, concept_id_b)

        if operator.name == "Assess":
            return self._run_assess(operator, noun_slot, concept_id)

        if operator.name == "Simulate":
            return self._run_simulate(operator, noun_slot, concept_id, direction)

        if operator.name == "Know":
            return self._run_know(operator, noun_slot, concept_id)

        if operator.name == "Understand":
            return self._run_understand(operator, noun_slot, concept_id)

        if operator.name == "Explain":
            return self._run_explain(operator, noun_slot, concept_id)

        if operator.name == "Greet":
            return self._run_greet(operator)

        if operator.name == "Sync":
            return self._run_sync(operator, noun_slot)

        return OperatorResult(
            operator_name = operator.name,
            noun_slot     = noun_slot,
            status        = "ok",
            raw_text      = f"Operator '{operator.name}' not yet implemented.",
        )

    # ── Operator implementations ───────────────────────────────────────────────

    def _run_build(self, op: Operator, slot: str,
                   concept_id: Optional[str]) -> OperatorResult:
        """
        Build: find the causal construction path to the target concept.
        Manifold operation: GEODESIC from current_state to concept_id.
        """
        if self._manifold is None or concept_id is None:
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                concept_id    = concept_id,
                status        = "no_manifold",
                raw_text      = self._fallback_kg_text(concept_id, slot),
            )
        try:
            # Find the "current state" anchor — use the closest known concept
            # to a generic "current_state" concept, or default to the concept itself
            start_id = self._find_starting_concept(concept_id)
            result = self._manifold.geodesic_query(
                start_id, concept_id, store=self._store
            )
            return OperatorResult(
                operator_name   = op.name,
                noun_slot       = slot,
                concept_id      = concept_id,
                manifold_result = result,
                status          = "ok",
            )
        except Exception as exc:
            log.warning("Build manifold query failed: %s", exc)
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                concept_id    = concept_id, status = "no_manifold",
                raw_text      = self._fallback_kg_text(concept_id, slot),
            )

    def _run_can_you(self, op: Operator, slot: str,
                     concept_id: Optional[str]) -> OperatorResult:
        """
        CanYou: feasibility check — does a causal path to the concept exist?
        Does NOT execute. Returns feasible=True/False + nearest_feasible if not.
        """
        if self._manifold is None or concept_id is None:
            node = self._store.get(concept_id) if concept_id else None
            feasible = node is not None
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                concept_id    = concept_id,
                feasible      = feasible,
                status        = "ok",
                raw_text      = (
                    f"'{slot}' exists in my knowledge graph — I have information about it."
                    if feasible else
                    f"I don't have '{slot}' in my knowledge graph. "
                    f"It may be beyond my current knowledge boundary."
                ),
            )
        try:
            # Check if concept_id is in the manifold's built concept list
            concept_ids_in_manifold = getattr(self._manifold, "_concept_ids", [])
            feasible = concept_id in concept_ids_in_manifold

            nearest = None
            if not feasible and concept_ids_in_manifold:
                # Find nearest known concept using KG edge proximity
                edges = self._store.edges_from(concept_id) or []
                neighbors = [e.get("to", "") for e in edges if e.get("to")]
                nearest = next((n for n in neighbors if n in concept_ids_in_manifold), None)
                if not nearest and concept_ids_in_manifold:
                    nearest = concept_ids_in_manifold[0]

            return OperatorResult(
                operator_name    = op.name,
                noun_slot        = slot,
                concept_id       = concept_id,
                feasible         = feasible,
                nearest_feasible = nearest,
                boundary_edge    = "" if feasible else (
                    f"The concept '{slot}' is at or beyond the current knowledge boundary. "
                    f"Nearest known concept: {nearest or 'none'}."
                ),
                status = "ok",
            )
        except Exception as exc:
            log.warning("CanYou manifold query failed: %s", exc)
            node = self._store.get(concept_id) if concept_id else None
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                concept_id    = concept_id,
                feasible      = node is not None,
                status        = "ok",
            )


    def _run_connect(self, op: Operator, slot_a: str, slot_b: str,
                     cid_a: Optional[str], cid_b: Optional[str]) -> OperatorResult:
        """Connect: trace the geodesic path between two concepts."""
        if not cid_a or not cid_b:
            if not slot_a and not slot_b:
                missing_msg = "I need two concepts to connect — try: 'how does X relate to Y'"
            elif not cid_a:
                missing_msg = f"I don't have '{slot_a or 'the first concept'}' in my knowledge graph."
            else:
                missing_msg = f"I don't have '{slot_b or 'the second concept'}' in my knowledge graph."
            return OperatorResult(
                operator_name = op.name, noun_slot = slot_a,
                concept_id    = cid_a, concept_id_b = cid_b,
                status        = "no_concept",
                raw_text      = missing_msg,
            )
        if self._manifold is None:
            return OperatorResult(
                operator_name = op.name, noun_slot = slot_a,
                concept_id    = cid_a, concept_id_b = cid_b,
                status        = "no_manifold",
                raw_text      = self._fallback_connection_text(cid_a, cid_b),
            )
        try:
            result = self._manifold.geodesic_query(cid_a, cid_b, store=self._store)
            return OperatorResult(
                operator_name   = op.name,
                noun_slot       = slot_a,
                concept_id      = cid_a,
                concept_id_b    = cid_b,
                manifold_result = result,
                status          = "ok",
            )
        except Exception as exc:
            log.warning("Connect manifold query failed: %s", exc)
            return OperatorResult(
                operator_name = op.name, noun_slot = slot_a,
                concept_id    = cid_a, concept_id_b = cid_b,
                status        = "no_manifold",
                raw_text      = self._fallback_connection_text(cid_a, cid_b),
            )

    def _run_assess(self, op: Operator, slot: str,
                    concept_id: Optional[str]) -> OperatorResult:
        """
        Assess: measure how much Flux knows about this concept.
        Structural KG assessment — counts connected concepts, domain coverage.
        Does NOT return a definition. Returns a coverage score.
        """
        if not concept_id:
            # Try domain-level assessment
            domain_concepts = self._concepts_in_domain(slot)
            if domain_concepts:
                score = min(1.0, len(domain_concepts) / 50.0)  # 50 = "well covered"
                return OperatorResult(
                    operator_name    = op.name,
                    noun_slot        = slot,
                    coverage_score   = score,
                    coverage_concepts = domain_concepts[:10],
                    status           = "ok",
                )
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                status        = "no_concept",
                raw_text      = (
                    f"I don't have '{slot}' or any concepts I associate with it. "
                    f"This is a genuine gap in my knowledge."
                ),
            )

        # Count edges (connections) from this concept — density = knowledge depth
        edges      = self._store.edges_from(concept_id) or []
        neighbors  = [e.get("to", "") for e in edges if e.get("to")]
        node       = self._store.get(concept_id) or {}
        domain     = node.get("domain", "")
        domain_cxs = self._concepts_in_domain(domain) if domain else []

        # Coverage score: log-scaled edge count / domain size
        edge_count  = len(neighbors)
        domain_size = max(1, len(domain_cxs))
        score       = min(1.0, edge_count / 10.0) * min(1.0, domain_size / 20.0)

        return OperatorResult(
            operator_name     = op.name,
            noun_slot         = slot,
            concept_id        = concept_id,
            coverage_score    = score,
            coverage_concepts = neighbors[:10],
            status            = "ok",
        )

    def _run_simulate(self, op: Operator, slot: str,
                      concept_id: Optional[str], direction: float) -> OperatorResult:
        """Simulate: perturb concept and trace causal cascade."""
        if self._manifold is None or concept_id is None:
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                concept_id    = concept_id, status = "no_manifold",
                raw_text      = self._fallback_kg_text(concept_id, slot),
            )
        try:
            result = self._manifold.perturb_query(
                concept_id, direction=direction, depth=4
            )
            return OperatorResult(
                operator_name   = op.name,
                noun_slot       = slot,
                concept_id      = concept_id,
                manifold_result = result,
                status          = "ok",
            )
        except Exception as exc:
            log.warning("Simulate manifold query failed: %s", exc)
            return OperatorResult(
                operator_name = op.name, noun_slot = slot,
                concept_id    = concept_id, status = "no_manifold",
                raw_text      = self._fallback_kg_text(concept_id, slot),
            )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _ground_concept(self, slot: str) -> Optional[str]:
        """Ground a noun slot to a KG concept ID."""
        if not slot:
            return None

        # 1. Direct ID match
        direct = slot.lower().replace(" ", "_").replace("-", "_")
        if self._store.get(direct):
            return direct

        # 2. KGIndex TF-IDF lookup (lazy load)
        if self._index is None:
            try:
                from flux.language.kg_index import get_index
                self._index = get_index()
            except Exception:
                self._index = False  # mark as unavailable

        if self._index and self._index is not False:
            try:
                hits = self._index.query_one(slot, top_k=3)
                if hits and hits[0][1] >= 0.3:
                    return hits[0][0]
            except Exception:
                pass

        # 3. Substring match
        slot_words = set(slot.lower().split())
        for cid in self._store.all_ids():
            cid_words = set(cid.replace("_", " ").lower().split())
            if slot_words & cid_words:
                return cid

        return None

    def _find_starting_concept(self, target_id: str) -> str:
        """Find a reasonable 'starting point' concept for Build geodesic."""
        node   = self._store.get(target_id) or {}
        domain = node.get("domain", "")
        if domain:
            domain_concepts = self._concepts_in_domain(domain)
            if domain_concepts and domain_concepts[0] != target_id:
                return domain_concepts[0]
        return target_id  # self-loop → manifold will report it's already there

    def _concepts_in_domain(self, domain: str) -> list[str]:
        """Return all concept IDs in the given domain."""
        if not domain:
            return []
        return [
            cid for cid in self._store.all_ids()
            if (self._store.get(cid) or {}).get("domain") == domain
        ]

    def _fallback_kg_text(self, concept_id: Optional[str], slot: str) -> str:
        """KG-grounded fallback when manifold unavailable. No raw definition return."""
        if not concept_id:
            return f"I don't have '{slot}' in my knowledge graph."
        node   = self._store.get(concept_id) or {}
        domain = node.get("domain", "unknown")
        layer  = node.get("layer", "")
        edges  = self._store.edges_from(concept_id) or []
        return (
            f"'{slot}' exists in my KG (domain: {domain}, layer: {layer}). "
            f"It has {len(edges)} known connections. "
            f"Manifold not available for deeper reasoning."
        )

    def _fallback_connection_text(self, cid_a: str, cid_b: str) -> str:
        """KG edge-based connection fallback when manifold unavailable."""
        edges_a = {e.get("to"): e.get("relation") for e in (self._store.edges_from(cid_a) or [])}
        if cid_b in edges_a:
            rel = edges_a[cid_b]
            return f"Direct KG edge: {cid_a} → [{rel}] → {cid_b}"
        return (
            f"No direct KG edge between '{cid_a}' and '{cid_b}'. "
            f"Manifold not available for geodesic path."
        )

    # ── Phase 2 epistemic executors ───────────────────────────────────────────

    def _run_know(self, op: Operator, slot: str,
                  concept_id: Optional[str]) -> OperatorResult:
        """
        Know: binary familiarity check.
        Does Flux have this concept in its KG? Yes/no + nearest neighbour if no.
        """
        node  = self._store.get(concept_id) or {}
        label = node.get("label", slot.replace("_", " ").title())
        edges = self._store.edges_from(concept_id) or []

        # Find nearest concept if manifold is available (gives richer context)
        nearest_txt = ""
        if self._manifold and hasattr(self._manifold, "_concept_ids") \
                and concept_id in self._manifold._concept_ids:
            try:
                vec      = self._manifold.get_embedding(concept_id)
                nearest  = self._manifold.nearest_concepts_to(vec, k=2)
                others   = [n for n in nearest if n != concept_id][:1]
                if others:
                    nearest_txt = f"  Nearest concept: {others[0].replace('_', ' ')}."
            except Exception:
                pass

        lines = [
            f"Yes — I know '{label}'.",
            f"  Domain: {node.get('domain', 'unknown')} | "
            f"Connections: {len(edges)} | "
            f"Layer: {node.get('layer', 'unknown')}",
        ]
        if nearest_txt:
            lines.append(nearest_txt)

        return OperatorResult(
            operator_name = op.name,
            noun_slot     = slot,
            concept_id    = concept_id,
            status        = "ok",
            raw_text      = "\n".join(lines),
        )

    def _run_understand(self, op: Operator, slot: str,
                        concept_id: Optional[str]) -> OperatorResult:
        """
        Understand: comprehension quality measured from KG graph structure.
        - Edge count → connectivity
        - Distinct relation types → relational richness
        - Manifold neighbourhood density → geometric comprehension
        These are real structural measurements, not hardcoded coverage percentages.
        """
        node  = self._store.get(concept_id) or {}
        label = node.get("label", slot.replace("_", " ").title())
        edges = self._store.edges_from(concept_id) or []

        # Structural measurements
        edge_count = len(edges)
        rel_types  = set(e.get("relation", "") for e in edges)
        n_rel_types = len(rel_types)

        # Manifold neighbourhood density
        density_txt = ""
        if self._manifold and hasattr(self._manifold, "_concept_ids") \
                and concept_id in self._manifold._concept_ids:
            try:
                vec      = self._manifold.get_embedding(concept_id)
                nearest  = self._manifold.nearest_concepts_to(vec, k=6)
                others   = [n.replace("_", " ") for n in nearest if n != concept_id][:5]
                density_txt = f"  Manifold neighbourhood: {', '.join(others)}"
            except Exception:
                pass

        # Comprehension verdict based on structural richness
        if edge_count >= 8 and n_rel_types >= 3:
            verdict = "strong"
        elif edge_count >= 4:
            verdict = "partial"
        else:
            verdict = "surface-level"

        lines = [
            f"My understanding of '{label}' is {verdict}.",
            f"  KG edges: {edge_count} | Relation types: {n_rel_types} "
            f"({', '.join(list(rel_types)[:4])})",
        ]
        if density_txt:
            lines.append(density_txt)

        return OperatorResult(
            operator_name = op.name,
            noun_slot     = slot,
            concept_id    = concept_id,
            status        = "ok",
            raw_text      = "\n".join(lines),
        )

    def _run_explain(self, op: Operator, slot: str,
                     concept_id: Optional[str]) -> OperatorResult:
        """
        Explain: structured causal explanation drawn from KG + manifold.
        No raw definition return — builds explanation from graph structure:
          1. Label + domain context
          2. Upstream causes (incoming edges)
          3. Downstream effects (outgoing edges)
          4. Geodesic neighbourhood (manifold context)
        """
        label  = slot.replace("_", " ").title()
        parts  = [f"Explanation of '{label}':"]

        if concept_id:
            node   = self._store.get(concept_id) or {}
            domain = node.get("domain", "unknown")
            layer  = node.get("layer", "unknown")
            parts.append(f"  Domain: {domain} | Layer: {layer}")

            # Outgoing edges — what does this concept lead to / cause?
            out_edges  = self._store.edges_from(concept_id) or []
            if out_edges:
                effects = [f"{e.get('to','?').replace('_',' ')} [{e.get('relation','→')}]"
                           for e in out_edges[:4]]
                parts.append(f"  Leads to: {', '.join(effects)}")

            # Incoming edges — what causes / defines this concept?
            in_edges = self._store.edges_to(concept_id) if hasattr(self._store, "edges_to") else []
            if in_edges:
                causes = [f"{e.get('from','?').replace('_',' ')} [{e.get('relation','→')}]"
                          for e in (in_edges or [])[:4]]
                parts.append(f"  Caused by: {', '.join(causes)}")

            # Manifold geodesic context
            if self._manifold and hasattr(self._manifold, "_concept_ids") \
                    and concept_id in self._manifold._concept_ids:
                try:
                    vec      = self._manifold.get_embedding(concept_id)
                    nearest  = self._manifold.nearest_concepts_to(vec, k=5)
                    nbrs     = [n.replace("_", " ") for n in nearest if n != concept_id][:4]
                    if nbrs:
                        parts.append(f"  Conceptually close to: {', '.join(nbrs)}")
                except Exception:
                    pass
        else:
            parts.append(
                f"  I don't have '{slot}' in my KG yet. "
                f"Teach me: [TEACH] {slot}: <definition>"
            )

        return OperatorResult(
            operator_name = op.name,
            noun_slot     = slot,
            concept_id    = concept_id,
            status        = "ok",
            raw_text      = "\n".join(parts),
        )

    # ── Phase 3 social executors ──────────────────────────────────────────────

    def _run_greet(self, op: Operator) -> OperatorResult:
        """
        Greet: social opening/closing/phatic exchange.
        Returns warm system status + invitation to engage.
        No concept grounding needed — purely relational.
        """
        n_nodes = len(list(self._store.all_ids()))
        n_edges = sum(len(self._store.edges_from(cid) or [])
                      for cid in self._store.all_ids())
        manifold_status = "stable" if self._manifold and getattr(
            self._manifold, "_built", False) else "not yet built"

        return OperatorResult(
            operator_name = op.name,
            noun_slot     = "",
            status        = "ok",
            raw_text      = (
                f"Hey Akhil. System is live — "
                f"KG: {n_nodes} concepts, {n_edges} edges | "
                f"Manifold: {manifold_status}.\n"
                f"What do you want to think about?"
            ),
        )

    def _run_sync(self, op: Operator, raw_utterance: str) -> OperatorResult:
        """
        Sync: mid-conversation state signal from the user.
        The RL encoder already classified this as a Sync intent —
        so we respond to the class of intent, not the literal words.

        Sync subtypes (learned by the encoder, not hardcoded here):
          - Affirmative (yes/okay/sure/go ahead/proceed/correct) → ready to continue
          - Negative/cancel (no/never mind/stop/forget it) → pause, await new direction
          - Gratitude (thanks/thank you/appreciate it) → acknowledge warmly
        """
        tl = raw_utterance.lower().strip().rstrip("?.,!")

        # Infer sync subtype from surface signal — not routing logic, just response shaping
        _CANCEL = {"no", "nope", "never mind", "forget it", "stop", "cancel",
                   "nevermind", "abort", "quit", "drop it"}
        _THANKS = {"thanks", "thank you", "appreciate it", "cheers",
                   "ty", "thx", "thank", "appreciated"}

        if any(w in tl for w in _CANCEL):
            msg = "Understood — standing by. Tell me when you're ready."
        elif any(w in tl for w in _THANKS):
            msg = "Of course. What's next?"
        else:
            # Affirmative / proceed
            msg = "Got it. Ready — what should I work on?"

        return OperatorResult(
            operator_name = op.name,
            noun_slot     = "",
            status        = "ok",
            raw_text      = msg,
        )

