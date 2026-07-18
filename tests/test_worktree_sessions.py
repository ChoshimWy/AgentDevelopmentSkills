from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import ROOT

from agent_workflow.adapters.contracts import build_adapter_request
from agent_workflow.models import ContractError, NodeStatus
from agent_workflow.runtime.ledger import RunLedger
from agent_workflow.runtime.state_machine import NodeStateMachine
from agent_workflow.worktree_sessions.cli import _create, _validate_platform_selection
from agent_workflow.worktree_sessions.gate import attach_adapter_result, evaluate_session_gate
from agent_workflow.worktree_sessions.git_workspace import (
    create_session_worktree,
    freeze_checkpoint,
    inspect_repository,
    repository_patch,
    session_source_identity,
)
from agent_workflow.worktree_sessions.registry import SessionRegistry, new_session_context


def load_apple_worktree_module():
    path = ROOT / "platforms/apple/skills/apple-verification/scripts/worktree_session.py"
    spec = importlib.util.spec_from_file_location("apple_worktree_session", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apple_worktree_session = load_apple_worktree_module()


def apple_platform_contexts() -> dict:
    return {
        "apple": {
            "bindings": {
                "verification.apple.auto": {"kind": "skill", "mode": "auto", "name": "apple-verification"},
            },
            "context": {},
            "provider_id": "ios-agent-skills",
        }
    }


def apple_capability_closure() -> dict:
    return {
        "review.independent": {
            "binding": {"kind": "skill", "name": "code-review"},
            "provider_id": "review",
        },
        "verification.apple.auto": {
            "binding": {"kind": "skill", "mode": "auto", "name": "apple-verification"},
            "provider_id": "ios-agent-skills",
        },
    }


def git_capability_closure() -> dict:
    return {
        "review.independent": {
            "binding": {"kind": "skill", "name": "code-review"},
            "provider_id": "review",
        },
        "verification.git.repository": {
            "binding": {"kind": "skill", "mode": "verify", "name": "session-worktree"},
            "provider_id": "git",
        },
    }


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_repo(parent: Path, name: str = "repo") -> Path:
    root = parent / name
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Session Tests")
    git(root, "config", "user.email", "session-tests@example.invalid")
    git(root, "config", "core.hooksPath", "/dev/null")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "file.txt")
    git(root, "commit", "-q", "-m", "base")
    return root


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


