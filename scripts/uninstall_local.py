#!/usr/bin/env python3
"""Safely uninstall the source-checkout Agent workflow from a Codex root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
import tomllib
from typing import Any

try:
    import fcntl
except ImportError:  # Windows remains fail-closed for production uninstall.
    fcntl = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.contracts import validate_activation_lock  # noqa: E402
from agent_workflow.installation import (  # noqa: E402
    EXTERNAL_ACTIVATION_LOCK,
    MANAGED_DIRECTORY_MODE,
    MANAGED_ROOTS,
    _is_managed_install,
    _path_exists,
    _resolve_child,
    target_lifecycle_lock,
)
from agent_workflow.models import ContractError  # noqa: E402
from install_local import ACTIVATED_FILES  # noqa: E402


MANAGER_ID = "agent-development-skills"
INSTALL_LOCK = ".agent-skills/install-lock.json"
ACTIVATION_LOCK = f".agent-skills/{EXTERNAL_ACTIVATION_LOCK}"
PROFILE_NAMES = (
    "budget.config.toml",
    "daily.config.toml",
    "deep.config.toml",
    "extreme.config.toml",
    "interactive-fast.config.toml",
    "readonly.config.toml",
)
SUPPORTED_ACTIVATED_PATHS = frozenset(destination for _, destination, _ in ACTIVATED_FILES)
NATIVE_INSTALLER_ACTIVATED_PATHS = SUPPORTED_ACTIVATED_PATHS | {"bin/agent-skills"}
# Stable source-install baseline. Newer checkouts may add activation files, but
# users must still be able to uninstall an intact earlier source installation.
SOURCE_INSTALLER_ACTIVATION_BASELINE = frozenset(
    {
        "agents/design_researcher.toml",
        "agents/reviewer.toml",
        "agents/explorer.toml",
        "agents/pm.toml",
        "agents/reporter.toml",
        "agents/builder.toml",
        "agents/docs_researcher.toml",
        "agents/tester.toml",
        "bin/codex_verify",
        "bin/digest-xcodebuild-log",
        "templates/codex_verify.example.sh",
        "templates/ui-smoke.example.yml",
    }
)
SUPPORTED_ACTIVATED_PATH_SETS = frozenset(
    {
        NATIVE_INSTALLER_ACTIVATED_PATHS,
        SUPPORTED_ACTIVATED_PATHS,
        SOURCE_INSTALLER_ACTIVATION_BASELINE,
    }
)
MANAGED_INSTRUCTIONS_ASSIGNMENT = re.compile(
    r'''^[ \t]*(?:model_instructions_file|"model_instructions_file"|'model_instructions_file')[ \t]*='''
)
TABLE_HEADER = re.compile(r"^[ \t]*\[")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FD_RELATIVE_SUPPORTED = (
    bool(getattr(os, "O_DIRECTORY", 0))
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)


def _directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _open_relative_directory(
    root: Path,
    parts: tuple[str, ...],
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> int:
    if not _FD_RELATIVE_SUPPORTED:
        raise ContractError("secure fd-relative uninstall is unsupported on this host")
    descriptor = os.open(root, _DIRECTORY_FLAGS)
    try:
        if expected_root_identity is not None and _directory_identity(descriptor) != expected_root_identity:
            raise ContractError("uninstall target directory identity changed")
        for part in parts:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_descendant_directory(root_descriptor: int, parts: tuple[str, ...]) -> int:
    if not _FD_RELATIVE_SUPPORTED:
        raise ContractError("secure fd-relative uninstall is unsupported on this host")
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_or_create_descendant_directory(
    root_descriptor: int,
    parts: tuple[str, ...],
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            try:
                os.mkdir(part, MANAGED_DIRECTORY_MODE, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _fd_exists(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _fd_file_snapshot(parent_descriptor: int, name: str) -> tuple[bytes, int]:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"expected a regular file: {name}")
        chunks = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        return b"".join(chunks), metadata.st_mode & 0o777
    finally:
        os.close(descriptor)


def _create_private_directory(parent_descriptor: int, prefix: str) -> str:
    for _ in range(32):
        name = prefix + secrets.token_hex(8)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            return name
        except FileExistsError:
            continue
    raise ContractError("unable to allocate a private uninstall backup directory")


def _stable_fd_path(descriptor: int) -> Path:
    proc_entry = Path("/proc/self/fd") / str(descriptor)
    if proc_entry.is_symlink():
        candidate = Path(os.readlink(proc_entry))
    elif sys.platform == "darwin" and fcntl is not None:
        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
        candidate = Path(raw.split(b"\0", 1)[0].decode("utf-8"))
    else:
        raise ContractError("stable file-descriptor path is unavailable on this host")
    probe = _open_relative_directory(
        candidate,
        (),
        expected_root_identity=_directory_identity(descriptor),
    )
    os.close(probe)
    return candidate


def _fd_file_matches(parent_descriptor: int, name: str, record: dict[str, Any]) -> bool:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_mode & 0o777 == record["mode"]
            and digest.hexdigest() == record["sha256"]
        )
    finally:
        os.close(descriptor)


def _managed_state(
    raw_target: str | Path,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], tuple[int, int], str | None]:
    requested = Path(raw_target).expanduser()
    if requested.is_symlink():
        raise ContractError(f"uninstall target must not be a symlink: {requested}")
    target = requested.resolve()
    if not target.is_dir():
        raise ContractError(f"managed install target does not exist: {target}")
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    if not _is_managed_install(target):
        raise ContractError(f"refusing to uninstall unmanaged or modified install: {target}")

    lock = load(target / INSTALL_LOCK)
    selected_package_ids = {
        item.get("id") for item in lock.get("selected_packages", [])
        if isinstance(item, dict)
    }
    activation_owned = (
        "codex" in lock.get("selected_runtime_configs", [])
        or "codex" in selected_package_ids
    )
    activation_path = target / ACTIVATION_LOCK
    if not _path_exists(activation_path):
        # Core/package-only installs (including Desktop and wheel-installed CLI
        # installs) own only MANAGED_ROOTS and intentionally have no external
        # activation lock.  Apple source installs select the Codex runtime and
        # must retain the lock so missing activation evidence cannot turn into
        # an unsafe best-effort cleanup of external files.
        if activation_owned:
            raise ContractError("managed source install is missing its activation lock")
        if (target.stat().st_dev, target.stat().st_ino) != target_identity:
            raise ContractError("uninstall target directory identity changed")
        return target, lock, [], target_identity, None
    if not activation_owned:
        raise ContractError("non-activated install must not contain an activation lock")
    activation = load(activation_path)
    validate_activation_lock(activation)
    activation_files = activation["files"]
    activated_paths = [item.get("path") for item in activation_files if isinstance(item, dict)]
    actual_paths = frozenset(activated_paths)
    if (
        len(activated_paths) != len(activation_files)
        or len(actual_paths) != len(activated_paths)
        or actual_paths not in SUPPORTED_ACTIVATED_PATH_SETS
    ):
        raise ContractError("activation lock does not cover the supported managed file set")
    if (target.stat().st_dev, target.stat().st_ino) != target_identity:
        raise ContractError("uninstall target directory identity changed")
    return target, lock, activation_files, target_identity, sha256(activation)


def _selected_platforms(lock: dict[str, Any], requested: list[str]) -> tuple[str, ...]:
    installed = tuple(lock["selected_platforms"])
    if not requested or requested == ["all"]:
        return installed
    if "all" in requested:
        raise ContractError("--platform all cannot be combined with another platform")
    if len(requested) != len(set(requested)):
        raise ContractError("selected platforms must be unique")
    unknown = sorted(set(requested) - set(installed))
    if unknown:
        raise ContractError("platform is not installed: " + ", ".join(unknown))
    if set(requested) != set(installed):
        raise ContractError(
            "partial platform uninstall is not available in the current source installer; "
            "select all installed platforms"
        )
    return tuple(sorted(requested))


def _remove_managed_instructions_assignment(
    original: bytes,
    config: dict[str, Any],
) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("config.toml must be UTF-8") from error

    lines = text.splitlines(keepends=True)
    matches: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if stripped.startswith("#"):
            continue
        if TABLE_HEADER.match(line):
            break
        if MANAGED_INSTRUCTIONS_ASSIGNMENT.match(line):
            matches.append(index)
    if len(matches) != 1:
        raise ContractError(
            "managed model_instructions_file must be one root-level single-line assignment"
        )

    del lines[matches[0]]
    candidate = "".join(lines).encode("utf-8")
    expected = dict(config)
    expected.pop("model_instructions_file")
    try:
        parsed_candidate = tomllib.loads(candidate.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ContractError(
            "refusing to rewrite config.toml because targeted key removal is not valid TOML"
        ) from error
    if parsed_candidate != expected:
        raise ContractError(
            "refusing to rewrite config.toml because targeted key removal changed other values"
        )
    return candidate


def _config_plan(
    target: Path,
    *,
    activation_owned: bool,
) -> tuple[bytes | None, int | None, str, bytes | None]:
    path = target / "config.toml"
    if not _path_exists(path):
        return None, None, "missing", None
    if path.is_symlink() or not path.is_file():
        raise ContractError("config.toml must be a regular file")

    mode = path.stat().st_mode & 0o777
    original = path.read_bytes()
    if not activation_owned:
        return original, mode, "preserved", original
    try:
        config = tomllib.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(f"config.toml must be valid UTF-8 TOML: {error}") from error
    managed_agents_path = str(target / "AGENTS.md")
    if config.get("model_instructions_file") != managed_agents_path:
        return original, mode, "preserved", original
    return (
        _remove_managed_instructions_assignment(original, config),
        mode,
        "removed-managed-instructions-path",
        original,
    )


def _build_plan(
    target: Path,
    lock: dict[str, Any],
    activation_files: list[dict[str, Any]],
    platforms: tuple[str, ...],
    target_identity: tuple[int, int],
    activation_identity: str | None,
) -> dict[str, Any]:
    if (target.stat().st_dev, target.stat().st_ino) != target_identity:
        raise ContractError("uninstall target directory identity changed")
    config_candidate, config_mode, config_action, config_original = _config_plan(
        target,
        activation_owned=bool(activation_files),
    )
    preserved_profiles = [name for name in PROFILE_NAMES if _path_exists(target / name)]
    preserved_system_skills = (target / "skills" / ".system").is_dir()
    if (target.stat().st_dev, target.stat().st_ino) != target_identity:
        raise ContractError("uninstall target directory identity changed")
    return {
        "schema_version": "1.0",
        "status": "planned",
        "target_root": str(target),
        "selected_platforms": list(platforms),
        "removed_packages": [item["id"] for item in lock["selected_packages"]],
        "managed_roots": list(MANAGED_ROOTS),
        "activated_files": [item["path"] for item in activation_files],
        "config_action": config_action,
        "preserved_profiles": preserved_profiles,
        "preserved_system_skills": preserved_system_skills,
        "legacy_links_restored": False,
        "_config_candidate": config_candidate,
        "_config_mode": config_mode,
        "_config_original": config_original,
        "_activation_lock_identity": activation_identity,
        "_install_lock_identity": sha256(lock),
        "_target_identity": target_identity,
    }


def _verify_uninstalled_state(target_descriptor: int, plan: dict[str, Any]) -> None:
    if _fd_exists(target_descriptor, "AGENTS.md") or _fd_exists(target_descriptor, ".agent-skills"):
        raise ContractError("managed roots remain after uninstall")
    for path in plan["activated_files"]:
        relative = Path(path)
        try:
            parent_descriptor = _open_descendant_directory(
                target_descriptor,
                tuple(relative.parts[:-1]),
            )
        except FileNotFoundError:
            continue
        try:
            if _fd_exists(parent_descriptor, relative.name):
                raise ContractError(f"activated file remains after uninstall: {path}")
        finally:
            os.close(parent_descriptor)
    if plan["preserved_system_skills"]:
        skills_descriptor = _open_descendant_directory(target_descriptor, ("skills",))
        try:
            system_descriptor = os.open(".system", _DIRECTORY_FLAGS, dir_fd=skills_descriptor)
            os.close(system_descriptor)
        finally:
            os.close(skills_descriptor)
    if plan["config_action"] == "removed-managed-instructions-path":
        content, mode = _fd_file_snapshot(target_descriptor, "config.toml")
        if content != plan["_config_candidate"] or mode != plan["_config_mode"]:
            raise ContractError("config.toml differs from the uninstall plan")


def _execute_uninstall(
    target: Path,
    activation_files: list[dict[str, Any]],
    plan: dict[str, Any],
) -> None:
    target_descriptor = _open_relative_directory(
        target,
        (),
        expected_root_identity=plan["_target_identity"],
    )
    backup_name = _create_private_directory(
        target_descriptor,
        ".agent-skills-uninstall-backup-",
    )
    backup = target / backup_name
    backup_descriptor = _open_descendant_directory(target_descriptor, (backup_name,))
    os.mkdir("managed", MANAGED_DIRECTORY_MODE, dir_fd=backup_descriptor)
    managed_backup_descriptor = _open_descendant_directory(backup_descriptor, ("managed",))
    managed_backup = backup / "managed"
    moved_roots: list[str] = []
    moved_activated: list[dict[str, Any]] = []
    config_backup = False
    config_written = False
    system_restored = False
    descriptors_open = True

    def close_backup_descriptors() -> None:
        nonlocal descriptors_open
        if descriptors_open:
            os.close(managed_backup_descriptor)
            os.close(backup_descriptor)
            descriptors_open = False

    try:
        try:
            for name in MANAGED_ROOTS:
                os.rename(
                    name,
                    name,
                    src_dir_fd=target_descriptor,
                    dst_dir_fd=managed_backup_descriptor,
                )
                moved_roots.append(name)

            managed_state_descriptor = _open_descendant_directory(
                managed_backup_descriptor,
                (".agent-skills",),
            )
            try:
                moved_lock_bytes, _ = _fd_file_snapshot(
                    managed_state_descriptor,
                    "install-lock.json",
                )
                moved_lock = json.loads(moved_lock_bytes.decode("utf-8"))
                if sha256(moved_lock) != plan["_install_lock_identity"]:
                    raise ContractError("uninstall plan install lock differs from moved target")
                if _fd_exists(managed_state_descriptor, EXTERNAL_ACTIVATION_LOCK):
                    moved_activation_bytes, _ = _fd_file_snapshot(
                        managed_state_descriptor,
                        EXTERNAL_ACTIVATION_LOCK,
                    )
                    moved_activation = json.loads(moved_activation_bytes.decode("utf-8"))
                    moved_activation_identity = sha256(moved_activation)
                else:
                    moved_activation_identity = None
                if moved_activation_identity != plan["_activation_lock_identity"]:
                    raise ContractError(
                        "uninstall plan activation lock differs from moved target"
                    )
            finally:
                os.close(managed_state_descriptor)

            for item in activation_files:
                relative = Path(item["path"])
                parent_parts = tuple(relative.parts[:-1])
                source_parent = _open_descendant_directory(target_descriptor, parent_parts)
                try:
                    destination_parent = _open_or_create_descendant_directory(
                        managed_backup_descriptor,
                        parent_parts,
                    )
                    try:
                        parent_identity = _directory_identity(source_parent)
                        os.rename(
                            relative.name,
                            relative.name,
                            src_dir_fd=source_parent,
                            dst_dir_fd=destination_parent,
                        )
                        moved_activated.append({
                            "destination": managed_backup / item["path"],
                            "item": item,
                            "parent_identity": parent_identity,
                            "parent_parts": parent_parts,
                            "source": target / item["path"],
                        })
                        if not _fd_file_matches(destination_parent, relative.name, item):
                            raise ContractError(
                                f"activated file changed during uninstall: {item['path']}"
                            )
                    finally:
                        os.close(destination_parent)
                finally:
                    os.close(source_parent)
            if not _is_managed_install(_stable_fd_path(managed_backup_descriptor)):
                raise ContractError("managed install changed during uninstall")

            if plan["config_action"] == "removed-managed-instructions-path":
                content, mode = _fd_file_snapshot(target_descriptor, "config.toml")
                if content != plan["_config_original"] or mode != plan["_config_mode"]:
                    raise ContractError("config.toml changed during uninstall")
                os.rename(
                    "config.toml",
                    "config.toml",
                    src_dir_fd=target_descriptor,
                    dst_dir_fd=backup_descriptor,
                )
                config_backup = True
                content, mode = _fd_file_snapshot(backup_descriptor, "config.toml")
                if content != plan["_config_original"] or mode != plan["_config_mode"]:
                    raise ContractError("config.toml changed during uninstall")
                temporary_name = ".config.toml." + secrets.token_hex(8)
                temporary_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    plan["_config_mode"],
                    dir_fd=target_descriptor,
                )
                try:
                    view = memoryview(plan["_config_candidate"])
                    while view:
                        written = os.write(temporary_descriptor, view)
                        view = view[written:]
                    os.fchmod(temporary_descriptor, plan["_config_mode"])
                finally:
                    os.close(temporary_descriptor)
                try:
                    os.link(
                        temporary_name,
                        "config.toml",
                        src_dir_fd=target_descriptor,
                        dst_dir_fd=target_descriptor,
                        follow_symlinks=False,
                    )
                    config_written = True
                finally:
                    os.unlink(temporary_name, dir_fd=target_descriptor)

            try:
                backup_skills = _open_descendant_directory(
                    managed_backup_descriptor,
                    ("skills",),
                )
                try:
                    system_descriptor = os.open(
                        ".system",
                        _DIRECTORY_FLAGS,
                        dir_fd=backup_skills,
                    )
                    os.close(system_descriptor)
                    has_system = True
                except FileNotFoundError:
                    has_system = False
            except FileNotFoundError:
                has_system = False
                backup_skills = None
            if has_system and backup_skills is not None:
                try:
                    os.mkdir("skills", MANAGED_DIRECTORY_MODE, dir_fd=target_descriptor)
                    os.chmod("skills", MANAGED_DIRECTORY_MODE, dir_fd=target_descriptor)
                    target_skills = _open_descendant_directory(target_descriptor, ("skills",))
                    try:
                        os.rename(
                            ".system",
                            ".system",
                            src_dir_fd=backup_skills,
                            dst_dir_fd=target_skills,
                        )
                    finally:
                        os.close(target_skills)
                    system_restored = True
                finally:
                    os.close(backup_skills)

            for record in moved_activated:
                parent_descriptor = _open_descendant_directory(
                    target_descriptor,
                    record["parent_parts"],
                )
                try:
                    if _directory_identity(parent_descriptor) != record["parent_identity"]:
                        raise ContractError(
                            "activated file parent changed during uninstall: "
                            + record["item"]["path"]
                        )
                finally:
                    os.close(parent_descriptor)

            _verify_uninstalled_state(target_descriptor, plan)
        except Exception as primary_error:
            recovery_errors: list[str] = []
            if system_restored:
                try:
                    target_skills = _open_descendant_directory(target_descriptor, ("skills",))
                    backup_skills = _open_descendant_directory(
                        managed_backup_descriptor,
                        ("skills",),
                    )
                    try:
                        os.rename(
                            ".system",
                            ".system",
                            src_dir_fd=target_skills,
                            dst_dir_fd=backup_skills,
                        )
                    finally:
                        os.close(backup_skills)
                        os.close(target_skills)
                    os.rmdir("skills", dir_fd=target_descriptor)
                except OSError as error:
                    recovery_errors.append(f"restore system skills: {error}")
            if config_backup:
                try:
                    config_backup_path = backup / "config.toml"
                    if config_written:
                        if _fd_exists(target_descriptor, "config.toml"):
                            content, mode = _fd_file_snapshot(target_descriptor, "config.toml")
                            config_changed = (
                                content != plan["_config_candidate"]
                                or mode != plan["_config_mode"]
                            )
                        else:
                            config_changed = False
                        if config_changed:
                            recovery_errors.append(
                                "restore config.toml: destination changed after managed rewrite; "
                                f"recovery copy remains at {config_backup_path}"
                            )
                        else:
                            if _fd_exists(target_descriptor, "config.toml"):
                                os.unlink("config.toml", dir_fd=target_descriptor)
                            os.rename(
                                "config.toml",
                                "config.toml",
                                src_dir_fd=backup_descriptor,
                                dst_dir_fd=target_descriptor,
                            )
                    elif not _fd_exists(target_descriptor, "config.toml"):
                        os.rename(
                            "config.toml",
                            "config.toml",
                            src_dir_fd=backup_descriptor,
                            dst_dir_fd=target_descriptor,
                        )
                    else:
                        recovery_errors.append(
                            "restore config.toml: destination was recreated before managed rewrite; "
                            f"recovery copy remains at {config_backup_path}"
                        )
                except (ContractError, OSError) as error:
                    recovery_errors.append(f"restore config.toml: {error}")
            for record in reversed(moved_activated):
                source = record["source"]
                destination = record["destination"]
                try:
                    source_parent = _open_descendant_directory(
                        target_descriptor,
                        record["parent_parts"],
                    )
                    try:
                        destination_parent = _open_descendant_directory(
                            managed_backup_descriptor,
                            record["parent_parts"],
                        )
                        try:
                            if _directory_identity(source_parent) != record["parent_identity"]:
                                recovery_errors.append(
                                    f"restore {source.relative_to(target)}: parent directory changed; "
                                    f"recovery copy remains at {destination}"
                                )
                            elif _fd_exists(source_parent, source.name):
                                recovery_errors.append(
                                    f"restore {source.relative_to(target)}: destination was recreated; "
                                    f"recovery copy remains at {destination}"
                                )
                            else:
                                os.rename(
                                    destination.name,
                                    source.name,
                                    src_dir_fd=destination_parent,
                                    dst_dir_fd=source_parent,
                                )
                        finally:
                            os.close(destination_parent)
                    finally:
                        os.close(source_parent)
                except (ContractError, OSError) as error:
                    recovery_errors.append(f"restore {source.relative_to(target)}: {error}")
            for name in reversed(moved_roots):
                try:
                    destination = managed_backup / name
                    if _fd_exists(target_descriptor, name):
                        recovery_errors.append(
                            f"restore {name}: destination was recreated; "
                            f"recovery copy remains at {destination}"
                        )
                    else:
                        os.rename(
                            name,
                            name,
                            src_dir_fd=managed_backup_descriptor,
                            dst_dir_fd=target_descriptor,
                        )
                except OSError as error:
                    recovery_errors.append(f"restore {name}: {error}")
            if recovery_errors:
                raise ContractError(
                    f"uninstall failed ({primary_error}); rollback incomplete; "
                    f"recovery backup preserved at {backup}: " + "; ".join(recovery_errors)
                ) from primary_error
            close_backup_descriptors()
            try:
                shutil.rmtree(backup_name, dir_fd=target_descriptor)
            except OSError as cleanup_error:
                raise ContractError(
                    f"uninstall failed ({primary_error}); rollback succeeded but temporary "
                    f"backup cleanup failed at {backup}: {cleanup_error}"
                ) from primary_error
            raise

        close_backup_descriptors()
        try:
            shutil.rmtree(backup_name, dir_fd=target_descriptor)
        except OSError as error:
            raise ContractError(
                "managed files were removed, but temporary backup cleanup failed; "
                f"remove the residual backup manually: {backup}: {error}"
            ) from error
    finally:
        close_backup_descriptors()
        os.close(target_descriptor)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Uninstall the managed Agent workflow from a local Codex root."
    )
    parser.add_argument(
        "--target-root",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        help="installation root (default: $CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help="installed platform id; repeat for all selected platforms or use all",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without writing files")
    parser.add_argument("--json", action="store_true", help="output canonical JSON for automation")
    return parser.parse_args()


def run(args: argparse.Namespace | None = None) -> dict[str, Any]:
    if sys.version_info < (3, 11):
        raise ContractError("uninstall.sh requires Python 3.11+")
    if args is None:
        args = parse_args()
    if args.dry_run:
        target, lock, activation_files, target_identity, activation_identity = _managed_state(
            args.target_root
        )
        platforms = _selected_platforms(lock, args.platform)
        plan = _build_plan(
            target, lock, activation_files, platforms, target_identity, activation_identity
        )
        return {key: value for key, value in plan.items() if not key.startswith("_")}
    with target_lifecycle_lock(args.target_root):
        target, lock, activation_files, target_identity, activation_identity = _managed_state(
            args.target_root
        )
        platforms = _selected_platforms(lock, args.platform)
        plan = _build_plan(
            target, lock, activation_files, platforms, target_identity, activation_identity
        )
        _execute_uninstall(target, activation_files, plan)
    result = {key: value for key, value in plan.items() if not key.startswith("_")}
    result["status"] = "uninstalled"
    return result


def _human_report(report: dict[str, Any]) -> str:
    dry_run = report["status"] == "planned"
    title = "卸载预览" if dry_run else "卸载完成"
    lines = [f"{'◇' if dry_run else '✓'} Agent Development Skills {title}", ""]
    lines.append("  平台：" + ("、".join(report["selected_platforms"]) or "全部受管内容"))
    lines.append(f"  受管根：{len(report['managed_roots'])} 个")
    lines.append(f"  激活文件：{len(report['activated_files'])} 个")
    lines.append(f"  config.toml：{report['config_action']}")
    if report["preserved_profiles"]:
        lines.append(f"  保留本机 Profiles：{len(report['preserved_profiles'])} 个")
    if report["preserved_system_skills"]:
        lines.append("  保留 Codex 系统 Skills：是")
    lines.append("  旧 iOSAgentSkills 软链：未恢复（安装时未创建持久备份）")
    if dry_run:
        lines.extend(["", "未写入任何文件；移除 --dry-run 后执行卸载。"])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        report = run(args)
        if args.json:
            print(dumps(report), end="")
        else:
            print(_human_report(report), end="")
        return 0
    except (ContractError, OSError, ValueError, KeyError, TypeError) as error:
        if args.json:
            print(dumps({"status": "blocked", "error": str(error)}), end="", file=sys.stderr)
        else:
            print(f"✗ Agent Development Skills 卸载未完成\n\n  原因：{error}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
