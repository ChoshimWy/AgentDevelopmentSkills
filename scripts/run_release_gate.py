#!/usr/bin/env python3
"""Evaluate the fail-closed Phase 6 release-candidate gate."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable
import unicodedata
import zipfile

try:
    import resource
except ImportError:  # pragma: no cover - Windows is not a production release host.
    resource = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_workflow.canonical_json import dump, dumps, load, sha256  # noqa: E402
from agent_workflow.contracts import (  # noqa: E402
    validate_upgrade_conformance_evidence,
    validate_upgrade_source_qualification,
)
from agent_workflow.models import ContractError  # noqa: E402

import bootstrap_install  # noqa: E402
import native_artifact_contract  # noqa: E402
import python_compatibility_evidence  # noqa: E402


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIXED_METADATA_FILES = {
    "provenance.json", "python-artifacts.json", "release-manifest.json", "sbom.json",
}
_OPTIONAL_METADATA_FILES = {
    "native-artifacts.json",
    "upgrade-source-qualification.json",
}
_EXPECTED_BOOTSTRAP_FILES = {
    "bootstrap_install.py",
    "install.ps1",
    "install.sh",
    "uninstall.sh",
}
_FIXED_RELEASE_FILES = _FIXED_METADATA_FILES | _EXPECTED_BOOTSTRAP_FILES
_MAX_RELEASE_FILES = 24
_MAX_RELEASE_BYTES = 1024 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_SDIST_BYTES = 128 * 1024 * 1024
_MAX_SDIST_ENTRIES = 10000
_MAX_SDIST_EXPANDED_BYTES = 512 * 1024 * 1024
_MAX_CANDIDATE_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_CANDIDATE_FILE_BYTES = _MAX_SDIST_BYTES
_RSA_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_POSIX_METADATA_BEGIN = "# BEGIN agent-skills embedded release metadata"
_POSIX_METADATA_END = "# END agent-skills embedded release metadata"
_POWERSHELL_METADATA_BEGIN = "# BEGIN agent-skills embedded release metadata"
_POWERSHELL_METADATA_END = "# END agent-skills embedded release metadata"


def _expected_posix_bootstrap(
    source: bytes,
    *,
    manifest: dict[str, Any],
    native_index: dict[str, Any],
) -> bytes:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("source POSIX bootstrap must be valid UTF-8") from error
    begin = text.find(_POSIX_METADATA_BEGIN)
    end = text.find(_POSIX_METADATA_END)
    if (
        begin < 0
        or end < begin
        or text.find(_POSIX_METADATA_BEGIN, begin + 1) >= 0
        or text.find(_POSIX_METADATA_END, end + 1) >= 0
    ):
        raise ContractError("source POSIX bootstrap embedded metadata block is invalid")
    end += len(_POSIX_METADATA_END)
    source_artifact = manifest["artifacts"][0]
    expected = [
        _POSIX_METADATA_BEGIN,
        f"AGENT_SKILLS_EMBEDDED_VERSION={shlex.quote(manifest['version'])}",
        "AGENT_SKILLS_EMBEDDED_ASSET_BASE_URL="
        + shlex.quote(manifest["asset_base_url"]),
        "AGENT_SKILLS_EMBEDDED_SOURCE_FILENAME="
        + shlex.quote(source_artifact["filename"]),
        "AGENT_SKILLS_EMBEDDED_SOURCE_SHA256="
        + shlex.quote(source_artifact["sha256"]),
        "AGENT_SKILLS_EMBEDDED_SOURCE_SIZE="
        + shlex.quote(str(source_artifact["size"])),
        "AGENT_SKILLS_EMBEDDED_SOURCE_ROOT="
        + shlex.quote(source_artifact["root"]),
        "agent_skills_select_native_record() {",
        '    case "$1" in',
    ]
    for record in sorted(native_index["artifacts"], key=lambda item: item["target"]):
        expected.extend([
            f"        {shlex.quote(record['target'])})",
            f"            native_filename={shlex.quote(record['filename'])}",
            f"            native_sha256={shlex.quote(record['sha256'])}",
            f"            native_size={shlex.quote(str(record['size']))}",
            "            return 0",
            "            ;;",
        ])
    expected.extend([
        "    esac",
        "    return 1",
        "}",
        _POSIX_METADATA_END,
    ])
    return (text[:begin] + "\n".join(expected) + text[end:]).encode("utf-8")


def _powershell_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _expected_powershell_bootstrap(
    source: bytes,
    *,
    manifest: dict[str, Any],
    native_index: dict[str, Any],
) -> bytes:
    """Rebuild the immutable PowerShell native-executable allowlist."""

    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("source PowerShell bootstrap must be valid UTF-8") from error
    begin = text.find(_POWERSHELL_METADATA_BEGIN)
    end = text.find(_POWERSHELL_METADATA_END)
    if (
        begin < 0
        or end < begin
        or text.find(_POWERSHELL_METADATA_BEGIN, begin + 1) >= 0
        or text.find(_POWERSHELL_METADATA_END, end + 1) >= 0
    ):
        raise ContractError("source PowerShell bootstrap embedded metadata block is invalid")
    end += len(_POWERSHELL_METADATA_END)
    expected = [
        _POWERSHELL_METADATA_BEGIN,
        "$script:AgentSkillsEmbeddedVersion = "
        + _powershell_single_quoted(manifest["version"]),
        "$script:AgentSkillsEmbeddedAssetBaseUrl = "
        + _powershell_single_quoted(manifest["asset_base_url"]),
        "$script:AgentSkillsEmbeddedNativeArtifacts = @("
    ]
    for record in sorted(native_index["artifacts"], key=lambda item: item["target"]):
        expected.append(
            "    [pscustomobject]@{ "
            f"Filename = {_powershell_single_quoted(record['filename'])}; "
            f"Sha256 = {_powershell_single_quoted(record['sha256'])}; "
            f"Size = {record['size']}; "
            f"Target = {_powershell_single_quoted(record['target'])} "
            "}"
        )
    expected.extend([
        ")",
        _POWERSHELL_METADATA_END,
    ])
    return (text[:begin] + "\n".join(expected) + text[end:]).encode("utf-8")


def _terminate_candidate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            process.kill()
    if process.poll() is None:
        process.wait()


def _run_candidate_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run reviewer-authorized candidate code with process and resource bounds.

    This is defense in depth, not a hostile-code sandbox.  The signed review is
    the authorization boundary and must be verified before this helper is used.
    """

    def limits() -> None:
        if resource is None:
            return
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (_MAX_CANDIDATE_FILE_BYTES + 1, _MAX_CANDIDATE_FILE_BYTES + 1),
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        cpu_limit = max(1, timeout)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))

    with tempfile.TemporaryDirectory(prefix="agent-skills-candidate-command-") as directory:
        stdout_path = Path(directory) / "stdout"
        stderr_path = Path(directory) / "stderr"
        with stdout_path.open("w+b") as stdout, stderr_path.open("w+b") as stderr:
            process: subprocess.Popen[bytes] | None = None
            violation: str | None = None
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name == "posix",
                    preexec_fn=limits if os.name == "posix" else None,
                )
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if (
                        os.fstat(stdout.fileno()).st_size > _MAX_CANDIDATE_OUTPUT_BYTES
                        or os.fstat(stderr.fileno()).st_size > _MAX_CANDIDATE_OUTPUT_BYTES
                    ):
                        violation = "candidate command exceeded the output limit"
                        break
                    if time.monotonic() >= deadline:
                        violation = "candidate command timed out"
                        break
                    time.sleep(0.05)
                if violation is not None:
                    raise ContractError(violation + ": " + " ".join(command))
                return_code = process.wait()
            finally:
                if process is not None:
                    _terminate_candidate_process(process)
            stdout_size = os.fstat(stdout.fileno()).st_size
            stderr_size = os.fstat(stderr.fileno()).st_size
            if stdout_size > _MAX_CANDIDATE_OUTPUT_BYTES or stderr_size > _MAX_CANDIDATE_OUTPUT_BYTES:
                raise ContractError("candidate command exceeded the output limit: " + " ".join(command))
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(
                command,
                return_code,
                stdout.read().decode("utf-8", errors="replace"),
                stderr.read().decode("utf-8", errors="replace"),
            )


