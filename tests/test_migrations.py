from __future__ import annotations

import unittest

from agent_workflow.contracts import validate_activation_lock, validate_migration_report
from agent_workflow.migrations import (
    MigrationEdge,
    MigrationRegistry,
    migrate_activation_lock,
)
from agent_workflow.models import ContractError


class MigrationRegistryTests(unittest.TestCase):
    def test_identity_path_is_empty_and_unknown_path_fails_closed(self) -> None:
        registry = MigrationRegistry()
        self.assertEqual(registry.path("agent-skills-lock", "1.0", "1.0"), ())
        with self.assertRaisesRegex(ContractError, "no supported migration path"):
            registry.path("agent-skills-lock", "1.0", "2.0")

    def test_deterministic_multi_step_path_and_losslessness_are_preserved(self) -> None:
        registry = MigrationRegistry((
            MigrationEdge("artifact", "2.0", "3.0", True),
            MigrationEdge("artifact", "1.0", "2.0", False),
        ))
        path = registry.path("artifact", "1.0", "3.0")
        self.assertEqual([(item.from_version, item.to_version) for item in path], [("1.0", "2.0"), ("2.0", "3.0")])
        self.assertEqual([item.lossless for item in path], [False, True])

    def test_duplicate_and_identity_edges_are_rejected(self) -> None:
        edge = MigrationEdge("artifact", "1.0", "2.0", True)
        with self.assertRaisesRegex(ContractError, "duplicate"):
            MigrationRegistry((edge, edge))
        with self.assertRaisesRegex(ContractError, "identity"):
            MigrationRegistry((MigrationEdge("artifact", "1.0", "1.0", True),))
        with self.assertRaisesRegex(ContractError, "boolean"):
            MigrationRegistry((MigrationEdge("artifact", "1.0", "2.0", "yes"),))  # type: ignore[arg-type]

    def test_activation_lock_v1_to_v2_is_deterministic_lossless_and_validated(self) -> None:
        legacy = {
            "files": [{"mode": 0o644, "path": "agents/reviewer.toml", "sha256": "1" * 64}],
            "manager": "agent-development-skills",
            "schema_version": "1.0",
        }
        first, report = migrate_activation_lock(legacy)
        second, second_report = migrate_activation_lock(legacy)
        self.assertEqual(first, second)
        self.assertEqual(report, second_report)
        self.assertEqual(legacy["schema_version"], "1.0")
        self.assertEqual(first["schema_version"], "2.0")
        self.assertEqual(first["handler"], "core.source-activation.apple-codex-v1")
        self.assertTrue(report["lossless"])
        self.assertEqual(report["status"], "planned")
        self.assertEqual(
            report["steps"][0]["changes"],
            ["add:/handler", "replace:/schema_version"],
        )
        validate_activation_lock(first)
        validate_migration_report(report)

    def test_migration_validates_before_and_after_and_requires_a_transform(self) -> None:
        invalid = {
            "files": [{"mode": 0o644, "path": "../escape", "sha256": "1" * 64}],
            "manager": "agent-development-skills",
            "schema_version": "1.0",
        }
        with self.assertRaisesRegex(ContractError, "path is invalid"):
            migrate_activation_lock(invalid)
        registry = MigrationRegistry((MigrationEdge("artifact", "1.0", "2.0", True),))
        with self.assertRaisesRegex(ContractError, "transform is unavailable"):
            registry.migrate(
                "artifact",
                {"schema_version": "1.0"},
                "2.0",
                validator=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
