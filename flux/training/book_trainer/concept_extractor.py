"""
flux/training/book_trainer/concept_extractor.py

STRICT concept extraction from book pages.

"Strict" means: only extract what is EXPLICITLY defined.
  ✓ "Definition 2.3. A metric space is a set X equipped with..."
  ✓ "Theorem 4.1 (Boltzmann). The entropy of a system is..."
  ✓ "A random variable X is said to be Gaussian if..."
  ✗ "We can think of entropy as disorder..." (interpretation, not definition)

Why strict? Because Flux learning = KG edges from grounded definitions.
Adding half-understood concepts creates noise that corrupts reasoning.

The formula extractor runs on every extracted concept to capture
the mathematical expression of each definition.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .formula_extractor import FormulaExtractor, ExtractedFormula
from .pdf_extractor import PageBlock, BookPage

log = logging.getLogger(__name__)

_formula_ex = FormulaExtractor()


@dataclass
class ExtractedConcept:
    """
    A single concept extracted from a book.
    This maps directly to a KGStore node.
    """
    concept_id:       str                        # snake_case ID for KG
    name:             str                        # human-readable name
    definition:       str                        # full definition text
    formulas:         list[ExtractedFormula]     # associated formulas
    domain:           str                        # e.g. "information_theory"
    source_book:      str                        # book title
    source_page:      int                        # page number
    chapter:          Optional[str]              # chapter title
    section:          Optional[str]              # section title
    concept_type:     str                        # "definition" | "theorem" | "lemma" | "corollary" | "axiom" | "algorithm"
    depends_on:       list[str]                  # concept IDs mentioned in definition (edge hints)
    confidence:       float = 0.90               # extraction confidence
    immutable:        bool  = False              # theorems get immutable=True
    examples:         list[str] = field(default_factory=list)
    raw_block:        str = ""                   # original text for debugging


# ── Extraction patterns ────────────────────────────────────────────────────────

# Explicit definition markers in academic texts
_DEFINITION_PATTERNS: list[tuple[str, str, float]] = [
    # (regex pattern, concept_type, confidence)

    # ── STANDARD ACADEMIC FORMAT ──────────────────────────────────────────────
    # "Definition 2.3. ..." or "Definition 2.3 (Name)."
    (r"^(?:Definition|Def\.?)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "definition", 0.98),
    # "Definition (Entropy). ..."
    (r"^(?:Definition|Def\.?)\s*\(([^)]+)\)\.?\s*(.+)",
     "definition", 0.97),
    # "Definition. ..."
    (r"^(?:Definition|Def\.?)\.\s+(.+)",
     "definition", 0.95),
    # "Theorem 4.1 (Bayes). ..."
    (r"^(?:Theorem|Thm\.?)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "theorem", 0.97),
    # "Lemma 2.1. ..."
    (r"^(?:Lemma|Lem\.?)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "lemma", 0.95),
    # "Corollary 3.2. ..."
    (r"^(?:Corollary|Cor\.?)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "corollary", 0.93),
    # "Axiom 1.1. ..."
    (r"^(?:Axiom)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "axiom", 0.96),
    # "Proposition 2.4. ..."
    (r"^(?:Proposition|Prop\.?)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "theorem", 0.94),
    # "Algorithm 3.1 (QuickSort). ..."
    (r"^(?:Algorithm|Algo\.?)\s+[\d\.]+\.?\s*(?:\(([^)]+)\))?\s*[.\:]?\s*(.+)",
     "algorithm", 0.92),

    # ── AXLER STYLE (LADR): "1.33\ndefinition: subspace" ─────────────────────
    # Number + label on same block: "1.33\ndefinition: direct sum"
    (r"^[\d\.]+\s*\n+definition:\s*(.+)",
     "definition", 0.97),
    # "1.40\ntheorem title"  (theorem without body — body in next block)
    (r"^[\d\.]+\s*\n+(?:theorem|lemma|proposition|corollary):\s*(.+)",
     "theorem", 0.95),
    # "1.35\nexample: subspaces"
    (r"^[\d\.]+\s*\n+example:\s*(.+)",
     "definition", 0.80),
    # Just "definition: name" (without leading number)
    (r"^definition:\s+(.+)",
     "definition", 0.94),
    (r"^theorem:\s+(.+)",
     "theorem", 0.93),
    (r"^lemma:\s+(.+)",
     "lemma", 0.91),

    # ── COVER+THOMAS / INFORMATION THEORY STYLE ───────────────────────────────
    # "Definition X.Y (Channel Capacity): ..."
    (r"^Definition\s+[\d\.]+\s+\(([^)]+)\):\s*(.+)",
     "definition", 0.98),
    # "Theorem X.Y.Z (Shannon). ..."
    (r"^Theorem\s+[\d\.]+\s+\(([^)]+)\)[:\.]?\s*(.+)",
     "theorem", 0.97),

    # ── FEYNMAN / GRIFFITHS STYLE — physics narrative ─────────────────────────
    # "The [concept] is defined by the equation:"
    (r"^The\s+([a-z][a-z\s\-]+?)\s+is (?:defined by|given by|expressed as)\s+(.+)",
     "definition", 0.88),
    # "We define the [concept] as:"
    (r"^We define the\s+([a-z][a-z\s\-]+?)\s+(?:as|to be)\s+(.+)",
     "definition", 0.87),

    # ── KNUTH / SICP / ALGORITHM BOOKS ───────────────────────────────────────
    # Standard: "Algorithm X.Y (Name): ..."
    (r"^Algorithm\s+[\d\.]+\s+\(([^)]+)\)[:\.]?\s*(.+)",
     "algorithm", 0.93),
    # Knuth letter format: "Algorithm T. (Title). Given..." or "Algorithm T (Sort)."
    (r"^Algorithm\s+([A-Z])\.\s*(?:\(([^)]+)\)\.?\s*)?([A-Z].{10,})",
     "algorithm", 0.91),
    # Knuth step format: "T1. [Name]. ..." inside an algorithm
    # (captured only when name in parens is meaningful)
    (r"^([A-Z])\.\s+\(([A-Za-z][A-Za-z\s\-]+)\)\.\s+(.+)",
     "algorithm", 0.87),

    # ── REINFORCEMENT LEARNING / ML PROSE ────────────────────────────────────
    # "By a policy we mean ..." / "By X we mean Y"
    (r"^By\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z\s\-]{2,30}?)\s+we\s+mean\s+(.{20,})",
     "definition", 0.88),
    # "The value function v_π(s) is defined as ..."
    (r"^(?:The\s+)?([A-Z][a-zA-Z\s\-_]{2,30}?)\s+(?:function\s+)?is\s+defined\s+(?:as|to\s+be)\s+(.{20,})",
     "definition", 0.87),
    # "We use the term X to mean ..."
    (r"^We\s+use\s+the\s+term\s+([A-Za-z][a-z\s]+?)\s+to\s+mean\s+(.{15,})",
     "definition", 0.87),
    # "X, defined as Y" (inline definition)
    (r"^([A-Z][a-zA-Z\s\-]{3,25}?),\s+defined\s+as\s+(.{20,})",
     "definition", 0.86),

    # ── UNIX / ENGINEERING RULE FORMAT ────────────────────────────────────────
    # "Rule of Modularity: ..." / "Rule of Clarity: ..."
    (r"^Rule\s+of\s+([A-Z][a-z][\w\s]{2,25}?)[:\.\-]\s*(.{20,})",
     "definition", 0.91),
    # "Modularity: ..." (bare rule name without "Rule of")
    (r"^([A-Z][a-z][a-z\s]{3,20}?):\s+(The\s+.{20,}|Always\s+.{20,}|Never\s+.{20,})",
     "definition", 0.82),

    # ── PYTHON / PROGRAMMING PATTERN NAMES ────────────────────────────────────
    # Python class docstring: "class Foo:\n    '''A Foo is ...\n'''"
    (r"^class\s+([A-Z][a-zA-Z]+)\b.{0,50}\n\s*[\"']{3}([^\"']{20,200})[\"']{3}",
     "definition", 0.85),
    # "NOTE: X is ..." or "TIP: ..." in practical books (section highlights)
    (r"^(?:NOTE|TIP|INFO|IMPORTANT|KEY POINT)[:\s]+([A-Z][a-z].{20,100})",
     "definition", 0.78),
    # Python design pattern: "The [Pattern] Pattern ..."
    (r"^The\s+([A-Z][a-z][\w\s]+?)\s+[Pp]attern\s+(.{20,})",
     "definition", 0.85),

    # ── UNIVERSAL PATTERNS ────────────────────────────────────────────────────
    # "A [concept] is defined as ...\" or \"A [concept] is called ...\"
    (r"^A(?:n)?\s+([A-Z][a-z][\w\s\-]+?)\s+(?:is defined as|is called|is said to be|refers to|denotes)\s+(.+)",
     "definition", 0.88),
    # "We define X to be ..."
    (r"^(?:We define|Let us define|Define)\s+([A-Za-z\s]+?)\s+(?:to be|as|=)\s+(.+)",
     "definition", 0.87),
    # "X is called Y if ..."
    (r"^([A-Za-z][^\.\,]{2,40}?)\s+is (?:called|known as|referred to as)\s+([A-Z][^\.\,]{2,60})",
     "definition", 0.85),
]


_DEF_PATTERNS_COMPILED = [
    (re.compile(pat, re.DOTALL | re.IGNORECASE), ctype, conf)
    for pat, ctype, conf in _DEFINITION_PATTERNS
]

# Dependency signal: when a definition references another concept
# "requires that X", "using the notion of X", "recall that X is"
_DEPENDENCY_RE = re.compile(
    r"(?:"
    r"using\s+(?:the\s+)?(?:notion\s+of\s+|concept\s+of\s+)?([A-Z][a-z][\w\s]+)"
    r"|requires?\s+(?:that\s+)?([A-Z][a-z][\w\s]+)"
    r"|recall\s+that\s+([A-Z][a-z][\w\s]+)"
    r"|(?:follows\s+from|derived\s+from|consequence\s+of)\s+([A-Z][a-z][\w\s]+)"
    r")",
    re.I
)

# Domain assignment: NOT done by the book trainer.
# Books are tagged at the manifest level (book_trainer.py BOOK_MANIFEST).
# Fine-grained domain inference is the job of Clingo from KG topology,
# not keyword matching over definition text.


class ConceptExtractor:
    """
    Strictly extracts defined concepts from academic book pages.

    Only extracts what is EXPLICITLY marked as a Definition, Theorem,
    Lemma, Axiom, Algorithm, or uses explicit definitional language.
    """

    def extract_from_page(self, page: BookPage, domain: str, book_title: str) -> list[ExtractedConcept]:
        """
        Extract all explicitly defined concepts from a single book page.

        Handles two formats:
          1. Self-contained: definition label + body in ONE block
             "Definition 2.3. A metric space is a set X equipped with..."
          2. Multi-block (Axler, Feynman): label in one block, body in next
             Block 1: "1.33\ndefinition: subspace"
             Block 2: "A subset U of V is called a subspace..."
        """
        concepts: list[ExtractedConcept] = []
        full_page_text = page.raw_text
        blocks = page.blocks

        for bidx, block in enumerate(blocks):
            text = block.text.strip()
            if len(text) < 8:
                continue

            # Try each definition pattern
            for pattern, ctype, base_conf in _DEF_PATTERNS_COMPILED:
                m = pattern.match(text)
                if not m:
                    continue

                # Parse name and body from match
                name, body = self._parse_match(m, text, ctype)
                if not name:
                    continue

                # MULTI-BLOCK LOOKAHEAD: if body is too short, grab next block
                # (Axler: "1.33\ndefinition: subspace" → body is in next block)
                if len(body) < 40 and bidx + 1 < len(blocks):
                    next_block_text = blocks[bidx + 1].text.strip()
                    # Only grab next block if it's not another definition label
                    is_next_label = any(
                        p.match(next_block_text)
                        for p, _, _ in _DEF_PATTERNS_COMPILED
                    )
                    if not is_next_label and len(next_block_text) > 20:
                        body = next_block_text
                        # Also try block after that if body still short
                        if len(body) < 60 and bidx + 2 < len(blocks):
                            body += " " + blocks[bidx + 2].text.strip()[:200]

                if len(body) < 15:
                    continue

                # Extend body from full page text if still short
                body = self._extend_body(body, full_page_text)

                concept_id       = self._make_id(name)
                if not concept_id:
                    continue   # garbage name — skip

                # Domain is NOT inferred from the definition text.
                # The book trainer's job is extraction — not domain classification.
                # Domain comes from the book manifest (the caller) and is treated
                # as a starting point. Final domain inference happens via Clingo
                # from KG topology (REQUIRES/ENABLES edges), not keyword matching.
                formulas         = _formula_ex.extract_from_text(body, context=name)
                depends_on       = self._extract_dependencies(body)
                is_immutable     = ctype in ("theorem", "axiom", "lemma")

                concept = ExtractedConcept(
                    concept_id   = concept_id,
                    name         = name.strip(),
                    definition   = body.strip(),
                    formulas     = formulas,
                    domain       = domain,           # straight from book manifest
                    source_book  = book_title,
                    source_page  = page.page_num,
                    chapter      = page.chapter,
                    section      = page.section,
                    concept_type = ctype,
                    depends_on   = depends_on,
                    confidence   = base_conf if block.is_bold else base_conf - 0.05,
                    immutable    = is_immutable,
                    raw_block    = text[:300],
                )
                concepts.append(concept)
                break  # one concept per block

        return concepts

    def extract_from_book_pages(
        self,
        pages: list[BookPage],
        domain: str,
        book_title: str,
    ) -> list[ExtractedConcept]:
        """Extract concepts from all pages of a book."""
        all_concepts: list[ExtractedConcept] = []
        seen_ids: set[str] = set()

        for page in pages:
            page_concepts = self.extract_from_page(page, domain, book_title)
            for c in page_concepts:
                if c.concept_id not in seen_ids:
                    seen_ids.add(c.concept_id)
                    all_concepts.append(c)
                else:
                    # Merge: update definition if longer
                    existing = next(x for x in all_concepts if x.concept_id == c.concept_id)
                    if len(c.definition) > len(existing.definition):
                        existing.definition = c.definition
                        existing.formulas.extend(c.formulas)

        log.info(
            "ConceptExtractor: %s → %d concepts from %d pages",
            book_title, len(all_concepts), len(pages)
        )
        return all_concepts

    # ── Private ────────────────────────────────────────────────────────────────

    def _parse_match(self, m: re.Match, text: str, ctype: str) -> tuple[str, str]:
        """Parse name and body from a regex match."""
        groups = [g for g in m.groups() if g]
        if not groups:
            return "", text

        if ctype in ("theorem", "lemma", "corollary", "axiom"):
            # First group is often the theorem name (in parentheses)
            # Body follows
            if len(groups) >= 2:
                name = groups[0].strip()
                body = groups[-1].strip()
            else:
                # Infer name from first noun phrase in body
                body = groups[0].strip()
                name = self._extract_name_from_body(body, ctype)
        else:
            # Definition: name is the thing being defined
            if len(groups) >= 2:
                name = groups[0].strip()
                body = groups[1].strip()
            else:
                body = groups[0].strip()
                name = self._extract_name_from_body(body, ctype)

        return name, body

    def _extract_name_from_body(self, body: str, ctype: str) -> str:
        """Guess the name of a concept from its definition body."""
        # "A metric space is..." → "metric space"
        m = re.match(r"^(?:A|An|The)\s+([A-Z][a-z][\w\s\-]+?)\s+(?:is|are|denotes)\s", body, re.I)
        if m:
            return m.group(1).strip()
        # "We call X the Y" → "Y"
        m = re.match(r"^(?:We call|Called)\s+.+?\s+the\s+([A-Z][a-z][\w\s\-]+)", body, re.I)
        if m:
            return m.group(1).strip()
        # Fallback: take first few words
        words = body.split()[:4]
        return " ".join(words)

    def _extend_body(self, body: str, full_page_text: str) -> str:
        """
        If the definition body seems truncated (< 60 chars),
        try to find the continuation in the full page text.
        """
        if len(body) > 60:
            return body
        idx = full_page_text.find(body[:30])
        if idx == -1:
            return body
        # Take up to 400 chars from that point
        extended = full_page_text[idx:idx+400].strip()
        # Stop at double newline (paragraph break)
        para_end = extended.find("\n\n")
        if para_end > len(body):
            return extended[:para_end].strip()
        return extended.strip() or body

    def _make_id(self, name: str) -> str | None:
        """
        Convert a concept name to a snake_case KG ID.
        Returns None if the name is clearly garbage (sentence fragment, stopwords).
        """
        # Normalise
        clean = re.sub(r"[^a-z0-9\s\-]", "", name.lower())
        clean = re.sub(r"[\s\-]+", "_", clean.strip())
        clean = re.sub(r"_+", "_", clean).strip("_")

        if not clean:
            return None

        tokens = clean.split("_")

        # Reject: too many tokens (sentence fragment)
        if len(tokens) > 4:
            return None

        # Reject: starts with a stopword
        _STOP = {
            "we","if","for","the","a","an","and","or","is","it","this","that",
            "in","of","to","as","at","by","on","with","from","be","are","was",
            "were","let","so","but","then","when","which","where","since",
            "while","hence","thus","therefore","note","recall","observe",
            "suppose","consider","assume","assuming","implies","given",
            "using","another","other","all","some","any","not",
        }
        if tokens[0] in _STOP:
            return None

        # Reject: purely numeric
        if clean.replace("_", "").isdigit():
            return None

        # Reject: trivially short (single char)
        if len(clean) <= 2:
            return None

        return clean[:60]


    def _extract_dependencies(self, text: str) -> list[str]:
        """
        Find concepts that this definition explicitly depends on.
        Returns a list of candidate concept IDs.
        """
        deps: list[str] = []
        for m in _DEPENDENCY_RE.finditer(text):
            for g in m.groups():
                if g:
                    dep_id = re.sub(r"[^a-z0-9\s]", "", g.lower()).strip()
                    dep_id = re.sub(r"\s+", "_", dep_id)
                    if dep_id:
                        deps.append(dep_id)
        return list(set(deps))
