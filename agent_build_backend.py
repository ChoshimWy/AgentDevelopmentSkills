"""Dependency-free PEP 517 backend for the deterministic release artifacts."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from scripts.build_python_artifacts import build_python_artifacts


ROOT = Path(__file__).resolve().parent


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    with tempfile.TemporaryDirectory(prefix="agent-skills-pep517-wheel-") as directory:
        artifacts = build_python_artifacts(ROOT, Path(directory))
        record = next(item for item in artifacts if item["kind"] == "wheel")
        destination = Path(wheel_directory) / str(record["filename"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(directory) / str(record["filename"]), destination)
        return destination.name


def build_sdist(sdist_directory, config_settings=None) -> str:
    with tempfile.TemporaryDirectory(prefix="agent-skills-pep517-sdist-") as directory:
        artifacts = build_python_artifacts(ROOT, Path(directory))
        record = next(item for item in artifacts if item["kind"] == "sdist")
        destination = Path(sdist_directory) / str(record["filename"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(directory) / str(record["filename"]), destination)
        return destination.name
