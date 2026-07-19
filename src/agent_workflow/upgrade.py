"""Phase 6 upgrade planning, approval and rollback lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from .canonical_json import load, sha256
from .activation import (
    ACTIVATION_HANDLER_ID,
    DEACTIVATION_HANDLER_ID,
    PRESERVE_HANDLER_ID,
    activation_handler_sha256,
    apply_source_activation,
    apply_source_deactivation,
    deactivation_external_paths,
    external_paths as activation_external_paths,
    run_installed_workflow_smoke,
)
from .contracts import (
    validate_rollback_point,
    validate_upgrade_conformance_evidence,
    validate_upgrade_plan,
    validate_upgrade_source_qualification,
)
from .doctor import diagnose_install
from .installation import (
    InstallBundle,
    EXTERNAL_ACTIVATION_LOCK,
    PERSISTENT_PACKAGE_LOCK,
    build_install_bundle,
    install_bundle,
    preview_rollback_point,
    rollback_install,
    target_lifecycle_lock,
)
from .models import ContractError
from .migrations import DEFAULT_MIGRATION_REGISTRY, migrate_activation_lock
from .package_lock import diff_package_locks


UPGRADE_SCHEMA_VERSION = "1.0"
CONFORMANCE_SUITE = "agent-skills-release-conformance-v1"


@dataclass(frozen=True)
class UpgradeCandidate:
    bundle: InstallBundle
    selection: dict[str, Any]


@dataclass(frozen=True)
class UpgradeOperation:
    plan: dict[str, Any]
    candidate: UpgradeCandidate
    conformance_evidence: dict[str, Any]
    external_paths: tuple[str, ...] = ()


def make_upgrade_conformance_evidence(
    package_lock: dict[str, Any],
    *,
    manifest_count: int,
    negative_contract_count: int,
    test_count: int,
    suite_definition_hash: str,
    runner_sha256: str,
    environment: dict[str, str],
    command_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build evidence metadata after a caller has completed the named suite."""

    evidence: dict[str, Any] = {
        "candidate_package_lock_hash": package_lock["fingerprint"],
        "command_results": command_results,
        "environment": environment,
        "manifest_count": manifest_count,
        "negative_contract_count": negative_contract_count,
        "schema_inventory_hash": package_lock["schema_inventory"]["content_sha256"],
        "schema_version": UPGRADE_SCHEMA_VERSION,
        "runner_sha256": runner_sha256,
        "status": "passed",
        "suite": CONFORMANCE_SUITE,
        "suite_definition_hash": suite_definition_hash,
        "test_count": test_count,
    }
    evidence["attestation_key"] = sha256({
        **evidence,
        "command_results": [
            {"command": item["command"], "exit_code": item["exit_code"]}
            for item in command_results
        ],
    })
    evidence["fingerprint"] = sha256(evidence)
    validate_upgrade_conformance_evidence(evidence)
    return evidence


def make_upgrade_source_qualification(
    conformance_evidence: dict[str, Any],
    *,
    source_revision: str,
    source_artifact_sha256: str,
    source_artifact_size: int,
    source_root: str,
    source_materials_sha256: str,
) -> dict[str, Any]:
    """Bind completed repository Conformance to one immutable source archive."""

    validate_upgrade_conformance_evidence(conformance_evidence)
    qualification: dict[str, Any] = {
        key: value
        for key, value in conformance_evidence.items()
        if key not in {
            "attestation_key",
            "candidate_package_lock_hash",
            "fingerprint",
        }
    }
    qualification["source"] = {
        "artifact_sha256": source_artifact_sha256,
        "artifact_size": source_artifact_size,
        "revision": source_revision,
        "root": source_root,
    }
    qualification["source_materials_sha256"] = source_materials_sha256
    qualification["attestation_key"] = sha256({
        **qualification,
        "command_results": [
            {"command": item["command"], "exit_code": item["exit_code"]}
            for item in qualification["command_results"]
        ],
    })
    qualification["fingerprint"] = sha256(qualification)
    validate_upgrade_source_qualification(qualification)
    return qualification


