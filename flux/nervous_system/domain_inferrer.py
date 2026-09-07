"""
flux/nervous_system/domain_inferrer.py

Infers domain labels for every KG node from definition text + concept ID.
No training — pure keyword/pattern matching across 15 domains.

Runs once at startup (or on demand). Batch-writes domain field into each KG
node and updates WorldModel knowledge.domains.

Call:
    from flux.nervous_system.domain_inferrer import infer_and_patch
    count = infer_and_patch(store, world_model)
    # → writes domain to every node, returns number updated
"""
from __future__ import annotations

import re
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Domain keyword rules ───────────────────────────────────────────────────────
# Each domain is a list of (regex_pattern, weight) tuples.
# The domain with the highest total weight wins.
# Weights ≥ 2 are "strong" signals (concept ID prefix or key term in definition).

_DOMAIN_RULES: dict[str, list[tuple[str, float]]] = {
    "flux_architecture": [
        (r"\bflux\b",                    2.0),
        (r"\bltc\b|\bliquid time",       2.0),
        (r"\brsm\b|\brecursive self",    2.0),
        (r"\bconsolidation head\b",      2.0),
        (r"\bspec head\b",               2.0),
        (r"\bkgstore\b|\bknowledge graph store\b", 2.0),
        (r"\bclingo\b|\banswer set",     1.5),
        (r"\bflm\b|\bflux language model", 2.0),
        (r"\bstage[- ][b-g]\b",          1.5),
        (r"\bakhil\b",                   1.5),
        (r"\bcons.bottleneck\b",         2.0),
        (r"\bembedding layer\b",         1.0),
        (r"\blayer system\b",            1.5),
        (r"\brk4\b|\bsolve ode\b",       1.5),
    ],
    "machine_learning": [
        (r"\bgradient\b",                1.5),
        (r"\bbackprop",                  2.0),
        (r"\bneural network\b",          1.5),
        (r"\boptimis",                   1.5),
        (r"\bloss\b",                    1.0),
        (r"\blearning rate\b",           1.5),
        (r"\bdropout\b",                 1.5),
        (r"\bbatch norm",                2.0),
        (r"\bweight\b",                  1.0),
        (r"\bactivation\b",              1.0),
        (r"\bepoch\b",                   1.5),
        (r"\boverfit",                   1.5),
        (r"\bregulari",                  1.5),
        (r"\btransfer learn",            2.0),
        (r"\bfine.tun",                  1.5),
        (r"\battention\b|\btransformer\b", 1.5),
        (r"\bembedding\b",               1.0),
        (r"\blstm\b|\bgru\b|\brnn\b",    2.0),
        (r"\bconvolution\b",             1.5),
    ],
    "thermodynamics": [
        (r"\bentropy\b",                 1.5),
        (r"\bthermody",                  2.0),
        (r"\bheat\b",                    1.5),
        (r"\bcarnot\b",                  2.0),
        (r"\bgibbs\b",                   2.0),
        (r"\bboltzmann\b",               2.0),
        (r"\btemperature\b",             1.0),
        (r"\bwork\b.{0,20}\benergy\b",   1.0),
        (r"\benergy\b",                  0.5),
        (r"\bequilibri",                 1.5),
        (r"\birreversible\b",            1.5),
        (r"\bsecond law\b",              2.0),
        (r"\bfirst law\b",              1.5),
        (r"\bstefan\b",                  2.0),
    ],
    "information_theory": [
        (r"\binformation.{0,10}entropy\b", 2.0),
        (r"\bshannon\b",                 2.0),
        (r"\bchannel capacity\b",        2.0),
        (r"\bbit\b|\bbits\b",            1.0),
        (r"\bcompression\b",             1.5),
        (r"\bencod",                     1.0),
        (r"\bdecod",                     1.0),
        (r"\bmutual information\b",      2.0),
        (r"\bkolmogorov\b",              2.0),
        (r"\btypical set\b",             2.0),
        (r"\brate.distortion\b",         2.0),
        (r"\bentropy rate\b",            2.0),
    ],
    "quantum_mechanics": [
        (r"\bquantum\b",                 1.5),
        (r"\bsuperposition\b",           2.0),
        (r"\bentanglement\b",            2.0),
        (r"\bdecoherence\b",             2.0),
        (r"\bwavefunction\b|\bwave function\b", 2.0),
        (r"\bschrodinger\b|\bschrödinger\b", 2.0),
        (r"\bplanck\b",                  2.0),
        (r"\bheisenberg\b",              2.0),
        (r"\bborn rule\b",               2.0),
        (r"\btunneling\b",               2.0),
        (r"\bspin\b",                    1.0),
        (r"\bde broglie\b",              2.0),
        (r"\bpath integral\b",           2.0),
        (r"\bquantum field\b",           2.0),
        (r"\bhamiltonian\b",             1.5),
        (r"\bhilbert space\b",           2.0),
    ],
    "neuroscience": [
        (r"\bneuron\b",                  1.5),
        (r"\bsynapse\b",                 2.0),
        (r"\bcortex\b|\bcortical\b",     2.0),
        (r"\bbasal ganglia\b",           2.0),
        (r"\bhippocampus\b",             2.0),
        (r"\bneural plasticity\b",       2.0),
        (r"\bhebbian\b",                 2.0),
        (r"\boscillation\b",             1.5),
        (r"\bsleep\b.{0,20}\bconsolidation\b", 2.0),
        (r"\bmemory consolidation\b",    2.0),
        (r"\blong.term potentiation\b",  2.0),
        (r"\baction potential\b",        2.0),
        (r"\bneuroplasticity\b",         2.0),
    ],
    "computer_science": [
        (r"\balgorithm\b",               1.5),
        (r"\bdata structure\b",          2.0),
        (r"\bbig.?o\b",                  2.0),
        (r"\bdynamic programming\b",     2.0),
        (r"\brecursion\b",               1.5),
        (r"\bbinary search\b",           2.0),
        (r"\bgraph\b.{0,30}\btraversal\b", 1.5),
        (r"\bbreadth.first\b|\bdepth.first\b", 2.0),
        (r"\bsort\b",                    1.0),
        (r"\bcomplex\b.{0,20}\btime\b",  1.0),
        (r"\bhash\b",                    1.0),
        (r"\btree\b",                    0.5),
        (r"\bstack\b|\bqueue\b",         1.0),
        (r"\bgreedy\b",                  1.5),
        (r"\bcache\b",                   1.0),
        (r"\bvirtual machine\b",         1.5),
    ],
    "mathematics": [
        (r"\bproof\b",                   1.0),
        (r"\btheorem\b",                 1.5),
        (r"\blinear algebra\b",          2.0),
        (r"\bmatrix\b|\bmatrices\b",     1.5),
        (r"\beigenvalue\b",              2.0),
        (r"\bdifferential equation\b",   2.0),
        (r"\bcalculus\b",                1.5),
        (r"\bintegral\b",               1.0),
        (r"\bderivative\b",              1.0),
        (r"\bprobability\b",             1.5),
        (r"\bstatistic\b",               1.5),
        (r"\bbayesian\b",                2.0),
        (r"\bfisher information\b",      2.0),
        (r"\bgaussian\b|\bnormal distribution\b", 1.5),
        (r"\bcramér.rao\b|\bcramer.rao\b", 2.0),
        (r"\bfourier\b",                 2.0),
        (r"\btopology\b",                2.0),
    ],
    "philosophy": [
        (r"\bconsciousness\b",           1.5),
        (r"\bqualia\b",                  2.0),
        (r"\bentit\b",                   1.0),
        (r"\bontology\b",                2.0),
        (r"\bepistemology\b",            2.0),
        (r"\baxiom\b|\baxiomatic\b",     1.5),
        (r"\bcognition\b|\bcognitive\b", 1.0),
        (r"\bself.aware",                1.5),
        (r"\bhard problem\b",            2.0),
    ],
    "cognitive_science": [
        (r"\bworking memory\b",          2.0),
        (r"\bcognitive load\b",          2.0),
        (r"\battention\b.{0,20}\bfocus\b", 1.5),
        (r"\blearning by doing\b",       2.0),
        (r"\bakhil.{0,20}learn",         2.0),
        (r"\bcurriculum learn",          2.0),
        (r"\bmental model\b",            2.0),
        (r"\bspaced repetition\b",       2.0),
    ],
    "physics": [
        (r"\bwave\b.{0,20}\bparticle\b", 1.5),
        (r"\brelativ",                   1.5),
        (r"\bfield theor",               1.5),
        (r"\bmomentum\b",                1.5),
        (r"\bforce\b",                   0.5),
        (r"\belectromagn",               1.5),
        (r"\bphoton\b",                  1.5),
        (r"\bfermion\b|\bboson\b",       2.0),
        (r"\bstandard model\b",          2.0),
        (r"\bgravit",                    1.5),
    ],
    "algorithms": [
        (r"\bdynamic programming\b",      2.0),
        (r"\bgreedy algorithm\b",          2.0),
        (r"\bsorting\b",                  1.5),
        (r"\bmerge sort\b|\bquick sort\b|\bheap sort\b", 2.0),
        (r"\bbfs\b|\bdfs\b|\bbreadth.first\b|\bdepth.first\b", 2.0),
        (r"\bdijkstra\b|\bbellman.ford\b|\bfloyd.warshall\b", 2.0),
        (r"\bmst\b|\bminimum spanning\b", 2.0),
        (r"\bmax.flow\b|\bmin.cut\b",     2.0),
        (r"\bnp.hard\b|\bnp.complete\b",  2.0),
        (r"\bamortized\b",                1.5),
        (r"\bdivide.and.conquer\b",       2.0),
        (r"\brecurrence relation\b",      1.5),
        (r"\boptimal substructure\b",     2.0),
        (r"\blinear program\b",           1.5),
        (r"\bsimplex\b",                  1.5),
    ],
    "linear_algebra": [
        (r"\beigenvectors?\b|\beigenvalues?\b", 2.0),
        (r"\bsvd\b|\bsingular values?\b|\bsingular value decomp", 2.0),
        (r"\bmatrix factori",              2.0),
        (r"\borthogonal\b",               1.5),
        (r"\bvector space\b",             2.0),
        (r"\bspan\b.{0,20}\bbasis\b|\bbasis\b.{0,20}\bspan\b", 2.0),
        (r"\brank\b.{0,20}\bmatrix\b",    1.5),
        (r"\bdeterminant\b",              1.5),
        (r"\blinear transform",           2.0),
        (r"\bnull space\b|\bkernel\b.{0,20}\bmatrix\b", 1.5),
        (r"\bpca\b|\bprincipal component", 2.0),
        (r"\bpseudoinverse\b",            2.0),
    ],
    "probability_theory": [
        (r"\bbayesian\b|\bbayes\b|\bbayes theorem\b", 2.0),
        (r"\bprobability distribution\b", 2.0),
        (r"\brandom variables?\b",        2.0),
        (r"\bexpected value\b",           1.5),
        (r"\bvariance\b|\bstd dev\b",     1.0),
        (r"\bmarkov chain\b",             2.0),
        (r"\blaw of large numbers\b|\bcentral limit\b", 2.0),
        (r"\bconditional probability\b",  2.0),
        (r"\bmonte carlo\b",              1.5),
        (r"\bpoisson\b|\bbinomial\b|\bgaussian\b", 1.5),
        (r"\bmaximum likelihood\b",       2.0),
    ],
    "abstract_algebra": [
        (r"\bgroup theory\b|\babelian group\b", 2.0),
        (r"\bring\b.{0,20}\bfield\b|\bfield\b.{0,20}\bring\b", 2.0),
        (r"\bhomomorphism\b|\bisomorphism\b", 2.0),
        (r"\bcoset\b|\bsubgroup\b",       2.0),
        (r"\bmodular arithmetic\b",       1.5),
        (r"\blattice\b",                  1.5),
        (r"\bvector space\b.{0,30}\bfield\b", 1.5),
    ],
    "type_theory": [
        (r"\btype theory\b|\blambda calculus\b", 2.0),
        (r"\bcurry.howard\b",             2.0),
        (r"\btype system\b|\bstatic type\b", 2.0),
        (r"\bpolymorphism\b.{0,20}\btype\b", 1.5),
        (r"\btype inference\b",           2.0),
        (r"\bdependent type\b",           2.0),
        (r"\bproposition.as.type\b",      2.0),
    ],
    "category_theory": [
        (r"\bcategory theory\b",          2.0),
        (r"\bfunctor\b",                  2.0),
        (r"\bnatural transformation\b",   2.0),
        (r"\badjunction\b|\badjoint\b",   2.0),
        (r"\bmonoid\b|\bmonad\b",         2.0),
        (r"\bmorphism\b",                 1.5),
        (r"\btopos\b|\btopos theory\b",   2.0),
        (r"\bcommutative diagram\b",      2.0),
    ],
}

