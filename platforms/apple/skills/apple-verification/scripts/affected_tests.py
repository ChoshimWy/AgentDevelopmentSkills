#!/usr/bin/env python3
"""Generate deterministic affected-test candidates from changed paths."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SUFFIXES = ("ViewModel", "Service", "Repository", "UseCase", "Manager")


def affected_tests(changed_files: list[str], impact_map: dict[str, list[str]] | None = None) -> dict[str, Any]:
    impact_map = impact_map or {}
    selectors: set[str] = set()
    reasons: list[str] = []
    for changed in sorted(set(changed_files)):
        mapped = impact_map.get(changed, [])
        selectors.update(mapped)
        if mapped:
            reasons.append(f"project impact map: {changed}")
            continue
        stem = Path(changed).stem
        if stem.endswith("Tests") or stem.endswith("UITests"):
            selectors.add(stem)
            reasons.append(f"changed test file: {changed}")
            continue
        for suffix in SUFFIXES:
            if stem.endswith(suffix):
                selectors.add(f"{stem}Tests")
                reasons.append(f"basename heuristic: {changed}")
                break
        lower = changed.lower()
        domains = (
            (("storekit", "subscription", "purchase", "receipt", "entitlement"), ("PurchaseTests", "ReceiptTests", "EntitlementTests")),
            (("coredata", "wcdb", "database", "persistence"), ("PersistenceTests",)),
            (("ble", "mesh", "provision"), ("ProtocolParserTests", "StateMachineTests")),
        )
        for tokens, tests in domains:
            if any(token in lower for token in tokens):
                selectors.update(tests)
                reasons.append(f"domain heuristic: {changed}")
    result: dict[str, Any] = {"selectors": sorted(selectors), "reasons": sorted(set(reasons))}
    if not selectors:
        result["no_test_reason"] = "No deterministic low-cost XCTest mapping was found for the changed files."
        result["suggested_validation"] = ["affected target compile", "add or configure a project impact map"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("changed_files", nargs="+")
    parser.add_argument("--impact-map", type=Path)
    args = parser.parse_args()
    mapping: dict[str, list[str]] = {}
    if args.impact_map:
        raw = json.loads(args.impact_map.read_text(encoding="utf-8"))
        sources = raw.get("sources", raw) if isinstance(raw, dict) else None
        if not isinstance(sources, dict) or any(not isinstance(value, list) for value in sources.values()):
            raise SystemExit("impact map must be an object of path -> selector array")
        mapping = sources
    print(json.dumps(affected_tests(args.changed_files, mapping), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
