#!/usr/bin/env python3
"""Read-only Desktop framework discovery and environment profiling."""

from __future__ import annotations

import argparse
import fnmatch
import json
import platform
from pathlib import Path, PurePosixPath
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.models import ContractError, require_version  # noqa: E402


HINTS = Path(__file__).resolve().parents[1] / "config" / "framework-hints-v1.json"
IGNORED = {".git", ".build", ".cache", ".gradle", ".venv", "DerivedData", "Pods", "build", "dist", "fixtures", "node_modules", "testdata"}
LEVEL_ORDER = {"strong": 0, "medium": 1, "weak": 2}
PROFILE_FIELDS = {
    "schema_version", "repository_root", "framework_hints_version", "status",
    "selected_framework", "module_root", "frameworks", "ambiguities", "fingerprint",
}
ENVIRONMENT_FIELDS = {
    "schema_version", "environment_id", "source", "os", "arch", "dpi", "displays",
    "inputs", "permissions", "matrix_dimensions", "fingerprint",
}


def inspect_repository(repository: str | Path, *, hints_path: str | Path = HINTS, max_depth: int = 5) -> dict[str, Any]:
    root = Path(repository).resolve()
    if not root.is_dir():
        raise ContractError("desktop repository root must be an existing directory")
    hints = load(hints_path)
    _validate_hints(hints)
    files = _files(root, max_depth=max_depth)
    frameworks: list[dict[str, Any]] = []
    for framework in hints["frameworks"]:
        evidence: list[dict[str, str]] = []
        for signal in framework["signals"]:
            for relative, path in files:
                if not _matches(relative, signal["glob"]):
                    continue
                if signal["contains_any"] and not _contains(path, signal["contains_any"]):
                    continue
                evidence.append({
                    "level": signal["level"],
                    "marker": signal["glob"],
                    "path": relative,
                    "root": _signal_root(relative, signal["root_up"]),
                })
        if not evidence:
            continue
        evidence.sort(key=lambda item: (LEVEL_ORDER[item["level"]], item["path"], item["marker"]))
        levels = {item["level"] for item in evidence}
        confidence = round(min(1.0, sum(hints["signal_weights"][level] for level in levels)), 2)
        root_level = "strong" if "strong" in levels else "medium" if "medium" in levels else "weak"
        frameworks.append({
            "adapter": framework["adapter"],
            "confidence": confidence,
            "evidence": evidence,
            "id": framework["id"],
            "module_roots": sorted({item["root"] for item in evidence if item["level"] == root_level}),
            "support": framework["support"],
            "targets": sorted(framework["targets"]),
        })
    frameworks.sort(key=lambda item: item["id"])

    strong = [item for item in frameworks if any(entry["level"] == "strong" for entry in item["evidence"])]
    status = "unsupported"
    selected: str | None = None
    module_root: str | None = None
    ambiguities: list[str] = []
    if frameworks and not strong:
        status = "ambiguous"
        ambiguities.append("no-strong-framework-signal")
    elif len(strong) > 1:
        status = "ambiguous"
        ambiguities.append("multiple-strong-frameworks:" + ",".join(item["id"] for item in strong))
    elif len(strong) == 1 and len(strong[0]["module_roots"]) != 1:
        status = "ambiguous"
        ambiguities.append("multiple-module-roots:" + ",".join(strong[0]["module_roots"]))
    elif len(strong) == 1:
        status = "supported"
        selected = strong[0]["id"]
        module_root = strong[0]["module_roots"][0]
    profile = {
        "ambiguities": sorted(ambiguities),
        "framework_hints_version": hints["schema_version"],
        "frameworks": frameworks,
        "module_root": module_root,
        "repository_root": str(root),
        "schema_version": "1.0",
        "selected_framework": selected,
        "status": status,
    }
    profile["fingerprint"] = _fingerprint(profile)
    validate_project_profile(profile)
    return profile


