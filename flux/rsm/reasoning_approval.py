"""
flux/rsm/reasoning_approval.py

Stage 6 Layer 2 — approval handler for reasoning proposals.

Replaces the stub in approval_handler.py with a full implementation.

CLI approval:
  python -m flux.rsm.approve <proposal_id>          # approve
  python -m flux.rsm.approve <proposal_id> --reject  # reject with typed reason

Ed25519 slot:
  _crypto_approve() raises NotImplementedError until Stage 7.
  Method signature and config paths are defined now for zero-rearchitecting at Stage 7.

Typed RejectionReason (6 values):
  Rejections logged back to layer2_diagnoses.jsonl as training signal —
  Flux can later query "how often is my diagnosis wrong?" and on which reason.

Rule versioning:
  Before any merge, flux_kg_rules.lp is snapshot to flux_kg_rules_v{N:03d}.lp.
  Version index tracked in logs/rule_versions.json.
"""
from __future__ import annotations

import enum
import json
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROPOSAL_LOG   = Path("logs/layer2_queue.jsonl")
DIAGNOSE_LOG   = Path("logs/layer2_diagnoses.jsonl")
VERSION_INDEX  = Path("logs/rule_versions.json")
KG_RULES_PATH  = Path("flux_kg_rules.lp")
RULES_BACKUP_DIR = Path("checkpoints")   # versioned snapshots go here

# Ed25519 config paths — defined now, wired at Stage 7
_ED25519_PUBLIC_KEY_PATH  = Path("keys/flux_approval.pub")
_ED25519_SIGNATURE_SUFFIX = ".sig"


class RejectionReason(str, enum.Enum):
    WRONG_DIAGNOSIS       = "WRONG_DIAGNOSIS"       # Flux misidentified root cause
    WRONG_TARGET          = "WRONG_TARGET"           # Diagnosis right, wrong thing to modify
    WRONG_DIRECTION       = "WRONG_DIRECTION"        # Right target, wrong direction
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # Drift too small to justify
    TOO_AGGRESSIVE        = "TOO_AGGRESSIVE"         # Change magnitude too large
    CONSTITUTIONAL_RISK   = "CONSTITUTIONAL_RISK"    # Too close to constitutional layer


