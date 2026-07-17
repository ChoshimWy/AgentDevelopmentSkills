#!/usr/bin/env python3
"""Run and merge deterministic Python 3.11–3.14 distribution evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from agent_workflow.canonical_json import dump, dumps, load, sha256  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402
from scripts.build_python_artifacts import build_python_artifacts  # noqa: E402


SUPPORTED = ("3.11", "3.12", "3.13", "3.14")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PYTHON_VERSION = re.compile(r"^3\.(11|12|13|14)\.[0-9]+(?:[+.-][A-Za-z0-9.-]+)?$")


def _validate_artifacts(value: Any) -> list[dict[str, Any]]:
    expected = {"filename", "kind", "sha256", "size"}
    if not isinstance(value, list) or len(value) != 2:
        raise ContractError("Python compatibility artifact set must contain wheel and sdist")
    records = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != expected
            or item.get("kind") not in {"wheel", "sdist"}
            or not isinstance(item.get("filename"), str)
            or not item["filename"]
            or Path(item["filename"]).name != item["filename"]
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
            or not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or item["size"] < 1
        ):
            raise ContractError("Python compatibility artifact record is invalid")
        records.append(item)
    records.sort(key=lambda item: (item["kind"], item["filename"]))
    if value != records:
        raise ContractError("Python compatibility artifacts must be canonically sorted")
    if [item["kind"] for item in records] != ["sdist", "wheel"]:
        raise ContractError("Python compatibility artifact kinds differ")
    return records


def _canonical_artifacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError("Python compatibility artifact set must be an array")
    return _validate_artifacts(sorted(value, key=lambda item: (
        item.get("kind", "") if isinstance(item, dict) else "",
        item.get("filename", "") if isinstance(item, dict) else "",
    )))


def validate_evidence(value: Any, *, require_complete: bool = False) -> dict[str, Any]:
    expected = {
        "artifact_set_sha256", "environments", "fingerprint", "schema_version",
        "source_dirty", "source_revision", "status",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("schema_version") != "1.0":
        raise ContractError("Python compatibility evidence contract is invalid")
    if not isinstance(value.get("source_revision"), str) or _REVISION.fullmatch(value["source_revision"]) is None:
        raise ContractError("Python compatibility source revision is invalid")
    if not isinstance(value.get("source_dirty"), bool):
        raise ContractError("Python compatibility source dirty state is invalid")
    environments = value.get("environments")
    if not isinstance(environments, list) or not 1 <= len(environments) <= len(SUPPORTED):
        raise ContractError("Python compatibility environments are invalid")
    expected_environment = {
        "artifacts", "implementation", "machine", "pep517_wheel_sha256", "platform",
        "python_minor", "python_version", "status", "test_count",
    }
    minors = []
    artifact_sets = []
    for environment in environments:
        if (
            not isinstance(environment, dict)
            or set(environment) != expected_environment
            or environment.get("implementation") != "CPython"
            or environment.get("status") != "passed"
            or environment.get("python_minor") not in SUPPORTED
            or not isinstance(environment.get("python_version"), str)
            or _PYTHON_VERSION.fullmatch(environment["python_version"]) is None
            or not environment["python_version"].startswith(environment["python_minor"] + ".")
            or not isinstance(environment.get("platform"), str)
            or not environment["platform"]
            or not isinstance(environment.get("machine"), str)
            or not environment["machine"]
            or not isinstance(environment.get("pep517_wheel_sha256"), str)
            or _SHA256.fullmatch(environment["pep517_wheel_sha256"]) is None
            or not isinstance(environment.get("test_count"), int)
            or isinstance(environment["test_count"], bool)
            or environment["test_count"] < 1
        ):
            raise ContractError("Python compatibility environment record is invalid")
        artifacts = _validate_artifacts(environment["artifacts"])
        wheel = next(item for item in artifacts if item["kind"] == "wheel")
        if environment["pep517_wheel_sha256"] != wheel["sha256"]:
            raise ContractError("PEP 517 wheel differs from deterministic artifact")
        minors.append(environment["python_minor"])
        artifact_sets.append(artifacts)
    if minors != sorted(set(minors), key=SUPPORTED.index):
        raise ContractError("Python compatibility environments must be sorted and unique")
    if any(records != artifact_sets[0] for records in artifact_sets[1:]):
        raise ContractError("Python compatibility artifacts differ across environments")
    if value.get("artifact_set_sha256") != sha256(artifact_sets[0]):
        raise ContractError("Python compatibility artifact set hash differs")
    complete = tuple(minors) == SUPPORTED
    qualifying = complete and not value["source_dirty"]
    if value.get("status") != ("passed" if qualifying else "partial"):
        raise ContractError("Python compatibility status differs from its environments")
    if require_complete and (not complete or value["source_dirty"]):
        raise ContractError(
            "Python 3.11–3.14 compatibility evidence is incomplete or from a dirty source"
        )
    if value.get("fingerprint") != sha256({key: item for key, item in value.items() if key != "fingerprint"}):
        raise ContractError("Python compatibility evidence fingerprint mismatch")
    return value


def _git_value(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def run(output: Path, *, source_revision: str | None = None, allow_dirty: bool = False) -> dict[str, Any]:
    revision = _git_value("rev-parse", "HEAD")
    if source_revision is not None and source_revision != revision:
        raise ContractError("requested source revision differs from Git HEAD")
    dirty = bool(_git_value("status", "--porcelain", "--untracked-files=all"))
    if not allow_dirty and dirty:
        raise ContractError("Python compatibility evidence requires a clean source")
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if minor not in SUPPORTED:
        raise ContractError("Python compatibility runner requires Python 3.11–3.14")
    with tempfile.TemporaryDirectory(prefix="agent-skills-python-compat-") as directory:
        root = Path(directory)
        artifacts_root = root / "artifacts"
        records = _canonical_artifacts(build_python_artifacts(ROOT, artifacts_root))
        wheel = next(item for item in records if item["kind"] == "wheel")
        pep517 = root / "pep517"
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps", ".", "--wheel-dir", str(pep517)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        if completed.returncode:
            raise ContractError("PEP 517 wheel build failed: " + (completed.stderr.strip() or completed.stdout.strip()))
        pep517_wheel = pep517 / wheel["filename"]
        if not pep517_wheel.is_file() or pep517_wheel.read_bytes() != (artifacts_root / wheel["filename"]).read_bytes():
            raise ContractError("PEP 517 wheel is not byte-identical")
        tests = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_python_packaging", "-v"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=1800,
        )
        if tests.returncode:
            raise ContractError("Python packaging smoke failed: " + (tests.stderr.strip() or tests.stdout.strip()))
        match = re.search(r"Ran (\d+) tests?", tests.stdout + tests.stderr)
        if match is None:
            raise ContractError("Python packaging smoke test count was not observed")
        environment = {
            "artifacts": records,
            "implementation": platform.python_implementation(),
            "machine": platform.machine() or "unknown",
            "pep517_wheel_sha256": hashlib.sha256(pep517_wheel.read_bytes()).hexdigest(),
            "platform": sys.platform,
            "python_minor": minor,
            "python_version": platform.python_version(),
            "status": "passed",
            "test_count": int(match.group(1)),
        }
    value: dict[str, Any] = {
        "artifact_set_sha256": sha256(records),
        "environments": [environment],
        "schema_version": "1.0",
        "source_dirty": dirty,
        "source_revision": revision,
        "status": "partial",
    }
    value["fingerprint"] = sha256(value)
    validate_evidence(value)
    dump(value, output)
    return value


def merge(inputs: list[Path], output: Path) -> dict[str, Any]:
    if len(inputs) != len(SUPPORTED):
        raise ContractError("Python compatibility merge requires exactly four evidence inputs")
    values = [validate_evidence(load(path)) for path in inputs]
    if any(len(value["environments"]) != 1 for value in values):
        raise ContractError("Python compatibility merge inputs must each contain one environment")
    revisions = {value["source_revision"] for value in values}
    dirty = any(value["source_dirty"] for value in values)
    environments = [environment for value in values for environment in value["environments"]]
    if len(revisions) != 1:
        raise ContractError("Python compatibility source revisions differ")
    environments.sort(key=lambda item: SUPPORTED.index(item["python_minor"]))
    value: dict[str, Any] = {
        "artifact_set_sha256": values[0]["artifact_set_sha256"],
        "environments": environments,
        "schema_version": "1.0",
        "source_dirty": dirty,
        "source_revision": next(iter(revisions)),
        "status": (
            "passed"
            if tuple(item["python_minor"] for item in environments) == SUPPORTED and not dirty
            else "partial"
        ),
    }
    value["fingerprint"] = sha256(value)
    validate_evidence(value, require_complete=True)
    dump(value, output)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--source-revision")
    run_parser.add_argument("--allow-dirty", action="store_true")
    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("inputs", nargs="+", type=Path)
    merge_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (
            run(args.output, source_revision=args.source_revision, allow_dirty=args.allow_dirty)
            if args.command == "run"
            else merge(args.inputs, args.output)
        )
    except (ContractError, OSError, subprocess.SubprocessError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
        return 2
    print(dumps({
        "fingerprint": result["fingerprint"],
        "python_versions": [item["python_minor"] for item in result["environments"]],
        "status": result["status"],
    }), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
