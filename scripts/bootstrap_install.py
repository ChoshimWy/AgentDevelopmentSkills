#!/usr/bin/env python3
"""Download, verify, extract, and run one AgentDevelopmentSkills release bundle.

This file is intentionally self-contained so both the POSIX and PowerShell
bootstrap scripts can download and execute the same cross-platform core.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Optional
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen
import zipfile


if sys.version_info < (3, 11):
    raise SystemExit(
        "AgentDevelopmentSkills requires Python 3.11+; current interpreter is "
        f"Python {platform.python_version()}. Set AGENT_SKILLS_PYTHON to a compatible interpreter."
    )


DEFAULT_RELEASE_BASE_URL = (
    "https://choshimwy.github.io/AgentDevelopmentSkills/"
)
DEFAULT_MANIFEST_URL = DEFAULT_RELEASE_BASE_URL + "release-manifest.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
USER_AGENT = "agent-development-skills-bootstrap/1.0"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class BootstrapError(RuntimeError):
    """Raised when release acquisition cannot continue safely."""


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _exact_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise BootstrapError(f"{label} fields are invalid")
    return value


def parse_release_manifest(data: bytes) -> dict[str, Any]:
    """Validate canonical release-manifest-v1 bytes."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError(f"release manifest is not valid UTF-8 JSON: {error}") from error
    _exact_object(
        value,
        {
            "asset_base_url",
            "artifacts",
            "bootstrap_assets",
            "channel",
            "minimum_python",
            "product",
            "schema_version",
            "source",
            "version",
        },
        "release manifest",
    )
    if _canonical_json(value) != data:
        raise BootstrapError("release manifest must use canonical JSON encoding")
    if value["schema_version"] != "1.0":
        raise BootstrapError("unsupported release manifest schema_version")
    if value["product"] != "agent-development-skills":
        raise BootstrapError("release manifest product is invalid")
    _validate_asset_base_url(value["asset_base_url"], allow_test_file=True)
    if not isinstance(value["version"], str) or _VERSION.fullmatch(value["version"]) is None:
        raise BootstrapError("release manifest version is invalid")
    if value["channel"] not in {"stable", "beta", "development"}:
        raise BootstrapError("release manifest channel is invalid")
    if not isinstance(value["minimum_python"], str) or _VERSION.fullmatch(value["minimum_python"]) is None:
        raise BootstrapError("release manifest minimum_python is invalid")
    source = _exact_object(
        value["source"], {"dirty", "repository", "revision"}, "release manifest source"
    )
    if (
        not isinstance(source["dirty"], bool)
        or not isinstance(source["repository"], str)
        or not source["repository"]
        or not isinstance(source["revision"], str)
        or not source["revision"]
    ):
        raise BootstrapError("release manifest source is invalid")
    if source["dirty"] and value["channel"] != "development":
        raise BootstrapError("dirty release sources are allowed only on the development channel")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise BootstrapError("release manifest artifacts must not be empty")
    artifact_ids: set[str] = set()
    artifact_filenames: set[str] = set()
    for index, raw in enumerate(artifacts):
        artifact = _exact_object(
            raw,
            {
                "entrypoint",
                "filename",
                "format",
                "host_os",
                "id",
                "root",
                "sha256",
                "size",
            },
            f"release manifest artifact[{index}]",
        )
        if (
            not isinstance(artifact["id"], str)
            or not artifact["id"]
            or artifact["id"] in artifact_ids
        ):
            raise BootstrapError("release manifest artifact id is invalid or duplicated")
        artifact_ids.add(artifact["id"])
        filename = artifact["filename"]
        if (
            not isinstance(filename, str)
            or not filename
            or filename != Path(filename).name
            or filename in artifact_filenames
        ):
            raise BootstrapError("release manifest artifact filename is invalid or duplicated")
        artifact_filenames.add(filename)
        if artifact["format"] != "zip":
            raise BootstrapError("release manifest artifact format is unsupported")
        if type(artifact["size"]) is not int or not 0 < artifact["size"] <= MAX_ARTIFACT_BYTES:
            raise BootstrapError("release manifest artifact size is invalid")
        if not isinstance(artifact["sha256"], str) or _SHA256.fullmatch(artifact["sha256"]) is None:
            raise BootstrapError("release manifest artifact sha256 is invalid")
        for field in ("root", "entrypoint"):
            candidate = artifact[field]
            if not isinstance(candidate, str) or not _safe_relative_path(candidate):
                raise BootstrapError(f"release manifest artifact {field} is invalid")
        hosts = artifact["host_os"]
        if (
            not isinstance(hosts, list)
            or not hosts
            or hosts != sorted(set(hosts))
            or not set(hosts) <= {"darwin", "linux", "windows"}
        ):
            raise BootstrapError("release manifest artifact host_os is invalid")
    assets = value["bootstrap_assets"]
    if not isinstance(assets, list) or not assets:
        raise BootstrapError("release manifest bootstrap_assets must not be empty")
    asset_filenames: list[str] = []
    for index, raw in enumerate(assets):
        asset = _exact_object(
            raw, {"filename", "sha256", "size"}, f"release manifest bootstrap_assets[{index}]"
        )
        if (
            not isinstance(asset["filename"], str)
            or asset["filename"] != Path(asset["filename"]).name
            or type(asset["size"]) is not int
            or not 0 < asset["size"] <= MAX_MANIFEST_BYTES
            or not isinstance(asset["sha256"], str)
            or _SHA256.fullmatch(asset["sha256"]) is None
        ):
            raise BootstrapError("release manifest bootstrap asset is invalid")
        asset_filenames.append(asset["filename"])
    if asset_filenames != sorted(set(asset_filenames)):
        raise BootstrapError("release manifest bootstrap asset filenames must be sorted and unique")
    return value


