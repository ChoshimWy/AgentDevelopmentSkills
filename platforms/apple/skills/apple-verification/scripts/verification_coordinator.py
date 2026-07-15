#!/usr/bin/env python3
"""Stateful evidence planner for the apple-verification wrapper/daemon boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from affected_tests import affected_tests  # noqa: E402
from fingerprint import canonical_bytes, environment_fingerprint  # noqa: E402
from session_store import SessionStore  # noqa: E402


PROJECT_CONFIG_SUFFIXES = (".pbxproj", ".xcscheme", ".xctestplan", ".xcconfig", "Package.resolved", "Podfile", "Podfile.lock")
UI_MARKERS = ("view.swift", "viewcontroller.swift", ".storyboard", ".xib", ".xcassets/", ".strings")
DEPENDENCY_NAMES = {"package.swift", "package.resolved", "podfile", "podfile.lock", "cartfile", "cartfile.resolved"}
RELEASE_SUFFIXES = (".entitlements", "exportoptions.plist", ".provisionprofile")


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def changed_paths(root: Path) -> list[str]:
    """Return tracked and untracked paths without parsing porcelain rename syntax."""
    paths: set[str] = set()
    for args in (("diff", "--name-only"), ("diff", "--name-only", "--cached"), ("ls-files", "--others", "--exclude-standard")):
        paths.update(line for line in git_output(root, *args).splitlines() if line)
    return sorted(paths)


def diff_hash(root: Path, changed_files: list[str]) -> str:
    inventory: list[dict[str, Any]] = []
    for raw in sorted(set(changed_files)):
        path = (root / raw).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"changed file escapes project root: {raw}") from exc
        inventory.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            }
        )
    payload = {"head": git_output(root, "rev-parse", "HEAD"), "files": inventory}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def classify(path: str) -> str:
    lower = path.lower()
    name = Path(lower).name
    if lower.endswith("agents.md") or lower.endswith("skill.md"):
        return "rule-only"
    if lower.endswith((".md", ".html", ".txt")):
        return "doc-only"
    if name in DEPENDENCY_NAMES or "/pods/manifest.lock" in lower:
        return "dependency"
    if lower.endswith(RELEASE_SUFFIXES):
        return "release"
    if path.endswith(PROJECT_CONFIG_SUFFIXES) or ".xcodeproj/" in lower or ".xcworkspace/" in lower or lower.endswith(".plist"):
        return "project-config"
    if lower.endswith(("tests.swift", "test.swift")) or "/tests/" in lower or "/uitests/" in lower:
        return "test-only"
    if any(marker in lower for marker in UI_MARKERS):
        return "ui-only"
    if lower.endswith((".swift", ".m", ".mm", ".h")):
        risky = any(token in lower for token in ("ble", "mesh", "database", "persistence", "storekit", "subscription", "network", "concurr"))
        return "swift-risky" if risky else "swift-small"
    if lower.endswith((".png", ".jpg", ".jpeg", ".pdf", ".json")):
        return "asset-only"
    return "other"


def evidence_plan(changed_files: list[str]) -> dict[str, Any]:
    classes = sorted({classify(path) for path in changed_files})
    tests = affected_tests(changed_files)
    requirements: list[dict[str, Any]] = []
    if not classes:
        return {"diff_types": [], "lane": "dev", "required_evidence": [], "status": "skipped", "reason": "no changed files"}
    if set(classes) <= {"doc-only", "rule-only"}:
        kind = "policy-lint" if "rule-only" in classes else "lint"
        requirements.append({"evidence_id": f"{kind}:current-diff", "kind": kind, "reason": "documentation or rule-only change"})
        lane = "dev"
    else:
        lane = "final" if "release" in classes else "checkpoint" if any(item in {"swift-risky", "project-config", "dependency", "other"} for item in classes) else "dev"
        if any(item in {"swift-small", "swift-risky", "ui-only", "project-config", "test-only"} for item in classes):
            requirements.append({"evidence_id": "compile:affected-target", "kind": "compile", "reason": "Apple source or project inputs changed"})
        for selector in tests.get("selectors", []):
            requirements.append({"evidence_id": f"test:{selector}", "kind": "unit-test", "identity": {"selectors": [selector]}, "reason": "affected-test mapping"})
        if "ui-only" in classes:
            requirements.append({"evidence_id": "ui:scenario-required", "kind": "ui", "reason": "UI-sensitive inputs changed"})
        if "project-config" in classes:
            requirements.append({"evidence_id": "integration:consumer", "kind": "consumer-integration", "reason": "project/dependency configuration changed"})
        if "dependency" in classes:
            requirements.extend(
                [
                    {"evidence_id": "dependency:resolve", "kind": "dependency-resolution", "reason": "dependency inputs changed"},
                    {"evidence_id": "integration:consumer", "kind": "consumer-integration", "reason": "resolved dependency must be proven in its consumer"},
                ]
            )
        if "asset-only" in classes:
            requirements.append({"evidence_id": "resource:integrity", "kind": "resource-check", "reason": "asset/resource input changed"})
        if "release" in classes:
            requirements.append({"evidence_id": "release:configuration", "kind": "release-configuration", "reason": "signing or entitlement input requires xcode-build/final evidence"})
        if "other" in classes:
            requirements.append({"evidence_id": "manual:classify-diff", "kind": "manual-classification", "reason": "unknown input cannot be verified automatically"})
    unique_requirements: dict[str, dict[str, Any]] = {}
    for requirement in requirements:
        evidence_id = requirement["evidence_id"]
        if evidence_id not in unique_requirements:
            unique_requirements[evidence_id] = requirement
            continue
        previous_reason = unique_requirements[evidence_id].get("reason", "")
        next_reason = requirement.get("reason", "")
        if next_reason and next_reason not in previous_reason:
            unique_requirements[evidence_id]["reason"] = f"{previous_reason}; {next_reason}".strip("; ")
    result: dict[str, Any] = {"diff_types": classes, "lane": lane, "required_evidence": list(unique_requirements.values())}
    if not tests.get("selectors") and any(item.startswith("swift") for item in classes):
        result["no_test_reason"] = tests["no_test_reason"]
        result["suggested_validation"] = tests["suggested_validation"]
    if "other" in classes:
        result["status"] = "blocked"
        result["next_action"] = "classify unknown inputs before execution"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--project", type=json.loads, default={})
    parser.add_argument("--environment", type=json.loads, default={})
    parser.add_argument("--config-input", action="append", default=[])
    args = parser.parse_args()
    root = args.root.resolve()
    if not isinstance(args.project, dict) or not isinstance(args.environment, dict):
        parser.error("--project and --environment must be JSON objects")
    changed_files = args.changed_file or changed_paths(root)
    current_diff_hash = diff_hash(root, changed_files)
    env_fingerprint = environment_fingerprint(root, {**args.environment, "project": args.project}, args.config_input)
    store = SessionStore(root, args.session_id)
    if store.path.exists():
        session = store.load()
        if session["base_commit"] != git_output(root, "rev-parse", "HEAD"):
            session["base_commit"] = git_output(root, "rev-parse", "HEAD")
        session["current_diff_hash"] = current_diff_hash
        session["environment_fingerprint"] = env_fingerprint
        session["project"] = args.project
        store.write(session)
    else:
        session = store.create(
            base_commit=git_output(root, "rev-parse", "HEAD"),
            current_diff_hash=current_diff_hash,
            environment_fingerprint=env_fingerprint,
            project=args.project,
        )
    output = {
        "schema_version": "1.0",
        "session_id": args.session_id,
        "current_diff_hash": current_diff_hash,
        "environment_fingerprint": env_fingerprint,
        **evidence_plan(changed_files),
    }
    print(canonical_bytes(output).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
