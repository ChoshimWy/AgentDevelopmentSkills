from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.support import ROOT


class IOSInstalledWorkflowSmokeTests(unittest.TestCase):
    def test_installed_apple_workflow_completes_and_other_platforms_stay_deferred(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "scripts/run_ios_installed_workflow_smoke.py"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["installed"]["selected_platforms"], ["apple"])
        self.assertEqual(report["workflow"]["detected_platforms"], ["apple"])
        self.assertEqual(report["workflow"]["plan_status"], "ready")
        self.assertIn("verification.apple.auto", report["workflow"]["capabilities"])
        self.assertEqual(report["workflow"]["final_status"], "completed")
        self.assertEqual(report["workflow"]["review_status"], "passed")
        self.assertEqual(set(report["deferred_platforms"].values()), {"bootstrap-only"})


if __name__ == "__main__":
    unittest.main()
