from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from agent_workflow.canonical_json import dump, sha256
from agent_workflow.models import ContractError
from scripts.build_python_artifacts import build_python_artifacts
from scripts.python_compatibility_evidence import (
    SUPPORTED,
    _canonical_artifacts,
    merge,
    validate_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


def evidence(minor: str, *, artifact_suffix: str = "") -> dict:
    artifacts = [
        {
            "filename": "agent_development_skills-0.2.0.tar.gz",
            "kind": "sdist",
            "sha256": sha256({"artifact": "sdist" + artifact_suffix}),
            "size": 100,
        },
        {
            "filename": "agent_development_skills-0.2.0-py3-none-any.whl",
            "kind": "wheel",
            "sha256": sha256({"artifact": "wheel" + artifact_suffix}),
            "size": 200,
        },
    ]
    value = {
        "artifact_set_sha256": sha256(artifacts),
        "environments": [{
            "artifacts": artifacts,
            "implementation": "CPython",
            "machine": "x86_64",
            "pep517_wheel_sha256": artifacts[1]["sha256"],
            "platform": "linux",
            "python_minor": minor,
            "python_version": minor + ".1",
            "status": "passed",
            "test_count": 2,
        }],
        "schema_version": "1.0",
        "source_dirty": False,
        "source_revision": "1" * 40,
        "status": "partial",
    }
    value["fingerprint"] = sha256(value)
    return value


class PythonCompatibilityEvidenceTests(unittest.TestCase):
    def test_builder_output_is_normalized_before_evidence_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = _canonical_artifacts(
                build_python_artifacts(ROOT, Path(directory) / "artifacts")
            )
        self.assertEqual([item["kind"] for item in records], ["sdist", "wheel"])

    def test_complete_matrix_requires_all_supported_versions_and_exact_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for minor in SUPPORTED:
                path = root / f"python-{minor}.json"
                dump(evidence(minor), path)
                inputs.append(path)
            output = root / "matrix.json"
            result = merge(inputs, output)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                [item["python_minor"] for item in result["environments"]],
                list(SUPPORTED),
            )
            self.assertEqual(validate_evidence(result, require_complete=True), result)

    def test_missing_version_and_cross_version_artifact_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for minor in SUPPORTED[:-1]:
                path = root / f"python-{minor}.json"
                dump(evidence(minor), path)
                inputs.append(path)
            with self.assertRaisesRegex(ContractError, "exactly four"):
                merge(inputs, root / "incomplete.json")

            drift_inputs = []
            for minor in SUPPORTED:
                path = root / f"drift-{minor}.json"
                dump(
                    evidence(minor, artifact_suffix="-drift" if minor == "3.14" else ""),
                    path,
                )
                drift_inputs.append(path)
            with self.assertRaisesRegex(ContractError, "artifacts differ"):
                merge(drift_inputs, root / "drift.json")

    def test_fingerprint_and_pep517_binding_reject_self_consistent_tampering(self) -> None:
        value = evidence("3.11")
        value["environments"][0]["pep517_wheel_sha256"] = "f" * 64
        value["fingerprint"] = sha256({
            key: item for key, item in value.items() if key != "fingerprint"
        })
        with self.assertRaisesRegex(ContractError, "PEP 517 wheel differs"):
            validate_evidence(value)

        value = deepcopy(evidence("3.11"))
        value["fingerprint"] = "f" * 64
        with self.assertRaisesRegex(ContractError, "fingerprint mismatch"):
            validate_evidence(value)

    def test_dirty_evidence_cannot_be_merged_into_release_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for minor in SUPPORTED:
                value = evidence(minor)
                value["source_dirty"] = True
                value["fingerprint"] = sha256({
                    key: item for key, item in value.items() if key != "fingerprint"
                })
                path = root / f"python-{minor}.json"
                dump(value, path)
                inputs.append(path)
            with self.assertRaisesRegex(ContractError, "dirty source"):
                merge(inputs, root / "matrix.json")

    def test_cross_source_duplicate_minor_and_reversed_artifacts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for index, minor in enumerate(SUPPORTED):
                value = evidence(minor)
                if index == 3:
                    value["source_revision"] = "2" * 40
                    value["fingerprint"] = sha256({
                        key: item for key, item in value.items() if key != "fingerprint"
                    })
                path = root / f"source-{minor}.json"
                dump(value, path)
                inputs.append(path)
            with self.assertRaisesRegex(ContractError, "source revisions differ"):
                merge(inputs, root / "source-drift.json")

            duplicate_inputs = []
            for index in range(4):
                path = root / f"duplicate-{index}.json"
                dump(evidence("3.11"), path)
                duplicate_inputs.append(path)
            with self.assertRaisesRegex(ContractError, "sorted and unique"):
                merge(duplicate_inputs, root / "duplicate.json")

            reversed_value = evidence("3.11")
            reversed_value["environments"][0]["artifacts"].reverse()
            reversed_value["fingerprint"] = sha256({
                key: item for key, item in reversed_value.items() if key != "fingerprint"
            })
            with self.assertRaisesRegex(ContractError, "canonically sorted"):
                validate_evidence(reversed_value)


if __name__ == "__main__":
    unittest.main()
