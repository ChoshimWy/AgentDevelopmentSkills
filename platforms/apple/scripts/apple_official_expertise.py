#!/usr/bin/env python3
"""Inspect a local Xcode skill export without copying or executing its content."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "Library/Developer/Xcode/CodingAssistant/codex/skills/__xcode"
DEFAULT_ROUTING = SCRIPT_ROOT / "config" / "apple-official-expertise-routing-v1.json"
MAX_FILE_COUNT = 5_000
MAX_FILE_SIZE = 4 * 1024 * 1024
MAX_TOTAL_SIZE = 64 * 1024 * 1024


class ExpertiseError(RuntimeError):
    """Raised when the local export cannot be trusted or normalized."""


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_routing(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ExpertiseError("routing map is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExpertiseError(f"routing map is invalid: {error}") from error
    if (
        value.get("schema_version") != "1.0"
        or value.get("redistribution") != "local-export-only"
        or not isinstance(value.get("expected_xcode_major"), int)
        or isinstance(value.get("expected_xcode_major"), bool)
        or value["expected_xcode_major"] < 1
        or not isinstance(value.get("skills"), dict)
        or not value["skills"]
    ):
        raise ExpertiseError("routing map contract is invalid")
    expected_major = value["expected_xcode_major"]
    for name, route in value["skills"].items():
        if not isinstance(name, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None:
            raise ExpertiseError("routing map contains an invalid skill name")
        if not isinstance(route, dict) or set(route) != {
            "activation", "capabilities", "local_routes", "tool_policy"
        }:
            raise ExpertiseError(f"routing map route is invalid: {name}")
        activation = route["activation"]
        if (
            not isinstance(activation, dict)
            or not set(activation) <= {"xcode_major", "sdk_major"}
            or "xcode_major" not in activation
            or activation.get("xcode_major") != expected_major
            or (
                "sdk_major" in activation
                and (not isinstance(activation["sdk_major"], int) or activation["sdk_major"] < 1)
            )
        ):
            raise ExpertiseError(f"routing map activation is invalid: {name}")
        capabilities = route["capabilities"]
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or len(capabilities) != len(set(capabilities))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", item) is None
                for item in capabilities
            )
        ):
            raise ExpertiseError(f"routing map capabilities are invalid: {name}")
        local_routes = route["local_routes"]
        if not isinstance(local_routes, list) or not local_routes:
            raise ExpertiseError(f"routing map local routes are invalid: {name}")
        normalized_routes: list[tuple[str, str]] = []
        for local_route in local_routes:
            if not isinstance(local_route, dict) or set(local_route) != {"mode", "skill"}:
                raise ExpertiseError(f"routing map local route is invalid: {name}")
            mode, skill = local_route["mode"], local_route["skill"]
            if (
                not isinstance(mode, str)
                or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", mode) is None
                or not isinstance(skill, str)
                or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill) is None
            ):
                raise ExpertiseError(f"routing map local route is invalid: {name}")
            normalized_routes.append((mode, skill))
        if len(normalized_routes) != len(set(normalized_routes)):
            raise ExpertiseError(f"routing map local routes are duplicated: {name}")
        if route["tool_policy"] not in {"guidance-only", "semantic-only", "translate-to-local"}:
            raise ExpertiseError(f"routing map tool policy is invalid: {name}")
    return value


def _safe_file_records(root: Path) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ExpertiseError("Xcode skill export is missing or unsafe")
    records: list[dict[str, Any]] = []
    total_size = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ExpertiseError(f"Xcode skill export contains a symlink: {relative.as_posix()}")
        if path.is_dir() or path.name == ".DS_Store":
            continue
        if not path.is_file():
            raise ExpertiseError(f"Xcode skill export contains an unsupported entry: {relative.as_posix()}")
        size = path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise ExpertiseError(f"Xcode skill export file is too large: {relative.as_posix()}")
        total_size += size
        if total_size > MAX_TOTAL_SIZE:
            raise ExpertiseError("Xcode skill export exceeds the total size limit")
        records.append({
            "path": relative.as_posix(),
            "sha256": file_sha256(path),
            "size": size,
        })
        if len(records) > MAX_FILE_COUNT:
            raise ExpertiseError("Xcode skill export exceeds the file count limit")
    if not records:
        raise ExpertiseError("Xcode skill export contains no files")
    return records


def _frontmatter_name(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ExpertiseError(f"cannot read exported SKILL.md: {path.name}: {error}") from error
    if not text.startswith("---\n"):
        raise ExpertiseError(f"exported SKILL.md has no frontmatter: {path.parent.name}")
    end = text.find("\n---", 4)
    if end < 0:
        raise ExpertiseError(f"exported SKILL.md frontmatter is unterminated: {path.parent.name}")
    match = re.search(r"(?m)^name:\s*[\"']?([a-z0-9]+(?:-[a-z0-9]+)*)[\"']?\s*$", text[4:end])
    if match is None:
        raise ExpertiseError(f"exported SKILL.md name is invalid: {path.parent.name}")
    return match.group(1)


def _major(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(
        r"\s*(\d+)(?:\.\d+){0,2}(?:\s*(?:beta|rc)\s*\d*)?\s*",
        value,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def build_packet(
    source_dir: Path,
    *,
    routing_path: Path = DEFAULT_ROUTING,
    xcode_version: str | None = None,
    xcode_build: str | None = None,
    sdk_major: int | None = None,
    attest_xcode_export: bool = False,
    expected_source_sha256: str | None = None,
    expected_routing_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a metadata-only federation packet for an existing local export."""
    routing = load_routing(routing_path)
    records = _safe_file_records(source_dir)
    normalized_source_path = source_dir.expanduser().absolute()
    managed_source_path = DEFAULT_SOURCE.expanduser().absolute()
    if normalized_source_path == managed_source_path:
        source_trust = "xcode-managed-path"
    elif attest_xcode_export:
        source_trust = "explicit-local-attestation"
    else:
        source_trust = "unverified"
    trusted_for_activation = source_trust != "unverified"
    record_by_skill: dict[str, list[dict[str, Any]]] = {}
    discovered: dict[str, Path] = {}
    for child in sorted(source_dir.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            raise ExpertiseError(f"Xcode skill export contains a symlink: {child.name}")
        skill_path = child / "SKILL.md"
        if not child.is_dir() or not skill_path.is_file():
            continue
        declared_name = _frontmatter_name(skill_path)
        if declared_name != child.name:
            raise ExpertiseError(f"exported Skill folder/name mismatch: {child.name} != {declared_name}")
        discovered[declared_name] = child
        prefix = f"{child.name}/"
        record_by_skill[child.name] = [record for record in records if record["path"].startswith(prefix)]

    known = routing["skills"]
    found_known = sorted(set(discovered) & set(known))
    if not found_known:
        raise ExpertiseError("Xcode skill export contains no mapped Apple expertise")

    if xcode_version is not None and not isinstance(xcode_version, str):
        raise ExpertiseError("Xcode version is invalid")
    xcode_major = _major(xcode_version)
    normalized_build = xcode_build.strip() if isinstance(xcode_build, str) and xcode_build.strip() else None
    if xcode_version is not None and xcode_major is None:
        raise ExpertiseError("Xcode version is invalid")
    if xcode_build is not None and (
        normalized_build is None
        or re.fullmatch(r"[0-9]{2,3}[A-Z][0-9A-Za-z]+", normalized_build) is None
    ):
        raise ExpertiseError("Xcode build is invalid")
    if sdk_major is not None and (
        not isinstance(sdk_major, int) or isinstance(sdk_major, bool) or sdk_major < 1
    ):
        raise ExpertiseError("SDK major is invalid")
    skills: list[dict[str, Any]] = []
    active_capabilities: set[str] = set()
    for name in found_known:
        route = known[name]
        requirements = route["activation"]
        reasons: list[str] = []
        if not trusted_for_activation:
            reasons.append("unverified-xcode-export-source")
        required_xcode = requirements.get("xcode_major")
        required_sdk = requirements.get("sdk_major")
        if xcode_major is None or normalized_build is None:
            reasons.append("missing-xcode-source-identity")
        elif xcode_major != required_xcode:
            reasons.append(f"requires-xcode-major-{required_xcode}")
        if required_sdk is not None and sdk_major != required_sdk:
            reasons.append(f"requires-sdk-major-{required_sdk}")
        eligible = not reasons
        capabilities = sorted(set(route["capabilities"]))
        if eligible:
            active_capabilities.update(capabilities)
        skill_records = record_by_skill[name]
        skills.append({
            "activation": {"eligible": eligible, "reasons": sorted(set(reasons))},
            "capabilities": capabilities,
            "content_sha256": canonical_sha256(skill_records),
            "file_count": len(skill_records),
            "local_routes": route["local_routes"],
            "name": name,
            "path": discovered[name].relative_to(source_dir).as_posix(),
            "tool_policy": route["tool_policy"],
        })

    unknown = sorted(set(discovered) - set(known))
    if unknown:
        # Unknown exported skills are a contract change. Preserve their names
        # for review, but do not activate even the known subset implicitly.
        status = "partial"
        active_capabilities = set()
        next_action = "update-routing-map"
    elif active_capabilities:
        status = "ready"
        next_action = "route-existing-entry"
    else:
        status = "partial"
        next_action = "provide-xcode-identity-or-sdk"
    packet = {
        "capabilities": sorted(active_capabilities),
        "missing_known_skills": sorted(set(known) - set(discovered)),
        "next_action": next_action,
        "routing_sha256": file_sha256(routing_path),
        "schema_version": "1.0",
        "skills": skills,
        "source": {
            "content_sha256": canonical_sha256(records),
            "file_count": len(records),
            "kind": "local-skill-export",
            "path": str(normalized_source_path),
            "redistribution": routing["redistribution"],
            "sdk_major": sdk_major,
            "xcode_build": normalized_build,
            "xcode_version": xcode_version,
            "trust": {
                "status": source_trust,
                "trusted_for_activation": trusted_for_activation,
            },
        },
        "status": status,
        "unknown_skills": unknown,
    }
    if expected_source_sha256 is not None and packet["source"]["content_sha256"] != expected_source_sha256:
        raise ExpertiseError("Xcode skill export content hash differs from the frozen source")
    if expected_routing_sha256 is not None and packet["routing_sha256"] != expected_routing_sha256:
        raise ExpertiseError("routing map hash differs from the frozen source")
    return packet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect an existing local Xcode skill export and emit a metadata-only federation packet."
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--xcode-version")
    parser.add_argument("--xcode-build")
    parser.add_argument("--sdk-major", type=int)
    parser.add_argument(
        "--attest-xcode-export",
        action="store_true",
        help="Explicitly attest that a non-default source directory was exported by the selected Xcode.",
    )
    parser.add_argument("--expect-source-sha256")
    parser.add_argument("--expect-routing-sha256")
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args(argv)
    try:
        packet = build_packet(
            args.source_dir.expanduser(),
            routing_path=args.routing.expanduser(),
            xcode_version=args.xcode_version,
            xcode_build=args.xcode_build,
            sdk_major=args.sdk_major,
            attest_xcode_export=args.attest_xcode_export,
            expected_source_sha256=args.expect_source_sha256,
            expected_routing_sha256=args.expect_routing_sha256,
        )
    except (ExpertiseError, OSError, ValueError) as error:
        print(canonical_dumps({"error": str(error), "status": "blocked"}), end="", file=sys.stderr)
        return 2
    print(canonical_dumps(packet), end="")
    return 2 if args.require_ready and packet["status"] != "ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
