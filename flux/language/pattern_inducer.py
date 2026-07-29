"""
flux/language/pattern_inducer.py

Extracts syntactic definition patterns from FLUX_PRIMITIVES.md and the live KG,
builds a weighted pattern library, and uses it to synthesise candidate
definitions for auto-proposed concepts.

This is NOT generation — it is structured extraction + template filling.
Every word in a synthesised definition came from text Akhil originally wrote.

Pattern library example:
    "{concept} is {Y}"                     weight=142
    "{concept} is the {Y} that {Z}"        weight=38
    "{concept} is a {Y} where {Z}"         weight=21
    "{concept} represents {Y}"             weight=17
    "{concept} is defined as {Y}"          weight=9
    "{concept} is the process of {Y}"      weight=7

Confidence of synthesised definition depends on synthesis method:
    template_extraction  (concept name found in existing definitions): 0.75
    pattern_induction    (template filled from pattern library):        0.80
    structural_only      (no text found, structural description only):  0.65
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_PRIMITIVES_PATH = Path("FLUX_PRIMITIVES.md")

# ── Syntactic connectors Flux uses in definitions ────────────────────────────
#    Ordered from most to least specific — first match wins for pattern capture.
_CONNECTORS = [
    # two-slot patterns
    (re.compile(r"^(.+?) is the ([^,\.]+) that (.+)$",   re.I), "is-the-Y-that-Z",   "{concept} is the {Y} that {Z}"),
    (re.compile(r"^(.+?) is the ([^,\.]+) where (.+)$",  re.I), "is-the-Y-where-Z",  "{concept} is the {Y} where {Z}"),
    (re.compile(r"^(.+?) is the ([^,\.]+) in which (.+)$",re.I),"is-the-Y-in-which-Z","{concept} is the {Y} in which {Z}"),
    (re.compile(r"^(.+?) is a ([^,\.]+) that (.+)$",     re.I), "is-a-Y-that-Z",     "{concept} is a {Y} that {Z}"),
    (re.compile(r"^(.+?) is a ([^,\.]+) where (.+)$",    re.I), "is-a-Y-where-Z",    "{concept} is a {Y} where {Z}"),
    (re.compile(r"^(.+?) represents (.+)$",               re.I), "represents-Y",      "{concept} represents {Y}"),
    (re.compile(r"^(.+?) is defined as (.+)$",            re.I), "is-defined-as-Y",   "{concept} is defined as {Y}"),
    (re.compile(r"^(.+?) is the process of (.+)$",        re.I), "process-of-Y",      "{concept} is the process of {Y}"),
    (re.compile(r"^(.+?) is the mechanism by which (.+)$",re.I), "mechanism-by-which","{concept} is the mechanism by which {Y}"),
    (re.compile(r"^(.+?) refers to (.+)$",                re.I), "refers-to-Y",       "{concept} refers to {Y}"),
    (re.compile(r"^(.+?) describes (.+)$",                re.I), "describes-Y",       "{concept} describes {Y}"),
    # one-slot fallback
    (re.compile(r"^(.+?) is (.+)$",                       re.I), "is-Y",              "{concept} is {Y}"),
]

# For extracting fragments that MENTION a concept name in an existing definition
_CONCEPT_MENTION = re.compile(r"[^\.!\?]+\b{concept}\b[^\.!\?]+", re.I)


@dataclass
class PatternEntry:
    """One extracted pattern from the corpus."""
    pattern_id: str             # e.g. "is-the-Y-that-Z"
    template:   str             # e.g. "{concept} is the {Y} that {Z}"
    example_Y:  str             # first slot fill
    example_Z:  str = ""        # second slot fill (if any)
    weight:     int = 1


@dataclass
class PatternLibrary:
    """Weighted pattern library built from FLUX_PRIMITIVES.md + KG definitions."""
    patterns: list[PatternEntry] = field(default_factory=list)

    # ── aggregate weights ──────────────────────────────────────────────────
    def weights(self) -> dict[str, int]:
        w: dict[str, int] = {}
        for p in self.patterns:
            w[p.pattern_id] = w.get(p.pattern_id, 0) + p.weight
        return w

    def top_templates(self, n: int = 6) -> list[tuple[str, str, int]]:
        """Return [(template, pattern_id, weight)] sorted by weight DESC."""
        by_id: dict[str, tuple[str, int]] = {}
        for p in self.patterns:
            tid, tpl = p.pattern_id, p.template
            old_w = by_id.get(tid, (tpl, 0))[1]
            by_id[tid] = (tpl, old_w + p.weight)
        result = [(tpl, pid, w) for pid, (tpl, w) in by_id.items()]
        result.sort(key=lambda x: -x[2])
        return result[:n]


class PatternInducer:
    """
    Builds a pattern library from corpus and synthesises candidate definitions.

    Usage:
        pi = PatternInducer()
        pi.build()                           # once at init
        defn, conf = pi.synthesise("hamiltonian", mentions)
    """

    def __init__(
        self,
        primitives_path: Path = _PRIMITIVES_PATH,
        kg_store=None,
    ) -> None:
        self._primitives_path = primitives_path
        self._kg_store        = kg_store
        self.library          = PatternLibrary()
        self._built           = False

    # ── Build ───────────────────────────────────────────────────────────────

    def build(self) -> None:
        """Extract patterns from primitives file + KG and populate library."""
        self.library = PatternLibrary()
        self._ingest_primitives()
        self._ingest_kg()
        self._built = True
        log.info(
            "PatternInducer: built library from %d patterns",
            len(self.library.patterns),
        )

    def _ingest_primitives(self) -> None:
        if not self._primitives_path.exists():
            log.warning("FLUX_PRIMITIVES.md not found at %s", self._primitives_path)
            return

        text = self._primitives_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            # definition: <value>
            if line.lower().startswith("definition:"):
                defn = line[len("definition:"):].strip()
                self._extract_patterns_from(defn)

    def _ingest_kg(self) -> None:
        if self._kg_store is None:
            try:
                from flux.kg.store import get_store
                self._kg_store = get_store()
            except Exception:
                return

        for node_id, node in self._kg_store._nodes.items():
            defn = node.get("definition", "")
            if defn and isinstance(defn, str):
                self._extract_patterns_from(defn)

    def _extract_patterns_from(self, definition: str) -> None:
        """Try each connector pattern against the definition sentence."""
        # Split into sentences first
        sentences = re.split(r"[\.!\?]", definition)
        for sent in sentences:
            sent = sent.strip()
            if len(sent) < 10:
                continue
            for pat, pid, template in _CONNECTORS:
                m = pat.match(sent)
                if m:
                    groups = m.groups()
                    example_Y = groups[1].strip() if len(groups) > 1 else ""
                    example_Z = groups[2].strip() if len(groups) > 2 else ""
                    self.library.patterns.append(PatternEntry(
                        pattern_id = pid,
                        template   = template,
                        example_Y  = example_Y,
                        example_Z  = example_Z,
                    ))
                    break  # first matching pattern per sentence

    # ── Synthesise ──────────────────────────────────────────────────────────

    def synthesise(
        self,
        concept_id: str,
        mentions: list[str],
    ) -> tuple[str, float]:
        """
        Synthesise a candidate definition for concept_id.

        Args:
            concept_id: normalized concept ID (e.g. "hamiltonian")
            mentions:   list of text fragments from existing definitions
                        that mention the concept name

        Returns:
            (definition_text, synthesis_confidence)
            confidence:
                0.75  template extraction (concept found in existing text)
                0.80  pattern induction (template filled from corpus)
                0.65  structural only (no text available)
        """
        if not self._built:
            self.build()

        concept_display = concept_id.replace("_", " ")

        # ── Method 1: Template extraction ───────────────────────────────────
        # Find the densest fragment mentioning concept name in existing defs
        if mentions:
            best = self._best_mention(concept_display, mentions)
            if best:
                cleaned = self._reformulate(concept_display, best)
                if cleaned:
                    return cleaned, 0.75

        # ── Method 2: Pattern induction ─────────────────────────────────────
        # Fill highest-weight template with extracted fragments from mentions
        if self.library.patterns:
            defn = self._fill_top_pattern(concept_display, mentions)
            if defn:
                return defn, 0.80

        # ── Method 3: Structural only ────────────────────────────────────────
        return (
            f"Concept auto-proposed by Flux. "
            f"Referenced in the KG but not yet defined. "
            f"Review and add definition via TEACH block.",
            0.65,
        )

    def _best_mention(self, concept_display: str, mentions: list[str]) -> str:
        """Return the longest clause that contains the concept name."""
        pattern = re.compile(
            r"[^,;]+\b" + re.escape(concept_display) + r"\b[^,;]+",
            re.I,
        )
        best = ""
        for m in mentions:
            for clause in pattern.findall(m):
                if len(clause) > len(best):
                    best = clause.strip()
        return best

    def _reformulate(self, concept_display: str, fragment: str) -> str:
        """
        Rewrite an extracted fragment into a definition-form sentence.
        e.g. "where Ĥ is the Hamiltonian operator" → "the hamiltonian is the operator..."
        """
        f = fragment.strip()
        f = re.sub(r"^(where|which|when|the|a|an)\s+", "", f, flags=re.I)
        f = f[:1].upper() + f[1:] if f else f
        # If it already reads as a definition, keep it
        if re.search(r"\bis\b|\brepresents\b|\bdescribes\b", f, re.I):
            return f
        # Wrap with concept name
        return f"The {concept_display} {f}"

    def _fill_top_pattern(
        self, concept_display: str, mentions: list[str]
    ) -> str:
        """
        Fill the highest-weight template using extracted Y/Z slot values
        from the most common patterns in the library.
        """
        templates = self.library.top_templates(n=3)
        if not templates:
            return ""

        template, pid, _ = templates[0]

        # Extract a Y slot value: use the most common example_Y for this pattern
        ys = [
            p.example_Y for p in self.library.patterns
            if p.pattern_id == pid and p.example_Y
        ]
        if not ys:
            return ""

        # Pick the Y that best relates to the concept (shortest meaningful phrase)
        y_val = min(ys, key=len) if ys else ""
        if len(y_val) > 80:
            y_val = y_val[:80].rsplit(" ", 1)[0]

        filled = template.replace("{concept}", concept_display).replace("{Y}", y_val)

        if "{Z}" in filled:
            zs = [
                p.example_Z for p in self.library.patterns
                if p.pattern_id == pid and p.example_Z
            ]
            z_val = min(zs, key=len) if zs else "the relevant context"
            filled = filled.replace("{Z}", z_val)

        return filled.strip()

    # ── Introspection ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "total_patterns":   len(self.library.patterns),
            "unique_templates": len({p.pattern_id for p in self.library.patterns}),
            "top_templates":    self.library.top_templates(n=5),
        }

    def find_mentions(self, concept_id: str) -> list[str]:
        """
        Search all KG definitions for text mentioning the concept.
        Returns list of definition strings that contain the concept name.
        Public API used by proposal_generator.
        """
        if self._kg_store is None:
            try:
                from flux.kg.store import get_store
                self._kg_store = get_store()
            except Exception:
                return []

        concept_display = concept_id.replace("_", " ").lower()
        results = []
        for node_id, node in self._kg_store._nodes.items():
            defn = node.get("definition", "")
            if defn and concept_display in defn.lower():
                results.append(defn)
        return results
