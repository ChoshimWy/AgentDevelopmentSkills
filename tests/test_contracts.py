from __future__ import annotations

import math
import unittest

from tests.support import ROOT  # noqa: F401

from agent_workflow.canonical_json import dumps, sha256
from agent_workflow.contracts import validate
from agent_workflow.models import ContractError


class CanonicalJSONTests(unittest.TestCase):
    def test_encoding_is_sorted_compact_and_utf8(self) -> None:
        self.assertEqual(dumps({"z": "中文", "a": 1}), '{"a":1,"z":"中文"}\n')
        self.assertEqual(sha256({"b": 2, "a": 1}), sha256({"a": 1, "b": 2}))

    def test_nan_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dumps({"value": math.nan})


class ContractTests(unittest.TestCase):
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