def _safe_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value
        and value == Path(value).name
        and "\\" not in value
        and ":" not in value
        and value not in {".", ".."}
    )


def _native_binary_filename(value: str) -> bool:
    return (
        value.startswith("agent-skills-")
        and any(
            value.endswith(target + (".exe" if "-windows-" in target else ""))
            for target in native_artifact_contract.EXPECTED_TARGETS
        )
    )


def _release_directory_identity(path: Path) -> dict[str, dict[str, Any]]:
    if path.is_symlink() or not path.is_dir():
        raise ContractError("release directory is missing or unsafe")
    entries: dict[str, dict[str, Any]] = {}
    candidates = sorted(path.iterdir(), key=lambda candidate: candidate.name)
    if len(candidates) > _MAX_RELEASE_FILES:
        raise ContractError("release directory exceeds the file-count limit")
    total_size = 0
    for item in candidates:
        if item.is_symlink() or not item.is_file() or not _safe_filename(item.name):
            raise ContractError(f"release directory contains an unsafe entry: {item.name}")
        if (
            item.name not in _FIXED_RELEASE_FILES
            and item.name not in _OPTIONAL_METADATA_FILES
            and not item.name.endswith((".zip", ".whl", ".tar.gz"))
            and not _native_binary_filename(item.name)
        ):
            raise ContractError(f"release directory contains an unknown entry: {item.name}")
        declared_size = item.stat().st_size
        total_size += declared_size
        if declared_size < 0 or declared_size > _MAX_RELEASE_BYTES or total_size > _MAX_RELEASE_BYTES:
            raise ContractError("release directory exceeds the snapshot size limit")
        value = item.read_bytes()
        if len(value) != declared_size:
            raise ContractError("release directory changed while reading its identity")
        entries[item.name] = {
            "mode": item.stat().st_mode & 0o777,
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    missing = sorted(_FIXED_RELEASE_FILES - set(entries))
    if missing:
        raise ContractError("release directory is missing required files: " + ", ".join(missing))
    return entries


def _snapshot_release(source: Path, destination: Path) -> dict[str, dict[str, Any]]:
    identity = _release_directory_identity(source)
    destination.mkdir()
    for name, record in identity.items():
        value = (source / name).read_bytes()
        if len(value) != record["size"] or hashlib.sha256(value).hexdigest() != record["sha256"]:
            raise ContractError("release directory changed while creating its snapshot")
        output = destination / name
        output.write_bytes(value)
        output.chmod(record["mode"])
    return identity


def _snapshot_file(source: Path | None, destination: Path) -> tuple[Path | None, dict[str, Any] | None]:
    if source is None:
        return None, None
    source = Path(os.path.abspath(source.expanduser()))
    if source.is_symlink() or not source.is_file():
        raise ContractError("release gate evidence input is missing or unsafe")
    declared_size = source.stat().st_size
    if declared_size > _MAX_EVIDENCE_BYTES:
        raise ContractError("release gate evidence input exceeds the size limit")
    value = source.read_bytes()
    if len(value) != declared_size or len(value) > _MAX_EVIDENCE_BYTES:
        raise ContractError("release gate evidence input changed while being snapshotted")
    identity = {"mode": source.stat().st_mode & 0o777, "sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    destination.write_bytes(value)
    destination.chmod(identity["mode"])
    return destination, {"path": str(source), **identity}


def _evidence_unchanged(identity: dict[str, Any] | None) -> bool:
    if identity is None:
        return True
    try:
        path = Path(identity["path"])
        if path.is_symlink() or not path.is_file():
            return False
        value = path.read_bytes()
        return (
            len(value) == identity["size"]
            and hashlib.sha256(value).hexdigest() == identity["sha256"]
            and path.stat().st_mode & 0o777 == identity["mode"]
        )
    except (OSError, TypeError, ValueError, KeyError):
        return False


def _snapshot_input_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.is_symlink() or not path.is_file():
        raise ContractError("release gate snapshot input is missing or unsafe")
    value = path.read_bytes()
    return {
        "mode": path.stat().st_mode & 0o777,
        "path": str(path),
        "sha256": hashlib.sha256(value).hexdigest(),
        "size": len(value),
    }


def _canonical(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"release metadata must be a JSON object: {path.name}")
    if bootstrap_install._canonical_json(value) != raw:
        raise ContractError(f"release metadata is not canonical JSON: {path.name}")
    return value


def _fingerprinted(value: dict[str, Any], label: str) -> None:
    if value.get("fingerprint") != sha256({key: item for key, item in value.items() if key != "fingerprint"}):
        raise ContractError(f"{label} fingerprint mismatch")


def _artifact(path: Path, record: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"release artifact is missing or unsafe: {path.name}")
    value = path.read_bytes()
    if len(value) != record.get("size") or hashlib.sha256(value).hexdigest() != record.get("sha256"):
        raise ContractError(f"release artifact differs from metadata: {path.name}")


def _validate_python_artifacts(value: dict[str, Any]) -> list[dict[str, Any]]:
    if set(value) != {"artifacts", "fingerprint", "product", "schema_version", "version"}:
        raise ContractError("Python artifact index fields are invalid")
    if value.get("schema_version") != "1.0" or value.get("product") != "agent-development-skills" or not isinstance(value.get("version"), str):
        raise ContractError("Python artifact index product/version is invalid")
    _fingerprinted(value, "python artifacts")
    records = value.get("artifacts")
    if not isinstance(records, list) or len(records) != 2:
        raise ContractError("Python artifact index requires exactly one wheel and one sdist")
    observed: set[tuple[str, str]] = set()
    filenames: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"filename", "kind", "sha256", "size"}:
            raise ContractError("Python artifact record fields are invalid")
        kind = record.get("kind")
        filename = record.get("filename")
        if (
            kind not in {"sdist", "wheel"}
            or not _safe_filename(filename)
            or filename in filenames
            or (kind == "wheel" and not filename.endswith(".whl"))
            or (kind == "sdist" and not filename.endswith(".tar.gz"))
            or type(record.get("size")) is not int
            or record["size"] <= 0
            or not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(record["sha256"]) is None
        ):
            raise ContractError("Python artifact record identity is invalid")
        filenames.add(filename)
        observed.add((kind, filename))
    if {item[0] for item in observed} != {"sdist", "wheel"}:
        raise ContractError("Python artifact kinds are incomplete")
    return records


def _validate_sbom(value: dict[str, Any]) -> None:
    expected = {
        "exclusions", "files", "fingerprint", "format", "product",
        "schema_version", "source_revision", "version",
    }
    if set(value) != expected or value.get("schema_version") != "1.0" or value.get("format") != "agent-skills-sbom-v1":
        raise ContractError("SBOM fields or version are invalid")
    if value.get("product") != "agent-development-skills" or not all(
        isinstance(value.get(field), str) and value[field] for field in ("version", "source_revision")
    ):
        raise ContractError("SBOM product/source identity is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not files or len(files) > _MAX_SDIST_ENTRIES:
        raise ContractError("SBOM files must be a bounded non-empty array")
    paths = []
    for record in files:
        if not isinstance(record, dict) or set(record) != {"classification", "mode", "path", "sha256", "size"}:
            raise ContractError("SBOM file record fields are invalid")
        if (
            record.get("classification") != "redistributable-source"
            or record.get("mode") not in {0o644, 0o755}
            or not isinstance(record.get("path"), str)
            or not bootstrap_install._safe_relative_path(record["path"])
            or type(record.get("size")) is not int
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(record["sha256"]) is None
        ):
            raise ContractError("SBOM file record identity is invalid")
        paths.append(record["path"])
    if paths != sorted(set(paths)):
        raise ContractError("SBOM paths must be sorted and unique")
    exclusions = value.get("exclusions")
    if not isinstance(exclusions, list):
        raise ContractError("SBOM exclusions must be an array")
    exclusion_ids = []
    for item in exclusions:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "reason"}
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("reason"), str)
            or not item["reason"]
        ):
            raise ContractError("SBOM exclusion record is invalid")
        exclusion_ids.append(item["id"])
    if len(exclusion_ids) != len(set(exclusion_ids)):
        raise ContractError("SBOM exclusion ids must be unique")
    _fingerprinted(value, "SBOM")


