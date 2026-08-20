"""
flux/language/response_assembler.py

Converts Stage 3 (QueryEngine) + Stage 4 (ReasoningEngine) output into
structured text responses.

Template-based composition — no natural language generation.
Every section has a clear template. CfC-Voice (Stage 8) will render
these structured responses into natural speech.

Confidence labels (Q9):
  >= 0.95  direct — taught by Akhil
  >= 0.80  high confidence
  >= 0.70  derived — reasoning chain
  < 0.70   uncertain — needs verification
  seed/constitutional — always known (immutable)

Response sections by intent:
  EXPLAIN:    WHAT I KNOW + WHAT IT NEEDS + WHAT IT CONNECTS TO + GAPS
  CONNECT:    WHAT I KNOW (both) + HOW THEY CONNECT (path) + GAPS
  GAP:        WHAT I DON'T KNOW (dangling + low-conf + chain gaps)
  TEACH_HINT: UNKNOWN CONCEPT + TEACH TEMPLATE (on 2nd+ query)
  CLARIFICATION_NEEDED: polite request for clarification
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from flux.kg.store             import KGStore
from flux.reasoning.inference  import EDGE_WEIGHTS
from flux.language.session_handler import DOMAIN_ANCHORS, SessionHandler


log = logging.getLogger(__name__)

# ── Confidence labeling (Q9) ───────────────────────────────────────────────────

def confidence_label(conf: float, source: str = "", immutable: bool = False) -> str:
    """Return '{conf:.3f} (qualitative label)' string."""
    if immutable or source in ("SEED",):
        return f"{conf:.3f} (constitutional — always known)"
    if conf >= 0.95:
        return f"{conf:.3f} (direct — taught by Akhil)"
    if conf >= 0.80:
        return f"{conf:.3f} (high confidence)"
    if conf >= 0.70:
        return f"{conf:.3f} (derived — reasoning chain)"
    return f"{conf:.3f} (uncertain — needs verification)"


# ── TEACH block template ───────────────────────────────────────────────────────

def _teach_template(concept_id: str) -> str:
    return (
        f"  [TEACH]\n"
        f"  id: {concept_id}\n"
        f"  concept: {concept_id}\n"
        f"  is_a: CONCEPT\n"
        f"  definition: (your definition here)\n"
        f"  examples: (example 1) | (example 2)\n"
        f"  non_examples: (non-example)\n"
        f"  requires: (required_concept)\n"
        f"  relates_to: (related_concept):ENABLES\n"
        f"  [/TEACH]"
    )


# ── AssembledResponse ──────────────────────────────────────────────────────────

@dataclass
class AssembledResponse:
    """Result of response assembly."""
    text:                str
    intent:              str
    primary_concept:     str | None
    secondary_concept:   str | None
    concepts_referenced: list[str]        = field(default_factory=list)
    gaps_surfaced:       list[str]        = field(default_factory=list)
    clarification_needed: bool            = False
    clarification_guesses: list[str]      = field(default_factory=list)


# ── ResponseAssembler ──────────────────────────────────────────────────────────

class ResponseAssembler:
    """
    Assembles structured text responses from Stage 3 + Stage 4 output.
    All methods return AssembledResponse with full metadata.
    """

    def __init__(self, store: KGStore, formatter=None) -> None:
        self.store = store
        self._fmt  = formatter   # TemplateFormatter (Tier 1 fallback)
        self._flm  = None        # FluxLanguageModel (Tier 2 — loaded lazily)
        self._flm_loaded = False # True once we've attempted to load (even if failed)

    def _load_flm(self) -> None:
        """Lazily load FluxLanguageModel. Called once on first explain query."""
        if self._flm_loaded:
            return
        self._flm_loaded = True
        try:
            from pathlib import Path
            ckpt = Path("checkpoints/generation/flux_lm_best.pt")
            if not ckpt.exists():
                return
            from flux.language.generation.flux_language_model import FluxLanguageModel
            import torch
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._flm = FluxLanguageModel.load(lambda t: None, device=device)
            self._flm.eval()
        except Exception as exc:
            log.debug("FluxLanguageModel not loaded (%s) — using TemplateFormatter", exc)

    @staticmethod
    def _flm_quality_ok(text: str) -> bool:
        """
        Quality gate for FluxLanguageModel output.
        Returns True only if the output is coherent natural language.

        Checks:
          1. At least 30 real alphabetic words (not punctuation tokens)
          2. Type-token ratio > 0.35 (enough vocabulary variety)
          3. Most common bigram < 20% of all bigrams (no repetition loops)
        """
        if not text:
            return False
        import re
        alpha_words = re.findall(r"[a-zA-Z']+", text)
        if len(alpha_words) < 30:
            return False
        unique = len(set(w.lower() for w in alpha_words))
        ttr = unique / len(alpha_words)
        if ttr < 0.35:
            return False
        # Bigram repetition
        bigrams = [f"{alpha_words[i]}_{alpha_words[i+1]}" for i in range(len(alpha_words) - 1)]
        if bigrams:
            most_common_count = max(bigrams.count(b) for b in set(bigrams))
            if most_common_count / len(bigrams) > 0.2:
                return False
        return True

    def assemble(
        self,
        intent:             str,
        primary_concept:    str | None,
        secondary_concept:  str | None,
        stage3_primary:     dict,
        stage3_secondary:   dict | None,
        stage4_result,                      # ReasoningResult | None
        gap_count:          int = 0,        # times this unknown concept was queried
    ) -> AssembledResponse:
        """
        Route to the correct assembly method based on intent.
        """
        if intent == "EXPLAIN":
            return self._assemble_explain(primary_concept, stage3_primary, stage4_result)
        elif intent == "CONNECT":
            return self._assemble_connect(
                primary_concept, secondary_concept,
                stage3_primary, stage3_secondary, stage4_result
            )
        elif intent == "GAP":
            return self._assemble_gap(primary_concept, stage3_primary, stage4_result)
        elif intent == "TEACH_HINT":
            return self._assemble_teach_hint(primary_concept, stage3_primary, gap_count)
        elif intent == "CLARIFICATION_NEEDED":
            return self._assemble_clarification(primary_concept, stage3_primary)
        else:
            return AssembledResponse(
                text             = f"[Unknown intent: {intent}]",
                intent           = intent,
                primary_concept  = primary_concept,
                secondary_concept = secondary_concept,
            )

    # ── EXPLAIN ────────────────────────────────────────────────────────────────

    def _assemble_explain(
        self,
        concept: str | None,
        stage3:  dict,
        stage4,
    ) -> AssembledResponse:
        lines:    list[str] = []
        refs:     list[str] = []
        gaps:     list[str] = []

        found = stage3.get("found", False)

        if not found:
            return self._unknown_concept(concept or "", stage3)

        target  = stage3["target"]
        conf    = stage3.get("confidence", 0.0)
        defn    = stage3.get("definition", "")
        src     = stage3.get("source",     "TAUGHT")
        immut   = stage3.get("immutable",  False)
        c_type  = stage3.get("type",       "")
        c_layer = stage3.get("layer",      "")
        related = stage3.get("related",    [])
        exmps   = stage3.get("examples",   [])
        non_ex  = stage3.get("non_examples", [])

        refs.append(target)

        # Section 1: WHAT I KNOW
        lines.append(f"WHAT I KNOW: {target}")
        lines.append(f"  Definition: {defn or '(no definition recorded)'}")
        lines.append(f"  Confidence: {confidence_label(conf, src, immut)}")
        if c_type:
            lines.append(f"  Type: {c_type}  |  Layer: {c_layer}")
        if exmps:
            lines.append(f"  Examples: {' | '.join(str(e) for e in exmps[:3])}")
        if non_ex:
            lines.append(f"  Non-examples: {' | '.join(str(e) for e in non_ex[:2])}")

        # Section 2: WHAT IT NEEDS (requires edges)
        requires = [r for r in related if r.get("edge") == "REQUIRES"]
        if requires:
            lines.append("")
            lines.append("WHAT IT NEEDS:")
            for r in requires[:5]:
                rc = r["concept"]
                refs.append(rc)
                rn = self.store.get(rc)
                status = "(known)" if rn else "[!!] (not yet taught)"
                if not rn:
                    gaps.append(rc)
                lines.append(f"  {rc}  {status}")

        # Section 3: WHAT IT CONNECTS TO (enables/relates_to edges)
        connects = [r for r in related if r.get("edge") not in ("REQUIRES", "IS_A")]
        if connects:
            lines.append("")
            lines.append("WHAT IT CONNECTS TO:")
            for r in connects[:8]:
                rc    = r["concept"]
                etype = r.get("edge", "RELATES_TO")
                rconf = r.get("confidence", 0.0)
                refs.append(rc)
                lines.append(f"  -> {rc} ({etype}, conf={rconf:.3f})")

        # Section 4: Stage 4 derivations
        if stage4 and hasattr(stage4, "derivations") and stage4.derivations:
            derives = [d for d in stage4.derivations if d.target != target][:5]
            if derives:
                lines.append("")
                lines.append("WHAT I CAN DERIVE:")
                for d in derives:
                    refs.append(d.target)
                    lines.append(
                        f"  {d.relation:<20} -> {d.target:<30} conf={d.confidence:.3f}"
                    )

        # Section 5: GAPS
        dangling = self._find_dangling(target)
        all_gaps = list(dict.fromkeys(gaps + [g["concept"] for g in dangling]))   # dedup
        if all_gaps:
            lines.append("")
            lines.append("WHAT I DON'T KNOW YET:")
            for g in all_gaps[:8]:
                n_refs = sum(1 for _, edges in self.store._edges_from.items()
                             for e in edges if e.get("to") == g)
                lines.append(f"  {g}  (referenced by {n_refs} known concepts)")

        # ── Build response text: FLM → TemplateFormatter → raw structured ──────
        self._load_flm()

        text = None

        # Tier 2: FluxLanguageModel (if loaded and quality gate passes)
        if self._flm is not None:
            try:
                from flux.language.generation.flux_language_model import build_node_fields
                import torch
                device = next(self._flm.parameters()).device
                node_fields = build_node_fields(target, stage3, self.store)
                raw = self._flm.generate(
                    node_fields, device=device, max_words=150,
                    temperature=0.85, top_k=20,
                )
                if self._flm_quality_ok(raw):
                    text = raw
            except Exception as exc:
                log.debug("FluxLanguageModel generation failed (%s) — falling back", exc)

        # Tier 1: TemplateFormatter
        if text is None and self._fmt is not None:
            text = self._fmt.format_explain(target, stage3, stage4)

        # Tier 0: raw structured text
        if text is None:
            text = "\n".join(lines)

        return AssembledResponse(
            text              = text,
            intent            = "EXPLAIN",
            primary_concept   = target,
            secondary_concept = None,
            concepts_referenced = list(dict.fromkeys(refs)),
            gaps_surfaced     = all_gaps,
        )

    # ── CONNECT ────────────────────────────────────────────────────────────────

    def _assemble_connect(
        self,
        primary:    str | None,
        secondary:  str | None,
        stage3_p:   dict,
        stage3_s:   dict | None,
        stage4,
    ) -> AssembledResponse:
        lines:    list[str] = []
        refs:     list[str] = []
        gaps:     list[str] = []

        p_found = stage3_p.get("found", False)
        s_found = stage3_s.get("found", False) if stage3_s else False

        # Handle unknown concepts (Q10)
        if not p_found and not s_found:
            return self._unknown_both(primary, secondary, stage3_p, stage3_s)
        if not p_found:
            return self._unknown_one_connect(primary, secondary, stage3_p, stage3_s, stage4)
        if not s_found:
            return self._unknown_one_connect(primary, secondary, stage3_p, stage3_s, stage4, unknown_is_secondary=True)

        p_target = stage3_p["target"]
        s_target = (stage3_s or {}).get("target", secondary or "")
        refs.extend([p_target, s_target])

        # Brief summary of each concept
        lines.append(f"WHAT I KNOW:")
        for s3, t in [(stage3_p, p_target), (stage3_s, s_target)]:
            if s3 and s3.get("found"):
                conf = s3.get("confidence", 0.0)
                src  = s3.get("source", "")
                immut= s3.get("immutable", False)
                defn = (s3.get("definition") or "")[:120]
                lines.append(f"  {t}")
                lines.append(f"    {defn}")
                lines.append(f"    Confidence: {confidence_label(conf, src, immut)}")

        # Path reasoning
        if stage4 and hasattr(stage4, "paths") and stage4.paths:
            lines.append("")
            lines.append(f"HOW THEY CONNECT ({p_target} -> {s_target}):")
            for i, path in enumerate(stage4.paths[:3], 1):
                path_str = " -> ".join(path.nodes)
                lines.append(
                    f"  Path {i}: {path_str}  "
                    f"(conf={path.propagated:.3f}, {path.depth} hop{'s' if path.depth != 1 else ''})"
                )
                refs.extend(path.nodes)
        elif stage4 and hasattr(stage4, "reachable"):
            # REACHABLE result
            if stage4.reachable:
                lines.append("")
                lines.append(f"CONNECTION: {p_target} can reach {s_target}")
            else:
                lines.append("")
                lines.append(f"NO DIRECT CONNECTION FOUND between {p_target} and {s_target}")
                lines.append("  (within depth 5, excluding CONTRADICTS edges)")
        else:
            lines.append("")
            lines.append(f"HOW THEY CONNECT: checking {p_target} -> {s_target}...")
            lines.append("  (run Stage 4 PATH query for path details)")

        # Known connections from Stage 3 related field
        p_related = stage3_p.get("related", [])
        direct_edge = next(
            (r for r in p_related if r.get("concept") == s_target), None
        )
        if direct_edge:
            lines.append(f"  Direct edge: {direct_edge.get('edge','RELATES_TO')} "
                         f"(conf={direct_edge.get('confidence',0.0):.3f})")

        return AssembledResponse(
            text              = self._fmt.format_connect(
                                    primary, secondary, stage3_p, stage3_s, stage4)
                                if self._fmt else "\n".join(lines),
            intent            = "CONNECT",
            primary_concept   = p_target,
            secondary_concept = s_target,
            concepts_referenced = list(dict.fromkeys(refs)),
            gaps_surfaced     = gaps,
        )

    # ── GAP ────────────────────────────────────────────────────────────────────

    def _assemble_gap(
        self,
        domain_concept: str | None,
        stage3:         dict,
        stage4,
    ) -> AssembledResponse:
        lines:    list[str] = []
        gaps:     list[str] = []

        lines.append("WHAT I DON'T KNOW YET:")
        lines.append("")

        # 1. Dangling edges (referenced but missing from KG)
        # Check if domain_concept maps to a known domain phrase first
        anchor_nodes = SessionHandler.get_domain_anchors(domain_concept or "") if domain_concept else []
        if not anchor_nodes:
            # Treat as a specific concept or global scan
            dangling = self._find_dangling_all() if not domain_concept else self._find_dangling(domain_concept or "")
        else:
            # Expand across all anchor nodes for the domain
            all_dangling: dict[str, int] = {}
            for anchor in anchor_nodes:
                for entry in self._find_dangling(anchor):
                    all_dangling[entry["concept"]] = all_dangling.get(entry["concept"], 0) + entry["ref_count"]
            # Also global dangling for good measure
            for entry in self._find_dangling_all():
                if entry["concept"] in all_dangling:
                    all_dangling[entry["concept"]] = max(all_dangling[entry["concept"]], entry["ref_count"])
            dangling = [{"concept": k, "ref_count": v} for k, v in all_dangling.items()]
            lines.append(f"  (Domain: {domain_concept or ''} | anchors: {', '.join(anchor_nodes[:3])}...)")

        if dangling:
            lines.append("  -- Missing concepts (referenced by known nodes):")
            for entry in sorted(dangling, key=lambda x: -x["ref_count"])[:15]:
                gaps.append(entry["concept"])
                lines.append(f"    {entry['concept']:<35} (referenced {entry['ref_count']}x)")


        # 2. Low-confidence concepts (< 0.70)
        low_conf = self._find_low_confidence(domain_concept)
        if low_conf:
            lines.append("")
            lines.append("  -- Uncertain concepts (confidence < 0.70):")
            for c in low_conf[:8]:
                gaps.append(c["id"])
                lines.append(f"    {c['id']:<35} conf={c['confidence']:.3f}")

        # 3. Stage 4 derivation chain gaps
        if stage4 and hasattr(stage4, "derivations"):
            chain_gaps = [
                d.target for d in stage4.derivations
                if not self.store.get(d.target)
            ][:5]
            if chain_gaps:
                lines.append("")
                lines.append("  -- Derivation chain gaps (would unlock new reasoning):")
                for g in chain_gaps:
                    gaps.append(g)
                    lines.append(f"    {g}")

        if not any([dangling, low_conf]):
            lines.append("  No significant gaps detected in the current KG.")
            if domain_concept:
                lines.append(f"  (Searched around: {domain_concept})")

        return AssembledResponse(
            text              = self._fmt.format_gap(
                                    domain_concept, dangling,
                                    low_conf, chain_gaps)
                                if self._fmt else "\n".join(lines),
            intent            = "GAP",
            primary_concept   = domain_concept,
            secondary_concept = None,
            concepts_referenced = [],
            gaps_surfaced     = list(dict.fromkeys(gaps)),
        )

    # ── TEACH_HINT ────────────────────────────────────────────────────────────

    def _assemble_teach_hint(
        self,
        concept:   str | None,
        stage3:    dict,
        gap_count: int,
    ) -> AssembledResponse:
        lines: list[str] = []
        found = stage3.get("found", False)

        if found:
            defn = stage3.get("definition", "")
            conf = stage3.get("confidence", 0.0)
            lines.append(f"I already know {concept}.")
            lines.append(f"  {defn}")
            lines.append(f"  Confidence: {confidence_label(conf)}")
        else:
            closest = stage3.get("closest_known", [])
            lines.append(f"I haven't been taught about {concept} yet.")
            if closest:
                lines.append(f"  Closest I know: {', '.join(closest)}")
            lines.append("")

            # Q8: TEACH suggestion only on 2+ gap queries
            if gap_count >= 2:
                lines.append(
                    f"You've asked about {concept} {gap_count} times."
                    " Here's a TEACH template:"
                )
                lines.append("")
                lines.append(_teach_template(concept or "unknown"))
            else:
                lines.append(
                    f"If you want to teach me {concept}, send a [TEACH] block."
                )

        return AssembledResponse(
            text              = "\n".join(lines),
            intent            = "TEACH_HINT",
            primary_concept   = concept,
            secondary_concept = None,
        )

    # ── CLARIFICATION_NEEDED ──────────────────────────────────────────────────

    def _assemble_clarification(
        self,
        concept: str | None,
        stage3:  dict,
    ) -> AssembledResponse:
        guesses = stage3.get("closest_known", [])
        if guesses:
            guess_str = " or ".join(f"'{g}'" for g in guesses[:2])
            text = (
                f"I'm not sure what you're asking about. "
                f"Did you mean {guess_str}? "
                f"Or rephrase as: 'what is X?' / 'how does X connect to Y?'"
            )
        else:
            text = (
                "I didn't understand that. Try:\n"
                "  'what is [concept]?'\n"
                "  'how does [A] connect to [B]?'\n"
                "  'what don't you know about [domain]?'"
            )
        return AssembledResponse(
            text                 = text,
            intent               = "CLARIFICATION_NEEDED",
            primary_concept      = concept,
            secondary_concept    = None,
            clarification_needed = True,
            clarification_guesses = guesses,
        )

    # ── Unknown concept helpers (Q10) ─────────────────────────────────────────

    def _unknown_concept(self, concept: str, stage3: dict) -> AssembledResponse:
        closest = stage3.get("closest_known", [])
        lines   = [f"I haven't been taught about '{concept}' yet."]
        if closest:
            lines.append(f"  Closest I know: {', '.join(closest)}")
            lines.append(
                f"\nWhat I can tell you about {closest[0]}:"
            )
            node = self.store.get(closest[0])
            if node:
                lines.append(f"  {node.get('definition','')[:120]}")
        return AssembledResponse(
            text             = "\n".join(lines),
            intent           = "EXPLAIN",
            primary_concept  = concept,
            secondary_concept = None,
            gaps_surfaced    = [concept],
        )

    def _unknown_one_connect(
        self, primary, secondary,
        stage3_p, stage3_s, stage4,
        unknown_is_secondary: bool = False,
    ) -> AssembledResponse:
        unknown = secondary if unknown_is_secondary else primary
        known   = primary   if unknown_is_secondary else secondary
        known_s3 = stage3_p if not unknown_is_secondary else stage3_s

        known_defn = (known_s3 or {}).get("definition", "")[:100]
        closest    = (stage3_s if unknown_is_secondary else stage3_p).get("closest_known", [])

        lines = [
            f"I don't know '{unknown}' yet — so I can't trace its connection to '{known}'.",
            f"If you teach me '{unknown}', I can immediately answer this.",
            "",
            f"What I know about '{known}':",
            f"  {known_defn}",
        ]
        known_node = self.store.get(known or "")
        if known_node:
            known_related = self.store.edges_from(known or "")
            if known_related:
                lines.append("")
                lines.append(f"What '{known}' connects to (concepts I do know):")
                for edge in known_related[:5]:
                    lines.append(f"  -> {edge.get('to')} ({edge.get('type','RELATES_TO')})")

        return AssembledResponse(
            text              = "\n".join(lines),
            intent            = "CONNECT",
            primary_concept   = primary,
            secondary_concept = secondary,
            gaps_surfaced     = [unknown or ""],
        )

    def _unknown_both(self, primary, secondary, stage3_p, stage3_s) -> AssembledResponse:
        return AssembledResponse(
            text = (
                f"I haven't been taught about '{primary}' or '{secondary}' yet.\n"
                f"Teach me both concepts and I can reason about their connection."
            ),
            intent            = "CONNECT",
            primary_concept   = primary,
            secondary_concept = secondary,
            gaps_surfaced     = [c for c in [primary, secondary] if c],
        )

    # ── KG gap analysis helpers ───────────────────────────────────────────────

    def _find_dangling(self, concept_id: str) -> list[dict]:
        """Find edges from concept_id pointing to unknown concepts."""
        result = []
        for edge in self.store.edges_from(concept_id):
            tgt = edge.get("to", "")
            if tgt and not self.store.get(tgt):
                result.append({"concept": tgt, "ref_count": 1})
        return result

    def _find_dangling_all(self) -> list[dict]:
        """Find all dangling edges across the full KG, sorted by reference count."""
        missing: dict[str, int] = {}
        for src_id, edges in self.store._edges_from.items():
            for edge in edges:
                tgt = edge.get("to", "")
                if tgt and not self.store.get(tgt):
                    missing[tgt] = missing.get(tgt, 0) + 1
        return [{"concept": k, "ref_count": v} for k, v in missing.items()]

    def _find_low_confidence(self, domain_concept: str | None) -> list[dict]:
        """Find live (non-seed) concepts with confidence < 0.70."""
        LOW_THRESH = 0.70
        result = []
        for cid, node in self.store.all_nodes().items():
            if node.get("source") in ("SEED", "SYNTHETIC"):
                continue
            conf = float(node.get("confidence", 1.0))
            if conf < LOW_THRESH:
                result.append({"id": cid, "confidence": conf})
        return sorted(result, key=lambda x: x["confidence"])[:10]