def run_upgrade_conformance(
    platform_root: str | Path,
    package_lock: dict[str, Any],
) -> dict[str, Any]:
    """Run the repository-owned suite and return its candidate-bound receipt."""

    root = Path(platform_root).resolve().parent
    runner = root / "scripts" / "run_conformance.py"
    if runner.is_symlink() or not runner.is_file():
        raise ContractError("upgrade Conformance runner is missing or unsafe")

    def digest() -> str:
        return hashlib.sha256(runner.read_bytes()).hexdigest()

    before = digest()
    with tempfile.TemporaryDirectory(prefix="agent-skills-upgrade-conformance-") as directory:
        lock_path = Path(directory) / "candidate.lock"
        from .canonical_json import dump

        dump(package_lock, lock_path)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root / "src")
        completed = subprocess.run(
            [sys.executable, str(runner), "--upgrade-lock", str(lock_path)],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    after = digest()
    if before != after:
        raise ContractError("upgrade Conformance runner changed during execution")
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise ContractError(f"upgrade Conformance failed: {detail}")
    try:
        import json

        evidence = json.loads(completed.stdout)
    except (ValueError, TypeError) as error:
        raise ContractError("upgrade Conformance runner returned invalid evidence") from error
    validate_upgrade_conformance_evidence(evidence)
    if (
        evidence["runner_sha256"] != before
        or evidence["candidate_package_lock_hash"] != package_lock["fingerprint"]
        or evidence["schema_inventory_hash"] != package_lock["schema_inventory"]["content_sha256"]
    ):
        raise ContractError("upgrade Conformance evidence differs from the executed runner or candidate")
    return evidence


def prepare_upgrade_candidate(
    platform_root: str | Path,
    target_root: str | Path,
    *,
    platforms: Iterable[str] | None = None,
    disciplines: Iterable[str] | None = None,
    runtime_configs: Iterable[str] | None = None,
    core_only: bool | None = None,
) -> UpgradeCandidate:
    target = _safe_target(target_root)
    install_lock, current_lock = _load_current_locks(target)
    selection = _resolve_selection(
        install_lock,
        platforms=platforms,
        disciplines=disciplines,
        runtime_configs=runtime_configs,
        core_only=core_only,
    )
    base = build_install_bundle(
        platform_root,
        platforms=selection["platforms"],
        disciplines=selection["disciplines"],
        runtime_configs=selection["runtime_configs"],
        core_only=selection["core_only"],
        schema_root=Path(platform_root).resolve().parent / "schemas",
    )
    if _semantic_lock_identity(base.package_lock) == _semantic_lock_identity(current_lock):
        return UpgradeCandidate(bundle=base, selection=selection)
    candidate = build_install_bundle(
        platform_root,
        platforms=selection["platforms"],
        disciplines=selection["disciplines"],
        runtime_configs=selection["runtime_configs"],
        core_only=selection["core_only"],
        previous_lock=current_lock,
        schema_root=Path(platform_root).resolve().parent / "schemas",
    )
    return UpgradeCandidate(bundle=candidate, selection=selection)


def plan_upgrade(
    platform_root: str | Path,
    target_root: str | Path,
    conformance_evidence: dict[str, Any],
    *,
    schema_root: str | Path,
    platforms: Iterable[str] | None = None,
    disciplines: Iterable[str] | None = None,
    runtime_configs: Iterable[str] | None = None,
    core_only: bool | None = None,
    external_paths: Iterable[str] = (),
    external_handler: str = "none",
    external_handler_sha256: str | None = None,
    action: str = "upgrade",
    removed_platforms: Iterable[str] = (),
    removed_runtime_configs: Iterable[str] = (),
) -> UpgradeOperation:
    target = _safe_target(target_root)
    if action not in {"partial-uninstall", "upgrade"}:
        raise ContractError("upgrade action is invalid")
    doctor = diagnose_install(target, schema_root=schema_root)
    if doctor["status"] != "passed":
        raise ContractError("upgrade requires a passing Doctor report for the current installation")
    install_lock, current_lock = _load_current_locks(target)
    candidate = prepare_upgrade_candidate(
        platform_root,
        target,
        platforms=platforms,
        disciplines=disciplines,
        runtime_configs=runtime_configs,
        core_only=core_only,
    )
    validate_upgrade_conformance_evidence(conformance_evidence)
    candidate_lock = candidate.bundle.package_lock
    raw_removed_platforms = tuple(removed_platforms)
    raw_removed_runtime_configs = tuple(removed_runtime_configs)
    removed_platform_values = sorted(set(raw_removed_platforms))
    removed_runtime_values = sorted(set(raw_removed_runtime_configs))
    if len(removed_platform_values) != len(raw_removed_platforms) or len(removed_runtime_values) != len(raw_removed_runtime_configs):
        raise ContractError("upgrade removal request must be unique")
    lock_schema_path = DEFAULT_MIGRATION_REGISTRY.path(
        "agent-skills-lock",
        current_lock["schema_version"],
        candidate_lock["schema_version"],
    )
    install_schema_path = DEFAULT_MIGRATION_REGISTRY.path(
        "install-plan-lock",
        install_lock.get("lock_schema_version", "legacy"),
        candidate.bundle.plan.get("lock_schema_version", "legacy"),
    )
    if lock_schema_path or install_schema_path:
        raise ContractError(
            "upgrade schema transforms are not implemented; only identity compatibility is supported"
        )
    if (
        conformance_evidence["candidate_package_lock_hash"] != candidate_lock["fingerprint"]
        or conformance_evidence["schema_inventory_hash"]
        != candidate_lock["schema_inventory"]["content_sha256"]
    ):
        raise ContractError("upgrade Conformance evidence is stale for the candidate Lockfile")
    migration_reports: list[dict[str, Any]] = []
    activation_lock_path = target / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK
    if action == "upgrade" and external_handler == ACTIVATION_HANDLER_ID and activation_lock_path.is_file():
        activation_lock = load(activation_lock_path)
        if activation_lock.get("schema_version") != "2.0":
            _, migration_report = migrate_activation_lock(activation_lock, status="planned")
            migration_reports.append(migration_report)
    semantic_change = (
        _semantic_lock_identity(current_lock) != _semantic_lock_identity(candidate_lock)
        or bool(migration_reports)
    )
    if action == "partial-uninstall":
        _validate_partial_uninstall_purity(current_lock, candidate_lock)
    normalized_external_paths = tuple(external_paths)
    if normalized_external_paths != tuple(sorted(set(normalized_external_paths))):
        raise ContractError("upgrade external lifecycle paths must be sorted and unique")
    if (external_handler == "none") != (not normalized_external_paths):
        raise ContractError("upgrade external lifecycle scope requires one bound handler")
    handler_sha256 = external_handler_sha256 or sha256("none")
    if not isinstance(handler_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", handler_sha256):
        raise ContractError("upgrade external lifecycle handler hash is invalid")
    if external_handler != "none":
        if external_handler not in {ACTIVATION_HANDLER_ID, DEACTIVATION_HANDLER_ID, PRESERVE_HANDLER_ID} or handler_sha256 != activation_handler_sha256():
            raise ContractError("upgrade external lifecycle handler is not a trusted Core handler")
        expected_paths = (
            activation_external_paths(target)
            if external_handler == ACTIVATION_HANDLER_ID
            else deactivation_external_paths(target)
        )
        if normalized_external_paths != tuple(sorted(expected_paths)):
            raise ContractError("source activation paths differ from the trusted Core handler scope")
    if action == "partial-uninstall":
        activation_lock = target / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK
        activation_managed = activation_lock.is_file() and not activation_lock.is_symlink()
        current_platforms = set(install_lock["selected_platforms"])
        current_runtime_configs = set(install_lock["selected_runtime_configs"])
        removes_activated_apple = (
            activation_managed
            and "apple" in current_platforms
            and "codex" in current_runtime_configs
            and "apple" in removed_platform_values
        )
        expected_removed_runtime = ["codex"] if removes_activated_apple else []
        if removed_runtime_values != expected_removed_runtime:
            raise ContractError(
                "partial uninstall may remove only activation-owned codex with an activated Apple platform"
            )
        expected_handler = (
            DEACTIVATION_HANDLER_ID
            if removes_activated_apple
            else PRESERVE_HANDLER_ID if activation_managed else "none"
        )
        if external_handler != expected_handler:
            raise ContractError(
                "partial uninstall external handler differs from the platform activation ownership policy"
            )
    if (
        action == "upgrade"
        and
        semantic_change
        and (target / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK).exists()
        and not normalized_external_paths
    ):
        raise ContractError("source-installer activation upgrade requires an external rollback scope")
    changes = diff_package_locks(current_lock, candidate_lock)
    if migration_reports and changes["status"] == "unchanged":
        changes = dict(changes)
        changes["status"] = "changed"
        changes["fingerprint"] = sha256({
            key: value for key, value in changes.items() if key != "fingerprint"
        })
    elif not semantic_change:
        changes = dict(changes)
        changes["status"] = "unchanged"
        changes["fingerprint"] = sha256({
            key: value for key, value in changes.items() if key != "fingerprint"
        })
    approvals = _permission_approvals(current_lock, candidate_lock)
    upgrade_steps = _upgrade_steps(current_lock, candidate_lock)
    rollback_point = preview_rollback_point(target, external_paths=normalized_external_paths)
    plan: dict[str, Any] = {
        "action": action,
        "approvals_required": approvals,
        "candidate": {
            "install_plan_fingerprint": candidate.bundle.plan["fingerprint"],
            "package_lock_hash": candidate_lock["fingerprint"],
        },
        "compatibility": {
            "agent_skills_lock": "identity",
            "install_plan_lock": "identity",
            "mode": "identity-only",
        },
        "changes": changes,
        "conformance_attestation_key": conformance_evidence["attestation_key"],
        "external": {
            "handler": external_handler,
            "handler_sha256": handler_sha256,
            "path_count": len(normalized_external_paths),
            "paths_sha256": sha256(list(normalized_external_paths)),
        },
        "current": {
            "install_plan_fingerprint": install_lock["fingerprint"],
            "package_lock_hash": current_lock["fingerprint"],
        },
        "current_selection": {
            "core_only": not install_lock["selected_platforms"]
            and not install_lock["selected_disciplines"]
            and not install_lock["selected_runtime_configs"],
            "disciplines": list(install_lock["selected_disciplines"]),
            "platforms": list(install_lock["selected_platforms"]),
            "runtime_configs": list(install_lock["selected_runtime_configs"]),
        },
        "migrations": migration_reports,
        "upgrade_steps": upgrade_steps,
        "rollback": {
            "point_id": rollback_point["point_id"],
            "point_fingerprint": rollback_point["fingerprint"],
            "previous_lock_hash": current_lock["fingerprint"],
        },
        "schema_version": UPGRADE_SCHEMA_VERSION,
        "removed_platforms": removed_platform_values,
        "removed_runtime_configs": removed_runtime_values,
        "selection": candidate.selection,
        "status": "planned" if semantic_change else "no-change",
        "target_root": str(target),
    }
    plan["fingerprint"] = sha256(plan)
    validate_upgrade_plan(plan)
    return UpgradeOperation(
        plan=plan,
        candidate=candidate,
        conformance_evidence=conformance_evidence,
        external_paths=normalized_external_paths,
    )


def apply_upgrade(
    operation: UpgradeOperation,
    target_root: str | Path,
    *,
    approve_plan: str,
    approvals: Iterable[str] = (),
) -> dict[str, Any]:
    plan = operation.plan
    validate_upgrade_plan(plan)
    validate_upgrade_conformance_evidence(operation.conformance_evidence)
    target = _safe_target(target_root)
    if str(target) != plan["target_root"]:
        raise ContractError("upgrade plan target differs from apply target")
    if approve_plan != plan["fingerprint"]:
        raise ContractError("upgrade apply requires the exact planned fingerprint")
    supplied = sorted(set(approvals))
    if supplied != plan["approvals_required"]:
        missing = sorted(set(plan["approvals_required"]) - set(supplied))
        extra = sorted(set(supplied) - set(plan["approvals_required"]))
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unexpected: " + ", ".join(extra))
        raise ContractError("upgrade permission approvals differ from plan: " + "; ".join(details))
    if (
        plan["external"]["paths_sha256"] != sha256(list(operation.external_paths))
        or plan["external"]["path_count"] != len(operation.external_paths)
    ):
        raise ContractError("upgrade external lifecycle handler differs from the approved plan")
    post_install = _bound_external_handler(operation)
    with target_lifecycle_lock(target) as lifecycle_token:
        install_lock, package_lock = _load_current_locks(target)
        if (
            install_lock["fingerprint"] != plan["current"]["install_plan_fingerprint"]
            or package_lock["fingerprint"] != plan["current"]["package_lock_hash"]
        ):
            raise ContractError("upgrade plan is stale for the current installation")
        if (
            operation.candidate.bundle.plan["fingerprint"] != plan["candidate"]["install_plan_fingerprint"]
            or operation.candidate.bundle.package_lock["fingerprint"] != plan["candidate"]["package_lock_hash"]
            or operation.conformance_evidence["attestation_key"] != plan["conformance_attestation_key"]
            or operation.conformance_evidence["candidate_package_lock_hash"]
            != operation.candidate.bundle.package_lock["fingerprint"]
        ):
            raise ContractError("upgrade candidate or Conformance evidence differs from the approved plan")
        if plan["status"] == "no-change":
            return {
                "conformance_evidence_fingerprint": operation.conformance_evidence["fingerprint"],
                "plan_fingerprint": plan["fingerprint"],
                "status": "no-change",
            }
        result = install_bundle(
            operation.candidate.bundle,
            target,
            persistent_rollback=True,
            lifecycle_token=lifecycle_token,
            expected_install_fingerprint=plan["current"]["install_plan_fingerprint"],
            expected_package_lock_hash=plan["current"]["package_lock_hash"],
            expected_rollback_point_fingerprint=plan["rollback"]["point_fingerprint"],
            persistent_rollback_external_paths=operation.external_paths,
            post_install=post_install,
        )
    return {
        "install_plan_fingerprint": result["fingerprint"],
        "package_lock_hash": result["package_lock_hash"],
        "conformance_evidence_fingerprint": operation.conformance_evidence["fingerprint"],
        "plan_fingerprint": plan["fingerprint"],
        "rollback_point": plan["rollback"],
        "status": "upgraded",
    }


def _bound_external_handler(operation: UpgradeOperation):
    external = operation.plan["external"]
    handler = external["handler"]
    if handler == "none":
        if operation.external_paths:
            raise ContractError("upgrade external paths exist without a bound handler")
        return None
    if handler not in {ACTIVATION_HANDLER_ID, DEACTIVATION_HANDLER_ID, PRESERVE_HANDLER_ID} or external["handler_sha256"] != activation_handler_sha256():
        raise ContractError("upgrade external handler is unknown or its implementation changed")
    expected_paths = tuple(sorted(
        activation_external_paths(Path(operation.plan["target_root"]))
        if handler == ACTIVATION_HANDLER_ID
        else deactivation_external_paths(Path(operation.plan["target_root"]))
    ))
    if expected_paths != operation.external_paths:
        raise ContractError("source activation scope differs from the approved upgrade plan")
    selected_platforms = tuple(operation.plan["selection"]["platforms"])

    def apply_bound_handler(installed_target: Path, _: dict[str, Any]) -> None:
        if handler == ACTIVATION_HANDLER_ID:
            run_installed_workflow_smoke(installed_target)
            apply_source_activation(installed_target, selected_platforms=selected_platforms)
        elif handler == DEACTIVATION_HANDLER_ID:
            apply_source_deactivation(installed_target)

    return apply_bound_handler


def rollback_upgrade(
    target_root: str | Path,
    *,
    approve_current_lock: str,
    approve_rollback_point: str,
) -> dict[str, Any]:
    target = _safe_target(target_root)
    with target_lifecycle_lock(target) as lifecycle_token:
        _, current_lock = _load_current_locks(target)
        if current_lock["fingerprint"] != approve_current_lock:
            raise ContractError("rollback requires approval of the exact current Lockfile")
        point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
        validate_rollback_point(point)
        if point["fingerprint"] != approve_rollback_point:
            raise ContractError("rollback requires approval of the exact rollback point")
        result = rollback_install(
            target,
            lifecycle_token=lifecycle_token,
            expected_current_lock_hash=approve_current_lock,
            expected_rollback_point_fingerprint=approve_rollback_point,
        )
    validate_rollback_point(result["rollback_point"])
    return result


def _safe_target(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ContractError(f"upgrade target must not be a symlink: {raw}")
    target = raw.resolve()
    if not target.is_dir():
        raise ContractError(f"upgrade target must be a directory: {target}")
    return target


def _load_current_locks(target: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    install_lock = load(target / ".agent-skills" / "install-lock.json")
    package_lock = load(target / ".agent-skills" / PERSISTENT_PACKAGE_LOCK)
    from .contracts import validate_install_plan
    from .package_lock import validate_package_lock

    validate_install_plan(install_lock)
    validate_package_lock(package_lock)
    if install_lock["status"] != "installed" or install_lock.get("package_lock_hash") != package_lock["fingerprint"]:
        raise ContractError("upgrade target Lockfiles are not an anchored installed state")
    return install_lock, package_lock


def _resolve_selection(
    install_lock: dict[str, Any],
    *,
    platforms: Iterable[str] | None,
    disciplines: Iterable[str] | None,
    runtime_configs: Iterable[str] | None,
    core_only: bool | None,
) -> dict[str, Any]:
    current = {
        "platforms": list(install_lock["selected_platforms"]),
        "disciplines": list(install_lock["selected_disciplines"]),
        "runtime_configs": list(install_lock["selected_runtime_configs"]),
    }
    def explicit(values: Iterable[str] | None, fallback: list[str], label: str) -> list[str]:
        if values is None:
            return fallback
        items = list(values)
        if any(not isinstance(item, str) or not item for item in items) or len(items) != len(set(items)):
            raise ContractError(f"upgrade {label} selection must contain unique non-empty ids")
        return sorted(items)

    selected = {
        "platforms": explicit(platforms, current["platforms"], "platform"),
        "disciplines": explicit(disciplines, current["disciplines"], "discipline"),
        "runtime_configs": explicit(runtime_configs, current["runtime_configs"], "runtime config"),
    }
    inferred_core_only = not any(selected.values())
    if core_only is not None and core_only != inferred_core_only:
        raise ContractError("upgrade core-only selection conflicts with selected packages")
    return {"core_only": inferred_core_only, **selected}


def _semantic_lock_identity(value: dict[str, Any]) -> str:
    return sha256({
        key: item
        for key, item in value.items()
        if key not in {"fingerprint", "install_plan_identity_hash", "lineage"}
    })


def _validate_partial_uninstall_purity(before: dict[str, Any], after: dict[str, Any]) -> None:
    before_packages = {item["id"]: item for item in before["packages"]}
    after_packages = {item["id"]: item for item in after["packages"]}
    if not set(after_packages) < set(before_packages):
        raise ContractError("partial uninstall must remove at least one package without adding packages")
    changed = sorted(
        package_id
        for package_id, record in after_packages.items()
        if before_packages.get(package_id) != record
    )
    if changed:
        raise ContractError(
            "partial uninstall would upgrade or modify remaining packages: " + ", ".join(changed)
        )
    if after["core"] != before["core"] or after["schema_inventory"] != before["schema_inventory"]:
        raise ContractError("partial uninstall would change Core or Schema identity")


def _permission_approvals(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    old = before["capability_providers"]
    new = after["capability_providers"]
    approvals = []
    for capability in sorted(new):
        previous = old.get(capability, {}).get("permission_profile", "none")
        current = new[capability]["permission_profile"]
        if previous != current:
            approvals.append(f"permission:{capability}:{previous}->{current}")
    return approvals


def _upgrade_steps(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    if before["core"]["runtime_version"] != after["core"]["runtime_version"]:
        steps.append({"kind": "core", "from": before["core"]["runtime_version"], "to": after["core"]["runtime_version"]})
    if before["schema_inventory"]["content_sha256"] != after["schema_inventory"]["content_sha256"]:
        steps.append({
            "kind": "schema",
            "from": before["schema_inventory"]["content_sha256"],
            "to": after["schema_inventory"]["content_sha256"],
        })
    before_packages = {item["id"]: item for item in before["packages"]}
    after_packages = {item["id"]: item for item in after["packages"]}
    for package_id in sorted(set(before_packages) | set(after_packages)):
        old = before_packages.get(package_id)
        new = after_packages.get(package_id)
        old_identity = "absent" if old is None else f"{old['version']}@{old['source']['sha256']}"
        new_identity = "absent" if new is None else f"{new['version']}@{new['source']['sha256']}"
        if old_identity != new_identity:
            steps.append({"kind": "package", "from": f"{package_id}:{old_identity}", "to": f"{package_id}:{new_identity}"})
    if _semantic_lock_identity(before) != _semantic_lock_identity(after):
        steps.append({"kind": "lock", "from": before["fingerprint"], "to": after["fingerprint"]})
    rank = {"core": 0, "schema": 1, "package": 2, "lock": 3}
    return sorted(steps, key=lambda item: (rank[item["kind"]], item["from"], item["to"]))