def _validate_provenance(value: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {
        "artifacts", "builder", "fingerprint", "materials_sha256", "product",
        "reproducible", "sbom_sha256", "schema_version", "source", "version",
    }
    schema_version = value.get("schema_version")
    if (
        set(value) != expected
        or schema_version not in {"1.0", "2.0"}
        or value.get("product") != "agent-development-skills"
    ):
        raise ContractError("provenance fields or version are invalid")
    if value.get("reproducible") is not True or not isinstance(value.get("version"), str) or not value["version"]:
        raise ContractError("provenance reproducibility/version is invalid")
    source = value.get("source")
    if (
        not isinstance(source, dict)
        or set(source) != {"dirty", "repository", "revision"}
        or not isinstance(source.get("dirty"), bool)
        or any(not isinstance(source.get(field), str) or not source[field] for field in ("repository", "revision"))
    ):
        raise ContractError("provenance source is invalid")
    builder = value.get("builder")
    if (
        not isinstance(builder, dict)
        or set(builder) != {"id", "sha256"}
        or builder.get("id") != "agent-development-skills.release-builder-v1"
        or not isinstance(builder.get("sha256"), str)
        or _SHA256.fullmatch(builder["sha256"]) is None
    ):
        raise ContractError("provenance builder is invalid")
    for field in ("materials_sha256", "sbom_sha256"):
        if not isinstance(value.get(field), str) or _SHA256.fullmatch(value[field]) is None:
            raise ContractError(f"provenance {field} is invalid")
    records = value.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ContractError("provenance artifacts must be a non-empty array")
    filenames = []
    qualification_count = 0
    allowed_kinds = {"bootstrap", "native-binary", "sdist", "source-bundle", "wheel"}
    if schema_version == "2.0":
        allowed_kinds.add("upgrade-source-qualification")
    for record in records:
        if not isinstance(record, dict) or set(record) != {"filename", "kind", "sha256", "size"}:
            raise ContractError("provenance artifact record fields are invalid")
        if (
            record.get("kind") not in allowed_kinds
            or not _safe_filename(record.get("filename"))
            or type(record.get("size")) is not int
            or record["size"] <= 0
            or not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(record["sha256"]) is None
        ):
            raise ContractError("provenance artifact record identity is invalid")
        qualification_count += record["kind"] == "upgrade-source-qualification"
        filenames.append(record["filename"])
    if len(filenames) != len(set(filenames)):
        raise ContractError("provenance artifact filenames must be unique")
    if qualification_count != (1 if schema_version == "2.0" else 0):
        raise ContractError(
            "provenance Upgrade Source Qualification count differs from its version"
        )
    _fingerprinted(value, "provenance")
    return records


def _safe_extract_sdist(path: Path, destination: Path) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SDIST_BYTES:
        raise ContractError("sdist is missing, unsafe, or exceeds the compressed size limit")
    roots: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > _MAX_SDIST_ENTRIES:
            raise ContractError("sdist exceeds the entry-count limit")
        normalized_names: set[str] = set()
        expanded_size = 0
        for member in members:
            relative = PurePosixPath(member.name)
            normalized = unicodedata.normalize("NFC", member.name).casefold()
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or not bootstrap_install._safe_relative_path(member.name)
                or unicodedata.normalize("NFC", member.name) != member.name
                or normalized in normalized_names
                or not member.isfile()
                or member.issym()
                or member.islnk()
                or member.size < 0
                or member.size > _MAX_SDIST_EXPANDED_BYTES
                or member.mode & 0o777 not in {0o644, 0o755}
            ):
                raise ContractError("sdist contains an unsafe member")
            normalized_names.add(normalized)
            roots.add(relative.parts[0])
            expanded_size += member.size
            if expanded_size > _MAX_SDIST_EXPANDED_BYTES:
                raise ContractError("sdist exceeds the expanded size limit")
        if len(roots) != 1:
            raise ContractError("sdist must contain exactly one root")
        for member in members:
            relative = PurePosixPath(member.name)
            source = archive.extractfile(member)
            if source is None:
                raise ContractError("sdist member cannot be read")
            value = source.read(member.size + 1)
            if len(value) != member.size:
                raise ContractError("sdist member size differs from its header")
            output = destination.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(value)
            output.chmod(member.mode & 0o777)
    return destination / next(iter(roots))