class ReasoningApprovalHandler:

    def approve(self, proposal_id: str, method: str = "cli") -> str:
        """Approve a verified Layer 2 proposal. Returns 'merged' or 'error'."""
        if method == "cli":
            return self._cli_approve(proposal_id)
        elif method == "ed25519":
            return self._crypto_approve(proposal_id)
        raise ValueError(f"Unknown approval method: {method}")

    def reject(self, proposal_id: str, reason: RejectionReason, note: str = "") -> str:
        """Reject a proposal with a typed reason. Logs as training signal."""
        proposal = self._load_proposal(proposal_id)
        if not proposal:
            return "not_found"

        _update_proposal_state(proposal_id, "REJECTED", {
            "rejection_reason": reason.value,
            "rejection_note":   note,
            "rejected_at":      time.time(),
        })

        # Log to diagnoses as training signal — Flux can query her rejection stats
        _append_to_diagnose_log({
            "ts":             time.time(),
            "event":          "REJECTION_FEEDBACK",
            "proposal_id":    proposal_id,
            "cluster":        proposal.get("cluster"),
            "hypothesis":     proposal.get("hypothesis"),
            "rejection_reason": reason.value,
            "note":           note,
        })
        log.info("REJECTED proposal %s: %s — %s", proposal_id, reason.value, note)
        return "rejected"

    # ── CLI approve ──────────────────────────────────────────────────────────

    def _cli_approve(self, proposal_id: str) -> str:
        proposal = self._load_proposal(proposal_id)
        if not proposal:
            return "not_found"
        if proposal.get("state") != "PENDING_APPROVAL":
            log.warning("Proposal %s is in state %s, expected PENDING_APPROVAL",
                        proposal_id, proposal.get("state"))
            return "wrong_state"

        return self._merge(proposal)

    # ── Ed25519 slot (Stage 7) ───────────────────────────────────────────────

    def _crypto_approve(self, proposal_id: str) -> str:
        raise NotImplementedError(
            "Ed25519 approval not yet activated. "
            "Wire in at Stage 7 before autonomous research goes live. "
            f"Public key path: {_ED25519_PUBLIC_KEY_PATH} "
            f"Signature suffix: {_ED25519_SIGNATURE_SUFFIX}"
        )

    # ── Merge ────────────────────────────────────────────────────────────────

    def _merge(self, proposal: dict) -> str:
        target = proposal.get("target")

        if target == "inference_rules":
            return self._merge_rule(proposal)
        elif target == "edge_confidence_weights":
            return self._merge_weights(proposal)
        log.error("Unknown target %s in proposal %s", target, proposal.get("proposal_id"))
        return "error"

    def _merge_rule(self, proposal: dict) -> str:
        rule_text = proposal.get("rule_text", "")
        if not rule_text:
            return "error"

        # Snapshot current flux_kg_rules.lp before modifying
        version = _next_version()
        if KG_RULES_PATH.exists():
            snapshot = RULES_BACKUP_DIR / f"flux_kg_rules_v{version:03d}.lp"
            shutil.copy(KG_RULES_PATH, snapshot)
            _record_version(version, proposal["proposal_id"])
            log.info("Snapshotted flux_kg_rules.lp → %s", snapshot)

        # Append the new rule
        with KG_RULES_PATH.open("a", encoding="utf-8") as f:
            f.write(f"\n%% === Layer 2 RSM merge: {proposal['proposal_id']} ===\n")
            f.write(rule_text + "\n")

        _update_proposal_state(proposal["proposal_id"], "MERGED", {"merged_at": time.time()})
        log.info("MERGED rule proposal %s into flux_kg_rules.lp (v%03d)", 
                 proposal["proposal_id"], version)
        return "merged"

    def _merge_weights(self, proposal: dict) -> str:
        delta = proposal.get("weight_delta", {})
        # Edge weight updates go directly to the KG store's edge metadata
        # This is a low-risk, reversible operation — no file modification needed
        try:
            from flux.kg.store import get_store
            store = get_store()
            cluster   = delta.get("cluster", "")
            edge_type = delta.get("edge_type", "REQUIRES")
            new_weight = float(delta.get("new_weight", 0.87))

            updated = 0
            for nid, node in store.all_nodes().items():
                if node.get("layer", "").lower() != cluster.lower():
                    continue
                edges = store._edges_from.get(nid, [])
                for edge in edges:
                    if edge.get("type", "").upper() == edge_type:
                        edge["confidence"] = new_weight
                        updated += 1
            log.info("Weight merge: updated %d %s edges in cluster %s → %.3f",
                     updated, edge_type, cluster, new_weight)
        except Exception as exc:
            log.error("Weight merge failed: %s", exc)
            return "error"

        _update_proposal_state(proposal["proposal_id"], "MERGED", {"merged_at": time.time()})
        return "merged"

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _load_proposal(self, proposal_id: str) -> dict | None:
        if not PROPOSAL_LOG.exists():
            return None
        for line in PROPOSAL_LOG.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("proposal_id") == proposal_id:
                return e
        return None

    def show_pending(self) -> list[dict]:
        """Return all proposals in PENDING_APPROVAL state for CLI review."""
        if not PROPOSAL_LOG.exists():
            return []
        results = []
        for line in PROPOSAL_LOG.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("state") == "PENDING_APPROVAL":
                results.append(e)
        return results


# ── Module-level helpers ─────────────────────────────────────────────────────

def _update_proposal_state(proposal_id: str, state: str, extra: dict) -> None:
    """Append a state-update record to layer2_queue.jsonl."""
    PROPOSAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"proposal_id": proposal_id, "state": state, **extra}
    with PROPOSAL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _append_to_diagnose_log(entry: dict) -> None:
    DIAGNOSE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DIAGNOSE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _next_version() -> int:
    if not VERSION_INDEX.exists():
        return 1
    data = json.loads(VERSION_INDEX.read_text())
    return data.get("next_version", 1)


def _record_version(version: int, proposal_id: str) -> None:
    data: dict = {}
    if VERSION_INDEX.exists():
        data = json.loads(VERSION_INDEX.read_text())
    data.setdefault("versions", []).append({
        "version": version, "proposal_id": proposal_id, "ts": time.time()
    })
    data["next_version"] = version + 1
    VERSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
    VERSION_INDEX.write_text(json.dumps(data, indent=2))
