#!/usr/bin/env python3
"""Validate GitHub publication metadata before creating immutable release state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


MAX_GITHUB_RESPONSE_BYTES = 2 * 1024 * 1024
QUALIFICATION_WORKFLOW_PATH = ".github/workflows/conformance.yml"
PUBLISH_BRANCH = "main"


class PublicationError(RuntimeError):
    """Raised when a GitHub publication precondition is not satisfied."""


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise PublicationError(f"GitHub response is missing or unsafe: {path}")
    declared_size = path.stat().st_size
    if declared_size > MAX_GITHUB_RESPONSE_BYTES:
        raise PublicationError("GitHub response exceeds the size limit")
    value = path.read_bytes()
    if len(value) != declared_size:
        raise PublicationError("GitHub response changed while being read")
    try:
        return json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"GitHub response is not valid JSON: {error}") from error


def _repository_name(value: Any, *, label: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("full_name"), str):
        raise PublicationError(f"qualification run {label} is missing")
    return value["full_name"]


def validate_publication_request(
    run: Any,
    main_branch: Any,
    *,
    repository: str,
    source_revision: str,
    workflow_revision: str,
) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise PublicationError("source revision must be a full lowercase Git commit")
    if not isinstance(repository, str) or re.fullmatch(r"[^/\s]+/[^/\s]+", repository) is None:
        raise PublicationError("repository must use the owner/name form")
    if workflow_revision != source_revision:
        raise PublicationError("publication request revision differs from the workflow revision")
    if not isinstance(run, dict):
        raise PublicationError("qualification run response must be an object")
    expected = {
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": PUBLISH_BRANCH,
        "head_sha": source_revision,
        "path": QUALIFICATION_WORKFLOW_PATH,
        "status": "completed",
    }
    for key, value in expected.items():
        if run.get(key) != value:
            raise PublicationError(
                f"qualification run {key} must be {value!r}, got {run.get(key)!r}"
            )
    if _repository_name(run.get("repository"), label="repository") != repository:
        raise PublicationError("qualification run belongs to a different repository")
    if _repository_name(run.get("head_repository"), label="head_repository") != repository:
        raise PublicationError("qualification run head belongs to a different repository")
    if (
        not isinstance(main_branch, dict)
        or main_branch.get("name") != PUBLISH_BRANCH
        or main_branch.get("protected") is not True
    ):
        raise PublicationError("GitHub main branch is missing or not protected")
    branch_commit = main_branch.get("commit")
    if not isinstance(branch_commit, dict) or branch_commit.get("sha") != source_revision:
        raise PublicationError("source revision is not the current protected main revision")


def validate_tag_absent(refs: Any, *, tag: str) -> None:
    if (
        not isinstance(tag, str)
        or re.fullmatch(
            r"v[0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
            tag,
        )
        is None
    ):
        raise PublicationError("release tag is unsafe")
    if not isinstance(refs, list):
        raise PublicationError("GitHub matching tag refs response must be an array")
    expected = f"refs/tags/{tag}"
    for item in refs:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            raise PublicationError("GitHub matching tag refs response contains an invalid entry")
        if item["ref"] == expected:
            raise PublicationError(f"release tag already exists and will not be replaced: {tag}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    request = subparsers.add_parser("request")
    request.add_argument("--run-json", type=Path, required=True)
    request.add_argument("--main-branch-json", type=Path, required=True)
    request.add_argument("--repository", required=True)
    request.add_argument("--source-revision", required=True)
    request.add_argument("--workflow-revision", required=True)

    tag = subparsers.add_parser("tag-absent")
    tag.add_argument("--refs-json", type=Path, required=True)
    tag.add_argument("--tag", required=True)

    args = parser.parse_args()
    try:
        if args.command == "request":
            validate_publication_request(
                _read_json(args.run_json),
                _read_json(args.main_branch_json),
                repository=args.repository,
                source_revision=args.source_revision,
                workflow_revision=args.workflow_revision,
            )
        else:
            validate_tag_absent(_read_json(args.refs_json), tag=args.tag)
    except (OSError, PublicationError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
