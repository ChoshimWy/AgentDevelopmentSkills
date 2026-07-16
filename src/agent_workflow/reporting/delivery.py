"""Build a compact cross-platform delivery report."""

from __future__ import annotations

from typing import Any

from ..models import ContractError
from ..qa.contracts import validate_qa_report
from ..ledger_status import current_validation_status


def delivery_report(
    plan: dict[str, Any],
    ledger: dict[str, Any],
    *,
    qa_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
            "status": current_validation_status(ledger),
        },
    }
    if reviews:
        report["review"] = {"evidence": reviews, "status": reviews[-1]["status"]}
    if qa_report is not None:
        validate_qa_report(qa_report)
        context = qa_report["delivery_context"]
        expected = {
            "run_id": ledger["run_id"],
            "workflow_plan_fingerprint": plan["fingerprint"],
            "workflow_plan_id": plan["plan_id"],
        }
        if context != expected:
            raise ContractError("qa-report delivery_context does not match current workflow run")
        if qa_report["verification"]["status"] != report["validation"]["status"]:
            raise ContractError("qa-report verification status conflicts with current ledger validation")
        if qa_report["release_recommendation"] == "go" and report["status"] != "completed":
            raise ContractError("qa-report go conflicts with non-completed delivery status")
        if qa_report["release_recommendation"] == "conditional-go" and report["status"] not in {"completed", "partial"}:
            raise ContractError("qa-report conditional-go conflicts with blocked or cancelled delivery status")
        report["quality"] = {
            "coverage_level": qa_report["quality"]["coverage_level"],
            "evidence_refs": qa_report["evidence_refs"],
            "qa_plan_fingerprint": qa_report["plan_fingerprint"],
            "release_recommendation": qa_report["release_recommendation"],
            "report_fingerprint": qa_report["fingerprint"],
            "run_id": context["run_id"],
            "status": qa_report["status"],
            "verification_status": qa_report["verification"]["status"],
            "workflow_plan_fingerprint": context["workflow_plan_fingerprint"],
            "workflow_plan_id": context["workflow_plan_id"],
        }
    return report