class GitWorkspaceTests(unittest.TestCase):
    def test_dirty_implicit_base_is_rejected_but_explicit_base_never_inherits_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repo(Path(temporary))
            base = git(root, "rev-parse", "HEAD")
            (root / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "dirty worktree"):
                create_session_worktree(root, name="implicit")
            record, notice = create_session_worktree(
                root,
                name="explicit",
                base_ref=base,
                worktree_root=Path(temporary) / "worktrees",
            )
            created = Path(record["worktree_path"])
            self.assertEqual("base\n", (created / "file.txt").read_text(encoding="utf-8"))
            self.assertTrue(notice["source_worktree_dirty"])
            self.assertFalse(notice["source_worktree_changes_inherited"])
            self.assertEqual(base, record["base"]["commit"])

    def test_repository_patch_covers_staged_unstaged_and_untracked_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repo(Path(temporary))
            base = git(root, "rev-parse", "HEAD")
            (root / "file.txt").write_text("staged\n", encoding="utf-8")
            git(root, "add", "file.txt")
            (root / "file.txt").write_text("unstaged-after-index\n", encoding="utf-8")
            (root / "new.bin").write_bytes(b"one\x00two")
            first = repository_patch(root, repository_id="app", base_commit=base)
            second = repository_patch(root, repository_id="app", base_commit=base)
            self.assertEqual(first, second)
            self.assertEqual(["file.txt", "new.bin"], first["changed_files"])
            self.assertEqual(["new.bin"], first["untracked_files"])
            (root / "new.bin").write_bytes(b"changed")
            self.assertNotEqual(
                first["patch_hash"],
                repository_patch(root, repository_id="app", base_commit=base)["patch_hash"],
            )

    def test_multi_repository_source_identity_is_order_independent_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            app = make_repo(parent, "app")
            component = make_repo(parent, "component")
            app_base = git(app, "rev-parse", "HEAD")
            component_base = git(component, "rev-parse", "HEAD")
            (component / "file.txt").write_text("component change\n", encoding="utf-8")
            repositories = [
                inspect_repository(component, repository_id="component", role="dependency", base_ref=component_base),
                inspect_repository(app, repository_id="app", role="primary", base_ref=app_base),
            ]
            first = session_source_identity(repositories, mode="working")
            self.assertEqual(first, session_source_identity(reversed(repositories), mode="working"))
            (app / "new.txt").write_text("new\n", encoding="utf-8")
            refreshed = [
                inspect_repository(component, repository_id="component", role="dependency", base_ref=component_base),
                inspect_repository(app, repository_id="app", role="primary", base_ref=app_base),
            ]
            self.assertNotEqual(first, session_source_identity(refreshed, mode="working"))

    def test_repository_patch_v1_rejects_gitlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repo(Path(temporary))
            base = git(root, "rev-parse", "HEAD")
            git(root, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/dependency")
            with self.assertRaisesRegex(ContractError, "does not support Git submodules"):
                repository_patch(root, repository_id="app", base_commit=base)

    def test_session_create_preflights_gitlink_base_without_orphaning_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_repo(parent)
            object_id = git(root, "rev-parse", "HEAD")
            git(root, "update-index", "--add", "--cacheinfo", f"160000,{object_id},vendor/dependency")
            git(root, "commit", "-q", "-m", "gitlink")
            worktrees = parent / "worktrees"
            with self.assertRaisesRegex(ContractError, "does not support Git submodules"):
                create_session_worktree(
                    root,
                    name="orphan",
                    base_ref="HEAD",
                    worktree_root=worktrees,
                )
            self.assertFalse((worktrees / "orphan").exists())
            self.assertEqual("", git(root, "branch", "--list", "agent/orphan"))


class RegistryTests(unittest.TestCase):
    def test_registry_checkpoint_requires_clean_commits_and_rejects_illegal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repo(Path(temporary))
            base = git(root, "rev-parse", "HEAD")
            repository = inspect_repository(root, repository_id="app", base_ref=base)
            context = new_session_context(session_id="feature-a", project_id="project", repositories=[repository])
            registry = SessionRegistry(root)
            registry.create(context)
            (root / "file.txt").write_text("dirty before activation\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "clean worktree"):
                registry.checkpoint("feature-a")
            self.assertEqual(
                "created",
                registry.load("feature-a")["lifecycle"]["state"],
            )
            git(root, "checkout", "--", "file.txt")
            with self.assertRaisesRegex(ContractError, "illegal"):
                registry.transition("feature-a", "gated")
            context = registry.transition("feature-a", "active")
            (root / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "clean worktree"):
                freeze_checkpoint(context)
            self.assertEqual("working", context["source_identity"]["mode"])
            commit_all(root, "feature")
            freeze_checkpoint(context)
            registry.write(context)
            loaded = registry.load("feature-a")
            self.assertEqual("checkpointed", loaded["lifecycle"]["state"])
            self.assertEqual("committed", loaded["source_identity"]["mode"])
            self.assertTrue(loaded["repositories"][0]["checkpoint"]["commit"])
            with self.assertRaisesRegex(ContractError, "evaluate_and_gate"):
                registry.transition("feature-a", "gated")
            reopened = registry.transition("feature-a", "active")
            self.assertEqual("working", reopened["source_identity"]["mode"])
            self.assertIsNone(reopened["repositories"][0]["checkpoint"])

    def test_registry_rejects_foreign_primary_and_immutable_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            owner = make_repo(parent, "owner")
            foreign = make_repo(parent, "foreign")
            repository = inspect_repository(
                foreign,
                repository_id="app",
                base_ref=git(foreign, "rev-parse", "HEAD"),
            )
            context = new_session_context(session_id="foreign", project_id="project", repositories=[repository])
            with self.assertRaisesRegex(ContractError, "does not belong"):
                SessionRegistry(owner).create(context)

            registry = SessionRegistry(foreign)
            registry.create(context)
            changed = registry.load("foreign")
            changed["repositories"][0]["branch"] = "forged"
            with self.assertRaisesRegex(ContractError, "immutable repository"):
                registry.write(changed)

    def test_registry_rejects_symlinked_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repo(Path(temporary))
            base = git(root, "rev-parse", "HEAD")
            context = new_session_context(
                session_id="unsafe-lock",
                project_id="project",
                repositories=[inspect_repository(root, repository_id="app", base_ref=base)],
            )
            registry = SessionRegistry(root)
            registry.directory.mkdir()
            target = Path(temporary) / "foreign-lock"
            target.write_text("not a registry lock", encoding="utf-8")
            registry.lock_path.symlink_to(target)
            with self.assertRaisesRegex(ContractError, "lock is unsafe"):
                registry.create(context)

    def test_create_compensates_exact_worktree_and_branch_when_registry_create_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_repo(parent)
            worktrees = parent / "worktrees"
            args = SimpleNamespace(
                base=None,
                base_source=None,
                branch=None,
                name="compensated",
                platform=[],
                platform_manifest_root=None,
                project_id="project",
                repository=root,
                session_id="compensated",
                worktree_root=worktrees,
            )
            with mock.patch.object(SessionRegistry, "create_active", side_effect=ContractError("simulated race")):
                with self.assertRaisesRegex(ContractError, "simulated race"):
                    _create(args)
            self.assertFalse((worktrees / "compensated").exists())
            self.assertEqual("", git(root, "branch", "--list", "agent/compensated"))

            args.name = "invalid-context"
            args.session_id = "invalid-context"
            args.project_id = ""
            with self.assertRaises(ContractError):
                _create(args)
            self.assertFalse((worktrees / "invalid-context").exists())
            self.assertEqual(
                "",
                git(root, "branch", "--list", "agent/invalid-context"),
            )

    def test_bootstrap_only_platform_selection_fails_closed(self) -> None:
        platforms = ROOT / "platforms"
        self.assertEqual(["apple"], _validate_platform_selection(["apple"], platforms))
        with self.assertRaisesRegex(ContractError, "bootstrap_required"):
            _validate_platform_selection(["web"], platforms)


class GateTests(unittest.TestCase):
    def _fixture(self, directory: Path, *, pure_git: bool = False):
        root = make_repo(directory)
        base = git(root, "rev-parse", "HEAD")
        (root / "file.txt").write_text("feature\n", encoding="utf-8")
        commit_all(root, "feature")
        repository = inspect_repository(root, repository_id="app", base_ref=base)
        context = new_session_context(
            session_id="feature",
            project_id="project",
            repositories=[repository],
            selected_platforms=[] if pure_git else ["apple"],
            platform_contexts={} if pure_git else apple_platform_contexts(),
            capability_closure=git_capability_closure() if pure_git else apple_capability_closure(),
        )
        registry = SessionRegistry(root)
        registry.create(context)
        context = registry.transition("feature", "active")
        freeze_checkpoint(context)
        registry.write(context)

        artifacts = directory / "artifacts"
        artifacts.mkdir()
        (artifacts / "verification.json").write_text('{"passed":true}\n', encoding="utf-8")
        (artifacts / "review.json").write_text('{"blocking":[]}\n', encoding="utf-8")
        plan = {
            "fingerprint": "plan-fingerprint",
            "nodes": [
                {
                    "binding": (
                        {"kind": "skill", "mode": "verify", "name": "session-worktree"}
                        if pure_git
                        else {"kind": "skill", "mode": "auto", "name": "apple-verification"}
                    ),
                    "capability": "verification.git.repository" if pure_git else "verification.apple.auto",
                    "id": "verify",
                    "provider": "git" if pure_git else "ios-agent-skills",
                },
                {
                    "binding": {"kind": "skill", "name": "code-review"},
                    "capability": "review.independent",
                    "id": "review",
                    "provider": "review",
                },
            ],
            "plan_id": "plan",
            "schema_version": "1.0",
        }
        task_context = {
            "actors": {"implementation_actor": "builder", "reviewer_actor": "reviewer"},
            "checkpoints": {"CP3": "pending"},
            "worktree_session": {
                "session_id": context["session_id"],
                "source_identity": context["source_identity"]["value"],
            },
        }
        requests = {
            "verify": build_adapter_request(plan, "verify", context=task_context, invocation_id="invoke-verify"),
            "review": build_adapter_request(plan, "review", context=task_context, invocation_id="invoke-review"),
        }
        results = {
            "verify": self._result(
                requests["verify"],
                artifacts / "verification.json",
                "verification-artifact",
                "validation",
                {"executed_validation": [{"kind": "unit-test", "status": "passed"}]},
            ),
            "review": self._result(
                requests["review"],
                artifacts / "review.json",
                "review-artifact",
                "review",
                {
                    "blocking_issues": [],
                    "implementation_actor": "builder",
                    "reviewer_actor": "reviewer",
                },
            ),
        }
        machine = NodeStateMachine()
        attempts = {}
        for node in ("verify", "review"):
            attempt = machine.new_attempt(node)
            machine.transition(attempt, NodeStatus.READY, "ready")
            machine.transition(attempt, NodeStatus.RUNNING, "run")
            machine.transition(attempt, NodeStatus.PASSED, "passed")
            attempts[node] = attempt
        ledger = RunLedger(plan["fingerprint"], run_id="run")
        pairs = []
        for node in ("verify", "review"):
            attempt = attempts[node]
            request = requests[node]
            result = results[node]
            ledger.append("node-attempt", attempt)
            ledger.append(
                "adapter-outcome",
                {
                    "attempt_id": attempt["attempt_id"],
                    "cleanup": [],
                    "failure_attribution": {"category": "none", "summary": "completed"},
                    "invocation_id": result["invocation_id"],
                    "node_id": result["node_id"],
                    "provider": result["provider"],
                    "request_id": result["request_id"],
                    "status": "completed",
                },
            )
            for artifact in result["artifacts"]:
                ledger.append(
                    "artifact-hash",
                    {
                        "artifact_id": artifact["artifact_id"],
                        "attempt_id": attempt["attempt_id"],
                        "kind": artifact["kind"],
                        "node_id": result["node_id"],
                        "sha256": artifact["sha256"],
                        "uri": artifact["uri"],
                    },
                )
            for evidence in result["evidence"]:
                ledger.append(
                    "adapter-evidence",
                    {
                        **deepcopy(evidence),
                        "attempt_id": attempt["attempt_id"],
                        "node_id": result["node_id"],
                        "provider": result["provider"],
                    },
                )
            pairs.append({"attempt_id": attempt["attempt_id"], "request": request, "result": result})
            attach_adapter_result(
                context,
                attempt_id=attempt["attempt_id"],
                request=request,
                result=result,
            )
        registry.write(context)
        return root, context, pairs, ledger.finalize("completed"), artifacts

    @staticmethod
    def _result(
        request: dict,
        artifact_path: Path,
        artifact_id: str,
        evidence_kind: str,
        data: dict,
    ) -> dict:
        artifact = {
            "artifact_id": artifact_id,
            "kind": "test-report" if evidence_kind == "validation" else "review-report",
            "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "uri": artifact_path.name,
        }
        return {
            "artifacts": [artifact],
            "binding": deepcopy(request["binding"]),
            "capability": request["capability"],
            "cleanup": [],
            "evidence": [
                {
                    "artifact_ids": [artifact_id],
                    "data": data,
                    "kind": evidence_kind,
                    "status": "passed",
                    "summary": "passed",
                }
            ],
            "failure_attribution": {"category": "none", "summary": "completed"},
            "invocation_id": request["invocation_id"],
            "node_id": request["node_id"],
            "plan_fingerprint": request["plan_fingerprint"],
            "provider": request["provider"],
            "request_id": request["request_id"],
            "schema_version": "1.0",
            "status": "completed",
        }

    def test_final_gate_passes_for_clean_checkpoint_latest_ledger_and_hashed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            result = SessionRegistry(root).evaluate_and_gate(
                context["session_id"], adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts
            )
            self.assertEqual("passed", result["status"])
            self.assertFalse(result["diagnostics"])
            self.assertEqual("gated", SessionRegistry(root).load(context["session_id"])["lifecycle"]["state"])

    def test_pure_git_session_uses_generic_repository_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, context, pairs, ledger, artifacts = self._fixture(Path(temporary), pure_git=True)
            result = SessionRegistry(root).evaluate_and_gate(
                context["session_id"], adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts
            )
            self.assertEqual("passed", result["status"])

    def test_final_gate_blocks_source_or_artifact_changes_after_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            (artifacts / "verification.json").write_text("tampered\n", encoding="utf-8")
            (root / "file.txt").write_text("post-gate mutation\n", encoding="utf-8")
            result = evaluate_session_gate(context, adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts)
            self.assertEqual("blocked", result["status"])
            codes = {item["code"] for item in result["diagnostics"]}
            self.assertIn("source-invalid", codes)
            self.assertIn("verification-invalid", codes)

    def test_final_gate_rejects_review_evidence_in_verification_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            context["verification"]["adapter_result_refs"] = deepcopy(
                context["review"]["adapter_result_refs"]
            )
            result = evaluate_session_gate(context, adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts)
            self.assertEqual("blocked", result["status"])
            self.assertIn("verification-invalid", {item["code"] for item in result["diagnostics"]})

    def test_final_gate_rejects_symlinked_artifact_even_when_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            verification = artifacts / "verification.json"
            preserved = artifacts / "same-bytes.json"
            preserved.write_bytes(verification.read_bytes())
            verification.unlink()
            verification.symlink_to(preserved.name)
            result = evaluate_session_gate(context, adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts)
            self.assertEqual("blocked", result["status"])
            self.assertIn("verification-invalid", {item["code"] for item in result["diagnostics"]})

    def test_final_gate_rejects_platform_or_ledger_plan_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            wrong_platform = deepcopy(context)
            wrong_platform["selected_platforms"] = []
            wrong_platform["platform_contexts"] = {}
            with self.assertRaisesRegex(ContractError, "selected platform"):
                attach_adapter_result(
                    wrong_platform,
                    attempt_id=pairs[0]["attempt_id"],
                    request=pairs[0]["request"],
                    result=pairs[0]["result"],
                )
            ledger["plan_fingerprint"] = "different-plan"
            result = evaluate_session_gate(context, adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts)
            self.assertEqual("blocked", result["status"])
            self.assertIn("verification-invalid", {item["code"] for item in result["diagnostics"]})
            ledger["plan_fingerprint"] = "plan-fingerprint"
            ledger["node_attempts"].append(deepcopy(ledger["node_attempts"][0]))
            with self.assertRaisesRegex(ContractError, "attempt ids must be globally unique"):
                evaluate_session_gate(context, adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts)

    def test_final_gate_rejects_ledger_evidence_semantic_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            review = next(item for item in ledger["evidence"] if item["kind"] == "review")
            review["data"] = {
                "blocking_issues": ["critical"],
                "implementation_actor": "builder",
                "reviewer_actor": "builder",
            }
            result = evaluate_session_gate(context, adapter_pairs=pairs, ledger=ledger, artifact_root=artifacts)
            self.assertEqual("blocked", result["status"])
            self.assertIn("review-invalid", {item["code"] for item in result["diagnostics"]})

    def test_attach_and_gate_errors_do_not_partially_mutate_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, context, pairs, ledger, artifacts = self._fixture(Path(temporary))
            registry = SessionRegistry(root)
            before = registry.load(context["session_id"])
            malformed = deepcopy(ledger)
            malformed["node_attempts"].append(
                deepcopy(malformed["node_attempts"][0])
            )
            with self.assertRaisesRegex(ContractError, "globally unique"):
                registry.attach_and_gate(
                    context["session_id"],
                    adapter_pairs=pairs,
                    ledger=malformed,
                    artifact_root=artifacts,
                )
            self.assertEqual(before, registry.load(context["session_id"]))

            registry.evaluate_and_gate(
                context["session_id"],
                adapter_pairs=pairs,
                ledger=ledger,
                artifact_root=artifacts,
            )
            gated = registry.load(context["session_id"])
            with self.assertRaisesRegex(ContractError, "checkpointed"):
                registry.attach_and_gate(
                    context["session_id"],
                    adapter_pairs=pairs,
                    ledger=ledger,
                    artifact_root=artifacts,
                )
            self.assertEqual(gated, registry.load(context["session_id"]))


class AppleAdapterTests(unittest.TestCase):
    def test_codex_verify_daemon_revalidates_and_exports_worktree_session_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_repo(parent)
            dependency = make_repo(parent, "dependency")
            base = git(root, "rev-parse", "HEAD")
            context = new_session_context(
                session_id="daemon-session",
                project_id="project",
                repositories=[
                    inspect_repository(root, repository_id="app", role="primary", base_ref=base),
                    inspect_repository(
                        dependency,
                        repository_id="component",
                        role="dependency",
                        base_ref=git(dependency, "rev-parse", "HEAD"),
                    ),
                ],
                selected_platforms=["apple"],
                platform_contexts=apple_platform_contexts(),
                capability_closure=apple_capability_closure(),
            )
            context["lifecycle"]["state"] = "active"
            freeze_checkpoint(context)
            slot = apple_worktree_session.expected_derived_data_slot("project", "env:apple")
            request = apple_worktree_session.build_request(
                context,
                attempt_id="attempt-1",
                mode="checkpoint",
                environment_fingerprint="env:apple",
                derived_data_slot=slot,
                destination="platform=iOS Simulator,id=fake",
                test_plan="AppTests",
                target_fingerprints=["target:app-tests"],
            )
            request_path = parent / "request.json"
            request_path.write_text(
                json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

            fake_bin = parent / "bin"
            fake_bin.mkdir()
            for name, body in {
                "xcodebuild": "#!/bin/sh\nprintf 'Xcode 99.0\\nBuild version TEST\\n'\n",
                "xcrun": "#!/bin/sh\nprintf '99.0\\n'\n",
            }.items():
                path = fake_bin / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)

            marker = parent / "executed.json"
            build_check = parent / "fake-build-check.sh"
            build_check.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
[[ "$PWD" == "$1" ]]
[[ "$XCODE_DESTINATION" == "platform=iOS Simulator,id=fake" ]]
[[ "$XCODE_TEST_PLAN" == "AppTests" ]]
[[ "$CODEX_WORKTREE_SESSION_ID" == "daemon-session" ]]
[[ "$CODEX_WORKTREE_SESSION_ATTEMPT_ID" == "attempt-1" ]]
[[ "$CODEX_WORKTREE_SESSION_SOURCE_IDENTITY" == session-source:* ]]
[[ "$CODEX_WORKTREE_SESSION_ENVIRONMENT_FINGERPRINT" == "env:apple" ]]
[[ "$CODEX_WORKTREE_SESSION_DERIVED_DATA_SLOT" == """ + json.dumps(slot) + """ ]]
[[ "$CODEX_WORKTREE_SESSION_ARTIFACT_NAMESPACE" == sessions/daemon-session/*/attempt-1 ]]
[[ "$CODEX_WORKTREE_SESSION_REQUEST_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ -f "$CODEX_WORKTREE_SESSION_REQUEST" ]]
[[ -d "$CODEX_VERIFY_ARTIFACT_DIR" ]]
printf '{"passed":true}\n' > "$CODEX_VERIFY_ARTIFACT_DIR/evidence.json"
python3 - "$CODEX_WORKTREE_SESSION_REQUEST" "$CODEX_VERIFY_JOB_DIR" "$CODEX_VERIFY_ARTIFACT_DIR" <<'PY'
import json, sys
from pathlib import Path
request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(""" + repr(str(marker)) + """ ).write_text(
    json.dumps({"request": request, "job_dir": sys.argv[2], "artifact_dir": sys.argv[3]}, sort_keys=True) + "\\n",
    encoding="utf-8",
)
PY
""",
                encoding="utf-8",
            )
            build_check.chmod(0o755)
            failing_build_check = parent / "failing-build-check.sh"
            failing_build_check.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
mkdir "$CODEX_VERIFY_JOB_DIR/verification-report.json"
""",
                encoding="utf-8",
            )
            failing_build_check.chmod(0o755)

            queue = parent / "queue"
            legacy_missing_state = queue / "jobs" / "000-legacy-missing-state"
            legacy_missing_state.mkdir(parents=True)
            (legacy_missing_state / "created_at_epoch").write_text("1\n", encoding="utf-8")
            stale_queued = queue / "jobs" / "001-stale-queued"
            stale_queued.mkdir()
            (stale_queued / "state").write_text("queued\n", encoding="utf-8")
            (stale_queued / "created_at_epoch").write_text("1\n", encoding="utf-8")
            home = parent / "home"
            home.mkdir()
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            helper = ROOT / "platforms/apple/skills/apple-verification/scripts/worktree_session.py"
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(ROOT / "src"),
                "CODEX_BUILD_QUEUE_ROOT": str(queue),
                "CODEX_WORKTREE_SESSION_HELPER": str(helper),
                "CODEX_XCODEBUILD_DIGEST_SCRIPT": str(parent / "missing-digest.sh"),
            }
            daemon_pid: int | None = None
            try:
                conflict = subprocess.run(
                    [
                        "bash", str(wrapper),
                        "--worktree-session-request", str(request_path),
                        "--", "-project", "Fake.xcodeproj", "-scheme", "App",
                        "-destination", request["destination"],
                        "-testPlan", "OtherTests", "test",
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(0, conflict.returncode)
                self.assertIn("test plan conflicts", conflict.stderr)
                reuse = subprocess.run(
                    [
                        "bash", str(wrapper),
                        "--worktree-session-request", str(request_path),
                        "--", "-project", "Fake.xcodeproj", "-scheme", "App",
                        "-destination", request["destination"],
                        "-testPlan", request["test_plan"], "test-without-building",
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(0, reuse.returncode)
                self.assertIn("immutable build artifact identity", reuse.stderr)
                repaired = subprocess.run(
                    ["bash", str(wrapper), "--queue-doctor", "--repair"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(0, repaired.returncode, repaired.stderr)
                completed = subprocess.run(
                    [
                        "bash", str(wrapper),
                        "--worktree-session-request", str(request_path),
                        "--build-check", str(build_check), str(root),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                summary = json.loads(completed.stdout.strip().splitlines()[-1])
                self.assertEqual("passed", summary["status"])
                self.assertFalse(legacy_missing_state.exists())
                self.assertFalse(stale_queued.exists())
                self.assertEqual(
                    {
                        "generation_id": "codex-verify-v2",
                        "producer": "codex_verify",
                        "schema_version": "2.0",
                    },
                    json.loads((queue / "queue-meta.json").read_text(encoding="utf-8")),
                )
                job_dir = Path(summary["queue_job_dir"])
                self.assertEqual("true", (job_dir / "ready").read_text(encoding="utf-8").strip())
                self.assertEqual("2.0", (job_dir / "job_schema_version").read_text(encoding="utf-8").strip())
                self.assertEqual(
                    "codex-verify-v2",
                    (job_dir / "queue_generation_id").read_text(encoding="utf-8").strip(),
                )
                quarantine_names = {path.name for path in (queue / "quarantine").glob("**/*")}
                self.assertIn(legacy_missing_state.name, quarantine_names)
                self.assertIn(stale_queued.name, quarantine_names)
                self.assertTrue(summary["worktree_session"]["daemon_validated"])
                self.assertEqual(request["source_identity"], summary["worktree_session"]["source_identity"])
                self.assertEqual("AppTests", summary["worktree_session"]["test_plan"])
                request_artifact = Path(summary["artifact_paths"]["worktree_session_request"])
                self.assertEqual(request, json.loads(request_artifact.read_text(encoding="utf-8")))
                executed = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(request, executed["request"])
                artifact_directory = Path(summary["worktree_session"]["artifact_directory"])
                self.assertEqual(artifact_directory, Path(executed["artifact_dir"]))
                self.assertEqual(
                    queue / "artifacts" / request["artifact_namespace"] / summary["queue_job_id"],
                    artifact_directory,
                )
                artifact_manifest = json.loads(
                    Path(summary["artifact_paths"]["worktree_session_artifacts"]).read_text(encoding="utf-8")
                )
                self.assertEqual(["evidence.json"], [item["path"] for item in artifact_manifest["files"]])
                lease = queue / "derived-data-slots" / slot / "lease.lockdir"
                self.assertFalse(lease.exists())
                request_artifact.write_text(
                    json.dumps(request, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                after_tamper = subprocess.run(
                    [
                        "bash", str(wrapper),
                        "--worktree-session-request", str(request_path),
                        "--build-check", str(build_check), str(root),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(0, after_tamper.returncode, after_tamper.stderr)
                replacement = json.loads(after_tamper.stdout.strip().splitlines()[-1])
                self.assertNotEqual(summary["queue_job_id"], replacement["queue_job_id"])
                self.assertEqual("new", replacement["request_reuse"])
                replacement_artifacts = Path(replacement["worktree_session"]["artifact_directory"])
                (replacement_artifacts / "evidence.json").unlink()
                after_artifact_tamper = subprocess.run(
                    [
                        "bash", str(wrapper),
                        "--worktree-session-request", str(request_path),
                        "--build-check", str(build_check), str(root),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(0, after_artifact_tamper.returncode, after_artifact_tamper.stderr)
                after_artifact_summary = json.loads(after_artifact_tamper.stdout.strip().splitlines()[-1])
                self.assertNotEqual(replacement["queue_job_id"], after_artifact_summary["queue_job_id"])
                self.assertEqual("new", after_artifact_summary["request_reuse"])
                artifact_failure = subprocess.run(
                    [
                        "bash", str(wrapper),
                        "--worktree-session-request", str(request_path),
                        "--build-check", str(failing_build_check), str(root),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(70, artifact_failure.returncode, artifact_failure.stderr)
                failed_jobs = [
                    path for path in (queue / "jobs").iterdir()
                    if (path / "exit_code").is_file()
                    and (path / "exit_code").read_text(encoding="utf-8").strip() == "70"
                ]
                self.assertEqual(1, len(failed_jobs))
                self.assertEqual("failed", (failed_jobs[0] / "state").read_text(encoding="utf-8").strip())
                self.assertFalse(lease.exists())
                if (queue / "daemon.pid").is_file():
                    daemon_pid = int((queue / "daemon.pid").read_text(encoding="utf-8").strip())
            finally:
                if daemon_pid is None and (queue / "daemon.pid").is_file():
                    daemon_pid = int((queue / "daemon.pid").read_text(encoding="utf-8").strip())
                if daemon_pid is not None:
                    try:
                        os.kill(daemon_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    for _ in range(50):
                        try:
                            os.kill(daemon_pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.02)

    def test_apple_request_and_immutable_build_identity_bind_committed_multi_session_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_repo(parent, "app")
            dependency = make_repo(parent, "component")
            base = git(root, "rev-parse", "HEAD")
            (root / "file.txt").write_text("feature\n", encoding="utf-8")
            commit_all(root, "feature")
            repository = inspect_repository(root, repository_id="app", role="primary", base_ref=base)
            dependency_repository = inspect_repository(
                dependency,
                repository_id="component",
                role="dependency",
                base_ref=git(dependency, "rev-parse", "HEAD"),
            )
            context = new_session_context(
                session_id="apple-feature",
                project_id="project",
                repositories=[repository, dependency_repository],
                selected_platforms=["apple"],
                platform_contexts=apple_platform_contexts(),
                capability_closure=apple_capability_closure(),
            )
            context["lifecycle"]["state"] = "active"
            freeze_checkpoint(context)
            slot = apple_worktree_session.expected_derived_data_slot("project", "env:apple")
            request = apple_worktree_session.build_request(
                context,
                attempt_id="attempt-1",
                mode="checkpoint",
                environment_fingerprint="env:apple",
                derived_data_slot=slot,
                destination="platform=iOS Simulator,name=iPhone",
                test_plan="AppTests",
                target_fingerprints=["target:app-tests"],
            )
            self.assertEqual(context["source_identity"]["value"], request["source_identity"])
            self.assertEqual("app", request["repositories"][0]["repository_id"])
            self.assertNotIn(str(root), request["derived_data_slot"])
            self.assertIn(request["source_identity"].split(":", 1)[1], request["artifact_namespace"])
            self.assertEqual(
                request,
                apple_worktree_session.validate_daemon_request(
                    request,
                    repo_root=root,
                    destination=request["destination"],
                    test_plan=request["test_plan"],
                ),
            )
            (root / "file.txt").write_text("stale after request\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "clean worktree"):
                apple_worktree_session.validate_daemon_request(
                    request,
                    repo_root=root,
                    destination=request["destination"],
                    test_plan=request["test_plan"],
                )
            (root / "file.txt").write_text("feature\n", encoding="utf-8")
            (dependency / "file.txt").write_text("stale dependency after request\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "clean worktree"):
                apple_worktree_session.validate_daemon_request(
                    request,
                    repo_root=root,
                    destination=request["destination"],
                    test_plan=request["test_plan"],
                )
            (dependency / "file.txt").write_text("base\n", encoding="utf-8")

            artifacts = Path(temporary) / "build-artifacts"
            (artifacts / "Tests.xctestrun").parent.mkdir(parents=True)
            (artifacts / "Tests.xctestrun").write_bytes(plistlib.dumps({
                "AppTests": {
                    "TestBundlePath": "__TESTROOT__/AppTests.xctest",
                    "TestHostPath": "__TESTROOT__/App.app/App",
                }
            }))
            bundle = artifacts / "AppTests.xctest"
            bundle.mkdir()
            (bundle / "AppTests").write_bytes(b"bundle")
            app = artifacts / "App.app"
            app.mkdir()
            (app / "App").write_bytes(b"host-app")
            identity = apple_worktree_session.immutable_build_artifact_identity(
                request,
                artifact_root=artifacts,
                xctestrun="Tests.xctestrun",
                test_bundles=["AppTests.xctest"],
                product_artifacts=["App.app"],
            )
            apple_worktree_session.validate_immutable_build_artifact_identity(
                identity, request, artifact_root=artifacts
            )
            valid_xctestrun = (artifacts / "Tests.xctestrun").read_bytes()
            (artifacts / "Tests.xctestrun").write_bytes(plistlib.dumps({
                "AppTests": {
                    "TestBundlePath": "__TESTROOT__/AppTests.xctest",
                    "TestHostPath": "/mutable/SharedDerivedData/App.app/App",
                }
            }))
            with self.assertRaisesRegex(ContractError, "outside the immutable artifact closure"):
                apple_worktree_session.immutable_build_artifact_identity(
                    request,
                    artifact_root=artifacts,
                    xctestrun="Tests.xctestrun",
                    test_bundles=["AppTests.xctest"],
                    product_artifacts=["App.app"],
                )
            (artifacts / "Tests.xctestrun").write_bytes(plistlib.dumps({
                "AppTests": {
                    "TestBundlePath": "__TESTROOT__/AppTests.xctest",
                    "TestHostPath": "../../SharedDerivedData/App.app/App",
                }
            }))
            with self.assertRaisesRegex(ContractError, "outside the immutable artifact closure"):
                apple_worktree_session.immutable_build_artifact_identity(
                    request,
                    artifact_root=artifacts,
                    xctestrun="Tests.xctestrun",
                    test_bundles=["AppTests.xctest"],
                    product_artifacts=["App.app"],
                )
            (artifacts / "Tests.xctestrun").write_bytes(plistlib.dumps({
                "AppTests": {
                    "TestBundlePath": "__TESTROOT__/AppTests.xctest",
                    "TestHostPath": "__TESTROOT__/App.app/../Outside.app/App",
                }
            }))
            with self.assertRaisesRegex(ContractError, "outside the immutable artifact closure"):
                apple_worktree_session.immutable_build_artifact_identity(
                    request,
                    artifact_root=artifacts,
                    xctestrun="Tests.xctestrun",
                    test_bundles=["AppTests.xctest"],
                    product_artifacts=["App.app"],
                )
            (artifacts / "Tests.xctestrun").write_bytes(plistlib.dumps({
                "AppTests": {
                    "TestBundlePath": "__TESTROOT__/AppTests.xctest",
                    "ToolPath": "__PLATFORMS__/../SharedDerivedData/tool",
                }
            }))
            with self.assertRaisesRegex(ContractError, "outside the immutable artifact closure"):
                apple_worktree_session.immutable_build_artifact_identity(
                    request,
                    artifact_root=artifacts,
                    xctestrun="Tests.xctestrun",
                    test_bundles=["AppTests.xctest"],
                    product_artifacts=["App.app"],
                )
            (artifacts / "Tests.xctestrun").write_bytes(valid_xctestrun)
            (app / "App").write_bytes(b"other host app")
            with self.assertRaisesRegex(ContractError, "changed"):
                apple_worktree_session.validate_immutable_build_artifact_identity(
                    identity, request, artifact_root=artifacts
                )

    def test_apple_request_rejects_path_shaped_derived_data_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repo(Path(temporary))
            base = git(root, "rev-parse", "HEAD")
            context = new_session_context(
                session_id="apple",
                project_id="project",
                repositories=[inspect_repository(root, repository_id="app", base_ref=base)],
                selected_platforms=["apple"],
                platform_contexts=apple_platform_contexts(),
                capability_closure=apple_capability_closure(),
            )
            context["lifecycle"]["state"] = "active"
            freeze_checkpoint(context)
            with self.assertRaisesRegex(ContractError, "project/environment identity"):
                apple_worktree_session.build_request(
                    context,
                    attempt_id="attempt",
                    mode="dev",
                    environment_fingerprint="env:apple",
                    derived_data_slot="/tmp/DerivedData",
                    destination="platform=iOS Simulator,name=iPhone",
                    test_plan="AppTests",
                    target_fingerprints=["target:app-tests"],
                )
            with self.assertRaisesRegex(ContractError, "project/environment identity"):
                apple_worktree_session.build_request(
                    context,
                    attempt_id="attempt",
                    mode="dev",
                    environment_fingerprint="env:apple",
                    derived_data_slot="project/env-wrong",
                    destination="platform=iOS Simulator,name=iPhone",
                    test_plan="AppTests",
                    target_fingerprints=["target:app-tests"],
                )
            with self.assertRaisesRegex(ContractError, "project/environment identity"):
                apple_worktree_session.build_request(
                    context,
                    attempt_id="attempt",
                    mode="dev",
                    environment_fingerprint="env:apple",
                    derived_data_slot="../DerivedData",
                    destination="platform=iOS Simulator,name=iPhone",
                    test_plan="AppTests",
                    target_fingerprints=["target:app-tests"],
                )


if __name__ == "__main__":
    unittest.main()
