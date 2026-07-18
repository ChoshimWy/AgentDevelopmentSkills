from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import sys
import secrets
import unittest
from unittest import mock
import time

from agent_workflow.canonical_json import dump, sha256
from agent_workflow.models import ContractError
from agent_workflow.installation import build_install_bundle
from agent_workflow.upgrade import make_upgrade_conformance_evidence
from scripts.python_compatibility_evidence import SUPPORTED


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"{name}_release_gate_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_script("build_release_bundle.py")
gate = load_script("run_release_gate.py")
native_contract = load_script("native_artifact_contract.py")
review_tool = load_script("prepare_release_review.py")
FIXTURE_SOURCE_REVISION = "1" * 40
TEST_RSA_EXPONENT = 65537


def _probable_prime(bits: int) -> int:
    bases = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if any(candidate % base == 0 for base in bases):
            continue
        odd = candidate - 1
        power = 0
        while odd % 2 == 0:
            power += 1
            odd //= 2
        for base in bases:
            witness = pow(base, odd, candidate)
            if witness in {1, candidate - 1}:
                continue
            for _ in range(power - 1):
                witness = pow(witness, 2, candidate)
                if witness == candidate - 1:
                    break
            else:
                break
        else:
            return candidate


def _ephemeral_test_rsa_keypair() -> tuple[str, int]:
    while True:
        first = _probable_prime(1024)
        second = _probable_prime(1024)
        modulus = first * second
        totient = (first - 1) * (second - 1)
        if (
            first != second
            and modulus.bit_length() >= 2048
            and math.gcd(TEST_RSA_EXPONENT, totient) == 1
        ):
            return format(modulus, "x"), pow(TEST_RSA_EXPONENT, -1, totient)


# Generated once per test process from OS randomness. No reusable private key or
# production signer identity is stored in the repository or release artifacts.
TEST_RSA_MODULUS_HEX, _TEST_RSA_PRIVATE_EXPONENT = _ephemeral_test_rsa_keypair()


def review_trust_store(*, status: str = "trusted") -> dict:
    key_id = sha256({
        "algorithm": "rsa-pkcs1v15-sha256",
        "exponent": TEST_RSA_EXPONENT,
        "modulus_hex": TEST_RSA_MODULUS_HEX,
    })
    value = {
        "keys": [{
            "algorithm": "rsa-pkcs1v15-sha256",
            "exponent": TEST_RSA_EXPONENT,
            "key_id": key_id,
            "modulus_hex": TEST_RSA_MODULUS_HEX,
            "owner": "phase-6-test-reviewer",
            "scopes": ["phase-6-release"],
            "status": status,
        }],
        "schema_version": "1.0",
    }
    value["fingerprint"] = sha256(value)
    return value


def signed_review(
    release_identity: str,
    source_revision: str,
    compatibility_fingerprint: str = "2" * 64,
) -> dict:
    trust_store = review_trust_store()
    key_id = trust_store["keys"][0]["key_id"]
    value = {
        "blockers": [],
        "reviewed_release_identity_sha256": release_identity,
        "reviewer": "independent",
        "python_compatibility_evidence_fingerprint": compatibility_fingerprint,
        "schema_version": "3.0",
        "scope": "phase-6-release",
        "signature": {
            "algorithm": "rsa-pkcs1v15-sha256",
            "key_id": key_id,
            "value_hex": "",
        },
        "source_revision": source_revision,
        "status": "approved",
    }
    value["signature"]["value_hex"] = _rsa_sign(gate._review_signature_payload(value)).hex()
    value["fingerprint"] = sha256(value)
    return value


def _rsa_sign(payload: bytes) -> bytes:
    digest_info = gate._RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(payload).digest()
    width = len(TEST_RSA_MODULUS_HEX) // 2
    encoded = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(
        int.from_bytes(encoded, "big"),
        _TEST_RSA_PRIVATE_EXPONENT,
        int(TEST_RSA_MODULUS_HEX, 16),
    )
    return signature.to_bytes(width, "big")


def build_fixture_release(release: Path) -> None:
    # Release-gate unit tests exercise artifact and policy contracts, not Git.
    # Inject a frozen source identity so the same suite runs from an extracted
    # source distribution where the repository metadata is intentionally absent.
    with mock.patch.object(
        builder,
        "_source_identity",
        return_value=(FIXTURE_SOURCE_REVISION, True),
    ), mock.patch.object(builder, "_git_file_modes", return_value={}):
        builder.build_release_bundle(ROOT, release, allow_dirty=True, channel="development")


