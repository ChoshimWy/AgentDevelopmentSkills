from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_workflow.canonical_json import dump, load, sha256
from agent_workflow.installation import build_install_bundle
from agent_workflow.models import ContractError
from scripts import prepare_release_qualification as qualification
from scripts.prepare_release_qualification import prepare, validate_handoff
from tests.test_release_gate import (
    FIXTURE_SOURCE_REVISION,
    ROOT,
    build_fixture_release,
    builder,
    review_trust_store,
    write_gate_prerequisites,
)


def build_clean_release(release: Path) -> None:
    with mock.patch.object(
        builder,
        "_source_identity",
        return_value=(FIXTURE_SOURCE_REVISION, False),
    ), mock.patch.object(builder, "_git_file_modes", return_value={}), mock.patch.object(
        builder,
        "_git_blob",
        side_effect=lambda root, relative: (root / relative).read_bytes(),
    ):
        builder.build_release_bundle(
            ROOT,
            release,
            allow_dirty=False,
            channel="beta",
        )


def write_candidate_lock(path: Path) -> Path:
    candidate = build_install_bundle(
        ROOT / "platforms",
        platforms=["apple", "desktop"],
        schema_root=ROOT / "schemas",
    ).package_lock
    dump(candidate, path)
    return path


def refresh_handoff(root: Path) -> None:
    value = load(root / "handoff.json")
    value["files"] = qualification._records(root)
    value["fingerprint"] = sha256({
        key: item for key, item in value.items() if key != "fingerprint"
    })
    dump(value, root / "handoff.json")


def rebind_conformance(path: Path, candidate_hash: str) -> dict:
    value = load(path)
    value["candidate_package_lock_hash"] = candidate_hash
    value["attestation_key"] = sha256({
        **{
            key: item
            for key, item in value.items()
            if key not in {"attestation_key", "fingerprint"}
        },
        "command_results": [
            {"command": item["command"], "exit_code": item["exit_code"]}
            for item in value["command_results"]
        ],
    })
    value["fingerprint"] = sha256({
        key: item for key, item in value.items() if key != "fingerprint"
    })
    dump(value, path)
    return value


