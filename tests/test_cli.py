from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS, ROOT
from agent_workflow.cli import default_manifest_directory, default_schema_directory


class CLITests(unittest.TestCase):
    def test_source_checkout_default_manifests_exist(self) -> None:
        self.assertEqual(default_manifest_directory(), MANIFESTS)
        self.assertTrue((default_manifest_directory() / "core" / "manifest.json").exists())
        self.assertEqual(default_schema_directory(), ROOT / "schemas")
        self.assertTrue((default_schema_directory() / "doctor-report-v1.schema.json").exists())

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [
                sys.executable, "-m", "agent_workflow.cli",
                "--manifests", str(MANIFESTS),
                *args,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_detect_and_explain(self) -> None:
        detected = self.run_cli("detect", str(FIXTURES / "apple-app"))
        self.assertEqual(detected.returncode, 0, detected.stderr)
        self.assertEqual(json.loads(detected.stdout)["platforms"], ["apple"])
        routed = self.run_cli("route", str(FIXTURES / "apple-app"), "--task", "实现 iOS 功能", "--explain")
        self.assertEqual(routed.returncode, 0, routed.stderr)
        self.assertTrue(json.loads(routed.stdout)["decisions"])

    def test_plan_and_fake_run(self) -> None:
        planned = self.run_cli("plan", str(FIXTURES / "apple-app"), "--task", "实现 iOS 功能", "--dry-run")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            ledger_path = Path(directory) / "ledger.jsonl"
            plan_path.write_text(planned.stdout, encoding="utf-8")
            run = self.run_cli("run", str(plan_path), "--ledger", str(ledger_path), "--fake-adapters")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "completed")
            resume = self.run_cli("resume", str(plan_path), "--ledger", str(ledger_path), "--fake-adapters")
            self.assertEqual(resume.returncode, 0, resume.stderr)
            self.assertEqual(json.loads(resume.stdout)["status"], "completed")
            validate = self.run_cli("validate", "workflow-plan", str(plan_path))
            self.assertEqual(validate.returncode, 0, validate.stderr)

    def test_plan_fails_closed_when_provider_is_disabled(self) -> None:
        planned = self.run_cli(
            "--disable-provider", "ios-agent-skills",
            "plan", str(FIXTURES / "apple-app"), "--task", "实现 iOS 功能", "--dry-run",
        )
        self.assertEqual(planned.returncode, 2)
        self.assertEqual(json.loads(planned.stdout)["status"], "blocked")

    def test_prepare_and_validate_structured_adapter_result(self) -> None:
        planned = self.run_cli("plan", str(FIXTURES / "apple-app"), "--task", "实现 iOS 功能", "--dry-run")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            context_path = root / "context.json"
            request_path = root / "request.json"
            result_path = root / "result.json"
            plan_path.write_text(planned.stdout, encoding="utf-8")
            context_path.write_text(json.dumps({
                "task": {"text": "实现 iOS 功能", "type": "code-medium", "risk": "medium"},
                "target_modules": ["."],
                "user_constraints": [],
                "checkpoints": {"CP0": "completed", "CP1": "in_progress", "CP2": "pending", "CP3": "pending"},
            }), encoding="utf-8")
            prepared = self.run_cli(
                "prepare-adapter", str(plan_path), "apple-2", "--context", str(context_path),
                "--invocation-id", "cli-verification-1"
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            request_path.write_text(prepared.stdout, encoding="utf-8")
            request = json.loads(prepared.stdout)
            result_path.write_text(json.dumps({
                "schema_version": "1.0",
                "request_id": request["request_id"],
                "invocation_id": request["invocation_id"],
                "plan_fingerprint": request["plan_fingerprint"],
                "node_id": request["node_id"],
                "capability": request["capability"],
                "provider": request["provider"],
                "binding": request["binding"],
                "status": "completed",
                "failure_attribution": {"category": "none", "summary": "未发现失败"},
                "cleanup": [],
                "evidence": [{
                    "kind": "validation", "status": "passed", "summary": "定向测试通过",
                    "data": {"tests": 1}, "artifact_ids": [],
                }],
                "artifacts": [],
            }), encoding="utf-8")
            validated = self.run_cli("validate-adapter-result", str(request_path), str(result_path))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(json.loads(validated.stdout)["status"], "passed")

    def test_provider_invocation_handoff_cli_uses_private_token_file(self) -> None:
        planned = self.run_cli(
            "plan",
            str(FIXTURES / "apple-app"),
            "--task",
            "实现 iOS 功能",
            "--dry-run",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = root / "handoff"
            plan_path = root / "plan.json"
            context_path = root / "context.json"
            result_path = root / "result.json"
            selection_path = root / "selection.json"
            token_path = root / "claim-token"
            plan_path.write_text(planned.stdout, encoding="utf-8")
            context_path.write_text(
                json.dumps({
                    "actors": {
                        "implementation_actor": "builder-1",
                        "reviewer_actor": "reviewer-1",
                    },
                    "checkpoints": {
                        "CP0": "completed",
                        "CP1": "in_progress",
                        "CP2": "pending",
                        "CP3": "pending",
                    },
                }),
                encoding="utf-8",
            )
            token_path.write_text(
                "provider-cli-private-token-0001-secure\n",
                encoding="utf-8",
            )
            token_path.chmod(0o600)
            prepared = self.run_cli(
                "invocation",
                "prepare",
                str(handoff),
                str(plan_path),
                "apple-2",
                "--context",
                str(context_path),
                "--invocation-id",
                "provider-cli-1",
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            record = json.loads(prepared.stdout)
            request = record["request"]
            claimed = self.run_cli(
                "invocation",
                "claim",
                str(handoff),
                request["request_id"],
                "--actor-id",
                "provider-host-1",
                "--claim-token-file",
                str(token_path),
            )
            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            self.assertNotIn("provider-cli-private-token-0001-secure", claimed.stdout)
            result_path.write_text(
                json.dumps({
                    "schema_version": "1.0",
                    "request_id": request["request_id"],
                    "invocation_id": request["invocation_id"],
                    "plan_fingerprint": request["plan_fingerprint"],
                    "node_id": request["node_id"],
                    "capability": request["capability"],
                    "provider": request["provider"],
                    "binding": request["binding"],
                    "status": "completed",
                    "failure_attribution": {"category": "none", "summary": "未发现失败"},
                    "cleanup": [],
                    "evidence": [{
                        "kind": "validation",
                        "status": "passed",
                        "summary": "定向测试通过",
                        "data": {"tests": 1},
                        "artifact_ids": [],
                    }],
                    "artifacts": [],
                }),
                encoding="utf-8",
            )
            submitted = self.run_cli(
                "invocation",
                "submit",
                str(handoff),
                request["request_id"],
                str(result_path),
                "--claim-token-file",
                str(token_path),
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            selection_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "plan_fingerprint": request["plan_fingerprint"],
                        "requests": {request["node_id"]: request["request_id"]},
                    }
                ),
                encoding="utf-8",
            )
            rejected_selection_without_root = self.run_cli(
                "run",
                str(plan_path),
                "--ledger",
                str(root / "ignored-ledger.jsonl"),
                "--fake-adapters",
                "--invocation-selection",
                str(selection_path),
            )
            self.assertEqual(rejected_selection_without_root.returncode, 2)
            self.assertIn(
                "--invocation-selection requires --invocation-root",
                rejected_selection_without_root.stderr,
            )
            run = self.run_cli(
                "run",
                str(plan_path),
                "--ledger",
                str(root / "ledger.jsonl"),
                "--invocation-root",
                str(handoff),
                "--adapter-context",
                str(context_path),
                "--invocation-selection",
                str(selection_path),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn(json.loads(run.stdout)["status"], {"blocked", "partial"})

    def test_platform_selective_install_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            dry_run = self.run_cli(
                "install", "--platform", "apple", "--target-root", str(target), "--dry-run"
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            apple_plan = json.loads(dry_run.stdout)
            self.assertEqual(apple_plan["selected_platforms"], ["apple"])
            self.assertEqual(
                [item["id"] for item in apple_plan["selected_packages"]],
                ["core", "design", "documentation", "git", "review", "workflow", "apple"],
            )
            self.assertFalse(target.exists())

            documentation = self.run_cli(
                "install", "--discipline", "documentation", "--target-root", str(target), "--dry-run"
            )
            self.assertEqual(documentation.returncode, 0, documentation.stderr)
            documentation_plan = json.loads(documentation.stdout)
            self.assertEqual(documentation_plan["selected_disciplines"], ["documentation"])
            self.assertEqual(
                [item["name"] for item in documentation_plan["skills"]],
                ["html-docs"],
            )

            runtime = self.run_cli(
                "install", "--runtime-config", "codex", "--target-root", str(target), "--dry-run"
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
            runtime_plan = json.loads(runtime.stdout)
            self.assertEqual(runtime_plan["selected_runtime_configs"], ["codex"])
            self.assertEqual(
                [item["id"] for item in runtime_plan["selected_packages"]], ["core", "codex"]
            )

            installed = self.run_cli("install", "--core-only", "--target-root", str(target))
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertEqual(json.loads(installed.stdout)["status"], "installed")
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertEqual(list((target / "skills").iterdir()), [])

    def test_lock_lifecycle_and_plan_freeze_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_plan_path = root / "install-plan.json"
            lock_path = root / "agent-skills.lock"
            planned = self.run_cli(
                "install", "--platform", "apple", "--target-root", str(target), "--dry-run"
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            install_plan_path.write_text(planned.stdout, encoding="utf-8")
            resolved = self.run_cli(
                "lock", "resolve", str(install_plan_path), "--output", str(lock_path)
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            lock = json.loads(resolved.stdout)
            self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8")), lock)
            validated = self.run_cli("lock", "validate", str(lock_path))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            malformed = dict(lock)
            malformed["packages"] = [dict(item) for item in lock["packages"]]
            malformed["packages"][0]["kind"] = []
            malformed_path = root / "malformed-agent-skills.lock"
            malformed_path.write_text(json.dumps(malformed), encoding="utf-8")
            rejected = self.run_cli("lock", "validate", str(malformed_path))
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(json.loads(rejected.stderr)["status"], "blocked")
            self.assertNotIn("Traceback", rejected.stderr)
            explained = self.run_cli("lock", "explain", str(lock_path))
            self.assertEqual(json.loads(explained.stdout)["lock_hash"], lock["fingerprint"])
            diffed = self.run_cli("lock", "diff", str(lock_path), str(lock_path))
            self.assertEqual(json.loads(diffed.stdout)["status"], "unchanged")
            workflow = self.run_cli(
                "plan", str(FIXTURES / "apple-app"), "--task", "实现 iOS 功能",
                "--lock", str(lock_path), "--dry-run",
            )
            self.assertEqual(workflow.returncode, 0, workflow.stderr)
            self.assertEqual(json.loads(workflow.stdout)["package_lock_hash"], lock["fingerprint"])
            workflow_path = root / "workflow-plan.json"
            workflow_path.write_text(workflow.stdout, encoding="utf-8")
            context_path = root / "context.json"
            context_path.write_text(json.dumps({
                "task": {"text": "实现 iOS 功能", "type": "code-medium", "risk": "medium"},
                "target_modules": ["."],
                "user_constraints": [],
                "checkpoints": {"CP0": "completed", "CP1": "in_progress", "CP2": "pending", "CP3": "pending"},
            }), encoding="utf-8")
            prepare_without_lock = self.run_cli(
                "prepare-adapter", str(workflow_path), "apple-2", "--context", str(context_path),
                "--invocation-id", "locked-prepare-1",
            )
            self.assertEqual(prepare_without_lock.returncode, 2)
            prepared = self.run_cli(
                "prepare-adapter", str(workflow_path), "apple-2", "--context", str(context_path),
                "--invocation-id", "locked-prepare-2", "--lock", str(lock_path),
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            missing_lock = self.run_cli(
                "run", str(workflow_path), "--ledger", str(root / "missing-lock.jsonl"), "--fake-adapters"
            )
            self.assertEqual(missing_lock.returncode, 2)
            self.assertIn("current package Lockfile", missing_lock.stderr)
            executed = self.run_cli(
                "run", str(workflow_path), "--ledger", str(root / "locked.jsonl"),
                "--lock", str(lock_path), "--fake-adapters",
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)


if __name__ == "__main__":
    unittest.main()
