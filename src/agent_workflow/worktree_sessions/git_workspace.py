"""Fail-closed Git Worktree creation and repository source fingerprints."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any, Iterable

from ..canonical_json import sha256
from ..models import ContractError


SESSION_NAME = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _git(
    root: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update({"LC_ALL": "C", "LANG": "C"})
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        capture_output=True,
        check=False,
        env=environment,
        text=text,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(f"git command failed ({' '.join(args)}): {stderr or 'unknown error'}")
    return result


def _git_text(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.strip()


def resolve_worktree(path: str | Path) -> tuple[Path, Path]:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir() or candidate.is_symlink():
        raise ContractError(f"worktree path is missing or unsafe: {candidate}")
    root = Path(_git_text(candidate, "rev-parse", "--show-toplevel")).resolve()
    # Commands may be invoked below the root; identity always binds the root.
    common_raw = _git_text(root, "rev-parse", "--git-common-dir")
    common_candidate = Path(common_raw)
    common = (common_candidate if common_candidate.is_absolute() else root / common_candidate).resolve()
    if not common.is_dir() or common.is_symlink():
        raise ContractError(f"git common dir is missing or unsafe: {common}")
    return root, common


def resolve_commit(root: str | Path, ref: str) -> str:
    worktree, _ = resolve_worktree(root)
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise ContractError("base ref is invalid")
    commit = _git_text(worktree, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if len(commit) not in {40, 64} or any(character not in "0123456789abcdef" for character in commit):
        raise ContractError("resolved base is not a full Git object id")
    return commit


def worktree_status(root: str | Path) -> dict[str, Any]:
    worktree, _ = resolve_worktree(root)
    staged = _git(
        worktree,
        "diff",
        "--cached",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        check=False,
    ).returncode
    unstaged = _git(
        worktree,
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        check=False,
    ).returncode
    if staged not in {0, 1} or unstaged not in {0, 1}:
        raise ContractError("unable to classify worktree status")
    untracked = _untracked_paths(worktree)
    return {
        "dirty": bool(staged or unstaged or untracked),
        "staged": staged == 1,
        "unstaged": unstaged == 1,
        "untracked": untracked,
    }


def inspect_repository(
    root: str | Path,
    *,
    repository_id: str,
    role: str = "primary",
    base_ref: str = "HEAD",
    base_source: str = "explicit",
    committed: bool = False,
) -> dict[str, Any]:
    _validate_identifier(repository_id, "repository id")
    if role not in {"primary", "dependency"}:
        raise ContractError("repository role is invalid")
    if base_source not in {"explicit", "integration-checkpoint", "stacked-checkpoint", "clean-head"}:
        raise ContractError("repository base source is invalid")
    worktree, common = resolve_worktree(root)
    base_commit = resolve_commit(worktree, base_ref)
    head = resolve_commit(worktree, "HEAD")
    ancestor = _git(worktree, "merge-base", "--is-ancestor", base_commit, head, check=False)
    if ancestor.returncode != 0:
        raise ContractError("repository HEAD does not descend from the frozen base commit")
    branch_result = _git(worktree, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    checkpoint = None
    if committed:
        status = worktree_status(worktree)
        if status["dirty"]:
            raise ContractError("committed repository identity requires a clean worktree")
        checkpoint = {"commit": head, "tree": _git_text(worktree, "rev-parse", f"{head}^{{tree}}")}
    change_set = repository_patch(
        worktree,
        repository_id=repository_id,
        base_commit=base_commit,
        checkpoint_commit=head if committed else None,
    )
    return {
        "base": {
            "commit": base_commit,
            "dirty_worktree_inherited": False,
            "ref": base_ref,
            "source": base_source,
        },
        "branch": branch,
        "change_set": change_set,
        "checkpoint": checkpoint,
        "git_common_dir": str(common),
        "repository_id": repository_id,
        "role": role,
        "worktree_path": str(worktree),
    }


def create_session_worktree(
    repository: str | Path,
    *,
    name: str,
    repository_id: str = "primary",
    base_ref: str | None = None,
    base_source: str | None = None,
    worktree_root: str | Path | None = None,
    branch: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_identifier(name, "session name")
    _validate_identifier(repository_id, "repository id")
    source_root, common = resolve_worktree(repository)
    status = worktree_status(source_root)
    if base_ref is None:
        if base_source is not None:
            raise ContractError("base_source requires an explicit base ref")
        if status["dirty"]:
            raise ContractError(
                "cannot infer session base from a dirty worktree; specify an explicit base commit or ref"
            )
        effective_ref = "HEAD"
        effective_source = "clean-head"
    else:
        if base_source == "clean-head":
            raise ContractError("clean-head base_source is reserved for an inferred clean HEAD")
        effective_ref = base_ref
        effective_source = base_source or "explicit"
    base_commit = resolve_commit(source_root, effective_ref)
    if _commit_has_gitlinks(source_root, base_commit):
        raise ContractError("repository-patch-v1 does not support Git submodules/gitlinks")
    branch_name = branch or f"agent/{name}"
    if not branch_name or branch_name.startswith("-") or ".." in branch_name:
        raise ContractError("session branch is invalid")
    branch_exists = _git(source_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}", check=False)
    if branch_exists.returncode == 0:
        raise ContractError(f"session branch already exists: {branch_name}")
    if branch_exists.returncode not in {0, 1}:
        raise ContractError("unable to inspect session branch")
    parent = Path(worktree_root).expanduser().resolve() if worktree_root else source_root.parent / ".agent-worktrees" / source_root.name
    target = (parent / name).resolve()
    if target == source_root or source_root in target.parents:
        raise ContractError("session worktree must not be nested inside the source worktree")
    if target.exists() or target.is_symlink():
        raise ContractError(f"session worktree path already exists: {target}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ContractError("session worktree root must not be a symlink")
    _git(source_root, "worktree", "add", str(target), "-b", branch_name, base_commit)
    try:
        created, created_common = resolve_worktree(target)
        if created != target or created_common != common:
            raise ContractError("created worktree does not belong to the expected Git common dir")
        record = inspect_repository(
            created,
            repository_id=repository_id,
            role="primary",
            base_ref=base_commit,
            base_source=effective_source,
        )
    except (ContractError, OSError) as error:
        try:
            _remove_exact_created_worktree(
                source_root=source_root,
                expected_common=common,
                worktree_path=target,
                branch=branch_name,
                base_commit=base_commit,
            )
        except (ContractError, OSError) as cleanup_error:
            raise ContractError(
                f"worktree creation validation failed ({error}); exact compensation was blocked ({cleanup_error})"
            ) from error
        raise
    notice = {
        "base_commit": base_commit,
        "base_ref": effective_ref,
        "source_worktree_dirty": status["dirty"],
        "source_worktree_changes_inherited": False,
    }
    return record, notice


def remove_created_session_worktree(record: dict[str, Any], *, source_repository: str | Path) -> None:
    """Compensate only a pristine Worktree/Branch created by this operation."""
    worktree, common = resolve_worktree(record["worktree_path"])
    if str(common) != record["git_common_dir"] or worktree_status(worktree)["dirty"]:
        raise ContractError("created worktree changed; refusing automatic compensation")
    head = resolve_commit(worktree, "HEAD")
    branch = _git_text(worktree, "symbolic-ref", "--short", "-q", "HEAD")
    if head != record["base"]["commit"] or branch != record["branch"]:
        raise ContractError("created worktree identity changed; refusing automatic compensation")
    source_root, source_common = resolve_worktree(source_repository)
    if source_common != common:
        raise ContractError("created worktree compensation source is invalid")
    _git(source_root, "worktree", "remove", str(worktree))
    _git(source_root, "branch", "-D", branch)


def _remove_exact_created_worktree(
    *,
    source_root: Path,
    expected_common: Path,
    worktree_path: Path,
    branch: str,
    base_commit: str,
) -> None:
    """Remove only the pristine branch/worktree pair created by this invocation."""
    worktree, common = resolve_worktree(worktree_path)
    if worktree != worktree_path or common != expected_common or worktree_status(worktree)["dirty"]:
        raise ContractError("created worktree changed; refusing automatic compensation")
    if resolve_commit(worktree, "HEAD") != base_commit:
        raise ContractError("created worktree HEAD changed; refusing automatic compensation")
    active_branch = _git_text(worktree, "symbolic-ref", "--short", "-q", "HEAD")
    if active_branch != branch or resolve_commit(source_root, f"refs/heads/{branch}") != base_commit:
        raise ContractError("created worktree branch changed; refusing automatic compensation")
    _git(source_root, "worktree", "remove", str(worktree))
    _git(source_root, "branch", "-D", branch)


def repository_patch(
    root: str | Path,
    *,
    repository_id: str,
    base_commit: str,
    checkpoint_commit: str | None = None,
) -> dict[str, Any]:
    _validate_identifier(repository_id, "repository id")
    worktree, _ = resolve_worktree(root)
    base = resolve_commit(worktree, base_commit)
    checkpoint = resolve_commit(worktree, checkpoint_commit) if checkpoint_commit else None
    if _has_gitlinks(worktree):
        raise ContractError("repository-patch-v1 does not support Git submodules/gitlinks")
    first = _repository_patch_once(worktree, repository_id, base, checkpoint)
    second = _repository_patch_once(worktree, repository_id, base, checkpoint)
    if first != second:
        raise ContractError("repository changed while computing the patch fingerprint")
    return {
        "algorithm": "repository-patch-v1",
        "changed_files": first["changed_files"],
        "patch_hash": f"repository-patch:{sha256(first)}",
        "untracked_files": first["untracked_files"],
    }


def _repository_patch_once(
    root: Path,
    repository_id: str,
    base_commit: str,
    checkpoint_commit: str | None,
) -> dict[str, Any]:
    if checkpoint_commit is None:
        diff_args = (
            "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", base_commit, "--",
        )
        untracked = _untracked_paths(root)
    else:
        status = worktree_status(root)
        if status["dirty"]:
            raise ContractError("checkpoint fingerprint requires a clean worktree")
        diff_args = (
            "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
            base_commit, checkpoint_commit, "--",
        )
        untracked = []
    diff = _git(root, *diff_args, text=False).stdout
    changed = _changed_paths(root, base_commit, checkpoint_commit)
    untracked_inventory = [_untracked_entry(root, path) for path in untracked]
    return {
        "base_commit": base_commit,
        "changed_files": changed,
        "checkpoint_commit": checkpoint_commit,
        "repository_id": repository_id,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "tracked_diff_size": len(diff),
        "untracked_files": untracked,
        "untracked_inventory": untracked_inventory,
    }


def _changed_paths(root: Path, base: str, checkpoint: str | None) -> list[str]:
    args = ["diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", base]
    if checkpoint:
        args.append(checkpoint)
    args.append("--")
    raw = _git(root, *args, text=False).stdout
    paths = _decode_git_paths(raw)
    return sorted(set(paths) | set(_untracked_paths(root) if checkpoint is None else []))


def _untracked_paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-z", "--others", "--exclude-standard", text=False).stdout
    return sorted(_decode_git_paths(raw))


def _decode_git_paths(raw: bytes) -> list[str]:
    try:
        values = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as error:
        raise ContractError("Git path is not valid UTF-8") from error
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value:
            raise ContractError(f"Git returned an unsafe path: {value!r}")
    return values


def _untracked_entry(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    before = path.lstat()
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISREG(before.st_mode):
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            opened = os.fstat(handle.fileno())
        after = path.lstat()
        if _stat_identity(before) != _stat_identity(opened) or _stat_identity(opened) != _stat_identity(after):
            raise ContractError(f"untracked file changed while hashing: {relative}")
        return {"kind": "file", "mode": mode, "path": relative, "sha256": digest.hexdigest(), "size": opened.st_size}
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(path)
        after = path.lstat()
        if _stat_identity(before) != _stat_identity(after):
            raise ContractError(f"untracked symlink changed while hashing: {relative}")
        data = os.fsencode(target)
        return {"kind": "symlink", "mode": mode, "path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    raise ContractError(f"unsupported untracked file type: {relative}")


def _has_gitlinks(root: Path) -> bool:
    raw = _git(root, "ls-files", "-s", "-z", text=False).stdout
    return any(record.startswith(b"160000 ") for record in raw.split(b"\0") if record)


def _commit_has_gitlinks(root: Path, commit: str) -> bool:
    raw = _git(root, "ls-tree", "-r", "-z", commit, text=False).stdout
    return any(record.startswith(b"160000 ") for record in raw.split(b"\0") if record)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def session_source_identity(repositories: Iterable[dict[str, Any]], *, mode: str) -> str:
    if mode not in {"working", "committed"}:
        raise ContractError("session source identity mode is invalid")
    payload = []
    for repository in sorted(repositories, key=lambda item: item.get("repository_id", "")):
        patch_hash = repository.get("change_set", {}).get("patch_hash")
        checkpoint = repository.get("checkpoint")
        if (
            not isinstance(patch_hash, str)
            or len(patch_hash) != len("repository-patch:") + 64
            or not patch_hash.startswith("repository-patch:")
            or any(
                character not in "0123456789abcdef"
                for character in patch_hash.removeprefix("repository-patch:")
            )
        ):
            raise ContractError("repository patch identity is missing")
        if mode == "committed" and not isinstance(checkpoint, dict):
            raise ContractError("committed session source identity requires repository checkpoints")
        payload.append(
            {
                "base_commit": repository.get("base", {}).get("commit"),
                "checkpoint": checkpoint,
                "patch_hash": patch_hash,
                "repository_id": repository.get("repository_id"),
                "role": repository.get("role"),
            }
        )
    if not payload or len({item["repository_id"] for item in payload}) != len(payload):
        raise ContractError("session repositories must be non-empty with unique identities")
    return f"session-source:{sha256({'algorithm': 'session-source-v1', 'mode': mode, 'repositories': payload})}"


def refresh_session_source_identity(context: dict[str, Any]) -> dict[str, Any]:
    mode = context.get("source_identity", {}).get("mode", "working")
    refreshed = []
    for repository in context.get("repositories", []):
        record = inspect_repository(
                repository["worktree_path"],
                repository_id=repository["repository_id"],
                role=repository["role"],
                base_ref=repository["base"]["commit"],
                base_source=repository["base"]["source"],
                committed=mode == "committed",
            )
        # Resolve from the frozen Commit while preserving the human-auditable
        # ref that selected it when the Session was created.
        record["base"]["ref"] = repository["base"]["ref"]
        refreshed.append(record)
    context["repositories"] = sorted(refreshed, key=lambda item: item["repository_id"])
    context["source_identity"] = {
        "algorithm": "session-source-v1",
        "mode": mode,
        "value": session_source_identity(context["repositories"], mode=mode),
    }
    return context


def freeze_checkpoint(context: dict[str, Any]) -> dict[str, Any]:
    from copy import deepcopy

    if context.get("lifecycle", {}).get("state") != "active":
        raise ContractError("checkpoint requires an active worktree session")
    candidate = deepcopy(context)
    candidate["source_identity"]["mode"] = "committed"
    refresh_session_source_identity(candidate)
    candidate["verification"] = {"adapter_result_refs": [], "status": "pending"}
    candidate["review"] = {"adapter_result_refs": [], "status": "pending"}
    candidate["lifecycle"]["state"] = "checkpointed"
    context.clear()
    context.update(candidate)
    return context


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not SESSION_NAME.fullmatch(value) or value in {".", ".."}:
        raise ContractError(f"{label} is invalid")
