#!/usr/bin/env python3
"""Controlled Desktop Adapter command planning and execution."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent_workflow.adapters import validate_adapter_request, validate_adapter_result  # noqa: E402
from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.models import ContractError, require_version  # noqa: E402
from platforms.desktop.scripts.desktop_discovery import (  # noqa: E402
    validate_environment_profile,
    validate_project_profile,
)


COMMANDS = Path(__file__).resolve().parents[1] / "config" / "desktop-adapters-v1.json"
MODE_CAPABILITIES = {
    "affected-tests": "verification.desktop.affected-tests",
    "build": "build.desktop",
    "interaction": "automation.desktop.interaction",
    "ui-smoke": "verification.desktop.ui-smoke",
}
RESOURCE_KEYS = {
    "affected-tests": ("build-queue:{target-root}",),
    "build": ("build-queue:{target-root}",),
    "interaction": ("desktop-session:{target-root}",),
    "ui-smoke": ("build-queue:{target-root}", "desktop-session:{target-root}"),
}


def plan_command(request: dict[str, Any], mode: str, *, command_path: str | Path = COMMANDS) -> dict[str, Any]:
    validate_adapter_request(request)
    if mode not in MODE_CAPABILITIES or request["capability"] != MODE_CAPABILITIES[mode]:
        raise ContractError("desktop adapter mode conflicts with requested capability")
    context = request["task_context"]
    profile = context.get("desktop_project_profile")
    environment = context.get("desktop_environment_profile")
    validate_project_profile(profile)
    validate_environment_profile(environment)
    if context.get("environment_fingerprint") != environment["fingerprint"]:
        raise ContractError("desktop adapter environment fingerprint is stale")
    if profile["status"] != "supported" or profile["selected_framework"] is None or profile["module_root"] is None:
        return _blocked_plan(request, mode, "desktop framework or module root is unresolved")
    repository_root = Path(profile["repository_root"]).resolve()
    module_root = _resolve_module_root(repository_root, profile["module_root"])
    config = load(command_path)
    _validate_command_config(config)
    permission_status = {item["name"]: item["status"] for item in environment["permissions"]}
    unavailable_permissions = [
        f"{name}:{permission_status[name]}"
        for name in config["mode_permissions"][mode]
        if permission_status[name] not in {"granted", "not-required"}
    ]
    if unavailable_permissions:
        return _blocked_plan(
            request,
            mode,
            "required desktop permissions are unavailable: " + ",".join(unavailable_permissions),
        )
    adapter = config["adapters"].get(profile["selected_framework"])
    if adapter is None:
        return _blocked_plan(request, mode, "framework adapter is not registered")
    if mode == "interaction":
        return _blocked_plan(request, mode, "interaction requires a framework-specific session adapter")
    command = adapter[mode]
    if command is None:
        reason = "native macOS execution is delegated to the selected Apple verification provider" if profile["selected_framework"] == "native-macos" else f"{mode} is unsupported by this framework adapter"
        return _blocked_plan(request, mode, reason)
    required_path = command.get("required_path")
    if required_path is not None:
        required = _safe_child(module_root, required_path)
        if not required.exists() or required.is_symlink():
            return _blocked_plan(request, mode, f"required adapter path is missing: {required_path}")
    required_script = command.get("required_json_script")
    if required_script is not None:
        package_path = _safe_child(module_root, command["required_path"])
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return _blocked_plan(request, mode, f"cannot read required package script: {error}")
        scripts = package.get("scripts")
        if not isinstance(scripts, dict) or not isinstance(scripts.get(required_script), str) or not scripts[required_script].strip():
            return _blocked_plan(request, mode, f"required package script is missing: {required_script}")
    argv = [part.replace("{module_root}", str(module_root)) for part in command["argv"]]
    return {
        "argv": argv,
        "cwd": str(module_root),
        "mode": mode,
        "reason": None,
        "resource_keys": [key.replace("{target-root}", str(repository_root)) for key in RESOURCE_KEYS[mode]],
        "status": "ready",
    }


def run_adapter(
    request: dict[str, Any],
    mode: str,
    *,
    execute: bool,
    runner: Callable[..., Any] = subprocess.run,
    command_path: str | Path = COMMANDS,
) -> dict[str, Any]:
    plan = plan_command(request, mode, command_path=command_path)
    if plan["status"] == "blocked" or not execute:
        reason = plan["reason"] if plan["status"] == "blocked" else "execution requires an explicit execute flag"
        return _blocked_result(request, mode, reason, plan["resource_keys"])
    if shutil.which(plan["argv"][0]) is None:
        return _blocked_result(request, mode, f"required tool is unavailable: {plan['argv'][0]}", plan["resource_keys"])

    context = request["task_context"]
    repository_root = Path(context["desktop_project_profile"]["repository_root"]).resolve()
    raw_artifact_directory = context.get("artifact_directory", ".agent-workflow/desktop-artifacts")
    if not isinstance(raw_artifact_directory, str) or not raw_artifact_directory:
        raise ContractError("desktop adapter artifact_directory is invalid")
    artifact_directory = _safe_child(repository_root, raw_artifact_directory)
    artifact_directory.mkdir(parents=True, exist_ok=True)
    timeout = context.get("timeout_seconds", 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ContractError("desktop adapter timeout_seconds is invalid")
    retention = context.get("artifact_retention", "task-scoped")
    if retention not in {"task-scoped", "until-run-complete"}:
        raise ContractError("desktop adapter artifact_retention is invalid")
    try:
        completed = runner(
            plan["argv"],
            cwd=plan["cwd"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return_code = completed.returncode
        output = f"$ {' '.join(plan['argv'])}\nexit_code={return_code}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        status = "completed" if return_code == 0 else "failed"
        attribution = {"category": "none", "summary": "Desktop adapter command completed."} if return_code == 0 else {"category": "code", "summary": "Desktop adapter command returned a non-zero exit code."}
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        output = f"$ {' '.join(plan['argv'])}\nblocked={type(error).__name__}\n"
        status = "blocked"
        attribution = {"category": "environment", "summary": "Desktop adapter command timed out or was cancelled."}
    except OSError as error:
        output = f"$ {' '.join(plan['argv'])}\nblocked={error}\n"
        status = "blocked"
        attribution = {"category": "environment", "summary": "Desktop adapter command could not start."}
    artifact_path = artifact_directory / f"{request['request_id']}-{mode}.log"
    artifact_path.write_text(output, encoding="utf-8")
    artifact_id = f"desktop-{mode}-log"
    evidence_kind = "validation" if request["capability"].startswith("verification.") else "diagnostic"
    evidence_status = "passed" if status == "completed" and evidence_kind == "validation" else "completed" if status == "completed" else status
    result = {
        "artifacts": [{
            "artifact_id": artifact_id,
            "kind": "test-report" if evidence_kind == "validation" else "raw-log",
            "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "uri": f"artifact://desktop/{request['request_id']}/{mode}.log",
        }],
        "binding": request["binding"],
        "capability": request["capability"],
        "cleanup": [
            {"detail": "Resource release recorded after command completion.", "resource": resource, "status": "completed"}
            for resource in plan["resource_keys"]
        ],
        "evidence": [{
            "artifact_ids": [artifact_id],
            "data": {
                "command": plan["argv"],
                "environment_fingerprint": context["environment_fingerprint"],
                "exit_code": return_code if "return_code" in locals() else None,
                "retention": retention,
            },
            "kind": evidence_kind,
            "status": evidence_status,
            "summary": attribution["summary"],
        }],
        "failure_attribution": attribution,
        "invocation_id": request["invocation_id"],
        "node_id": request["node_id"],
        "plan_fingerprint": request["plan_fingerprint"],
        "provider": request["provider"],
        "request_id": request["request_id"],
        "schema_version": "1.0",
        "status": status,
    }
    validate_adapter_result(request, result)
    return result


def _blocked_plan(request: dict[str, Any], mode: str, reason: str) -> dict[str, Any]:
    repository_root = request["task_context"].get("desktop_project_profile", {}).get("repository_root", "unresolved")
    return {
        "argv": [],
        "cwd": None,
        "mode": mode,
        "reason": reason,
        "resource_keys": [key.replace("{target-root}", repository_root) for key in RESOURCE_KEYS[mode]],
        "status": "blocked",
    }


def _blocked_result(request: dict[str, Any], mode: str, reason: str, resources: list[str]) -> dict[str, Any]:
    result = {
        "artifacts": [],
        "binding": request["binding"],
        "capability": request["capability"],
        "cleanup": [
            {"detail": "Resource was not acquired.", "resource": resource, "status": "not-required"}
            for resource in resources
        ],
        "evidence": [{
            "artifact_ids": [],
            "data": {"mode": mode, "reason": reason},
            "kind": "diagnostic",
            "status": "blocked",
            "summary": reason,
        }],
        "failure_attribution": {"category": "environment", "summary": reason},
        "invocation_id": request["invocation_id"],
        "node_id": request["node_id"],
        "plan_fingerprint": request["plan_fingerprint"],
        "provider": request["provider"],
        "request_id": request["request_id"],
        "schema_version": "1.0",
        "status": "blocked",
    }
    if request["capability"].startswith("verification."):
        result["no_test_reason"] = reason
        result["suggested_validation"] = "Install or extend the selected framework adapter, then rerun the scoped verification."
    validate_adapter_result(request, result)
    return result


def _validate_command_config(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"schema_version", "adapters", "mode_permissions"}:
        raise ContractError("desktop adapter command matrix fields are invalid")
    require_version(value)
    permissions = value["mode_permissions"]
    if not isinstance(permissions, dict) or set(permissions) != set(MODE_CAPABILITIES):
        raise ContractError("desktop adapter permission matrix is invalid")
    allowed_permissions = {"automation", "filesystem", "installer-elevation", "network", "notification"}
    for mode, required in permissions.items():
        if not isinstance(required, list) or required != sorted(set(required)) or set(required) - allowed_permissions:
            raise ContractError(f"desktop adapter required permissions are invalid for {mode}")
    if not isinstance(value["adapters"], dict) or not value["adapters"]:
        raise ContractError("desktop adapter command matrix is empty")
    for adapter in value["adapters"].values():
        if not isinstance(adapter, dict) or set(adapter) != {"build", "affected-tests", "ui-smoke"}:
            raise ContractError("desktop adapter mode matrix is invalid")
        for command in adapter.values():
            if command is None:
                continue
            if not isinstance(command, dict) or not {"argv", "required_path"} <= set(command) <= {"argv", "required_path", "required_json_script"}:
                raise ContractError("desktop adapter command fields are invalid")
            argv = command["argv"]
            if not isinstance(argv, list) or not argv or any(not isinstance(part, str) or not part for part in argv):
                raise ContractError("desktop adapter argv is invalid")
            for field in ("required_path", "required_json_script"):
                if field in command and command[field] is not None and (not isinstance(command[field], str) or not command[field]):
                    raise ContractError(f"desktop adapter {field} is invalid")


def _resolve_module_root(repository_root: Path, relative: str) -> Path:
    return repository_root if relative == "." else _safe_child(repository_root, relative)


def _safe_child(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("desktop adapter relative path is unsafe")
    resolved = (root / Path(*path.parts)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ContractError("desktop adapter path escapes repository root") from error
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(MODE_CAPABILITIES))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    request = load(arguments.request)
    sys.stdout.write(dumps(run_adapter(request, arguments.mode, execute=arguments.execute)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        raise SystemExit(2)
