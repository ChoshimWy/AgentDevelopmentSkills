"""Build the shared UI validation report from platform-collected evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..canonical_json import sha256
from ..models import ContractError
from .contracts import validate_design_agent_packet, validate_ui_validation_report


def build_ui_validation_report(
    packet: dict[str, Any],
    *,
    environment: dict[str, Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive report status without treating environment noise as implementation failure."""

    validate_design_agent_packet(packet)
    if not checks:
        raise ContractError("ui validation requires checks")
    blockers = [
        f"stale-input:{identity}" for identity in packet["stale_inputs"]
    ] + [
        f"unknown:{item['id']}" for item in packet["unknowns"] if item["blocking"]
    ]
    normalized = sorted(
        [deepcopy(check) for check in checks],
        key=lambda item: (item.get("kind", ""), item.get("target_id", "")),
    )
    for check in normalized:
        if check.get("status") in {"blocked", "failed"} and check.get("classification") != "environment-noise":
            blockers.append(f"{check.get('classification')}:{check.get('target_id')}")
    if any(check.get("status") == "failed" and check.get("classification") != "environment-noise" for check in normalized):
        status = "failed"
    elif blockers:
        status = "blocked"
    elif any(check.get("status") != "passed" for check in normalized):
        status = "partial"
    else:
        status = "passed"
    identity = {
        "blockers": sorted(set(blockers)),
        "checks": normalized,
        "environment": environment,
        "packet_fingerprint": packet["fingerprint"],
        "status": status,
    }
    value = {
        "blockers": sorted(set(blockers)),
        "checks": normalized,
        "environment": deepcopy(environment),
        "packet_fingerprint": packet["fingerprint"],
        "report_id": f"ui-report-{sha256(identity)[:16]}",
        "schema_version": "1.0",
        "status": status,
    }
    validate_ui_validation_report(value)
    return value
