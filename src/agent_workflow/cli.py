"""Command-line interface for the Phase 1 workflow core."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .activation import (
    ACTIVATION_HANDLER_ID,
    DEACTIVATION_HANDLER_ID,
    PRESERVE_HANDLER_ID,
    activation_handler_sha256,
    external_paths as activation_external_paths,
    deactivation_external_paths,
)
from .adapters import (
    build_adapter_request,
    claim_provider_invocation,
    collect_submitted_results,
    inspect_provider_invocation,
    load_claim_token_file,
    prepare_provider_invocation,
    submit_provider_invocation,
    validate_adapter_result,
    validate_provider_invocation_plan,
)
from .canonical_json import dump, dumps, load
from .contracts import validate
from .discovery import DiscoveryEngine
from .doctor import diagnose_install
from .installation import build_install_bundle, install_bundle
from .models import ContractError
from .package_lock import (
    diff_package_locks,
    explain_package_lock,
    resolve_package_lock,
    validate_package_lock,
    validate_plan_package_lock,
)
from .planning import PlanCompiler
from .policy import PolicyResolver
from .registry import ManifestRegistry
from .reporting import delivery_report
from .runtime import FakeAdapterExecutor, RecordedAdapterExecutor
from .upgrade import (
    apply_upgrade,
    plan_upgrade,
    prepare_upgrade_candidate,
    rollback_upgrade,
    run_upgrade_conformance,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _source_upgrade_context(
    manifests: str | Path,
    target_root: str | Path,
    selection: dict[str, object],
):
    """Build the trusted Core activation rollback scope and handler identity."""

    target = Path(target_root).expanduser().resolve()
    activation_lock_path = target / ".agent-skills" / "activation-lock.json"
    if not activation_lock_path.exists():
        return (), "none", None
    if activation_lock_path.is_symlink() or not activation_lock_path.is_file():
        raise ContractError("source activation lock is missing or unsafe")
    if "apple" in selection["platforms"] and "codex" in selection["runtime_configs"]:
        return activation_external_paths(target), ACTIVATION_HANDLER_ID, activation_handler_sha256()
    return deactivation_external_paths(target), DEACTIVATION_HANDLER_ID, activation_handler_sha256()


def _partial_uninstall_external_context(
    target_root: str | Path,
    removed_platforms: list[str],
):
    """Return the only external lifecycle action allowed for a partial uninstall.

    Activation owned by a remaining Apple selection is deliberately left untouched.
    Only removal of an activated Apple package may invoke the deactivation handler.
    """

    target = Path(target_root).expanduser().resolve()
    activation_lock_path = target / ".agent-skills" / "activation-lock.json"
    if not activation_lock_path.exists():
        return (), "none", None
    if activation_lock_path.is_symlink() or not activation_lock_path.is_file():
        raise ContractError("source activation lock is missing or unsafe")
    if "apple" not in removed_platforms:
        return deactivation_external_paths(target), PRESERVE_HANDLER_ID, activation_handler_sha256()
    return deactivation_external_paths(target), DEACTIVATION_HANDLER_ID, activation_handler_sha256()


def _partial_uninstall_selection(target_root: str | Path, requested: list[str]) -> dict[str, object]:
    target = Path(target_root).expanduser().resolve()
    lock = load(target / ".agent-skills" / "install-lock.json")
    validate("install-plan", lock)
    installed = list(lock["selected_platforms"])
    if requested == ["all"]:
        removed = set(installed)
    else:
        if "all" in requested or len(requested) != len(set(requested)):
            raise ContractError("partial uninstall platforms must be unique; all cannot be combined")
        unknown = sorted(set(requested) - set(installed))
        if unknown:
            raise ContractError("platform is not installed: " + ", ".join(unknown))
        removed = set(requested)
    if not removed:
        raise ContractError("partial uninstall requires at least one installed platform")
    remaining = sorted(set(installed) - removed)
    runtime_configs = list(lock["selected_runtime_configs"])
    if "apple" in removed and (target / ".agent-skills" / "activation-lock.json").is_file():
        runtime_configs = [item for item in runtime_configs if item != "codex"]
    disciplines = list(lock["selected_disciplines"])
    remaining_runtime_configs = runtime_configs
    return {
        "core_only": not remaining and not disciplines and not runtime_configs,
        "disciplines": disciplines,
        "platforms": remaining,
        "removed_platforms": sorted(removed),
        "removed_runtime_configs": sorted(
            set(lock["selected_runtime_configs"]) - set(remaining_runtime_configs)
        ),
        "runtime_configs": remaining_runtime_configs,
    }


def default_manifest_directory() -> Path:
    source_checkout = PROJECT_ROOT / "platforms"
    if source_checkout.is_dir():
        return source_checkout
    installed = Path(sys.prefix) / "share" / "agent-workflow" / "platforms"
    return installed


def default_schema_directory() -> Path:
    source_checkout = PROJECT_ROOT / "schemas"
    if source_checkout.is_dir():
        return source_checkout
    installed = Path(sys.prefix) / "share" / "agent-workflow" / "schemas"
    return installed


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    invoked_name = Path(sys.argv[0]).name
    default_prog = invoked_name if invoked_name in {"agent-skills", "agent-workflow"} else "agent-workflow"
    parser = argparse.ArgumentParser(prog=prog or default_prog)
    parser.add_argument("--manifests", default=str(default_manifest_directory()))
    parser.add_argument("--provider-manifests", action="append", default=[])
    parser.add_argument("--disable-provider", action="append", default=[])
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect")
    detect.add_argument("repository")
    detect.add_argument("--target-file", action="append", default=[])
    detect.add_argument("--changed-file", action="append", default=[])

    route = subparsers.add_parser("route")
    route.add_argument("repository")
    route.add_argument("--task", required=True)
    route.add_argument("--platform", action="append", default=[])
    route.add_argument("--target-file", action="append", default=[])
    route.add_argument("--changed-file", action="append", default=[])
    route.add_argument("--explain", action="store_true")

    plan = subparsers.add_parser("plan")
    plan.add_argument("repository")
    plan.add_argument("--task", required=True)
    plan.add_argument("--platform", action="append", default=[])
    plan.add_argument("--target-file", action="append", default=[])
    plan.add_argument("--changed-file", action="append", default=[])
    plan.add_argument("--dry-run", action="store_true")
    plan.add_argument("--lock", help="validated agent-skills.lock used to freeze the plan")

    lock = subparsers.add_parser("lock")
    lock_commands = lock.add_subparsers(dest="lock_command", required=True)
    lock_resolve = lock_commands.add_parser("resolve")
    lock_resolve.add_argument("install_plan")
    lock_resolve.add_argument("--schemas", default=str(default_schema_directory()))
    lock_resolve.add_argument("--previous")
    lock_resolve.add_argument("--source", action="append", default=[])
    lock_resolve.add_argument("--source-base", default=".")
    lock_resolve.add_argument("--source-sha256", action="append", default=[])
    lock_resolve.add_argument("--output")
    lock_validate = lock_commands.add_parser("validate")
    lock_validate.add_argument("lockfile")
    lock_diff = lock_commands.add_parser("diff")
    lock_diff.add_argument("before")
    lock_diff.add_argument("after")
    lock_explain = lock_commands.add_parser("explain")
    lock_explain.add_argument("lockfile")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("kind")
    validate_parser.add_argument("artifact")

    prepare_adapter = subparsers.add_parser("prepare-adapter")
    prepare_adapter.add_argument("plan")
    prepare_adapter.add_argument("node_id")
    prepare_adapter.add_argument("--context", required=True)
    prepare_adapter.add_argument("--invocation-id", required=True)
    prepare_adapter.add_argument("--lock")

    validate_adapter = subparsers.add_parser("validate-adapter-result")
    validate_adapter.add_argument("request")
    validate_adapter.add_argument("result")

    invocation = subparsers.add_parser("invocation")
    invocation_commands = invocation.add_subparsers(dest="invocation_command", required=True)
    invocation_prepare = invocation_commands.add_parser("prepare")
    invocation_prepare.add_argument("root")
    invocation_prepare.add_argument("plan")
    invocation_prepare.add_argument("node_id")
    invocation_prepare.add_argument("--context", required=True)
    invocation_prepare.add_argument("--invocation-id", required=True)
    invocation_prepare.add_argument("--lock")
    invocation_claim = invocation_commands.add_parser("claim")
    invocation_claim.add_argument("root")
    invocation_claim.add_argument("request_id")
    invocation_claim.add_argument("--actor-id", required=True)
    invocation_claim.add_argument("--claim-token-file", required=True)
    invocation_submit = invocation_commands.add_parser("submit")
    invocation_submit.add_argument("root")
    invocation_submit.add_argument("request_id")
    invocation_submit.add_argument("result")
    invocation_submit.add_argument("--claim-token-file", required=True)
    invocation_inspect = invocation_commands.add_parser("inspect")
    invocation_inspect.add_argument("root")
    invocation_inspect.add_argument("request_id")

    install = subparsers.add_parser("install")
    install.add_argument("--core-only", action="store_true")
    install.add_argument("--platform", action="append", default=[])
    install.add_argument("--discipline", action="append", default=[])
    install.add_argument("--runtime-config", action="append", default=[])
    install.add_argument("--target-root", default=str(Path.home() / ".codex"))
    install.add_argument("--dry-run", action="store_true")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--target-root", default=str(Path.home() / ".codex"))
    doctor.add_argument("--schemas", default=str(default_schema_directory()))

    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("--target-root", default=str(Path.home() / ".codex"))
    upgrade.add_argument("--schemas", default=str(default_schema_directory()))
    upgrade.add_argument("--platform", action="append", default=None)
    upgrade.add_argument("--discipline", action="append", default=None)
    upgrade.add_argument("--runtime-config", action="append", default=None)
    upgrade.add_argument("--core-only", action="store_true", default=None)
    upgrade.add_argument("--dry-run", action="store_true")
    upgrade.add_argument("--output")
    upgrade.add_argument("--evidence-output")
    upgrade.add_argument("--plan")
    upgrade.add_argument("--approve-plan")
    upgrade.add_argument("--approve", action="append", default=[])

    uninstall = subparsers.add_parser("uninstall")
    uninstall.add_argument("--target-root", default=str(Path.home() / ".codex"))
    uninstall.add_argument("--schemas", default=str(default_schema_directory()))
    uninstall.add_argument("--platform", action="append", required=True)
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--output")
    uninstall.add_argument("--evidence-output")
    uninstall.add_argument("--plan")
    uninstall.add_argument("--approve-plan")
    uninstall.add_argument("--approve", action="append", default=[])

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--target-root", default=str(Path.home() / ".codex"))
    rollback.add_argument("--approve-current-lock", required=True)
    rollback.add_argument("--approve-rollback-point", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("plan")
    run.add_argument("--ledger", default="run-ledger.jsonl")
    run_mode = run.add_mutually_exclusive_group(required=True)
    run_mode.add_argument("--fake-adapters", action="store_true")
    run_mode.add_argument("--adapter-results")
    run_mode.add_argument("--invocation-root")
    run.add_argument("--adapter-context")
    run.add_argument("--invocation-selection")
    run.add_argument("--lock")

    resume = subparsers.add_parser("resume")
    resume.add_argument("plan")
    resume.add_argument("--ledger", required=True)
    resume_mode = resume.add_mutually_exclusive_group(required=True)
    resume_mode.add_argument("--fake-adapters", action="store_true")
    resume_mode.add_argument("--adapter-results")
    resume_mode.add_argument("--invocation-root")
    resume.add_argument("--adapter-context")
    resume.add_argument("--invocation-selection")
    resume.add_argument("--lock")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lock":
            if args.lock_command == "resolve":
                sources = _parse_lock_sources(args.source)
                artifact_hashes = _parse_package_hashes(args.source_sha256)
                package_lock = resolve_package_lock(
                    load(args.install_plan),
                    schema_root=args.schemas,
                    package_sources=sources,
                    package_source_artifact_hashes=artifact_hashes,
                    source_base=args.source_base,
                    previous_lock=load(args.previous) if args.previous else None,
                )
                if args.output:
                    dump(package_lock, args.output)
                print(dumps(package_lock), end="")
                return 0
            if args.lock_command == "validate":
                package_lock = load(args.lockfile)
                validate_package_lock(package_lock)
                print(dumps({"lock_hash": package_lock["fingerprint"], "status": "passed"}), end="")
                return 0
            if args.lock_command == "diff":
                print(dumps(diff_package_locks(load(args.before), load(args.after))), end="")
                return 0
            if args.lock_command == "explain":
                print(dumps(explain_package_lock(load(args.lockfile))), end="")
                return 0
        if args.command == "install":
            bundle = build_install_bundle(
                args.manifests,
                platforms=args.platform,
                disciplines=args.discipline,
                runtime_configs=args.runtime_config,
                core_only=args.core_only,
                schema_root=Path(args.manifests).resolve().parent / "schemas",
            )
            print(dumps(install_bundle(bundle, args.target_root, dry_run=args.dry_run)), end="")
            return 0
        if args.command == "doctor":
            report = diagnose_install(args.target_root, schema_root=args.schemas)
            print(dumps(report), end="")
            return 0 if report["status"] == "passed" else 2
        if args.command == "upgrade":
            saved_plan = load(args.plan) if args.plan else None
            selection = saved_plan.get("selection", {}) if saved_plan else {}
            candidate = prepare_upgrade_candidate(
                args.manifests,
                args.target_root,
                platforms=(selection.get("platforms") if saved_plan else args.platform),
                disciplines=(selection.get("disciplines") if saved_plan else args.discipline),
                runtime_configs=(selection.get("runtime_configs") if saved_plan else args.runtime_config),
                core_only=(selection.get("core_only") if saved_plan else args.core_only),
            )
            evidence = run_upgrade_conformance(args.manifests, candidate.bundle.package_lock)
            if args.evidence_output:
                dump(evidence, args.evidence_output)
            external_paths, external_handler, external_handler_sha256 = _source_upgrade_context(
                args.manifests,
                args.target_root,
                candidate.selection,
            )
            operation = plan_upgrade(
                args.manifests,
                args.target_root,
                evidence,
                schema_root=args.schemas,
                platforms=(selection.get("platforms") if saved_plan else args.platform),
                disciplines=(selection.get("disciplines") if saved_plan else args.discipline),
                runtime_configs=(selection.get("runtime_configs") if saved_plan else args.runtime_config),
                core_only=(selection.get("core_only") if saved_plan else args.core_only),
                external_paths=external_paths,
                external_handler=external_handler,
                external_handler_sha256=external_handler_sha256,
            )
            if saved_plan is not None and saved_plan != operation.plan:
                raise ContractError("saved upgrade plan is stale or differs from the current candidate")
            if args.dry_run:
                if args.approve_plan or args.approve:
                    raise ContractError("upgrade --dry-run does not accept approvals")
                if args.output:
                    dump(operation.plan, args.output)
                print(dumps(operation.plan), end="")
                return 0
            if saved_plan is None or args.approve_plan is None:
                raise ContractError("upgrade apply requires --plan and --approve-plan")
            result = apply_upgrade(
                operation,
                args.target_root,
                approve_plan=args.approve_plan,
                approvals=args.approve,
            )
            print(dumps(result), end="")
            return 0
        if args.command == "uninstall":
            saved_plan = load(args.plan) if args.plan else None
            request = _partial_uninstall_selection(args.target_root, args.platform)
            selection = (
                saved_plan.get("selection", {})
                if saved_plan is not None
                else request
            )
            if saved_plan is not None and saved_plan.get("action") != "partial-uninstall":
                raise ContractError("saved plan is not a partial-uninstall plan")
            if saved_plan is not None and (
                saved_plan.get("removed_platforms") != request["removed_platforms"]
                or saved_plan.get("removed_runtime_configs") != request["removed_runtime_configs"]
            ):
                raise ContractError("apply platform request differs from the saved partial-uninstall plan")
            candidate = prepare_upgrade_candidate(
                args.manifests,
                args.target_root,
                platforms=selection["platforms"],
                disciplines=selection["disciplines"],
                runtime_configs=selection["runtime_configs"],
                core_only=selection["core_only"],
            )
            evidence = run_upgrade_conformance(args.manifests, candidate.bundle.package_lock)
            if args.evidence_output:
                dump(evidence, args.evidence_output)
            external_paths, external_handler, external_handler_sha256 = _partial_uninstall_external_context(
                args.target_root,
                request["removed_platforms"],
            )
            operation = plan_upgrade(
                args.manifests,
                args.target_root,
                evidence,
                schema_root=args.schemas,
                platforms=selection["platforms"],
                disciplines=selection["disciplines"],
                runtime_configs=selection["runtime_configs"],
                core_only=selection["core_only"],
                external_paths=external_paths,
                external_handler=external_handler,
                external_handler_sha256=external_handler_sha256,
                action="partial-uninstall",
                removed_platforms=request["removed_platforms"],
                removed_runtime_configs=request["removed_runtime_configs"],
            )
            if saved_plan is not None and saved_plan != operation.plan:
                raise ContractError("saved partial-uninstall plan is stale or differs from the current candidate")
            if args.dry_run:
                if args.approve_plan or args.approve:
                    raise ContractError("uninstall --dry-run does not accept approvals")
                if args.output:
                    dump(operation.plan, args.output)
                print(dumps(operation.plan), end="")
                return 0
            if saved_plan is None or args.approve_plan is None:
                raise ContractError("partial uninstall apply requires --plan and --approve-plan")
            result = apply_upgrade(
                operation,
                args.target_root,
                approve_plan=args.approve_plan,
                approvals=args.approve,
            )
            result["status"] = "partially-uninstalled"
            print(dumps(result), end="")
            return 0
        if args.command == "rollback":
            result = rollback_upgrade(
                args.target_root,
                approve_current_lock=args.approve_current_lock,
                approve_rollback_point=args.approve_rollback_point,
            )
            print(dumps(result), end="")
            return 0
        if args.command == "validate":
            validate(args.kind, load(args.artifact))
            print(dumps({"artifact": args.artifact, "kind": args.kind, "status": "passed"}), end="")
            return 0
        if args.command == "prepare-adapter":
            plan = load(args.plan)
            validate("workflow-plan", plan)
            active_package_lock = _require_active_package_lock(plan, args.lock)
            validate_provider_invocation_plan(plan, active_package_lock)
            request = build_adapter_request(
                plan, args.node_id, context=load(args.context), invocation_id=args.invocation_id
            )
            print(dumps(request), end="")
            return 0
        if args.command == "validate-adapter-result":
            request = load(args.request)
            validate_adapter_result(request, load(args.result))
            print(dumps({"request_id": request["request_id"], "status": "passed"}), end="")
            return 0
        if args.command == "invocation":
            if args.invocation_command == "prepare":
                plan = load(args.plan)
                validate("workflow-plan", plan)
                active_package_lock = _require_active_package_lock(plan, args.lock)
                value = prepare_provider_invocation(
                    args.root,
                    plan,
                    args.node_id,
                    context=load(args.context),
                    invocation_id=args.invocation_id,
                    package_lock=active_package_lock,
                )
            elif args.invocation_command == "claim":
                value = claim_provider_invocation(
                    args.root,
                    args.request_id,
                    actor_id=args.actor_id,
                    claim_token=load_claim_token_file(args.claim_token_file),
                )
            elif args.invocation_command == "submit":
                value = submit_provider_invocation(
                    args.root,
                    args.request_id,
                    load(args.result),
                    claim_token=load_claim_token_file(args.claim_token_file),
                )
            else:
                value = inspect_provider_invocation(
                    args.root,
                    args.request_id,
                )
            print(dumps(value), end="")
            return 0
        if args.command in {"run", "resume"}:
            plan = load(args.plan)
            validate("workflow-plan", plan)
            active_package_lock = _require_active_package_lock(plan, args.lock)
            if args.invocation_root:
                validate_provider_invocation_plan(plan, active_package_lock)
            if args.invocation_selection and not args.invocation_root:
                raise ContractError(
                    "--invocation-selection requires --invocation-root"
                )
            if args.adapter_results or args.invocation_root:
                if not args.adapter_context:
                    raise ContractError(
                        "--adapter-context is required with --adapter-results or --invocation-root"
                    )
                if args.invocation_root and not args.invocation_selection:
                    raise ContractError(
                        "--invocation-selection is required with --invocation-root"
                    )
                results = (
                    load(args.adapter_results)
                    if args.adapter_results
                    else collect_submitted_results(
                        args.invocation_root,
                        plan["fingerprint"],
                        load(args.invocation_selection),
                    )
                )
                executor = RecordedAdapterExecutor(results, context=load(args.adapter_context))
            else:
                executor = FakeAdapterExecutor()
            ledger = executor.run(
                plan,
                ledger_path=args.ledger,
                package_lock=active_package_lock,
                resume=args.command == "resume",
            )
            print(dumps(delivery_report(plan, ledger)), end="")
            return 0

        environment_roots = [item for item in os.environ.get("AGENT_WORKFLOW_PROVIDER_PATHS", "").split(os.pathsep) if item]
        registry = ManifestRegistry.from_directory(
            args.manifests,
            provider_roots=[*environment_roots, *args.provider_manifests],
            disabled_providers=args.disable_provider,
        )
        profile = DiscoveryEngine(registry).discover(
            args.repository,
            target_files=getattr(args, "target_file", []),
            changed_files=getattr(args, "changed_file", []),
        )
        if args.command == "detect":
            print(dumps(profile), end="")
            return 0
        policy = PolicyResolver().resolve(profile, args.task, explicit_platforms=args.platform)
        if args.command == "route":
            output = policy if args.explain else {"schema_version": "1.0", "selected_platforms": policy["selected_platforms"], "task": policy["task"]}
            print(dumps(output), end="")
            return 0
        package_lock = None
        if getattr(args, "lock", None):
            package_lock = load(args.lock)
            validate_package_lock(package_lock)
        workflow_plan = PlanCompiler(registry).compile(
            profile,
            policy,
            package_lock=package_lock,
        )
        print(dumps(workflow_plan), end="")
        return 0 if workflow_plan["status"] != "blocked" else 2
    except (ContractError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(dumps({"error": str(error), "status": "blocked"}), end="", file=sys.stderr)
        return 2


def _parse_lock_sources(values: list[str]) -> dict[str, dict[str, str]]:
    sources: dict[str, dict[str, str]] = {}
    for value in values:
        package_id, separator, uri = value.partition("=")
        if not separator or not package_id or not uri:
            raise ContractError("--source must use PACKAGE=URI")
        if package_id in sources:
            raise ContractError(f"duplicate --source package: {package_id}")
        if uri.startswith("registry://"):
            kind = "local-registry"
        elif uri.startswith("./"):
            kind = "relative-path"
        elif uri.startswith("https://"):
            kind = "https"
        else:
            raise ContractError(f"unsupported --source URI: {package_id}")
        sources[package_id] = {"kind": kind, "uri": uri}
    return sources


def _parse_package_hashes(values: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for value in values:
        package_id, separator, digest = value.partition("=")
        if not separator or not package_id or not digest:
            raise ContractError("--source-sha256 must use PACKAGE=SHA256")
        if package_id in hashes:
            raise ContractError(f"duplicate --source-sha256 package: {package_id}")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ContractError(f"invalid --source-sha256 digest: {package_id}")
        hashes[package_id] = digest
    return hashes


def _require_active_package_lock(
    plan: dict[str, Any],
    lock_path: str | None,
) -> dict[str, Any] | None:
    if plan.get("package_lock_hash") is None:
        if lock_path is not None:
            raise ContractError("workflow plan is not frozen to the supplied package Lockfile")
        return None
    if lock_path is None:
        raise ContractError("locked workflow operation requires the current package Lockfile")
    package_lock = load(lock_path)
    validate_package_lock(package_lock)
    validate_plan_package_lock(plan, package_lock)
    return package_lock


if __name__ == "__main__":
    raise SystemExit(main())
