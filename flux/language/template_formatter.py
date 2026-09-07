"""
flux/language/template_formatter.py

Deterministic natural-language formatter for KG responses.

Converts structured KG query output into fluent English paragraphs.
No model. No training. Instant deployment.

Serves two purposes:
  1. Immediate production output — Flux speaks natural English today.
  2. Training data baseline — you correct its output for 20-30 concepts
     rather than writing from scratch. Those corrections become the gold
     training set for FluxLanguageModel.

Node-type-aware opening sentences:
  CONCEPT  → "Entropy is a thermodynamic state function..."
  AXIOM    → "The zeroth law states that if system A..."
  ACTION   → "To apply X..."
  DOMAIN   → "X refers to the study of..."
  Others   → "X — definition"
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flux.kg.store import KGStore

log = logging.getLogger(__name__)

# Relation type → natural language verb phrase (for connection sentences)
_RELATION_VERB: dict[str, str] = {
    "REQUIRES":     "requires",
    "ENABLES":      "enables",
    "ANALOGOUS_TO": "is analogous to",
    "PRECEDES":     "leads to",
    "CAUSES":       "causes",
    "CONTRADICTS":  "contradicts",
    "SIMILAR_TO":   "is similar to",
    "IS_A":         "is a type of",
    "RELATES_TO":   "connects to",
    "PRODUCES":     "produces",
    "IMPLIES":      "implies",
    "DERIVES_FROM": "derives from",
    "MODELS":       "models",
    "GENERALIZES":  "generalizes",
}

# Confidence → qualifier string
def _conf_phrase(conf: float) -> str:
    if conf >= 0.95:
        return ""          # high confidence — say nothing, it sounds natural
    if conf >= 0.80:
        return " (high confidence)"
    if conf >= 0.70:
        return " (derived)"
    return " (uncertain)"


def _name(concept_id: str) -> str:
    """concept_id → readable lowercase name."""
    return concept_id.replace("_", " ")


def _title(concept_id: str) -> str:
    """concept_id → title-cased, respecting short stop words."""
    _LOWER = frozenset({"of", "the", "a", "an", "and", "or", "in", "on",
                        "at", "to", "for", "by", "with"})
    words = concept_id.replace("_", " ").split()
    return " ".join(
        w if (i > 0 and w in _LOWER) else w.capitalize()
        for i, w in enumerate(words)
    )


def _opening(name: str, definition: str, node_type: str) -> str:
    """
    Build the opening sentence. Type-aware:
      CONCEPT  → "{Name} is {def lowercased}."
      AXIOM    → "The {name} states that {def lowercased}."
      ACTION   → "To {name lowercased}: {def}."
      DOMAIN   → "{Name} refers to {def lowercased}."
      *        → "{Name} — {def}."
    """
    nt   = (node_type or "").upper()
    defn = (definition or "").strip()

    # Helper: lower-case first char of definition (it usually starts with A/The)
    def lc(s: str) -> str:
        return s[0].lower() + s[1:] if s else s

    if not defn:
        return f"{name}."

    if nt == "CONCEPT":
        # "Entropy is a thermodynamic state function..."
        return f"{name} is {lc(defn).rstrip('.')}."

    elif nt == "AXIOM":
        # "The zeroth law states that if system A..."
        return f"The {name.lower()} states that {lc(defn).rstrip('.')}."

    elif nt == "ACTION":
        return f"To {name.lower()}: {lc(defn).rstrip('.')}."

    elif nt == "DOMAIN":
        return f"{name} refers to {lc(defn).rstrip('.')}."

    else:
        return f"{name} — {defn.rstrip('.')}."


def _join_list(items: list[str], oxford: bool = True) -> str:
    """Join a list into natural English: 'a, b, and c'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    comma = ", and " if oxford else ", "
    return ", ".join(items[:-1]) + comma + items[-1]


