from __future__ import annotations

import math
import unittest

from tests.support import ROOT  # noqa: F401

from agent_workflow.canonical_json import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
    dumps,
    sha256,
)
from agent_workflow.contracts import validate
from agent_workflow.models import ContractError


class CanonicalJSONTests(unittest.TestCase):
    def test_encoding_is_sorted_compact_and_utf8(self) -> None:
        self.assertEqual(dumps({"z": "中文", "a": 1}), '{"a":1,"z":"中文"}\n')
        self.assertEqual(sha256({"b": 2, "a": 1}), sha256({"a": 1, "b": 2}))

    def test_nan_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dumps({"value": math.nan})

    def test_integer_and_nesting_limits_are_fail_closed(self) -> None:
        self.assertTrue(
            dumps({"value": int("9" * MAX_CANONICAL_INTEGER_DIGITS)})
        )
        with self.assertRaisesRegex(ValueError, "maximum"):
            dumps({"value": 10 ** MAX_CANONICAL_INTEGER_DIGITS})

        value: object = 0
        for _ in range(MAX_CANONICAL_JSON_DEPTH):
            value = [value]
        self.assertTrue(dumps(value))
        value = [value]
        with self.assertRaisesRegex(ValueError, "nesting depth"):
            dumps(value)


class ContractTests(unittest.TestCase):
    def test_platform_implementation_status_and_bootstrap_plan_fail_closed(self) -> None:
        manifest = {
            "bindings": {},
            "capabilities": [],
            "detection": {"medium": [], "strong": [], "weak": []},
            "id": "fixture",
            "implementation_status": "pretend-ready",
            "kind": "platform",
            "schema_version": "1.0",
        }
        with self.assertRaisesRegex(ContractError, "implementation_status"):
            validate("plugin-manifest", manifest)
        bootstrap = {
            **manifest,
            "implementation_status": "bootstrap-only",
            "role": "bootstrap",
            "provider_contract": {
                "advisory_capabilities": [],
                "allowed_permission_profiles": ["repository-read-only"],
                "allowed_side_effects": [],
                "capability_permissions": {"analysis.fixture": "repository-read-only"},
                "capability_side_effects": {"analysis.fixture": []},
                "optional_capabilities": [],
                "package_compatibility": ">=0.1.0 <0.2.0",
                "package_id": "fixture-agent-skills",
                "required_capabilities": ["analysis.fixture"],
            },
        }
        mutations = (
            lambda value: value.update({"role": "builtin"}),
            lambda value: value.update({"capabilities": [{"id": "analysis.fixture"}]}),
            lambda value: value.update({"bindings": {"analysis.fixture": "fake"}}),
            lambda value: value.update({"installation": {"asset_roots": [], "instruction_fragments": [], "skill_roots": []}}),
        )
        from copy import deepcopy
        for mutate in mutations:
            value = deepcopy(bootstrap)
            mutate(value)
            with self.assertRaisesRegex(ContractError, "bootstrap-only platform"):
                validate("plugin-manifest", value)
        implemented = deepcopy(bootstrap)
        implemented["implementation_status"] = "implemented"
        with self.assertRaisesRegex(ContractError, "implemented platform"):
            validate("plugin-manifest", implemented)
        with self.assertRaisesRegex(ContractError, "must block execution"):
            validate(
                "workflow-plan",
                {
                    "bootstrap_required": [{
                        "package_compatibility": ">=0.1.0 <0.2.0",
                        "platform": "android",
                        "provider": "android-agent-skills",
                        "required_capabilities": ["analysis.android"],
                    }],
                    "edges": [],
                    "fingerprint": "bootstrap",
                    "nodes": [],
                    "plan_id": "bootstrap",
                    "schema_version": "1.0",
                    "status": "ready",
                },
            )

    def test_additive_phase_2_fields_keep_v1_artifacts_compatible(self) -> None:
        validate(
            "workflow-plan",
            {
                "edges": [],
                "fingerprint": "legacy-plan",
                "nodes": [],
                "plan_id": "legacy-plan",
                "schema_version": "1.0",
                "status": "ready",
            },
        )
        validate(
            "run-ledger",
            {
                "approval_records": [],
                "final_status": "completed",
                "node_attempts": [],
                "plan_fingerprint": "legacy-plan",
                "resource_events": [],
                "run_id": "legacy-run",
                "schema_version": "1.0",
            },
        )

    def test_project_profile_requires_nested_repository_root(self) -> None:
        with self.assertRaises(ContractError):
            validate(
                "project-profile",
                {"schema_version": "1.0", "repository": {"kind": "single"}, "platforms": [], "modules": [], "ambiguities": []},
            )

    def test_policy_requires_complete_decision_contract(self) -> None:
        with self.assertRaises(ContractError):
            validate(
                "resolved-policy",
                {
                    "schema_version": "1.0", "fingerprint": "f", "selected_platforms": [],
                    "task": {"text": "x", "type": "review-only", "risk": "low", "disciplines": []},
                    "constraints": {},
                    "decisions": [{"decision": "x", "reason_code": "r", "source": "s", "confidence": 1.0}],
                },
            )
    def test_unknown_kind_fails_closed(self) -> None:
        with self.assertRaises(ContractError):
            validate("unknown", {})

    def test_invalid_schema_version_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate(
                "project-profile",
                {"ambiguities": [], "modules": [], "platforms": [], "repository": {"kind": "unknown"}, "schema_version": "2.0"},
            )

    def test_workflow_cycle_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate(
                "workflow-plan",
                {
                    "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
                    "fingerprint": "x",
                    "nodes": [
                        {"capability": "a", "id": "a", "mandatory": True, "max_retries": 0, "status": "pending", "timeout_seconds": 300},
                        {"capability": "b", "id": "b", "mandatory": True, "max_retries": 0, "status": "pending", "timeout_seconds": 300},
                    ],
                    "plan_id": "cycle",
                    "schema_version": "1.0",
                    "status": "ready",
                },
            )


if __name__ == "__main__":
    unittest.main()