def _python_distribution_smoke(
    release: Path,
    records: list[dict[str, Any]],
    sbom_files: list[dict[str, Any]],
    frozen_artifacts: dict[str, bytes],
    state: dict[str, Any],
) -> dict[str, Any]:
    wheel = next((item for item in records if item.get("kind") == "wheel"), None)
    sdist = next((item for item in records if item.get("kind") == "sdist"), None)
    if wheel is None or sdist is None:
        raise ContractError("release must contain one wheel and one sdist")
    with tempfile.TemporaryDirectory(prefix="agent-skills-release-gate-") as directory:
        root = Path(directory)
        frozen_root = root / "frozen"
        frozen_root.mkdir(mode=0o700)
        sdist_path = frozen_root / sdist["filename"]
        sdist_path.write_bytes(frozen_artifacts[sdist["filename"]])
        sdist_path.chmod(0o400)
        wheel_path = frozen_root / wheel["filename"]
        wheel_path.write_bytes(frozen_artifacts[wheel["filename"]])
        wheel_path.chmod(0o400)
        source = _safe_extract_sdist(sdist_path, root / "source")
        observed_source = []
        for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
            if path.is_symlink():
                raise ContractError("sdist extracted a symlink")
            if not path.is_file() or path.name == "PKG-INFO" and path.parent == source:
                continue
            value = path.read_bytes()
            observed_source.append({
                "classification": "redistributable-source",
                "mode": path.stat().st_mode & 0o777,
                "path": path.relative_to(source).as_posix(),
                "sha256": hashlib.sha256(value).hexdigest(),
                "size": len(value),
            })
        if observed_source != sbom_files:
            raise ContractError("sdist source tree differs from the bound SBOM materials")
        migration_audit = load(source / "migration/ios-agent-skills-map-v2.json")
        if migration_audit.get("schema_version") != "2.0" or migration_audit.get("content_sha256") != sha256({
            key: value for key, value in migration_audit.items() if key != "content_sha256"
        }):
            raise ContractError("migration audit identity is invalid")
        license_record = migration_audit["source"]["license"]
        license_verified = False
        if license_record.get("status") == "verified":
            notice_value = license_record.get("notice_path")
            if not isinstance(notice_value, str):
                raise ContractError("verified License/NOTICE path is invalid")
            notice_relative = PurePosixPath(notice_value)
            if notice_relative.is_absolute() or ".." in notice_relative.parts or notice_relative.as_posix() != notice_value:
                raise ContractError("verified License/NOTICE path is unsafe")
            notice = source.joinpath(*notice_relative.parts)
            if notice.is_symlink() or not notice.is_file():
                raise ContractError("verified License/NOTICE file is missing or unsafe")
            if hashlib.sha256(notice.read_bytes()).hexdigest() != license_record.get("notice_sha256"):
                raise ContractError("verified License/NOTICE hash differs")
            if not isinstance(license_record.get("spdx"), str) or not license_record["spdx"]:
                raise ContractError("verified License SPDX identity is missing")
            license_verified = True
        venv = root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            timeout=120,
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        installed = _run_candidate_command(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel_path)],
            timeout=300,
        )
        if installed.returncode:
            raise ContractError("wheel offline install failed: " + installed.stderr.strip())
        executable = scripts / ("agent-skills.exe" if os.name == "nt" else "agent-skills")
        target = root / "target"
        commands = (
            [str(executable), "--help"],
            [str(executable), "install", "--platform", "apple", "--platform", "desktop", "--target-root", str(target)],
            [str(executable), "doctor", "--target-root", str(target)],
        )
        for command in commands:
            result = _run_candidate_command(command, timeout=120)
            if result.returncode:
                raise ContractError("installed wheel smoke failed: " + (result.stderr.strip() or result.stdout.strip()))
        rebuilt = root / "rebuilt"
        completed = _run_candidate_command(
            [sys.executable, str(source / "scripts/build_python_artifacts.py"), "--output", str(rebuilt)],
            cwd=source,
            timeout=300,
        )
        if completed.returncode:
            raise ContractError("sdist rebuild failed: " + (completed.stderr.strip() or completed.stdout.strip()))
        for record in records:
            if frozen_artifacts[record["filename"]] != (rebuilt / record["filename"]).read_bytes():
                raise ContractError(f"sdist rebuild is not byte-identical: {record['filename']}")
        installed_lock = load(target / ".agent-skills" / "agent-skills.lock")
        state["package_lock"] = installed_lock
        return {
            "license": license_record,
            "license_verified": license_verified,
            "package_lock_hash": installed_lock["fingerprint"],
            "python": ".".join(str(item) for item in sys.version_info[:2]),
            "schema_inventory_hash": installed_lock["schema_inventory"]["content_sha256"],
            "sdist_rebuild": "byte-identical",
            "wheel_smoke": "passed",
        }


