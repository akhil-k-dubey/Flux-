"""
flux/rsm/approve.py

CLI for reviewing and approving/rejecting Layer 2 proposals.

Usage:
  python -m flux.rsm.approve                              # list pending
  python -m flux.rsm.approve <proposal_id>               # approve
  python -m flux.rsm.approve <proposal_id> --reject <REASON>  # reject

Valid rejection reasons:
  WRONG_DIAGNOSIS | WRONG_TARGET | WRONG_DIRECTION |
  INSUFFICIENT_EVIDENCE | TOO_AGGRESSIVE | CONSTITUTIONAL_RISK
"""
from __future__ import annotations

import sys


def _list_pending():
    from flux.rsm.reasoning_approval import ReasoningApprovalHandler
    handler  = ReasoningApprovalHandler()
    pending  = handler.show_pending()
    if not pending:
        print("No Layer 2 proposals pending approval.")
        return
    print(f"\n{'='*70}")
    print(f"  {len(pending)} proposal(s) pending approval")
    print(f"{'='*70}")
    for p in pending:
        print(f"\n  ID:       {p['proposal_id']}")
        print(f"  Cluster:  {p.get('cluster')}")
        print(f"  Target:   {p.get('target')}")
        print(f"  Hypothesis: {p.get('hypothesis')}")
        print(f"\n  --- [DIAGNOSE] ---")
        print(p.get("diagnosis_block", "(none)"))
        print(f"\n  --- Rule / Weight Change ---")
        if p.get("rule_text"):
            print(p["rule_text"])
        elif p.get("weight_delta"):
            print(p["weight_delta"])
        print(f"\n  Verification: {p.get('verification_report', 'n/a')}")
        print(f"{'─'*70}")


def _approve(proposal_id: str):
    from flux.rsm.reasoning_approval import ReasoningApprovalHandler
    handler = ReasoningApprovalHandler()
    result  = handler.approve(proposal_id, method="cli")
    print(f"Approval result: {result}")


def _reject(proposal_id: str, reason_str: str, note: str = ""):
    from flux.rsm.reasoning_approval import ReasoningApprovalHandler, RejectionReason
    try:
        reason = RejectionReason(reason_str.upper())
    except ValueError:
        print(f"Invalid rejection reason: {reason_str}")
        print(f"Valid values: {[r.value for r in RejectionReason]}")
        sys.exit(1)
    handler = ReasoningApprovalHandler()
    result  = handler.reject(proposal_id, reason, note)
    print(f"Rejection result: {result}")


def main():
    args = sys.argv[1:]
    if not args:
        _list_pending()
        return

    proposal_id = args[0]
    if "--reject" in args:
        idx    = args.index("--reject")
        reason = args[idx + 1] if idx + 1 < len(args) else ""
        note   = " ".join(args[idx + 2:]) if idx + 2 < len(args) else ""
        _reject(proposal_id, reason, note)
    else:
        _approve(proposal_id)


if __name__ == "__main__":
    main()
