from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests.support import FIXTURES, MANIFESTS, ROOT  # noqa: F401

from agent_workflow import canonical_json
from agent_workflow.adapters import (
    load_claim_token_file,
    validate_provider_invocation,
    validate_provider_invocation_selection,
)
from agent_workflow.adapters.invocations import _ensure_record_size
from agent_workflow.adapters.invocations import (
    _claim_provider_invocation_at as claim_provider_invocation,
)
from agent_workflow.adapters.invocations import (
    _collect_submitted_results_at as collect_submitted_results,
)
from agent_workflow.adapters.invocations import (
    _inspect_provider_invocation_at as inspect_provider_invocation,
)
from agent_workflow.adapters.invocations import (
    _prepare_provider_invocation_at as prepare_provider_invocation,
)
from agent_workflow.adapters.invocations import (
    _submit_provider_invocation_at as submit_provider_invocation,
)
from agent_workflow.canonical_json import dump, load
from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.registry import ManifestRegistry


class ProviderInvocationTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        self.plan = PlanCompiler(registry).compile(profile, policy)
        self.node_id = "apple-2"
        self.context = {
            "actors": {"implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            "checkpoints": {
                "CP0": "completed",
                "CP1": "in_progress",
                "CP2": "pending",
                "CP3": "pending",
            },
            "task": policy["task"],
        }
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "handoff"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, *, invocation_id: str = "provider-transport-1") -> dict[str, object]:
        return prepare_provider_invocation(
            self.root,
            self.plan,
            self.node_id,
            context=self.context,
            invocation_id=invocation_id,
            prepared_at=100,
        )

    @staticmethod
    def result(record: dict[str, object]) -> dict[str, object]:
        request = record["request"]
        return {
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
                "summary": "定向验证通过",
                "data": {"executed_validation": [{"kind": "unit-tests", "status": "passed"}]},
                "artifact_ids": [],
            }],
            "artifacts": [],
        }

    def test_prepare_freezes_permissions_without_executing_binding(self) -> None:
        record = self.prepare()
        validate_provider_invocation(record)
        node = next(node for node in self.plan["nodes"] if node["id"] == self.node_id)
        contract = record["execution_contract"]
        self.assertEqual(contract["permission_profile"], node["permission_profile"])
        self.assertEqual(contract["side_effects"], sorted(node["side_effects"]))
        self.assertEqual(contract["resource_keys"], sorted(node["resource_keys"]))
        self.assertEqual(contract["provider_manifest_digest"], node["provider_manifest_digest"])
        self.assertEqual(
            inspect_provider_invocation(
                self.root,
                record["request"]["request_id"],
                at=100,
            )["state"],
            "prepared",
        )

    def test_claim_hides_token_and_submit_feeds_recorded_results(self) -> None:
        record = self.prepare()
        request_id = record["request"]["request_id"]
        token = "claim-token-transport-0001-secure-random"
        claimed = claim_provider_invocation(
            self.root,
            request_id,
            actor_id="host-agent-1",
            claim_token=token,
            claimed_at=110,
        )
        self.assertNotIn(token, str(claimed))
        self.assertEqual(claimed["claim"]["deadline"], 110 + claimed["execution_contract"]["timeout_seconds"])
        self.assertEqual(
            inspect_provider_invocation(self.root, request_id, at=111)["state"],
            "claimed",
        )
        submitted = submit_provider_invocation(
            self.root,
            request_id,
            self.result(record),
            claim_token=token,
            submitted_at=120,
        )
        self.assertEqual(
            inspect_provider_invocation(self.root, request_id, at=10_000)["state"],
            "submitted",
        )
        self.assertEqual(
            collect_submitted_results(
                self.root,
                self.plan["fingerprint"],
                {
                    "schema_version": "1.0",
                    "plan_fingerprint": self.plan["fingerprint"],
                    "requests": {self.node_id: request_id},
                },
                at=120,
            ),
            {self.node_id: submitted["result"]},
        )

    def test_collection_requires_an_exact_submitted_request_selection(self) -> None:
        record = self.prepare()
        request_id = record["request"]["request_id"]
        token = "claim-token-transport-selection-0001"
        claim_provider_invocation(
            self.root,
            request_id,
            actor_id="host",
            claim_token=token,
            claimed_at=110,
        )
        submit_provider_invocation(
            self.root,
            request_id,
            self.result(record),
            claim_token=token,
            submitted_at=120,
        )
        selection = {
            "schema_version": "1.0",
            "plan_fingerprint": self.plan["fingerprint"],
            "requests": {self.node_id: request_id},
        }
        validate_provider_invocation_selection(selection)
        selected = collect_submitted_results(
            self.root,
            self.plan["fingerprint"],
            selection,
            at=120,
        )
        self.assertEqual(selected[self.node_id]["request_id"], request_id)
        retry = self.prepare(invocation_id="provider-transport-retry")
        retry_id = retry["request"]["request_id"]
        retry_token = "claim-token-transport-selection-retry-0002"
        claim_provider_invocation(
            self.root,
            retry_id,
            actor_id="retry-host",
            claim_token=retry_token,
            claimed_at=121,
        )
        submit_provider_invocation(
            self.root,
            retry_id,
            self.result(retry),
            claim_token=retry_token,
            submitted_at=122,
        )
        retry_selection = deepcopy(selection)
        retry_selection["requests"][self.node_id] = retry_id
        selected_retry = collect_submitted_results(
            self.root,
            self.plan["fingerprint"],
            retry_selection,
            at=122,
        )
        self.assertEqual(selected_retry[self.node_id]["request_id"], retry_id)
        selected_original = collect_submitted_results(
            self.root,
            self.plan["fingerprint"],
            selection,
            at=122,
        )
        self.assertEqual(selected_original[self.node_id]["request_id"], request_id)
        drifted = deepcopy(selection)
        drifted["requests"][self.node_id] = "adapter-request-0000000000000000"
        with self.assertRaisesRegex(ContractError, "not submitted"):
            collect_submitted_results(
                self.root,
                self.plan["fingerprint"],
                drifted,
                at=120,
            )

    def test_expiry_wrong_token_duplicate_and_identity_drift_fail_closed(self) -> None:
        record = self.prepare()
        request_id = record["request"]["request_id"]
        token = "claim-token-transport-0001-secure-random"
        claim = claim_provider_invocation(
            self.root,
            request_id,
            actor_id="host-agent-1",
            claim_token=token,
            claimed_at=110,
        )
        with self.assertRaisesRegex(ContractError, "cannot be claimed"):
            claim_provider_invocation(
                self.root,
                request_id,
                actor_id="host-agent-2",
                claim_token="claim-token-transport-0002-secure-random",
                claimed_at=111,
            )
        with self.assertRaisesRegex(ContractError, "does not match"):
            submit_provider_invocation(
                self.root,
                request_id,
                self.result(record),
                claim_token="claim-token-transport-wrong-secure-random",
                submitted_at=120,
            )
        with self.assertRaisesRegex(ContractError, "expired"):
            submit_provider_invocation(
                self.root,
                request_id,
                self.result(record),
                claim_token=token,
                submitted_at=claim["claim"]["deadline"],
            )
        path = self.root / f"{request_id}.json"
        tampered = load(path)
        tampered["execution_contract"]["permission_profile"] = "unrestricted"
        dump(tampered, path)
        with self.assertRaisesRegex(ContractError, "frozen identity"):
            inspect_provider_invocation(self.root, request_id, at=120)

    def test_exact_request_can_only_be_prepared_once(self) -> None:
        self.prepare()
        with self.assertRaisesRegex(ContractError, "already exists"):
            self.prepare()

    def test_resource_claims_conflict_and_approval_nodes_fail_closed(self) -> None:
        first = prepare_provider_invocation(
            self.root,
            self.plan,
            "apple-1",
            context=self.context,
            invocation_id="resource-claim-1",
            prepared_at=100,
        )
        claim_provider_invocation(
            self.root,
            first["request"]["request_id"],
            actor_id="host-resource-1",
            claim_token="resource-claim-token-0001-secure-random",
            claimed_at=110,
        )
        second = prepare_provider_invocation(
            self.root,
            self.plan,
            "apple-1",
            context=self.context,
            invocation_id="resource-claim-2",
            prepared_at=100,
        )
        with self.assertRaisesRegex(ContractError, "resource is already claimed"):
            claim_provider_invocation(
                self.root,
                second["request"]["request_id"],
                actor_id="host-resource-2",
                claim_token="resource-claim-token-0002-secure-random",
                claimed_at=111,
            )

        approval_plan = deepcopy(self.plan)
        node = next(node for node in approval_plan["nodes"] if node["id"] == self.node_id)
        node["approval"] = {
            "action": "execute-provider",
            "reason": "requires user approval",
            "scope": {"node_id": self.node_id},
        }
        with self.assertRaisesRegex(ContractError, "runtime-granted attempt proof"):
            prepare_provider_invocation(
                self.root,
                approval_plan,
                self.node_id,
                context=self.context,
                invocation_id="approval-bound-1",
                prepared_at=100,
            )

    def test_concurrent_claim_has_one_winner(self) -> None:
        record = self.prepare()
        request_id = record["request"]["request_id"]
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def claim(index: int) -> None:
            barrier.wait()
            try:
                claim_provider_invocation(
                    self.root,
                    request_id,
                    actor_id=f"host-{index}",
                    claim_token=f"claim-token-concurrent-{index:04d}-secure-random",
                    claimed_at=110,
                )
                outcomes.append("claimed")
            except ContractError:
                outcomes.append("blocked")

        threads = [threading.Thread(target=claim, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["blocked", "claimed"])

    @unittest.skipIf(os.name == "nt", "symlink creation policy differs on Windows")
    def test_symlink_root_and_entry_are_rejected(self) -> None:
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        symlink_root = Path(self.temporary.name) / "link"
        symlink_root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "root is unsafe"):
            prepare_provider_invocation(
                symlink_root,
                self.plan,
                self.node_id,
                context=self.context,
                invocation_id="symlink-root",
                prepared_at=100,
            )

        record = self.prepare()
        request_id = record["request"]["request_id"]
        entry = self.root / f"{request_id}.json"
        entry.unlink()
        entry.symlink_to(Path(self.temporary.name) / "missing.json")
        with self.assertRaisesRegex(ContractError, "unsafe"):
            inspect_provider_invocation(self.root, request_id, at=100)

    def test_missing_read_root_and_record_size_guard_are_non_mutating(self) -> None:
        missing = Path(self.temporary.name) / "missing-handoff"
        selection = {
            "schema_version": "1.0",
            "plan_fingerprint": self.plan["fingerprint"],
            "requests": {},
        }
        self.assertEqual(
            collect_submitted_results(
                missing,
                self.plan["fingerprint"],
                selection,
                at=100,
            ),
            {},
        )
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(ContractError, "root does not exist"):
            inspect_provider_invocation(
                missing,
                "adapter-request-0000000000000000",
                at=100,
            )
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(ContractError, "more than 4 bytes"):
            _ensure_record_size(b"12345", 4)
        with mock.patch.object(canonical_json, "MAX_CONTRACT_JSON_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "more than 4 bytes"):
                canonical_json.loads(b"\xff\xff\xff\xff\xff")

    @unittest.skipIf(os.name == "nt", "Unix mode contract")
    def test_broad_root_and_lock_permissions_are_rejected(self) -> None:
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o770)
        with self.assertRaisesRegex(ContractError, "root permissions"):
            self.prepare()
        self.root.chmod(0o700)
        record = self.prepare()
        (self.root / ".invocations.lock").chmod(0o660)
        with self.assertRaisesRegex(ContractError, "lock permissions"):
            claim_provider_invocation(
                self.root,
                record["request"]["request_id"],
                actor_id="host",
                claim_token="claim-token-permission-check-00000001",
                claimed_at=110,
            )

    def test_submission_rejects_result_identity_drift(self) -> None:
        record = self.prepare()
        request_id = record["request"]["request_id"]
        token = "claim-token-transport-0001-secure-random"
        claim_provider_invocation(
            self.root,
            request_id,
            actor_id="host-agent-1",
            claim_token=token,
            claimed_at=110,
        )
        result = deepcopy(self.result(record))
        result["provider"] = "other-provider"
        with self.assertRaisesRegex(ContractError, "does not match request"):
            submit_provider_invocation(
                self.root,
                request_id,
                result,
                claim_token=token,
                submitted_at=120,
            )

    def test_claim_token_file_is_bounded_single_line_and_no_follow(self) -> None:
        token_path = Path(self.temporary.name) / "token"
        token_path.write_text("private-claim-token-0001-secure-random\n", encoding="utf-8")
        token_path.chmod(0o600)
        self.assertEqual(
            load_claim_token_file(token_path),
            "private-claim-token-0001-secure-random",
        )
        token_path.write_text("private-claim-token-0001-secure-random\n\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "one line"):
            load_claim_token_file(token_path)
        token_path.write_bytes(b"x" * 4097)
        with self.assertRaisesRegex(ContractError, "too large"):
            load_claim_token_file(token_path)
        if os.name != "nt":
            target = Path(self.temporary.name) / "real-token"
            target.write_text("private-claim-token-0002-secure-random", encoding="utf-8")
            token_path.unlink()
            token_path.symlink_to(target)
            with self.assertRaisesRegex(ContractError, "unsafe"):
                load_claim_token_file(token_path)
