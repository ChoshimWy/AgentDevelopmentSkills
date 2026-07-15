from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS, ROOT
from agent_workflow.cli import default_manifest_directory


class CLITests(unittest.TestCase):
    def test_source_checkout_default_manifests_exist(self) -> None:
        self.assertEqual(default_manifest_directory(), MANIFESTS)
        self.assertTrue((default_manifest_directory() / "core" / "manifest.json").exists())

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


if __name__ == "__main__":
    unittest.main()
