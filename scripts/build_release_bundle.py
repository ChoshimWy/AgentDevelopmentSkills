#!/usr/bin/env python3
"""Build a deterministic, bootstrap-verifiable source release bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable
import unicodedata
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY = "https://github.com/ChoshimWy/AgentDevelopmentSkills"
RELEASE_ROOTS = (
    "crates",
    "disciplines",
    "migration",
    "platforms",
    "providers",
    "runtime-configs",
    "schemas",
    "scripts",
    "src/agent_workflow",
    "tests",
)
RELEASE_FILES = (
    ".github/workflows/conformance.yml",
    ".github/workflows/publish-release.yml",
    "Cargo.lock",
    "Cargo.toml",
    "LICENSE",
    "NOTICE",
    "README.md",
    "README.zh-CN.md",
    "agent_build_backend.py",
    "docs/architecture.md",
    "docs/multi-session-worktree.md",
    "docs/rust-migration.md",
    "docs/skill-naming.md",
    "install.ps1",
    "install.sh",
    "pyproject.toml",
    "rust-toolchain.toml",
    "skill-naming-policy.json",
    "uninstall.sh",
)
BOOTSTRAP_FILES = ("install.sh", "install.ps1", "scripts/bootstrap_install.py")
IGNORED_NAMES = {".DS_Store", "__pycache__"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
DEFAULT_HOST_OS = ("darwin", "linux")
WINDOWS_RELEASE_ENABLED = False


class ReleaseBuildError(RuntimeError):
    """Raised when a release cannot be built deterministically."""


def _load_bootstrap_module():
    path = ROOT / "scripts/bootstrap_install.py"
    spec = importlib.util.spec_from_file_location("agent_skills_bootstrap_contract", path)
    if spec is None or spec.loader is None:
        raise ReleaseBuildError("cannot load bootstrap manifest contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_python_artifact_module():
    path = ROOT / "scripts/build_python_artifacts.py"
    spec = importlib.util.spec_from_file_location("agent_skills_python_artifact_builder", path)
    if spec is None or spec.loader is None:
        raise ReleaseBuildError("cannot load Python artifact builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_native_artifact_module():
    path = ROOT / "scripts/native_artifact_contract.py"
    spec = importlib.util.spec_from_file_location("agent_skills_native_artifact_contract", path)
    if spec is None or spec.loader is None:
        raise ReleaseBuildError("cannot load native artifact contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_json(value: Any) -> bytes:
    bootstrap = _load_bootstrap_module()
    return bootstrap._canonical_json(value)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        value = tomllib.load(stream)["project"]["version"]
    if not isinstance(value, str) or not value:
        raise ReleaseBuildError("pyproject project.version is invalid")
    return value


def _git_value(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_identity(root: Path) -> tuple[str, bool]:
    try:
        revision = _git_value(root, "rev-parse", "HEAD")
        dirty = bool(_git_value(root, "status", "--porcelain", "--untracked-files=all"))
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseBuildError(f"cannot determine source identity: {error}") from error
    return revision, dirty


def _iter_tree(root: Path, relative_root: str) -> Iterable[Path]:
    source = root / relative_root
    if source.is_symlink() or not source.is_dir():
        raise ReleaseBuildError(f"release root is missing or unsafe: {relative_root}")
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in IGNORED_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            raise ReleaseBuildError(f"release source contains a symlink: {relative.as_posix()}")
        if path.is_file():
            yield path


def release_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for relative_root in RELEASE_ROOTS:
        paths.extend(_iter_tree(root, relative_root))
    for relative_file in RELEASE_FILES:
        path = root / relative_file
        if path.is_symlink() or not path.is_file():
            raise ReleaseBuildError(f"release file is missing or unsafe: {relative_file}")
        paths.append(path)
    unique = {path.relative_to(root).as_posix(): path for path in paths}
    casefolded: dict[str, str] = {}
    for relative in sorted(unique):
        key = unicodedata.normalize("NFC", relative).casefold()
        if key in casefolded:
            raise ReleaseBuildError(
                f"release paths collide on a case-insensitive host: {casefolded[key]}, {relative}"
            )
        casefolded[key] = relative
    return [unique[name] for name in sorted(unique)]


def _git_file_modes(root: Path) -> dict[str, int]:
    try:
        output = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseBuildError(f"cannot read canonical git file modes: {error}") from error
    modes: dict[str, int] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        relative = raw_path.decode("utf-8")
        modes[relative] = 0o755 if mode == b"100755" else 0o644
    return modes


def _git_blob(root: Path, relative: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseBuildError(f"cannot read canonical git blob: {relative}: {error}") from error


def _file_mode(path: Path, relative: str, git_modes: dict[str, int]) -> int:
    if relative in git_modes:
        return git_modes[relative]
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _write_zip(root: Path, destination: Path, *, bundle_root: str, clean_source: bool) -> None:
    git_modes = _git_file_modes(root)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in release_files(root):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(f"{bundle_root}/{relative}", date_time=FIXED_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | _file_mode(path, relative, git_modes)) << 16
            content = _git_blob(root, relative) if clean_source else path.read_bytes()
            archive.writestr(info, content)


def build_release_bundle(
    root: Path,
    output: Path,
    *,
    allow_dirty: bool = False,
    channel: str = "stable",
    host_os: tuple[str, ...] = DEFAULT_HOST_OS,
    repository: str = DEFAULT_REPOSITORY,
    native_artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    output = Path(os.path.abspath(output.expanduser()))
    boundary_output = output.resolve(strict=False)
    if boundary_output == root or root.is_relative_to(boundary_output):
        raise ReleaseBuildError("release output must not be the source root or its ancestor")
    if output.exists() or output.is_symlink():
        if output.is_symlink():
            raise ReleaseBuildError("release output path must not be a symlink")
        raise ReleaseBuildError("release output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve() / output.name
    if output == root or root.is_relative_to(output):
        raise ReleaseBuildError("release output must not be the source root or its ancestor")
    if output.exists() or output.is_symlink():
        raise ReleaseBuildError("resolved release output must not already exist")
    revision, dirty = _source_identity(root)
    if dirty and not allow_dirty:
        raise ReleaseBuildError("release source is dirty; commit scoped changes or pass --allow-dirty for development only")
    if dirty and channel != "development":
        raise ReleaseBuildError("dirty release sources must use the development channel")
    selected_hosts = tuple(sorted(set(host_os)))
    if not selected_hosts or not set(selected_hosts) <= {"darwin", "linux", "windows"}:
        raise ReleaseBuildError("release host_os is invalid")
    if "windows" in selected_hosts and not WINDOWS_RELEASE_ENABLED:
        raise ReleaseBuildError("windows release artifacts remain blocked until Windows Conformance is enabled")
    version = _version(root)
    asset_base_url = f"{repository.rstrip('/')}/releases/download/v{version}/"
    bundle_root = f"agent-development-skills-{version}"
    artifact_name = f"{bundle_root}.zip"
    with tempfile.TemporaryDirectory(prefix="agent-skills-release-", dir=output.parent) as directory:
        stage = Path(directory) / "release"
        stage.mkdir()
        artifact_path = stage / artifact_name
        _write_zip(root, artifact_path, bundle_root=bundle_root, clean_source=not dirty)
        python_artifacts = _load_python_artifact_module().build_python_artifacts(root, stage)
        native_artifacts = []
        if native_artifacts_dir is not None:
            native_root = Path(os.path.abspath(native_artifacts_dir.expanduser()))
            native = _load_native_artifact_module()
            try:
                native_index = native.load_native_artifacts(
                    native_root / "native-artifacts.json",
                    native_root,
                    expected_source_revision=revision,
                    expected_version=version,
                )
            except native.NativeArtifactError as error:
                raise ReleaseBuildError(str(error)) from error
            expected_cargo_lock = _sha256(
                _git_blob(root, "Cargo.lock")
                if not dirty
                else (root / "Cargo.lock").read_bytes()
            )
            if any(
                record["cargo_lock_sha256"] != expected_cargo_lock
                for record in native_index["artifacts"]
            ):
                raise ReleaseBuildError(
                    "native artifact Cargo.lock differs from the release source"
                )
            index_bytes = (native_root / "native-artifacts.json").read_bytes()
            if index_bytes != native.canonical_json(native_index):
                raise ReleaseBuildError(
                    "native artifact index changed after validation"
                )
            (stage / "native-artifacts.json").write_bytes(index_bytes)
            for record in native_index["artifacts"]:
                source = native_root / record["filename"]
                destination = stage / record["filename"]
                if source.is_symlink() or not source.is_file():
                    raise ReleaseBuildError(
                        f"native executable changed after validation: {record['filename']}"
                    )
                value = source.read_bytes()
                if (
                    len(value) != record["size"]
                    or _sha256(value) != record["sha256"]
                ):
                    raise ReleaseBuildError(
                        f"native executable changed after validation: {record['filename']}"
                    )
                destination.write_bytes(value)
                native_artifacts.append({
                    "filename": record["filename"],
                    "kind": "native-binary",
                    "sha256": record["sha256"],
                    "size": record["size"],
                })
        bootstrap_assets = []
        for relative in BOOTSTRAP_FILES:
            source = root / relative
            if source.is_symlink() or not source.is_file():
                raise ReleaseBuildError(f"bootstrap asset is missing or unsafe: {relative}")
            filename = "bootstrap_install.py" if relative == "scripts/bootstrap_install.py" else source.name
            destination = stage / filename
            data = _git_blob(root, relative) if not dirty else source.read_bytes()
            destination.write_bytes(data)
            bootstrap_assets.append({"filename": filename, "sha256": _sha256(data), "size": len(data)})
        artifact_data = artifact_path.read_bytes()
        python_index = {
            "artifacts": python_artifacts,
            "product": "agent-development-skills",
            "schema_version": "1.0",
            "version": version,
        }
        python_index["fingerprint"] = _sha256(_canonical_json(python_index))
        (stage / "python-artifacts.json").write_bytes(_canonical_json(python_index))
        git_modes = _git_file_modes(root)
        sbom_files = []
        for path in release_files(root):
            relative = path.relative_to(root).as_posix()
            content = _git_blob(root, relative) if not dirty else path.read_bytes()
            sbom_files.append({
                "classification": "redistributable-source",
                "mode": _file_mode(path, relative, git_modes),
                "path": relative,
                "sha256": _sha256(content),
                "size": len(content),
            })
        sbom = {
            "exclusions": [{
                "id": "xcode-official-export-content",
                "reason": "Runtime-federated Apple official expertise is identity/hash metadata only and is not redistributed.",
            }],
            "files": sbom_files,
            "format": "agent-skills-sbom-v1",
            "product": "agent-development-skills",
            "schema_version": "1.0",
            "source_revision": revision,
            "version": version,
        }
        sbom["fingerprint"] = _sha256(_canonical_json(sbom))
        sbom_bytes = _canonical_json(sbom)
        (stage / "sbom.json").write_bytes(sbom_bytes)
        generated_artifacts = [{
            "filename": artifact_name,
            "kind": "source-bundle",
            "sha256": _sha256(artifact_data),
            "size": len(artifact_data),
        }, *native_artifacts, *python_artifacts, *[
            {**item, "kind": "bootstrap"} for item in bootstrap_assets
        ]]
        builder_relative = "scripts/build_release_bundle.py"
        builder_bytes = _git_blob(root, builder_relative) if not dirty else (root / builder_relative).read_bytes()
        provenance = {
            "artifacts": sorted(generated_artifacts, key=lambda item: (item["kind"], item["filename"])),
            "builder": {
                "id": "agent-development-skills.release-builder-v1",
                "sha256": _sha256(builder_bytes),
            },
            "materials_sha256": _sha256(_canonical_json(sbom_files)),
            "product": "agent-development-skills",
            "reproducible": True,
            "sbom_sha256": _sha256(sbom_bytes),
            "schema_version": "1.0",
            "source": {
                "dirty": dirty,
                "repository": repository,
                "revision": revision,
            },
            "version": version,
        }
        provenance["fingerprint"] = _sha256(_canonical_json(provenance))
        (stage / "provenance.json").write_bytes(_canonical_json(provenance))
        manifest = {
            "asset_base_url": asset_base_url,
            "artifacts": [{
                "entrypoint": "scripts/install_local.py",
                "filename": artifact_name,
                "format": "zip",
                "host_os": list(selected_hosts),
                "id": "universal-source-bundle",
                "root": bundle_root,
                "sha256": _sha256(artifact_data),
                "size": len(artifact_data),
            }],
            "bootstrap_assets": sorted(bootstrap_assets, key=lambda item: item["filename"]),
            "channel": channel,
            "minimum_python": "3.11",
            "product": "agent-development-skills",
            "schema_version": "1.0",
            "source": {
                "dirty": dirty,
                "repository": repository,
                "revision": revision,
            },
            "version": version,
        }
        manifest_bytes = _canonical_json(manifest)
        (stage / "release-manifest.json").write_bytes(manifest_bytes)
        bootstrap = _load_bootstrap_module()
        bootstrap.parse_release_manifest(manifest_bytes)
        bootstrap._verify_artifact(artifact_data, manifest["artifacts"][0])
        validation_root = Path(directory) / "validated"
        validation_root.mkdir()
        bootstrap.extract_verified_artifact(artifact_data, manifest["artifacts"][0], validation_root)
        final_revision, final_dirty = _source_identity(root)
        if final_revision != revision or final_dirty != dirty:
            raise ReleaseBuildError("release source identity changed while the bundle was being built")
        os.replace(stage, output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--channel", choices=("stable", "beta", "development"), default="stable")
    parser.add_argument("--host-os", action="append", choices=("darwin", "linux", "windows"))
    parser.add_argument("--native-artifacts-dir", type=Path)
    arguments = parser.parse_args()
    try:
        manifest = build_release_bundle(
            ROOT,
            arguments.output,
            allow_dirty=arguments.allow_dirty,
            channel=arguments.channel,
            host_os=tuple(arguments.host_os or DEFAULT_HOST_OS),
            native_artifacts_dir=arguments.native_artifacts_dir,
        )
    except (ReleaseBuildError, OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
        print(f"release build blocked: {error}", file=sys.stderr)
        return 2
    print(_canonical_json({
        "artifacts": manifest["artifacts"],
        "output": str(arguments.output.resolve()),
        "status": "built",
        "version": manifest["version"],
    }).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
