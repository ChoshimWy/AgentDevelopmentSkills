from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.support import MANIFESTS

from agent_workflow.canonical_json import dump, load, sha256
import agent_workflow.installation as installation_module
from agent_workflow.contracts import validate
from agent_workflow.installation import (
    MANAGED_HEADER,
    build_install_bundle,
    install_bundle,
    resolve_platform_selection,
)
from agent_workflow.models import ContractError


class InstallationTests(unittest.TestCase):
    def write_installable_package(
        self,
        root: Path,
        package_id: str,
        *,
        scope: str,
        skill: str | None,
        kind: str | None = None,
        package_requires: list[dict[str, object]] | None = None,
    ) -> None:
        package = root / package_id
        (package / "agent-instructions").mkdir(parents=True)
        (package / "agent-instructions" / "global.md").write_text(
            f"## {package_id} instructions\n", encoding="utf-8"
        )
        skill_roots = []
        if skill is not None:
            (package / "skills" / skill).mkdir(parents=True)
            (package / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            skill_roots = ["skills"]
        asset_roots = []
        if skill is None:
            (package / "tools").mkdir()
            (package / "tools" / "fixture").write_text("fixture\n", encoding="utf-8")
            asset_roots = ["tools"]
        binding = skill if skill is not None else {"kind": "tool", "name": "fixture"}
        dump({
            "bindings": {f"fixture.{package_id}": binding},
            "capabilities": [{"id": f"fixture.{package_id}", "version": "1.0"}],
            "conflicts": [],
            "detection": {"medium": [], "strong": [], "weak": []},
            "id": package_id,
            "installation": {
                "asset_roots": asset_roots,
                "instruction_fragments": [{
                    "id": f"{package_id}.global",
                    "merge_strategy": "locked",
                    "order": 0 if package_id == "core" else 100,
                    "path": "agent-instructions/global.md",
                    "scope": scope,
                }],
                "skill_roots": skill_roots,
            },
            "kind": kind or ("adapter" if package_id == "core" else "platform"),
            "optional_requires": [],
            "package_requires": package_requires or [],
            "permissions": {"detection": "repository-read-only"},
            "requires": [],
            "schema_version": "1.0",
            "targets": [] if package_id == "core" else [package_id],
            "version": "1.0.0",
        }, package / "manifest.json")

    def test_core_only_excludes_all_apple_assets_and_permissions(self) -> None:
        bundle = build_install_bundle(MANIFESTS, core_only=True)
        self.assertEqual(bundle.plan["selected_platforms"], [])
        self.assertEqual(bundle.plan["skills"], [])
        self.assertEqual(bundle.plan["permission_profiles"], ["repository-read-only"])
        self.assertNotIn("platform.apple.global", bundle.instructions)
        self.assertNotIn("implementation.apple", bundle.plan["bindings"])
        common_rule_ids = (
            "core.default-language",
            "core.temporal-fact-verification",
            "core.skill-route-announcement",
            "core.nearest-source-of-truth",
            "core.doc-rule-completion",
        )
        for rule_id in common_rule_ids:
            self.assertEqual(bundle.instructions.count(f"rule:{rule_id}"), 1)
            matches = [
                item
                for item in bundle.plan["instructions"]["rule_trace"]
                if item["id"] == rule_id
            ]
            self.assertEqual(len(matches), 1)
            self.assertEqual(
                {
                    key: matches[0][key]
                    for key in ("decision", "effect", "locked", "package", "scope")
                },
                {
                    "decision": "accepted",
                    "effect": "allow",
                    "locked": True,
                    "package": "core",
                    "scope": "global",
                },
            )
        self.assertNotIn("Skill Schema v1", bundle.instructions)
        self.assertNotIn("lint_skill_schema.py", bundle.instructions)
        validate("install-plan", bundle.plan)

    def test_apple_bundle_is_deterministic_and_uses_one_agents_document(self) -> None:
        first = build_install_bundle(MANIFESTS, platforms=["apple"])
        second = build_install_bundle(MANIFESTS, platforms=["apple"])
        self.assertEqual(first.plan, second.plan)
        self.assertEqual(first.instructions, second.instructions)
        self.assertEqual(first.instructions.count(MANAGED_HEADER), 1)
        self.assertIn("fragment:core.global", first.instructions)
        self.assertIn("fragment:platform.apple.global", first.instructions)
        self.assertIn("apple-verification", {item["name"] for item in first.plan["skills"]})
        self.assertEqual(
            first.plan["bindings"]["implementation.apple"]["binding"]["name"],
            "ios-feature-implementation",
        )
        self.assertEqual(
            [item["id"] for item in first.plan["selected_packages"]],
            ["core", "design", "documentation", "git", "review", "workflow", "apple"],
        )
        skill_packages = {item["name"]: item["package"] for item in first.plan["skills"]}
        self.assertEqual(skill_packages["html-docs"], "documentation")
        self.assertEqual(skill_packages["git-workflow"], "git")
        self.assertEqual(skill_packages["gh-pr-flow"], "git")
        self.assertEqual(skill_packages["code-review"], "review")
        self.assertEqual(skill_packages["workflow-orchestration"], "workflow")
        self.assertEqual(skill_packages["apple-code-review"], "apple")
        self.assertEqual(skill_packages["ui-ux-design-system"], "design")
        self.assertEqual(skill_packages["design-ir-compiler"], "design")
        self.assertEqual(skill_packages["apple-design-source"], "apple")
        self.assertEqual(skill_packages["apple-design-context-compiler"], "apple")
        self.assertEqual(len(skill_packages), len(first.plan["skills"]))
        self.assertNotIn("report.apple.html", first.plan["bindings"])
        self.assertEqual(first.plan["bindings"]["documentation.html"]["package"], "documentation")
        self.assertEqual(first.plan["bindings"]["git.workflow"]["package"], "git")
        self.assertEqual(first.plan["bindings"]["review.independent"]["package"], "review")
        self.assertEqual(first.plan["bindings"]["review.apple.static"]["package"], "apple")
        self.assertEqual(first.plan["bindings"]["reporting.delivery"]["package"], "workflow")
        self.assertEqual(first.plan["bindings"]["design.system"]["package"], "design")
        self.assertEqual(first.plan["bindings"]["design.ir.compile"]["package"], "design")
        self.assertEqual(first.plan["bindings"]["design.apple.binding"]["package"], "apple")
        self.assertEqual(first.plan["lock_schema_version"], "2.0")
        self.assertEqual(
            first.plan["asset_summary"]["content_sha256"], sha256(first.plan["assets"])
        )
        self.assertEqual(
            set(first.plan["capability_providers"]), set(first.plan["bindings"])
        )
        self.assertTrue(first.plan["instructions"]["rule_trace"])
        self.assertTrue(
            all("source_sha256" in item for item in first.plan["selected_packages"])
        )
        self.assertEqual(first.plan["selected_runtime_configs"], [])

    def test_install_and_profile_switch_are_managed_and_repeatable(self) -> None:
        apple = build_install_bundle(MANIFESTS, platforms=["apple"])
        core = build_install_bundle(MANIFESTS, core_only=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            first = install_bundle(apple, target)
            second = install_bundle(apple, target)
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertTrue((target / "skills" / "apple-verification" / "SKILL.md").is_file())
            self.assertTrue((target / "skills" / "code-review" / "SKILL.md").is_file())
            self.assertTrue((target / "skills" / "workflow-orchestration" / "SKILL.md").is_file())
            self.assertTrue((target / "skills" / "apple-code-review" / "SKILL.md").is_file())
            self.assertTrue((target / ".agent-skills" / "packages" / "apple" / "provider" / "manifest.json").is_file())
            self.assertFalse((target / ".agent-skills" / "packages" / "apple" / "skills" / "code-review").exists())
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), apple.instructions)
            self.assertEqual(load(target / ".agent-skills" / "install-lock.json")["status"], "installed")

            install_bundle(core, target)
            self.assertEqual(list((target / "skills").iterdir()), [])
            self.assertFalse((target / ".agent-skills" / "packages" / "apple").exists())
            self.assertNotIn("platform.apple.global", (target / "AGENTS.md").read_text(encoding="utf-8"))

    def test_runtime_config_requires_explicit_selection(self) -> None:
        apple = build_install_bundle(MANIFESTS, platforms=["apple"])
        self.assertNotIn("codex", [item["id"] for item in apple.plan["selected_packages"]])
        selected = build_install_bundle(MANIFESTS, runtime_configs=["codex"])
        self.assertEqual(selected.plan["selected_platforms"], [])
        self.assertEqual(selected.plan["selected_runtime_configs"], ["codex"])
        self.assertEqual(
            [item["id"] for item in selected.plan["selected_packages"]], ["core", "codex"]
        )
        self.assertEqual(
            selected.plan["bindings"]["runtime.codex.configure"]["package"], "codex"
        )
        self.assertTrue(
            any(item["path"].endswith("codex.shared.toml") for item in selected.plan["assets"])
        )

    def test_unmanaged_roots_are_never_overwritten(self) -> None:
        bundle = build_install_bundle(MANIFESTS, platforms=["apple"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "AGENTS.md").write_text("user-owned\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "unmanaged"):
                install_bundle(bundle, target)
            with self.assertRaisesRegex(ContractError, "unmanaged"):
                install_bundle(bundle, target, dry_run=True)
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "user-owned\n")

    def test_modified_managed_install_is_not_silently_replaced(self) -> None:
        bundle = build_install_bundle(MANIFESTS, platforms=["apple"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(bundle, target)
            (target / "AGENTS.md").write_text("locally modified\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(bundle, target)
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "locally modified\n")

    def test_modified_package_and_extra_skill_root_file_are_not_replaced(self) -> None:
        apple = build_install_bundle(MANIFESTS, platforms=["apple"])
        core = build_install_bundle(MANIFESTS, core_only=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(apple, target)
            provider = target / ".agent-skills" / "packages" / "apple" / "provider" / "manifest.json"
            provider.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(core, target)
            self.assertEqual(provider.read_text(encoding="utf-8"), "{}\n")

            install_bundle(apple, Path(directory) / "clean")
            clean = Path(directory) / "clean"
            extra = clean / "skills" / "user-note.txt"
            extra.write_text("user-owned\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(core, clean)
            self.assertEqual(extra.read_text(encoding="utf-8"), "user-owned\n")

    def test_lock_status_symlink_and_mode_changes_fail_closed(self) -> None:
        bundle = build_install_bundle(MANIFESTS, platforms=["apple"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(bundle, target)
            lock_path = target / ".agent-skills" / "install-lock.json"
            lock = load(lock_path)
            lock["status"] = "planned"
            dump(lock, lock_path)
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(bundle, target)

            shutil.rmtree(target)
            install_bundle(bundle, target)
            skill_file = target / "skills" / "apple-verification" / "SKILL.md"
            skill_file.chmod(0o600)
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(bundle, target)

            shutil.rmtree(target)
            install_bundle(bundle, target)
            lock_copy = target / ".agent-skills" / "lock-copy.json"
            shutil.copyfile(lock_path, lock_copy)
            lock_path.unlink()
            lock_path.symlink_to(lock_copy.name)
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(bundle, target)

    def test_top_level_managed_modes_fail_closed(self) -> None:
        bundle = build_install_bundle(MANIFESTS, platforms=["apple"])
        cases = (
            ("AGENTS.md", 0o600),
            ("skills", 0o700),
            (".agent-skills", 0o700),
            (".agent-skills/packages", 0o700),
            (".agent-skills/install-lock.json", 0o600),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (relative, mode) in enumerate(cases):
                with self.subTest(path=relative):
                    target = Path(directory) / f"codex-{index}"
                    install_bundle(bundle, target)
                    (target / relative).chmod(mode)
                    with self.assertRaisesRegex(ContractError, "modified"):
                        install_bundle(bundle, target)

    def test_failed_profile_switch_rolls_back_previous_managed_install(self) -> None:
        apple = build_install_bundle(MANIFESTS, platforms=["apple"])
        core = build_install_bundle(MANIFESTS, core_only=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(apple, target)
            previous_agents = (target / "AGENTS.md").read_bytes()
            real_replace = __import__("os").replace
            failed = False

            def fail_during_skill_swap(source: Path, destination: Path) -> None:
                nonlocal failed
                source_path = Path(source)
                if not failed and source_path.name == "skills" and source_path.parent.name.startswith(".agent-skills-stage-"):
                    failed = True
                    raise OSError("injected swap failure")
                real_replace(source, destination)

            with mock.patch("agent_workflow.installation.os.replace", side_effect=fail_during_skill_swap):
                with self.assertRaisesRegex(OSError, "injected"):
                    install_bundle(core, target)
            self.assertEqual((target / "AGENTS.md").read_bytes(), previous_agents)
            self.assertTrue((target / "skills" / "apple-verification" / "SKILL.md").is_file())
            self.assertEqual(load(target / ".agent-skills" / "install-lock.json")["fingerprint"], apple.plan["fingerprint"])

    def test_failed_rollback_preserves_recovery_backup(self) -> None:
        apple = build_install_bundle(MANIFESTS, platforms=["apple"])
        core = build_install_bundle(MANIFESTS, core_only=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(apple, target)
            real_replace = os.replace
            primary_failed = False
            recovery_failed = False

            def fail_swap_and_restore(source: Path, destination: Path) -> None:
                nonlocal primary_failed, recovery_failed
                source_path = Path(source)
                if (
                    not primary_failed
                    and source_path.name == "skills"
                    and source_path.parent.name.startswith(".agent-skills-stage-")
                ):
                    primary_failed = True
                    raise OSError("injected primary failure")
                if (
                    primary_failed
                    and not recovery_failed
                    and source_path.parent.name.startswith(".agent-skills-backup-")
                ):
                    recovery_failed = True
                    raise OSError("injected restore failure")
                real_replace(source, destination)

            with mock.patch("agent_workflow.installation.os.replace", side_effect=fail_swap_and_restore):
                with self.assertRaisesRegex(ContractError, "recovery backup preserved") as raised:
                    install_bundle(core, target)
            recovery_text = str(raised.exception).split("recovery backup preserved at ", 1)[1].split(": restore", 1)[0]
            recovery = Path(recovery_text)
            self.assertTrue((recovery / "skills" / "apple-verification" / "SKILL.md").is_file())
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertFalse((target / "skills").exists())

    def test_source_change_after_plan_is_rejected_before_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="apple-skill")
            bundle = build_install_bundle(root, platforms=["apple"])
            (root / "apple" / "skills" / "apple-skill" / "SKILL.md").write_text(
                "changed after planning\n", encoding="utf-8"
            )
            target = Path(directory) / "target"
            with self.assertRaisesRegex(ContractError, "staged package differs"):
                install_bundle(bundle, target)
            self.assertFalse((target / "AGENTS.md").exists())

    def test_provider_change_during_plan_build_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            shutil.copytree(MANIFESTS / "core", root / "core")
            shutil.copytree(MANIFESTS / "apple", root / "apple")
            shutil.copytree(MANIFESTS.parent / "disciplines", root.parent / "disciplines")
            real_collect = installation_module._collect_files
            mutated = False

            def mutate_before_collect(package_root: Path, roots: object) -> object:
                nonlocal mutated
                if package_root.name == "apple" and not mutated:
                    mutated = True
                    provider_path = package_root / "provider" / "manifest.json"
                    provider = load(provider_path)
                    provider["capabilities"][0]["permission_profile"] = "credential-admin"
                    dump(provider, provider_path)
                return real_collect(package_root, roots)

            with mock.patch("agent_workflow.installation._collect_files", side_effect=mutate_before_collect):
                with self.assertRaisesRegex(ContractError, "provider manifest changed"):
                    build_install_bundle(root, platforms=["apple"])

    def test_skill_change_during_plan_build_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="apple-skill")
            real_collect = installation_module._collect_files
            mutated = False

            def mutate_before_collect(package_root: Path, roots: object) -> object:
                nonlocal mutated
                if package_root.name == "apple" and not mutated:
                    mutated = True
                    (package_root / "skills" / "apple-skill" / "SKILL.md").write_text(
                        "changed during planning\n", encoding="utf-8"
                    )
                return real_collect(package_root, roots)

            with mock.patch("agent_workflow.installation._collect_files", side_effect=mutate_before_collect):
                with self.assertRaisesRegex(ContractError, "skill changed"):
                    build_install_bundle(root, platforms=["apple"])

    def test_selection_requires_explicit_installable_platform(self) -> None:
        self.assertEqual(resolve_platform_selection(MANIFESTS, platforms=["all"]), ("apple",))
        with self.assertRaisesRegex(ContractError, "select --core-only"):
            resolve_platform_selection(MANIFESTS)
        with self.assertRaisesRegex(ContractError, "not installable"):
            resolve_platform_selection(MANIFESTS, platforms=["web"])
        with self.assertRaisesRegex(ContractError, "cannot be combined"):
            resolve_platform_selection(MANIFESTS, platforms=["all", "apple"])

    def test_discipline_dependency_closure_is_versioned_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            platforms = repository / "platforms"
            disciplines = repository / "disciplines"
            self.write_installable_package(platforms, "core", scope="global", skill=None)
            self.write_installable_package(
                disciplines,
                "documentation",
                scope="discipline:documentation",
                skill="html-docs",
                kind="discipline",
            )
            dependency = [{
                "id": "documentation",
                "required_capabilities": ["fixture.documentation"],
                "requirement": "required",
                "version": ">=1.0.0 <2.0.0",
            }]
            self.write_installable_package(
                platforms,
                "apple",
                scope="platform:apple",
                skill="apple-skill",
                package_requires=dependency,
            )

            bundle = build_install_bundle(platforms, platforms=["apple"])

            self.assertEqual([item["id"] for item in bundle.plan["selected_packages"]], [
                "core", "documentation", "apple",
            ])
            self.assertEqual(bundle.plan["selected_disciplines"], [])
            self.assertEqual(bundle.plan["resolved_dependencies"], [{
                "from": "apple",
                "required_capabilities": ["fixture.documentation"],
                "requirement": "required",
                "to": "documentation",
                "version": ">=1.0.0 <2.0.0",
            }])
            self.assertEqual(
                bundle.plan["selected_packages"][1]["selection_reasons"],
                ["dependency:apple"],
            )
            self.assertIn("fragment:documentation.global", bundle.instructions)
            self.assertEqual(
                [item["name"] for item in bundle.plan["skills"]],
                ["html-docs", "apple-skill"],
            )

    def test_discipline_can_be_selected_without_a_platform_and_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            platforms = repository / "platforms"
            disciplines = repository / "disciplines"
            self.write_installable_package(platforms, "core", scope="global", skill=None)
            self.write_installable_package(
                disciplines,
                "documentation",
                scope="discipline:documentation",
                skill="html-docs",
                kind="discipline",
            )
            bundle = build_install_bundle(platforms, disciplines=["documentation"])
            self.assertEqual(bundle.plan["selected_platforms"], [])
            self.assertEqual(bundle.plan["selected_disciplines"], ["documentation"])
            self.assertEqual(
                [item["id"] for item in bundle.plan["selected_packages"]],
                ["core", "documentation"],
            )

    def test_package_dependency_missing_version_and_cycle_fail_closed(self) -> None:
        dependency = lambda package: [{
            "id": package,
            "required_capabilities": [f"fixture.{package}"],
            "requirement": "required",
            "version": ">=1.0.0 <2.0.0",
        }]
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            platforms = repository / "platforms"
            disciplines = repository / "disciplines"
            self.write_installable_package(platforms, "core", scope="global", skill=None)
            self.write_installable_package(
                platforms, "apple", scope="platform:apple", skill="apple-skill",
                package_requires=dependency("documentation"),
            )
            with self.assertRaisesRegex(ContractError, "requires missing package"):
                build_install_bundle(platforms, platforms=["apple"])

            self.write_installable_package(
                disciplines, "documentation", scope="discipline:documentation", skill="html-docs",
                kind="discipline", package_requires=dependency("apple"),
            )
            with self.assertRaisesRegex(ContractError, "dependency cycle"):
                build_install_bundle(platforms, platforms=["apple"])

            documentation = load(disciplines / "documentation" / "manifest.json")
            documentation["package_requires"] = []
            documentation["version"] = "2.0.0"
            dump(documentation, disciplines / "documentation" / "manifest.json")
            with self.assertRaisesRegex(ContractError, "found 2.0.0"):
                build_install_bundle(platforms, platforms=["apple"])

    def test_package_dependency_required_capabilities_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            platforms = repository / "platforms"
            disciplines = repository / "disciplines"
            self.write_installable_package(platforms, "core", scope="global", skill=None)
            self.write_installable_package(
                disciplines, "documentation", scope="discipline:documentation", skill="html-docs",
                kind="discipline",
            )
            self.write_installable_package(
                platforms,
                "apple",
                scope="platform:apple",
                skill="apple-skill",
                package_requires=[{
                    "id": "documentation",
                    "required_capabilities": ["documentation.html"],
                    "requirement": "required",
                    "version": ">=1.0.0 <2.0.0",
                }],
            )
            with self.assertRaisesRegex(ContractError, "missing capabilities: documentation.html"):
                build_install_bundle(platforms, platforms=["apple"])

    def test_optional_package_dependency_is_only_checked_when_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            platforms = repository / "platforms"
            disciplines = repository / "disciplines"
            self.write_installable_package(platforms, "core", scope="global", skill=None)
            self.write_installable_package(
                platforms,
                "apple",
                scope="platform:apple",
                skill="apple-skill",
                package_requires=[{
                    "id": "documentation",
                    "required_capabilities": ["fixture.documentation"],
                    "requirement": "optional",
                    "version": ">=1.0.0 <2.0.0",
                }],
            )
            without_optional = build_install_bundle(platforms, platforms=["apple"])
            self.assertEqual([item["id"] for item in without_optional.plan["selected_packages"]], ["core", "apple"])

            self.write_installable_package(
                disciplines, "documentation", scope="discipline:documentation", skill="html-docs",
                kind="discipline",
            )
            selected = build_install_bundle(
                platforms, platforms=["apple"], disciplines=["documentation"]
            )
            self.assertEqual(selected.plan["resolved_dependencies"][0]["requirement"], "optional")

            document = load(disciplines / "documentation" / "manifest.json")
            document["version"] = "2.0.0"
            dump(document, disciplines / "documentation" / "manifest.json")
            with self.assertRaisesRegex(ContractError, "optionally requires documentation"):
                build_install_bundle(platforms, platforms=["apple"], disciplines=["documentation"])

    def test_install_lock_dependency_metadata_fails_closed(self) -> None:
        baseline = build_install_bundle(MANIFESTS, platforms=["apple"]).plan

        def refresh(plan: dict[str, object]) -> None:
            plan["fingerprint"] = sha256({
                key: value for key, value in plan.items() if key not in {"fingerprint", "status"}
            })

        cases = {
            "empty selected packages": lambda plan: plan.update({"selected_packages": []}),
            "ghost discipline": lambda plan: plan["selected_disciplines"].append("ghost"),
            "core kind": lambda plan: plan["selected_packages"][0].update({"kind": "platform"}),
            "self dependency": lambda plan: plan["resolved_dependencies"].append({
                "from": "apple", "to": "apple", "requirement": "required",
                "version": ">=0.1.0", "required_capabilities": ["implementation.apple"],
            }),
            "empty version": lambda plan: plan["resolved_dependencies"].append({
                "from": "apple", "to": "core", "requirement": "required",
                "version": "", "required_capabilities": ["core.intent-lock"],
            }),
            "duplicate capabilities": lambda plan: plan["resolved_dependencies"].append({
                "from": "apple", "to": "core", "requirement": "required",
                "version": ">=0.1.0", "required_capabilities": ["core.intent-lock", "core.intent-lock"],
            }),
            "unsatisfied version": lambda plan: plan["resolved_dependencies"].append({
                "from": "apple", "to": "core", "requirement": "optional",
                "version": ">=9.0.0 <10.0.0", "required_capabilities": ["core.intent-lock"],
            }),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                plan = deepcopy(baseline)
                mutate(plan)
                refresh(plan)
                with self.assertRaises(ContractError):
                    validate("install-plan", plan)

    def test_lock_v2_freeze_metadata_fails_closed(self) -> None:
        baseline = build_install_bundle(MANIFESTS, platforms=["apple"]).plan

        def refresh(plan: dict[str, object]) -> None:
            plan["fingerprint"] = sha256({
                key: value for key, value in plan.items() if key not in {"fingerprint", "status"}
            })

        cases = {
            "missing source digest": lambda plan: plan["selected_packages"][0].pop("source_sha256"),
            "missing rule trace": lambda plan: plan["instructions"].pop("rule_trace"),
            "missing assets": lambda plan: plan.pop("assets"),
            "forged provider": lambda plan: plan["capability_providers"]["implementation.apple"].update(
                {"package_version": "9.9.9"}
            ),
            "forged asset": lambda plan: plan["assets"][0].update({"package": "apple"}),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                plan = deepcopy(baseline)
                mutate(plan)
                if "assets" in plan:
                    plan["asset_summary"]["content_sha256"] = sha256(plan["assets"])
                refresh(plan)
                with self.assertRaises(ContractError):
                    validate("install-plan", plan)

    def test_multiple_platforms_merge_into_one_agents_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="apple-skill")
            self.write_installable_package(root, "web", scope="platform:web", skill="web-skill")
            bundle = build_install_bundle(root, platforms=["web", "apple"])
            self.assertEqual(bundle.plan["selected_platforms"], ["apple", "web"])
            self.assertEqual(bundle.instructions.count(MANAGED_HEADER), 1)
            self.assertIn("apple instructions", bundle.instructions)
            self.assertIn("web instructions", bundle.instructions)
            target = Path(directory) / "codex"
            install_bundle(bundle, target)
            self.assertTrue((target / "skills" / "apple-skill" / "SKILL.md").is_file())
            self.assertTrue((target / "skills" / "web-skill" / "SKILL.md").is_file())

    def test_instruction_rules_are_locked_and_deny_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="apple-skill")
            core_fragment = root / "core" / "agent-instructions" / "global.md"
            apple_fragment = root / "apple" / "agent-instructions" / "global.md"
            core_fragment.write_text(
                "<!-- rule:shared.network effect=allow -->\n- allow network\n", encoding="utf-8"
            )
            apple_fragment.write_text(
                "<!-- rule:shared.network effect=deny -->\n- deny network\n", encoding="utf-8"
            )
            for package in ("core", "apple"):
                manifest_path = root / package / "manifest.json"
                manifest = load(manifest_path)
                manifest["installation"]["instruction_fragments"][0]["merge_strategy"] = "append"
                dump(manifest, manifest_path)
            bundle = build_install_bundle(root, platforms=["apple"])
            self.assertEqual(bundle.plan["instructions"]["rule_trace"][-1]["decision"], "deny-wins")
            self.assertEqual(bundle.plan["instructions"]["rule_trace"][-1]["effect"], "deny")
            self.assertNotIn("- allow network", bundle.instructions)
            self.assertEqual(bundle.instructions.count("- deny network"), 1)
            self.assertEqual(bundle.instructions.count("rule:shared.network"), 1)

            core_manifest_path = root / "core" / "manifest.json"
            core_manifest = load(core_manifest_path)
            core_manifest["installation"]["instruction_fragments"][0]["merge_strategy"] = "locked"
            dump(core_manifest, core_manifest_path)
            with self.assertRaisesRegex(ContractError, "locked instruction rule conflict"):
                build_install_bundle(root, platforms=["apple"])

            # Effect is part of the locked identity even when the bullet text is unchanged.
            apple_fragment.write_text(
                "<!-- rule:shared.network effect=deny -->\n- allow network\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractError, "locked instruction rule conflict"):
                build_install_bundle(root, platforms=["apple"])

    def test_later_identical_locked_rule_freezes_following_fragments(self) -> None:
        fragments = [
            {
                "content": "<!-- rule:shared.network effect=allow -->\n- allow network\n",
                "id": "first",
                "merge_strategy": "append",
                "package": "core",
                "scope": "global",
            },
            {
                "content": "<!-- rule:shared.network effect=allow -->\n- allow network\n",
                "id": "second",
                "merge_strategy": "locked",
                "package": "security",
                "scope": "global",
            },
            {
                "content": "<!-- rule:shared.network effect=deny -->\n- deny network\n",
                "id": "third",
                "merge_strategy": "append",
                "package": "apple",
                "scope": "platform:apple",
            },
        ]
        with self.assertRaisesRegex(ContractError, "locked instruction rule conflict"):
            installation_module._resolve_instruction_rules(fragments)

    def test_multiple_platform_fragment_and_skill_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="shared-skill")
            self.write_installable_package(root, "web", scope="platform:web", skill="shared-skill")
            with self.assertRaisesRegex(ContractError, "skill name conflict"):
                build_install_bundle(root, platforms=["apple", "web"])

            web_manifest = load(root / "web" / "manifest.json")
            web_manifest["installation"]["instruction_fragments"][0]["id"] = "apple.global"
            (root / "web" / "skills" / "shared-skill").rename(root / "web" / "skills" / "web-skill")
            dump(web_manifest, root / "web" / "manifest.json")
            with self.assertRaisesRegex(ContractError, "fragment ids conflict"):
                build_install_bundle(root, platforms=["apple", "web"])

    def test_fragment_order_is_core_then_platform_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="apple-skill")
            apple = load(root / "apple" / "manifest.json")
            apple["installation"]["instruction_fragments"][0]["order"] = -100
            dump(apple, root / "apple" / "manifest.json")
            bundle = build_install_bundle(root, platforms=["apple"])
            self.assertEqual(
                [item["id"] for item in bundle.plan["instructions"]["fragments"]],
                ["core.global", "apple.global"],
            )

    def test_installation_paths_cannot_escape_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            manifest = load(root / "core" / "manifest.json")
            manifest["installation"]["instruction_fragments"][0]["path"] = "../outside.md"
            dump(manifest, root / "core" / "manifest.json")
            with self.assertRaisesRegex(ContractError, "package-relative"):
                build_install_bundle(root, core_only=True)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support is required")
    def test_package_and_declared_asset_symlinks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            self.write_installable_package(root, "core", scope="global", skill=None)
            self.write_installable_package(root, "apple", scope="platform:apple", skill="apple-skill")

            manifest = root / "apple" / "manifest.json"
            external_manifest = Path(directory) / "apple-manifest.json"
            manifest.replace(external_manifest)
            manifest.symlink_to(external_manifest)
            with self.assertRaisesRegex(ContractError, "unsafe|not installable"):
                build_install_bundle(root, platforms=["apple"])

            manifest.unlink()
            shutil.copyfile(external_manifest, manifest)
            fragment = root / "apple" / "agent-instructions" / "global.md"
            target = root / "apple" / "agent-instructions" / "replacement.md"
            fragment.replace(target)
            fragment.symlink_to(target.name)
            with self.assertRaisesRegex(ContractError, "must not traverse a symlink"):
                build_install_bundle(root, platforms=["apple"])

            fragment.unlink()
            target.replace(fragment)
            apple_package = root / "apple"
            external_package = Path(directory) / "apple-package"
            apple_package.replace(external_package)
            apple_package.symlink_to(external_package, target_is_directory=True)
            with self.assertRaisesRegex(ContractError, "candidate is unsafe|directory is missing or unsafe"):
                build_install_bundle(root, platforms=["apple"])
            with self.assertRaisesRegex(ContractError, "candidate is unsafe"):
                resolve_platform_selection(root, platforms=["all"])

    def test_install_preflight_enforces_provider_contract(self) -> None:
        cases = (
            ("permission", "expands permission"),
            ("core", "incompatible with core"),
            ("required", "missing required capabilities"),
        )
        for mutation, message in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "platforms"
                shutil.copytree(MANIFESTS / "core", root / "core")
                shutil.copytree(MANIFESTS / "apple", root / "apple")
                shutil.copytree(MANIFESTS.parent / "disciplines", root.parent / "disciplines")
                provider_path = root / "apple" / "provider" / "manifest.json"
                provider = load(provider_path)
                if mutation == "permission":
                    provider["capabilities"][0]["permission_profile"] = "credential-admin"
                elif mutation == "core":
                    provider["package"]["core_compatibility"] = ">=9.0.0"
                else:
                    required = load(root / "apple" / "manifest.json")["provider_contract"]["required_capabilities"][0]
                    provider["capabilities"] = [item for item in provider["capabilities"] if item["id"] != required]
                    provider["bindings"].pop(required)
                dump(provider, provider_path)
                with self.assertRaisesRegex(ContractError, message):
                    build_install_bundle(root, platforms=["apple"])

    def test_install_preflight_requires_skill_binding_target_in_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            shutil.copytree(MANIFESTS / "core", root / "core")
            shutil.copytree(MANIFESTS / "apple", root / "apple")
            shutil.copytree(MANIFESTS.parent / "disciplines", root.parent / "disciplines")
            provider_path = root / "apple" / "provider" / "manifest.json"
            provider = load(provider_path)
            provider["bindings"]["implementation.apple"]["name"] = "missing-skill"
            dump(provider, provider_path)
            with self.assertRaisesRegex(ContractError, "binding target is missing"):
                build_install_bundle(root, platforms=["apple"])

    def test_install_preflight_requires_non_skill_binding_target_in_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            root = repository / "platforms"
            shutil.copytree(MANIFESTS / "core", root / "core")
            shutil.copytree(MANIFESTS.parent / "runtime-configs", repository / "runtime-configs")
            manifest_path = repository / "runtime-configs" / "codex" / "manifest.json"
            manifest = load(manifest_path)
            manifest["bindings"]["runtime.codex.configure"]["name"] = "does-not-exist.py"
            dump(manifest, manifest_path)
            with self.assertRaisesRegex(ContractError, "binding target is missing"):
                build_install_bundle(root, runtime_configs=["codex"])

    def test_dry_run_does_not_touch_target(self) -> None:
        bundle = build_install_bundle(MANIFESTS, platforms=["apple"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing"
            report = install_bundle(bundle, target, dry_run=True)
            self.assertEqual(report["status"], "planned")
            self.assertFalse(target.exists())

            file_target = Path(directory) / "not-a-directory"
            file_target.write_text("occupied\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "must be a directory"):
                install_bundle(bundle, file_target, dry_run=True)


if __name__ == "__main__":
    unittest.main()
