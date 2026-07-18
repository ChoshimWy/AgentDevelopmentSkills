"""CLI for cross-platform Git Worktree Session lifecycle primitives."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from ..canonical_json import dumps, load
from ..contracts import validate_manifest
from ..models import ContractError
from ..registry import ManifestRegistry
from .git_workspace import (
    create_session_worktree,
    refresh_session_source_identity,
    remove_created_session_worktree,
)
from .registry import SessionRegistry, new_session_context


def _manifest_root_default() -> Path | None:
    executable = Path(sys.argv[0]).expanduser().resolve()
    if len(executable.parents) >= 2:
        installed = executable.parents[1] / ".agent-skills" / "packages"
        if installed.is_dir() and not installed.is_symlink():
            return installed
    wheel_data = Path(sys.prefix) / "share" / "agent-workflow" / "platforms"
    if wheel_data.is_dir() and not wheel_data.is_symlink():
        return wheel_data
    candidate = Path(__file__).resolve().parents[3] / "platforms"
    return candidate if candidate.is_dir() else None


def _validate_platform_selection(selected: list[str], root: Path | None) -> list[str]:
    values = sorted(set(selected))
    if len(values) != len(selected):
        raise ContractError("selected platforms must be unique")
    if not values:
        return []
    if root is None or not root.is_dir() or root.is_symlink():
        raise ContractError("platform selection requires an explicit trusted Manifest root")
    for platform_id in values:
        manifest_path = root / platform_id / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ContractError(f"bootstrap_required: platform Manifest is unavailable: {platform_id}")
        manifest = load(manifest_path)
        validate_manifest(manifest)
        if manifest.get("id") != platform_id or manifest.get("kind") != "platform":
            raise ContractError(f"bootstrap_required: invalid platform package: {platform_id}")
        if manifest.get("implementation_status") != "implemented" or not isinstance(manifest.get("installation"), dict):
            raise ContractError(f"bootstrap_required: platform Provider is not implemented: {platform_id}")
    return values


def _platform_contexts(selected: list[str], root: Path | None) -> dict[str, dict[str, Any]]:
    if not selected:
        return {}
    assert root is not None
    registry = ManifestRegistry.from_directory(root)
    contexts: dict[str, dict[str, Any]] = {}
    for platform_id in selected:
        manifest = load(root / platform_id / "manifest.json")
        provider_relative = manifest["installation"].get("provider_manifest")
        provider_contract = manifest.get("provider_contract")
        if not isinstance(provider_relative, str) or not isinstance(provider_contract, dict):
            raise ContractError(f"bootstrap_required: platform Provider contract is unavailable: {platform_id}")
        registered = registry.by_id(provider_contract.get("package_id"))
        if registered is None:
            raise ContractError(f"bootstrap_required: platform Provider Manifest is unavailable: {platform_id}")
        provider = registered.value
        if provider.get("id") != provider_contract.get("package_id") or provider.get("role") != "provider":
            raise ContractError(f"bootstrap_required: platform Provider identity is invalid: {platform_id}")
        bindings = provider.get("bindings")
        if not isinstance(bindings, dict) or not bindings:
            raise ContractError(f"bootstrap_required: platform Provider binding closure is empty: {platform_id}")
        contexts[platform_id] = {
            "bindings": bindings,
            "context": {},
            "provider_id": provider["id"],
        }
    return contexts


def _capability_closure(
    platform_contexts: dict[str, dict[str, Any]], root: Path | None
) -> dict[str, dict[str, Any]]:
    closure = {
        capability: {"binding": binding, "provider_id": context["provider_id"]}
        for context in platform_contexts.values()
        for capability, binding in context["bindings"].items()
    }
    if root is None:
        return closure
    registry = ManifestRegistry.from_directory(root)
    for capability in ("review.independent", "verification.git.repository"):
        resolved = registry.resolve_binding(capability)
        if resolved is None:
            raise ContractError(f"{capability} capability closure is unavailable")
        closure[capability] = {
            "binding": resolved.binding,
            "provider_id": resolved.provider_id,
        }
    return closure


def _registry(repository: Path) -> SessionRegistry:
    return SessionRegistry(repository)


def _create(args: argparse.Namespace) -> dict[str, Any]:
    manifest_root = args.platform_manifest_root.resolve() if args.platform_manifest_root else _manifest_root_default()
    selected = _validate_platform_selection(args.platform, manifest_root)
    platform_contexts = _platform_contexts(selected, manifest_root)
    registry = _registry(args.repository)
    session_id = args.session_id or args.name
    registry.assert_available(session_id)
    repository, notice = create_session_worktree(
        args.repository,
        name=args.name,
        repository_id="primary",
        base_ref=args.base,
        base_source=args.base_source,
        worktree_root=args.worktree_root,
        branch=args.branch,
    )
    try:
        context = new_session_context(
            session_id=session_id,
            project_id=args.project_id,
            repositories=[repository],
            selected_platforms=selected,
            platform_contexts=platform_contexts,
            capability_closure=_capability_closure(platform_contexts, manifest_root),
        )
        context = registry.create_active(context)
    except (ContractError, OSError) as error:
        try:
            remove_created_session_worktree(repository, source_repository=args.repository)
        except (ContractError, OSError) as cleanup_error:
            raise ContractError(
                f"session registration failed ({error}); exact Worktree compensation was blocked ({cleanup_error})"
            ) from error
        raise
    return {"notice": notice, "operation": "create", "schema_version": "1.0", "session": context}


def _list(args: argparse.Namespace) -> dict[str, Any]:
    return {"schema_version": "1.0", "sessions": _registry(args.repository).list()}


def _inspect(args: argparse.Namespace) -> dict[str, Any]:
    registry = _registry(args.repository)
    context = registry.load(args.session_id)
    if args.refresh:
        refresh_session_source_identity(context)
    return context


def _fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    context = _registry(args.repository).load(args.session_id)
    return refresh_session_source_identity(context)


def _checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    registry = _registry(args.repository)
    context = registry.checkpoint(args.session_id)
    return {
        "notice": {"commits_created": False, "staging_changed": False},
        "operation": "checkpoint",
        "schema_version": "1.0",
        "session": context,
    }


def _gate(args: argparse.Namespace) -> dict[str, Any]:
    registry = _registry(args.repository)
    pairs = [load(path) for path in args.pair]
    ledger = load(args.ledger)
    result = registry.attach_and_gate(
        args.session_id,
        adapter_pairs=pairs,
        ledger=ledger,
        artifact_root=args.artifact_root,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create an isolated Worktree from a stable Commit")
    create.add_argument("name")
    create.add_argument("--repository", type=Path, default=Path("."))
    create.add_argument("--project-id", required=True)
    create.add_argument("--session-id")
    create.add_argument("--base")
    create.add_argument(
        "--base-source",
        choices=("explicit", "integration-checkpoint", "stacked-checkpoint", "clean-head"),
    )
    create.add_argument("--branch")
    create.add_argument("--worktree-root", type=Path)
    create.add_argument("--platform", action="append", default=[])
    create.add_argument("--platform-manifest-root", type=Path)
    create.set_defaults(handler=_create)

    listing = subparsers.add_parser("list", help="list registered Worktree Sessions")
    listing.add_argument("--repository", type=Path, default=Path("."))
    listing.set_defaults(handler=_list)

    inspect = subparsers.add_parser("inspect", help="read or refresh one Worktree Session")
    inspect.add_argument("session_id")
    inspect.add_argument("--repository", type=Path, default=Path("."))
    inspect.add_argument("--refresh", action="store_true")
    inspect.set_defaults(handler=_inspect)

    fingerprint = subparsers.add_parser("fingerprint", help="refresh source identity without writing Registry state")
    fingerprint.add_argument("session_id")
    fingerprint.add_argument("--repository", type=Path, default=Path("."))
    fingerprint.set_defaults(handler=_fingerprint)

    checkpoint = subparsers.add_parser("checkpoint", help="freeze existing clean HEAD Commits; does not stage or commit")
    checkpoint.add_argument("session_id")
    checkpoint.add_argument("--repository", type=Path, default=Path("."))
    checkpoint.set_defaults(handler=_checkpoint)

    gate = subparsers.add_parser("gate", help="validate Adapter/Ledger evidence against a committed Session")
    gate.add_argument("session_id")
    gate.add_argument("--repository", type=Path, default=Path("."))
    gate.add_argument("--pair", type=Path, action="append", required=True)
    gate.add_argument("--ledger", type=Path, required=True)
    gate.add_argument("--artifact-root", type=Path, required=True)
    gate.set_defaults(handler=_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        value = args.handler(args)
    except (ContractError, OSError) as error:
        print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
        return 2
    print(dumps(value), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
