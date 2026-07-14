"""Command-line interface for the Phase 1 workflow core."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .canonical_json import dumps, load
from .contracts import validate
from .discovery import DiscoveryEngine
from .models import ContractError
from .planning import PlanCompiler
from .policy import PolicyResolver
from .registry import ManifestRegistry
from .reporting import delivery_report
from .runtime import FakeAdapterExecutor


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_manifest_directory() -> Path:
    source_checkout = PROJECT_ROOT / "platforms"
    if source_checkout.is_dir():
        return source_checkout
    installed = Path(sys.prefix) / "share" / "agent-workflow" / "platforms"
    return installed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-workflow")
    parser.add_argument("--manifests", default=str(default_manifest_directory()))
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

    run = subparsers.add_parser("run")
    run.add_argument("plan")
    run.add_argument("--ledger", default="run-ledger.jsonl")
    run.add_argument("--fake-adapters", action="store_true", required=True)

    resume = subparsers.add_parser("resume")
    resume.add_argument("plan")
    resume.add_argument("--ledger", required=True)
    resume.add_argument("--fake-adapters", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            validate(args.kind, load(args.artifact))
            print(dumps({"artifact": args.artifact, "kind": args.kind, "status": "passed"}), end="")
            return 0
        if args.command in {"run", "resume"}:
            plan = load(args.plan)
            validate("workflow-plan", plan)
            ledger = FakeAdapterExecutor().run(plan, ledger_path=args.ledger, resume=args.command == "resume")
            print(dumps(delivery_report(plan, ledger)), end="")
            return 0

        registry = ManifestRegistry.from_directory(args.manifests)
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
