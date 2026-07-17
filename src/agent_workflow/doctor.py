"""Read-only installation diagnostics for the Phase 6 lifecycle."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from typing import Any, Callable

from . import __version__ as CORE_VERSION
from .canonical_json import load, sha256
from .contracts import (
    validate_activation_lock,
    validate_doctor_report,
    validate_install_plan,
    validate_manifest,
)
from .installation import (
    EXTERNAL_ACTIVATION_LOCK,
    EXTERNAL_SKILL_ROOTS,
    MANAGED_DIRECTORY_MODE,
    MANAGED_FILE_MODE,
    LIFECYCLE_LOCK_DIRECTORY,
    PERSISTENT_PACKAGE_LOCK,
    ROLLBACK_POINT_DIRECTORY,
    _validate_rollback_point_directory,
    derive_installed_package_semantics,
    _is_ignored_os_metadata,
    _path_exists,
    _resolve_child,
    _snapshot_tree,
    _tree_matches_record,
)
from .models import ContractError
from .package_lock import install_plan_identity_hash, schema_inventory, validate_package_lock


DOCTOR_SCHEMA_VERSION = "1.0"
PYTHON_REQUIREMENT = ">=3.11"
_RECOVERY_PREFIXES = (
    (".agent-skills-backup-", "install-backup"),
    (".agent-skills-stage-", "install-stage"),
    (".agent-skills-uninstall-backup-", "uninstall-backup"),
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_file(path: Path, *, mode: int, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or unsafe")
    if path.stat().st_mode & 0o777 != mode:
        raise ContractError(f"{label} mode is not canonical")


def _require_directory(path: Path, *, mode: int | None, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ContractError(f"{label} is missing or unsafe")
    if mode is not None and path.stat().st_mode & 0o777 != mode:
        raise ContractError(f"{label} mode is not canonical")


def _error_details(error: Exception) -> dict[str, Any]:
    return {"errors": [str(error) or error.__class__.__name__]}


def diagnose_install(
    target_root: str | Path,
    *,
    schema_root: str | Path,
) -> dict[str, Any]:
    """Return a deterministic, canonical diagnostic report without modifying the target."""

    target = Path(target_root).expanduser().absolute()
    schemas = Path(schema_root).expanduser().absolute()
    checks: list[dict[str, Any]] = []

    def record(
        check_id: str,
        category: str,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        checks.append({
            "category": category,
            "details": details or {},
            "id": check_id,
            "status": status,
            "summary": summary,
        })

    def run_check(
        check_id: str,
        category: str,
        summary: str,
        action: Callable[[], dict[str, Any] | None],
    ) -> None:
        try:
            details = action() or {}
        except (ContractError, KeyError, OSError, TypeError, UnicodeError, ValueError) as error:
            record(check_id, category, "failed", summary, _error_details(error))
        else:
            record(check_id, category, "passed", summary, details)

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info[:2] >= (3, 11):
        record(
            "environment.python",
            "environment",
            "passed",
            "Python runtime satisfies the supported baseline",
            {"actual": python_version, "required": PYTHON_REQUIREMENT},
        )
    else:
        record(
            "environment.python",
            "environment",
            "failed",
            "Python runtime does not satisfy the supported baseline",
            {"actual": python_version, "required": PYTHON_REQUIREMENT},
        )

    target_safe = False

    def check_target() -> dict[str, Any]:
        nonlocal target_safe
        _require_directory(target, mode=None, label="install target")
        # Force a read now so an unreadable target is attributed before any
        # dependent check or recovery scan attempts to traverse it.
        iterator = target.iterdir()
        try:
            next(iterator)
        except StopIteration:
            pass
        target_safe = True
        return {"path": str(target)}

    run_check("filesystem.target", "filesystem", "Install target is a safe directory", check_target)

    recovery_candidates: list[dict[str, str]] = []
    recovery_unknown = not target_safe
    if target_safe:
        recovery_error: OSError | None = None
        try:
            for child in target.iterdir():
                if child.name == LIFECYCLE_LOCK_DIRECTORY:
                    recovery_candidates.append({"kind": "lifecycle-lock", "path": child.name})
                    continue
                for prefix, kind in _RECOVERY_PREFIXES:
                    if child.name.startswith(prefix):
                        recovery_candidates.append({"kind": kind, "path": child.name})
                        break
        except OSError as error:
            recovery_error = error
            recovery_unknown = True
        recovery_candidates.sort(key=lambda item: item["path"])
        if recovery_error is not None:
            record(
                "recovery.residue",
                "recovery",
                "failed",
                "Lifecycle recovery residue could not be inspected",
                _error_details(recovery_error),
            )
        elif recovery_candidates:
            record(
                "recovery.residue",
                "recovery",
                "failed",
                "Interrupted lifecycle transaction residue requires attention",
                {"candidates": recovery_candidates},
            )
        else:
            record(
                "recovery.residue",
                "recovery",
                "passed",
                "No interrupted lifecycle transaction residue was found",
            )
    else:
        record(
            "recovery.residue",
            "recovery",
            "skipped",
            "Recovery residue check requires a safe install target",
        )

    managed = target / ".agent-skills"
    agents = target / "AGENTS.md"
    skills_root = target / "skills"

    def check_layout() -> dict[str, Any]:
        _require_directory(managed, mode=MANAGED_DIRECTORY_MODE, label="managed metadata directory")
        _require_file(agents, mode=MANAGED_FILE_MODE, label="global AGENTS.md")
        _require_directory(skills_root, mode=MANAGED_DIRECTORY_MODE, label="managed skills directory")
        entries = sorted(
            item.name for item in managed.iterdir() if not _is_ignored_os_metadata(item)
        )
        expected = {"install-lock.json", PERSISTENT_PACKAGE_LOCK, "packages"}
        if EXTERNAL_ACTIVATION_LOCK in entries:
            expected.add(EXTERNAL_ACTIVATION_LOCK)
        if ROLLBACK_POINT_DIRECTORY in entries:
            expected.add(ROLLBACK_POINT_DIRECTORY)
        if set(entries) != expected or len(entries) != len(expected):
            raise ContractError("managed metadata contains missing or unknown entries")
        return {"managed_entries": entries}

    if target_safe:
        run_check("filesystem.layout", "filesystem", "Managed root layout and modes are canonical", check_layout)
    else:
        record("filesystem.layout", "filesystem", "skipped", "Managed root layout requires a safe install target")

    install_lock: dict[str, Any] | None = None

    def check_install_lock() -> dict[str, Any]:
        nonlocal install_lock
        path = managed / "install-lock.json"
        _require_file(path, mode=MANAGED_FILE_MODE, label="Install Lock")
        value = load(path)
        validate_install_plan(value)
        if value["status"] != "installed":
            raise ContractError("Install Lock status is not installed")
        install_lock = value
        return {"fingerprint": value["fingerprint"], "lock_schema_version": value.get("lock_schema_version")}

    if target_safe:
        run_check("install.lock", "install", "Install Lock is valid and installed", check_install_lock)
    else:
        record("install.lock", "install", "skipped", "Install Lock check requires a safe install target")

    package_lock: dict[str, Any] | None = None

    def check_package_lock() -> dict[str, Any]:
        nonlocal package_lock
        path = managed / PERSISTENT_PACKAGE_LOCK
        _require_file(path, mode=MANAGED_FILE_MODE, label="persistent package Lockfile")
        value = load(path)
        validate_package_lock(value)
        if install_lock is None:
            raise ContractError("persistent package Lockfile cannot be anchored without a valid Install Lock")
        if value["fingerprint"] != install_lock.get("package_lock_hash"):
            raise ContractError("persistent package Lockfile fingerprint differs from Install Lock")
        if value["install_plan_identity_hash"] != install_plan_identity_hash(install_lock):
            raise ContractError("persistent package Lockfile Install Plan identity differs")
        package_lock = value
        return {"fingerprint": value["fingerprint"], "previous": value["lineage"]["previous_lock_hash"]}

    if target_safe and install_lock is not None:
        run_check("lock.persistent", "lock", "Persistent Lockfile is valid and anchored", check_package_lock)
    else:
        record(
            "lock.persistent",
            "lock",
            "skipped",
            "Persistent Lockfile check requires a valid Install Lock",
        )

    def check_rollback_point() -> dict[str, Any]:
        path = managed / ROLLBACK_POINT_DIRECTORY
        if not _path_exists(path):
            return {"available": False}
        point = _validate_rollback_point_directory(path)
        return {
            "available": True,
            "package_lock_hash": point["package_lock_hash"],
            "point_id": point["point_id"],
        }

    if target_safe:
        run_check(
            "recovery.rollback-point",
            "recovery",
            "Persistent rollback point is absent or valid",
            check_rollback_point,
        )
    else:
        record(
            "recovery.rollback-point",
            "recovery",
            "skipped",
            "Rollback point verification requires a safe install target",
        )

    def check_core() -> dict[str, Any]:
        if install_lock is None or package_lock is None:
            raise ContractError("Core identity requires valid Install and package Lockfiles")
        versions = {
            "runtime": CORE_VERSION,
            "install_lock": install_lock["core_version"],
            "package_lock": package_lock["core"]["runtime_version"],
        }
        if len(set(versions.values())) != 1:
            raise ContractError("Core runtime version differs from the installed Lockfiles")
        return versions

    if install_lock is not None and package_lock is not None:
        run_check("environment.core", "environment", "Core runtime identity matches both Lockfiles", check_core)
    else:
        record("environment.core", "environment", "skipped", "Core identity check requires both Lockfiles")

    def check_schemas() -> dict[str, Any]:
        if package_lock is None:
            raise ContractError("Schema identity requires a valid package Lockfile")
        current = schema_inventory(schemas)
        if current != package_lock["schema_inventory"]:
            raise ContractError("runtime Schema inventory differs from persistent package Lockfile")
        return {"content_sha256": current["content_sha256"], "file_count": len(current["files"])}

    if package_lock is not None:
        run_check("schema.inventory", "schema", "Runtime Schema inventory matches the package Lockfile", check_schemas)
    else:
        record("schema.inventory", "schema", "skipped", "Schema inventory check requires a valid package Lockfile")

    installed_semantics: dict[str, Any] | None = None

    def check_packages() -> dict[str, Any]:
        nonlocal installed_semantics
        if install_lock is None or package_lock is None:
            raise ContractError("Package verification requires both Lockfiles")
        packages_root = managed / "packages"
        _require_directory(packages_root, mode=MANAGED_DIRECTORY_MODE, label="installed package directory")
        actual_ids = sorted(
            item.name for item in packages_root.iterdir() if not _is_ignored_os_metadata(item)
        )
        expected_ids = sorted(item["id"] for item in install_lock["packages"])
        if actual_ids != expected_ids:
            raise ContractError("installed package set differs from Install Lock")
        locked_package_ids = [item["id"] for item in package_lock["packages"]]
        if locked_package_ids != [item["id"] for item in install_lock["packages"]]:
            raise ContractError("persistent Lockfile package closure or order differs from Install Lock")
        locked_packages = {item["id"]: item for item in package_lock["packages"]}
        selected = {item["id"]: item for item in install_lock["selected_packages"]}
        for record in install_lock["packages"]:
            package_id = record["id"]
            root = packages_root / package_id
            _require_directory(root, mode=record["root_mode"], label=f"package {package_id}")
            files, directories = _snapshot_tree(root, ignore_os_metadata=True)
            if not _tree_matches_record(files, directories, record, digest_field="files_sha256"):
                raise ContractError(f"installed package content differs: {package_id}")
            manifest = load(root / "manifest.json")
            validate_manifest(manifest)
            if manifest.get("id") != package_id or sha256(manifest) != record["manifest_sha256"]:
                raise ContractError(f"installed package Manifest differs: {package_id}")
            frozen = locked_packages.get(package_id)
            if frozen is None or (
                frozen["kind"] != selected[package_id]["kind"]
                or frozen["manifest_sha256"] != record["manifest_sha256"]
                or frozen["provider_manifest_sha256"] != record["provider_manifest_sha256"]
                or frozen["source"]["sha256"] != record["files_sha256"]
                or frozen["version"] != selected[package_id]["version"]
                or frozen["core_compatibility"] != selected[package_id]["core_compatibility"]
                or frozen["provider_version"] != selected[package_id]["provider_version"]
                or frozen["provider_compatibility"] != selected[package_id]["provider_compatibility"]
            ):
                raise ContractError(f"installed package identity differs from persistent Lockfile: {package_id}")
            provider_relative = manifest.get("installation", {}).get("provider_manifest")
            if record["provider_manifest_sha256"] is None:
                if provider_relative is not None:
                    raise ContractError(f"package unexpectedly declares a Provider: {package_id}")
            else:
                provider_path = _resolve_child(root, provider_relative, label="installed Provider Manifest")
                provider = load(provider_path)
                validate_manifest(provider)
                if sha256(provider) != record["provider_manifest_sha256"]:
                    raise ContractError(f"installed Provider Manifest differs: {package_id}")
        installed_semantics = derive_installed_package_semantics(
            [(item["id"], packages_root / item["id"]) for item in install_lock["packages"]],
            install_lock["packages"],
        )
        semantic_fields = {
            "core_compatibility", "kind", "provider_compatibility", "provider_version", "version"
        }
        for package_id, expected in installed_semantics["selected_package_identities"].items():
            installed_identity = {field: selected[package_id][field] for field in semantic_fields}
            persistent_identity = {field: locked_packages[package_id][field] for field in semantic_fields}
            if installed_identity != expected or persistent_identity != expected:
                raise ContractError(f"Lockfile package semantics differ from installed Manifests: {package_id}")
        if sha256(install_lock["assets"]) != package_lock["assets_sha256"]:
            raise ContractError("installed asset allowlist differs from persistent Lockfile")
        if package_lock["dependencies"] != install_lock["resolved_dependencies"]:
            raise ContractError("persistent Lockfile dependency closure differs from Install Lock")
        if installed_semantics["dependencies"] != install_lock["resolved_dependencies"]:
            raise ContractError("locked dependency closure differs from installed Manifests")
        if package_lock["selection"] != {
            "disciplines": install_lock["selected_disciplines"],
            "platforms": install_lock["selected_platforms"],
            "runtime_configs": install_lock["selected_runtime_configs"],
        }:
            raise ContractError("persistent Lockfile selection differs from Install Lock")
        if package_lock["side_effects"] != install_lock["side_effects"]:
            raise ContractError("persistent Lockfile side effects differ from Install Lock")
        if installed_semantics["side_effects"] != install_lock["side_effects"]:
            raise ContractError("locked side effects differ from installed Manifests")
        return {"package_count": len(expected_ids), "packages": expected_ids}

    if install_lock is not None and package_lock is not None:
        run_check("package.integrity", "package", "Installed packages and Manifests match both Lockfiles", check_packages)
    else:
        record("package.integrity", "package", "skipped", "Package verification requires both Lockfiles")

    def check_skills() -> dict[str, Any]:
        if install_lock is None:
            raise ContractError("Skill verification requires a valid Install Lock")
        if installed_semantics is None:
            raise ContractError("Skill verification requires rebuilt installed Manifest semantics")
        semantic_skill_fields = ("file_count", "files", "name", "package", "sha256")
        rebuilt_skills = [
            {field: item[field] for field in semantic_skill_fields}
            for item in installed_semantics["skills"]
        ]
        locked_skills = [
            {field: item[field] for field in semantic_skill_fields}
            for item in install_lock["skills"]
        ]
        if rebuilt_skills != locked_skills:
            raise ContractError("locked Skill identities differ from installed Manifests")
        external = {
            item.name
            for item in skills_root.iterdir()
            if item.name in EXTERNAL_SKILL_ROOTS and item.is_dir() and not item.is_symlink()
        }
        metadata = {item.name for item in skills_root.iterdir() if _is_ignored_os_metadata(item)}
        actual = sorted(item.name for item in skills_root.iterdir() if item.name not in external | metadata)
        expected = sorted(item["name"] for item in install_lock["skills"])
        if actual != expected:
            raise ContractError("installed Skill set differs from Install Lock")
        for record in install_lock["skills"]:
            root = skills_root / record["name"]
            _require_directory(root, mode=record["root_mode"], label=f"Skill {record['name']}")
            files, directories = _snapshot_tree(root, ignore_os_metadata=True)
            if not _tree_matches_record(files, directories, record, digest_field="sha256"):
                raise ContractError(f"installed Skill content differs: {record['name']}")
        return {"external_roots": sorted(external), "skill_count": len(expected)}

    if install_lock is not None:
        run_check("skill.integrity", "skill", "Installed Skills match the Install Lock", check_skills)
    else:
        record("skill.integrity", "skill", "skipped", "Skill verification requires a valid Install Lock")

    def check_instructions() -> dict[str, Any]:
        if install_lock is None or package_lock is None:
            raise ContractError("AGENTS verification requires both Lockfiles")
        if installed_semantics is None:
            raise ContractError("AGENTS verification requires rebuilt installed Manifest semantics")
        instructions = install_lock["instructions"]
        if instructions["path"] != "AGENTS.md":
            raise ContractError("Install Lock does not select the unique global AGENTS.md path")
        _require_file(agents, mode=MANAGED_FILE_MODE, label="global AGENTS.md")
        if _file_sha256(agents) != instructions["sha256"]:
            raise ContractError("global AGENTS.md content differs from Install Lock")
        if package_lock["instructions"]["sha256"] != instructions["sha256"]:
            raise ContractError("global AGENTS.md hash differs between Lockfiles")
        if package_lock["instructions"]["rule_trace_sha256"] != sha256(instructions["rule_trace"]):
            raise ContractError("AGENTS rule trace differs from persistent Lockfile")
        expected_instructions = installed_semantics["instructions"]
        if (
            instructions["fragments"] != expected_instructions["fragments"]
            or instructions["rule_trace"] != expected_instructions["rule_trace"]
            or instructions["sha256"] != expected_instructions["sha256"]
            or agents.read_text(encoding="utf-8") != expected_instructions["content"]
        ):
            raise ContractError("global AGENTS semantics differ from installed Manifests")
        positions = {item["id"]: index for index, item in enumerate(install_lock["packages"])}
        fragments = instructions["fragments"]
        expected_order = sorted(
            fragments,
            key=lambda item: (positions[item["package"]], item["order"], item["id"]),
        )
        if fragments != expected_order:
            raise ContractError("AGENTS instruction fragment order is not canonical")
        for fragment in fragments:
            source = _resolve_child(
                managed / "packages" / fragment["package"],
                fragment["path"],
                label="installed instruction fragment",
            )
            if source.is_symlink() or not source.is_file():
                raise ContractError(f"AGENTS instruction fragment is missing or unsafe: {fragment['id']}")
            content = source.read_text(encoding="utf-8").strip() + "\n"
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != fragment["sha256"]:
                raise ContractError(f"AGENTS instruction fragment differs: {fragment['id']}")
        return {
            "fragment_count": len(fragments),
            "path": instructions["path"],
            "sha256": instructions["sha256"],
        }

    if install_lock is not None and package_lock is not None:
        run_check(
            "instructions.global",
            "instructions",
            "Unique global AGENTS source, fragment order, rule trace and final hash are valid",
            check_instructions,
        )
    else:
        record("instructions.global", "instructions", "skipped", "AGENTS verification requires both Lockfiles")

    def check_bindings() -> dict[str, Any]:
        if install_lock is None or package_lock is None:
            raise ContractError("Binding verification requires both Lockfiles")
        if installed_semantics is None:
            raise ContractError("Binding verification requires rebuilt installed Manifest semantics")
        if package_lock["bindings_sha256"] != sha256(install_lock["bindings"]):
            raise ContractError("Capability Binding digest differs from Install Lock")
        if package_lock["capability_providers"] != install_lock["capability_providers"]:
            raise ContractError("Capability Provider closure differs between Lockfiles")
        if (
            install_lock["bindings"] != installed_semantics["bindings"]
            or install_lock["capability_providers"] != installed_semantics["capability_providers"]
        ):
            raise ContractError("Capability Binding semantics differ from installed Manifests")
        return {
            "binding_count": len(install_lock["bindings"]),
            "bindings_sha256": package_lock["bindings_sha256"],
        }

    if install_lock is not None and package_lock is not None:
        run_check("binding.freeze", "binding", "Capability Bindings and Provider closure are frozen", check_bindings)
    else:
        record("binding.freeze", "binding", "skipped", "Binding verification requires both Lockfiles")

    def check_permissions() -> dict[str, Any]:
        if install_lock is None or package_lock is None:
            raise ContractError("Permission verification requires both Lockfiles")
        if installed_semantics is None:
            raise ContractError("Permission verification requires rebuilt installed Manifest semantics")
        if package_lock["permission_profiles"] != install_lock["permission_profiles"]:
            raise ContractError("permission profile set differs between Lockfiles")
        allowed = set(install_lock["permission_profiles"])
        capability_permissions = {
            capability: provider["permission_profile"]
            for capability, provider in install_lock["capability_providers"].items()
        }
        if any(permission not in allowed for permission in capability_permissions.values()):
            raise ContractError("Capability Provider requests a permission outside the installed profile set")
        if (
            install_lock["permission_profiles"] != installed_semantics["permission_profiles"]
            or capability_permissions != {
                capability: provider["permission_profile"]
                for capability, provider in installed_semantics["capability_providers"].items()
            }
        ):
            raise ContractError("Capability permission semantics differ from installed Manifests")
        return {
            "capability_permissions": capability_permissions,
            "permission_profiles": install_lock["permission_profiles"],
        }

    if install_lock is not None and package_lock is not None:
        run_check("permission.freeze", "permission", "Permission profiles and per-Capability grants are frozen", check_permissions)
    else:
        record("permission.freeze", "permission", "skipped", "Permission verification requires both Lockfiles")

    def check_activation() -> dict[str, Any]:
        path = managed / EXTERNAL_ACTIVATION_LOCK
        if not path.exists() and not path.is_symlink():
            return {"managed": False}
        _require_file(path, mode=MANAGED_FILE_MODE, label="activation Lock")
        activation = load(path)
        validate_activation_lock(activation)
        paths: list[str] = []
        for entry in activation["files"]:
            if not isinstance(entry, dict) or set(entry) != {"mode", "path", "sha256"}:
                raise ContractError("activation Lock file entry is invalid")
            activated = _resolve_child(target, entry["path"], label="activated file")
            if (
                activated.is_symlink()
                or not activated.is_file()
                or activated.stat().st_mode & 0o777 != entry["mode"]
                or _file_sha256(activated) != entry["sha256"]
            ):
                raise ContractError(f"activated file differs: {entry['path']}")
            paths.append(entry["path"])
        if len(paths) != len(set(paths)):
            raise ContractError("activation Lock file paths must be unique")
        return {
            "deprecation": "blocked-new-use" if activation["schema_version"] == "1.0" else "current",
            "file_count": len(paths),
            "managed": True,
            "schema_version": activation["schema_version"],
        }

    if target_safe:
        run_check("activation.integrity", "activation", "Activation files are absent or match their Lock", check_activation)
    else:
        record("activation.integrity", "activation", "skipped", "Activation verification requires a safe install target")

    counts = {status: sum(check["status"] == status for check in checks) for status in ("passed", "failed", "skipped", "warning")}
    report: dict[str, Any] = {
        "checks": checks,
        "environment": {
            "core_version": CORE_VERSION,
            "python_required": PYTHON_REQUIREMENT,
            "python_version": python_version,
            "schema_root": str(schemas),
        },
        "install": {
            "install_plan_fingerprint": install_lock.get("fingerprint") if install_lock else None,
            "package_lock_hash": package_lock.get("fingerprint") if package_lock else None,
            "selected_disciplines": list(install_lock.get("selected_disciplines", [])) if install_lock else [],
            "selected_platforms": list(install_lock.get("selected_platforms", [])) if install_lock else [],
            "selected_runtime_configs": list(install_lock.get("selected_runtime_configs", [])) if install_lock else [],
        },
        "recovery": {
            "candidates": recovery_candidates,
            "status": (
                "unknown" if recovery_unknown
                else "attention" if recovery_candidates
                else "clean"
            ),
        },
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "status": "blocked" if counts["failed"] else "passed",
        "summary": counts,
        "target_root": str(target),
    }
    report["fingerprint"] = sha256(report)
    validate_doctor_report(report)
    return report