def build_native_fixture(root: Path, source_revision: str, version: str = "0.2.0") -> Path:
    output = root / "native-fixture"
    output.mkdir()
    cargo_lock_sha256 = hashlib.sha256((ROOT / "Cargo.lock").read_bytes()).hexdigest()
    records = []
    for target in native_contract.EXPECTED_TARGETS:
        arch, host_os, format_name, machine = native_contract._TARGETS[target]
        if format_name == "elf":
            value = bytearray(64)
            value[:4] = b"\x7fELF"
            value[4] = 2
            value[5] = 1
            value[18:20] = machine.to_bytes(2, "little")
        elif format_name == "macho":
            value = bytearray(32)
            value[:4] = b"\xcf\xfa\xed\xfe"
            value[4:8] = machine.to_bytes(4, "little")
        else:
            value = bytearray(128)
            value[:2] = b"MZ"
            value[60:64] = (64).to_bytes(4, "little")
            value[64:68] = b"PE\0\0"
            value[68:70] = machine.to_bytes(2, "little")
        filename = native_contract.native_filename(version, target)
        (output / filename).write_bytes(value)
        record = {
            "arch": arch,
            "cargo_lock_sha256": cargo_lock_sha256,
            "filename": filename,
            "fingerprint": "",
            "kind": "native-binary",
            "os": host_os,
            "profile": "release",
            "rustc_version": "rustc 1.97.1 (fixture 2026-01-01)",
            "schema_version": "1.0",
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
            "smoke_output": f"agent-skills-rs {version}\n",
            "smoke_status": "passed",
            "source_revision": source_revision,
            "target": target,
            "version": version,
        }
        record["fingerprint"] = native_contract.fingerprint({
            key: item for key, item in record.items() if key != "fingerprint"
        })
        records.append(record)
    index = {
        "artifacts": records,
        "fingerprint": "",
        "product": "agent-development-skills",
        "schema_version": "1.0",
        "source_revision": source_revision,
        "target_set_sha256": native_contract.fingerprint(
            list(native_contract.EXPECTED_TARGETS)
        ),
        "version": version,
    }
    index["fingerprint"] = native_contract.fingerprint({
        key: item for key, item in index.items() if key != "fingerprint"
    })
    (output / "native-artifacts.json").write_bytes(
        native_contract.canonical_json(index)
    )
    return output


def compatibility_evidence(release: Path, source_revision: str) -> dict:
    records = sorted(
        json.loads((release / "python-artifacts.json").read_text(encoding="utf-8"))["artifacts"],
        key=lambda item: (item["kind"], item["filename"]),
    )
    wheel = next(item for item in records if item["kind"] == "wheel")
    environments = [{
        "artifacts": records,
        "implementation": "CPython",
        "machine": "x86_64",
        "pep517_wheel_sha256": wheel["sha256"],
        "platform": "linux",
        "python_minor": minor,
        "python_version": minor + ".1",
        "status": "passed",
        "test_count": 2,
    } for minor in SUPPORTED]
    value = {
        "artifact_set_sha256": sha256(records),
        "environments": environments,
        "schema_version": "1.0",
        "source_dirty": False,
        "source_revision": source_revision,
        "status": "passed",
    }
    value["fingerprint"] = sha256(value)
    return value


def write_gate_prerequisites(root: Path, release: Path) -> tuple[Path, Path, Path, Path]:
    candidate = build_install_bundle(
        ROOT / "platforms",
        platforms=["apple", "desktop"],
        schema_root=ROOT / "schemas",
    ).package_lock
    evidence = make_upgrade_conformance_evidence(
        candidate,
        manifest_count=19,
        negative_contract_count=18,
        test_count=1,
        suite_definition_hash="2" * 64,
        runner_sha256=hashlib.sha256((ROOT / "scripts/run_conformance.py").read_bytes()).hexdigest(),
        environment={"platform": "fixture", "python": "3.11.0"},
        command_results=[{
            "command": "fixture",
            "exit_code": 0,
            "stderr_sha256": "3" * 64,
            "stdout_sha256": "4" * 64,
        }],
    )
    evidence_path = root / "evidence.json"
    dump(evidence, evidence_path)
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    compatibility_path = root / "python-compatibility.json"
    compatibility = compatibility_evidence(release, manifest["source"]["revision"])
    dump(compatibility, compatibility_path)
    review_path = root / "review.json"
    dump(
        signed_review(
            sha256(gate._release_directory_identity(release)),
            manifest["source"]["revision"],
            compatibility["fingerprint"],
        ),
        review_path,
    )
    trust_path = root / "trust.json"
    dump(review_trust_store(), trust_path)
    return evidence_path, compatibility_path, review_path, trust_path