def _safe_relative_path(value: str) -> bool:
    if "\\" in value or ":" in value:
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and path.as_posix() == value
        and all(
            part not in {"", ".", ".."}
            and not part.endswith((" ", "."))
            and part.split(".", 1)[0].casefold() not in _WINDOWS_RESERVED_NAMES
            for part in path.parts
        )
    )


def _validate_asset_base_url(value: Any, *, allow_test_file: bool = False) -> str:
    if not isinstance(value, str) or not value.endswith("/"):
        raise BootstrapError("release manifest asset_base_url is invalid")
    parsed = urlparse(value)
    allowed = {"https"} | ({"file"} if allow_test_file and _allow_file_urls() else set())
    if parsed.scheme not in allowed or (parsed.scheme == "https" and not parsed.netloc):
        raise BootstrapError("release manifest asset_base_url must use HTTPS")
    return value


def _host_os() -> str:
    name = platform.system().lower()
    if name.startswith("msys") or name.startswith("mingw"):
        return "windows"
    if name not in {"darwin", "linux", "windows"}:
        raise BootstrapError(f"unsupported host operating system: {name or 'unknown'}")
    return name


def select_artifact(manifest: dict[str, Any], *, host_os: Optional[str] = None) -> dict[str, Any]:
    selected_host = host_os or _host_os()
    matches = [item for item in manifest["artifacts"] if selected_host in item["host_os"]]
    if len(matches) != 1:
        raise BootstrapError(
            f"release manifest must provide exactly one artifact for host_os={selected_host}; found {len(matches)}"
        )
    return matches[0]


def _allow_file_urls() -> bool:
    return os.environ.get("AGENT_SKILLS_ALLOW_FILE_URL") == "1"


def fetch(url: str, *, maximum: int) -> tuple[bytes, str]:
    parsed = urlparse(url)
    allowed = {"https"} | ({"file"} if _allow_file_urls() else set())
    if parsed.scheme not in allowed:
        raise BootstrapError(f"unsupported or insecure download URL scheme: {parsed.scheme or 'missing'}")
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            final_scheme = urlparse(final_url).scheme
            if final_scheme not in allowed:
                raise BootstrapError("download redirected to an unsupported URL scheme")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum:
                raise BootstrapError("download exceeds the configured size limit")
            data = response.read(maximum + 1)
    except BootstrapError:
        raise
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise BootstrapError(f"download failed: {error}") from error
    if len(data) > maximum:
        raise BootstrapError("download exceeds the configured size limit")
    return data, final_url


def fetch_bytes(url: str, *, maximum: int) -> bytes:
    return fetch(url, maximum=maximum)[0]


def _artifact_url(asset_base_url: str, filename: str) -> str:
    return urljoin(asset_base_url, quote(filename))


def _verify_artifact(data: bytes, artifact: dict[str, Any]) -> None:
    if len(data) != artifact["size"]:
        raise BootstrapError("release artifact size does not match manifest")
    digest = hashlib.sha256(data).hexdigest()
    if digest != artifact["sha256"]:
        raise BootstrapError("release artifact sha256 does not match manifest")


def _safe_archive_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
        raise BootstrapError("release artifact entry count is invalid")
    names: set[str] = set()
    casefolded: set[str] = set()
    expanded_size = 0
    for entry in entries:
        name = entry.filename.rstrip("/")
        if not name or not _safe_relative_path(name):
            raise BootstrapError(f"release artifact contains an unsafe path: {entry.filename}")
        canonical_name = PurePosixPath(name).as_posix()
        collision_key = unicodedata.normalize("NFC", canonical_name).casefold()
        if name in names or collision_key in casefolded:
            raise BootstrapError(f"release artifact contains a duplicate path: {name}")
        names.add(name)
        casefolded.add(collision_key)
        file_type = stat.S_IFMT(entry.external_attr >> 16)
        if file_type == stat.S_IFLNK:
            raise BootstrapError(f"release artifact contains a symlink: {name}")
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise BootstrapError(f"release artifact contains an unsupported file type: {name}")
        expanded_size += entry.file_size
        if expanded_size > MAX_EXTRACTED_BYTES:
            raise BootstrapError("release artifact expands beyond the configured size limit")
    return entries


