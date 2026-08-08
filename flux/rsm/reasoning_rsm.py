"""
flux/rsm/reasoning_rsm.py

Stage 6 Layer 2 orchestrator.

Ties the full Layer 2 pipeline together in one call:
  1. Check reasoning_drift for flagged clusters
  2. Run benchmark_probe on each flagged cluster
  3. If root_cause == rule_application:
       a. diagnose.generate() → [DIAGNOSE] block
       b. reasoning_proposal.generate() → [PROPOSE_MODIFICATION] block
       c. rule_verifier.verify() → formal check
       d. If passes: update proposal state to PENDING_APPROVAL
  4. Return summary

Called from FluxInterface.end_session() — runs only if drift has flagged
at least one cluster. Zero-cost when no clusters are flagged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Layer2Summary:
    flagged_clusters:   int
    probed:             int
    diagnosed:          int
    proposed:           int
    verified:           int
    pending_approval:   int
    skipped:            dict   # cluster → skip reason


def run_cycle(store, query_engine) -> Layer2Summary:
    """Full Layer 2 RSM cycle. Returns summary."""
    from flux.rsm import reasoning_drift, benchmark_probe, diagnose
    from flux.rsm import reasoning_proposal as rp
    from flux.rsm import rule_verifier
    from flux.rsm.reasoning_approval import _update_proposal_state

    flags   = reasoning_drift.flagged_clusters()
    summary = Layer2Summary(
        flagged_clusters = len(flags),
        probed=0, diagnosed=0, proposed=0, verified=0, pending_approval=0,
        skipped={},
    )

    # Valid reasoning domains only — skip noise clusters
    _SKIP_CLUSTERS = {"unknown", "", "none", "meta"}

    for flag in flags:
        cluster = flag["cluster"]
        if cluster.lower() in _SKIP_CLUSTERS:
            summary.skipped[cluster] = "non_domain_cluster"
            continue
        log.info("Layer2: flagged cluster %s (mean_delta=%.3f, consecutive=%d)",
                 cluster, flag["mean_delta"], flag["consecutive_above"])

        # Probe
        probe = benchmark_probe.run(cluster, store, query_engine)
        summary.probed += 1

        if probe.root_cause != "rule_application":
            reason = f"probe→{probe.root_cause}"
            summary.skipped[cluster] = reason
            log.info("Layer2: %s skipped (%s)", cluster, reason)
            continue

        # Diagnose
        drift_stats = reasoning_drift.cluster_stats(cluster)
        diagnosis   = diagnose.generate(cluster, drift_stats, probe)
        if diagnosis is None:
            summary.skipped[cluster] = "low_confidence_diagnosis"
            continue
        summary.diagnosed += 1

        # Propose
        proposal = rp.generate(diagnosis, drift_stats)
        if proposal is None:
            summary.skipped[cluster] = "no_template_for_hypothesis"
            continue
        summary.proposed += 1

        # Verify
        vr = rule_verifier.verify(proposal)
        log.info("Layer2: verification for %s — passed=%s stage=%s",
                 proposal.proposal_id, vr.passed, vr.stage)

        if not vr.passed:
            summary.skipped[cluster] = f"verification_failed:{vr.stage}"
            _update_proposal_state(proposal.proposal_id, "VERIFICATION_FAILED", {
                "stage": vr.stage, "report": vr.report
            })
            log.info("Layer2: %s rejected by verifier: %s", proposal.proposal_id, vr.report)
            continue
        summary.verified += 1

        # Ready for approval
        _update_proposal_state(proposal.proposal_id, "PENDING_APPROVAL", {
            "verification_report": vr.report,
            "diagnosis_block":     diagnosis.raw_block,
        })
        summary.pending_approval += 1
        log.info("Layer2: proposal %s is PENDING_APPROVAL", proposal.proposal_id)

    return summary
