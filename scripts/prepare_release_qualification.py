#!/usr/bin/env python3
"""Prepare or validate a clean, externally signable Phase 6 qualification handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src", ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from agent_workflow.canonical_json import dump, dumps, load, sha256  # noqa: E402
from agent_workflow.contracts import validate_upgrade_conformance_evidence  # noqa: E402
from agent_workflow.installation import build_install_bundle  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402
from agent_workflow.package_lock import validate_package_lock  # noqa: E402
import prepare_release_review as review_tool  # noqa: E402
import python_compatibility_evidence  # noqa: E402
import run_release_gate as gate  # noqa: E402


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MAX_HANDOFF_FILES = 4096
_FIXED_FILES = {
    "candidate-package-lock.json",
    "conformance-evidence.json",
    "python-compatibility-evidence.json",
    "release-gate-preflight.json",
    "release-review-draft.json",
    "release-review-payload.json",
}
_REQUIRED_PREFLIGHT_BLOCKERS = {
    "release.conformance",
    "release.independent-review",
    "release.python-distribution",
}
_ALLOWED_PREFLIGHT_BLOCKERS = {
    *_REQUIRED_PREFLIGHT_BLOCKERS,
    "release.license-notice",
}
_REQUIRED_PREFLIGHT_PASSES = {
    "release.manifest",
    "release.python-compatibility",
    "release.source-policy",
    "release.supply-chain",
}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != value
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise ContractError("release qualification handoff path is unsafe")
    return relative


def _records(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ContractError("release qualification handoff contains a symlink")
        if not path.is_file() or path.name == "handoff.json" and path.parent == root:
            continue
        result.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": _digest(path),
            "size": path.stat().st_size,
        })
    if not result or len(result) > _MAX_HANDOFF_FILES:
        raise ContractError("release qualification handoff file count is invalid")
    return result


def validate_handoff(root: Path, value: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(os.path.abspath(root.expanduser()))
    if root.is_symlink() or not root.is_dir():
        raise ContractError("release qualification handoff directory is missing or unsafe")
    handoff_path = root / "handoff.json"
    if handoff_path.is_symlink() or not handoff_path.is_file():
        raise ContractError("release qualification handoff manifest is missing or unsafe")
    stored_document = load(handoff_path)
    if value is not None and stored_document != value:
        raise ContractError("release qualification handoff manifest differs from prepared value")
    document = stored_document
    expected = {
        "candidate_package_lock_hash", "conformance_evidence_fingerprint", "files",
        "fingerprint", "preflight_blockers", "preflight_gate_fingerprint",
        "python_compatibility_evidence_fingerprint", "release_identity_sha256",
        "release_manifest_sha256", "review_key_id", "review_payload_sha256",
        "schema_version", "source_revision", "status",
    }
    hashes = (
        expected
        - {"files", "preflight_blockers", "schema_version", "source_revision", "status"}
    )
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or document.get("schema_version") != "1.0"
        or document.get("status") != "awaiting-external-signature"
        or not isinstance(document.get("source_revision"), str)
        or _REVISION.fullmatch(document["source_revision"]) is None
        or any(
            not isinstance(document.get(field), str)
            or _SHA256.fullmatch(document[field]) is None
            for field in hashes
        )
    ):
        raise ContractError("release qualification handoff contract is invalid")
    blockers = document.get("preflight_blockers")
    if (
        not isinstance(blockers, list)
        or blockers != sorted(set(blockers))
        or not _REQUIRED_PREFLIGHT_BLOCKERS <= set(blockers) <= _ALLOWED_PREFLIGHT_BLOCKERS
    ):
        raise ContractError("release qualification preflight blockers are invalid")
    files = document.get("files")
    if not isinstance(files, list) or len(files) > _MAX_HANDOFF_FILES:
        raise ContractError("release qualification handoff files are invalid")
    paths = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "size"}
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
            or type(item.get("size")) is not int
            or item["size"] < 1
        ):
            raise ContractError("release qualification handoff file record is invalid")
        _safe_relative(item["path"])
        paths.append(item["path"])
    if paths != sorted(set(paths)) or not _FIXED_FILES <= set(paths):
        raise ContractError("release qualification handoff file set is invalid")
    if not any(path.startswith("release/") for path in paths):
        raise ContractError("release qualification handoff has no release candidate")
    if files != _records(root):
        raise ContractError("release qualification handoff files differ from its manifest")
    expected_directories = {
        parent.as_posix()
        for path in paths
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    actual_directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != expected_directories:
        raise ContractError("release qualification handoff directories differ from its manifest")
    if document["fingerprint"] != sha256({
        key: item for key, item in document.items() if key != "fingerprint"
    }):
        raise ContractError("release qualification handoff fingerprint mismatch")

    conformance = load(root / "conformance-evidence.json")
    validate_upgrade_conformance_evidence(conformance)
    candidate_lock = load(root / "candidate-package-lock.json")
    validate_package_lock(candidate_lock)
    compatibility = python_compatibility_evidence.validate_evidence(
        load(root / "python-compatibility-evidence.json"), require_complete=True
    )
    preflight = load(root / "release-gate-preflight.json")
    draft = load(root / "release-review-draft.json")
    gate._validate_release_gate_report(preflight)
    recomputed_preflight = gate.evaluate_release_gate(
        root / "release",
        conformance_evidence=root / "conformance-evidence.json",
        python_compatibility_evidence_path=root / "python-compatibility-evidence.json",
    )
    if recomputed_preflight != preflight:
        raise ContractError("release qualification preflight differs from fresh evaluation")
    if (
        not isinstance(draft, dict)
        or set(draft) != {
            "blockers", "python_compatibility_evidence_fingerprint",
            "reviewed_release_identity_sha256", "reviewer", "schema_version", "scope",
            "signature", "source_revision", "status",
        }
        or draft.get("schema_version") != "3.0"
        or draft.get("scope") != "phase-6-release"
        or draft.get("reviewer") != "independent"
        or draft.get("status") != "approved"
        or draft.get("blockers") != []
        or draft.get("signature", {}).get("algorithm") != "rsa-pkcs1v15-sha256"
        or draft.get("signature", {}).get("value_hex") != ""
    ):
        raise ContractError("release qualification review draft is invalid")
    payload_path = root / "release-review-payload.json"
    if payload_path.read_bytes() != gate._review_signature_payload(draft):
        raise ContractError("release qualification review payload differs from its draft")
    manifest_path = root / "release" / "release-manifest.json"
    manifest = gate._canonical(manifest_path)
    python_index = gate._canonical(root / "release" / "python-artifacts.json")
    python_records = sorted(
        gate._validate_python_artifacts(python_index),
        key=lambda item: (item["kind"], item["filename"]),
    )
    release_identity = sha256(gate._release_directory_identity(root / "release"))
    checks = {item["id"]: item["status"] for item in preflight["checks"]}
    expected_paths = {
        *_FIXED_FILES,
        *(f"release/{name}" for name in gate._release_directory_identity(root / "release")),
    }
    if set(paths) != expected_paths:
        raise ContractError("release qualification handoff exact file allowlist differs")
    if (
        conformance["fingerprint"] != document["conformance_evidence_fingerprint"]
        or conformance["candidate_package_lock_hash"]
        != document["candidate_package_lock_hash"]
        or candidate_lock["fingerprint"] != document["candidate_package_lock_hash"]
        or compatibility["fingerprint"]
        != document["python_compatibility_evidence_fingerprint"]
        or compatibility["source_revision"] != document["source_revision"]
        or preflight.get("fingerprint") != document["preflight_gate_fingerprint"]
        or preflight.get("blockers") != blockers
        or preflight.get("release_identity_sha256") != release_identity
        or any(checks.get(item) != "passed" for item in _REQUIRED_PREFLIGHT_PASSES)
        or draft.get("reviewed_release_identity_sha256")
        != document["release_identity_sha256"]
        or draft.get("python_compatibility_evidence_fingerprint")
        != compatibility["fingerprint"]
        or draft.get("signature", {}).get("key_id") != document["review_key_id"]
        or manifest.get("source", {}).get("revision") != document["source_revision"]
        or _digest(manifest_path) != document["release_manifest_sha256"]
        or _digest(payload_path)
        != document["review_payload_sha256"]
        or release_identity != document["release_identity_sha256"]
    ):
        raise ContractError("release qualification handoff cross-binding differs")
    source_contracts = manifest.get("artifacts")
    if not isinstance(source_contracts, list) or len(source_contracts) != 1:
        raise ContractError("release qualification source artifact contract is invalid")
    source_contract = source_contracts[0]
    source_artifact = root / "release" / source_contract.get("filename", "")
    if source_artifact.is_symlink() or not source_artifact.is_file():
        raise ContractError("release qualification source artifact is missing or unsafe")
    artifact_bytes = source_artifact.read_bytes()
    gate.bootstrap_install._verify_artifact(artifact_bytes, source_contract)
    with tempfile.TemporaryDirectory(prefix="release-qualification-source-") as directory:
        extracted = Path(directory)
        gate.bootstrap_install.extract_verified_artifact(
            artifact_bytes,
            source_contract,
            extracted,
        )
        source_root = extracted / "extracted" / source_contract["root"]
        expected_lock = build_install_bundle(
            source_root / "platforms",
            platforms=["apple", "desktop"],
            schema_root=source_root / "schemas",
        ).package_lock
        runner_sha256 = _digest(source_root / "scripts" / "run_conformance.py")
    if expected_lock != candidate_lock:
        raise ContractError("candidate package lock differs from the frozen release source")
    if (
        conformance["schema_inventory_hash"]
        != candidate_lock["schema_inventory"]["content_sha256"]
        or conformance["runner_sha256"] != runner_sha256
    ):
        raise ContractError("Conformance evidence differs from the frozen release source")
    if any(
        sorted(environment["artifacts"], key=lambda item: (item["kind"], item["filename"]))
        != python_records
        for environment in compatibility["environments"]
    ):
        raise ContractError("Python compatibility artifacts differ from the frozen release")
    return document


def prepare(
    release: Path,
    candidate_lock: Path,
    conformance_evidence: Path,
    compatibility_evidence: Path,
    key_id: str,
    output: Path,
) -> dict[str, Any]:
    if _SHA256.fullmatch(key_id) is None:
        raise ContractError("review signer key id must be a SHA-256 identity")
    requested_output = Path(os.path.abspath(output.expanduser()))
    requested_output.parent.mkdir(parents=True, exist_ok=True)
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise ContractError("release qualification output must not already exist")
    with tempfile.TemporaryDirectory(
        prefix=".release-qualification-", dir=output.parent
    ) as directory:
        stage = Path(directory) / "handoff"
        stage.mkdir(mode=0o700)
        release_snapshot = stage / "release"
        gate._snapshot_release(Path(os.path.abspath(release.expanduser())), release_snapshot)
        for source, name in (
            (candidate_lock, "candidate-package-lock.json"),
            (conformance_evidence, "conformance-evidence.json"),
            (compatibility_evidence, "python-compatibility-evidence.json"),
        ):
            source = Path(os.path.abspath(source.expanduser()))
            if source.is_symlink() or not source.is_file():
                raise ContractError(f"release qualification input is missing or unsafe: {name}")
            shutil.copyfile(source, stage / name, follow_symlinks=False)
            (stage / name).chmod(0o600)

        conformance = load(stage / "conformance-evidence.json")
        validate_upgrade_conformance_evidence(conformance)
        candidate = load(stage / "candidate-package-lock.json")
        validate_package_lock(candidate)
        if candidate["fingerprint"] != conformance["candidate_package_lock_hash"]:
            raise ContractError("candidate Lock differs from Conformance evidence")
        compatibility = python_compatibility_evidence.validate_evidence(
            load(stage / "python-compatibility-evidence.json"), require_complete=True
        )
        manifest_path = release_snapshot / "release-manifest.json"
        manifest = gate._canonical(manifest_path)
        source = manifest.get("source", {})
        if (
            source.get("dirty") is not False
            or manifest.get("channel") not in {"beta", "stable"}
            or not isinstance(source.get("revision"), str)
            or _REVISION.fullmatch(source["revision"]) is None
        ):
            raise ContractError("release qualification requires a clean beta/stable candidate")
        if compatibility["source_revision"] != source["revision"]:
            raise ContractError("Python compatibility evidence source differs from release")

        preflight = gate.evaluate_release_gate(
            release_snapshot,
            conformance_evidence=stage / "conformance-evidence.json",
            python_compatibility_evidence_path=stage / "python-compatibility-evidence.json",
        )
        checks = {item["id"]: item["status"] for item in preflight["checks"]}
        if (
            not _REQUIRED_PREFLIGHT_BLOCKERS <= set(preflight["blockers"])
            or not set(preflight["blockers"]) <= _ALLOWED_PREFLIGHT_BLOCKERS
            or any(checks.get(item) != "passed" for item in _REQUIRED_PREFLIGHT_PASSES)
        ):
            raise ContractError(
                "release qualification preflight has unexpected blockers: "
                + ", ".join(preflight["blockers"])
            )
        dump(preflight, stage / "release-gate-preflight.json")
        report = review_tool.prepare(
            release_snapshot,
            stage / "python-compatibility-evidence.json",
            key_id,
            stage / "release-review-draft.json",
            stage / "release-review-payload.json",
        )
        value: dict[str, Any] = {
            "candidate_package_lock_hash": conformance["candidate_package_lock_hash"],
            "conformance_evidence_fingerprint": conformance["fingerprint"],
            "files": _records(stage),
            "preflight_blockers": preflight["blockers"],
            "preflight_gate_fingerprint": preflight["fingerprint"],
            "python_compatibility_evidence_fingerprint": compatibility["fingerprint"],
            "release_identity_sha256": report["release_identity_sha256"],
            "release_manifest_sha256": _digest(manifest_path),
            "review_key_id": key_id,
            "review_payload_sha256": report["payload_sha256"],
            "schema_version": "1.0",
            "source_revision": source["revision"],
            "status": "awaiting-external-signature",
        }
        value["fingerprint"] = sha256(value)
        dump(value, stage / "handoff.json")
        validate_handoff(stage, value)
        # Publish the already validated directory in one rename. The output
        # parent is a trusted CI-owned directory and must not have concurrent
        # writers; this is an explicit precondition because portable Python has
        # no atomic directory RENAME_NOREPLACE primitive on every supported OS.
        os.rename(stage, output)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--release-dir", type=Path, required=True)
    prepare_parser.add_argument("--candidate-lock", type=Path, required=True)
    prepare_parser.add_argument("--conformance-evidence", type=Path, required=True)
    prepare_parser.add_argument("--python-compatibility-evidence", type=Path, required=True)
    prepare_parser.add_argument("--review-key-id", required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("handoff", type=Path)
    args = parser.parse_args()
    try:
        result = (
            prepare(
                args.release_dir,
                args.candidate_lock,
                args.conformance_evidence,
                args.python_compatibility_evidence,
                args.review_key_id,
                args.output,
            )
            if args.command == "prepare"
            else validate_handoff(args.handoff)
        )
    except (
        ContractError, OSError, TypeError, ValueError, KeyError, json.JSONDecodeError
    ) as error:
        print(
            dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}),
            end="",
            file=sys.stderr,
        )
        return 2
    print(dumps({
        "fingerprint": result["fingerprint"],
        "release_identity_sha256": result["release_identity_sha256"],
        "status": result["status"],
    }), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
