#!/usr/bin/env python3
"""Build the gated GitHub Pages control plane for one verified release."""

from __future__ import annotations

import argparse
from html import escape
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for entry in (SRC, SCRIPTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402
import bootstrap_install  # noqa: E402
import run_release_gate as gate  # noqa: E402


DEFAULT_PAGES_BASE_URL = "https://choshimwy.github.io/AgentDevelopmentSkills/"
MAX_PUBLIC_FILE_BYTES = 2 * 1024 * 1024
MAX_SITE_BYTES = 8 * 1024 * 1024
PUBLIC_RELEASE_FILES = ("install.ps1", "install.sh", "release-manifest.json")
SITE_FILES = {
    ".nojekyll",
    "index.html",
    "install.ps1",
    "install.sh",
    "release-gate.json",
    "release-manifest.json",
    "release.json",
}


def _pages_base_url(value: str) -> str:
    normalized = value.rstrip("/") + "/"
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError("Pages base URL must be a public HTTPS URL without credentials, query, or fragment")
    return normalized


def _safe_file(path: Path, *, label: str, maximum: int = MAX_PUBLIC_FILE_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or unsafe")
    declared = path.stat().st_size
    if declared > maximum:
        raise ContractError(f"{label} exceeds the size limit")
    value = path.read_bytes()
    if len(value) != declared or len(value) > maximum:
        raise ContractError(f"{label} changed while being read")
    return value


def _validate_gate(release: Path, report_path: Path) -> dict[str, Any]:
    report_bytes = _safe_file(report_path, label="release gate report")
    report = load(report_path)
    gate._validate_release_gate_report(report)
    if dumps(report).encode("utf-8") != report_bytes:
        raise ContractError("release gate report must use canonical JSON encoding")
    if report["status"] != "passed" or report["blockers"]:
        raise ContractError("GitHub Pages publication requires a passed release gate")
    release_identity = sha256(gate._release_directory_identity(release))
    if report["release_identity_sha256"] != release_identity:
        raise ContractError("release gate report does not match the release directory")
    return report


def _file_identity(path: Path, *, label: str) -> dict[str, Any]:
    value = _safe_file(path, label=label)
    return {
        "mode": path.stat().st_mode & 0o777,
        "sha256": hashlib.sha256(value).hexdigest(),
        "size": len(value),
    }


def _validate_site(stage: Path) -> None:
    candidates = tuple(stage.iterdir())
    if {item.name for item in candidates} != SITE_FILES:
        raise ContractError("Pages site file allowlist differs")
    total = 0
    for item in candidates:
        if item.is_symlink() or not item.is_file():
            raise ContractError(f"Pages site contains an unsafe entry: {item.name}")
        size = item.stat().st_size
        if size > MAX_PUBLIC_FILE_BYTES:
            raise ContractError(f"Pages site file exceeds the size limit: {item.name}")
        total += size
        if total > MAX_SITE_BYTES:
            raise ContractError("Pages site exceeds the total size limit")


def _landing_page(manifest: dict[str, Any], gate_report: dict[str, Any], base_url: str) -> bytes:
    version = escape(manifest["version"])
    channel = escape(manifest["channel"])
    revision = escape(manifest["source"]["revision"])
    asset_base_url = escape(manifest["asset_base_url"])
    posix_command = (
        "curl -fsSL --proto '=https' --tlsv1.2 "
        + base_url
        + "install.sh | bash"
    )
    powershell_command = "iwr -useb " + base_url + "install.ps1 | iex"
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>AgentDevelopmentSkills {version}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ max-width: 880px; margin: 0 auto; padding: 48px 24px 72px; line-height: 1.65; }}
    h1 {{ line-height: 1.15; }}
    .status {{ display: inline-block; padding: 4px 10px; border: 1px solid currentColor; border-radius: 999px; }}
    pre {{ overflow: auto; padding: 16px; border-radius: 10px; background: color-mix(in srgb, CanvasText 8%, Canvas); }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    dl {{ display: grid; grid-template-columns: 160px 1fr; gap: 6px 16px; }}
    dt {{ font-weight: 700; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <main>
    <p class="status">Release Gate passed · {channel}</p>
    <h1>AgentDevelopmentSkills {version}</h1>
    <p>此静态站点只承载经过发布门禁的安装入口与 canonical manifest；不可变版本资产由 GitHub Releases 提供。</p>
    <h2>macOS / Linux / WSL2</h2>
    <pre><code>{escape(posix_command)}</code></pre>
    <h2>Windows PowerShell</h2>
    <p>入口已提供；production manifest 未声明 Windows artifact 时会 fail-closed。</p>
    <pre><code>{escape(powershell_command)}</code></pre>
    <h2>发布身份</h2>
    <dl>
      <dt>Channel</dt><dd><code>{channel}</code></dd>
      <dt>Source revision</dt><dd><code>{revision}</code></dd>
      <dt>Release identity</dt><dd><code>{escape(gate_report["release_identity_sha256"])}</code></dd>
      <dt>Gate fingerprint</dt><dd><code>{escape(gate_report["fingerprint"])}</code></dd>
      <dt>Asset base</dt><dd><code>{asset_base_url}</code></dd>
    </dl>
  </main>
</body>
</html>
"""
    return html.encode("utf-8")


def build_pages_site(
    release: Path,
    gate_report_path: Path,
    output: Path,
    *,
    pages_base_url: str = DEFAULT_PAGES_BASE_URL,
) -> dict[str, Any]:
    release = Path(os.path.abspath(release.expanduser()))
    output = Path(os.path.abspath(output.expanduser()))
    if output.exists() or output.is_symlink():
        raise ContractError("Pages output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    base_url = _pages_base_url(pages_base_url)
    gate_report_path = Path(os.path.abspath(gate_report_path.expanduser()))
    with tempfile.TemporaryDirectory(prefix=".agent-skills-pages-", dir=output.parent) as directory:
        temporary = Path(directory)
        snapshot_release = temporary / "release"
        release_identity = gate._snapshot_release(release, snapshot_release)
        gate_bytes = _safe_file(gate_report_path, label="release gate report")
        gate_identity = {
            "mode": gate_report_path.stat().st_mode & 0o777,
            "sha256": hashlib.sha256(gate_bytes).hexdigest(),
            "size": len(gate_bytes),
        }
        snapshot_gate = temporary / "release-gate.json"
        snapshot_gate.write_bytes(gate_bytes)
        snapshot_gate.chmod(gate_identity["mode"])

        gate_report = _validate_gate(snapshot_release, snapshot_gate)
        manifest_bytes = _safe_file(
            snapshot_release / "release-manifest.json",
            label="release manifest",
        )
        manifest = bootstrap_install.parse_release_manifest(manifest_bytes)
        if manifest["channel"] not in {"beta", "stable"} or manifest["source"]["dirty"]:
            raise ContractError("Pages publication requires a clean beta or stable release")
        if manifest["asset_base_url"] == base_url:
            raise ContractError("Pages control plane must not be the immutable release asset origin")

        release_metadata = {
            "asset_base_url": manifest["asset_base_url"],
            "channel": manifest["channel"],
            "gate_fingerprint": gate_report["fingerprint"],
            "product": manifest["product"],
            "release_identity_sha256": gate_report["release_identity_sha256"],
            "schema_version": "1.0",
            "source_revision": manifest["source"]["revision"],
            "status": "published",
            "version": manifest["version"],
        }
        stage = temporary / "site"
        stage.mkdir()
        (stage / ".nojekyll").write_bytes(b"")
        for filename in PUBLIC_RELEASE_FILES:
            (stage / filename).write_bytes(
                _safe_file(snapshot_release / filename, label=f"release file {filename}")
            )
        (stage / "release-gate.json").write_bytes(
            _safe_file(snapshot_gate, label="release gate report")
        )
        (stage / "release.json").write_text(dumps(release_metadata), encoding="utf-8")
        (stage / "index.html").write_bytes(_landing_page(manifest, gate_report, base_url))
        _validate_site(stage)
        if gate._release_directory_identity(release) != release_identity:
            raise ContractError("release directory changed while building the Pages site")
        if _file_identity(gate_report_path, label="release gate report") != gate_identity:
            raise ContractError("release gate report changed while building the Pages site")
        os.replace(stage, output)
    return release_metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--release-gate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages-base-url", default=DEFAULT_PAGES_BASE_URL)
    args = parser.parse_args()
    try:
        result = build_pages_site(
            args.release_dir,
            args.release_gate,
            args.output,
            pages_base_url=args.pages_base_url,
        )
    except (ContractError, OSError, TypeError, ValueError, KeyError) as error:
        print(dumps({"error": str(error), "schema_version": "1.0", "status": "blocked"}), end="", file=sys.stderr)
        return 2
    print(dumps(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
