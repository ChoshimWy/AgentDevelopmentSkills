from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from agent_workflow.canonical_json import dump, sha256
from agent_workflow.models import ContractError
from scripts import build_pages_site as pages
from scripts import build_release_bundle as builder
from scripts import run_release_gate as gate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SOURCE_REVISION = "2" * 40


def build_clean_beta_release(output: Path) -> dict:
    with mock.patch.object(
        builder,
        "_source_identity",
        return_value=(FIXTURE_SOURCE_REVISION, False),
    ), mock.patch.object(builder, "_git_file_modes", return_value={}), mock.patch.object(
        builder,
        "_git_blob",
        side_effect=lambda root, relative: (root / relative).read_bytes(),
    ):
        return builder.build_release_bundle(
            ROOT,
            output,
            allow_dirty=False,
            channel="beta",
        )


def write_gate_report(release: Path, output: Path, *, passed: bool = True) -> dict:
    check = {
        "details": {"scope": "test-only-publication-fixture"},
        "id": "release.publication",
        "status": "passed" if passed else "blocked",
    }
    report = {
        "blockers": [] if passed else ["release.publication"],
        "checks": [check],
        "release_identity_sha256": sha256(gate._release_directory_identity(release)),
        "schema_version": "1.0",
        "status": "passed" if passed else "blocked",
    }
    report["fingerprint"] = sha256(report)
    dump(report, output)
    return report


class PagesDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="agent-skills-pages-tests-")
        cls.root = Path(cls.temporary.name)
        cls.release = cls.root / "release"
        cls.manifest = build_clean_beta_release(cls.release)
        cls.gate_path = cls.root / "release-gate.json"
        cls.gate = write_gate_report(cls.release, cls.gate_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_pages_site_is_small_deterministic_and_keeps_release_assets_external(self) -> None:
        first = self.root / "pages-first"
        second = self.root / "pages-second"
        result = pages.build_pages_site(self.release, self.gate_path, first)
        second_result = pages.build_pages_site(self.release, self.gate_path, second)

        self.assertEqual(result, second_result)
        self.assertEqual({item.name for item in first.iterdir()}, pages.SITE_FILES)
        self.assertEqual(
            {
                item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                for item in first.iterdir()
            },
            {
                item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                for item in second.iterdir()
            },
        )
        self.assertEqual(
            (first / "release-manifest.json").read_bytes(),
            (self.release / "release-manifest.json").read_bytes(),
        )
        self.assertFalse(any(item.name.endswith((".zip", ".whl", ".tar.gz")) for item in first.iterdir()))
        self.assertEqual(result["asset_base_url"], self.manifest["asset_base_url"])
        self.assertIn("/releases/download/v", result["asset_base_url"])
        self.assertIn(
            pages.DEFAULT_PAGES_BASE_URL + "install.sh",
            (first / "index.html").read_text(encoding="utf-8"),
        )
        self.assertIn(
            pages.DEFAULT_PAGES_BASE_URL + "uninstall.sh",
            (first / "index.html").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            json.loads((first / "release.json").read_text(encoding="utf-8"))["status"],
            "published",
        )

    def test_pages_publication_rejects_blocked_or_cross_candidate_gate(self) -> None:
        blocked_path = self.root / "blocked-gate.json"
        write_gate_report(self.release, blocked_path, passed=False)
        with self.assertRaisesRegex(ContractError, "passed release gate"):
            pages.build_pages_site(
                self.release,
                blocked_path,
                self.root / "blocked-pages",
            )

        mismatch_path = self.root / "mismatch-gate.json"
        mismatch = dict(self.gate)
        mismatch["release_identity_sha256"] = "0" * 64
        mismatch["fingerprint"] = sha256({
            key: value for key, value in mismatch.items() if key != "fingerprint"
        })
        dump(mismatch, mismatch_path)
        with self.assertRaisesRegex(ContractError, "does not match"):
            pages.build_pages_site(
                self.release,
                mismatch_path,
                self.root / "mismatch-pages",
            )

    def test_pages_output_is_create_only(self) -> None:
        output = self.root / "existing-pages"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "must not already exist"):
            pages.build_pages_site(self.release, self.gate_path, output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_pages_publication_rejects_source_changes_during_build(self) -> None:
        release = self.root / "release-toctou"
        shutil.copytree(self.release, release)
        gate_path = self.root / "release-toctou-gate.json"
        write_gate_report(release, gate_path)
        original_landing_page = pages._landing_page

        def mutate_source(*args, **kwargs) -> bytes:
            with (release / "install.sh").open("ab") as stream:
                stream.write(b"\n# changed during Pages build\n")
            return original_landing_page(*args, **kwargs)

        output = self.root / "pages-toctou"
        with mock.patch.object(pages, "_landing_page", side_effect=mutate_source):
            with self.assertRaisesRegex(ContractError, "changed while building"):
                pages.build_pages_site(release, gate_path, output)
        self.assertFalse(output.exists())

    def test_pages_site_enforces_per_file_and_total_size_limits(self) -> None:
        oversized = b"x" * (pages.MAX_PUBLIC_FILE_BYTES + 1)
        with mock.patch.object(pages, "_landing_page", return_value=oversized):
            with self.assertRaisesRegex(ContractError, "file exceeds the size limit"):
                pages.build_pages_site(
                    self.release,
                    self.gate_path,
                    self.root / "pages-oversized-file",
                )
        with mock.patch.object(pages, "MAX_SITE_BYTES", 1):
            with self.assertRaisesRegex(ContractError, "total size limit"):
                pages.build_pages_site(
                    self.release,
                    self.gate_path,
                    self.root / "pages-oversized-total",
                )

    def test_bootstrap_defaults_use_pages_but_manifest_assets_use_releases(self) -> None:
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts/bootstrap_install.py").read_text(encoding="utf-8")
        self.assertIn("https://choshimwy.github.io/AgentDevelopmentSkills", shell)
        self.assertIn("https://choshimwy.github.io/AgentDevelopmentSkills", powershell)
        self.assertIn(pages.DEFAULT_PAGES_BASE_URL, bootstrap)
        self.assertEqual(
            self.manifest["asset_base_url"],
            "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/",
        )

    def test_publish_workflow_keeps_final_gate_before_release_and_pages(self) -> None:
        workflow = (ROOT / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        self.assertIn("environment:\n      name: release", workflow)
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn("RELEASE_REVIEW_TRUST_STORE_BASE64", workflow)
        self.assertIn("scripts/run_release_gate.py", workflow)
        self.assertIn("scripts/build_pages_site.py", workflow)
        self.assertIn("scripts/validate_github_publication.py request", workflow)
        self.assertIn("scripts/validate_github_publication.py tag-absent", workflow)
        self.assertIn("git/matching-refs/tags/$RELEASE_TAG", workflow)
        self.assertIn('git/refs" \\\n            -f "ref=refs/tags/$RELEASE_TAG" -f "sha=$SOURCE_REVISION"', workflow)
        self.assertIn('gh release create "$RELEASE_TAG" --verify-tag', workflow)
        self.assertIn('final-main-branch.json', workflow)
        self.assertIn('Smoke deployed Pages against immutable Release', workflow)
        self.assertIn('release, manifest = (json.load(open(path, encoding="utf-8"))', workflow)
        self.assertNotIn('release, manifest, install =', workflow)
        self.assertIn("--proto '=https' --proto-redir '=https'", workflow)
        self.assertIn(
            "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b",
            workflow,
        )
        self.assertIn(
            "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
            workflow,
        )
        self.assertNotRegex(workflow, r"uses: actions/[^@]+@v[0-9]")
        self.assertIn("gh release create", workflow)
        self.assertLess(workflow.index("scripts/run_release_gate.py"), workflow.index("gh release create"))
        self.assertLess(workflow.index("scripts/run_release_gate.py"), workflow.index("scripts/build_pages_site.py"))
        self.assertLess(
            workflow.index("scripts/validate_github_publication.py tag-absent"),
            workflow.index("gh release create"),
        )
        self.assertIn(
            "publish:\n    name: Verify, publish assets, and stage Pages\n"
            "    if: github.ref == 'refs/heads/main'\n"
            "    runs-on: ubuntu-latest\n"
            "    permissions:\n      actions: read\n      contents: write",
            workflow,
        )
        self.assertIn(
            "deploy-pages:\n    name: Deploy GitHub Pages control plane\n"
            "    needs: publish\n    runs-on: ubuntu-latest\n"
            "    permissions:\n      contents: read\n      id-token: write\n      pages: write",
            workflow,
        )
        self.assertNotIn(
            '--signature "${{ inputs.review_signature_base64 }}"',
            workflow,
        )
        self.assertNotIn("RELEASE_REVIEW_TRUST_STORE_BASE64\" >", workflow)


if __name__ == "__main__":
    unittest.main()
