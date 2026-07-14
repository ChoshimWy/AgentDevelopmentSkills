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
            [sys.executable, "-m", "agent_workflow.cli", "--manifests", str(MANIFESTS), *args],
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


if __name__ == "__main__":
    unittest.main()