class ReleaseGateTests(unittest.TestCase):
    def test_beta_release_requires_complete_native_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            with mock.patch.object(
                builder,
                "_source_identity",
                return_value=(FIXTURE_SOURCE_REVISION, False),
            ), mock.patch.object(
                builder,
                "_git_file_modes",
                return_value={},
            ), mock.patch.object(
                builder,
                "_git_blob",
                side_effect=lambda root, relative: (root / relative).read_bytes(),
            ):
                builder.build_release_bundle(ROOT, release, channel="beta")

            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=None,
                python_compatibility_evidence_path=None,
                review_evidence=None,
                review_trust_store=None,
            )

            supply = next(
                item for item in report["checks"] if item["id"] == "release.supply-chain"
            )
            self.assertEqual(supply["status"], "blocked")
            self.assertIn("complete native artifact matrix", supply["details"]["error"])

    def test_native_matrix_rejects_self_consistent_wrong_binary_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = build_native_fixture(root, FIXTURE_SOURCE_REVISION)
            index_path = native / "native-artifacts.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            record = index["artifacts"][0]
            binary_path = native / record["filename"]
            value = bytearray(binary_path.read_bytes())
            value[:8] = b"\x7fELF\x02\x01\x00\x00"
            value[18:20] = (183).to_bytes(2, "little")
            binary_path.write_bytes(value)
            record["sha256"] = hashlib.sha256(value).hexdigest()
            record["fingerprint"] = native_contract.fingerprint({
                key: item for key, item in record.items() if key != "fingerprint"
            })
            index["fingerprint"] = native_contract.fingerprint({
                key: item for key, item in index.items() if key != "fingerprint"
            })
            index_path.write_bytes(native_contract.canonical_json(index))

            with self.assertRaisesRegex(
                native_contract.NativeArtifactError,
                "header differs",
            ):
                native_contract.load_native_artifacts(
                    index_path,
                    native,
                    expected_source_revision=FIXTURE_SOURCE_REVISION,
                    expected_version="0.2.0",
                )

    def test_v2_manifest_binds_the_exact_native_index_and_default_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = build_native_fixture(root, FIXTURE_SOURCE_REVISION)
            release = root / "release"
            with mock.patch.object(
                builder,
                "_source_identity",
                return_value=(FIXTURE_SOURCE_REVISION, False),
            ), mock.patch.object(
                builder,
                "_git_file_modes",
                return_value={},
            ), mock.patch.object(
                builder,
                "_git_blob",
                side_effect=lambda source, relative: (source / relative).read_bytes(),
            ):
                manifest = builder.build_release_bundle(
                    ROOT,
                    release,
                    channel="beta",
                    native_artifacts_dir=native,
                )
            self.assertEqual(manifest["schema_version"], "2.0")
            self.assertEqual(manifest["default_engine"], "rust")
            self.assertEqual(len(manifest["native_artifacts"]), 6)
            self.assertEqual(
                manifest["native_index_sha256"],
                hashlib.sha256(
                    (release / "native-artifacts.json").read_bytes()
                ).hexdigest(),
            )

            manifest["native_artifacts"][0]["sha256"] = "9" * 64
            (release / "release-manifest.json").write_bytes(
                gate.bootstrap_install._canonical_json(manifest)
            )
            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=None,
                python_compatibility_evidence_path=None,
                review_evidence=None,
                review_trust_store=None,
            )
            supply = next(
                item for item in report["checks"] if item["id"] == "release.supply-chain"
            )
            self.assertEqual(supply["status"], "blocked")
            self.assertIn("native execution contract", supply["details"]["error"])

    def test_native_matrix_cannot_claim_a_different_cargo_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = build_native_fixture(root, FIXTURE_SOURCE_REVISION)
            release = root / "release"
            with mock.patch.object(
                builder,
                "_source_identity",
                return_value=(FIXTURE_SOURCE_REVISION, False),
            ), mock.patch.object(
                builder,
                "_git_file_modes",
                return_value={},
            ), mock.patch.object(
                builder,
                "_git_blob",
                side_effect=lambda source, relative: (source / relative).read_bytes(),
            ):
                builder.build_release_bundle(
                    ROOT,
                    release,
                    channel="beta",
                    native_artifacts_dir=native,
                )
            index_path = release / "native-artifacts.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for record in index["artifacts"]:
                record["cargo_lock_sha256"] = "4" * 64
                record["fingerprint"] = native_contract.fingerprint({
                    key: item for key, item in record.items() if key != "fingerprint"
                })
            index["fingerprint"] = native_contract.fingerprint({
                key: item for key, item in index.items() if key != "fingerprint"
            })
            index_bytes = native_contract.canonical_json(index)
            index_path.write_bytes(index_bytes)
            manifest_path = release / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["native_artifacts"] = [
                {
                    "arch": item["arch"],
                    "filename": item["filename"],
                    "os": item["os"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "target": item["target"],
                }
                for item in index["artifacts"]
            ]
            manifest["native_index_sha256"] = hashlib.sha256(index_bytes).hexdigest()
            manifest_path.write_bytes(
                gate.bootstrap_install._canonical_json(manifest)
            )

            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=None,
                python_compatibility_evidence_path=None,
                review_evidence=None,
                review_trust_store=None,
            )
            supply = next(
                item for item in report["checks"] if item["id"] == "release.supply-chain"
            )
            self.assertEqual(supply["status"], "blocked")
            self.assertIn("Cargo.lock differs", supply["details"]["error"])

    def test_development_candidate_runs_distribution_smoke_but_blocks_missing_governance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            build_fixture_release(release)
            old_candidate = build_install_bundle(
                ROOT / "platforms", core_only=True, schema_root=ROOT / "schemas"
            ).package_lock
            old_evidence = make_upgrade_conformance_evidence(
                old_candidate,
                manifest_count=1,
                negative_contract_count=1,
                test_count=1,
                suite_definition_hash="2" * 64,
                runner_sha256=hashlib.sha256((ROOT / "scripts/run_conformance.py").read_bytes()).hexdigest(),
                environment={"platform": "fixture", "python": "3.11.0"},
                command_results=[{
                    "command": "fixture",
                    "exit_code": 0,
                    "stderr_sha256": "3" * 64,
                    "stdout_sha256": "4" * 64,
                }],
            )
            evidence_path = Path(directory) / "old-evidence.json"
            dump(old_evidence, evidence_path)
            manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
            compatibility = compatibility_evidence(release, manifest["source"]["revision"])
            compatibility_path = Path(directory) / "python-compatibility.json"
            dump(compatibility, compatibility_path)
            old_review = signed_review("5" * 64, "old-revision", compatibility["fingerprint"])
            review_path = Path(directory) / "old-review.json"
            dump(old_review, review_path)
            trust_path = Path(directory) / "review-trust-store.json"
            dump(review_trust_store(), trust_path)
            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=evidence_path,
                python_compatibility_evidence_path=compatibility_path,
                review_evidence=review_path,
                review_trust_store=trust_path,
            )
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                report["blockers"],
                [
                    "release.conformance",
                    "release.independent-review",
                    "release.license-notice",
                    "release.python-distribution",
                    "release.source-policy",
                ],
            )
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.python-distribution"]["status"], "blocked")
            self.assertEqual(checks["release.supply-chain"]["status"], "passed")
            self.assertIn("not authorized by signed review", checks["release.conformance"]["details"]["error"])
            self.assertIn("not bound to this release candidate", checks["release.independent-review"]["details"]["error"])
            self.assertEqual(report["fingerprint"], sha256({
                key: value for key, value in report.items() if key != "fingerprint"
            }))

            index_path = release / "python-artifacts.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            original_index = json.loads(index_path.read_text(encoding="utf-8"))
            index["artifacts"][0]["sha256"] = "0" * 64
            index["fingerprint"] = sha256({key: value for key, value in index.items() if key != "fingerprint"})
            dump(index, index_path)
            tampered = gate.evaluate_release_gate(
                release,
                conformance_evidence=evidence_path,
                python_compatibility_evidence_path=compatibility_path,
                review_evidence=review_path,
                review_trust_store=trust_path,
            )
            tampered_checks = {item["id"]: item for item in tampered["checks"]}
            self.assertEqual(tampered_checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("artifact records differ", tampered_checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(tampered_checks["release.python-distribution"]["status"], "blocked")
            self.assertIn("must pass before artifact execution", tampered_checks["release.python-distribution"]["details"]["error"])

            original_index["artifacts"][0]["filename"] = "../external.whl"
            original_index["fingerprint"] = sha256({
                key: value for key, value in original_index.items() if key != "fingerprint"
            })
            dump(original_index, index_path)
            escaped = gate.evaluate_release_gate(
                release,
                conformance_evidence=evidence_path,
                python_compatibility_evidence_path=compatibility_path,
                review_evidence=review_path,
                review_trust_store=trust_path,
            )
            escaped_checks = {item["id"]: item for item in escaped["checks"]}
            self.assertEqual(escaped_checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("record identity is invalid", escaped_checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(escaped_checks["release.python-distribution"]["status"], "blocked")

    def test_malformed_manifest_returns_a_canonical_blocked_gate_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            build_fixture_release(release)
            (release / "release-manifest.json").write_bytes(b"{")
            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=None,
                review_evidence=None,
            )
            self.assertEqual(report["status"], "blocked")
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.manifest"]["status"], "blocked")
            self.assertEqual(checks["release.python-distribution"]["status"], "blocked")
            self.assertEqual(report["fingerprint"], sha256({
                key: value for key, value in report.items() if key != "fingerprint"
            }))

    def test_additional_host_artifact_cannot_escape_provenance_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            build_fixture_release(release)
            manifest_path = release / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original = manifest["artifacts"][0]
            other_name = "unbound-windows.zip"
            (release / other_name).write_bytes((release / original["filename"]).read_bytes())
            manifest["artifacts"].append({
                **original,
                "filename": other_name,
                "host_os": ["windows"],
                "id": "unbound-windows",
            })
            dump(manifest, manifest_path)
            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=None,
                review_evidence=None,
            )
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.manifest"]["status"], "passed")
            self.assertEqual(checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("exactly one universal", checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(checks["release.python-distribution"]["status"], "blocked")

    def test_orphan_artifact_and_wrong_metadata_shape_are_canonical_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            (release / "backdoor.whl").write_bytes(b"unbound")
            orphan = gate.evaluate_release_gate(release, conformance_evidence=None, review_evidence=None)
            orphan_checks = {item["id"]: item for item in orphan["checks"]}
            self.assertEqual(orphan_checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("exact provenance allowlist", orphan_checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(orphan_checks["release.python-distribution"]["status"], "blocked")

            (release / "backdoor.whl").unlink()
            manifest_path = release / "release-manifest.json"
            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            incomplete_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            incomplete_manifest["bootstrap_assets"] = [
                item for item in incomplete_manifest["bootstrap_assets"]
                if item["filename"] != "install.ps1"
            ]
            dump(incomplete_manifest, manifest_path)
            incomplete = gate.evaluate_release_gate(
                release, conformance_evidence=None, review_evidence=None
            )
            incomplete_checks = {item["id"]: item for item in incomplete["checks"]}
            self.assertEqual(incomplete_checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("bootstrap asset set differs", incomplete_checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(incomplete_checks["release.python-distribution"]["status"], "blocked")

            dump(original_manifest, manifest_path)
            wrong_entrypoint = json.loads(manifest_path.read_text(encoding="utf-8"))
            wrong_entrypoint["artifacts"][0]["entrypoint"] = "scripts/run_release_gate.py"
            dump(wrong_entrypoint, manifest_path)
            wrong_execution = gate.evaluate_release_gate(
                release, conformance_evidence=None, review_evidence=None
            )
            execution_checks = {item["id"]: item for item in wrong_execution["checks"]}
            self.assertEqual(execution_checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("execution contract differs", execution_checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(execution_checks["release.python-distribution"]["status"], "blocked")

            dump(original_manifest, manifest_path)
            (release / "sbom.json").write_text("[]\n", encoding="utf-8")
            wrong_shape = gate.evaluate_release_gate(release, conformance_evidence=None, review_evidence=None)
            shape_checks = {item["id"]: item for item in wrong_shape["checks"]}
            self.assertEqual(shape_checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("must be a JSON object", shape_checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(shape_checks["release.python-distribution"]["status"], "blocked")

    def test_sdist_rejects_casefold_collisions_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "candidate.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in ("bundle/File.txt", "bundle/file.txt"):
                    value = b"fixture"
                    info = tarfile.TarInfo(name)
                    info.mode = 0o644
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))
            with self.assertRaisesRegex(ContractError, "unsafe member"):
                gate._safe_extract_sdist(archive_path, root / "output")

    def test_self_consistent_python_artifacts_must_still_match_source_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            index_path = release / "python-artifacts.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            sdist = next(item for item in index["artifacts"] if item["kind"] == "sdist")
            source = gate._safe_extract_sdist(release / sdist["filename"], root / "source")
            implementation = source / "src/agent_workflow/__init__.py"
            implementation.write_bytes(implementation.read_bytes() + b"\n# unbound implementation\n")
            rebuilt = root / "rebuilt"
            completed = subprocess.run(
                [sys.executable, str(source / "scripts/build_python_artifacts.py"), "--output", str(rebuilt)],
                cwd=source,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            for record in index["artifacts"]:
                shutil.copyfile(rebuilt / record["filename"], release / record["filename"])
                value = (release / record["filename"]).read_bytes()
                record["size"] = len(value)
                record["sha256"] = hashlib.sha256(value).hexdigest()
            index["fingerprint"] = sha256({key: value for key, value in index.items() if key != "fingerprint"})
            dump(index, index_path)
            provenance_path = release / "provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            by_name = {item["filename"]: item for item in index["artifacts"]}
            for record in provenance["artifacts"]:
                if record["filename"] in by_name:
                    record.update({
                        "sha256": by_name[record["filename"]]["sha256"],
                        "size": by_name[record["filename"]]["size"],
                    })
            provenance["fingerprint"] = sha256({
                key: value for key, value in provenance.items() if key != "fingerprint"
            })
            dump(provenance, provenance_path)
            evidence_path, compatibility_path, review_path, trust_path = write_gate_prerequisites(root, release)
            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=evidence_path,
                python_compatibility_evidence_path=compatibility_path,
                review_evidence=review_path,
                review_trust_store=trust_path,
            )
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.supply-chain"]["status"], "passed")
            self.assertEqual(checks["release.python-distribution"]["status"], "blocked")
            self.assertIn("differs from the bound SBOM", checks["release.python-distribution"]["details"]["error"])

    def test_transient_snapshot_replacement_cannot_change_frozen_wheel_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            index = json.loads((release / "python-artifacts.json").read_text(encoding="utf-8"))
            records = index["artifacts"]
            sbom_files = json.loads((release / "sbom.json").read_text(encoding="utf-8"))["files"]
            frozen = {
                record["filename"]: (release / record["filename"]).read_bytes()
                for record in records
            }
            wheel = next(record for record in records if record["kind"] == "wheel")

            def malicious_builder(command, **kwargs):
                if "--output" not in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                rebuilt = Path(command[command.index("--output") + 1])
                rebuilt.mkdir(parents=True)
                for record in records:
                    value = frozen[record["filename"]]
                    if record["kind"] == "wheel":
                        value = b"malicious replacement wheel"
                    (rebuilt / record["filename"]).write_bytes(value)
                wheel_path = release / wheel["filename"]
                original = wheel_path.read_bytes()
                wheel_path.write_bytes(b"malicious replacement wheel")
                wheel_path.write_bytes(original)
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(
                gate, "_run_candidate_command", side_effect=malicious_builder
            ):
                with self.assertRaisesRegex(ContractError, "sdist rebuild is not byte-identical"):
                    gate._python_distribution_smoke(
                        release,
                        records,
                        sbom_files,
                        frozen,
                        {},
                    )

    def test_candidate_commands_have_time_and_output_limits(self) -> None:
        with self.assertRaisesRegex(ContractError, "timed out"):
            gate._run_candidate_command(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=0,
            )
        with mock.patch.object(gate, "_MAX_CANDIDATE_OUTPUT_BYTES", 1024):
            with self.assertRaisesRegex(ContractError, "output limit"):
                gate._run_candidate_command(
                    [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"],
                    timeout=10,
                )
        if os.name == "posix":
            completed = gate._run_candidate_command(
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                    "print(p.pid, flush=True)",
                ],
                timeout=10,
            )
            child = int(completed.stdout.strip())
            for _ in range(20):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("candidate child process survived its isolated process group")

    def test_resigned_standalone_bootstrap_must_match_source_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            build_fixture_release(release)
            bootstrap_path = release / "install.sh"
            bootstrap_path.write_bytes(bootstrap_path.read_bytes() + b"\n# unbound bootstrap\n")
            value = bootstrap_path.read_bytes()
            digest = hashlib.sha256(value).hexdigest()
            manifest_path = release / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            record = next(item for item in manifest["bootstrap_assets"] if item["filename"] == "install.sh")
            record.update({"sha256": digest, "size": len(value)})
            dump(manifest, manifest_path)
            provenance_path = release / "provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance_record = next(
                item for item in provenance["artifacts"] if item["filename"] == "install.sh"
            )
            provenance_record.update({"sha256": digest, "size": len(value)})
            provenance["fingerprint"] = sha256({
                key: item for key, item in provenance.items() if key != "fingerprint"
            })
            dump(provenance, provenance_path)
            report = gate.evaluate_release_gate(
                release, conformance_evidence=None, review_evidence=None
            )
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.supply-chain"]["status"], "blocked")
            self.assertIn("standalone bootstrap differs", checks["release.supply-chain"]["details"]["error"])
            self.assertEqual(checks["release.python-distribution"]["status"], "blocked")

    def test_active_release_mutation_is_detected_after_snapshot_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            evidence_path, compatibility_path, review_path, trust_path = write_gate_prerequisites(root, release)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            real_smoke = gate._python_distribution_smoke

            def mutate_after_snapshot(snapshot, records, sbom_files, frozen_artifacts, state):
                (release / "provenance.json").write_bytes(
                    (release / "provenance.json").read_bytes() + b" "
                )
                return real_smoke(snapshot, records, sbom_files, frozen_artifacts, state)

            with mock.patch.object(
                gate, "_python_distribution_smoke", side_effect=mutate_after_snapshot
            ), mock.patch.object(
                gate, "_execute_candidate_conformance", return_value=evidence
            ):
                report = gate.evaluate_release_gate(
                    release,
                    conformance_evidence=evidence_path,
                    python_compatibility_evidence_path=compatibility_path,
                    review_evidence=review_path,
                    review_trust_store=trust_path,
                )
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.python-distribution"]["status"], "passed")
            self.assertEqual(checks["release.snapshot-stability"]["status"], "blocked")

    def test_exact_candidate_evidence_and_review_pass_their_release_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            candidate = build_install_bundle(
                ROOT / "platforms",
                platforms=["apple", "desktop"],
                schema_root=ROOT / "schemas",
            ).package_lock
            evidence = make_upgrade_conformance_evidence(
                candidate,
                manifest_count=19,
                negative_contract_count=18,
                test_count=1,
                suite_definition_hash="2" * 64,
                runner_sha256=hashlib.sha256((ROOT / "scripts/run_conformance.py").read_bytes()).hexdigest(),
                environment={"platform": "fixture", "python": "3.11.0"},
                command_results=[{
                    "command": "fixture",
                    "exit_code": 0,
                    "stderr_sha256": "3" * 64,
                    "stdout_sha256": "4" * 64,
                }],
            )
            evidence_path = root / "evidence.json"
            dump(evidence, evidence_path)
            release_identity = sha256(gate._release_directory_identity(release))
            manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
            compatibility = compatibility_evidence(release, manifest["source"]["revision"])
            compatibility_path = root / "python-compatibility.json"
            dump(compatibility, compatibility_path)
            review = signed_review(
                release_identity,
                manifest["source"]["revision"],
                compatibility["fingerprint"],
            )
            review_path = root / "review.json"
            dump(review, review_path)
            trust_path = root / "review-trust-store.json"
            dump(review_trust_store(), trust_path)
            with mock.patch.object(
                gate, "_execute_candidate_conformance", return_value=evidence
            ) as executed_suite:
                report = gate.evaluate_release_gate(
                    release,
                    conformance_evidence=evidence_path,
                    python_compatibility_evidence_path=compatibility_path,
                    review_evidence=review_path,
                    review_trust_store=trust_path,
                )
            executed_suite.assert_called_once()
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.conformance"]["status"], "passed")
            self.assertEqual(checks["release.independent-review"]["status"], "passed")
            self.assertEqual(checks["release.license-notice"]["status"], "passed")
            self.assertEqual(report["blockers"], ["release.source-policy"])

    def test_python_matrix_is_required_bound_and_precedes_candidate_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            evidence_path, compatibility_path, review_path, trust_path = write_gate_prerequisites(root, release)
            with mock.patch.object(gate, "_execute_candidate_conformance") as candidate:
                missing = gate.evaluate_release_gate(
                    release,
                    conformance_evidence=evidence_path,
                    review_evidence=review_path,
                    review_trust_store=trust_path,
                )
            candidate.assert_not_called()
            missing_checks = {item["id"]: item for item in missing["checks"]}
            self.assertEqual(missing_checks["release.python-compatibility"]["status"], "blocked")
            self.assertEqual(missing_checks["release.python-distribution"]["status"], "blocked")

            matrix = json.loads(compatibility_path.read_text(encoding="utf-8"))
            for environment in matrix["environments"]:
                environment["artifacts"][0]["sha256"] = "f" * 64
            matrix["artifact_set_sha256"] = sha256(matrix["environments"][0]["artifacts"])
            matrix["fingerprint"] = sha256({
                key: item for key, item in matrix.items() if key != "fingerprint"
            })
            dump(matrix, compatibility_path)
            manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
            dump(
                signed_review(
                    sha256(gate._release_directory_identity(release)),
                    manifest["source"]["revision"],
                    matrix["fingerprint"],
                ),
                review_path,
            )
            drift = gate.evaluate_release_gate(
                release,
                conformance_evidence=evidence_path,
                python_compatibility_evidence_path=compatibility_path,
                review_evidence=review_path,
                review_trust_store=trust_path,
            )
            drift_checks = {item["id"]: item for item in drift["checks"]}
            self.assertIn(
                "artifacts differ from the release",
                drift_checks["release.python-compatibility"]["details"]["error"],
            )

    def test_independent_review_evidence_is_fingerprinted_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            trust_path = Path(directory) / "review-trust-store.json"
            value = signed_review("1" * 64, "fixture-revision")
            dump(value, path)
            dump(review_trust_store(), trust_path)
            self.assertEqual(gate._review_evidence(path, trust_path), value)
            value["blockers"] = ["unresolved"]
            value["fingerprint"] = sha256({key: item for key, item in value.items() if key != "fingerprint"})
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "not an approval"):
                gate._review_evidence(path, trust_path)

    def test_review_signature_rejects_tampering_unknown_and_revoked_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "review.json"
            trust_path = root / "trust.json"
            value = signed_review("1" * 64, "fixture-revision")
            dump(review_trust_store(), trust_path)

            value["source_revision"] = "tampered-revision"
            value["fingerprint"] = sha256({
                key: item for key, item in value.items() if key != "fingerprint"
            })
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "signature is invalid"):
                gate._review_evidence(path, trust_path)

            value = signed_review("1" * 64, "fixture-revision")
            value["signature"]["key_id"] = "f" * 64
            value["fingerprint"] = sha256({
                key: item for key, item in value.items() if key != "fingerprint"
            })
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "not in the external trust store"):
                gate._review_evidence(path, trust_path)

            dump(review_trust_store(status="revoked"), trust_path)
            value = signed_review("1" * 64, "fixture-revision")
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "signer is revoked"):
                gate._review_evidence(path, trust_path)

            oversized = review_trust_store()
            oversized["keys"][0]["modulus_hex"] += "ff" * 769
            oversized["fingerprint"] = sha256({
                key: item for key, item in oversized.items() if key != "fingerprint"
            })
            dump(oversized, trust_path)
            with self.assertRaisesRegex(ContractError, "trust key is invalid"):
                gate._review_evidence(path, trust_path)

    def test_review_prepare_and_finalize_keep_private_key_outside_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            trust = review_trust_store()
            trust_path = root / "trust.json"
            dump(trust, trust_path)
            draft = root / "review-draft.json"
            payload = root / "review-payload.json"
            manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
            compatibility_path = root / "python-compatibility.json"
            dump(
                compatibility_evidence(release, manifest["source"]["revision"]),
                compatibility_path,
            )
            prepared = review_tool.prepare(
                release,
                compatibility_path,
                trust["keys"][0]["key_id"],
                draft,
                payload,
            )
            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(prepared["payload_sha256"], hashlib.sha256(payload.read_bytes()).hexdigest())
            signature = root / "review.sig"
            signature.write_bytes(_rsa_sign(payload.read_bytes()))
            evidence = root / "review.json"
            finalized = review_tool.finalize(draft, signature, trust_path, evidence)
            self.assertEqual(finalized["status"], "finalized")
            verified = gate._review_evidence(evidence, trust_path)
            self.assertEqual(
                verified["reviewed_release_identity_sha256"],
                sha256(gate._release_directory_identity(release)),
            )
            self.assertNotIn(
                format(_TEST_RSA_PRIVATE_EXPONENT, "x"),
                evidence.read_text(encoding="utf-8"),
            )

    def test_review_trust_store_is_part_of_snapshot_stability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            review_path = root / "review.json"
            dump(signed_review("1" * 64, FIXTURE_SOURCE_REVISION), review_path)
            trust_path = root / "trust.json"
            dump(review_trust_store(), trust_path)

            def mutate_trust_store(*args, **kwargs):
                trust_path.write_bytes(trust_path.read_bytes() + b" ")
                return gate._blocked_release_gate("release.fixture", ContractError("fixture"))

            with mock.patch.object(
                gate,
                "_evaluate_release_gate_snapshot",
                side_effect=mutate_trust_store,
            ):
                report = gate.evaluate_release_gate(
                    release,
                    conformance_evidence=None,
                    review_evidence=review_path,
                    review_trust_store=trust_path,
                )
            self.assertIn("release.snapshot-stability", report["blockers"])

    def test_review_trust_store_cannot_be_supplied_by_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            trust_path = release / "candidate-controlled-trust.json"
            dump(review_trust_store(), trust_path)
            report = gate.evaluate_release_gate(
                release,
                conformance_evidence=None,
                review_evidence=None,
                review_trust_store=trust_path,
            )
            self.assertEqual(report["blockers"], ["release.snapshot"])
            self.assertIn(
                "must remain outside the candidate",
                report["checks"][0]["details"]["error"],
            )

    def test_candidate_execution_cannot_replace_snapshotted_review_or_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            build_fixture_release(release)
            candidate = build_install_bundle(
                ROOT / "platforms",
                platforms=["apple", "desktop"],
                schema_root=ROOT / "schemas",
            ).package_lock
            runner_sha256 = hashlib.sha256(
                (ROOT / "scripts/run_conformance.py").read_bytes()
            ).hexdigest()
            evidence = make_upgrade_conformance_evidence(
                candidate,
                manifest_count=19,
                negative_contract_count=18,
                test_count=1,
                suite_definition_hash="2" * 64,
                runner_sha256=runner_sha256,
                environment={"platform": "fixture", "python": "3.11.0"},
                command_results=[{
                    "command": "fixture",
                    "exit_code": 0,
                    "stderr_sha256": "3" * 64,
                    "stdout_sha256": "4" * 64,
                }],
            )
            evidence_path = root / "evidence.json"
            dump(evidence, evidence_path)
            manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
            release_identity = sha256(gate._release_directory_identity(release))
            compatibility = compatibility_evidence(release, manifest["source"]["revision"])
            compatibility_path = root / "python-compatibility.json"
            dump(compatibility, compatibility_path)
            review_path = root / "review.json"
            dump(
                signed_review(
                    release_identity,
                    manifest["source"]["revision"],
                    compatibility["fingerprint"],
                ),
                review_path,
            )
            trust_path = root / "trust.json"
            dump(review_trust_store(), trust_path)

            def malicious_smoke(snapshot_release, records, sbom_files, frozen_artifacts, state):
                snapshot_root = snapshot_release.parent
                (snapshot_root / "review-evidence.json").write_text("{}\n", encoding="utf-8")
                (snapshot_root / "review-trust-store.json").write_text("{}\n", encoding="utf-8")
                state["package_lock"] = candidate
                return {
                    "license": {"notice_path": None, "notice_sha256": None, "spdx": None, "status": "pending"},
                    "license_verified": False,
                    "package_lock_hash": candidate["fingerprint"],
                    "python": "3.11",
                    "schema_inventory_hash": candidate["schema_inventory"]["content_sha256"],
                    "sdist_rebuild": "byte-identical",
                    "wheel_smoke": "passed",
                }

            with mock.patch.object(
                gate, "_python_distribution_smoke", side_effect=malicious_smoke
            ), mock.patch.object(
                gate, "_execute_candidate_conformance", return_value=evidence
            ):
                report = gate.evaluate_release_gate(
                    release,
                    conformance_evidence=evidence_path,
                    python_compatibility_evidence_path=compatibility_path,
                    review_evidence=review_path,
                    review_trust_store=trust_path,
                )
            checks = {item["id"]: item for item in report["checks"]}
            self.assertEqual(checks["release.independent-review"]["status"], "passed")
            self.assertEqual(checks["release.conformance"]["status"], "passed")
            self.assertEqual(checks["release.snapshot-stability"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
