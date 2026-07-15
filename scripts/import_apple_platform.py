#!/usr/bin/env python3
"""一次性导入 iOSAgentSkills 受控资产，并冻结可审计来源清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


ALLOWED_ROOTS = ("config", "daemon", "scripts", "skills", "tools")
INVENTORY_NAME = "migration-source.json"


def _git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _tracked_files(source: Path) -> list[Path]:
    tracked = _git(source, "ls-files", "-z").split("\0")
    roots = set(ALLOWED_ROOTS)
    return sorted(
        Path(item)
        for item in tracked
        if item and Path(item).parts and Path(item).parts[0] in roots
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def build_inventory(source: Path) -> dict[str, object]:
    files = []
    for relative in _tracked_files(source):
        path = source / relative
        if not path.is_file():
            raise ValueError(f"tracked Apple asset is not a regular file: {relative.as_posix()}")
        files.append(
            {
                "mode": _canonical_mode(path),
                "path": relative.as_posix(),
                "sha256": _file_digest(path),
            }
        )
    content_digest = hashlib.sha256(_canonical_bytes(files)).hexdigest()
    return {
        "allowed_roots": list(ALLOWED_ROOTS),
        "files": files,
        "schema_version": "1.0",
        "source_content_sha256": content_digest,
        "source_head": _git(source, "rev-parse", "HEAD").strip(),
        "source_repository": "iOSAgentSkills",
    }


def import_assets(
    source: Path,
    destination: Path,
    *,
    check: bool,
    replace: bool,
) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    inventory = build_inventory(source)
    expected_inventory = _canonical_bytes(inventory)
    inventory_path = destination / INVENTORY_NAME

    if check:
        if not inventory_path.is_file() or inventory_path.read_bytes() != expected_inventory:
            raise ValueError("frozen Apple migration source inventory differs from the provided source")
        return inventory

    if inventory_path.exists() and not replace:
        raise ValueError(
            "Apple assets are already imported; the monorepo package is now the source of truth "
            "(use --replace only for an explicitly approved re-import)"
        )
    destination.mkdir(parents=True, exist_ok=True)
    for entry in inventory["files"]:
        relative = Path(entry["path"])
        source_path = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
    inventory_path.write_bytes(expected_inventory)
    return inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "platforms" / "apple",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check and args.replace:
            raise ValueError("--check cannot be combined with --replace")
        inventory = import_assets(
            args.source,
            args.destination,
            check=args.check,
            replace=args.replace,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(_canonical_bytes({
        "file_count": len(inventory["files"]),
        "source_content_sha256": inventory["source_content_sha256"],
        "status": "passed" if args.check else "synchronized",
    }).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
