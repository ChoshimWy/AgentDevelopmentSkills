#!/usr/bin/env python3
"""Fail-closed compatibility validator for Rust native release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


EXPECTED_TARGETS = (
    "aarch64-apple-darwin",
    "aarch64-pc-windows-msvc",
    "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "x86_64-unknown-linux-gnu",
)
MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RUSTC = re.compile(r"^rustc 1\.97\.1 [^\r\n]+$")
_TARGETS = {
    "aarch64-apple-darwin": ("aarch64", "darwin", "macho", 0x0100000C),
    "aarch64-pc-windows-msvc": ("aarch64", "windows", "pe", 0xAA64),
    "aarch64-unknown-linux-gnu": ("aarch64", "linux", "elf", 183),
    "x86_64-apple-darwin": ("x86_64", "darwin", "macho", 0x01000007),
    "x86_64-pc-windows-msvc": ("x86_64", "windows", "pe", 0x8664),
    "x86_64-unknown-linux-gnu": ("x86_64", "linux", "elf", 62),
}
_INDEX_FIELDS = {
    "artifacts",
    "fingerprint",
    "product",
    "schema_version",
    "source_revision",
    "target_set_sha256",
    "version",
}
_RECORD_FIELDS = {
    "arch",
    "cargo_lock_sha256",
    "filename",
    "fingerprint",
    "kind",
    "os",
    "profile",
    "rustc_version",
    "schema_version",
    "sha256",
    "size",
    "smoke_output",
    "smoke_status",
    "source_revision",
    "target",
    "version",
}


class NativeArtifactError(RuntimeError):
    """Raised when a native artifact matrix differs from its frozen contract."""


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def native_filename(version: str, target: str) -> str:
    suffix = ".exe" if "-windows-" in target else ""
    return f"agent-skills-{version}-{target}{suffix}"


def load_native_artifacts(
    index_path: Path,
    artifacts_dir: Path,
    *,
    expected_source_revision: str,
    expected_version: str,
) -> dict[str, Any]:
    """Load and verify one complete six-target matrix without executing binaries."""

    raw = _read(index_path, MAX_INDEX_BYTES, "native artifact index")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeArtifactError(f"native artifact index is invalid JSON: {error}") from error
    if not isinstance(value, dict) or set(value) != _INDEX_FIELDS:
        raise NativeArtifactError("native artifact index fields are invalid")
    if canonical_json(value) != raw:
        raise NativeArtifactError("native artifact index is not canonical JSON")
    if (
        value.get("schema_version") != "1.0"
        or value.get("product") != "agent-development-skills"
        or value.get("version") != expected_version
        or value.get("source_revision") != expected_source_revision
        or _VERSION.fullmatch(value.get("version", "")) is None
        or _REVISION.fullmatch(value.get("source_revision", "")) is None
    ):
        raise NativeArtifactError("native artifact index identity is invalid")
    records = value.get("artifacts")
    if not isinstance(records, list) or len(records) != len(EXPECTED_TARGETS):
        raise NativeArtifactError("native artifact index requires exactly six targets")
    cargo_locks: set[str] = set()
    observed_targets: list[str] = []
    observed_filenames: set[str] = set()
    for record in records:
        _validate_record(
            record,
            artifacts_dir,
            expected_source_revision=expected_source_revision,
            expected_version=expected_version,
        )
        observed_targets.append(record["target"])
        if record["filename"] in observed_filenames:
            raise NativeArtifactError("native artifact filenames must be unique")
        observed_filenames.add(record["filename"])
        cargo_locks.add(record["cargo_lock_sha256"])
    if tuple(observed_targets) != EXPECTED_TARGETS or len(cargo_locks) != 1:
        raise NativeArtifactError(
            "native artifact targets must be sorted, complete, and share one Cargo.lock"
        )
    target_set_sha256 = fingerprint(list(EXPECTED_TARGETS))
    if value.get("target_set_sha256") != target_set_sha256:
        raise NativeArtifactError("native artifact target-set identity differs")
    unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
    if value.get("fingerprint") != fingerprint(unsigned):
        raise NativeArtifactError("native artifact index fingerprint differs")
    return value


def _validate_record(
    record: Any,
    artifacts_dir: Path,
    *,
    expected_source_revision: str,
    expected_version: str,
) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise NativeArtifactError("native artifact record fields are invalid")
    target = record.get("target")
    descriptor = _TARGETS.get(target)
    if descriptor is None:
        raise NativeArtifactError("native artifact target is unsupported")
    arch, host_os, _, _ = descriptor
    filename = native_filename(expected_version, target)
    if (
        record.get("schema_version") != "1.0"
        or record.get("kind") != "native-binary"
        or record.get("profile") != "release"
        or record.get("version") != expected_version
        or record.get("source_revision") != expected_source_revision
        or record.get("arch") != arch
        or record.get("os") != host_os
        or record.get("filename") != filename
        or type(record.get("size")) is not int
        or not 0 < record["size"] <= MAX_BINARY_BYTES
        or _SHA256.fullmatch(record.get("sha256", "")) is None
        or _SHA256.fullmatch(record.get("cargo_lock_sha256", "")) is None
        or _RUSTC.fullmatch(record.get("rustc_version", "")) is None
        or record.get("smoke_status") != "passed"
        or record.get("smoke_output") != f"agent-skills-rs {expected_version}\n"
    ):
        raise NativeArtifactError(f"native artifact record identity is invalid: {target}")
    unsigned = {key: item for key, item in record.items() if key != "fingerprint"}
    if record.get("fingerprint") != fingerprint(unsigned):
        raise NativeArtifactError(f"native artifact record fingerprint differs: {target}")
    binary = _read(artifacts_dir / filename, MAX_BINARY_BYTES, "native executable")
    if (
        len(binary) != record["size"]
        or hashlib.sha256(binary).hexdigest() != record["sha256"]
    ):
        raise NativeArtifactError(f"native executable differs from its record: {filename}")
    _validate_header(binary, target)


def _validate_header(binary: bytes, target: str) -> None:
    _, _, format_name, machine = _TARGETS[target]
    matched = False
    if format_name == "elf":
        matched = (
            len(binary) >= 20
            and binary[:4] == b"\x7fELF"
            and binary[4] == 2
            and binary[5] == 1
            and int.from_bytes(binary[18:20], "little") == machine
        )
    elif format_name == "macho":
        matched = (
            len(binary) >= 8
            and binary[:4] == b"\xcf\xfa\xed\xfe"
            and int.from_bytes(binary[4:8], "little") == machine
        )
    elif len(binary) >= 64 and binary[:2] == b"MZ":
        offset = int.from_bytes(binary[60:64], "little")
        matched = (
            offset + 6 <= len(binary)
            and binary[offset : offset + 4] == b"PE\0\0"
            and int.from_bytes(binary[offset + 4 : offset + 6], "little") == machine
        )
    if not matched:
        raise NativeArtifactError(
            f"native executable header differs from its target: {target}"
        )


def _read(path: Path, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise NativeArtifactError(f"{label} is missing or unsafe: {path.name}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise NativeArtifactError(
            f"{label} cannot be opened safely: {path.name}: {error}"
        ) from error
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        declared = metadata.st_size
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < declared <= maximum
        ):
            raise NativeArtifactError(f"{label} is empty or exceeds its size limit")
        value = stream.read(maximum + 1)
    if len(value) != declared or len(value) > maximum:
        raise NativeArtifactError(f"{label} changed while being read")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify",))
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    try:
        value = load_native_artifacts(
            arguments.index,
            arguments.artifacts_dir,
            expected_source_revision=arguments.source_revision,
            expected_version=arguments.version,
        )
    except (NativeArtifactError, OSError, ValueError) as error:
        print(f"native artifact verification blocked: {error}", file=sys.stderr)
        return 2
    print(canonical_json({
        "fingerprint": value["fingerprint"],
        "status": "verified",
        "targets": len(value["artifacts"]),
    }).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
