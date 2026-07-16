"""Phase 4 QA runtime guards for evidence reuse, regression and loop limits."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from ..models import ContractError
from .contracts import (
    qa_fingerprint,
    validate_defect_report,
    validate_regression_set,
    validate_test_result,
)


def qa_execution_identity(
    *,
    plan_fingerprint: str,
    environment_fingerprint: str,
    test_data_fingerprint: str,
    source_fingerprints: list[str],
) -> str:
    """Freeze every input that controls whether a test result can be reused."""

    _fingerprint(plan_fingerprint, "QA execution plan fingerprint", {"qa-v1"})
    _fingerprint(environment_fingerprint, "QA execution environment fingerprint", {"qa-v1", "desktop-v1"})
    _fingerprint(test_data_fingerprint, "QA execution test data fingerprint", {"qa-v1"})
    sources = _fingerprints(source_fingerprints, "QA execution source fingerprints", {"qa-v1"})
    return qa_fingerprint({
        "environment_fingerprint": environment_fingerprint,
        "plan_fingerprint": plan_fingerprint,
        "source_fingerprints": sources,
        "test_data_fingerprint": test_data_fingerprint,
    })


def evidence_reuse_status(
    result: dict[str, Any],
    *,
    plan_fingerprint: str,
    environment_fingerprint: str,
    test_data_fingerprint: str,
    recorded_source_fingerprints: list[str],
    current_source_fingerprints: list[str],
) -> dict[str, Any]:
    """Explain whether an existing Test Result remains reusable."""

    validate_test_result(result)
    reasons: list[str] = []
    if result["plan_fingerprint"] != plan_fingerprint:
        reasons.append("plan-fingerprint-changed")
    if result["environment_fingerprint"] != environment_fingerprint:
        reasons.append("environment-fingerprint-changed")
    if result["test_data_fingerprint"] != test_data_fingerprint:
        reasons.append("test-data-fingerprint-changed")
    recorded = _fingerprints(recorded_source_fingerprints, "recorded source fingerprints", {"qa-v1"})
    current = _fingerprints(current_source_fingerprints, "current source fingerprints", {"qa-v1"})
    if recorded != current:
        reasons.append("source-fingerprint-changed")
    return {
        "current_identity": qa_execution_identity(
            plan_fingerprint=plan_fingerprint,
            environment_fingerprint=environment_fingerprint,
            test_data_fingerprint=test_data_fingerprint,
            source_fingerprints=current,
        ),
        "reasons": sorted(reasons),
        "recorded_identity": qa_execution_identity(
            plan_fingerprint=result["plan_fingerprint"],
            environment_fingerprint=result["environment_fingerprint"],
            test_data_fingerprint=result["test_data_fingerprint"],
            source_fingerprints=recorded,
        ),
        "status": "reusable" if not reasons else "stale",
    }


def refresh_regression_set(
    regression: dict[str, Any],
    *,
    environment_fingerprints: list[str],
    source_fingerprints: list[str],
    reopened_defect_refs: list[str] = (),
) -> dict[str, Any]:
    """Mark a regression set stale when its environment, source or defect state changes."""

    validate_regression_set(regression)
    environments = _fingerprints(environment_fingerprints, "regression environment fingerprints", {"qa-v1", "desktop-v1"})
    sources = _fingerprints(source_fingerprints, "regression source fingerprints", {"qa-v1"})
    reopened = _strings(reopened_defect_refs, "reopened defect refs")
    unknown = sorted(set(reopened) - set(regression["defect_refs"]))
    if unknown:
        raise ContractError("reopened defects are outside the regression set")
    reasons: list[str] = []
    if environments != regression["environment_fingerprints"]:
        reasons.append("environment-fingerprint-changed")
    if sources != regression["source_fingerprints"]:
        reasons.append("source-fingerprint-changed")
    reasons.extend(f"defect-reopened:{defect}" for defect in reopened)
    if not reasons:
        return deepcopy(regression)
    revised = deepcopy(regression)
    revised["environment_fingerprints"] = environments
    revised["source_fingerprints"] = sources
    revised["stale_reasons"] = sorted(set(reasons))
    revised["status"] = "stale"
    revised["fingerprint"] = qa_fingerprint({key: value for key, value in revised.items() if key != "fingerprint"})
    validate_regression_set(revised)
    return revised


def reopen_defect(defect: dict[str, Any], *, evidence_refs: list[dict[str, Any]]) -> dict[str, Any]:
    """Reopen a previously verified defect while preserving its ownership links."""

    validate_defect_report(defect)
    if defect["status"] not in {"verified", "closed"}:
        raise ContractError("only verified or closed defects can be reopened")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise ContractError("reopened defect requires new evidence")
    revised = deepcopy(defect)
    revised["evidence_refs"] = sorted(
        [*revised["evidence_refs"], *deepcopy(evidence_refs)],
        key=lambda item: (item.get("kind", ""), item.get("uri", "")),
    )
    revised["status"] = "reopened"
    revised["fingerprint"] = qa_fingerprint({key: value for key, value in revised.items() if key != "fingerprint"})
    validate_defect_report(revised)
    return revised


class FailFixReportGuard:
    """Allow at most two failed iterations for one normalized issue class."""

    def __init__(self, max_attempts: int = 2) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ContractError("QA loop max_attempts is invalid")
        self.max_attempts = max_attempts
        self._attempts: dict[str, int] = {}

    def record(self, issue_class: str, outcome: str) -> dict[str, Any]:
        if not isinstance(issue_class, str) or not issue_class.strip():
            raise ContractError("QA loop issue_class is invalid")
        if outcome not in {"failed", "resolved", "cancelled"}:
            raise ContractError("QA loop outcome is invalid")
        attempts = self._attempts.get(issue_class, 0)
        if outcome == "failed":
            attempts += 1
            self._attempts[issue_class] = attempts
            blocked = attempts >= self.max_attempts
            return {
                "action": "blocked" if blocked else "fix-and-rerun",
                "attempt": attempts,
                "issue_class": issue_class,
                "next_action": "independent-triage" if blocked else "fix",
                "status": "blocked" if blocked else "retrying",
            }
        return {
            "action": "report" if outcome == "resolved" else "cancel-cleanup",
            "attempt": attempts,
            "issue_class": issue_class,
            "next_action": "report" if outcome == "resolved" else "confirm-cleanup",
            "status": "passed" if outcome == "resolved" else "cancelled",
        }


def _fingerprints(value: Any, label: str, namespaces: set[str]) -> list[str]:
    items = _strings(value, label)
    for item in items:
        _fingerprint(item, label, namespaces)
    return items


def _fingerprint(value: Any, label: str, namespaces: set[str]) -> None:
    namespace = "(?:" + "|".join(sorted(re.escape(item) for item in namespaces)) + ")"
    if not isinstance(value, str) or not re.fullmatch(namespace + r":[0-9a-f]{64}", value):
        raise ContractError(f"{label} is invalid")


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{label} must be strings")
    return sorted(set(value))