# Compile all patterns once
_COMPILED: dict[str, list[tuple[re.Pattern, float]]] = {
    domain: [(re.compile(pat, re.I), w) for pat, w in rules]
    for domain, rules in _DOMAIN_RULES.items()
}


def infer_domain(concept_id: str, definition: str = "", related: list | None = None) -> str:
    """
    Infer the domain for a concept.

    Parameters
    ----------
    concept_id  : KG node ID (e.g. "entropy", "flux_rk4_dynamics")
    definition  : node definition text
    related     : list of related concept IDs (for cross-concept hints)

    Returns
    -------
    str : domain name (lowercase_underscore)
    """
    corpus = f"{concept_id.replace('_', ' ')} {definition}".lower()

    scores: dict[str, float] = {}
    for domain, compiled in _COMPILED.items():
        score = 0.0
        for pattern, weight in compiled:
            if pattern.search(corpus):
                score += weight
        if score > 0:
            scores[domain] = score

    if not scores:
        return "unknown"

    # No hardcoded priority — highest score wins.
    # (Previously flux_architecture was always returned if score >= 2.0, but
    #  that caused every "Flux, ..." query to be classified as flux_architecture.)
    return max(scores, key=lambda d: scores[d])


def infer_query_domain(query: str) -> str:
    """
    Infer the domain of a free-form query (not a KG node).
    Used by the Akhil model to track current_focus_domain.

    Strips agent address prefix ('Flux, ') before inference so
    'Flux, what is entropy?' → thermodynamics, not flux_architecture.
    """
    # Strip agent address — "Flux, ..." is just addressing the AI, not about Flux
    q = re.sub(r"^(?:flux|f)\s*,\s*", "", query.strip(), flags=re.I)
    return infer_domain("", q)


def infer_and_patch(store, world_model) -> int:
    """
    Batch-infer domains for all KG nodes and write back to store + world_model.

    Returns number of nodes updated.
    """
    all_ids  = list(store.all_ids())
    updated  = 0
    domain_counts: dict[str, int] = {}

    for nid in all_ids:
        node = store.get(nid)
        if not node:
            continue

        current_domain = str(node.get("domain", "") or "")
        if current_domain and current_domain != "unknown":
            # Already has a real domain — count it but don't overwrite
            domain_counts[current_domain] = domain_counts.get(current_domain, 0) + 1
            continue

        defn    = str(node.get("definition", "") or "")
        related = [e.get("to", "") for e in store.edges_from(nid)]
        domain  = infer_domain(nid, defn, related)

        try:
            store.update_field(nid, "domain", domain)
        except Exception:
            # If store doesn't support update_field, patch the node dict
            node["domain"] = domain

        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        updated += 1

    world_model.update("knowledge.kg_nodes", len(all_ids))
    world_model.update("knowledge.domains",  domain_counts)
    log.info("DomainInferrer: classified %d / %d nodes", updated, len(all_ids))
    return updated
