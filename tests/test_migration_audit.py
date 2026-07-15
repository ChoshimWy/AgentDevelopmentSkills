from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest import mock

from tests.support import ROOT

from agent_workflow.canonical_json import dump, sha256
from agent_workflow.models import ContractError
from scripts.build_migration_audit import build_documents
import scripts.build_migration_audit as migration_builder
from scripts.validate_apple_package import validate_migration_audit
from agent_workflow.canonical_json import load


def refresh_content_digest(document: dict[str, object]) -> None:
    document["content_sha256"] = sha256({
        key: value for key, value in document.items() if key != "content_sha256"
    })


class MigrationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load(ROOT / "platforms" / "apple" / "migration-source.json")
        cls.audit, cls.inventory = build_documents()

    def test_generated_v2_audit_matches_current_package_tree(self) -> None:
        validate_migration_audit(ROOT, self.source, self.audit, self.inventory)
        dispositions = [entry["disposition"] for entry in self.audit["entries"]]
        self.assertEqual(dispositions.count("retained"), 206)
        self.assertEqual(dispositions.count("relocated"), 59)
        self.assertEqual(dispositions.count("transformed"), 22)
        self.assertEqual(dispositions.count("removed"), 1)
        self.assertEqual(
            [(item["id"], len(item["files"])) for item in self.inventory["packages"]],
            [
                ("apple", 222),
                ("codex", 9),
                ("design", 33),
                ("documentation", 8),
                ("git", 7),
                ("review", 4),
                ("workflow", 13),
            ],
        )
        self.assertEqual(self.audit["source"]["license"]["status"], "pending")
        apple_inventory = next(item for item in self.inventory["packages"] if item["id"] == "apple")
        self.assertEqual(len(apple_inventory["capabilities"]), 21)
        self.assertEqual(
            apple_inventory["permissions"]["capability_profiles"]["implementation.apple"],
            "project-write",
        )

    def test_every_source_path_must_be_mapped_once(self) -> None:
        audit = deepcopy(self.audit)
        audit["entries"].pop()
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "every source path exactly once"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

    def test_duplicate_target_and_unknown_package_fail_closed(self) -> None:
        audit = deepcopy(self.audit)
        audit["entries"][1]["targets"] = deepcopy(audit["entries"][0]["targets"])
        audit["entries"][1]["disposition"] = "transformed"
        audit["entries"][1]["reason"] = "test duplicate"
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "target is duplicated"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

        audit = deepcopy(self.audit)
        audit["entries"][0]["targets"][0]["package"] = "missing"
        audit["entries"][0]["disposition"] = "relocated"
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "package is unknown"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

    def test_transformed_and_removed_entries_require_reasons(self) -> None:
        audit = deepcopy(self.audit)
        transformed = next(item for item in audit["entries"] if item["disposition"] == "transformed")
        transformed["reason"] = None
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "transformed migration entry requires a reason"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

        audit = deepcopy(self.audit)
        audit["entries"][0]["disposition"] = "removed"
        audit["entries"][0]["targets"] = []
        audit["entries"][0]["reason"] = None
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "removed migration entry requires a reason"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

    def test_stale_package_inventory_fails_closed(self) -> None:
        inventory = deepcopy(self.inventory)
        inventory["packages"][0]["files"].pop()
        inventory["packages"][0]["content_sha256"] = sha256(inventory["packages"][0]["files"])
        refresh_content_digest(inventory)
        with self.assertRaisesRegex(ContractError, "current package inventory is stale"):
            validate_migration_audit(ROOT, self.source, self.audit, inventory)

    def test_package_inventory_contract_metadata_fails_closed(self) -> None:
        for field, value, message in (
            ("version", "999.0.0", "version differs"),
            ("capabilities", [], "capabilities differ"),
            ("permissions", {"detection": "unrestricted"}, "permissions differ"),
        ):
            with self.subTest(field=field):
                inventory = deepcopy(self.inventory)
                package = next(item for item in inventory["packages"] if item["id"] == "documentation")
                package[field] = value
                refresh_content_digest(inventory)
                with self.assertRaisesRegex(ContractError, message):
                    validate_migration_audit(ROOT, self.source, self.audit, inventory)

    def test_forged_repository_and_unproven_verified_license_fail_closed(self) -> None:
        audit = deepcopy(self.audit)
        audit["source"]["repository_url"] = "git@example.invalid:forged.git"
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "source provenance differs"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

        audit = deepcopy(self.audit)
        audit["source"]["license"]["status"] = "verified"
        refresh_content_digest(audit)
        with self.assertRaisesRegex(ContractError, "requires SPDX and notice digest"):
            validate_migration_audit(ROOT, self.source, audit, self.inventory)

    def test_relocated_source_can_leave_apple_when_target_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apple = root / "platforms" / "apple"
            documentation = root / "disciplines" / "documentation"
            apple.mkdir(parents=True)
            target = documentation / "skills" / "html-docs" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("# html-docs\n", encoding="utf-8")
            dump({
                "id": "apple",
                "capabilities": [],
                "installation": {"asset_roots": [], "instruction_fragments": [], "skill_roots": ["skills"]},
                "permissions": {"detection": "repository-read-only"},
                "version": "0.1.0",
            }, apple / "manifest.json")
            dump({
                "id": "documentation",
                "capabilities": [{"id": "documentation.html", "permission_profile": "project-write", "version": "1.0"}],
                "installation": {"asset_roots": [], "instruction_fragments": [], "skill_roots": ["skills"]},
                "permissions": {"detection": "repository-read-only"},
                "version": "0.1.0",
            }, documentation / "manifest.json")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            source = {
                "allowed_roots": ["skills"],
                "files": [{"mode": 0o644, "path": "skills/html-docs/SKILL.md", "sha256": digest}],
                "source_content_sha256": "a" * 64,
                "source_head": "b" * 40,
                "source_repository": "iOSAgentSkills",
                "source_repository_url": "git@example.invalid:iOSAgentSkills.git",
            }
            dump(source, apple / "migration-source.json")
            dump({"additions": [], "overrides": [], "schema_version": "1.0"}, apple / "migration-overrides.json")
            audit = {
                "additions": [],
                "entries": [{
                    "disposition": "relocated",
                    "reason": "Extract generic documentation discipline.",
                    "source_path": "skills/html-docs/SKILL.md",
                    "targets": [{
                        "mode": 0o644,
                        "package": "documentation",
                        "path": "skills/html-docs/SKILL.md",
                        "sha256": digest,
                    }],
                }],
                "schema_version": "2.0",
                "source": {
                    "commit": "b" * 40,
                    "content_sha256": "a" * 64,
                    "inventory": "platforms/apple/migration-source.json",
                    "license": {"notice_path": None, "notice_sha256": None, "spdx": None, "status": "pending"},
                    "repository": "iOSAgentSkills",
                    "repository_url": "git@example.invalid:iOSAgentSkills.git",
                },
            }
            refresh_content_digest(audit)
            with mock.patch.object(migration_builder, "ROOT", root):
                generated_audit, inventory = migration_builder.build_documents(audit)
            self.assertEqual(generated_audit["entries"][0]["targets"][0]["package"], "documentation")
            self.assertEqual([item["id"] for item in inventory["packages"]], ["documentation"])
            validate_migration_audit(root, source, generated_audit, inventory)


if __name__ == "__main__":
    unittest.main()
