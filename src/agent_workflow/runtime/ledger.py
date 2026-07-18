"""Append-only JSONL run ledger with a validated snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..canonical_json import dumps
from ..contracts import validate_run_ledger
from ..models import ContractError


MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_EVENTS = 100_000


class RunLedger:
    def __init__(
        self,
        plan_fingerprint: str,
        *,
        path: str | Path | None = None,
        package_lock_hash: str = "",
        resolved_policy_hash: str = "",
        run_id: str | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self._event_count = 0
        self.value: dict[str, Any] = {
            "approval_records": [],
            "artifact_hashes": [],
            "adapter_outcomes": [],
            "evidence": [],
            "final_status": "active",
            "node_attempts": [],
            "package_lock_hash": package_lock_hash,
            "plan_fingerprint": plan_fingerprint,
            "resolved_policy_hash": resolved_policy_hash,
            "resource_events": [],
            "run_id": run_id or f"run-{uuid4().hex}",
            "schema_version": "1.0",
        }

    @property
    def event_count(self) -> int:
        return self._event_count

    def append(self, event_type: str, value: dict[str, Any]) -> None:
        supported = {
            "adapter-evidence", "adapter-outcome", "approval-record", "artifact-hash",
            "node-attempt", "resource-event", "run-blocked", "run-finalized", "run-resumed", "run-started",
        }
        if event_type not in supported:
            raise ContractError(f"unknown ledger event type: {event_type}")
        if self._event_count >= MAX_LEDGER_EVENTS:
            raise ContractError(f"runtime ledger has more than {MAX_LEDGER_EVENTS} events")
        event = {"event_type": event_type, "run_id": self.value["run_id"], "value": value}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise ContractError("runtime ledger path must be a regular file")
            encoded = dumps(event)
            if self.path.exists() and self.path.stat().st_size + len(encoded.encode("utf-8")) > MAX_LEDGER_BYTES:
                raise ContractError(f"runtime ledger has more than {MAX_LEDGER_BYTES} bytes")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
        self._event_count += 1
        if event_type == "node-attempt":
            # JSONL remains append-only, while the validated materialized view keeps
            # exactly one current snapshot for each logical attempt.  Resume may
            # advance an approval-blocked attempt without minting a new attempt id.
            # Replacing that snapshot avoids ambiguous duplicate identities in the
            # gate-facing ledger contract without discarding the event history.
            existing = next(
                (
                    index
                    for index, attempt in enumerate(self.value["node_attempts"])
                    if attempt["attempt_id"] == value["attempt_id"]
                ),
                None,
            )
            if existing is None:
                self.value["node_attempts"].append(value)
            else:
                self.value["node_attempts"][existing] = value
        elif event_type == "resource-event":
            self.value["resource_events"].append(value)
        elif event_type == "approval-record":
            self.value["approval_records"].append(value)
        elif event_type == "artifact-hash":
            self.value["artifact_hashes"].append(value)
        elif event_type == "adapter-evidence":
            self.value["evidence"].append(value)
        elif event_type == "adapter-outcome":
            self.value["adapter_outcomes"].append(value)
        elif event_type == "run-finalized":
            self.value["final_status"] = value["status"]
        elif event_type == "run-started":
            if value["plan_fingerprint"] != self.value["plan_fingerprint"]:
                raise ContractError("run-started fingerprint does not match ledger")
            if value.get("package_lock_hash", "") != self.value["package_lock_hash"]:
                raise ContractError("run-started package lock does not match ledger")
        elif event_type == "run-resumed":
            if value.get("package_lock_hash", "") != self.value["package_lock_hash"]:
                raise ContractError("run-resumed package lock does not match ledger")
            self.value["final_status"] = "active"

    def finalize(self, status: str) -> dict[str, Any]:
        self.append("run-finalized", {"status": status})
        validate_run_ledger(self.value)
        return self.value

    @staticmethod
    def replay(path: str | Path, plan_fingerprint: str) -> "RunLedger":
        ledger_path = Path(path)
        if ledger_path.is_symlink() or not ledger_path.is_file():
            raise ContractError("runtime ledger path must be a regular file")
        if ledger_path.stat().st_size > MAX_LEDGER_BYTES:
            raise ContractError(f"runtime ledger has more than {MAX_LEDGER_BYTES} bytes")
        events = []
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            dumps(event)
            events.append(event)
        if len(events) > MAX_LEDGER_EVENTS:
            raise ContractError(f"runtime ledger has more than {MAX_LEDGER_EVENTS} events")
        if (
            not events
            or events[0].get("event_type") != "run-started"
            or sum(event.get("event_type") == "run-started" for event in events) != 1
        ):
            raise ContractError(
                "runtime ledger must begin with exactly one run-started event"
            )
        run_id = events[0]["run_id"] if events else None
        started = events[0]
        if started and started["value"]["plan_fingerprint"] != plan_fingerprint:
            raise ContractError("cannot resume ledger with a different plan fingerprint")
        package_lock_hash = started["value"].get("package_lock_hash", "") if started else ""
        ledger = RunLedger(
            plan_fingerprint,
            path=None,
            package_lock_hash=package_lock_hash,
            run_id=run_id,
        )
        for event in events:
            if event["run_id"] != ledger.value["run_id"]:
                raise ValueError("ledger contains multiple run ids")
            ledger.append(event["event_type"], event["value"])
        validate_run_ledger(ledger.value)
        ledger.path = ledger_path
        return ledger
