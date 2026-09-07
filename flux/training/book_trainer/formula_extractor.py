"""
flux/training/book_trainer/formula_extractor.py

Extracts and normalises mathematical formulas from book text.

Key insight: formulas in PDFs are either:
  1. Inline: embedded in sentences "where S = k_B ln(Ω) is the entropy"
  2. Display: on their own line, centered, often with equation number

For KG storage, we want:
  - The formula itself (as clean text/LaTeX)
  - What each symbol means (links to other KG concepts)
  - What the formula expresses (its semantic label)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ExtractedFormula:
    """A single mathematical formula with context."""
    raw:          str                        # raw text as extracted
    cleaned:      str                        # cleaned/normalised form
    label:        str                        # semantic label e.g. "entropy formula"
    symbols:      dict[str, str]             # symbol → meaning e.g. {"S": "entropy"}
    context:      str                        # surrounding sentence
    formula_type: str                        # "inline" | "display" | "definition"


# ── Symbol vocabulary ─────────────────────────────────────────────────────────
# Maps common mathematical symbols to their semantic meaning.
# This is the bridge between formula and concept.
_SYMBOL_MAP: dict[str, str] = {
    # Information theory
    r"\bH\b":              "information_entropy",
    r"\bH\(X\)":           "information_entropy",
    r"\bH\(X\|Y\)":        "conditional_entropy",
    r"\bI\(X;Y\)":         "mutual_information",
    r"\bD_\{?KL\}?":       "kl_divergence",
    r"\bC\b":              "channel_capacity",
    # Thermodynamics
    r"\bS\b":              "entropy",
    r"\bk_B\b|\bk_b\b":    "boltzmann_constant",
    r"\b\\Omega\b|\bΩ\b":  "microstate_count",
    r"\bT\b":              "temperature",
    r"\bF\b":              "helmholtz_free_energy",
    r"\bG\b":              "gibbs_free_energy",
    r"\bU\b":              "internal_energy",
    r"\bZ\b":              "partition_function",
    r"\bP\b":              "pressure",
    # Quantum mechanics
    r"\b\\psi\b|\bψ\b":    "wavefunction",
    r"\b\\Psi\b|\bΨ\b":    "wavefunction",
    r"\b\\hbar\b|\bℏ\b":   "reduced_planck_constant",
    r"\b\\hat\{H\}":       "hamiltonian_operator",
    r"\b\\nabla\b|\b∇\b":  "gradient_operator",
    r"\b\\rho\b|\bρ\b":    "density_matrix",
    # Machine learning
    r"\bL\b|\b\\mathcal\{L\}": "loss_function",
    r"\b\\theta\b|\bθ\b":  "model_parameters",
    r"\b\\nabla_\\theta":  "gradient_descent",
    r"\bp\(x\)":           "probability_distribution",
    r"\bKL\b":             "kl_divergence",
    # Linear algebra
    r"\bA\b":              "matrix",
    r"\b\\lambda\b|\bλ\b": "eigenvalue",
    r"\bv\b":              "eigenvector",
    r"\b\\sigma\b|\bσ\b":  "singular_value",
    r"\bI\b":              "identity_matrix",
    # Calculus / analysis
    r"\b\\int\b":          "integral",
    r"\b\\sum\b":          "summation",
    r"\b\\prod\b":         "product",
    r"\b\\lim\b":          "limit",
    r"\b\\partial\b|\b∂\b": "partial_derivative",
    r"\b\\infty\b|\b∞\b":  "infinity",
    # Probability
    r"\bE\[":              "expectation",
    r"\bVar\b":            "variance",
    r"\bCov\b":            "covariance",
    r"\bP\(":              "probability",
}

_SYMBOL_COMPILED = {re.compile(pat, re.I): meaning
                    for pat, meaning in _SYMBOL_MAP.items()}

# ── Formula detection patterns ────────────────────────────────────────────────

# Display formulas: lines that are ONLY mathematical content
_DISPLAY_FORMULA_RE = re.compile(
    r"^[\s]*"                                 # optional leading space
    r"(?:"
    r"[A-Za-z0-9\(\)\[\]\{\}\+\-\*\/\=\^\|\\σλθψρ∇∂∞∑∫≤≥≠≈∝∈∉⊂⊃∪∩]"  # math chars
    r"[\s,\.]*"
    r")+"
    r"(?:\s*\(\d+[\.\d]*\))?$",               # optional equation number (2.3)
    re.MULTILINE
)

# Inline formulas: mathematical expressions embedded in text
_INLINE_FORMULA_RE = re.compile(
    r"(?<!\w)"                                # not preceded by word char
    r"([A-Za-z]\s*=\s*"                       # "S = ..." or "H(X) = ..."
    r"[^,;\.]{3,80})"                         # formula body
    r"(?=[,;\.)]|\s+(?:is|are|where|for|when|denotes|represents))",
    re.I
)

# "where X = Y" pattern (symbol definitions)
_WHERE_PATTERN_RE = re.compile(
    r"where\s+([A-Za-z_\\][A-Za-z0-9_\\]*)\s+(?:=|is|denotes|represents)\s+([^,;\.]{3,80})",
    re.I
)

# Definition/formula introduction keywords
_FORMULA_INTRO_RE = re.compile(
    r"(?:defined as|given by|expressed as|equal to|formula|equation|"
    r"satisfies|obeys|follows from|we have|it follows that)\s*:?\s*",
    re.I
)


class FormulaExtractor:
    """
    Extracts mathematical formulas from page/block text.
    Produces ExtractedFormula objects with symbol-to-concept mappings.
    """

    def extract_from_text(self, text: str, context: str = "") -> list[ExtractedFormula]:
        """
        Extract all formulas from a text block.

        Parameters
        ----------
        text    : the text block to scan
        context : surrounding text for labelling

        Returns
        -------
        list of ExtractedFormula
        """
        formulas: list[ExtractedFormula] = []

        # 1. Look for display formulas (isolated lines)
        for line in text.split("\n"):
            line_stripped = line.strip()
            if self._looks_like_formula(line_stripped):
                f = self._build_formula(line_stripped, context, "display")
                if f:
                    formulas.append(f)

        # 2. Look for inline "X = ..." patterns
        for m in _INLINE_FORMULA_RE.finditer(text):
            raw = m.group(1).strip()
            if len(raw) > 3 and "=" in raw:
                f = self._build_formula(raw, context, "inline")
                if f:
                    formulas.append(f)

        # 3. Extract "where X = ..." symbol definitions
        for m in _WHERE_PATTERN_RE.finditer(text):
            symbol  = m.group(1).strip()
            meaning = m.group(2).strip()
            # Turn this into a mini formula
            raw = f"{symbol} = {meaning}"
            f = self._build_formula(raw, context, "definition")
            if f:
                formulas.append(f)

        # Deduplicate by raw formula text
        seen: set[str] = set()
        unique: list[ExtractedFormula] = []
        for f in formulas:
            key = f.cleaned[:40]
            if key not in seen:
                seen.add(key)
                unique.append(f)

        return unique

    def extract_symbols(self, text: str) -> dict[str, str]:
        """
        Extract symbol → concept_id mappings from any text.
        Used to generate KG edge hints.
        """
        found: dict[str, str] = {}
        for pattern, concept_id in _SYMBOL_COMPILED.items():
            if pattern.search(text):
                found[concept_id] = concept_id   # concept → concept
        return found

    def clean_formula(self, raw: str) -> str:
        """Normalise a formula string for storage."""
        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", raw).strip()
        # Remove PDF artifacts (ligatures, etc.)
        cleaned = cleaned.replace("ﬁ", "fi").replace("ﬂ", "fl")
        # Normalise common Unicode math chars
        cleaned = (cleaned
                   .replace("−", "-")
                   .replace("×", "*")
                   .replace("÷", "/")
                   .replace("≤", "<=")
                   .replace("≥", ">=")
                   .replace("≠", "!=")
                   .replace("≈", "~")
                   .replace("∞", "inf"))
        return cleaned

    # ── Private ───────────────────────────────────────────────────────────────

    def _looks_like_formula(self, text: str) -> bool:
        """Quick check: does this text look like a mathematical formula?"""
        if len(text) < 3 or len(text) > 200:
            return False
        # Must contain at least one math signal
        math_chars = set("=+-*/^∑∫∂∇ΩψΨθλσρ")
        math_words = {"log", "ln", "exp", "sin", "cos", "max", "min", "lim", "sum"}
        has_math = (
            any(c in text for c in math_chars)
            or any(w in text.lower() for w in math_words)
        )
        # Should not be mostly plain English
        word_count = len(re.findall(r"[a-z]{4,}", text.lower()))
        total_tokens = len(text.split())
        if total_tokens > 0 and word_count / total_tokens > 0.7:
            return False
        return has_math

    def _build_formula(self, raw: str, context: str, ftype: str) -> ExtractedFormula | None:
        """Build a formula object from a raw string."""
        cleaned = self.clean_formula(raw)
        if len(cleaned) < 3:
            return None

        # Extract symbols referenced
        symbols = {}
        for pattern, concept_id in _SYMBOL_COMPILED.items():
            if pattern.search(raw):
                # Guess the symbol letter
                m = pattern.search(raw)
                if m:
                    sym = m.group(0).strip()[:4]
                    symbols[sym] = concept_id

        # Generate a label from context
        label = self._infer_label(raw, context)

        return ExtractedFormula(
            raw          = raw,
            cleaned      = cleaned,
            label        = label,
            symbols      = symbols,
            context      = context[:120],
            formula_type = ftype,
        )

    def _infer_label(self, formula: str, context: str) -> str:
        """Guess what a formula expresses based on context."""
        ctx_lower = (formula + " " + context).lower()

        labels = [
            ("entropy formula",         ["entropy", "k_b", "ln(omega", "s ="]),
            ("information entropy",     ["h(x", "h(p", "log p", "-sum", "shannon"]),
            ("mutual information",      ["i(x;y", "mutual", "h(x) - h(x|y"]),
            ("channel capacity",        ["capacity", "max", "mutual information"]),
            ("free energy",             ["free energy", "helmholtz", "gibbs", "f ="]),
            ("partition function",      ["partition", "z =", "z="]),
            ("boltzmann distribution",  ["boltzmann", "e^{-", "exp(-beta"]),
            ("schrodinger equation",    ["hbar", "d/dt", "h psi", "ih"]),
            ("loss function",           ["loss", "l =", "minimize", "gradient"]),
            ("bayes theorem",           ["p(a|b", "bayes", "posterior", "prior"]),
            ("gradient descent",        ["theta", "gradient", "learning rate", "nabla"]),
            ("taylor expansion",        ["taylor", "f(x+", "nth order"]),
            ("fourier transform",       ["fourier", "integral", "e^{iwt", "frequency"]),
            ("eigenvalue equation",     ["eigenvalue", "av =", "lambda v"]),
        ]
        for label, keywords in labels:
            if any(kw in ctx_lower for kw in keywords):
                return label

        # Fallback: extract LHS of equation
        m = re.match(r"^([A-Za-z_\\][A-Za-z0-9_\\()\s]*)\s*=", formula)
        if m:
            return f"{m.group(1).strip()} equation"

        return "mathematical expression"
