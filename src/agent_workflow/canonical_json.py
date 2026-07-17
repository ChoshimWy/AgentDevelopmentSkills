"""Deterministic JSON encoding and hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


MAX_CANONICAL_INTEGER_DIGITS = 4_300
MAX_CANONICAL_JSON_DEPTH = 512


def _validate_text_limits(text: str) -> None:
    depth = 0
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
            if depth > MAX_CANONICAL_JSON_DEPTH:
                raise ValueError(
                    f"JSON nesting depth {depth} exceeds maximum "
                    f"{MAX_CANONICAL_JSON_DEPTH}"
                )
        elif character in "}]":
            depth = max(0, depth - 1)
        elif character == "-" or character.isascii() and character.isdigit():
            start = index
            index += 1
            while index < len(text) and text[index] not in " \t\r\n,]}":
                index += 1
            token = text[start:index]
            if not any(marker in token for marker in ".eE"):
                digits = sum(character.isascii() and character.isdigit() for character in token)
                if digits > MAX_CANONICAL_INTEGER_DIGITS:
                    raise ValueError(
                        f"integer has {digits} digits; maximum is "
                        f"{MAX_CANONICAL_INTEGER_DIGITS}"
                    )
            continue
        index += 1


def _validate_value_limits(value: Any) -> None:
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active_containers: set[int] = set()
    while stack:
        current, parent_depth, exiting = stack.pop()
        if exiting:
            active_containers.remove(id(current))
            continue
        if isinstance(current, bool) or current is None:
            continue
        if isinstance(current, int):
            try:
                digits = len(str(abs(current)))
            except ValueError as error:
                raise ValueError(
                    f"integer exceeds maximum {MAX_CANONICAL_INTEGER_DIGITS} digits"
                ) from error
            if digits > MAX_CANONICAL_INTEGER_DIGITS:
                raise ValueError(
                    f"integer has {digits} digits; maximum is "
                    f"{MAX_CANONICAL_INTEGER_DIGITS}"
                )
            continue
        if not isinstance(current, (dict, list, tuple)):
            continue
        depth = parent_depth + 1
        if depth > MAX_CANONICAL_JSON_DEPTH:
            raise ValueError(
                f"JSON nesting depth {depth} exceeds maximum "
                f"{MAX_CANONICAL_JSON_DEPTH}"
            )
        identity = id(current)
        if identity in active_containers:
            raise ValueError("Circular reference detected")
        active_containers.add(identity)
        stack.append((current, depth, True))
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth, False) for child in children)


def dumps(value: Any) -> str:
    """Return canonical UTF-8 JSON text with a trailing newline."""

    _validate_value_limits(value)
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
    text = Path(path).read_text(encoding="utf-8")
    _validate_text_limits(text)
    value = json.loads(text)
    _validate_value_limits(value)
    return value


def sha256(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()
