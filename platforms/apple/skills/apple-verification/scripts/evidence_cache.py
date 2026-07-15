#!/usr/bin/env python3
"""Evidence freshness and same-or-stronger capability comparison."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _identity_covers(required: Any, candidate: Any) -> bool:
    if not isinstance(required, dict) or not isinstance(candidate, dict):
        return required == candidate
    for key, value in required.items():
        candidate_value = candidate.get(key)
        if key == "selectors" and isinstance(value, list) and isinstance(candidate_value, list):
            if not set(value).issubset(set(candidate_value)):
                return False
        elif candidate_value != value:
            return False
    return True


def _artifact_is_valid(item: Any, artifact_root: Path) -> bool:
    if not isinstance(item, dict):
        return False
    uri = item.get("uri")
    expected = item.get("sha256")
    if not isinstance(uri, str) or not isinstance(expected, str) or not SHA256.fullmatch(expected):
        return False
    raw = uri.removeprefix("file://")
    path = Path(raw)
    if not path.is_absolute():
        path = artifact_root / path
    try:
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    except OSError:
        return False


def reusable_evidence(
    requirement: dict[str, Any],
    candidates: list[dict[str, Any]],
    artifact_root: Path | None = None,
) -> dict[str, Any] | None:
    artifact_root = (artifact_root or Path.cwd()).resolve()
    required_capabilities = set(requirement.get("minimum_capabilities", []))
    for candidate in candidates:
        if candidate.get("status") != "passed":
            continue
        if candidate.get("environment_fingerprint") != requirement.get("environment_fingerprint"):
            continue
        if candidate.get("current_diff_hash") != requirement.get("current_diff_hash"):
            continue
        required_sources = set(requirement.get("source_fingerprints", []))
        if not required_sources.issubset(set(candidate.get("source_fingerprints", []))):
            continue
        if not required_capabilities.issubset(set(candidate.get("capabilities", []))):
            continue
        if not _identity_covers(requirement.get("identity", {}), candidate.get("identity", {})):
            continue
        artifacts = candidate.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            continue
        if any(not _artifact_is_valid(item, artifact_root) for item in artifacts):
            continue
        return candidate
    return None


def deterministic_failure_reusable(failure: dict[str, Any], fingerprint: str) -> bool:
    return (
        failure.get("fingerprint") == fingerprint
        and failure.get("classification") in {"compile", "link", "test-assertion"}
        and failure.get("retryable") is False
    )
