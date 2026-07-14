"""Build a compact cross-platform delivery report."""

from __future__ import annotations

from typing import Any


def delivery_report(plan: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    blocked = [item["node_id"] for item in ledger["node_attempts"] if item["status"] == "blocked"]
    blocked.extend(plan.get("missing_capabilities", []) if plan.get("status") == "blocked" else [])
    return {
        "blocked_items": blocked,
        "known_risks": ["fake-adapter-evidence-only"],
        "routing": {
            "missing_capabilities": plan.get("missing_capabilities", []),
            "plan_id": plan["plan_id"],
        },
        "run_id": ledger["run_id"],
        "schema_version": "1.0",
        "status": ledger["final_status"],
        "validation": {"mode": "phase-1-fake-adapters", "status": ledger["final_status"]},
    }
