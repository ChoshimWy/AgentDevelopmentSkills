#!/usr/bin/env python3
"""Deterministic environment, target-source, and evidence fingerprints."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(value: Any, prefix: str) -> str:
    return f"{prefix}:{hashlib.sha256(canonical_bytes(value)).hexdigest()[:16]}"


def file_inventory(root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    root = root.resolve()
    inventory: list[dict[str, Any]] = []
    for raw in sorted(set(paths)):
        candidate = Path(raw)
        path = candidate if candidate.is_absolute() else root / candidate
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"fingerprint input escapes project root: {raw}") from exc
        if not resolved.is_file():
            inventory.append({"path": relative, "state": "missing"})
            continue
        inventory.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                "state": "present",
            }
        )
    return inventory


def environment_fingerprint(root: Path, metadata: dict[str, Any], config_paths: Iterable[str]) -> str:
    payload = {"metadata": metadata, "configuration_inputs": file_inventory(root, config_paths)}
    return digest(payload, "env")


def target_source_fingerprint(
    root: Path,
    target: str,
    input_paths: Iterable[str],
    dependency_fingerprints: Iterable[str] = (),
    compiler_settings: dict[str, Any] | None = None,
) -> str:
    payload = {
        "target": target,
        "inputs": file_inventory(root, input_paths),
        "dependency_fingerprints": sorted(set(dependency_fingerprints)),
        "compiler_settings": compiler_settings or {},
    }
    return digest(payload, "target")


def evidence_fingerprint(
    evidence_id: str,
    environment: str,
    source_fingerprints: Iterable[str],
    identity: dict[str, Any],
) -> str:
    payload = {
        "evidence_id": evidence_id,
        "environment_fingerprint": environment,
        "source_fingerprints": sorted(set(source_fingerprints)),
        "identity": identity,
    }
    return digest(payload, "evidence")


def _json_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    environment = subparsers.add_parser("environment")
    environment.add_argument("--root", type=Path, default=Path("."))
    environment.add_argument("--metadata", type=_json_object, required=True)
    environment.add_argument("--config", action="append", default=[])

    target = subparsers.add_parser("target")
    target.add_argument("--root", type=Path, default=Path("."))
    target.add_argument("--target", required=True)
    target.add_argument("--input", action="append", default=[])
    target.add_argument("--dependency-fingerprint", action="append", default=[])
    target.add_argument("--compiler-settings", type=_json_object, default={})

    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--evidence-id", required=True)
    evidence.add_argument("--environment-fingerprint", required=True)
    evidence.add_argument("--source-fingerprint", action="append", default=[])
    evidence.add_argument("--identity", type=_json_object, required=True)

    args = parser.parse_args()
    if args.command == "environment":
        value = environment_fingerprint(args.root, args.metadata, args.config)
    elif args.command == "target":
        value = target_source_fingerprint(
            args.root,
            args.target,
            args.input,
            args.dependency_fingerprint,
            args.compiler_settings,
        )
    else:
        value = evidence_fingerprint(
            args.evidence_id,
            args.environment_fingerprint,
            args.source_fingerprint,
            args.identity,
        )
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
