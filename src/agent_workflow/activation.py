"""Trusted Core source-activation lifecycle for installed package snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable
import zipfile
import re

from .adapters import build_adapter_request
from .canonical_json import dumps, load
from .contracts import validate_activation_lock
from .discovery import DiscoveryEngine
from .installation import MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE, _path_exists, _resolve_child
from .models import ContractError
from .migrations import migrate_activation_lock
from .planning import PlanCompiler
from .policy import PolicyResolver
from .registry import ManifestRegistry
from .reporting import delivery_report
from .runtime import RecordedAdapterExecutor


@dataclass(frozen=True)
class ActivationAsset:
    package: str
    source: str
    destination: str
    mode: int


ACTIVATION_HANDLER_ID = "core.source-activation.apple-codex-v1"
DEACTIVATION_HANDLER_ID = "core.source-deactivation.apple-codex-v1"
PRESERVE_HANDLER_ID = "core.source-preserve.apple-codex-v1"
ACTIVATION_ASSETS = (
    ActivationAsset("design", "assets/codex/agents/design_researcher.toml", "agents/design_researcher.toml", 0o644),
    ActivationAsset("review", "assets/codex/agents/reviewer.toml", "agents/reviewer.toml", 0o644),
    ActivationAsset("workflow", "assets/codex/agents/explorer.toml", "agents/explorer.toml", 0o644),
    ActivationAsset("workflow", "assets/codex/agents/pm.toml", "agents/pm.toml", 0o644),
    ActivationAsset("workflow", "assets/codex/agents/reporter.toml", "agents/reporter.toml", 0o644),
    ActivationAsset("apple", "config/codex/templates/agents/builder.toml", "agents/builder.toml", 0o644),
    ActivationAsset("apple", "config/codex/templates/agents/docs_researcher.toml", "agents/docs_researcher.toml", 0o644),
    ActivationAsset("apple", "config/codex/templates/agents/tester.toml", "agents/tester.toml", 0o644),
    ActivationAsset("apple", "config/codex/templates/codex_verify.example.sh", "bin/codex_verify", 0o755),
    ActivationAsset("apple", "tools/digest-xcodebuild-log.sh", "bin/digest-xcodebuild-log", 0o755),
    ActivationAsset("@generated", "agent-session.pyz", "bin/agent-session", 0o755),
    ActivationAsset("apple", "config/codex/templates/codex_verify.example.sh", "templates/codex_verify.example.sh", 0o755),
    ActivationAsset("apple", "config/codex/templates/ui-smoke.example.yml", "templates/ui-smoke.example.yml", 0o644),
)
PROFILE_ASSETS = tuple(
    ActivationAsset("codex", f"assets/codex/profiles/{name}", name, 0o644)
    for name in (
        "budget.config.toml", "daily.config.toml", "deep.config.toml",
        "extreme.config.toml", "interactive-fast.config.toml", "readonly.config.toml",
    )
)


def activation_handler_sha256() -> str:
    digest = hashlib.sha256(
        f"{ACTIVATION_HANDLER_ID}\0{DEACTIVATION_HANDLER_ID}\0{PRESERVE_HANDLER_ID}".encode("utf-8")
    )
    package_root = Path(__file__).resolve().parent
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root.parent).as_posix().encode("utf-8")
        digest.update(b"\0" + relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _generated_agent_session() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        main = zipfile.ZipInfo("__main__.py", date_time=(1980, 1, 1, 0, 0, 0))
        main.external_attr = 0o644 << 16
        archive.writestr(
            main,
            "from agent_workflow.worktree_sessions.cli import main\nraise SystemExit(main())\n",
        )
        package_root = Path(__file__).resolve().parent
        for path in sorted(package_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(package_root.parent).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return b"#!/usr/bin/env python3\n" + buffer.getvalue()


def _asset_bytes(target: Path, asset: ActivationAsset) -> bytes:
    if asset.package == "@generated":
        return _generated_agent_session()
    path = target / ".agent-skills" / "packages" / asset.package / asset.source
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"installed activation asset is missing or unsafe: {asset.package}/{asset.source}")
    return path.read_bytes()


def _atomic_write(path: Path, value: bytes, mode: int) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        path.parent.chmod(MANAGED_DIRECTORY_MODE)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def external_paths(target: Path) -> tuple[str, ...]:
    lock = load(target / ".agent-skills" / "activation-lock.json")
    validate_activation_lock(lock)
    records = lock.get("files") if isinstance(lock, dict) else None
    if not isinstance(records, list):
        raise ContractError("source activation lock files are invalid")
    current: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ContractError("source activation lock file record is invalid")
        current.append(record["path"])
    candidate = [item.destination for item in (*ACTIVATION_ASSETS, *PROFILE_ASSETS)]
    return tuple(sorted(set([*current, *candidate, "config.toml"])))


def deactivation_external_paths(target: Path) -> tuple[str, ...]:
    lock = load(target / ".agent-skills" / "activation-lock.json")
    validate_activation_lock(lock)
    records = lock.get("files") if isinstance(lock, dict) else None
    if not isinstance(records, list):
        raise ContractError("source activation lock files are invalid")
    paths = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ContractError("source activation lock file record is invalid")
        paths.append(record["path"])
    return tuple(sorted(set([*paths, "config.toml"])))


def _config_candidate(target: Path) -> bytes:
    package = target / ".agent-skills" / "packages" / "codex"
    script = package / "assets/scripts/sync_codex_shared_config.py"
    shared = package / "assets/codex/codex.shared.toml"
    if any(path.is_symlink() or not path.is_file() for path in (script, shared)):
        raise ContractError("installed Codex config activation assets are missing or unsafe")
    command = [
        sys.executable, str(script), "--shared-config", str(shared),
        "--agents-path", str(target / "AGENTS.md"),
    ]
    config = target / "config.toml"
    if _path_exists(config):
        if config.is_symlink() or not config.is_file():
            raise ContractError("config.toml must be a regular file")
        command.extend(["--existing-config", str(config)])
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).decode(errors="replace").strip()
        raise ContractError(f"installed Codex config activation failed: {detail}") from error
    return completed.stdout


def apply_source_activation(target: Path, *, selected_platforms: Iterable[str]) -> dict[str, Any]:
    selected = tuple(selected_platforms)
    if "apple" not in selected:
        raise ContractError("Apple/Codex source activation requires the Apple package")
    lock_path = target / ".agent-skills" / "activation-lock.json"
    current_lock = load(lock_path)
    validate_activation_lock(current_lock)
    migration_report = None
    if current_lock["schema_version"] == "1.0":
        current_lock, migration_report = migrate_activation_lock(current_lock, status="applied")
    current_records = current_lock.get("files") if isinstance(current_lock, dict) else None
    if not isinstance(current_records, list):
        raise ContractError("source activation lock files are invalid")
    current_by_path = {item["path"]: item for item in current_records if isinstance(item, dict) and isinstance(item.get("path"), str)}
    if len(current_by_path) != len(current_records):
        raise ContractError("source activation lock records must be unique")

    candidate_values = {
        asset.destination: (_asset_bytes(target, asset), asset.mode)
        for asset in ACTIVATION_ASSETS
    }
    for relative, (value, mode) in candidate_values.items():
        path = _resolve_child(target, relative, label="activation file")
        current = current_by_path.get(relative)
        if current is not None:
            if (
                path.is_symlink() or not path.is_file()
                or path.stat().st_mode & 0o777 != current.get("mode")
                or _digest(path.read_bytes()) != current.get("sha256")
            ):
                raise ContractError(f"managed activation preimage differs from Lock: {relative}")
        elif _path_exists(path) and (
            path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o777 != mode
            or path.read_bytes() != value
        ):
            raise ContractError(f"refusing to overwrite unmanaged activation destination: {relative}")
    retired = sorted(set(current_by_path) - set(candidate_values))
    for relative in retired:
        path = _resolve_child(target, relative, label="retired activation file")
        record = current_by_path[relative]
        if (
            path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o777 != record.get("mode")
            or _digest(path.read_bytes()) != record.get("sha256")
        ):
            raise ContractError(f"retired activation file preimage differs from Lock: {relative}")
        path.unlink()

    updated: list[str] = []
    for relative, (value, mode) in candidate_values.items():
        path = _resolve_child(target, relative, label="activation file")
        if (
            not path.is_file() or path.is_symlink()
            or path.stat().st_mode & 0o777 != mode or path.read_bytes() != value
        ):
            _atomic_write(path, value, mode)
            updated.append(relative)

    created_profiles: list[str] = []
    for asset in PROFILE_ASSETS:
        path = _resolve_child(target, asset.destination, label="Codex profile")
        if not _path_exists(path):
            _atomic_write(path, _asset_bytes(target, asset), asset.mode)
            created_profiles.append(asset.destination)
        elif path.is_symlink() or not path.is_file():
            raise ContractError(f"Codex profile is unsafe: {asset.destination}")

    config = target / "config.toml"
    config_mode = config.stat().st_mode & 0o777 if config.is_file() and not config.is_symlink() else MANAGED_FILE_MODE
    config_value = _config_candidate(target)
    config_changed = not config.is_file() or config.is_symlink() or config.read_bytes() != config_value
    if config_changed:
        _atomic_write(config, config_value, config_mode)

    records = [
        {"mode": mode, "path": relative, "sha256": _digest(value)}
        for relative, (value, mode) in sorted(candidate_values.items())
    ]
    activation_lock = {
        "files": records,
        "handler": ACTIVATION_HANDLER_ID,
        "manager": "agent-development-skills",
        "schema_version": "2.0",
    }
    _atomic_write(lock_path, dumps(activation_lock).encode("utf-8"), MANAGED_FILE_MODE)
    return {
        "config_changed": config_changed,
        "created_profiles": created_profiles,
        "handler": ACTIVATION_HANDLER_ID,
        "migration": migration_report,
        "retired_files": retired,
        "updated_files": sorted(updated),
    }


_MODEL_INSTRUCTIONS = re.compile(
    r'''^[ \t]*(?:model_instructions_file|"model_instructions_file"|'model_instructions_file')[ \t]*='''
)
_TABLE_HEADER = re.compile(r"^[ \t]*\[")


def _deactivated_config(target: Path) -> tuple[bytes | None, int | None, str]:
    path = target / "config.toml"
    if not _path_exists(path):
        return None, None, "missing"
    if path.is_symlink() or not path.is_file():
        raise ContractError("config.toml must be a regular file")
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text) if text.strip() else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError("config.toml must be valid UTF-8 TOML") from error
    if parsed.get("model_instructions_file") != str(target / "AGENTS.md"):
        return original, path.stat().st_mode & 0o777, "preserved"
    lines = text.splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        if line.lstrip(" \t").startswith("#"):
            continue
        if _TABLE_HEADER.match(line):
            break
        if _MODEL_INSTRUCTIONS.match(line):
            matches.append(index)
    if len(matches) != 1:
        raise ContractError("managed model_instructions_file must be one root-level assignment")
    del lines[matches[0]]
    candidate = "".join(lines).encode("utf-8")
    expected = dict(parsed)
    expected.pop("model_instructions_file")
    try:
        reparsed = tomllib.loads(candidate.decode("utf-8")) if candidate.strip() else {}
    except tomllib.TOMLDecodeError as error:
        raise ContractError("targeted config deactivation is not valid TOML") from error
    if reparsed != expected:
        raise ContractError("targeted config deactivation changed unmanaged values")
    return candidate, path.stat().st_mode & 0o777, "removed-managed-instructions-path"


def apply_source_deactivation(target: Path) -> dict[str, Any]:
    lock_path = target / ".agent-skills" / "activation-lock.json"
    lock = load(lock_path)
    validate_activation_lock(lock)
    records = lock.get("files") if isinstance(lock, dict) else None
    if not isinstance(records, list):
        raise ContractError("source activation lock files are invalid")
    validated: list[Path] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"mode", "path", "sha256"}:
            raise ContractError("source activation lock record is invalid")
        path = _resolve_child(target, record["path"], label="activated file")
        if (
            path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o777 != record["mode"]
            or _digest(path.read_bytes()) != record["sha256"]
        ):
            raise ContractError(f"activated file preimage differs from Lock: {record['path']}")
        validated.append(path)
    config_value, config_mode, config_action = _deactivated_config(target)
    for path in validated:
        path.unlink()
    config = target / "config.toml"
    if config_value is not None and config.read_bytes() != config_value:
        _atomic_write(config, config_value, config_mode or MANAGED_FILE_MODE)
    lock_path.unlink()
    return {
        "config_action": config_action,
        "handler": DEACTIVATION_HANDLER_ID,
        "removed_files": sorted(path.relative_to(target).as_posix() for path in validated),
    }


def _smoke_result(plan: dict[str, Any], node: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    request = build_adapter_request(
        plan,
        node["id"],
        context=context,
        invocation_id=f"source-upgrade-smoke-{node['id']}-1",
    )
    capability = node["capability"]
    if capability.startswith("verification."):
        kind = "validation"
        data: dict[str, Any] = {"level": "affected-tests", "tests": 1}
        if capability.endswith(".auto"):
            data = {
                "executed_validation": [{"kind": "installed-contract-smoke", "status": "passed"}],
                "level": "lint",
            }
    elif capability.startswith("review."):
        kind = "review"
        data = {
            "blocking_issues": [],
            "implementation_actor": "source-upgrade-smoke-builder",
            "reviewer_actor": "source-upgrade-smoke-reviewer",
        }
    elif capability.startswith("implementation."):
        kind = "delivery"
        data = {"changed_files": ["Fixture.swift"]}
    elif capability.startswith("reporting."):
        kind = "delivery"
        data = {"acceptance_matrix": []}
    else:
        kind = "diagnostic"
        data = {"checkpoint": "CP0", "scope": "apple"}
    return {
        "artifacts": [],
        "binding": request["binding"],
        "capability": request["capability"],
        "cleanup": [],
        "evidence": [{
            "artifact_ids": [],
            "data": data,
            "kind": kind,
            "status": "passed" if kind in {"review", "validation"} else "completed",
            "summary": f"{kind} structured evidence",
        }],
        "failure_attribution": {"category": "none", "summary": "no failure"},
        "invocation_id": request["invocation_id"],
        "node_id": request["node_id"],
        "plan_fingerprint": request["plan_fingerprint"],
        "provider": request["provider"],
        "request_id": request["request_id"],
        "schema_version": "1.0",
        "status": "completed",
    }


def run_installed_workflow_smoke(target: Path) -> dict[str, Any]:
    registry = ManifestRegistry.from_directory(target / ".agent-skills" / "packages")
    with tempfile.TemporaryDirectory(prefix="source-upgrade-smoke-") as directory:
        fixture = Path(directory)
        (fixture / "App.xcodeproj").mkdir()
        (fixture / "App.xcodeproj" / "project.pbxproj").write_text("// fixture\n", encoding="utf-8")
        (fixture / "Podfile").write_text("platform :ios, '16.0'\n", encoding="utf-8")
        profile = DiscoveryEngine(registry).discover(fixture)
        policy = PolicyResolver().resolve(
            profile,
            "实现 iOS 功能并补充测试",
            explicit_platforms=["apple"],
        )
        plan = PlanCompiler(registry).compile(profile, policy)
    if plan["status"] != "ready":
        raise ContractError("installed source upgrade smoke did not produce a ready plan")
    missing = sorted({
        node["binding"]["name"]
        for node in plan["nodes"]
        if node["binding"]["kind"] == "skill"
        and not (target / "skills" / node["binding"]["name"] / "SKILL.md").is_file()
    })
    if missing:
        raise ContractError("installed source upgrade smoke references missing Skills: " + ", ".join(missing))
    context = {
        "actors": {
            "implementation_actor": "source-upgrade-smoke-builder",
            "reviewer_actor": "source-upgrade-smoke-reviewer",
        },
        "checkpoints": {"CP0": "completed", "CP1": "in_progress", "CP2": "pending", "CP3": "pending"},
        "target_modules": profile["target_modules"],
        "task": policy["task"],
        "user_constraints": ["narrow verification", "independent reviewer"],
    }
    results = {
        node["id"]: _smoke_result(plan, node, context)
        for node in plan["nodes"]
        if node["binding"]["kind"] != "tool"
    }
    ledger = RecordedAdapterExecutor(results, context=context).run(plan)
    report = delivery_report(plan, ledger)
    if ledger["final_status"] != "completed" or report["status"] != "completed":
        raise ContractError("installed source upgrade smoke did not complete")
    return {
        "final_status": ledger["final_status"],
        "plan_fingerprint": plan["fingerprint"],
        "plan_status": plan["status"],
        "review_status": report["review"]["status"],
        "status": "passed",
    }
