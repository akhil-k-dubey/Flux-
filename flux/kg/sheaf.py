"""
flux/kg/sheaf.py — Sheaf Theory over the KG

At 2071+ concepts across 19 domains, the KG is a distributed knowledge space.
The same word ("entropy", "field", "normal", "type") lives in multiple domains
simultaneously with different — but related — meanings.

Sheaf theory is the right mathematical framework:

  - Domains are open sets covering the knowledge space
  - Concepts are LOCAL SECTIONS over those open sets
  - ANALOGOUS_TO edges are RESTRICTION MAPS between domains
  - Global understanding = GLUING consistent local sections
  - Knowledge gaps = H¹ cohomology — obstructions to gluing

This replaces string matching. ConceptGrounder asks the sheaf:
  "Given word W in domain context D, which concept does W refer to?"
The sheaf answers using domain-aware section lookup, not flat ID matching.

Reference: Curry (2014) "Sheaves on Graphs" — applied here to KGs.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class LocalSection:
    """
    A concept as understood within one domain.
    In sheaf language: a section of the sheaf over the open set 'domain'.
    """
    concept_id:  str
    domain:      str
    definition:  str
    confidence:  float
    edges_out:   list[dict] = field(default_factory=list)   # outgoing edges in this domain
    cross_edges: list[dict] = field(default_factory=list)   # ANALOGOUS_TO to other domains
    keywords:    list[str]  = field(default_factory=list)   # words that resolve to this section


@dataclass
class RestrictionMap:
    """
    How a concept in domain_A is seen from domain_B.
    In sheaf language: the restriction map ρ_{AB}: F(A) → F(B).
    These come from ANALOGOUS_TO edges in the KG.
    """
    from_concept: str
    from_domain:  str
    to_concept:   str
    to_domain:    str
    edge_type:    str    # ANALOGOUS_TO, CONNECTS, GENERALIZES, etc.
    weight:       float  = 1.0
    reason:       str    = ""


@dataclass
class GlobalSection:
    """
    A concept as understood across ALL domains simultaneously.
    In sheaf language: a global section of the sheaf — exists when local sections
    are CONSISTENT across domain boundaries (can be glued).

    consistency_score = 1.0  → perfect sheaf (sections agree everywhere)
    consistency_score < 0.9  → H¹ obstruction (genuine knowledge gap)
    """
    concept_id:          str
    canonical_id:        str                        # the "primary" concept ID
    local_sections:      dict[str, LocalSection]    # domain → section
    restriction_maps:    list[RestrictionMap]
    consistency_score:   float
    canonical_definition: str                       # synthesized from all sections
    all_keywords:        list[str]                  # all words that resolve to this concept


@dataclass
class CohomologyGap:
    """
    H¹ gap — an obstruction where local sections DON'T glue.
    These are the mathematical representation of "Flux's knowledge gaps."
    """
    concept_pair:  tuple[str, str]    # (concept_A, concept_B) that should connect but don't
    domain_pair:   tuple[str, str]    # (domain_A, domain_B)
    gap_type:      str                # "missing_restriction", "inconsistent", "isolated"
    description:   str


# ── Main sheaf class ───────────────────────────────────────────────────────────

class KGSheaf:
    """
    A sheaf of concepts over the knowledge domain topology.

    The KG node set is the base space.
    Domains are the open cover.
    Concept nodes are sections over their domains.
    ANALOGOUS_TO edges are restriction maps between domain opens.

    Usage:
        sheaf = KGSheaf(store)
        sheaf.build()   # one-time construction (cached)

        # Resolve "entropy" in info theory context
        concept_id = sheaf.resolve_in_context("entropy", "information_theory")
        → "information_entropy"

        # Get all sections for a word
        sections = sheaf.find_sections_for_word("entropy")
        → [LocalSection("entropy", "thermodynamics"), LocalSection("information_entropy", "information_theory")]

        # Knowledge gaps
        gaps = sheaf.h1_gaps()
        → [CohomologyGap(("entropy", "gradient_descent"), ...)]
    """

    def __init__(self, store):
        self.store = store
        self._sections:     dict[str, LocalSection]    = {}  # concept_id → section
        self._globals:      dict[str, GlobalSection]   = {}  # canonical_id → global section
        self._restrictions: list[RestrictionMap]       = []
        self._word_index:   dict[str, list[str]]       = {}  # word → [concept_ids]
        self._built = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def build(self) -> None:
        """
        Construct the sheaf from the KGStore.
        Called once at startup, then cached.
        """
        if self._built:
            return
        log.info("KGSheaf: building sheaf over %d concepts...", len(self.store.all_ids()))
        self._build_sections()
        self._build_restriction_maps()
        self._build_word_index()
        self._build_global_sections()
        self._built = True
        log.info(
            "KGSheaf: built %d local sections, %d restriction maps, %d global sections",
            len(self._sections), len(self._restrictions), len(self._globals)
        )

    def section(self, concept_id: str) -> Optional[LocalSection]:
        """Get the local section for a concept."""
        self._ensure_built()
        return self._sections.get(concept_id)

    def global_section(self, concept_id: str) -> Optional[GlobalSection]:
        """Get the global (multi-domain) view of a concept."""
        self._ensure_built()
        return self._globals.get(concept_id)

    def find_sections_for_word(self, word: str) -> list[LocalSection]:
        """
        Find all KG concepts that could be referred to by this word.
        Uses the word index built from concept IDs, keywords, and definitions.

        This replaces n-gram string matching against flat ID list.
        Returns sections ordered by relevance (exact match first).
        """
        self._ensure_built()
        word_clean = self._normalize(word)
        if not word_clean:
            return []

        matches: list[tuple[float, LocalSection]] = []

        # Check word index (built from concept ID tokens + definition keywords)
        candidate_ids = set()
        for token in word_clean.split():
            if len(token) < 3:
                continue
            for cid in self._word_index.get(token, []):
                candidate_ids.add(cid)

        # Also try multi-word as underscore ID
        underscore_id = word_clean.replace(" ", "_")
        if underscore_id in self._sections:
            candidate_ids.add(underscore_id)

        for cid in candidate_ids:
            sec = self._sections.get(cid)
            if not sec:
                continue
            score = self._relevance_score(word_clean, sec)
            if score > 0.3:
                matches.append((score, sec))

        matches.sort(key=lambda x: -x[0])
        return [sec for _, sec in matches]

    def resolve_in_context(
        self,
        word: str,
        domain_context: Optional[str] = None,
        akhil_domain_history: Optional[list[str]] = None,
    ) -> Optional[str]:
        """
        Resolve an ambiguous word to a specific KG concept ID.
        Uses domain context to disambiguate (sheaf restriction).

        Parameters
        ----------
        word           : raw word or phrase from the query
        domain_context : domain inferred from the query (e.g. "information_theory")
        akhil_domain_history : Akhil's recent domain activity (personalizes resolution)

        Returns concept_id or None if no match / protected name.
        """
        self._ensure_built()
        sections = self.find_sections_for_word(word)
        if not sections:
            return None

        # Only one match — return it directly
        if len(sections) == 1:
            return sections[0].concept_id

        # Near-exact name match (score ≥ 0.95) wins unconditionally.
        # If the slot IS the concept's name, no domain context should override it.
        # Example: "entropy" → score 1.0 under `entropy` — always wins.
        # This prevents "entropy" resolving to "entropy_power_inequality_if"
        # (score 0.82) just because domain_context=information_theory.
        top_score = self._relevance_score(word, sections[0])
        if top_score >= 0.95:
            return sections[0].concept_id

        # Multiple matches — use domain context to pick the right section
        if domain_context:
            for sec in sections:
                if sec.domain == domain_context:
                    return sec.concept_id
            # Try restriction maps: can we reach domain_context from this concept?
            for sec in sections:
                for rmap in self._restrictions:
                    if rmap.from_concept == sec.concept_id and rmap.to_domain == domain_context:
                        return rmap.to_concept

        # Use Akhil's recent domain history to prefer familiar domains
        if akhil_domain_history:
            for domain in reversed(akhil_domain_history):
                for sec in sections:
                    if sec.domain == domain:
                        return sec.concept_id

        # Default: highest-confidence section
        return sections[0].concept_id


    def restrict(self, concept_id: str, to_domain: str) -> Optional[str]:
        """
        Apply restriction map: view concept_id from to_domain's perspective.
        Returns the concept ID of the restricted section, or None.
        """
        self._ensure_built()
        for rmap in self._restrictions:
            if rmap.from_concept == concept_id and rmap.to_domain == to_domain:
                return rmap.to_concept
        return None

    def h1_gaps(self) -> list[CohomologyGap]:
        """
        Compute H¹ cohomology — the knowledge gaps.

        A gap exists when:
        1. Two concepts should be connected (same word in different domains) but aren't
        2. A concept is isolated (no edges to other domains) — cohomologically trivial but practically a gap
        3. Restriction maps are inconsistent across a triangle of domains

        These are the places Flux needs to learn more.
        """
        self._ensure_built()
        gaps: list[CohomologyGap] = []

        # Find isolated concepts (no cross-domain edges)
        for cid, sec in self._sections.items():
            if not sec.cross_edges and sec.domain != "unknown":
                gaps.append(CohomologyGap(
                    concept_pair = (cid, ""),
                    domain_pair  = (sec.domain, ""),
                    gap_type     = "isolated",
                    description  = (
                        f"'{cid}' (domain: {sec.domain}) has no connections to other domains. "
                        f"Stage 7 research target."
                    ),
                ))

        # Find concepts that share keywords but have no restriction map (should be connected)
        word_to_concepts: dict[str, list[str]] = {}
        for cid, sec in self._sections.items():
            for kw in sec.keywords[:3]:  # top 3 keywords
                word_to_concepts.setdefault(kw, []).append(cid)

        for word, concepts in word_to_concepts.items():
            if len(concepts) <= 1:
                continue
            # Multiple concepts share this keyword — should they be connected?
            for i in range(len(concepts)):
                for j in range(i + 1, len(concepts)):
                    ca, cb = concepts[i], concepts[j]
                    sec_a = self._sections[ca]
                    sec_b = self._sections[cb]
                    if sec_a.domain == sec_b.domain:
                        continue  # same domain — not a gap
                    # Check if they're connected by a restriction map
                    connected = any(
                        (r.from_concept == ca and r.to_concept == cb) or
                        (r.from_concept == cb and r.to_concept == ca)
                        for r in self._restrictions
                    )
                    if not connected:
                        gaps.append(CohomologyGap(
                            concept_pair = (ca, cb),
                            domain_pair  = (sec_a.domain, sec_b.domain),
                            gap_type     = "missing_restriction",
                            description  = (
                                f"'{ca}' ({sec_a.domain}) and '{cb}' ({sec_b.domain}) "
                                f"share keyword '{word}' but have no restriction map between them."
                            ),
                        ))

        return gaps[:50]  # top 50 gaps

    def domain_vocabulary(self, domain: str) -> list[str]:
        """Return all concept IDs in a given domain."""
        self._ensure_built()
        return [cid for cid, sec in self._sections.items() if sec.domain == domain]

    def all_domains(self) -> list[str]:
        """Return all domains in the sheaf."""
        self._ensure_built()
        return list({sec.domain for sec in self._sections.values()})

    # ── Build internals ────────────────────────────────────────────────────────

    def _build_sections(self) -> None:
        for cid in self.store.all_ids():
            node = self.store.get(cid)
            if not node:
                continue
            domain    = node.get("domain", "unknown")
            edges_out = self.store.edges_from(cid) or []
            cross     = [e for e in edges_out
                         if e.get("type") in ("ANALOGOUS_TO", "CONNECTS", "GENERALIZES")]

            # Extract keywords from concept ID and definition
            keywords = self._extract_keywords(cid, node.get("definition", ""))

            self._sections[cid] = LocalSection(
                concept_id  = cid,
                domain      = domain,
                definition  = node.get("definition", ""),
                confidence  = float(node.get("confidence", 0.8)),
                edges_out   = edges_out,
                cross_edges = cross,
                keywords    = keywords,
            )

    def _build_restriction_maps(self) -> None:
        for cid, sec in self._sections.items():
            for edge in sec.cross_edges:
                to_id   = edge.get("to", "")
                to_sec  = self._sections.get(to_id)
                if not to_sec or to_sec.domain == sec.domain:
                    continue
                self._restrictions.append(RestrictionMap(
                    from_concept = cid,
                    from_domain  = sec.domain,
                    to_concept   = to_id,
                    to_domain    = to_sec.domain,
                    edge_type    = edge.get("type", "ANALOGOUS_TO"),
                    weight       = float(edge.get("weight", 1.0)),
                    reason       = edge.get("reason", ""),
                ))

    def _build_word_index(self) -> None:
        """Build inverted index: word → [concept_ids that contain this word]."""
        for cid, sec in self._sections.items():
            # Index from concept ID tokens
            for token in cid.split("_"):
                if len(token) >= 3:
                    self._word_index.setdefault(token, []).append(cid)
            # Index from definition keywords
            for kw in sec.keywords:
                for token in kw.split():
                    if len(token) >= 3:
                        self._word_index.setdefault(token, []).append(cid)

        # Deduplicate
        for word in self._word_index:
            self._word_index[word] = list(dict.fromkeys(self._word_index[word]))

    def _build_global_sections(self) -> None:
        """
        Build global sections by grouping concepts connected via restriction maps.
        Concepts that are ANALOGOUS_TO each other form a global section cluster.
        """
        # Union-Find over restriction map graph
        parent: dict[str, str] = {cid: cid for cid in self._sections}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for rmap in self._restrictions:
            if rmap.edge_type == "ANALOGOUS_TO" and rmap.weight >= 0.8:
                if rmap.from_concept in parent and rmap.to_concept in parent:
                    union(rmap.from_concept, rmap.to_concept)

        # Group by root
        clusters: dict[str, list[str]] = {}
        for cid in self._sections:
            root = find(cid)
            clusters.setdefault(root, []).append(cid)

        # Build global sections for clusters with >1 concept
        for root, members in clusters.items():
            local_secs = {
                cid: self._sections[cid]
                for cid in members
                if cid in self._sections
            }
            if len(local_secs) < 2:
                continue

            # Consistency: do all members share common keywords?
            all_kws   = [set(sec.keywords) for sec in local_secs.values()]
            shared_kws = all_kws[0].intersection(*all_kws[1:]) if len(all_kws) > 1 else all_kws[0]
            consistency = len(shared_kws) / max(len(all_kws[0]), 1) if all_kws else 0.0
            consistency = min(1.0, consistency * 2)  # scale

            # Canonical definition: use highest-confidence section's definition
            best_sec = max(local_secs.values(), key=lambda s: s.confidence)

            all_kws_flat = list({kw for sec in local_secs.values() for kw in sec.keywords})

            self._globals[root] = GlobalSection(
                concept_id           = root,
                canonical_id         = best_sec.concept_id,
                local_sections       = local_secs,
                restriction_maps     = [
                    r for r in self._restrictions
                    if r.from_concept in members or r.to_concept in members
                ],
                consistency_score    = consistency,
                canonical_definition = best_sec.definition,
                all_keywords         = all_kws_flat,
            )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _extract_keywords(self, concept_id: str, definition: str) -> list[str]:
        """Extract meaningful keywords from concept ID and definition.

        ID tokens are always indexed.
        Definition words are indexed only if they are likely technical terms
        (not common English words that appear in descriptive sentences).
        """
        keywords: list[str] = []
        # From concept ID — always trusted
        tokens = concept_id.split("_")
        keywords.extend(t for t in tokens if len(t) >= 3)
        # Natural name from ID (e.g. "singular values" from "singular_values")
        if tokens:
            keywords.append(" ".join(tokens))

        # From definition: only index if the word looks like a technical term.
        # We skip common English words that appear in explanatory sentences.
        if definition:
            _DEF_STOP = {
                "that", "this", "with", "from", "have", "been", "will", "also",
                "which", "they", "their", "when", "each", "such", "used", "more",
                "than", "into", "some", "then", "here", "what", "where", "called",
                "given", "defined", "denoted", "where", "note", "since", "thus",
                "hence", "follows", "consider", "suppose", "assume", "let",
                "show", "proof", "theorem", "lemma", "example", "case",
                # Common adjectives that appear in definitions but aren't technical:
                "dynamic", "static", "basic", "simple", "general", "specific",
                "current", "multiple", "various", "different", "similar", "total",
                "local", "global", "single", "unique", "special", "standard",
                "across", "between", "within", "without", "about", "above",
                "below", "after", "before", "under", "over", "both", "either",
                "through", "along", "using", "based", "given", "defined",
                "known", "called", "written", "equal", "greater", "less",
                "small", "large", "high", "lower", "upper", "first", "second",
                "third", "order", "number", "value", "values", "point", "points",
                "form", "forms", "space", "spaces", "sets", "elements", "element",
                "function", "functions", "vector", "vectors", "matrix", "matrices",
            }
            words = re.findall(r"[a-zA-Z]{5,}", definition[:300])
            for w in words[:6]:
                wl = w.lower()
                if wl not in _DEF_STOP:
                    keywords.append(wl)

        return list(dict.fromkeys(keywords))[:12]

    def _relevance_score(self, word: str, section: LocalSection) -> float:
        """Score how well a word matches a section.

        Key insight: clean short concept IDs (entropy, eigenvalue, functor)
        should always beat noisy long IDs from book extraction
        (in_the_machine_learning_literature_f, using_linear_algebra_to).
        """
        word_clean = word.replace(" ", "_")
        cid        = section.concept_id
        cid_tokens = cid.split("_")
        n_tokens   = len(cid_tokens)

        # Noise penalty: long IDs from book extraction are likely sentence fragments
        # A clean concept ID has ≤3 tokens; 5+ tokens = high noise probability
        noise_penalty = max(0.0, (n_tokens - 3) * 0.08)  # -0.08 per extra token

        # Exact concept_id match — highest possible
        if word_clean == cid:
            return max(0.0, 1.0 - noise_penalty)

        # Concept_id starts with word (good prefix match)
        if cid.startswith(word_clean + "_") or cid == word_clean:
            return max(0.0, 0.9 - noise_penalty)

        # Word is substring of concept_id
        if word_clean in cid:
            return max(0.0, 0.75 - noise_penalty)

        # Word in keywords
        word_natural = word.lower()
        if word_natural in section.keywords:
            return max(0.0, 0.8 - noise_penalty)
        if any(word_natural in kw for kw in section.keywords):
            return max(0.0, 0.6 - noise_penalty)

        # Partial token match — all query tokens appear in concept ID tokens
        word_tokens = set(word.lower().split())
        id_set      = set(cid_tokens)
        overlap     = len(word_tokens & id_set) / max(len(word_tokens), 1)
        if overlap > 0.5:
            return max(0.0, overlap * 0.7 - noise_penalty)

        return 0.0

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^a-z0-9 _]", "", text.lower().strip())

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()
