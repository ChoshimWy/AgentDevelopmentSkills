#!/usr/bin/env python3
"""Build portable Desktop routing, environment and CP1 anchor goldens."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent_workflow.adapters import build_adapter_request  # noqa: E402
from agent_workflow.canonical_json import dump, load, sha256  # noqa: E402
from agent_workflow.discovery import DiscoveryEngine  # noqa: E402
from agent_workflow.planning import PlanCompiler  # noqa: E402
from agent_workflow.policy import PolicyResolver  # noqa: E402
from agent_workflow.qa import evidence_reuse_status  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402
from platforms.desktop.scripts import desktop_adapter  # noqa: E402
from platforms.desktop.scripts.desktop_discovery import (  # noqa: E402
    build_environment_profile,
    inspect_repository,
)


FIXTURES = ROOT / "tests" / "fixtures"
MANIFESTS = ROOT / "platforms"
ROUTES = ("desktop-native", "desktop-electron", "desktop-tauri", "desktop-ambiguous")


def build(output_root: Path) -> dict:
    output_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    for fixture in ROUTES:
        profile = inspect_repository(FIXTURES / fixture)
        artifact = _portable_route(fixture, profile)
        _write(output_root, f"routes/{fixture}.json", artifact, entries)
    facts = load(FIXTURES / "desktop-environment.json")
    environment = build_environment_profile(facts)
    _write(output_root, "environment.json", environment, entries)
    _write(output_root, "cp1-anchor.json", _cp1_anchor(environment), entries)
    index = {"entries": entries, "schema_version": "1.0"}
    dump(index, output_root / "golden-index.json")
    return index


def _portable_route(fixture: str, profile: dict) -> dict:
    artifact = {
        "ambiguities": profile["ambiguities"],
        "fixture": fixture,
        "frameworks": [{
            "confidence": framework["confidence"],
            "evidence": [{key: evidence[key] for key in ("level", "marker", "path", "root")} for evidence in framework["evidence"]],
            "id": framework["id"],
            "module_roots": framework["module_roots"],
            "support": framework["support"],
            "targets": framework["targets"],
        } for framework in profile["frameworks"]],
        "module_root": profile["module_root"],
        "schema_version": "1.0",
        "selected_framework": profile["selected_framework"],
        "status": profile["status"],
    }
    return {**artifact, "fingerprint": "desktop-golden-v1:" + sha256(artifact)}


def _cp1_anchor(environment: dict) -> dict:
    registry = ManifestRegistry.from_directory(MANIFESTS)
    discovered = DiscoveryEngine(registry).discover(FIXTURES / "desktop-tauri")
    policy = PolicyResolver().resolve(discovered, "执行 Desktop Bug 回归测试", explicit_platforms=["desktop"])
    plan = PlanCompiler(registry).compile(discovered, policy)
    qa = load(ROOT / "tests" / "golden" / "phase4-qa" / "bug" / "passed.json")
    changed_facts = deepcopy(load(FIXTURES / "desktop-environment.json"))
    changed_facts["dpi"]["scale_factor"] = 1
    changed_environment = build_environment_profile(changed_facts)
    first_result = qa["test_results"][0]
    source_fingerprints = [qa["defect_reports"][0]["fingerprint"]]
    stale = evidence_reuse_status(
        first_result,
        plan_fingerprint=qa["plan"]["fingerprint"],
        environment_fingerprint=changed_environment["fingerprint"],
        test_data_fingerprint=first_result["test_data_fingerprint"],
        recorded_source_fingerprints=source_fingerprints,
        current_source_fingerprints=source_fingerprints,
    )
    environment_linked = (
        {result["environment_fingerprint"] for result in qa["test_results"]} == {environment["fingerprint"]}
        and qa["defect_reports"][0]["environment_fingerprint"] == environment["fingerprint"]
        and qa["regression_set"]["environment_fingerprints"] == [environment["fingerprint"]]
    )

    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory) / "electron"
        shutil.copytree(FIXTURES / "desktop-electron", repository)
        package = json.loads((repository / "package.json").read_text(encoding="utf-8"))
        package["scripts"] = {"test": "controlled-conformance-runner"}
        (repository / "package.json").write_text(json.dumps(package), encoding="utf-8")
        profile = inspect_repository(repository)
        request = _adapter_request(profile, environment)
        original_which = desktop_adapter.shutil.which
        desktop_adapter.shutil.which = lambda _: "/usr/bin/npm"
        try:
            result = desktop_adapter.run_adapter(
                request,
                "affected-tests",
                execute=True,
                runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1 controlled test passed", stderr=""),
            )
        finally:
            desktop_adapter.shutil.which = original_which

    artifact = {
        "adapter": {
            "capability": result["capability"],
            "cleanup": [{"resource_kind": item["resource"].split(":", 1)[0], "status": item["status"]} for item in result["cleanup"]],
            "evidence_kinds": [item["kind"] for item in result["evidence"]],
            "execution_kind": "controlled-conformance-runner",
            "status": result["status"],
        },
        "desktop": _portable_route("desktop-electron", profile),
        "environment_fingerprint": environment["fingerprint"],
        "plan": {
            "edges": plan["edges"],
            "nodes": [{"capability": node["capability"], "id": node["id"], "mandatory": node["mandatory"]} for node in plan["nodes"]],
            "status": plan["status"],
        },
        "qa": {
            "defect_environment_fingerprint": qa["defect_reports"][0]["environment_fingerprint"],
            "defect_status": qa["defect_reports"][0]["status"],
            "environment_linked": environment_linked,
            "environment_stale_reasons": stale["reasons"],
            "environment_stale_status": stale["status"],
            "plan_fingerprint": qa["plan"]["fingerprint"],
            "regression_environment_fingerprints": qa["regression_set"]["environment_fingerprints"],
            "regression_status": qa["regression_set"]["status"],
            "report_fingerprint": qa["report"]["fingerprint"],
            "result_environment_fingerprints": sorted({result["environment_fingerprint"] for result in qa["test_results"]}),
            "status": qa["status"],
        },
        "schema_version": "1.0",
        "status": "passed" if plan["status"] == "ready" and result["status"] == "completed" and qa["status"] == "passed" and environment_linked and stale["status"] == "stale" else "blocked",
    }
    return {**artifact, "fingerprint": "phase4-cp1-v1:" + sha256(artifact)}


def _adapter_request(profile: dict, environment: dict) -> dict:
    plan = {
        "fingerprint": "phase4-cp1-adapter-plan",
        "nodes": [{
            "binding": {"kind": "script", "mode": "affected-tests", "name": "scripts/desktop_adapter.py"},
            "capability": "verification.desktop.affected-tests",
            "id": "desktop-verify",
            "provider": "desktop-agent-skills",
        }],
        "plan_id": "phase4-cp1-adapter-plan",
        "schema_version": "1.0",
    }
    return build_adapter_request(
        plan,
        "desktop-verify",
        context={
            "checkpoints": {"CP1": "anchor"},
            "desktop_environment_profile": environment,
            "desktop_project_profile": profile,
            "environment_fingerprint": environment["fingerprint"],
        },
        invocation_id="phase4-cp1-adapter-invocation",
    )


def _write(root: Path, relative: str, artifact: dict, entries: list[dict[str, str]]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    dump(artifact, path)
    entries.append({
        "artifact_fingerprint": artifact["fingerprint"],
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })


def _tree_differences(expected: Path, actual: Path) -> list[str]:
    expected_files = {path.relative_to(expected).as_posix(): path for path in expected.rglob("*") if path.is_file()}
    actual_files = {path.relative_to(actual).as_posix(): path for path in actual.rglob("*") if path.is_file()}
    differences = sorted(set(expected_files) ^ set(actual_files))
    differences.extend(
        name for name in sorted(set(expected_files) & set(actual_files))
        if expected_files[name].read_bytes() != actual_files[name].read_bytes()
    )
    return differences


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "tests" / "golden" / "phase4-desktop")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.check:
        if not arguments.output.is_dir():
            print(f"FAIL missing Phase 4 Desktop golden directory: {arguments.output}")
            return 1
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "phase4-desktop"
            build(generated)
            differences = _tree_differences(arguments.output, generated)
        if differences:
            print("FAIL Phase 4 Desktop goldens differ: " + ", ".join(differences))
            return 1
        print("PASS verified Phase 4 Desktop routing and CP1 goldens")
        return 0
    index = build(arguments.output)
    print(f"PASS built {len(index['entries'])} Phase 4 Desktop goldens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
