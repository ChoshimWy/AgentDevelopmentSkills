#!/usr/bin/env python3
"""Prepare and finalize externally signed Phase 6 release-review evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for entry in (SRC, SCRIPTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402
import run_release_gate as gate  # noqa: E402
import python_compatibility_evidence  # noqa: E402


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SIGNATURE_BYTES = 16 * 1024


def _write_new(path: Path, value: bytes) -> None:
    path = Path(os.path.abspath(path.expanduser()))
    if path.is_symlink() or path.exists():
        raise ContractError(f"output must not already exist: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def prepare(
    release: Path,
    compatibility_evidence: Path,
    key_id: str,
    draft_output: Path,
    payload_output: Path,
) -> dict[str, Any]:
    if _SHA256.fullmatch(key_id) is None:
        raise ContractError("review signer key id must be a SHA-256 identity")
    release = Path(os.path.abspath(release.expanduser()))
    first_identity = gate._release_directory_identity(release)
    manifest = gate._canonical(release / "release-manifest.json")
    revision = manifest.get("source", {}).get("revision")
    if not isinstance(revision, str) or not revision:
        raise ContractError("release manifest source revision is missing")
    compatibility = python_compatibility_evidence.validate_evidence(
        load(compatibility_evidence), require_complete=True
    )
    if compatibility["source_revision"] != revision:
        raise ContractError("Python compatibility evidence source differs from the release")
    value: dict[str, Any] = {
        "blockers": [],
        "reviewed_release_identity_sha256": sha256(first_identity),
        "reviewer": "independent",
        "python_compatibility_evidence_fingerprint": compatibility["fingerprint"],
        "schema_version": "3.0",
        "scope": "phase-6-release",
        "signature": {
            "algorithm": "rsa-pkcs1v15-sha256",
            "key_id": key_id,
            "value_hex": "",
        },
        "source_revision": revision,
        "status": "approved",
    }
    payload = gate._review_signature_payload(value)
    if gate._release_directory_identity(release) != first_identity:
        raise ContractError("release candidate changed while preparing review evidence")
    _write_new(draft_output, dumps(value).encode("utf-8"))
    _write_new(payload_output, payload)
    return {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "python_compatibility_evidence_fingerprint": compatibility["fingerprint"],
        "release_identity_sha256": value["reviewed_release_identity_sha256"],
        "schema_version": "1.0",
        "source_revision": revision,
        "status": "prepared",
    }


def finalize(draft: Path, signature: Path, trust_store: Path, output: Path) -> dict[str, Any]:
    value = load(draft)
    if (
        not isinstance(value, dict)
        or "fingerprint" in value
        or not isinstance(value.get("signature"), dict)
        or value["signature"].get("value_hex") != ""
    ):
        raise ContractError("release review draft contract is invalid")
    signature_path = Path(os.path.abspath(signature.expanduser()))
    if signature_path.is_symlink() or not signature_path.is_file():
        raise ContractError("release review signature file is missing or unsafe")
    signature_bytes = signature_path.read_bytes()
    if not signature_bytes or len(signature_bytes) > _MAX_SIGNATURE_BYTES:
        raise ContractError("release review signature size is invalid")
    value["signature"]["value_hex"] = signature_bytes.hex()
    value["fingerprint"] = sha256(value)
    _write_new(output, dumps(value).encode("utf-8"))
    try:
        gate._review_evidence(output, trust_store)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "evidence_fingerprint": value["fingerprint"],
        "key_id": value["signature"]["key_id"],
        "schema_version": "1.0",
        "status": "finalized",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--release-dir", type=Path, required=True)
    prepare_parser.add_argument("--python-compatibility-evidence", type=Path, required=True)
    prepare_parser.add_argument("--key-id", required=True)
    prepare_parser.add_argument("--draft-output", type=Path, required=True)
    prepare_parser.add_argument("--payload-output", type=Path, required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--draft", type=Path, required=True)
    finalize_parser.add_argument("--signature", type=Path, required=True)
    finalize_parser.add_argument("--review-trust-store", type=Path, required=True)
    finalize_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            report = prepare(
                args.release_dir,
                args.python_compatibility_evidence,
                args.key_id,
                args.draft_output,
                args.payload_output,
            )
        else:
            report = finalize(
                args.draft,
                args.signature,
                args.review_trust_store,
                args.output,
            )
    except (ContractError, OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
        return 2
    print(dumps(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
