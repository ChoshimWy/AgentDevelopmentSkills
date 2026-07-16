"""Deterministic risk-based QA coverage compiler."""

from __future__ import annotations

from typing import Any

from ..models import ContractError


COVERAGE_LEVELS = (
    "smoke", "targeted", "regression", "compatibility", "end-to-end", "release-candidate",
)

_CATEGORY_FLOORS = {
    "accessibility": "regression",
    "compatibility": "compatibility",
    "cross-system": "end-to-end",
    "data-loss": "regression",
    "privacy": "regression",
    "release-critical": "release-candidate",
    "security": "regression",
}


def compile_coverage(
    risks: list[dict[str, Any]],
    *,
    workflow_kind: str,
    requested_level: str = "smoke",
) -> dict[str, Any]:
    """Map scored risks to a QA level without changing verification level.

    The result is canonical-order friendly and contains only QA coverage facts;
    build/test verification selection deliberately remains outside this compiler.
    """

    if workflow_kind not in {"prd", "bug", "release"}:
        raise ContractError("coverage compiler workflow_kind is invalid")
    if requested_level not in COVERAGE_LEVELS:
        raise ContractError("coverage compiler requested_level is invalid")
    risk_ids: list[str] = []
    categories: set[str] = set()
    maximum_score = 0
    for risk in risks:
        if not isinstance(risk, dict):
            raise ContractError("coverage compiler risk must be an object")
        risk_id = risk.get("id")
        likelihood = risk.get("likelihood")
        impact = risk.get("impact")
        risk_categories = risk.get("categories")
        if not isinstance(risk_id, str) or not risk_id:
            raise ContractError("coverage compiler risk id is invalid")
        if any(isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5 for score in (likelihood, impact)):
            raise ContractError("coverage compiler risk score is invalid")
        if not isinstance(risk_categories, list) or not risk_categories or any(not isinstance(item, str) or not item for item in risk_categories):
            raise ContractError("coverage compiler risk categories are invalid")
        risk_ids.append(risk_id)
        categories.update(risk_categories)
        maximum_score = max(maximum_score, likelihood * impact)
    if risk_ids != sorted(set(risk_ids)):
        raise ContractError("coverage compiler risk ids must be sorted and unique")

    score_level = _score_level(maximum_score)
    floors = [requested_level, score_level]
    rationales = [f"requested-floor:{requested_level}", f"max-risk-score:{maximum_score}"]
    for category in sorted(categories):
        floor = _CATEGORY_FLOORS.get(category)
        if floor is not None:
            floors.append(floor)
            rationales.append(f"category-floor:{category}:{floor}")
    if workflow_kind == "release":
        floors.append("release-candidate")
        rationales.append("workflow-floor:release:release-candidate")
    compiled = max(floors, key=COVERAGE_LEVELS.index)
    dimensions = sorted(categories or {"functional"})
    return {
        "compiled_level": compiled,
        "dimensions": dimensions,
        "rationales": sorted(set(rationales)),
        "requested_level": requested_level,
        "risk_refs": risk_ids,
    }


def _score_level(score: int) -> str:
    if score == 0:
        return "smoke"
    if score <= 4:
        return "targeted"
    if score <= 12:
        return "regression"
    if score <= 19:
        return "compatibility"
    return "end-to-end"
