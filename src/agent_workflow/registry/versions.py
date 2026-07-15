"""Small, dependency-free version range checks for Provider manifests."""

from __future__ import annotations

import re

from ..models import ContractError


_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$")
_CONSTRAINT = re.compile(r"^(>=|<=|>|<|==)(.+)$")


def parse_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ContractError("version must be a string")
    match = _VERSION.fullmatch(value)
    if not match:
        raise ContractError(f"unsupported version: {value!r}")
    return tuple(int(part or 0) for part in match.groups())


def satisfies(version: str, expression: str) -> bool:
    """Return whether a numeric SemVer core satisfies a space-separated range."""

    actual = parse_version(version)
    if not isinstance(expression, str) or not expression.strip():
        raise ContractError("compatibility range must be a non-empty string")
    for raw in expression.split():
        match = _CONSTRAINT.fullmatch(raw)
        if not match:
            raise ContractError(f"unsupported compatibility constraint: {raw!r}")
        operator, expected_text = match.groups()
        expected = parse_version(expected_text)
        passed = {
            ">=": actual >= expected,
            "<=": actual <= expected,
            ">": actual > expected,
            "<": actual < expected,
            "==": actual == expected,
        }[operator]
        if not passed:
            return False
    return True
