"""Build a compact cross-platform delivery report."""

from __future__ import annotations

from typing import Any


def delivery_report(plan: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    latest_attempts: dict[str, dict[str, Any]] = {}
    for attempt in ledger["node_attempts"]:
        latest_attempts[attempt["node_id"]] = attempt
    latest_attempt_ids = {attempt["attempt_id"] for attempt in latest_attempts.values()}
    blocked = [
        node_id for node_id, attempt in latest_attempts.items() if attempt["status"] == "blocked"
    ]
    blocked.extend(plan.get("missing_capabilities", []) if plan.get("status") == "blocked" else [])
    evidence = [
        item for item in ledger.get("evidence", []) if item.get("attempt_id") in latest_attempt_ids
    ]
    validation = [item for item in evidence if item.get("kind") == "validation"]
    reviews = [item for item in evidence if item.get("kind") == "review"]
    structured = bool(evidence)
    structured_history = bool(ledger.get("adapter_outcomes"))
    report = {
        "blocked_items": blocked,
        "known_risks": (
            []
            if structured
            else ["missing-current-structured-evidence"]
            if structured_history
            else ["fake-adapter-evidence-only"]
        ),
        "routing": {
            "missing_capabilities": plan.get("missing_capabilities", []),
            "plan_id": plan["plan_id"],
        },
        "run_id": ledger["run_id"],
        "schema_version": "1.0",
        "status": ledger["final_status"],
        "validation": {
            "evidence": validation,
            "mode": "structured-provider" if structured or structured_history else "phase-1-fake-adapters",
            "status": validation[-1]["status"] if validation else ledger["final_status"],
        },
    }
    if reviews:
        report["review"] = {"evidence": reviews, "status": reviews[-1]["status"]}
    return report
