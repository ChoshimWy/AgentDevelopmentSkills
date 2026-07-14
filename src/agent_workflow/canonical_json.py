"""Deterministic JSON encoding and hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def dumps(value: Any) -> str:
    """Return canonical UTF-8 JSON text with a trailing newline."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def dump(value: Any, path: str | Path) -> None:
    Path(path).write_text(dumps(value), encoding="utf-8")


def load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()