def build_environment_profile(facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if facts is None:
        source = "host"
        os_family = {"darwin": "macos", "win32": "windows", "linux": "linux"}.get(sys.platform, "unknown")
        normalized = {
            "arch": platform.machine() or "unknown",
            "displays": [],
            "dpi": {"scale_factor": None, "source": "unknown"},
            "inputs": [{"kind": "keyboard", "status": "unknown"}, {"kind": "mouse", "status": "unknown"}],
            "os": {"family": os_family, "version": platform.release() or None},
            "permissions": [
                {"name": name, "status": "unknown"}
                for name in ("automation", "filesystem", "installer-elevation", "network", "notification")
            ],
        }
    else:
        source = "fixture"
        normalized = _normalize_environment_facts(facts)
    matrix = {
        "display_scales": sorted({
            value for value in [normalized["dpi"]["scale_factor"], *[item["scale_factor"] for item in normalized["displays"]]]
            if value is not None
        }),
        "input_kinds": sorted({item["kind"] for item in normalized["inputs"]}),
        "permission_names": sorted({item["name"] for item in normalized["permissions"]}),
    }
    identity = {"source": source, **normalized, "matrix_dimensions": matrix}
    profile = {
        "arch": normalized["arch"],
        "displays": normalized["displays"],
        "dpi": normalized["dpi"],
        "environment_id": "desktop-env-" + sha256(identity)[:12],
        "inputs": normalized["inputs"],
        "matrix_dimensions": matrix,
        "os": normalized["os"],
        "permissions": normalized["permissions"],
        "schema_version": "1.0",
        "source": source,
    }
    profile["fingerprint"] = _fingerprint(profile)
    validate_environment_profile(profile)
    return profile


def validate_project_profile(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS:
        raise ContractError("desktop-project-profile fields are invalid")
    require_version(value)
    if value["status"] not in {"supported", "ambiguous", "unsupported"}:
        raise ContractError("desktop-project-profile status is invalid")
    if not isinstance(value["repository_root"], str) or not Path(value["repository_root"]).is_absolute():
        raise ContractError("desktop-project-profile repository_root must be absolute")
    if not isinstance(value["frameworks"], list) or not isinstance(value["ambiguities"], list):
        raise ContractError("desktop-project-profile collections are invalid")
    ids: list[str] = []
    for framework in value["frameworks"]:
        if not isinstance(framework, dict) or set(framework) != {"id", "adapter", "confidence", "evidence", "module_roots", "targets", "support"}:
            raise ContractError("desktop-project-profile framework fields are invalid")
        ids.append(framework["id"])
        if framework["module_roots"] != sorted(set(framework["module_roots"])):
            raise ContractError("desktop-project-profile module roots must be sorted and unique")
        for root in framework["module_roots"]:
            _require_relative(root, "desktop-project-profile module root")
        for evidence in framework["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != {"level", "marker", "path", "root"}:
                raise ContractError("desktop-project-profile evidence fields are invalid")
            _require_relative(evidence["path"], "desktop-project-profile evidence path")
            _require_relative(evidence["root"], "desktop-project-profile evidence root")
    if ids != sorted(set(ids)) or value["ambiguities"] != sorted(set(value["ambiguities"])):
        raise ContractError("desktop-project-profile ids and ambiguities must be sorted and unique")
    if value["status"] == "supported":
        matches = [item for item in value["frameworks"] if item["id"] == value["selected_framework"]]
        if len(matches) != 1 or value["module_root"] not in matches[0]["module_roots"] or value["ambiguities"]:
            raise ContractError("supported desktop-project-profile selection is inconsistent")
    elif value["selected_framework"] is not None or value["module_root"] is not None:
        raise ContractError("unresolved desktop-project-profile cannot select a framework")
    _validate_fingerprint(value, PROFILE_FIELDS, "desktop-project-profile")


def validate_environment_profile(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != ENVIRONMENT_FIELDS:
        raise ContractError("desktop-environment-profile fields are invalid")
    require_version(value)
    if value["source"] not in {"host", "fixture"} or not isinstance(value["arch"], str) or not value["arch"]:
        raise ContractError("desktop-environment-profile source or arch is invalid")
    normalized = _normalize_environment_facts({key: value[key] for key in ("os", "arch", "dpi", "displays", "inputs", "permissions")})
    expected_matrix = {
        "display_scales": sorted({
            scale for scale in [normalized["dpi"]["scale_factor"], *[item["scale_factor"] for item in normalized["displays"]]]
            if scale is not None
        }),
        "input_kinds": sorted({item["kind"] for item in normalized["inputs"]}),
        "permission_names": sorted({item["name"] for item in normalized["permissions"]}),
    }
    if value["matrix_dimensions"] != expected_matrix:
        raise ContractError("desktop-environment-profile matrix dimensions are inconsistent")
    _validate_fingerprint(value, ENVIRONMENT_FIELDS, "desktop-environment-profile")


def _normalize_environment_facts(value: Any) -> dict[str, Any]:
    fields = {"os", "arch", "dpi", "displays", "inputs", "permissions"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError("desktop environment facts fields are invalid")
    os_value = value["os"]
    if not isinstance(os_value, dict) or set(os_value) != {"family", "version"} or os_value["family"] not in {"macos", "windows", "linux", "unknown"}:
        raise ContractError("desktop environment OS is invalid")
    if os_value["version"] is not None and (not isinstance(os_value["version"], str) or not os_value["version"]):
        raise ContractError("desktop environment OS version is invalid")
    if not isinstance(value["arch"], str) or not value["arch"]:
        raise ContractError("desktop environment arch is invalid")
    dpi = value["dpi"]
    if not isinstance(dpi, dict) or set(dpi) != {"scale_factor", "source"} or dpi["source"] not in {"observed", "fixture", "unknown"}:
        raise ContractError("desktop environment DPI is invalid")
    _scale(dpi["scale_factor"], "desktop environment DPI scale")
    displays = _object_list(value["displays"], {"id", "resolution", "scale_factor", "hdr"}, "desktop environment display")
    display_ids: list[str] = []
    for display in displays:
        if not isinstance(display["id"], str) or not display["id"] or display["hdr"] not in {"supported", "unsupported", "unknown"}:
            raise ContractError("desktop environment display is invalid")
        if display["resolution"] is not None and (not isinstance(display["resolution"], str) or not display["resolution"]):
            raise ContractError("desktop environment display resolution is invalid")
        _scale(display["scale_factor"], "desktop environment display scale")
        display_ids.append(display["id"])
    inputs = _object_list(value["inputs"], {"kind", "status"}, "desktop environment input")
    for item in inputs:
        if item["kind"] not in {"keyboard", "mouse", "touch", "pen", "gamepad", "unknown"} or item["status"] not in {"available", "unavailable", "unknown"}:
            raise ContractError("desktop environment input is invalid")
    permissions = _object_list(value["permissions"], {"name", "status"}, "desktop environment permission")
    required_permissions = {"filesystem", "network", "notification", "automation", "installer-elevation"}
    if {item["name"] for item in permissions} != required_permissions:
        raise ContractError("desktop environment permissions must cover the fixed permission set")
    if any(item["status"] not in {"granted", "denied", "not-required", "unknown"} for item in permissions):
        raise ContractError("desktop environment permission status is invalid")
    if display_ids != sorted(set(display_ids)):
        raise ContractError("desktop environment display ids must be sorted and unique")
    if inputs != sorted(inputs, key=lambda item: item["kind"]) or permissions != sorted(permissions, key=lambda item: item["name"]):
        raise ContractError("desktop environment inputs and permissions must be sorted")
    return {"arch": value["arch"], "displays": displays, "dpi": dpi, "inputs": inputs, "os": os_value, "permissions": permissions}


def _validate_hints(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {"schema_version", "signal_weights", "frameworks"}:
        raise ContractError("desktop framework hints fields are invalid")
    require_version(value)
    if value["signal_weights"] != {"strong": 0.65, "medium": 0.25, "weak": 0.1}:
        raise ContractError("desktop framework signal weights are invalid")
    ids: list[str] = []
    for framework in value["frameworks"]:
        if not isinstance(framework, dict) or set(framework) != {"id", "adapter", "targets", "signals", "support"}:
            raise ContractError("desktop framework hint is invalid")
        ids.append(framework["id"])
        if not isinstance(framework["signals"], list) or not framework["signals"]:
            raise ContractError("desktop framework signals must be non-empty")
        for signal in framework["signals"]:
            if not isinstance(signal, dict) or set(signal) != {"level", "glob", "contains_any", "root_up"}:
                raise ContractError("desktop framework signal fields are invalid")
            if signal["level"] not in LEVEL_ORDER or not isinstance(signal["glob"], str) or not signal["glob"] or not isinstance(signal["root_up"], int) or signal["root_up"] < 1:
                raise ContractError("desktop framework signal is invalid")
            if not isinstance(signal["contains_any"], list) or any(not isinstance(item, str) or not item for item in signal["contains_any"]):
                raise ContractError("desktop framework signal content markers are invalid")
    if len(ids) != len(set(ids)):
        raise ContractError("desktop framework hint ids must be unique")


def _files(root: Path, *, max_depth: int) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if len(relative_path.parts) > max_depth or any(part in IGNORED for part in relative_path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found.append((relative_path.as_posix(), path))
    return sorted(found)


def _matches(relative: str, pattern: str) -> bool:
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(Path(relative).name, pattern) or Path(relative).match(pattern)


def _contains(path: Path, markers: list[str]) -> bool:
    if path.stat().st_size > 1_000_000:
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return any(marker.lower() in content for marker in markers)


def _signal_root(relative: str, root_up: int) -> str:
    root = Path(relative)
    for _ in range(root_up):
        root = root.parent
    return root.as_posix() or "."


def _fingerprint(value: dict[str, Any]) -> str:
    return "desktop-v1:" + sha256(value)


def _validate_fingerprint(value: dict[str, Any], fields: set[str], label: str) -> None:
    expected = _fingerprint({key: value[key] for key in fields - {"fingerprint"}})
    if value["fingerprint"] != expected:
        raise ContractError(f"{label} fingerprint does not match artifact body")


def _require_relative(value: Any, label: str) -> None:
    if value == ".":
        return
    path = PurePosixPath(value) if isinstance(value, str) else None
    if path is None or not value or path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError(f"{label} is unsafe")


def _object_list(value: Any, fields: set[str], label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) or set(item) != fields for item in value):
        raise ContractError(f"{label} entries are invalid")
    return list(value)


def _scale(value: Any, label: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
        raise ContractError(f"{label} is invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--repository", type=Path, required=True)
    inspect_parser.add_argument("--hints", type=Path, default=HINTS)
    environment_parser = subparsers.add_parser("environment")
    environment_parser.add_argument("--facts", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "inspect":
        result = inspect_repository(arguments.repository, hints_path=arguments.hints)
    else:
        facts = load(arguments.facts) if arguments.facts else None
        result = build_environment_profile(facts)
    sys.stdout.write(dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        raise SystemExit(2)
