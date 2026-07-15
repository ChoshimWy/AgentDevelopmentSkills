#!/usr/bin/env python3
"""Apple adapter for committed cross-platform Worktree Session identities."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import sys
from typing import Any, Iterable

installed_runtime = Path(__file__).resolve().parents[3] / "bin" / "agent-session"
if installed_runtime.is_file() and not installed_runtime.is_symlink():
    sys.path.insert(0, str(installed_runtime))

try:
    from agent_workflow.canonical_json import dumps
    from agent_workflow.contracts import validate_worktree_session_context
    from agent_workflow.models import ContractError
    from agent_workflow.worktree_sessions.git_workspace import (
        inspect_repository,
        refresh_session_source_identity,
        session_source_identity,
    )
except ImportError as error:  # pragma: no cover - deployment preflight path.
    raise SystemExit(
        "worktree_session.py requires the AgentDevelopmentSkills Python package; "
        "install it or run with PYTHONPATH=<checkout>/src"
    ) from error


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def build_request(
    context: dict[str, Any],
    *,
    attempt_id: str,
    mode: str,
    environment_fingerprint: str,
    derived_data_slot: str,
    destination: str,
    test_plan: str,
    target_fingerprints: Iterable[str],
) -> dict[str, Any]:
    validate_worktree_session_context(context)
    if "apple" not in context["selected_platforms"]:
        raise ContractError("Apple verification requires apple in selected_platforms")
    if context["source_identity"]["mode"] != "committed":
        raise ContractError("Apple verification requires a committed Worktree Session checkpoint")
    if not IDENTIFIER.fullmatch(attempt_id):
        raise ContractError("Apple Worktree Session attempt_id is invalid")
    if mode not in {"dev", "checkpoint", "final"}:
        raise ContractError("Apple Worktree Session verification mode is invalid")
    if not isinstance(environment_fingerprint, str) or not environment_fingerprint:
        raise ContractError("Apple environment fingerprint is required")
    if derived_data_slot != expected_derived_data_slot(context["project_id"], environment_fingerprint):
        raise ContractError("DerivedData slot does not match the frozen project/environment identity")
    targets = sorted(set(target_fingerprints))
    if not destination or not test_plan or not targets:
        raise ContractError("Apple verification requires destination, test plan and target fingerprints")
    refreshed = refresh_session_source_identity(deepcopy(context))
    if refreshed["source_identity"] != context["source_identity"]:
        raise ContractError("Apple Worktree Session source identity is stale")
    primary = next(item for item in context["repositories"] if item["role"] == "primary")
    digest = context["source_identity"]["value"].split(":", 1)[1]
    request = {
        "artifact_namespace": f"sessions/{context['session_id']}/{digest}/{attempt_id}",
        "attempt_id": attempt_id,
        "derived_data_slot": derived_data_slot,
        "destination": destination,
        "environment_fingerprint": environment_fingerprint,
        "git_common_dir": primary["git_common_dir"],
        "mode": mode,
        "project_id": context["project_id"],
        "repositories": [
            {
                "base_commit": item["base"]["commit"],
                "checkpoint_commit": item["checkpoint"]["commit"],
                "checkpoint_tree": item["checkpoint"]["tree"],
                "git_common_dir": item["git_common_dir"],
                "patch_hash": item["change_set"]["patch_hash"],
                "repository_id": item["repository_id"],
                "role": item["role"],
                "worktree_path": item["worktree_path"],
            }
            for item in context["repositories"]
        ],
        "schema_version": "1.0",
        "session_id": context["session_id"],
        "source_identity": context["source_identity"]["value"],
        "target_fingerprints": targets,
        "test_plan": test_plan,
        "worktree_path": primary["worktree_path"],
    }
    validate_request(request, context)
    return request


def validate_request(request: dict[str, Any], context: dict[str, Any]) -> None:
    _validate_request_shape(request)
    validate_worktree_session_context(context)
    if "apple" not in context["selected_platforms"] or context["source_identity"]["mode"] != "committed":
        raise ContractError("Apple Worktree Session request requires a committed Apple Session")
    if not IDENTIFIER.fullmatch(request["attempt_id"]):
        raise ContractError("Apple Worktree Session request attempt_id is invalid")
    if (
        request["session_id"] != context["session_id"]
        or request["project_id"] != context["project_id"]
        or request["source_identity"] != context["source_identity"]["value"]
    ):
        raise ContractError("Apple Worktree Session request identity mismatch")
    primary = next(item for item in context["repositories"] if item["role"] == "primary")
    if request["worktree_path"] != primary["worktree_path"] or request["git_common_dir"] != primary["git_common_dir"]:
        raise ContractError("Apple Worktree Session primary Git paths mismatch")
    expected_repositories = [
        {
            "base_commit": item["base"]["commit"],
            "checkpoint_commit": item["checkpoint"]["commit"],
            "checkpoint_tree": item["checkpoint"]["tree"],
            "git_common_dir": item["git_common_dir"],
            "patch_hash": item["change_set"]["patch_hash"],
            "repository_id": item["repository_id"],
            "role": item["role"],
            "worktree_path": item["worktree_path"],
        }
        for item in context["repositories"]
    ]
    if request["repositories"] != expected_repositories:
        raise ContractError("Apple Worktree Session repository identities mismatch")
    if (
        request["mode"] not in {"dev", "checkpoint", "final"}
        or request["derived_data_slot"] != expected_derived_data_slot(
            request["project_id"], request["environment_fingerprint"]
        )
    ):
        raise ContractError("Apple Worktree Session mode or DerivedData slot is invalid")
    if not isinstance(request["environment_fingerprint"], str) or not request["environment_fingerprint"]:
        raise ContractError("Apple Worktree Session environment fingerprint is invalid")
    if (
        not isinstance(request["destination"], str)
        or not request["destination"]
        or not isinstance(request["test_plan"], str)
        or not request["test_plan"]
        or not isinstance(request["target_fingerprints"], list)
        or request["target_fingerprints"] != sorted(set(request["target_fingerprints"]))
        or not request["target_fingerprints"]
    ):
        raise ContractError("Apple Worktree Session execution identity is invalid")
    digest = request["source_identity"].split(":", 1)[1]
    expected_namespace = f"sessions/{request['session_id']}/{digest}/{request['attempt_id']}"
    if request["artifact_namespace"] != expected_namespace:
        raise ContractError("Apple Worktree Session artifact namespace is invalid")


def validate_daemon_request(
    request: dict[str, Any],
    *,
    repo_root: Path,
    destination: str,
    test_plan: str,
) -> dict[str, Any]:
    """Revalidate a queued request against live committed Worktrees before execution."""
    _validate_request_shape(request)
    root = repo_root.expanduser().resolve()
    primary = [item for item in request["repositories"] if item["role"] == "primary"]
    if len(primary) != 1 or Path(primary[0]["worktree_path"]).resolve() != root:
        raise ContractError("Apple daemon request primary Worktree does not match repo root")
    if request["worktree_path"] != primary[0]["worktree_path"] or request["git_common_dir"] != primary[0]["git_common_dir"]:
        raise ContractError("Apple daemon request primary Git identity is inconsistent")
    if request["destination"] != destination or request["test_plan"] != test_plan:
        raise ContractError("Apple daemon request execution metadata changed before queueing")

    live_repositories: list[dict[str, Any]] = []
    for frozen in request["repositories"]:
        live = inspect_repository(
            frozen["worktree_path"],
            repository_id=frozen["repository_id"],
            role=frozen["role"],
            base_ref=frozen["base_commit"],
            base_source="explicit",
            committed=True,
        )
        expected = {
            "base_commit": live["base"]["commit"],
            "checkpoint_commit": live["checkpoint"]["commit"],
            "checkpoint_tree": live["checkpoint"]["tree"],
            "git_common_dir": live["git_common_dir"],
            "patch_hash": live["change_set"]["patch_hash"],
            "repository_id": live["repository_id"],
            "role": live["role"],
            "worktree_path": live["worktree_path"],
        }
        if frozen != expected:
            raise ContractError(f"Apple daemon request repository identity is stale: {frozen['repository_id']}")
        live_repositories.append(live)
    if session_source_identity(live_repositories, mode="committed") != request["source_identity"]:
        raise ContractError("Apple daemon request Session source identity is stale")
    return deepcopy(request)


def _validate_request_shape(request: dict[str, Any]) -> None:
    fields = {
        "schema_version", "session_id", "project_id", "attempt_id", "worktree_path",
        "git_common_dir", "source_identity", "repositories", "mode",
        "environment_fingerprint", "derived_data_slot", "artifact_namespace",
        "destination", "test_plan", "target_fingerprints",
    }
    if not isinstance(request, dict) or set(request) != fields or request.get("schema_version") != "1.0":
        raise ContractError("Apple Worktree Session request fields are invalid")
    if any(not isinstance(request[field], str) or not IDENTIFIER.fullmatch(request[field]) for field in ("session_id", "project_id", "attempt_id")):
        raise ContractError("Apple Worktree Session request identifiers are invalid")
    if (
        request["mode"] not in {"dev", "checkpoint", "final"}
        or request["derived_data_slot"] != expected_derived_data_slot(
            request["project_id"], request["environment_fingerprint"]
        )
    ):
        raise ContractError("Apple Worktree Session mode or DerivedData slot is invalid")
    if (
        not isinstance(request["environment_fingerprint"], str)
        or not request["environment_fingerprint"]
        or not isinstance(request["destination"], str)
        or not request["destination"]
        or not isinstance(request["test_plan"], str)
        or not request["test_plan"]
        or not isinstance(request["target_fingerprints"], list)
        or request["target_fingerprints"] != sorted(set(request["target_fingerprints"]))
        or not request["target_fingerprints"]
        or any(not isinstance(item, str) or not item for item in request["target_fingerprints"])
    ):
        raise ContractError("Apple Worktree Session execution identity is invalid")
    if not re.fullmatch(r"session-source:[0-9a-f]{64}", request["source_identity"]):
        raise ContractError("Apple Worktree Session source identity is invalid")
    digest = request["source_identity"].split(":", 1)[1]
    expected_namespace = f"sessions/{request['session_id']}/{digest}/{request['attempt_id']}"
    if request["artifact_namespace"] != expected_namespace:
        raise ContractError("Apple Worktree Session artifact namespace is invalid")
    repositories = request.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ContractError("Apple Worktree Session repositories are invalid")
    expected_fields = {
        "base_commit", "checkpoint_commit", "checkpoint_tree", "git_common_dir", "patch_hash",
        "repository_id", "role", "worktree_path",
    }
    ids: list[str] = []
    paths: list[str] = []
    for item in repositories:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ContractError("Apple Worktree Session repository fields are invalid")
        if item["role"] not in {"primary", "dependency"}:
            raise ContractError("Apple Worktree Session repository role is invalid")
        if not all(isinstance(item[field], str) and item[field] for field in expected_fields):
            raise ContractError("Apple Worktree Session repository identity is invalid")
        if (
            not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item["base_commit"])
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item["checkpoint_commit"])
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item["checkpoint_tree"])
            or not re.fullmatch(r"repository-patch:[0-9a-f]{64}", item["patch_hash"])
            or not Path(item["worktree_path"]).is_absolute()
            or not Path(item["git_common_dir"]).is_absolute()
        ):
            raise ContractError("Apple Worktree Session repository identity is invalid")
        ids.append(item["repository_id"])
        paths.append(item["worktree_path"])
    if ids != sorted(set(ids)) or len(paths) != len(set(paths)) or sum(item["role"] == "primary" for item in repositories) != 1:
        raise ContractError("Apple Worktree Session repository closure is invalid")


def immutable_build_artifact_identity(
    request: dict[str, Any],
    *,
    artifact_root: Path,
    xctestrun: str,
    test_bundles: Iterable[str],
    product_artifacts: Iterable[str],
) -> dict[str, Any]:
    """Freeze build-for-testing output before any test-without-building reuse."""
    if PurePosixPath(xctestrun).suffix != ".xctestrun":
        raise ContractError("immutable Apple build identity requires an .xctestrun file")
    bundle_paths = sorted(set(test_bundles))
    if not bundle_paths or any(PurePosixPath(item).suffix != ".xctest" for item in bundle_paths):
        raise ContractError("immutable Apple build identity requires at least one .xctest bundle")
    product_paths = sorted(set(product_artifacts))
    if not product_paths or set(product_paths) & ({xctestrun} | set(bundle_paths)):
        raise ContractError("immutable Apple build identity requires a distinct non-empty product closure")
    xctestrun_entry = _artifact_entry(artifact_root, xctestrun)
    _validate_xctestrun_closure(artifact_root, xctestrun, [*bundle_paths, *product_paths])
    files = [{**xctestrun_entry, "role": "xctestrun"}]
    files.extend({**_artifact_entry(artifact_root, item), "role": "test-bundle"} for item in bundle_paths)
    files.extend({**_artifact_entry(artifact_root, item), "role": "build-product"} for item in product_paths)
    identity = {
        "artifact_files": files,
        "attempt_id": request["attempt_id"],
        "derived_data_slot": request["derived_data_slot"],
        "destination": request["destination"],
        "environment_fingerprint": request["environment_fingerprint"],
        "schema_version": "1.0",
        "source_identity": request["source_identity"],
        "target_fingerprints": request["target_fingerprints"],
        "test_plan": request["test_plan"],
    }
    identity["identity_sha256"] = hashlib.sha256(dumps(identity).encode("utf-8")).hexdigest()
    return identity


def validate_immutable_build_artifact_identity(
    value: dict[str, Any], request: dict[str, Any], *, artifact_root: Path
) -> None:
    fields = {
        "schema_version", "attempt_id", "source_identity", "environment_fingerprint",
        "derived_data_slot", "destination", "test_plan", "target_fingerprints",
        "artifact_files", "identity_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != "1.0":
        raise ContractError("immutable Apple build artifact identity fields are invalid")
    for field in (
        "attempt_id", "source_identity", "environment_fingerprint", "derived_data_slot",
        "destination", "test_plan", "target_fingerprints",
    ):
        if value[field] != request[field]:
            raise ContractError(f"immutable Apple build artifact {field} mismatch")
    identity = {key: item for key, item in value.items() if key != "identity_sha256"}
    if hashlib.sha256(dumps(identity).encode("utf-8")).hexdigest() != value["identity_sha256"]:
        raise ContractError("immutable Apple build artifact identity digest mismatch")
    for item in value["artifact_files"]:
        actual = {**_artifact_entry(artifact_root, item["path"]), "role": item["role"]}
        if actual != item:
            raise ContractError(f"immutable Apple build artifact changed: {item['path']}")
    roles = [item.get("role") for item in value["artifact_files"]]
    if roles.count("xctestrun") != 1 or "test-bundle" not in roles or "build-product" not in roles:
        raise ContractError("immutable Apple build artifact closure is incomplete")
    xctestrun = next(item["path"] for item in value["artifact_files"] if item["role"] == "xctestrun")
    closure = [item["path"] for item in value["artifact_files"] if item["role"] != "xctestrun"]
    _validate_xctestrun_closure(artifact_root, xctestrun, closure)


def _artifact_entry(root: Path, raw: str) -> dict[str, Any]:
    first = _artifact_entry_once(root, raw)
    second = _artifact_entry_once(root, raw)
    if first != second:
        raise ContractError(f"Apple build artifact changed while hashing: {raw}")
    return first


def _artifact_entry_once(root: Path, raw: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ContractError("Apple build artifact path must be safe and relative")
    path = root / Path(*relative.parts)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ContractError("Apple build artifact escapes the artifact root") from error
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ContractError(f"Apple build artifact path contains a symlink: {raw}")
    if not path.exists():
        raise ContractError(f"Apple build artifact is missing or unsafe: {raw}")
    if path.is_file():
        snapshot = _file_snapshot(path)
        return {
            "kind": "file",
            "path": relative.as_posix(),
            "sha256": snapshot["sha256"],
            "size": snapshot["size"],
        }
    if not path.is_dir():
        raise ContractError(f"unsupported Apple build artifact type: {raw}")
    inventory = []
    total_size = 0
    for child in sorted(path.rglob("*")):
        child_relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            raise ContractError(f"Apple build artifact bundle contains a symlink: {raw}/{child_relative}")
        if child.is_dir():
            continue
        if not child.is_file():
            raise ContractError(f"unsupported Apple build artifact bundle entry: {raw}/{child_relative}")
        snapshot = _file_snapshot(child)
        total_size += snapshot["size"]
        inventory.append({
            "mode": snapshot["mode"],
            "path": child_relative,
            "sha256": snapshot["sha256"],
            "size": snapshot["size"],
        })
    return {
        "kind": "directory",
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(dumps(inventory).encode("utf-8")).hexdigest(),
        "size": total_size,
    }


def _valid_slot(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return len(parts) == 2 and all(IDENTIFIER.fullmatch(part) for part in parts)


def expected_derived_data_slot(project_id: str, environment_fingerprint: str) -> str:
    digest = hashlib.sha256(environment_fingerprint.encode("utf-8")).hexdigest()
    value = f"{project_id}/env-{digest}"
    if not _valid_slot(value):
        raise ContractError("Apple project/environment identity cannot form a safe DerivedData slot")
    return value


def _load_request_file(path: Path) -> Any:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ContractError("Apple daemon request file is missing or unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        payload = stream.read()
    after = path.lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise ContractError("Apple daemon request file changed while reading")
    return json.loads(payload.decode("utf-8"))


def _validate_xctestrun_closure(root: Path, xctestrun: str, artifacts: list[str]) -> None:
    path = root.expanduser().resolve() / Path(*PurePosixPath(xctestrun).parts)
    try:
        document = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ContractError("immutable Apple build identity requires a valid .xctestrun plist") from error

    references: set[str] = set()
    unsafe_references: set[str] = set()
    placeholder_pattern = re.compile(r"(__[A-Z0-9_]+__/[^:\s]+)")
    absolute_pattern = re.compile(r"(?:^|:)(/[^:\s]+)")

    def add_reference(raw: str) -> None:
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            unsafe_references.add(raw)
        else:
            references.add(path.as_posix())

    def validate_platform_reference(token: str) -> None:
        suffix = token.removeprefix("__PLATFORMS__/")
        path = PurePosixPath(suffix)
        if not suffix or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            unsafe_references.add(token)

    def collect(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for item_key, item in value.items():
                collect(item, item_key)
        elif isinstance(value, list):
            for item in value:
                collect(item, key)
        elif isinstance(value, str):
            unsafe_references.update(match.group(1) for match in absolute_pattern.finditer(value))
            for match in placeholder_pattern.finditer(value):
                token = match.group(1)
                if token.startswith("__TESTROOT__/"):
                    add_reference(token.removeprefix("__TESTROOT__/"))
                elif token.startswith("__PLATFORMS__/"):
                    validate_platform_reference(token)
                else:
                    # SDK/platform paths are frozen by the environment fingerprint;
                    # all host, bundle and built-product placeholders must resolve
                    # through the hashed TESTROOT artifact closure.
                    unsafe_references.add(token)
            if key and (key.endswith("Path") or key.endswith("Paths")):
                for token in filter(None, value.split(":")):
                    if token.startswith("__TESTROOT__/"):
                        add_reference(token.removeprefix("__TESTROOT__/"))
                    elif token.startswith("__PLATFORMS__/"):
                        validate_platform_reference(token)
                        continue
                    elif not token.startswith("__") and not PurePosixPath(token).is_absolute():
                        add_reference(token)

    collect(document)
    if unsafe_references:
        raise ContractError(
            ".xctestrun contains execution paths outside the immutable artifact closure: "
            + ", ".join(sorted(unsafe_references))
        )
    if not references:
        raise ContractError(".xctestrun does not declare a TESTROOT product closure")
    roots = [PurePosixPath(item).as_posix().rstrip("/") for item in artifacts]
    uncovered = sorted(
        reference
        for reference in references
        if not any(reference == item or reference.startswith(f"{item}/") for item in roots)
    )
    if uncovered:
        raise ContractError(f".xctestrun product closure is incomplete: {', '.join(uncovered)}")


def _file_snapshot(path: Path) -> dict[str, Any]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ContractError(f"Apple build artifact is not a regular file: {path}")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        opened = os.fstat(stream.fileno())
    after = path.lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise ContractError(f"Apple build artifact changed while hashing: {path}")
    return {"mode": opened.st_mode & 0o777, "sha256": digest.hexdigest(), "size": opened.st_size}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "validate-daemon-request":
        validator = argparse.ArgumentParser(description="Validate a queued Apple Worktree Session request")
        validator.add_argument("validate-daemon-request")
        validator.add_argument("--request", type=Path, required=True)
        validator.add_argument("--repo-root", type=Path, required=True)
        validator.add_argument("--destination")
        validator.add_argument("--test-plan")
        args = validator.parse_args(arguments)
        try:
            request = _load_request_file(args.request)
            validated = validate_daemon_request(
                request,
                repo_root=args.repo_root,
                destination=args.destination or (request.get("destination", "") if isinstance(request, dict) else ""),
                test_plan=args.test_plan or (request.get("test_plan", "") if isinstance(request, dict) else ""),
            )
        except (ContractError, OSError, UnicodeError, json.JSONDecodeError) as error:
            print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
            return 2
        print(dumps(validated), end="")
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--mode", choices=("dev", "checkpoint", "final"), required=True)
    parser.add_argument("--environment-fingerprint", required=True)
    parser.add_argument("--derived-data-slot", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--test-plan", required=True)
    parser.add_argument("--target-fingerprint", action="append", required=True)
    args = parser.parse_args(arguments)
    context = json.loads(args.context.read_text(encoding="utf-8"))
    try:
        request = build_request(
            context,
            attempt_id=args.attempt_id,
            mode=args.mode,
            environment_fingerprint=args.environment_fingerprint,
            derived_data_slot=args.derived_data_slot,
            destination=args.destination,
            test_plan=args.test_plan,
            target_fingerprints=args.target_fingerprint,
        )
    except (ContractError, OSError) as error:
        print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
        return 2
    print(dumps(request), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