def _execute_candidate_conformance(
    source_artifact_bytes: bytes,
    source_artifact: dict[str, Any],
    package_lock: dict[str, Any],
) -> dict[str, Any]:
    """Execute the bound repository-owned suite; fingerprints alone are not attestations."""

    with tempfile.TemporaryDirectory(prefix="agent-skills-rc-conformance-") as directory:
        root = Path(directory)
        bootstrap_install._verify_artifact(source_artifact_bytes, source_artifact)
        entrypoint = bootstrap_install.extract_verified_artifact(
            source_artifact_bytes, source_artifact, root
        )
        source = entrypoint.parents[1]
        lock_path = root / "candidate.lock"
        dump(package_lock, lock_path)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source / "src")
        completed = _run_candidate_command(
            [sys.executable, str(source / "scripts/run_conformance.py"), "--upgrade-lock", str(lock_path)],
            cwd=source,
            env=environment,
            timeout=1800,
        )
        if completed.returncode:
            raise ContractError(
                "release candidate Conformance execution failed: "
                + (completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
            )
        try:
            evidence = json.loads(completed.stdout)
        except (TypeError, ValueError) as error:
            raise ContractError("release candidate Conformance returned invalid JSON") from error
        if not isinstance(evidence, dict):
            raise ContractError("release candidate Conformance receipt must be an object")
        validate_upgrade_conformance_evidence(evidence)
        return evidence


def _compare_conformance_receipts(
    supplied: dict[str, Any],
    executed: dict[str, Any],
) -> None:
    fields = {
        "candidate_package_lock_hash", "manifest_count", "negative_contract_count",
        "runner_sha256", "schema_inventory_hash", "status", "suite",
        "suite_definition_hash", "test_count",
    }
    if any(supplied.get(field) != executed.get(field) for field in fields):
        raise ContractError("supplied Conformance evidence differs from the executed release suite")
    supplied_commands = [
        (item["command"], item["exit_code"]) for item in supplied["command_results"]
    ]
    executed_commands = [
        (item["command"], item["exit_code"]) for item in executed["command_results"]
    ]
    if supplied_commands != executed_commands:
        raise ContractError("supplied Conformance command set differs from the executed release suite")


def _review_trust_store(path: Path) -> dict[str, dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise ContractError("release review trust store is missing or unsafe")
    value = load(path)
    if set(value) != {"fingerprint", "keys", "schema_version"} or value.get("schema_version") != "1.0":
        raise ContractError("release review trust store contract is invalid")
    _fingerprinted(value, "release review trust store")
    keys = value.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > 128:
        raise ContractError("release review trust store key count is invalid")
    result: dict[str, dict[str, Any]] = {}
    expected = {
        "algorithm", "exponent", "key_id", "modulus_hex", "owner", "scopes", "status",
    }
    for key in keys:
        if not isinstance(key, dict) or set(key) != expected:
            raise ContractError("release review trust key contract is invalid")
        modulus_hex = key.get("modulus_hex")
        exponent = key.get("exponent")
        scopes = key.get("scopes")
        if (
            key.get("algorithm") != "rsa-pkcs1v15-sha256"
            or not isinstance(modulus_hex, str)
            or re.fullmatch(r"[0-9a-f]+", modulus_hex) is None
            or len(modulus_hex) < 512
            or len(modulus_hex) > 2048
            or len(modulus_hex) % 2
            or modulus_hex.startswith("00")
            or int(modulus_hex, 16).bit_length() < 2048
            or int(modulus_hex, 16) % 2 == 0
            or not isinstance(exponent, int)
            or isinstance(exponent, bool)
            or exponent != 65537
            or not isinstance(key.get("owner"), str)
            or not key["owner"]
            or not isinstance(scopes, list)
            or scopes != sorted(set(scopes))
            or "phase-6-release" not in scopes
            or key.get("status") not in {"trusted", "revoked"}
        ):
            raise ContractError("release review trust key is invalid")
        expected_key_id = sha256({
            "algorithm": key["algorithm"],
            "exponent": exponent,
            "modulus_hex": modulus_hex,
        })
        if key.get("key_id") != expected_key_id or expected_key_id in result:
            raise ContractError("release review trust key identity is invalid or duplicated")
        result[expected_key_id] = key
    return result


def _review_signature_payload(value: dict[str, Any]) -> bytes:
    signature = value["signature"]
    payload = {
        key: item for key, item in value.items()
        if key not in {"fingerprint", "signature"}
    }
    payload["signature"] = {
        "algorithm": signature["algorithm"],
        "key_id": signature["key_id"],
    }
    return dumps(payload).encode("utf-8")


def _verify_review_signature(value: dict[str, Any], key: dict[str, Any]) -> None:
    signature = value["signature"]
    signature_hex = signature["value_hex"]
    modulus = int(key["modulus_hex"], 16)
    width = (modulus.bit_length() + 7) // 8
    if (
        re.fullmatch(r"[0-9a-f]+", signature_hex) is None
        or len(signature_hex) != width * 2
        or int(signature_hex, 16) >= modulus
    ):
        raise ContractError("release review signature encoding is invalid")
    encoded = pow(int(signature_hex, 16), key["exponent"], modulus).to_bytes(width, "big")
    digest_info = _RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(
        _review_signature_payload(value)
    ).digest()
    padding_length = width - len(digest_info) - 3
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    if padding_length < 8 or not hmac.compare_digest(encoded, expected):
        raise ContractError("release review signature is invalid")


def _review_evidence(path: Path, trust_store_path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise ContractError("release review evidence is missing or unsafe")
    value = load(path)
    expected = {
        "schema_version", "scope", "reviewer", "status", "blockers",
        "reviewed_release_identity_sha256", "source_revision",
        "python_compatibility_evidence_fingerprint", "signature", "fingerprint",
    }
    if set(value) != expected or value.get("schema_version") != "3.0":
        raise ContractError("release review evidence contract is invalid")
    signature = value.get("signature")
    if (
        value.get("scope") != "phase-6-release"
        or value.get("reviewer") != "independent"
        or value.get("status") != "approved"
        or value.get("blockers") != []
        or not isinstance(value.get("reviewed_release_identity_sha256"), str)
        or len(value["reviewed_release_identity_sha256"]) != 64
        or not isinstance(value.get("source_revision"), str)
        or not value["source_revision"]
        or not isinstance(value.get("python_compatibility_evidence_fingerprint"), str)
        or _SHA256.fullmatch(value["python_compatibility_evidence_fingerprint"]) is None
        or not isinstance(signature, dict)
        or set(signature) != {"algorithm", "key_id", "value_hex"}
        or signature.get("algorithm") != "rsa-pkcs1v15-sha256"
        or not isinstance(signature.get("key_id"), str)
        or _SHA256.fullmatch(signature["key_id"]) is None
        or not isinstance(signature.get("value_hex"), str)
    ):
        raise ContractError("release review evidence is not an approval")
    _fingerprinted(value, "release review evidence")
    trust_store = _review_trust_store(trust_store_path)
    key = trust_store.get(signature["key_id"])
    if key is None:
        raise ContractError("release review signer is not in the external trust store")
    if key["status"] != "trusted":
        raise ContractError("release review signer is revoked")
    _verify_review_signature(value, key)
    return value


def _validate_release_gate_report(value: dict[str, Any]) -> None:
    if set(value) != {
        "blockers", "checks", "fingerprint", "release_identity_sha256", "schema_version", "status"
    } or value.get("schema_version") != "1.0":
        raise ContractError("release-gate report fields are invalid")
    checks = value.get("checks")
    check_ids = [item.get("id") for item in checks] if isinstance(checks, list) else []
    if (
        not isinstance(checks, list)
        or any(not isinstance(item, str) for item in check_ids)
        or check_ids != sorted(set(check_ids))
    ):
        raise ContractError("release-gate checks must be sorted and unique")
    blocked = []
    for item in checks:
        if (
            not isinstance(item, dict)
            or set(item) != {"details", "id", "status"}
            or not isinstance(item["details"], dict)
            or not isinstance(item["id"], str)
            or not item["id"].startswith("release.")
            or item["status"] not in {"blocked", "passed"}
        ):
            raise ContractError("release-gate check is invalid")
        if item["status"] == "blocked":
            blocked.append(item["id"])
    if value.get("blockers") != sorted(blocked) or value.get("status") != ("blocked" if blocked else "passed"):
        raise ContractError("release-gate status differs from its checks")
    if not isinstance(value.get("release_identity_sha256"), str) or _SHA256.fullmatch(value["release_identity_sha256"]) is None:
        raise ContractError("release-gate identity is invalid")
    _fingerprinted(value, "release-gate report")


def _blocked_release_gate(check_id: str, error: Exception) -> dict[str, Any]:
    report: dict[str, Any] = {
        "blockers": [check_id],
        "checks": [{"details": {"error": str(error)}, "id": check_id, "status": "blocked"}],
        "release_identity_sha256": sha256({"blocked": check_id, "error": str(error)}),
        "schema_version": "1.0",
        "status": "blocked",
    }
    report["fingerprint"] = sha256(report)
    _validate_release_gate_report(report)
    return report


def _evaluate_release_gate_snapshot(
    release: Path,
    *,
    conformance_evidence: Path | None,
    python_compatibility_evidence_path: Path | None,
    review_evidence: Path | None,
    review_trust_store: Path | None,
) -> dict[str, Any]:
    release = release.resolve()
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    bound: dict[str, Any] = {}

    def check(check_id: str, action: Callable[[], Any]) -> Any | None:
        try:
            details = action()
        except (
            ContractError,
            native_artifact_contract.NativeArtifactError,
            OSError,
            TypeError,
            AttributeError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
            bootstrap_install.BootstrapError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ) as error:
            blockers.append(check_id)
            checks.append({"details": {"error": str(error)}, "id": check_id, "status": "blocked"})
            return None
        checks.append({"details": details if isinstance(details, dict) else {}, "id": check_id, "status": "passed"})
        return details

    manifest = check("release.manifest", lambda: bootstrap_install.parse_release_manifest((release / "release-manifest.json").read_bytes()))

    def supply_chain() -> dict[str, Any]:
        python_index = _canonical(release / "python-artifacts.json")
        sbom = _canonical(release / "sbom.json")
        provenance = _canonical(release / "provenance.json")
        python_records = _validate_python_artifacts(python_index)
        _validate_sbom(sbom)
        provenance_records = _validate_provenance(provenance)
        if "xcode-official-export-content" not in {item["id"] for item in sbom["exclusions"]}:
            raise ContractError("SBOM must explicitly exclude local Xcode official export content")
        if not isinstance(manifest, dict):
            raise ContractError("release manifest is unavailable for supply-chain binding")
        expected_provenance_version = (
            "2.0" if manifest.get("schema_version") == "3.0" else "1.0"
        )
        if provenance.get("schema_version") != expected_provenance_version:
            raise ContractError(
                "release manifest and provenance versions are not cross-bound"
            )
        native_index_path = release / "native-artifacts.json"
        native_index: dict[str, Any] | None = None
        native_records: list[dict[str, Any]] = []
        if native_index_path.exists() or native_index_path.is_symlink():
            native_index = native_artifact_contract.load_native_artifacts(
                native_index_path,
                release,
                expected_source_revision=manifest.get("source", {}).get("revision", ""),
                expected_version=manifest.get("version", ""),
            )
            native_records = [
                {
                    "filename": item["filename"],
                    "kind": "native-binary",
                    "sha256": item["sha256"],
                    "size": item["size"],
                }
                for item in native_index["artifacts"]
            ]
            manifest_native_records = [
                {
                    "arch": item["arch"],
                    "filename": item["filename"],
                    "os": item["os"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "target": item["target"],
                }
                for item in native_index["artifacts"]
            ]
            if (
                manifest.get("schema_version") not in {"2.0", "3.0"}
                or manifest.get("default_engine") != "rust"
                or manifest.get("native_artifacts") != manifest_native_records
                or manifest.get("native_index_sha256")
                != hashlib.sha256(native_index_path.read_bytes()).hexdigest()
            ):
                raise ContractError(
                    "release manifest native execution contract differs from the verified matrix"
                )
        elif manifest.get("channel") != "development":
            raise ContractError("beta and stable releases require the complete native artifact matrix")
        elif manifest.get("schema_version") != "1.0":
            raise ContractError(
                "development releases without a native matrix must use the fallback manifest"
            )
        if (
            len(manifest.get("artifacts", [])) != 1
            or manifest["artifacts"][0].get("id") != "universal-source-bundle"
        ):
            raise ContractError("this release contract requires exactly one universal source artifact")
        expected_repository = manifest.get("source", {}).get("repository")
        expected_version = manifest.get("version")
        expected_source_name = f"agent-development-skills-{expected_version}.zip"
        expected_source_root = f"agent-development-skills-{expected_version}"
        source_contract = manifest["artifacts"][0]
        if (
            not isinstance(expected_repository, str)
            or not expected_repository.startswith("https://")
            or manifest.get("asset_base_url")
            != f"{expected_repository.rstrip('/')}/releases/download/v{expected_version}/"
            or source_contract.get("format") != "zip"
            or source_contract.get("filename") != expected_source_name
            or source_contract.get("root") != expected_source_root
            or source_contract.get("entrypoint") != "scripts/install_local.py"
            or source_contract.get("host_os") != ["darwin", "linux"]
        ):
            raise ContractError("release manifest source execution contract differs from the frozen policy")
        if {item["filename"] for item in manifest.get("bootstrap_assets", [])} != _EXPECTED_BOOTSTRAP_FILES:
            raise ContractError("release manifest bootstrap asset set differs from the frozen contract")
        if (
            python_index.get("product") != manifest["product"]
            or python_index.get("version") != manifest["version"]
            or sbom.get("product") != manifest["product"]
            or sbom.get("version") != manifest["version"]
            or sbom.get("source_revision") != manifest["source"]["revision"]
            or provenance.get("product") != manifest["product"]
            or provenance.get("version") != manifest["version"]
            or provenance.get("source") != manifest["source"]
        ):
            raise ContractError("release metadata product/version/source identities differ")
        if provenance["sbom_sha256"] != hashlib.sha256((release / "sbom.json").read_bytes()).hexdigest():
            raise ContractError("provenance SBOM binding differs")
        if provenance.get("materials_sha256") != sha256(sbom["files"]):
            raise ContractError("provenance materials hash differs from SBOM")
        if native_index is not None:
            cargo_lock = next(
                (item for item in sbom["files"] if item["path"] == "Cargo.lock"),
                None,
            )
            if cargo_lock is None or any(
                item["cargo_lock_sha256"] != cargo_lock["sha256"]
                for item in native_index["artifacts"]
            ):
                raise ContractError(
                    "native artifact Cargo.lock differs from the bound source SBOM"
                )
        source_record = {
            "filename": manifest["artifacts"][0]["filename"],
            "kind": "source-bundle",
            "sha256": manifest["artifacts"][0]["sha256"],
            "size": manifest["artifacts"][0]["size"],
        }
        bootstrap_records = [
            {**item, "kind": "bootstrap"} for item in manifest["bootstrap_assets"]
        ]
        source_qualification: dict[str, Any] | None = None
        qualification_records: list[dict[str, Any]] = []
        if manifest.get("schema_version") == "3.0":
            qualification_record = manifest["upgrade_source_qualification"]
            qualification_path = release / qualification_record["filename"]
            _artifact(qualification_path, qualification_record)
            source_qualification = _canonical(qualification_path)
            validate_upgrade_source_qualification(source_qualification)
            qualification_records.append({
                **qualification_record,
                "kind": "upgrade-source-qualification",
            })
        expected_artifacts = sorted(
            [
                source_record,
                *native_records,
                *python_records,
                *bootstrap_records,
                *qualification_records,
            ],
            key=lambda item: (item["kind"], item["filename"]),
        )
        if (
            provenance_records != expected_artifacts
            or any(not _safe_filename(item.get("filename")) for item in provenance_records)
            or len({item["filename"] for item in provenance_records}) != len(provenance_records)
        ):
            raise ContractError(
                "manifest, native/Python indexes and provenance artifact records differ"
            )
        expected_release_files = _FIXED_METADATA_FILES | {
            item["filename"] for item in provenance_records
        }
        if native_records:
            expected_release_files.add("native-artifacts.json")
        actual_release_files = {item.name for item in release.iterdir()}
        if actual_release_files != expected_release_files:
            raise ContractError("release directory files differ from the exact provenance allowlist")
        frozen_artifacts: dict[str, bytes] = {}
        for record in provenance["artifacts"]:
            _artifact(release / record["filename"], record)
            frozen_artifacts[record["filename"]] = (release / record["filename"]).read_bytes()
        source_artifact = manifest["artifacts"][0]
        _artifact(release / source_artifact["filename"], source_artifact)
        with tempfile.TemporaryDirectory(prefix="agent-skills-source-audit-") as directory:
            artifact_bytes = (release / source_artifact["filename"]).read_bytes()
            bootstrap_install._verify_artifact(artifact_bytes, source_artifact)
            bootstrap_install.extract_verified_artifact(
                artifact_bytes, source_artifact, Path(directory)
            )
            bundle = Path(directory) / "extracted" / source_artifact["root"]
            observed = []
            for path in sorted(bundle.rglob("*"), key=lambda item: item.relative_to(bundle).as_posix()):
                if path.is_symlink():
                    raise ContractError("source bundle extracted a symlink")
                if not path.is_file():
                    continue
                value = path.read_bytes()
                observed.append({
                    "classification": "redistributable-source",
                    "mode": path.stat().st_mode & 0o777,
                    "path": path.relative_to(bundle).as_posix(),
                    "sha256": hashlib.sha256(value).hexdigest(),
                    "size": len(value),
                })
            bootstrap_sources = {
                "bootstrap_install.py": bundle / "scripts/bootstrap_install.py",
                "install.ps1": bundle / "install.ps1",
                "install.sh": bundle / "install.sh",
                "uninstall.sh": bundle / "uninstall.sh",
            }
            for standalone, source_path in bootstrap_sources.items():
                if source_path.is_symlink() or not source_path.is_file():
                    raise ContractError(f"source bundle bootstrap is missing or unsafe: {standalone}")
                expected = source_path.read_bytes()
                if standalone in {"install.sh", "uninstall.sh"} and native_index is not None:
                    expected = _expected_posix_bootstrap(
                        expected,
                        manifest=manifest,
                        native_index=native_index,
                    )
                elif standalone == "install.ps1" and native_index is not None:
                    expected = _expected_powershell_bootstrap(
                        expected,
                        manifest=manifest,
                        native_index=native_index,
                    )
                if (release / standalone).read_bytes() != expected:
                    raise ContractError(f"standalone bootstrap differs from source SBOM materials: {standalone}")
        if sorted(observed, key=lambda item: item["path"]) != sbom["files"]:
            raise ContractError("source bundle contents differ from SBOM")
        sbom_by_path = {item["path"]: item for item in sbom["files"]}
        builder = sbom_by_path.get("scripts/build_release_bundle.py")
        runner = sbom_by_path.get("scripts/run_conformance.py")
        if builder is None or runner is None or provenance["builder"]["sha256"] != builder["sha256"]:
            raise ContractError("builder or Conformance runner is absent from the bound SBOM")
        if source_qualification is not None and (
            source_qualification["runner_sha256"] != runner["sha256"]
            or source_qualification["source_materials_sha256"]
            != provenance["materials_sha256"]
            or source_qualification["source"] != {
                "artifact_sha256": source_artifact["sha256"],
                "artifact_size": source_artifact["size"],
                "revision": manifest["source"]["revision"],
                "root": source_artifact["root"],
            }
        ):
            raise ContractError(
                "Upgrade Source Qualification differs from the frozen source release"
            )
        bound.update({
            "manifest": manifest,
            "frozen_artifacts": frozen_artifacts,
            "python_index": python_index,
            "python_records": python_records,
            "runner_sha256": runner["sha256"],
            "sbom_files": sbom["files"],
            "source_artifact": source_artifact,
            "source_artifact_bytes": artifact_bytes,
            "source_qualification": source_qualification,
        })
        return {"artifact_count": len(provenance["artifacts"]), "file_count": len(sbom["files"])}

    supply_result = check("release.supply-chain", supply_chain)
    release_identity = sha256(_release_directory_identity(release))
    compatibility_value: dict[str, Any] | None = None

    def python_compatibility() -> dict[str, Any]:
        nonlocal compatibility_value
        if python_compatibility_evidence_path is None:
            raise ContractError("Python 3.11–3.14 compatibility evidence is required")
        value = python_compatibility_evidence.validate_evidence(
            load(python_compatibility_evidence_path),
            require_complete=True,
        )
        manifest_source = bound.get("manifest", {}).get("source", {})
        if value["source_revision"] != manifest_source.get("revision"):
            raise ContractError("Python compatibility evidence source differs from the release")
        python_records = bound.get("python_records")
        normalized_python_records = (
            sorted(python_records, key=lambda item: (item["kind"], item["filename"]))
            if isinstance(python_records, list)
            else None
        )
        if normalized_python_records is None or any(
            environment["artifacts"] != normalized_python_records
            for environment in value["environments"]
        ):
            raise ContractError("Python compatibility evidence artifacts differ from the release")
        compatibility_value = value
        return {
            "fingerprint": value["fingerprint"],
            "python_versions": [item["python_minor"] for item in value["environments"]],
        }

    compatibility_result = check("release.python-compatibility", python_compatibility)
    review_result = check(
        "release.independent-review",
        lambda: (
            {"fingerprint": review["fingerprint"], "key_id": review["signature"]["key_id"]}
            if (
                review_trust_store is not None
                and (review := _review_evidence(review_evidence, review_trust_store))["reviewed_release_identity_sha256"] == release_identity
                and review["source_revision"] == bound.get("manifest", {}).get("source", {}).get("revision")
                and compatibility_value is not None
                and review["python_compatibility_evidence_fingerprint"] == compatibility_value["fingerprint"]
            )
            else (_ for _ in ()).throw(ContractError("independent review is not bound to this release candidate"))
        ) if review_evidence is not None and review_trust_store is not None else (_ for _ in ()).throw(
            ContractError("signed independent review evidence and an external trust store are required")
        ),
    )
    supplied_evidence: dict[str, Any] | None = None
    conformance_input_error: Exception | None = None
    try:
        if conformance_evidence is None:
            raise ContractError("candidate-bound Conformance evidence is required")
        candidate_evidence = load(conformance_evidence)
        validate_upgrade_conformance_evidence(candidate_evidence)
        if candidate_evidence["runner_sha256"] != bound.get("runner_sha256"):
            raise ContractError("Conformance evidence runner is not bound to this release candidate")
        supplied_evidence = candidate_evidence
    except (
        ContractError, OSError, TypeError, AttributeError, ValueError, KeyError,
    ) as error:
        conformance_input_error = error

    execution_state: dict[str, Any] = {}
    if (
        supply_result is not None
        and compatibility_result is not None
        and review_result is not None
        and supplied_evidence is not None
    ):
        smoke = check(
            "release.python-distribution",
            lambda: _python_distribution_smoke(
                release,
                bound["python_records"],
                bound["sbom_files"],
                bound["frozen_artifacts"],
                execution_state,
            ),
        )
    else:
        blockers.append("release.python-distribution")
        checks.append({
            "details": {
                "error": "supply-chain, Python matrix, signed review and candidate-bound evidence must pass before artifact execution"
            },
            "id": "release.python-distribution",
            "status": "blocked",
        })
        smoke = None

    def conformance() -> dict[str, Any]:
        if conformance_input_error is not None:
            raise ContractError(str(conformance_input_error))
        if supplied_evidence is None:
            raise ContractError("candidate-bound Conformance evidence is unavailable")
        value = supplied_evidence
        if not isinstance(smoke, dict):
            if review_result is None:
                raise ContractError("candidate execution was not authorized by signed review")
            raise ContractError("candidate distribution execution did not complete")
        if (
            value["candidate_package_lock_hash"] != smoke["package_lock_hash"]
            or value["schema_inventory_hash"] != smoke["schema_inventory_hash"]
            or value["runner_sha256"] != bound.get("runner_sha256")
        ):
            raise ContractError("Conformance evidence is not bound to this release candidate")
        source_qualification = bound.get("source_qualification")
        if manifest.get("schema_version") == "3.0":
            shared_fields = {
                "command_results",
                "environment",
                "manifest_count",
                "negative_contract_count",
                "runner_sha256",
                "schema_inventory_hash",
                "status",
                "suite",
                "suite_definition_hash",
                "test_count",
            }
            if not isinstance(source_qualification, dict) or any(
                source_qualification.get(field) != value.get(field)
                for field in shared_fields
            ):
                raise ContractError(
                    "Upgrade Source Qualification differs from candidate Conformance"
                )
        package_lock = execution_state.get("package_lock")
        if not isinstance(package_lock, dict):
            raise ContractError("installed release Package Lock is unavailable for Conformance")
        executed = _execute_candidate_conformance(
            bound["source_artifact_bytes"], bound["source_artifact"], package_lock
        )
        _compare_conformance_receipts(value, executed)
        return {
            "attestation_key": executed["attestation_key"],
            "test_count": executed["test_count"],
        }

    check("release.conformance", conformance)

    def license_gate() -> dict[str, Any]:
        license_record = smoke["license"] if isinstance(smoke, dict) else None
        if (
            not isinstance(license_record, dict)
            or license_record.get("status") != "verified"
            or not smoke.get("license_verified")
        ):
            raise ContractError("License/NOTICE provenance is not verified")
        if not license_record.get("spdx") or not license_record.get("notice_path") or not license_record.get("notice_sha256"):
            raise ContractError("License/NOTICE provenance is incomplete")
        return {"spdx": license_record["spdx"], "status": "verified"}

    check("release.license-notice", license_gate)
    if not isinstance(manifest, dict) or manifest.get("channel") not in {"beta", "stable"} or manifest.get("source", {}).get("dirty"):
        blockers.append("release.source-policy")
        checks.append({
            "details": {"error": "release candidate requires a clean beta/stable source"},
            "id": "release.source-policy",
            "status": "blocked",
        })
    else:
        checks.append({"details": {"channel": manifest["channel"]}, "id": "release.source-policy", "status": "passed"})
    checks.sort(key=lambda item: item["id"])
    report: dict[str, Any] = {
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "release_identity_sha256": release_identity,
        "schema_version": "1.0",
        "status": "passed" if not blockers else "blocked",
    }
    report["fingerprint"] = sha256(report)
    _validate_release_gate_report(report)
    return report


def evaluate_release_gate(
    release: Path,
    *,
    conformance_evidence: Path | None,
    python_compatibility_evidence_path: Path | None = None,
    review_evidence: Path | None = None,
    review_trust_store: Path | None = None,
) -> dict[str, Any]:
    original = Path(os.path.abspath(release.expanduser()))
    with tempfile.TemporaryDirectory(prefix="agent-skills-release-snapshot-") as directory:
        root = Path(directory)
        snapshot = root / "release"
        try:
            if review_trust_store is not None:
                trust_source = Path(os.path.abspath(review_trust_store.expanduser()))
                if trust_source == original or trust_source.is_relative_to(original):
                    raise ContractError("release review trust store must remain outside the candidate")
            original_identity = _snapshot_release(original, snapshot)
            evidence_snapshot, evidence_identity = _snapshot_file(
                conformance_evidence, root / "conformance-evidence.json"
            )
            compatibility_snapshot, compatibility_identity = _snapshot_file(
                python_compatibility_evidence_path,
                root / "python-compatibility-evidence.json",
            )
            review_snapshot, review_identity = _snapshot_file(
                review_evidence, root / "review-evidence.json"
            )
            trust_snapshot, trust_identity = _snapshot_file(
                review_trust_store, root / "review-trust-store.json"
            )
            evidence_snapshot_identity = _snapshot_input_identity(evidence_snapshot)
            compatibility_snapshot_identity = _snapshot_input_identity(compatibility_snapshot)
            review_snapshot_identity = _snapshot_input_identity(review_snapshot)
            trust_snapshot_identity = _snapshot_input_identity(trust_snapshot)
        except (ContractError, OSError, TypeError, AttributeError, ValueError) as error:
            return _blocked_release_gate("release.snapshot", error)
        report = _evaluate_release_gate_snapshot(
            snapshot,
            conformance_evidence=evidence_snapshot,
            python_compatibility_evidence_path=compatibility_snapshot,
            review_evidence=review_snapshot,
            review_trust_store=trust_snapshot,
        )
        try:
            unchanged = (
                _release_directory_identity(original) == original_identity
                and _release_directory_identity(snapshot) == original_identity
            )
        except (ContractError, OSError):
            unchanged = False
        unchanged = (
            unchanged
            and _evidence_unchanged(evidence_identity)
            and _evidence_unchanged(compatibility_identity)
            and _evidence_unchanged(review_identity)
            and _evidence_unchanged(trust_identity)
            and _evidence_unchanged(evidence_snapshot_identity)
            and _evidence_unchanged(compatibility_snapshot_identity)
            and _evidence_unchanged(review_snapshot_identity)
            and _evidence_unchanged(trust_snapshot_identity)
        )
        if not unchanged:
            report["checks"].append({
                "details": {"error": "release candidate or evidence changed during gate evaluation"},
                "id": "release.snapshot-stability",
                "status": "blocked",
            })
            report["checks"].sort(key=lambda item: item["id"])
            report["blockers"] = sorted({*report["blockers"], "release.snapshot-stability"})
            report["status"] = "blocked"
            report["fingerprint"] = sha256({
                key: value for key, value in report.items() if key != "fingerprint"
            })
        _validate_release_gate_report(report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--conformance-evidence", type=Path)
    parser.add_argument("--python-compatibility-evidence", type=Path)
    parser.add_argument("--review-evidence", type=Path)
    parser.add_argument("--review-trust-store", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = evaluate_release_gate(
            args.release_dir,
            conformance_evidence=args.conformance_evidence,
            python_compatibility_evidence_path=args.python_compatibility_evidence,
            review_evidence=args.review_evidence,
            review_trust_store=args.review_trust_store,
        )
    except (
        ContractError,
        OSError,
        TypeError,
        AttributeError,
        ValueError,
        KeyError,
        bootstrap_install.BootstrapError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
        return 2
    if args.output:
        dump(report, args.output)
    print(dumps(report), end="")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
