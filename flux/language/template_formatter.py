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
        Convert an EXPLAIN stage3+stage4 result to a natural English paragraph.

        Sections (only rendered if data exists):
          1. Opening sentence  — name + definition
          2. Examples          — "For example, ..."
          3. Non-examples      — "Worth noting: ..."
          4. Dependencies      — "It requires ..."
          5. Connections       — "It enables / is analogous to ..."
          6. Derivations       — "From this, I can reason toward ..."
          7. Gaps              — "I reference X but haven't been taught it yet"
        """
        node_type  = stage3.get("type",        "")
        name       = _title(concept)
        definition = stage3.get("definition",  "")
        examples   = stage3.get("examples",    [])
        non_ex     = stage3.get("non_examples", [])
        related    = stage3.get("related",     [])

        sentences: list[str] = []

        # 1. Opening
        sentences.append(_opening(name, definition, node_type))

        # 2. Examples
        ex_list = [str(e).strip() for e in examples if str(e).strip()][:3]
        if ex_list:
            ex_strs = [e.rstrip(".") for e in ex_list]
            if len(ex_strs) == 1:
                sentences.append(f"For example: {ex_strs[0].lower()}.")
            else:
                sentences.append(
                    f"Examples include {_join_list([s.lower() for s in ex_strs])}."
                )

        # 3. Non-examples
        ne_list = [str(e).strip() for e in non_ex if str(e).strip()][:1]
        if ne_list:
            ne = ne_list[0].rstrip(".")
            sentences.append(f"Worth noting: {ne[0].lower()}{ne[1:]}.")

        # 4. Dependencies (REQUIRES edges)
        requires = [r for r in related if r.get("edge") == "REQUIRES"][:3]
        if requires:
            req_names = [_name(r["concept"]) for r in requires]
            sentences.append(
                f"It requires {_join_list(req_names)}."
            )

        # 5. Connections (everything except REQUIRES / IS_A)
        connects = [r for r in related
                    if r.get("edge") not in ("REQUIRES", "IS_A")][:4]
        if connects:
            conn_parts = []
            for r in connects:
                verb  = _RELATION_VERB.get(r.get("edge", ""), "connects to")
                tname = _name(r["concept"])
                conn_parts.append(f"{verb} {tname}")
            sentences.append(f"It {_join_list(conn_parts)}.")

        # 6. Stage 4 derivations (only well-known targets)
        if stage4 and hasattr(stage4, "derivations") and stage4.derivations:
            derives = [
                d for d in stage4.derivations
                if d.target != concept and self._store.get(d.target)
            ][:2]
            if derives:
                d_names = [_name(d.target) for d in derives]
                sentences.append(
                    f"From this, I can reason toward {_join_list(d_names)}."
                )

        # 7. Gaps (dangling edges)
        dangling_names = []
        for edge in self._store.edges_from(concept):
            tgt = edge.get("to", "")
            if tgt and not self._store.get(tgt):
                dangling_names.append(_name(tgt))
        if dangling_names:
            gn = dangling_names[:2]
            them = "it" if len(gn) == 1 else "them"
            sentences.append(
                f"I reference {_join_list(gn)} but haven't been taught "
                f"{them} yet — want to teach me?"
            )

        return " ".join(sentences)

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
