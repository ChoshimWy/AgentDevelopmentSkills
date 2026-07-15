from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_skill_naming", ROOT / "scripts" / "validate_skill_naming.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SkillNamingTests(unittest.TestCase):
    def test_repository_policy_passes(self) -> None:
        self.assertEqual(
            MODULE.validate_repository(ROOT, ROOT / "skill-naming-policy.json"),
            [],
        )

    def test_policy_instance_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy_path = self._write_fixture(root)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["unexpected"] = True
            self._write_json(policy_path, policy)
            failures = MODULE.validate_repository(root, policy_path)
            self.assertTrue(any("policy must contain exactly" in item for item in failures))

    def test_runtime_prefix_is_rejected_for_new_platform_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = self._write_fixture(root, include_runtime_skill=True)
            failures = MODULE.validate_repository(root, policy)
            self.assertTrue(any("runtime/vendor-prefixed Skill is forbidden" in item for item in failures))

    def test_runtime_prefix_is_rejected_for_shared_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = self._write_fixture(root, include_shared_runtime_skill=True)
            failures = MODULE.validate_repository(root, policy)
            self.assertTrue(any("workflow: runtime/vendor-prefixed Skill is forbidden" in item for item in failures))

    def test_bootstrap_platform_cannot_hide_phantom_skill_without_declaring_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = self._write_fixture(root, platform_status="bootstrap-only")
            failures = MODULE.validate_repository(root, policy)
            self.assertTrue(any("bootstrap-only platform must not ship Skills" in item for item in failures))

    def test_deprecated_skill_cannot_disappear_before_removal_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = self._write_fixture(root, include_deprecated=False)
            failures = MODULE.validate_repository(root, policy)
            self.assertTrue(any("must remain until 0.3.0" in item for item in failures))

    def test_deprecated_skill_must_be_removed_at_removal_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = self._write_fixture(root, package_version="0.3.0")
            failures = MODULE.validate_repository(root, policy)
            self.assertTrue(any("must be removed in 0.3.0" in item for item in failures))

    def test_deprecated_skill_cannot_remain_provider_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = self._write_fixture(root, bind_deprecated=True)
            failures = MODULE.validate_repository(root, policy)
            self.assertTrue(any("must bind canonical orchestration" in item for item in failures))
            self.assertTrue(any("deprecated Skill must not be a Provider binding" in item for item in failures))

    def _write_fixture(
        self,
        root: Path,
        *,
        bind_deprecated: bool = False,
        include_deprecated: bool = True,
        include_runtime_skill: bool = False,
        include_shared_runtime_skill: bool = False,
        package_version: str = "0.2.0",
        platform_status: str = "implemented",
    ) -> Path:
        workflow = root / "disciplines" / "workflow"
        apple = root / "platforms" / "apple"
        self._write_json(
            workflow / "manifest.json",
            {"id": "workflow", "kind": "discipline", "installation": {"skill_roots": ["skills"]}},
        )
        self._write_skill(workflow / "skills" / "workflow-orchestration", "workflow-orchestration")
        if include_shared_runtime_skill:
            self._write_skill(workflow / "skills" / "codex-helper", "codex-helper")
        apple_installation = (
            {"skill_roots": ["skills"], "provider_manifest": "provider/manifest.json"}
            if platform_status == "implemented"
            else None
        )
        apple_manifest = {
            "id": "apple",
            "kind": "platform",
            "version": package_version,
            "implementation_status": platform_status,
        }
        if apple_installation is not None:
            apple_manifest["installation"] = apple_installation
        self._write_json(
            apple / "manifest.json",
            apple_manifest,
        )
        self._write_skill(apple / "skills" / "apple-orchestration", "apple-orchestration")
        if include_deprecated:
            deprecated = apple / "skills" / "codex-subagent-orchestration"
            self._write_skill(deprecated, "codex-subagent-orchestration")
            (deprecated / "agents").mkdir(parents=True)
            (deprecated / "agents" / "openai.yaml").write_text(
                "policy:\n  allow_implicit_invocation: false\n", encoding="utf-8"
            )
        if include_runtime_skill:
            self._write_skill(apple / "skills" / "codex-new-flow", "codex-new-flow")
        if platform_status == "implemented":
            self._write_json(
                apple / "provider" / "manifest.json",
                {
                    "bindings": {
                        "analysis.apple": {
                            "kind": "skill",
                            "name": "codex-subagent-orchestration" if bind_deprecated else "apple-orchestration",
                        }
                    }
                },
            )
        policy = root / "skill-naming-policy.json"
        self._write_json(
            policy,
            {
                "schema_version": "1.0",
                "shared_orchestration": "workflow-orchestration",
                "global": {
                    "name_pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "max_length": 64,
                    "folder_must_match_name": True,
                    "forbidden_runtime_prefixes": ["claude-", "codex-", "gemini-", "openai-"],
                },
                "platforms": {
                    "apple": {
                        "canonical_orchestration": "apple-orchestration",
                        "provider_binding": "analysis.apple",
                        "allowed_prefixes": ["apple-"],
                        "grandfathered": [],
                        "deprecated": [] if platform_status == "bootstrap-only" else [
                            {
                                "name": "codex-subagent-orchestration",
                                "replacement": "apple-orchestration",
                                "remove_in": "0.3.0",
                            }
                        ],
                    }
                },
            },
        )
        return policy

    @staticmethod
    def _write_skill(root: Path, name: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n", encoding="utf-8"
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