def extract_verified_artifact(data: bytes, artifact: dict[str, Any], destination: Path) -> Path:
    archive_path = destination / "release.zip"
    archive_path.write_bytes(data)
    extract_root = destination / "extracted"
    extract_root.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = _safe_archive_entries(archive)
            for entry in entries:
                target = extract_root.joinpath(*PurePosixPath(entry.filename.rstrip("/")).parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = (entry.external_attr >> 16) & 0o777
                if os.name != "nt" and mode:
                    target.chmod(mode)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise BootstrapError(f"release artifact extraction failed: {error}") from error
    bundle_root = extract_root.joinpath(*PurePosixPath(artifact["root"]).parts)
    entrypoint = bundle_root.joinpath(*PurePosixPath(artifact["entrypoint"]).parts)
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        raise BootstrapError("release artifact root is missing")
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise BootstrapError("release artifact entrypoint is missing")
    return entrypoint


def bootstrap_install(
    manifest_url: Optional[str],
    installer_arguments: list[str],
    *,
    bootstrap_dry_run: bool = False,
    manifest_file: Optional[Path] = None,
    artifact_base_url: Optional[str] = None,
) -> int:
    if manifest_file is not None:
        if manifest_file.is_symlink() or not manifest_file.is_file():
            raise BootstrapError("release manifest file is missing or unsafe")
        if manifest_file.stat().st_size > MAX_MANIFEST_BYTES:
            raise BootstrapError("release manifest exceeds the configured size limit")
        manifest_bytes = manifest_file.read_bytes()
        resolved_manifest_url = manifest_file.as_uri()
    else:
        if manifest_url is None:
            raise BootstrapError("release manifest URL is required")
        manifest_bytes, resolved_manifest_url = fetch(manifest_url, maximum=MAX_MANIFEST_BYTES)
    manifest = parse_release_manifest(manifest_bytes)
    minimum = tuple(int(part) for part in manifest["minimum_python"].split("."))
    if sys.version_info < minimum:
        raise BootstrapError(
            f"release requires Python {manifest['minimum_python']}+; current interpreter is "
            f"Python {platform.python_version()}"
        )
    artifact = select_artifact(manifest)
    selected_base_url = artifact_base_url or manifest["asset_base_url"]
    _validate_asset_base_url(selected_base_url, allow_test_file=True)
    artifact_url = _artifact_url(selected_base_url, artifact["filename"])
    if bootstrap_dry_run:
        sys.stdout.buffer.write(_canonical_json({
            "artifact": {
                "filename": artifact["filename"],
                "sha256": artifact["sha256"],
                "url": artifact_url,
            },
            "host_os": _host_os(),
            "manifest_url": resolved_manifest_url,
            "status": "planned",
            "version": manifest["version"],
        }))
        return 0
    artifact_bytes = fetch_bytes(artifact_url, maximum=MAX_ARTIFACT_BYTES)
    _verify_artifact(artifact_bytes, artifact)
    with tempfile.TemporaryDirectory(prefix="agent-skills-bootstrap-") as directory:
        entrypoint = extract_verified_artifact(artifact_bytes, artifact, Path(directory))
        environment = {
            **os.environ,
            "AGENT_SKILLS_RELEASE_SHA256": artifact["sha256"],
            "AGENT_SKILLS_RELEASE_VERSION": manifest["version"],
        }
        completed = subprocess.run(
            [sys.executable, str(entrypoint), *installer_arguments],
            env=environment,
            check=False,
        )
        return completed.returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verified AgentDevelopmentSkills release bootstrap",
        add_help=False,
    )
    manifest_group = parser.add_mutually_exclusive_group()
    manifest_group.add_argument(
        "--manifest-url",
    )
    manifest_group.add_argument("--manifest-file", type=Path)
    parser.add_argument("--artifact-base-url")
    parser.add_argument("--bootstrap-dry-run", action="store_true")
    parser.add_argument("--bootstrap-json", action="store_true")
    parser.add_argument("--bootstrap-help", action="store_true")
    arguments, installer_arguments = parser.parse_known_args(argv)
    if arguments.bootstrap_help:
        parser.print_help()
        return 0
    try:
        manifest_url = arguments.manifest_url
        if manifest_url is None and arguments.manifest_file is None:
            manifest_url = os.environ.get("AGENT_SKILLS_RELEASE_MANIFEST_URL", DEFAULT_MANIFEST_URL)
        if arguments.manifest_file is not None and arguments.artifact_base_url is None:
            raise BootstrapError("--manifest-file requires --artifact-base-url")
        return bootstrap_install(
            manifest_url,
            installer_arguments,
            bootstrap_dry_run=arguments.bootstrap_dry_run,
            manifest_file=arguments.manifest_file,
            artifact_base_url=arguments.artifact_base_url,
        )
    except (BootstrapError, OSError, subprocess.SubprocessError) as error:
        if arguments.bootstrap_json:
            sys.stderr.buffer.write(_canonical_json({"error": str(error), "status": "blocked"}))
        else:
            print(f"AgentDevelopmentSkills bootstrap blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
