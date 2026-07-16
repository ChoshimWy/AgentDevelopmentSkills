from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.support import ROOT


class DesktopInstalledWorkflowSmokeTests(unittest.TestCase):
    def test_installed_desktop_workflow_is_ready_without_apple_side_effects(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = f"{ROOT / 'src'}:{ROOT}"
        result = subprocess.run(
            [sys.executable, "scripts/run_desktop_installed_workflow_smoke.py"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["platform"], "desktop")
        self.assertEqual(report["installed"]["selected_platforms"], ["desktop"])
        self.assertNotIn("apple", report["installed"]["selected_packages"])
        self.assertEqual(report["workflow"], {
            "final_status": "completed",
            "plan_status": "ready",
            "review_status": "passed",
        })


if __name__ == "__main__":
    unittest.main()
