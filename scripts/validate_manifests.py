#!/usr/bin/env python3
"""Validate built-in Manifest contracts and capability uniqueness."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.models import ContractError  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402


def main() -> int:
    try:
        provider_roots = [ROOT / "providers"] if (ROOT / "providers").is_dir() else []
        registry = ManifestRegistry.from_directory(ROOT / "platforms", provider_roots=provider_roots)
    except ContractError as error:
        print(f"FAIL {error}")
        return 1
    schema_names = {
        path.name.removesuffix(".schema.json")
        for path in [
            *(ROOT / "schemas").glob("*.schema.json"),
            *(ROOT / "disciplines").glob("*/contracts/*.schema.json"),
            *(ROOT / "platforms").glob("*/contracts/*.schema.json"),
        ]
    }
    for registered in registry.manifests:
        for entry in registered.value["capabilities"]:
            contract = registry.capability_contract(entry["id"])
            for field in ("input_schema", "output_schema"):
                if contract[field] not in schema_names:
                    print(f"FAIL {entry['id']} references missing {field}: {contract[field]}")
                    return 1
    print(f"PASS {len(registry.manifests)} manifests; registry={registry.digest()[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
