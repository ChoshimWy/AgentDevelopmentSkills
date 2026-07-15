#!/usr/bin/env python3
"""Atomic Verification Session storage with fail-closed path validation."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable


SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _canonical_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _validate_session(value: dict[str, Any], expected_id: str) -> None:
    required = {
        "schema_version",
        "session_id",
        "base_commit",
        "current_diff_hash",
        "environment_fingerprint",
        "project",
        "test_list_cache",
        "target_fingerprints",
        "evidence",
        "in_flight_requests",
        "failed_requests",
    }
    if set(value) != required:
        raise ValueError(f"session fields mismatch: {sorted(set(value) ^ required)}")
    if value["schema_version"] != "1.0" or value["session_id"] != expected_id:
        raise ValueError("session identity mismatch")
    for field in ("project", "test_list_cache", "target_fingerprints", "evidence", "in_flight_requests", "failed_requests"):
        if not isinstance(value[field], dict):
            raise ValueError(f"session {field} must be an object")


class SessionStore:
    def __init__(self, project_root: Path, session_id: str):
        if not SESSION_ID.fullmatch(session_id) or session_id in {".", ".."}:
            raise ValueError("invalid session id")
        self.project_root = project_root.resolve()
        self.session_id = session_id
        self.directory = self.project_root / ".codex" / "verification" / "sessions" / session_id
        self.path = self.directory / "session.json"

    def create(
        self,
        *,
        base_commit: str,
        current_diff_hash: str,
        environment_fingerprint: str,
        project: dict[str, Any],
    ) -> dict[str, Any]:
        if self.path.exists():
            raise FileExistsError(f"verification session already exists: {self.session_id}")
        value = {
            "schema_version": "1.0",
            "session_id": self.session_id,
            "base_commit": base_commit,
            "current_diff_hash": current_diff_hash,
            "environment_fingerprint": environment_fingerprint,
            "project": project,
            "test_list_cache": {},
            "target_fingerprints": {},
            "evidence": {},
            "in_flight_requests": {},
            "failed_requests": {},
        }
        self.write(value)
        return value

    def load(self) -> dict[str, Any]:
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("session must be an object")
        _validate_session(value, self.session_id)
        return value

    def write(self, value: dict[str, Any]) -> None:
        _validate_session(value, self.session_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".session.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(_canonical_text(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def update(self, transform: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        value = self.load()
        transform(value)
        self.write(value)
        return value


def _json_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--session-id", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--base-commit", required=True)
    create.add_argument("--diff-hash", required=True)
    create.add_argument("--environment-fingerprint", required=True)
    create.add_argument("--project", type=_json_object, required=True)
    subparsers.add_parser("show")
    args = parser.parse_args()
    store = SessionStore(args.root, args.session_id)
    if args.command == "create":
        value = store.create(
            base_commit=args.base_commit,
            current_diff_hash=args.diff_hash,
            environment_fingerprint=args.environment_fingerprint,
            project=args.project,
        )
    else:
        value = store.load()
    print(_canonical_text(value), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
