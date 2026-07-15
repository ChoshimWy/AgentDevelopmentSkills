from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "platforms/apple/skills/apple-verification/scripts"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fingerprint = load_module("fingerprint")
session_store = load_module("session_store")
evidence_cache = load_module("evidence_cache")
affected_tests = load_module("affected_tests")
verification_coordinator = load_module("verification_coordinator")
build_check = load_module("build_check")


class FingerprintTests(unittest.TestCase):
    def test_target_fingerprint_is_stable_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Sources/Feature.swift"
            source.parent.mkdir(parents=True)
            source.write_text("let value = 1\n", encoding="utf-8")
            first = fingerprint.target_source_fingerprint(root, "Feature", ["Sources/Feature.swift"])
            second = fingerprint.target_source_fingerprint(root, "Feature", ["Sources/Feature.swift"])
            self.assertEqual(first, second)
            source.write_text("let value = 2\n", encoding="utf-8")
            self.assertNotEqual(first, fingerprint.target_source_fingerprint(root, "Feature", ["Sources/Feature.swift"]))

    def test_fingerprint_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                fingerprint.file_inventory(Path(temporary), ["../outside"])


class SessionTests(unittest.TestCase):
    def test_session_is_canonical_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = session_store.SessionStore(Path(temporary), "feature-1")
            created = store.create(
                base_commit="abc",
                current_diff_hash="diff",
                environment_fingerprint="env:123",
                project={"scheme": "App"},
            )
            self.assertEqual(created, store.load())
            raw = store.path.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("\n"))
            self.assertEqual(raw, json.dumps(created, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def test_session_id_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                session_store.SessionStore(Path(temporary), "../escape")


class EvidenceTests(unittest.TestCase):
    def test_same_or_stronger_evidence_requires_fresh_identity_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            report.write_text("{}\n", encoding="utf-8")
            artifact = {"uri": "report.json", "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
            requirement = {
                "environment_fingerprint": "env:1",
                "current_diff_hash": "diff:1",
                "source_fingerprints": ["target:1"],
                "minimum_capabilities": ["compile", "test"],
                "identity": {"selectors": ["FeatureTests"]},
            }
            weaker = {**requirement, "status": "passed", "capabilities": ["compile"], "artifacts": [artifact]}
            stronger = {
                **requirement,
                "identity": {"selectors": ["FeatureTests", "OtherTests"]},
                "status": "passed",
                "capabilities": ["compile", "test", "consumer"],
                "artifacts": [artifact],
            }
            self.assertEqual(stronger, evidence_cache.reusable_evidence(requirement, [weaker, stronger], root))
            self.assertIsNone(evidence_cache.reusable_evidence({**requirement, "current_diff_hash": "diff:2"}, [stronger], root))
            report.write_text("changed\n", encoding="utf-8")
            self.assertIsNone(evidence_cache.reusable_evidence(requirement, [stronger], root))

    def test_only_deterministic_failures_are_reusable(self) -> None:
        self.assertTrue(
            evidence_cache.deterministic_failure_reusable(
                {"fingerprint": "f", "classification": "compile", "retryable": False}, "f"
            )
        )
        self.assertFalse(
            evidence_cache.deterministic_failure_reusable(
                {"fingerprint": "f", "classification": "environment", "retryable": True}, "f"
            )
        )


class PlannerTests(unittest.TestCase):
    def test_changed_paths_combines_worktree_index_and_untracked_names(self) -> None:
        outputs = {
            ("diff", "--name-only"): "Sources/Renamed.swift\n",
            ("diff", "--name-only", "--cached"): "Tests/RenamedTests.swift\n",
            ("ls-files", "--others", "--exclude-standard"): "Fixtures/new.json\nSources/Renamed.swift\n",
        }
        with mock.patch.object(verification_coordinator, "git_output", side_effect=lambda _root, *args: outputs[args]):
            self.assertEqual(
                ["Fixtures/new.json", "Sources/Renamed.swift", "Tests/RenamedTests.swift"],
                verification_coordinator.changed_paths(Path(".")),
            )

    def test_affected_tests_use_basename_and_domain_rules(self) -> None:
        result = affected_tests.affected_tests(
            ["Sources/DeviceControlViewModel.swift", "Sources/Subscription/ReceiptService.swift"]
        )
        self.assertIn("DeviceControlViewModelTests", result["selectors"])
        self.assertIn("ReceiptServiceTests", result["selectors"])
        self.assertIn("EntitlementTests", result["selectors"])

    def test_ui_diff_requires_compile_and_ui_evidence(self) -> None:
        plan = verification_coordinator.evidence_plan(["Sources/DeviceControlView.swift"])
        ids = {item["evidence_id"] for item in plan["required_evidence"]}
        self.assertIn("compile:affected-target", ids)
        self.assertIn("ui:scenario-required", ids)

    def test_asset_dependency_release_and_unknown_diffs_fail_closed(self) -> None:
        asset = verification_coordinator.evidence_plan(["Assets/fixture.json"])
        self.assertIn("resource:integrity", {item["evidence_id"] for item in asset["required_evidence"]})
        dependency = verification_coordinator.evidence_plan(["Package.swift"])
        self.assertEqual("checkpoint", dependency["lane"])
        self.assertIn("dependency:resolve", {item["evidence_id"] for item in dependency["required_evidence"]})
        release = verification_coordinator.evidence_plan(["App/App.entitlements"])
        self.assertEqual("final", release["lane"])
        self.assertIn("release:configuration", {item["evidence_id"] for item in release["required_evidence"]})
        unknown = verification_coordinator.evidence_plan(["Scripts/generated.opaque"])
        self.assertEqual("blocked", unknown["status"])
        self.assertTrue(unknown["required_evidence"])
        combined = verification_coordinator.evidence_plan(["App.xcodeproj/project.pbxproj", "Package.resolved"])
        ids = [item["evidence_id"] for item in combined["required_evidence"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rule_diff_routes_to_policy_lint(self) -> None:
        plan = verification_coordinator.evidence_plan(["skills/apple-verification/SKILL.md"])
        self.assertEqual("dev", plan["lane"])
        self.assertEqual("policy-lint:current-diff", plan["required_evidence"][0]["evidence_id"])

    def test_source_below_a_directory_named_skills_is_not_rule_only(self) -> None:
        self.assertEqual("swift-small", verification_coordinator.classify("Sources/Skills/Feature.swift"))


class WrapperPolicyTests(unittest.TestCase):
    def test_queue_doctor_is_read_only_and_rejects_forged_live_active_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            absent_queue = root / "absent-queue"
            env = {**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(absent_queue), "HOME": str(root / "home")}
            absent = subprocess.run(
                ["bash", str(wrapper), "--queue-doctor"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, absent.returncode)
            self.assertFalse(absent_queue.exists())

            queue = root / "live-queue"
            running = queue / "jobs" / "job-1"
            running.mkdir(parents=True)
            (running / "state").write_text("running\n", encoding="utf-8")
            (queue / "queue-meta.json").write_text(
                '{"generation_id":"codex-verify-v2","producer":"codex_verify","schema_version":"2.0"}\n',
                encoding="utf-8",
            )
            (queue / "daemon.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            (queue / "active_job").write_text(f"{running}\n", encoding="utf-8")
            env["CODEX_BUILD_QUEUE_ROOT"] = str(queue)
            live = subprocess.run(
                ["bash", str(wrapper), "--queue-doctor"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, live.returncode, live.stderr)
            report = json.loads(live.stdout.strip().splitlines()[-1])
            self.assertFalse(report["healthy"])
            self.assertIn("daemon-identity-invalid", {item["code"] for item in report["issues"]})
            self.assertIn("running-job-interrupted", {item["code"] for item in report["issues"]})
            self.assertEqual("running", (running / "state").read_text(encoding="utf-8").strip())

    def test_queue_status_json_propagates_configuration_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            result = subprocess.run(
                ["bash", str(wrapper), "--queue-status", "--json"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "CODEX_BUILD_QUEUE_ROOT": str(root / "queue"),
                    "CODEX_VERIFY_QUEUE_TTL_SECONDS": "invalid",
                    "HOME": str(root / "home"),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(result.stdout.strip())
            self.assertIn("must be an integer", result.stderr)

    def test_concurrent_direct_daemon_start_has_exactly_one_live_owner(self) -> None:
        import signal
        import time

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            env = {**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")}
            processes = [
                subprocess.Popen(
                    ["bash", str(wrapper), "--daemon"],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]

            def stop_processes() -> None:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    if process.stderr is not None:
                        process.stderr.close()

            self.addCleanup(stop_processes)
            deadline = time.time() + 10
            while time.time() < deadline:
                if (queue / "daemon.pid").is_file() and sum(process.poll() is None for process in processes) == 1:
                    break
                time.sleep(0.05)
            alive = [process for process in processes if process.poll() is None]
            self.assertEqual(1, len(alive))
            recorded_pid = int((queue / "daemon.pid").read_text(encoding="utf-8").strip())
            self.assertEqual(alive[0].pid, recorded_pid)
            owner = json.loads((queue / "daemon-owner.json").read_text(encoding="utf-8"))
            self.assertEqual(recorded_pid, owner["pid"])
            os.kill(recorded_pid, signal.SIGTERM)
            alive[0].wait(timeout=5)

    def test_missing_metadata_with_live_legacy_runtime_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            (queue / "jobs" / "legacy").mkdir(parents=True)
            (queue / "jobs" / "legacy" / "state").write_text("queued\n", encoding="utf-8")
            sleeper = subprocess.Popen(["sleep", "30"])
            def stop_sleeper() -> None:
                if sleeper.poll() is None:
                    sleeper.terminate()
                    sleeper.wait(timeout=5)
            self.addCleanup(stop_sleeper)
            (queue / "daemon.pid").write_text(f"{sleeper.pid}\n", encoding="utf-8")
            script = root / "check.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            result = subprocess.run(
                ["bash", str(wrapper), "--build-check", str(script), str(root)],
                cwd=ROOT,
                env={**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("live legacy daemon", result.stderr)
            self.assertFalse((queue / "queue-meta.json").exists())
            self.assertEqual(["legacy"], sorted(path.name for path in (queue / "jobs").iterdir()))

    def test_repair_never_follows_active_or_lease_paths_outside_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            external = root / "external-job"
            external.mkdir()
            (external / "state").write_text("running\n", encoding="utf-8")
            queue.mkdir()
            (queue / "queue-meta.json").write_text(
                '{"generation_id":"codex-verify-v2","producer":"codex_verify","schema_version":"2.0"}\n',
                encoding="utf-8",
            )
            (queue / "active_job").write_text(f"{external}\n", encoding="utf-8")
            lease = queue / "derived-data-slots" / "slot" / "test" / "lease.lockdir"
            lease.mkdir(parents=True)
            (lease / "job_dir").write_text(f"{external}\n", encoding="utf-8")
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            result = subprocess.run(
                ["bash", str(wrapper), "--queue-doctor", "--repair", "--delete-invalid"],
                cwd=ROOT,
                env={**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("running", (external / "state").read_text(encoding="utf-8").strip())
            self.assertFalse((queue / "active_job").exists())
            self.assertFalse(lease.exists())

    def test_queue_doctor_fail_closes_and_can_delete_unsafe_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            missing = queue / "jobs" / "000-missing"
            missing.mkdir(parents=True)
            stale = queue / "jobs" / "001-stale"
            stale.mkdir()
            (stale / "state").write_text("queued\n", encoding="utf-8")
            terminal = queue / "jobs" / "002-terminal"
            terminal.mkdir()
            (terminal / "state").write_text("succeeded\n", encoding="utf-8")
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            env = {**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")}

            diagnosed = subprocess.run(
                ["bash", str(wrapper), "--queue-doctor"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, diagnosed.returncode, diagnosed.stderr)
            diagnosis = json.loads(diagnosed.stdout.strip().splitlines()[-1])
            self.assertFalse(diagnosis["healthy"])
            self.assertIn("job-state-missing", {item["code"] for item in diagnosis["issues"]})
            self.assertIn("queued-job-invalid", {item["code"] for item in diagnosis["issues"]})

            repaired = subprocess.run(
                ["bash", str(wrapper), "--queue-doctor", "--repair", "--delete-invalid"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, repaired.returncode, repaired.stderr)
            self.assertFalse(missing.exists())
            self.assertFalse(stale.exists())
            self.assertTrue(terminal.exists())

            healthy = subprocess.run(
                ["bash", str(wrapper), "--queue-status", "--json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, healthy.returncode, healthy.stderr)
            status = json.loads(healthy.stdout.strip().splitlines()[-1])
            self.assertTrue(status["healthy"])
            self.assertEqual({"succeeded": 1}, status["state_counts"])

    def test_incompatible_queue_generation_blocks_before_job_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            queue.mkdir()
            (queue / "queue-meta.json").write_text(
                '{"generation_id":"legacy","producer":"codex_verify","schema_version":"1.0"}\n',
                encoding="utf-8",
            )
            build_check = root / "build-check.sh"
            build_check.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            build_check.chmod(0o755)
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            env = {**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")}
            blocked = subprocess.run(
                ["bash", str(wrapper), "--build-check", str(build_check), str(root)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn("queue schema or generation is incompatible", blocked.stderr)
            self.assertEqual([], list((queue / "jobs").iterdir()))

    def test_live_repair_is_refused_and_tampered_queued_job_unblocks_waiter(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            slow = root / "slow.sh"
            slow.write_text("#!/bin/sh\nsleep 3\nexit 0\n", encoding="utf-8")
            slow.chmod(0o755)
            quick = root / "quick.sh"
            quick.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            quick.chmod(0o755)
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            env = {**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")}

            first = subprocess.Popen(
                ["bash", str(wrapper), "--build-check", str(slow), str(root)],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: first.poll() is None and first.terminate())

            deadline = time.time() + 10
            running = None
            while time.time() < deadline:
                for candidate in (queue / "jobs").glob("*") if (queue / "jobs").is_dir() else []:
                    if (candidate / "state").read_text(encoding="utf-8").strip() == "running":
                        running = candidate
                        break
                if running is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(running)

            repair = subprocess.run(
                ["bash", str(wrapper), "--queue-doctor", "--repair"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(75, repair.returncode, repair.stderr)
            self.assertEqual("running", (running / "state").read_text(encoding="utf-8").strip())

            second = subprocess.Popen(
                ["bash", str(wrapper), "--build-check", str(quick), str(root)],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: second.poll() is None and second.terminate())
            queued = None
            deadline = time.time() + 10
            while time.time() < deadline:
                candidates = [path for path in (queue / "jobs").glob("*") if path != running]
                for candidate in candidates:
                    if (candidate / "state").read_text(encoding="utf-8").strip() == "queued":
                        queued = candidate
                        break
                if queued is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(queued)
            with (queued / "command.args0").open("ab") as stream:
                stream.write(b"tampered\0")

            _first_stdout, first_stderr = first.communicate(timeout=15)
            second_stdout, second_stderr = second.communicate(timeout=15)
            self.assertEqual(0, first.returncode, first_stderr)
            self.assertEqual(70, second.returncode, second_stdout + second_stderr)
            self.assertIn("invalid, missing, or quarantined", second_stderr)
            self.assertFalse(queued.exists())
            self.assertTrue(any((queue / "quarantine").rglob(queued.name)))

            daemon_pid = int((queue / "daemon.pid").read_text(encoding="utf-8").strip())
            os.kill(daemon_pid, 15)

    def test_legacy_succeeded_job_without_v2_manifest_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            script = root / "check.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            subprocess.run(["git", "add", "check.sh"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test(queue): [Codex-GENERATED] 创建队列测试仓库"],
                cwd=root,
                check=True,
            )
            queue = root / "queue"
            wrapper = ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh"
            env = {**os.environ, "CODEX_BUILD_QUEUE_ROOT": str(queue), "HOME": str(root / "home")}
            command = ["bash", str(wrapper), "--build-check", str(script), str(root)]
            first = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            first_job = Path(json.loads(first.stdout.strip().splitlines()[-1])["queue_job_dir"])
            (first_job / "job-manifest.json").unlink()
            (first_job / "job_manifest_sha256").unlink()
            second = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(0, second.returncode, second.stderr)
            second_job = Path(json.loads(second.stdout.strip().splitlines()[-1])["queue_job_dir"])
            self.assertNotEqual(first_job, second_job)
            daemon_pid = int((queue / "daemon.pid").read_text(encoding="utf-8").strip())
            os.kill(daemon_pid, 15)

    def test_build_check_blocks_worktree_test_without_immutable_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"CODEX_WORKTREE_SESSION_ID": "session"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "immutable build artifact identity"):
                    build_check.resolve_build_config(Path(temporary), ["test-without-building"])

    def test_build_check_injects_frozen_test_plan_for_test_actions_only(self) -> None:
        config = build_check.BuildConfig(
            root=Path("/tmp/project"),
            workspace="App.xcworkspace",
            project=None,
            scheme="App",
            configuration="Debug",
            action="test",
            destination="platform=iOS Simulator,id=fake",
            test_plan="AppTests",
            device_fallback_enabled=False,
            explicit_device_id=None,
            explicit_device_name=None,
            preferred_model=None,
            validation_platform="simulator",
            show_output=False,
            ui_smoke_mode="off",
            ui_smoke_spec=".codex/ui-smoke.yml",
            derived_data_path=None,
            derived_data_mode=None,
            artifacts_dir=Path("/tmp/artifacts"),
            formatter_preference="off",
            tool_install_policy="never",
            tool_install_overrides={},
        )
        test_command = config.command_for_destination(config.destination)
        self.assertEqual("AppTests", test_command[test_command.index("-testPlan") + 1])
        config.action = "build"
        self.assertNotIn("-testPlan", config.command_for_destination(config.destination))

    def test_wrapper_contains_fingerprint_dedupe_and_cache_controls(self) -> None:
        wrapper = (ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh").read_text(encoding="utf-8")
        for marker in (
            "compute_request_fingerprint",
            "request_fingerprint",
            "queue_or_reuse_job",
            "MATCHING_JOB_KIND='attached'",
            "MATCHING_JOB_KIND='cached'",
            "QUEUE_SCHEMA_VERSION='2.0'",
            "job_ready_for_execution",
            "queue_maintenance",
            "--queue-doctor",
            "--force",
            "--no-cache",
        ):
            self.assertIn(marker, wrapper)


if __name__ == "__main__":
    unittest.main()
