#!/usr/bin/env python3
"""Safely uninstall the source-checkout Agent workflow from a Codex root."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_workflow.canonical_json import dumps, load  # noqa: E402
from agent_workflow.installation import (  # noqa: E402
    EXTERNAL_ACTIVATION_LOCK,
    MANAGED_DIRECTORY_MODE,
    MANAGED_ROOTS,
    _is_managed_install,
    _path_exists,
    _resolve_child,
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
    {SUPPORTED_ACTIVATED_PATHS, SOURCE_INSTALLER_ACTIVATION_BASELINE}
)
MANAGED_INSTRUCTIONS_ASSIGNMENT = re.compile(
    r'''^[ \t]*(?:model_instructions_file|"model_instructions_file"|'model_instructions_file')[ \t]*='''
)
TABLE_HEADER = re.compile(r"^[ \t]*\[")


def _load_config_tools() -> ModuleType:
    path = ROOT / "runtime-configs/codex/assets/scripts/sync_codex_shared_config.py"
    spec = importlib.util.spec_from_file_location("agent_skills_config_tools", path)
    if spec is None or spec.loader is None:
        raise ContractError("unable to load Codex config tools")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _managed_state(raw_target: str | Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    requested = Path(raw_target).expanduser()
    if requested.is_symlink():
        raise ContractError(f"uninstall target must not be a symlink: {requested}")
    target = requested.resolve()
    if not target.is_dir():
        raise ContractError(f"managed install target does not exist: {target}")
    if not _is_managed_install(target):
        raise ContractError(f"refusing to uninstall unmanaged or modified install: {target}")

    lock = load(target / INSTALL_LOCK)
    activation_path = target / ACTIVATION_LOCK
    if not _path_exists(activation_path):
        raise ContractError("managed source install is missing its activation lock")
    activation = load(activation_path)
    if (
        activation.get("schema_version") != "1.0"
        or activation.get("manager") != MANAGER_ID
        or not isinstance(activation.get("files"), list)
    ):
        raise ContractError("activation lock is not owned by agent-development-skills")
    activation_files = activation["files"]
    activated_paths = [item.get("path") for item in activation_files if isinstance(item, dict)]
    actual_paths = frozenset(activated_paths)
    if (
        len(activated_paths) != len(activation_files)
        or len(actual_paths) != len(activated_paths)
        or actual_paths not in SUPPORTED_ACTIVATED_PATH_SETS
    ):
        raise ContractError("activation lock does not cover the supported managed file set")
    return target, lock, activation_files


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
    path: Path,
    config: dict[str, Any],
) -> bytes:
    original = path.read_bytes()
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


def _config_plan(target: Path) -> tuple[bytes | None, int | None, str]:
    path = target / "config.toml"
    if not _path_exists(path):
        return None, None, "missing"
    if path.is_symlink() or not path.is_file():
        raise ContractError("config.toml must be a regular file")

    mode = path.stat().st_mode & 0o777
    tools = _load_config_tools()
    config = tools.load_toml(str(path))
    managed_agents_path = str(target / "AGENTS.md")
    if config.get("model_instructions_file") != managed_agents_path:
        return path.read_bytes(), mode, "preserved"
    return (
        _remove_managed_instructions_assignment(path, config),
        mode,
        "removed-managed-instructions-path",
    )


def _build_plan(
    target: Path,
    lock: dict[str, Any],
    activation_files: list[dict[str, Any]],
    platforms: tuple[str, ...],
) -> dict[str, Any]:
    config_candidate, config_mode, config_action = _config_plan(target)
    preserved_profiles = [name for name in PROFILE_NAMES if _path_exists(target / name)]
    preserved_system_skills = (target / "skills" / ".system").is_dir()
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
    }


def _verify_uninstalled_state(target: Path, plan: dict[str, Any]) -> None:
    if _path_exists(target / "AGENTS.md") or _path_exists(target / ".agent-skills"):
        raise ContractError("managed roots remain after uninstall")
    for path in plan["activated_files"]:
        if _path_exists(_resolve_child(target, path, label="activated file")):
            raise ContractError(f"activated file remains after uninstall: {path}")
    if plan["preserved_system_skills"]:
        system_skills = target / "skills" / ".system"
        if system_skills.is_symlink() or not system_skills.is_dir():
            raise ContractError("Codex system skills were not preserved")
    config_candidate = plan["_config_candidate"]
    if config_candidate is not None and (target / "config.toml").read_bytes() != config_candidate:
        raise ContractError("config.toml differs from the uninstall plan")


def _execute_uninstall(
    target: Path,
    activation_files: list[dict[str, Any]],
    plan: dict[str, Any],
) -> None:
    backup = Path(tempfile.mkdtemp(prefix=".agent-skills-uninstall-backup-", dir=target))
    moved_roots: list[tuple[Path, Path]] = []
    moved_activated: list[tuple[Path, Path]] = []
    config_backup: Path | None = None
    system_restored = False
    try:
        managed_backup = backup / "managed"
        managed_backup.mkdir()
        for name in MANAGED_ROOTS:
            source = target / name
            destination = managed_backup / name
            os.replace(source, destination)
            moved_roots.append((source, destination))

        activated_backup = backup / "activated"
        for item in activation_files:
            source = _resolve_child(target, item["path"], label="activated file")
            destination = activated_backup / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved_activated.append((source, destination))

        config_candidate = plan["_config_candidate"]
        config_path = target / "config.toml"
        if config_candidate is not None and config_path.read_bytes() != config_candidate:
            config_backup = backup / "config.toml"
            os.replace(config_path, config_backup)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".config.toml.", dir=target)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(config_candidate)
                temporary.chmod(plan["_config_mode"])
                os.replace(temporary, config_path)
            finally:
                temporary.unlink(missing_ok=True)

        preserved_system = managed_backup / "skills" / ".system"
        if preserved_system.is_dir() and not preserved_system.is_symlink():
            (target / "skills").mkdir()
            (target / "skills").chmod(MANAGED_DIRECTORY_MODE)
            os.replace(preserved_system, target / "skills" / ".system")
            system_restored = True

        _verify_uninstalled_state(target, plan)
    except Exception as primary_error:
        recovery_errors: list[str] = []
        if system_restored:
            try:
                os.replace(target / "skills" / ".system", backup / "managed" / "skills" / ".system")
                (target / "skills").rmdir()
            except OSError as error:
                recovery_errors.append(f"restore system skills: {error}")
        if config_backup is not None:
            try:
                (target / "config.toml").unlink(missing_ok=True)
                os.replace(config_backup, target / "config.toml")
            except OSError as error:
                recovery_errors.append(f"restore config.toml: {error}")
        for source, destination in reversed(moved_activated):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            except OSError as error:
                recovery_errors.append(f"restore {source.relative_to(target)}: {error}")
        for source, destination in reversed(moved_roots):
            try:
                os.replace(destination, source)
            except OSError as error:
                recovery_errors.append(f"restore {source.name}: {error}")
        if recovery_errors:
            raise ContractError(
                f"uninstall failed ({primary_error}); rollback incomplete; recovery backup preserved at {backup}: "
                + "; ".join(recovery_errors)
            ) from primary_error
        try:
            shutil.rmtree(backup)
        except OSError as cleanup_error:
            raise ContractError(
                f"uninstall failed ({primary_error}); rollback succeeded but temporary backup "
                f"cleanup failed at {backup}: {cleanup_error}"
            ) from primary_error
        raise
    try:
        shutil.rmtree(backup)
    except OSError as error:
        raise ContractError(
            "managed files were removed, but temporary backup cleanup failed; "
            f"remove the residual backup manually: {backup}: {error}"
        ) from error


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
    target, lock, activation_files = _managed_state(args.target_root)
    platforms = _selected_platforms(lock, args.platform)
    plan = _build_plan(target, lock, activation_files, platforms)
    if args.dry_run:
        return {key: value for key, value in plan.items() if not key.startswith("_")}

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
