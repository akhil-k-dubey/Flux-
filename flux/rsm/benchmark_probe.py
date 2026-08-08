"""
flux/rsm/benchmark_probe.py

Stage 6 Layer 2 — post-session probe for flagged clusters.

When reasoning_drift flags a cluster, this module runs 10 targeted
reasoning probe queries against that cluster's KG concepts and classifies
the root cause as one of:
  rule_application   — reasoning chain exists but produces wrong confidence
  knowledge_retrieval — concept missing from KG entirely (Layer 1 target)
  domain_uncertainty  — variance is intrinsically high (no action needed)

Only "rule_application" generates a Layer 2 proposal.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

log = logging.getLogger(__name__)

PROBE_COUNT     = 10
VARIANCE_THRESH = 0.25   # std dev above which → domain_uncertainty
MISSING_THRESH  = 0.50   # fraction missing → knowledge_retrieval


@dataclass
class ProbeResult:
    cluster:        str
    root_cause:     str    # rule_application | knowledge_retrieval | domain_uncertainty
    missing_frac:   float  # fraction of probe concepts not in KG
    conf_variance:  float  # std dev of returned confidence values
    chain_errors:   int    # reasoning chains that returned wrong confidence tier
    probe_concepts: list[str]


def run(cluster: str, store, query_engine) -> ProbeResult:
    """
    Run PROBE_COUNT targeted reasoning queries for the cluster and classify.
    """
    # Select probe concepts from the cluster's KG nodes
    probe_concepts = _select_probe_concepts(cluster, store)

    missing   = 0
    confs     = []
    chain_err = 0

    for cid in probe_concepts:
        node = store.get(cid)
        if node is None:
            missing += 1
            continue

        node_conf = float(node.get("confidence", 0.5))
        confs.append(node_conf)

        # Run a DERIVE query and check if chain confidence matches node confidence
        try:
            result = query_engine.derive(cid)
            derived_confs = [
                e.get("confidence", node_conf)
                for e in (result.get("derivations") or [])[:3]
            ]
            if derived_confs:
                chain_mean = sum(derived_confs) / len(derived_confs)
                # Flag if chain-derived confidence diverges > 0.2 from node confidence
                if abs(chain_mean - node_conf) > 0.2:
                    chain_err += 1
        except Exception:
            pass   # derive unavailable for this concept — not an error

    n            = len(probe_concepts)
    missing_frac = missing / n if n else 0.0
    variance     = _std_dev(confs) if confs else 0.0

    # Classify root cause
    if missing_frac >= MISSING_THRESH:
        root_cause = "knowledge_retrieval"
    elif variance >= VARIANCE_THRESH:
        root_cause = "domain_uncertainty"
    else:
        root_cause = "rule_application"

    log.info(
        "BenchmarkProbe[%s]: root_cause=%s  missing=%.0f%%  variance=%.3f  chain_errors=%d",
        cluster, root_cause, missing_frac * 100, variance, chain_err,
    )
    return ProbeResult(
        cluster=cluster, root_cause=root_cause,
        missing_frac=missing_frac, conf_variance=variance,
        chain_errors=chain_err, probe_concepts=probe_concepts,
    )


def _select_probe_concepts(cluster: str, store) -> list[str]:
    """Pick up to PROBE_COUNT concept IDs from the cluster."""
    all_nodes = store.all_nodes()
    cluster_nodes = [
        nid for nid, nd in all_nodes.items()
        if str(nd.get("layer") or "").lower() == cluster.lower()
        and nd.get("type") not in ("ACTION_CONSEQUENCE_RECORD", "META")
    ]
    if not cluster_nodes:
        # Fallback: any trainable concept
        cluster_nodes = [
            nid for nid, nd in all_nodes.items()
            if nd.get("trainable", True)
        ]
    random.shuffle(cluster_nodes)
    return cluster_nodes[:PROBE_COUNT]


def _std_dev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5