class TemplateFormatter:
    """
    Deterministic natural-language formatter for Flux KG responses.

    Usage in ResponseAssembler::
        self._fmt = TemplateFormatter(store)
        # Then in _assemble_explain():
        text = self._fmt.format_explain(concept, stage3, stage4)
    """

    def __init__(self, store: "KGStore") -> None:
        self._store = store

    # ── EXPLAIN ───────────────────────────────────────────────────────────────

    def format_explain(self, concept: str, stage3: dict, stage4) -> str:
        """
        Convert an EXPLAIN stage3+stage4 result to a rich multi-paragraph response.

        Paragraphs (rendered only when data exists):
          1. Opening paragraph — name + full definition + domain context
          2. Examples paragraph — concrete cases and use-cases
          3. Relations paragraph — requires, enables, connects (from KG edges)
          4. Deeper reasoning — stage4 derivations / causal chains
          5. Knowledge gaps — concepts Flux references but hasn't learned yet
          6. Confidence note — only if partial knowledge
        """
        node_type  = stage3.get("type",         "")
        name       = _title(concept)
        definition = stage3.get("definition",   "")
        examples   = stage3.get("examples",     [])
        non_ex     = stage3.get("non_examples", [])
        related    = stage3.get("related",      [])
        confidence = float(stage3.get("confidence", 1.0))
        domain     = str(stage3.get("domain", "") or "").replace("_", " ")
        layer      = str(stage3.get("layer", "") or "")
        immutable  = bool(stage3.get("immutable", False))

        paragraphs: list[str] = []

        # ── Paragraph 1: Opening + definition ────────────────────────────────
        opening = _opening(name, definition, node_type)
        if domain and domain not in ("unknown", "none", ""):
            domain_phrase = f" This falls within the domain of {domain}."
        else:
            domain_phrase = ""
        if layer in ("0", "1") or immutable:
            foundation = " This is a foundational concept — it serves as a basis for much of what I know."
        else:
            foundation = ""
        paragraphs.append(opening + domain_phrase + foundation)

        # ── Paragraph 2: Examples and use-cases ──────────────────────────────
        ex_parts: list[str] = []
        ex_list = [str(e).strip() for e in examples if str(e).strip()][:4]
        if ex_list:
            ex_strs = [e.rstrip(".") for e in ex_list]
            if len(ex_strs) == 1:
                ex_parts.append(f"For example: {ex_strs[0]}.")
            else:
                ex_parts.append(f"Concrete examples include: {_join_list(ex_strs)}.")
        ne_list = [str(e).strip() for e in non_ex if str(e).strip()][:1]
        if ne_list:
            ne = ne_list[0].rstrip(".")
            ex_parts.append(f"Worth noting: {ne[0].lower()}{ne[1:]}.")
        if ex_parts:
            paragraphs.append(" ".join(ex_parts))

        # ── Paragraph 3: Relations — pull directly from KG edges + stage3 ────
        # Merge stage3 related with live KG edges for completeness
        live_edges = self._store.edges_from(concept) if self._store else []
        edge_targets: dict[str, list[str]] = {}   # edge_type → [concept_names]
        for edge in live_edges:
            etype = str(edge.get("type", edge.get("edge", "RELATES_TO"))).upper()
            tgt   = edge.get("to", edge.get("concept", ""))
            if tgt and tgt != concept:
                edge_targets.setdefault(etype, []).append(_name(tgt))

        # Augment with stage3 related items
        for r in related:
            etype = str(r.get("edge", "RELATES_TO")).upper()
            tgt   = r.get("concept", "")
            if tgt and tgt != concept:
                if tgt not in edge_targets.get(etype, []):
                    edge_targets.setdefault(etype, []).append(_name(tgt))

        rel_parts: list[str] = []
        req = edge_targets.get("REQUIRES", [])[:3]
        if req:
            rel_parts.append(f"It requires {_join_list(req)}.")
        enables = edge_targets.get("ENABLES", [])[:3]
        if enables:
            rel_parts.append(f"Understanding this enables {_join_list(enables)}.")
        causes = edge_targets.get("CAUSES", [])[:2]
        if causes:
            rel_parts.append(f"It causes or produces {_join_list(causes)}.")
        connects = (
            edge_targets.get("CONNECTS", []) +
            edge_targets.get("RELATES_TO", []) +
            edge_targets.get("SIMILAR_TO", []) +
            edge_targets.get("ANALOGOUS_TO", [])
        )[:3]
        if connects:
            rel_parts.append(f"It connects to {_join_list(connects)}.")
        if rel_parts:
            paragraphs.append(" ".join(rel_parts))

        # ── Paragraph 4: Derivations from reasoning ───────────────────────────
        if stage4 and hasattr(stage4, "derivations") and stage4.derivations:
            derives = [
                d for d in stage4.derivations
                if d.target != concept and self._store.get(d.target)
            ][:3]
            if derives:
                d_names = [_name(d.target) for d in derives]
                paragraphs.append(
                    f"From this concept, I can reason toward {_join_list(d_names)}."
                )

        # ── Paragraph 5: Knowledge gaps ───────────────────────────────────────
        dangling: list[str] = []
        for edge in live_edges:
            tgt = edge.get("to", edge.get("concept", ""))
            if tgt and not self._store.get(tgt):
                dangling.append(_name(tgt))
        if dangling:
            gn = list(dict.fromkeys(dangling))[:2]   # deduplicate
            them = "it" if len(gn) == 1 else "them"
            paragraphs.append(
                f"I reference {_join_list(gn)} but haven't fully learned "
                f"{them} yet — you can teach me by saying "
                f"\"[TEACH] {gn[0].replace(' ', '_')} ...\""
            )

        # ── Paragraph 6: Confidence note (only for partial knowledge) ─────────
        if confidence < 0.75:
            paragraphs.append(
                f"Note: my knowledge of {name.lower()} is partial "
                f"(confidence {confidence:.0%}). I may be missing nuance."
            )

        return "\n\n".join(paragraphs)

    def format_deep_explain(self, concept: str, stage3: dict, stage4) -> str:
        """
        Elaboration mode — called when user says 'tell me more', 'elaborate', etc.
        Produces a longer response that digs into the KG node's full context.
        """
        # Start with the standard explain
        base = self.format_explain(concept, stage3, stage4)

        # Add: full definition as a separate paragraph if it was truncated
        definition = stage3.get("definition", "")
        if definition and len(definition) > 150:
            base += f"\n\nFull definition:\n{definition}"

        # Add: all concepts that point TO this concept (in-edges)
        in_edges = self._store.edges_to(concept) if hasattr(self._store, "edges_to") else []
        parents: list[str] = []
        for edge in in_edges[:4]:
            src = edge.get("from", edge.get("concept", ""))
            if src and src != concept:
                parents.append(_name(src))
        if parents:
            base += f"\n\nConcepts that depend on or reference this: {_join_list(parents)}."

        return base



    # ── CONNECT ───────────────────────────────────────────────────────────────

    def format_connect(
        self,
        primary: str, secondary: str,
        stage3_p: dict, stage3_s: dict | None,
        stage4,
    ) -> str:
        """Format a CONNECT result as natural English."""
        p = _title(primary)
        s = _title(secondary)

        parts: list[str] = []

        # Brief concept summaries
        p_defn = (stage3_p.get("definition") or "")[:120].rstrip(".")
        s_defn = ((stage3_s or {}).get("definition") or "")[:120].rstrip(".")
        if p_defn:
            lp = p_defn[0].lower() + p_defn[1:]
            parts.append(f"{p} is {lp}.")
        if s_defn:
            ls = s_defn[0].lower() + s_defn[1:]
            parts.append(f"{s} is {ls}.")

        if parts:
            parts.append("")  # blank line separator

        # Path result
        if stage4 and hasattr(stage4, "paths") and stage4.paths:
            best     = max(stage4.paths, key=lambda x: x.propagated)
            path_str = " → ".join(_name(n) for n in best.nodes)
            conf_pct = int(best.propagated * 100)
            hops     = best.depth

            parts.append(
                f"{p} connects to {s} through: {path_str} "
                f"({conf_pct}% confidence, {hops} hop{'s' if hops != 1 else ''})."
            )
            if hops > 3:
                parts.append(
                    "The connection is indirect — lower confidence reflects "
                    "path length, not doubt about the relationship itself."
                )
            if len(stage4.paths) > 1:
                parts.append(f"I found {len(stage4.paths)} possible paths total.")

        elif stage4 and hasattr(stage4, "reachable"):
            if stage4.reachable:
                parts.append(f"{p} can reach {s} through the knowledge graph.")
            else:
                parts.append(
                    f"I don't find a direct path between {p} and {s} within "
                    "5 hops. They may be genuinely unrelated in my current "
                    "graph, or the connection runs through concepts I haven't "
                    "learned yet."
                )
        else:
            # Fallback: check direct edge
            p_related = stage3_p.get("related", [])
            s_target  = (stage3_s or {}).get("target", secondary)
            direct    = next(
                (r for r in p_related if r.get("concept") == s_target), None
            )
            if direct:
                verb = _RELATION_VERB.get(direct.get("edge", ""), "connects to")
                parts.append(f"{p} directly {verb} {s}.")
            else:
                parts.append(
                    f"I don't find a direct connection between {p} and {s} "
                    "in my current knowledge graph."
                )

        return "\n".join(parts)

    # ── GAP ───────────────────────────────────────────────────────────────────

    def format_gap(
        self,
        domain_concept: str | None,
        dangling: list[dict],
        low_conf: list[dict],
        chain_gaps: list[str],
    ) -> str:
        """Format a GAP result as natural English."""
        domain = _name(domain_concept) if domain_concept else None

        if not any([dangling, low_conf, chain_gaps]):
            if domain:
                return (
                    f"My knowledge around {domain} looks reasonably complete "
                    "for what I've been taught. No significant gaps detected."
                )
            return "I don't detect significant gaps in my current knowledge graph."

        parts: list[str] = []

        # Missing (dangling edge targets)
        if dangling:
            top      = sorted(dangling, key=lambda x: -x["ref_count"])[:5]
            gnames   = [_name(g["concept"]) for g in top]
            prefix   = f"Around {domain}, " if domain else ""
            if len(gnames) == 1:
                parts.append(
                    f"{prefix}I'm missing {gnames[0]} — it's referenced by "
                    "concepts I know but I've never been explicitly taught it."
                )
            else:
                parts.append(
                    f"{prefix}I'm missing {_join_list(gnames)}. "
                    "These are referenced by concepts I know but I haven't "
                    "been taught them."
                )

        # Uncertain (low-confidence) concepts
        if low_conf:
            ucnames = [_name(c["id"]) for c in low_conf[:3]]
            avg     = sum(c["confidence"] for c in low_conf[:3]) / len(low_conf[:3])
            parts.append(
                f"I also have uncertain knowledge of {_join_list(ucnames)} "
                f"(average {avg:.0%} confidence) — these could use verification."
            )

        # Derivation chain gaps
        if chain_gaps:
            cgnames = [_name(g) for g in chain_gaps[:3]]
            parts.append(
                f"Teaching me {_join_list(cgnames)} would unlock new reasoning "
                "chains I can't currently complete."
            )

        return "\n\n".join(parts)

    # ── Unknown concept ───────────────────────────────────────────────────────

    def format_unknown(self, concept: str, stage3: dict) -> str:
        """Natural English when the concept isn't in the KG."""
        name    = _title(concept)
        closest = stage3.get("closest_known", [])

        if closest:
            cn     = _title(closest[0])
            cnode  = self._store.get(closest[0])
            cdefn  = ((cnode or {}).get("definition") or "")[:120].rstrip(".")
            text   = f"I haven't been taught about {name} yet."
            if cdefn:
                lc = cdefn[0].lower() + cdefn[1:]
                text += f" The closest I know is {cn}: {lc}."
            else:
                text += f" The closest I know is {cn}."
            text += (
                f"\n\nIf you want to teach me about {name}, send a [TEACH] block."
            )
            return text

        return (
            f"I haven't been taught about {name} yet. "
            f"If you'd like to, send a [TEACH] block and I'll add it to my "
            "knowledge graph."
        )

    # ── TEACH_HINT ───────────────────────────────────────────────────────────

    def format_teach_hint(self, concept: str, stage3: dict, gap_count: int) -> str:
        """Natural English for TEACH_HINT intent."""
        name = _title(concept)
        if stage3.get("found"):
            defn     = stage3.get("definition", "")
            conf     = stage3.get("confidence", 0.0)
            conf_pct = int(conf * 100)
            return f"I already know about {name} ({conf_pct}% confidence). {defn}"

        closest = stage3.get("closest_known", [])
        parts   = [f"I haven't been taught about {name} yet."]
        if closest:
            parts.append(f"The closest concept I know is {_title(closest[0])}.")
        if gap_count >= 2:
            parts.append(
                f"You've asked about {name} {gap_count} times. "
                "Here's a template to teach me:"
            )
        else:
            parts.append("You can teach me with a [TEACH] block.")
        return " ".join(parts)
