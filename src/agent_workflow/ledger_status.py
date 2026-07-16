"""Deterministic status projections over a validated Run Ledger."""

from __future__ import annotations

from typing import Any


def current_validation_status(ledger: dict[str, Any]) -> str:
    """Return the worst status across validation evidence on latest node attempts."""

    latest_attempts: dict[str, dict[str, Any]] = {}
    for attempt in ledger["node_attempts"]:
        previous = latest_attempts.get(attempt["node_id"])
        if previous is None or attempt["attempt_number"] > previous["attempt_number"]:
            latest_attempts[attempt["node_id"]] = attempt
    latest_attempt_ids = {attempt["attempt_id"] for attempt in latest_attempts.values()}
    statuses = [
        item["status"]
        for item in ledger.get("evidence", [])
        if item.get("attempt_id") in latest_attempt_ids and item.get("kind") == "validation"
    ]
    if not statuses:
        return ledger["final_status"]
    severity = {"completed": 0, "passed": 0, "partial": 1, "blocked": 2, "failed": 3}
    return max(statuses, key=lambda status: (severity[status], status))