class ReleaseQualificationTests(unittest.TestCase):
    def test_ci_dispatch_freezes_handoff_without_shell_interpolating_inputs(self) -> None:
        workflow = (ROOT / ".github/workflows/conformance.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("release-qualification-handoff:", workflow)
        self.assertIn("needs: python-compatibility-matrix", workflow)
        self.assertIn("REVIEW_KEY_ID: ${{ inputs.review_key_id }}", workflow)
        self.assertIn('RELEASE_CHANNEL: ${{ inputs.release_channel }}', workflow)
        self.assertIn('--review-key-id "$REVIEW_KEY_ID"', workflow)
        self.assertIn('--channel "$RELEASE_CHANNEL"', workflow)
        self.assertIn('--candidate-lock "$RUNNER_TEMP/qualification/agent-skills.lock"', workflow)
        self.assertNotIn('--review-key-id "${{ inputs.review_key_id }}"', workflow)
        self.assertNotIn('--channel "${{ inputs.release_channel }}"', workflow)

    def test_clean_handoff_is_exact_validatable_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release-input"
            build_clean_release(release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            key_id = review_trust_store()["keys"][0]["key_id"]
            output = root / "handoff"

            result = prepare(release, candidate, evidence, compatibility, key_id, output)

            self.assertEqual(result["status"], "awaiting-external-signature")
            self.assertEqual(validate_handoff(output), result)
            self.assertEqual(
                set(result["preflight_blockers"]),
                {
                    "release.conformance",
                    "release.independent-review",
                    "release.license-notice",
                    "release.python-distribution",
                },
            )
            self.assertEqual(
                load(output / "release-review-draft.json")["signature"]["value_hex"],
                "",
            )
            self.assertFalse(any(path.is_symlink() for path in output.rglob("*")))

            payload = output / "release-review-payload.json"
            payload.write_bytes(payload.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ContractError, "files differ"):
                validate_handoff(output)

    def test_dirty_candidate_and_incomplete_matrix_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dirty_release = root / "dirty-release"
            build_fixture_release(dirty_release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, dirty_release)
            candidate = write_candidate_lock(root / "candidate.json")
            key_id = review_trust_store()["keys"][0]["key_id"]
            with self.assertRaisesRegex(ContractError, "clean beta/stable"):
                prepare(
                    dirty_release,
                    candidate,
                    evidence,
                    compatibility,
                    key_id,
                    root / "dirty-handoff",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_clean_release(release)
            evidence, compatibility_path, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            compatibility = load(compatibility_path)
            compatibility["environments"].pop()
            compatibility["status"] = "partial"
            compatibility["fingerprint"] = sha256({
                key: item for key, item in compatibility.items() if key != "fingerprint"
            })
            dump(compatibility, compatibility_path)
            with self.assertRaisesRegex(ContractError, "incomplete"):
                prepare(
                    release,
                    candidate,
                    evidence,
                    compatibility_path,
                    review_trust_store()["keys"][0]["key_id"],
                    root / "incomplete-handoff",
                )

    def test_output_must_be_new_and_unexpected_supply_blocker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_clean_release(release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            key_id = review_trust_store()["keys"][0]["key_id"]
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(ContractError, "must not already exist"):
                prepare(release, candidate, evidence, compatibility, key_id, existing)

            (release / "install.sh").write_bytes(b"tampered bootstrap\n")
            with self.assertRaisesRegex(ContractError, "unexpected blockers"):
                prepare(
                    release, candidate, evidence, compatibility, key_id, root / "tampered"
                )

    def test_self_consistent_preflight_identity_and_extra_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_clean_release(release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            key_id = review_trust_store()["keys"][0]["key_id"]
            output = root / "handoff"
            prepare(release, candidate, evidence, compatibility, key_id, output)

            preflight_path = output / "release-gate-preflight.json"
            preflight = load(preflight_path)
            preflight["release_identity_sha256"] = "f" * 64
            preflight["fingerprint"] = sha256({
                key: item for key, item in preflight.items() if key != "fingerprint"
            })
            dump(preflight, preflight_path)
            handoff = load(output / "handoff.json")
            handoff["preflight_gate_fingerprint"] = preflight["fingerprint"]
            dump(handoff, output / "handoff.json")
            refresh_handoff(output)
            with self.assertRaisesRegex(ContractError, "fresh evaluation"):
                validate_handoff(output)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_clean_release(release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            output = root / "handoff"
            prepare(
                release,
                candidate,
                evidence,
                compatibility,
                review_trust_store()["keys"][0]["key_id"],
                output,
            )
            (output / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
            refresh_handoff(output)
            with self.assertRaisesRegex(ContractError, "exact file allowlist differs"):
                validate_handoff(output)

    def test_forged_static_report_and_install_plan_identity_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_clean_release(release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            output = root / "handoff"
            prepare(
                release,
                candidate,
                evidence,
                compatibility,
                review_trust_store()["keys"][0]["key_id"],
                output,
            )

            provenance_path = output / "release" / "provenance.json"
            provenance = load(provenance_path)
            provenance["product"] = "forged-product"
            provenance["fingerprint"] = sha256({
                key: item for key, item in provenance.items() if key != "fingerprint"
            })
            dump(provenance, provenance_path)
            release_identity = sha256(
                qualification.gate._release_directory_identity(output / "release")
            )
            preflight_path = output / "release-gate-preflight.json"
            preflight = load(preflight_path)
            preflight["release_identity_sha256"] = release_identity
            preflight["fingerprint"] = sha256({
                key: item for key, item in preflight.items() if key != "fingerprint"
            })
            dump(preflight, preflight_path)
            draft_path = output / "release-review-draft.json"
            draft = load(draft_path)
            draft["reviewed_release_identity_sha256"] = release_identity
            dump(draft, draft_path)
            payload_path = output / "release-review-payload.json"
            payload_path.write_bytes(qualification.gate._review_signature_payload(draft))
            handoff = load(output / "handoff.json")
            handoff["release_identity_sha256"] = release_identity
            handoff["preflight_gate_fingerprint"] = preflight["fingerprint"]
            handoff["review_payload_sha256"] = qualification._digest(payload_path)
            dump(handoff, output / "handoff.json")
            refresh_handoff(output)
            with self.assertRaisesRegex(ContractError, "fresh evaluation"):
                validate_handoff(output)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_clean_release(release)
            evidence, compatibility, _, _ = write_gate_prerequisites(root, release)
            candidate = write_candidate_lock(root / "candidate.json")
            output = root / "handoff"
            prepare(
                release,
                candidate,
                evidence,
                compatibility,
                review_trust_store()["keys"][0]["key_id"],
                output,
            )
            candidate_path = output / "candidate-package-lock.json"
            frozen_candidate = load(candidate_path)
            frozen_candidate["install_plan_identity_hash"] = "0" * 64
            frozen_candidate["fingerprint"] = sha256({
                key: item for key, item in frozen_candidate.items() if key != "fingerprint"
            })
            dump(frozen_candidate, candidate_path)
            conformance_path = output / "conformance-evidence.json"
            conformance = rebind_conformance(
                conformance_path, frozen_candidate["fingerprint"]
            )
            handoff = load(output / "handoff.json")
            handoff["candidate_package_lock_hash"] = frozen_candidate["fingerprint"]
            handoff["conformance_evidence_fingerprint"] = conformance["fingerprint"]
            dump(handoff, output / "handoff.json")
            refresh_handoff(output)
            with self.assertRaisesRegex(ContractError, "candidate package lock differs"):
                validate_handoff(output)


if __name__ == "__main__":
    unittest.main()
