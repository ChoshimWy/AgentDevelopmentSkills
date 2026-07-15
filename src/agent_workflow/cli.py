"""Command-line interface for the Phase 1 workflow core."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .adapters import build_adapter_request, validate_adapter_result
from .canonical_json import dumps, load
from .contracts import validate
from .discovery import DiscoveryEngine
from .installation import build_install_bundle, install_bundle
from .models import ContractError
from .planning import PlanCompiler
from .policy import PolicyResolver
from .registry import ManifestRegistry
from .reporting import delivery_report
from .runtime import FakeAdapterExecutor, RecordedAdapterExecutor


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_manifest_directory() -> Path:
    source_checkout = PROJECT_ROOT / "platforms"
    if source_checkout.is_dir():
        return source_checkout
    installed = Path(sys.prefix) / "share" / "agent-workflow" / "platforms"
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

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("kind")
    validate_parser.add_argument("artifact")

    prepare_adapter = subparsers.add_parser("prepare-adapter")
    prepare_adapter.add_argument("plan")
    prepare_adapter.add_argument("node_id")
    prepare_adapter.add_argument("--context", required=True)
    prepare_adapter.add_argument("--invocation-id", required=True)

    validate_adapter = subparsers.add_parser("validate-adapter-result")
    validate_adapter.add_argument("request")
    validate_adapter.add_argument("result")

    install = subparsers.add_parser("install")
    install.add_argument("--core-only", action="store_true")
    install.add_argument("--platform", action="append", default=[])
    install.add_argument("--discipline", action="append", default=[])
    install.add_argument("--runtime-config", action="append", default=[])
    install.add_argument("--target-root", default=str(Path.home() / ".codex"))
    install.add_argument("--dry-run", action="store_true")

    run = subparsers.add_parser("run")
    run.add_argument("plan")
    run.add_argument("--ledger", default="run-ledger.jsonl")
    run_mode = run.add_mutually_exclusive_group(required=True)
    run_mode.add_argument("--fake-adapters", action="store_true")
    run_mode.add_argument("--adapter-results")
    run.add_argument("--adapter-context")

    resume = subparsers.add_parser("resume")
    resume.add_argument("plan")
    resume.add_argument("--ledger", required=True)
    resume_mode = resume.add_mutually_exclusive_group(required=True)
    resume_mode.add_argument("--fake-adapters", action="store_true")
    resume_mode.add_argument("--adapter-results")
    resume.add_argument("--adapter-context")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install":
            bundle = build_install_bundle(
                args.manifests,
                platforms=args.platform,
                disciplines=args.discipline,
                runtime_configs=args.runtime_config,
                core_only=args.core_only,
            )
            print(dumps(install_bundle(bundle, args.target_root, dry_run=args.dry_run)), end="")
            return 0
        if args.command == "validate":
            validate(args.kind, load(args.artifact))
            print(dumps({"artifact": args.artifact, "kind": args.kind, "status": "passed"}), end="")
            return 0
        if args.command == "prepare-adapter":
            plan = load(args.plan)
            validate("workflow-plan", plan)
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
        if args.command in {"run", "resume"}:
            plan = load(args.plan)
            validate("workflow-plan", plan)
            if args.adapter_results:
                if not args.adapter_context:
                    raise ContractError("--adapter-context is required with --adapter-results")
                executor = RecordedAdapterExecutor(load(args.adapter_results), context=load(args.adapter_context))
            else:
                executor = FakeAdapterExecutor()
            ledger = executor.run(plan, ledger_path=args.ledger, resume=args.command == "resume")
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
        workflow_plan = PlanCompiler(registry).compile(profile, policy)
        print(dumps(workflow_plan), end="")
        return 0 if workflow_plan["status"] != "blocked" else 2
    except (ContractError, OSError, json.JSONDecodeError) as error:
        print(dumps({"error": str(error), "status": "blocked"}), end="", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
