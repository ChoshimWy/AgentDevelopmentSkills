#!/usr/bin/env python3
"""Build byte-stable wheel and sdist artifacts without network build dependencies."""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import tomllib
from typing import Iterable
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOTS = (
    "disciplines", "migration", "platforms", "providers", "runtime-configs",
    "schemas", "scripts", "src", "tests",
)
DATA_FILES = (
    ".github/workflows/conformance.yml", ".github/workflows/publish-release.yml",
    "README.md", "agent_build_backend.py",
    "pyproject.toml", "skill-naming-policy.json",
    "install.sh", "install.ps1", "uninstall.sh",
)
IGNORED_NAMES = {".DS_Store", "__pycache__"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class PythonArtifactError(RuntimeError):
    pass


def _project(root: Path) -> tuple[str, str, str]:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    name = project["name"]
    version = project["version"]
    description = project["description"]
    if not all(isinstance(item, str) and item for item in (name, version, description)):
        raise PythonArtifactError("pyproject project identity is invalid")
    return name, version, description


def _iter_root(root: Path, relative_root: str) -> Iterable[Path]:
    source = root / relative_root
    if source.is_symlink() or not source.is_dir():
        raise PythonArtifactError(f"distribution root is missing or unsafe: {relative_root}")
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in IGNORED_NAMES for part in relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise PythonArtifactError(f"distribution source contains a symlink: {relative.as_posix()}")
        if path.is_file():
            yield path


def distribution_files(root: Path) -> tuple[Path, ...]:
    paths: dict[str, Path] = {}
    for relative_root in DATA_ROOTS:
        for path in _iter_root(root, relative_root):
            paths[path.relative_to(root).as_posix()] = path
    for relative in DATA_FILES:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise PythonArtifactError(f"distribution file is missing or unsafe: {relative}")
        paths[relative] = path
    return tuple(paths[key] for key in sorted(paths))


def _mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _record_digest(value: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def _wheel_bytes(root: Path) -> tuple[str, bytes]:
    name, version, description = _project(root)
    normalized = name.replace("-", "_")
    filename = f"{normalized}-{version}-py3-none-any.whl"
    dist_info = f"{normalized}-{version}.dist-info"
    data_prefix = f"{normalized}-{version}.data/data/share/agent-workflow"
    entries: dict[str, tuple[bytes, int]] = {}
    for path in distribution_files(root):
        relative = path.relative_to(root).as_posix()
        entries[f"{data_prefix}/{relative}"] = (path.read_bytes(), _mode(path))
    package_root = root / "src" / "agent_workflow"
    for path in _iter_root(root, "src/agent_workflow"):
        relative = path.relative_to(package_root).as_posix()
        entries[f"agent_workflow/{relative}"] = (path.read_bytes(), _mode(path))
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        f"Summary: {description}\n"
        "Requires-Python: >=3.11\n\n"
    ).encode("utf-8")
    wheel = (
        "Wheel-Version: 1.0\n"
        "Generator: agent-development-skills deterministic-builder-v1\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")
    entry_points = (
        "[console_scripts]\n"
        "agent-skills = agent_workflow.cli:main\n"
        "agent-session = agent_workflow.worktree_sessions.cli:main\n"
        "agent-workflow = agent_workflow.cli:main\n"
    ).encode("utf-8")
    entries[f"{dist_info}/METADATA"] = (metadata, 0o644)
    entries[f"{dist_info}/WHEEL"] = (wheel, 0o644)
    entries[f"{dist_info}/entry_points.txt"] = (entry_points, 0o644)
    entries[f"{dist_info}/top_level.txt"] = (b"agent_workflow\n", 0o644)
    record_path = f"{dist_info}/RECORD"
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    for relative in sorted(entries):
        value = entries[relative][0]
        writer.writerow((relative, _record_digest(value), len(value)))
    writer.writerow((record_path, "", ""))
    entries[record_path] = (record_buffer.getvalue().encode("utf-8"), 0o644)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative in sorted(entries):
            value, mode = entries[relative]
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, value)
    return filename, buffer.getvalue()


def _sdist_bytes(root: Path) -> tuple[str, bytes]:
    name, version, description = _project(root)
    normalized = name.replace("-", "_")
    filename = f"{normalized}-{version}.tar.gz"
    archive_root = f"{normalized}-{version}"
    files = {path.relative_to(root).as_posix(): path for path in distribution_files(root)}
    pkg_info = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        f"Summary: {description}\n"
        "Requires-Python: >=3.11\n\n"
    ).encode("utf-8")
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for relative in sorted([*files, "PKG-INFO"]):
            value = pkg_info if relative == "PKG-INFO" else files[relative].read_bytes()
            mode = 0o644 if relative == "PKG-INFO" else _mode(files[relative])
            info = tarfile.TarInfo(f"{archive_root}/{relative}")
            info.size = len(value)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(value))
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        compressed.write(tar_buffer.getvalue())
    return filename, buffer.getvalue()


def build_python_artifacts(root: Path, output: Path) -> list[dict[str, object]]:
    root = root.resolve()
    output = Path(os.path.abspath(output.expanduser()))
    if output.is_symlink():
        raise PythonArtifactError("Python artifact output must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for kind, builder in (("wheel", _wheel_bytes), ("sdist", _sdist_bytes)):
        filename, value = builder(root)
        destination = output / filename
        if destination.exists() or destination.is_symlink():
            raise PythonArtifactError(f"Python artifact already exists: {filename}")
        destination.write_bytes(value)
        destination.chmod(0o644)
        artifacts.append({
            "filename": filename,
            "kind": kind,
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        })
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        artifacts = build_python_artifacts(ROOT, arguments.output)
    except (OSError, KeyError, PythonArtifactError) as error:
        parser.error(str(error))
    import json
    print(json.dumps({"artifacts": artifacts, "status": "built"}, sort_keys=True, separators=(",", ":")) + "\n", end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
