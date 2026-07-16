"""Runtime invariants for Phase 4 platform-neutral QA artifacts."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from ..canonical_json import sha256
from ..models import ContractError, require_version
from .coverage import compile_coverage


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^qa-v1:[0-9a-f]{64}$")
_ENVIRONMENT_FINGERPRINT = re.compile(r"^(?:qa|desktop)-v1:[0-9a-f]{64}$")
_WORKFLOWS = {"prd", "bug", "release"}
_COVERAGE_LEVELS = {
    "smoke", "targeted", "regression", "compatibility", "end-to-end", "release-candidate",
}
_VERIFICATION_LEVELS = {
    "none", "static", "lint", "affected-tests", "module-build", "integration", "ui-smoke", "full",
}
_TERMINAL_QA_STATUSES = {"passed", "partial", "blocked", "failed", "cancelled"}


def qa_fingerprint(value: Any) -> str:
    """Return a namespaced deterministic identity for a QA artifact body."""

    return f"qa-v1:{sha256(value)}"


def validate_qa_plan(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "plan_id", "workflow_kind", "objective", "scope", "risks",
        "environments", "entry_criteria", "exit_criteria", "coverage", "verification",
        "status", "blockers", "fingerprint",
    }
    _exact(value, fields, "qa-plan")
    require_version(value)
    _nonempty(value, {"plan_id", "objective"}, "qa-plan")
    if value["workflow_kind"] not in _WORKFLOWS:
        raise ContractError("qa-plan workflow_kind is invalid")
    _validate_scope(value["scope"], "qa-plan.scope")
    if not isinstance(value["risks"], list):
        raise ContractError("qa-plan risks must be an array")
    risk_ids: list[str] = []
    for risk in value["risks"]:
        _exact(
            risk,
            {"id", "title", "likelihood", "impact", "categories", "requirement_refs"},
            "qa-plan.risk",
        )
        _nonempty(risk, {"id", "title"}, "qa-plan.risk")
        for field in ("likelihood", "impact"):
            score = risk[field]
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                raise ContractError(f"qa-plan risk {field} is invalid")
        _strings(risk["categories"], "qa-plan risk categories", nonempty=True)
        _sorted_unique(risk["categories"], "qa-plan risk categories")
        _strings(risk["requirement_refs"], "qa-plan risk requirement_refs")
        _sorted_unique(risk["requirement_refs"], "qa-plan risk requirement_refs")
        risk_ids.append(risk["id"])
    _sorted_unique(risk_ids, "qa-plan risk ids")
    _validate_environments(value["environments"], "qa-plan")
    for field in ("entry_criteria", "exit_criteria", "blockers"):
        _strings(value[field], f"qa-plan {field}", nonempty=field != "blockers")
        _sorted_unique(value[field], f"qa-plan {field}")
    _validate_coverage(value["coverage"], risk_ids)
    expected_coverage = compile_coverage(
        value["risks"],
        workflow_kind=value["workflow_kind"],
        requested_level=value["coverage"]["requested_level"],
    )
    if value["coverage"] != expected_coverage:
        raise ContractError("qa-plan coverage differs from canonical risk compilation")
    verification = value["verification"]
    _exact(verification, {"level", "status", "evidence_refs"}, "qa-plan.verification")
    if verification["level"] not in _VERIFICATION_LEVELS:
        raise ContractError("qa-plan verification level is invalid")
    if verification["status"] not in {"pending", *_TERMINAL_QA_STATUSES}:
        raise ContractError("qa-plan verification status is invalid")
    _validate_evidence_refs(verification["evidence_refs"], "qa-plan.verification", allow_empty=True)
    if verification["status"] == "passed" and not verification["evidence_refs"]:
        raise ContractError("passed qa-plan verification requires evidence")
    if value["status"] not in {"planned", "partial", "blocked", "cancelled"}:
        raise ContractError("qa-plan status is invalid")
    if value["status"] == "blocked" and not value["blockers"]:
        raise ContractError("blocked qa-plan requires blockers")
    if value["status"] != "blocked" and value["blockers"]:
        raise ContractError("non-blocked qa-plan cannot carry blockers")
    _validate_fingerprint(value, fields, "qa-plan", "plan_id")


def validate_test_case(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "case_id", "title", "requirement_refs", "risk_refs", "preconditions",
        "test_data", "steps", "expected_results", "coverage_tags", "automation_suitability",
        "status", "fingerprint",
    }
    _exact(value, fields, "test-case")
    require_version(value)
    _nonempty(value, {"case_id", "title"}, "test-case")
    for field in ("requirement_refs", "risk_refs", "preconditions", "expected_results", "coverage_tags"):
        _strings(value[field], f"test-case {field}", nonempty=field in {"preconditions", "expected_results", "coverage_tags"})
        _sorted_unique(value[field], f"test-case {field}")
    if not isinstance(value["test_data"], dict):
        raise ContractError("test-case test_data must be an object")
    if not isinstance(value["steps"], list) or not value["steps"]:
        raise ContractError("test-case steps must be non-empty")
    for step_number, step in enumerate(value["steps"], start=1):
        _exact(step, {"number", "action", "expected"}, "test-case.step")
        if step["number"] != step_number:
            raise ContractError("test-case step numbers must be contiguous")
        _nonempty(step, {"action", "expected"}, "test-case.step")
    if value["automation_suitability"] not in {"high", "medium", "low", "none"}:
        raise ContractError("test-case automation_suitability is invalid")
    if value["status"] not in {"active", "deprecated"}:
        raise ContractError("test-case status is invalid")
    _validate_fingerprint(value, fields, "test-case", "case_id")


def validate_test_result(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "result_id", "attempt_id", "case_id", "plan_fingerprint",
        "environment_fingerprint", "test_data_fingerprint", "status", "evidence_refs",
        "defect_refs", "blockers", "fingerprint",
    }
    _exact(value, fields, "test-result")
    require_version(value)
    _nonempty(value, {"result_id", "attempt_id", "case_id"}, "test-result")
    for field in ("plan_fingerprint", "test_data_fingerprint"):
        _require_fingerprint(value[field], f"test-result {field}")
    _require_environment_fingerprint(value["environment_fingerprint"], "test-result environment_fingerprint")
    if value["status"] not in _TERMINAL_QA_STATUSES - {"partial"} | {"skipped"}:
        raise ContractError("test-result status is invalid")
    _validate_evidence_refs(value["evidence_refs"], "test-result", allow_empty=True)
    for field in ("defect_refs", "blockers"):
        _strings(value[field], f"test-result {field}")
        _sorted_unique(value[field], f"test-result {field}")
    if value["status"] in {"passed", "failed"} and not value["evidence_refs"]:
        raise ContractError("passed/failed test-result requires evidence")
    if value["status"] == "passed" and (value["defect_refs"] or value["blockers"]):
        raise ContractError("passed test-result cannot carry defects or blockers")
    if value["status"] == "failed" and not value["defect_refs"]:
        raise ContractError("failed test-result requires defect_refs")
    if value["status"] == "blocked" and not value["blockers"]:
        raise ContractError("blocked test-result requires blockers")
    if value["status"] not in {"blocked", "cancelled", "skipped"} and value["blockers"]:
        raise ContractError("test-result blockers conflict with status")
    _validate_fingerprint(value, fields, "test-result", "result_id")


def validate_defect_report(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "defect_id", "title", "reproduction", "severity", "priority",
        "environment_fingerprint", "evidence_refs", "attribution", "impact", "owner",
        "status", "fix_verification_result_refs", "regression_case_refs", "blockers", "fingerprint",
    }
    _exact(value, fields, "defect-report")
    require_version(value)
    _nonempty(value, {"defect_id", "title", "owner"}, "defect-report")
    reproduction = value["reproduction"]
    _exact(reproduction, {"rate", "steps", "expected", "actual"}, "defect-report.reproduction")
    if reproduction["rate"] not in {"always", "intermittent", "once", "not-reproduced"}:
        raise ContractError("defect-report reproduction rate is invalid")
    _strings(reproduction["steps"], "defect-report reproduction steps", nonempty=True)
    _nonempty(reproduction, {"expected", "actual"}, "defect-report.reproduction")
    if value["severity"] not in {"blocker", "critical", "major", "minor", "trivial"}:
        raise ContractError("defect-report severity is invalid")
    if value["priority"] not in {"p0", "p1", "p2", "p3", "p4"}:
        raise ContractError("defect-report priority is invalid")
    _require_environment_fingerprint(value["environment_fingerprint"], "defect-report environment_fingerprint")
    _validate_evidence_refs(value["evidence_refs"], "defect-report", allow_empty=False)
    attribution = value["attribution"]
    _exact(attribution, {"category", "confidence", "component"}, "defect-report.attribution")
    if attribution["category"] not in {"code", "environment", "test-data", "requirement", "unknown"}:
        raise ContractError("defect-report attribution category is invalid")
    if not isinstance(attribution["confidence"], (int, float)) or isinstance(attribution["confidence"], bool) or not 0 <= attribution["confidence"] <= 1:
        raise ContractError("defect-report attribution confidence is invalid")
    if attribution["component"] is not None and not _is_nonempty(attribution["component"]):
        raise ContractError("defect-report attribution component is invalid")
    impact = value["impact"]
    _exact(impact, {"scope", "regression_risk"}, "defect-report.impact")
    _strings(impact["scope"], "defect-report impact scope", nonempty=True)
    _sorted_unique(impact["scope"], "defect-report impact scope")
    if impact["regression_risk"] not in {"low", "medium", "high"}:
        raise ContractError("defect-report regression_risk is invalid")
    if value["status"] not in {"open", "fixed", "verified", "closed", "blocked", "reopened"}:
        raise ContractError("defect-report status is invalid")
    for field in ("fix_verification_result_refs", "regression_case_refs", "blockers"):
        _strings(value[field], f"defect-report {field}")
        _sorted_unique(value[field], f"defect-report {field}")
    if value["status"] in {"verified", "closed"} and (
        not value["fix_verification_result_refs"] or not value["regression_case_refs"]
    ):
        raise ContractError("verified/closed defect-report requires fix verification and regression ownership")
    if value["status"] == "blocked" and not value["blockers"]:
        raise ContractError("blocked defect-report requires blockers")
    if reproduction["rate"] == "not-reproduced" and not value["blockers"]:
        raise ContractError("not-reproduced defect-report requires next evidence blocker")
    if value["status"] not in {"blocked", "open"} and value["blockers"]:
        raise ContractError("defect-report blockers conflict with status")
    _validate_fingerprint(value, fields, "defect-report", "defect_id")


def validate_regression_set(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "set_id", "case_refs", "defect_refs", "source_fingerprints",
        "environment_fingerprints", "status", "stale_reasons", "fingerprint",
    }
    _exact(value, fields, "regression-set")
    require_version(value)
    _nonempty(value, {"set_id"}, "regression-set")
    for field in ("case_refs", "defect_refs"):
        _strings(value[field], f"regression-set {field}", nonempty=field == "case_refs")
        _sorted_unique(value[field], f"regression-set {field}")
    for field in ("source_fingerprints", "environment_fingerprints"):
        _strings(value[field], f"regression-set {field}", nonempty=True)
        _sorted_unique(value[field], f"regression-set {field}")
        for fingerprint in value[field]:
            if field == "environment_fingerprints":
                _require_environment_fingerprint(fingerprint, f"regression-set {field}")
            else:
                _require_fingerprint(fingerprint, f"regression-set {field}")
    if value["status"] not in {"current", "stale", "blocked"}:
        raise ContractError("regression-set status is invalid")
    _strings(value["stale_reasons"], "regression-set stale_reasons")
    _sorted_unique(value["stale_reasons"], "regression-set stale_reasons")
    if value["status"] == "current" and value["stale_reasons"]:
        raise ContractError("current regression-set cannot carry stale reasons")
    if value["status"] in {"stale", "blocked"} and not value["stale_reasons"]:
        raise ContractError("stale/blocked regression-set requires reasons")
    _validate_fingerprint(value, fields, "regression-set", "set_id")


def validate_qa_report(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "report_id", "plan_fingerprint", "workflow_kind", "status",
        "delivery_context", "evaluated_on", "verification", "quality", "waivers",
        "release_recommendation", "residual_risks", "evidence_refs", "blockers", "fingerprint",
    }
    _exact(value, fields, "qa-report")
    require_version(value)
    _nonempty(value, {"report_id"}, "qa-report")
    _require_fingerprint(value["plan_fingerprint"], "qa-report plan_fingerprint")
    if value["workflow_kind"] not in _WORKFLOWS:
        raise ContractError("qa-report workflow_kind is invalid")
    if not isinstance(value["evaluated_on"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["evaluated_on"]):
        raise ContractError("qa-report evaluated_on is invalid")
    try:
        evaluated_on = date.fromisoformat(value["evaluated_on"])
    except ValueError as error:
        raise ContractError("qa-report evaluated_on is invalid") from error
    delivery_context = value["delivery_context"]
    _exact(
        delivery_context,
        {"workflow_plan_id", "workflow_plan_fingerprint", "run_id"},
        "qa-report.delivery_context",
    )
    _nonempty(
        delivery_context,
        {"workflow_plan_id", "workflow_plan_fingerprint", "run_id"},
        "qa-report.delivery_context",
    )
    if value["status"] not in _TERMINAL_QA_STATUSES:
        raise ContractError("qa-report status is invalid")
    verification = value["verification"]
    _exact(verification, {"level", "status", "evidence_refs"}, "qa-report.verification")
    if verification["level"] not in _VERIFICATION_LEVELS:
        raise ContractError("qa-report verification level is invalid")
    if verification["status"] not in _TERMINAL_QA_STATUSES:
        raise ContractError("qa-report verification status is invalid")
    _validate_evidence_refs(verification["evidence_refs"], "qa-report.verification", allow_empty=True)
    if verification["status"] == "passed" and not verification["evidence_refs"]:
        raise ContractError("passed qa-report verification requires evidence")
    quality = value["quality"]
    _exact(
        quality,
        {"coverage_level", "total", "executed", "passed", "failed", "blocked", "skipped", "cancelled", "gaps", "defect_refs"},
        "qa-report.quality",
    )
    if quality["coverage_level"] not in _COVERAGE_LEVELS:
        raise ContractError("qa-report quality coverage_level is invalid")
    counts = []
    for field in ("total", "executed", "passed", "failed", "blocked", "skipped", "cancelled"):
        count = quality[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ContractError("qa-report quality counts are invalid")
        counts.append(count)
    if quality["executed"] != quality["passed"] + quality["failed"] + quality["blocked"]:
        raise ContractError("qa-report executed count is inconsistent")
    if quality["total"] != quality["executed"] + quality["skipped"] + quality["cancelled"]:
        raise ContractError("qa-report total count is inconsistent")
    for field in ("gaps", "defect_refs"):
        _strings(quality[field], f"qa-report quality {field}")
        _sorted_unique(quality[field], f"qa-report quality {field}")
    if quality["failed"] and not quality["defect_refs"]:
        raise ContractError("qa-report failed cases require defect_refs")
    if not isinstance(value["waivers"], list):
        raise ContractError("qa-report waivers must be an array")
    waiver_ids: list[str] = []
    for waiver in value["waivers"]:
        _exact(waiver, {"id", "owner", "expires_on", "reason"}, "qa-report.waiver")
        _nonempty(waiver, {"id", "owner", "expires_on", "reason"}, "qa-report.waiver")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", waiver["expires_on"]):
            raise ContractError("qa-report waiver expires_on is invalid")
        try:
            expires_on = date.fromisoformat(waiver["expires_on"])
        except ValueError as error:
            raise ContractError("qa-report waiver expires_on is invalid") from error
        if expires_on < evaluated_on:
            raise ContractError("qa-report waiver is expired for evaluated_on")
        waiver_ids.append(waiver["id"])
    _sorted_unique(waiver_ids, "qa-report waiver ids")
    if value["release_recommendation"] not in {"go", "conditional-go", "no-go", "not-applicable"}:
        raise ContractError("qa-report release_recommendation is invalid")
    for field in ("residual_risks", "blockers"):
        _strings(value[field], f"qa-report {field}")
        _sorted_unique(value[field], f"qa-report {field}")
    _validate_evidence_refs(value["evidence_refs"], "qa-report", allow_empty=True)
    if value["status"] == "passed" and (
        quality["gaps"]
        or quality["failed"]
        or quality["blocked"]
        or quality["skipped"]
        or quality["cancelled"]
        or value["blockers"]
    ):
        raise ContractError("passed qa-report cannot hide gaps, failures, or blockers")
    if value["status"] == "passed" and (quality["executed"] == 0 or not value["evidence_refs"]):
        raise ContractError("passed qa-report requires executed cases and QA evidence")
    if value["status"] == "partial" and not quality["gaps"]:
        raise ContractError("partial qa-report requires explicit coverage gaps")
    if value["status"] == "blocked" and not value["blockers"]:
        raise ContractError("blocked qa-report requires blockers")
    if value["status"] == "failed" and not quality["failed"]:
        raise ContractError("failed qa-report requires failed test results")
    if value["status"] == "cancelled" and not quality["cancelled"]:
        raise ContractError("cancelled qa-report requires cancelled test results")
    if value["workflow_kind"] == "release" and value["release_recommendation"] == "not-applicable":
        raise ContractError("release qa-report requires a release recommendation")
    if value["workflow_kind"] != "release" and value["release_recommendation"] != "not-applicable":
        raise ContractError("non-release qa-report cannot make a release recommendation")
    if value["release_recommendation"] == "go" and (
        value["status"] != "passed"
        or verification["status"] != "passed"
        or value["residual_risks"]
        or value["waivers"]
    ):
        raise ContractError("go recommendation requires passed QA and verification without waivers or residual risks")
    if value["release_recommendation"] == "conditional-go" and (
        value["status"] not in {"partial", "passed"}
        or verification["status"] not in {"passed", "partial"}
        or not value["waivers"]
        or not value["residual_risks"]
    ):
        raise ContractError("conditional-go requires explicit waivers and residual risks")
    if value["release_recommendation"] == "no-go" and not (
        value["blockers"]
        or quality["failed"]
        or quality["blocked"]
        or verification["status"] in {"failed", "blocked", "cancelled"}
    ):
        raise ContractError("no-go requires failed or blocked quality evidence")
    if value["workflow_kind"] == "release" and verification["status"] in {"failed", "blocked", "cancelled"} and value["release_recommendation"] != "no-go":
        raise ContractError("failed or blocked release verification requires no-go")
    _validate_fingerprint(value, fields, "qa-report", "report_id")


def _validate_scope(value: Any, label: str) -> None:
    _exact(value, {"included", "excluded"}, label)
    for field in ("included", "excluded"):
        _strings(value[field], f"{label} {field}", nonempty=field == "included")
        _sorted_unique(value[field], f"{label} {field}")
    overlap = set(value["included"]) & set(value["excluded"])
    if overlap:
        raise ContractError(f"{label} included/excluded overlap")


def _validate_environments(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{label} environments must be non-empty")
    environment_ids: list[str] = []
    for environment in value:
        _exact(environment, {"id", "platform", "attributes", "fingerprint"}, f"{label}.environment")
        _nonempty(environment, {"id", "platform"}, f"{label}.environment")
        if not isinstance(environment["attributes"], dict) or not environment["attributes"]:
            raise ContractError(f"{label} environment attributes must be non-empty")
        _require_environment_fingerprint(environment["fingerprint"], f"{label} environment fingerprint")
        environment_ids.append(environment["id"])
    _sorted_unique(environment_ids, f"{label} environment ids")


def _validate_coverage(value: Any, risk_ids: list[str]) -> None:
    _exact(value, {"requested_level", "compiled_level", "dimensions", "risk_refs", "rationales"}, "qa-plan.coverage")
    if value["requested_level"] not in _COVERAGE_LEVELS or value["compiled_level"] not in _COVERAGE_LEVELS:
        raise ContractError("qa-plan coverage level is invalid")
    levels = list(_COVERAGE_LEVELS_ORDERED)
    if levels.index(value["compiled_level"]) < levels.index(value["requested_level"]):
        raise ContractError("qa-plan compiled coverage cannot be below requested level")
    for field in ("dimensions", "risk_refs", "rationales"):
        _strings(value[field], f"qa-plan coverage {field}", nonempty=field in {"dimensions", "rationales"})
        _sorted_unique(value[field], f"qa-plan coverage {field}")
    if set(value["risk_refs"]) - set(risk_ids):
        raise ContractError("qa-plan coverage references unknown risks")


_COVERAGE_LEVELS_ORDERED = (
    "smoke", "targeted", "regression", "compatibility", "end-to-end", "release-candidate",
)


def _validate_evidence_refs(value: Any, label: str, *, allow_empty: bool) -> None:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ContractError(f"{label} evidence_refs are invalid")
    identities: list[tuple[str, str]] = []
    for reference in value:
        _exact(reference, {"kind", "sha256", "uri"}, f"{label}.evidence_ref")
        if reference["kind"] not in {"log", "screenshot", "test-report", "trace", "structured-report", "video"}:
            raise ContractError(f"{label} evidence kind is invalid")
        if not isinstance(reference["sha256"], str) or not _SHA256.fullmatch(reference["sha256"]):
            raise ContractError(f"{label} evidence sha256 is invalid")
        uri = reference["uri"]
        if not isinstance(uri, str) or not uri.startswith("artifact://") or len(uri) == len("artifact://") or ".." in uri or any(character.isspace() for character in uri):
            raise ContractError(f"{label} evidence uri is uncontrolled")
        identities.append((reference["kind"], uri))
    if identities != sorted(set(identities)):
        raise ContractError(f"{label} evidence_refs must be sorted and unique")


def _validate_fingerprint(value: dict[str, Any], fields: set[str], label: str, identity_field: str) -> None:
    _require_fingerprint(value["fingerprint"], f"{label} fingerprint")
    if not _is_nonempty(value[identity_field]):
        raise ContractError(f"{label} {identity_field} is invalid")
    body = {key: value[key] for key in fields - {"fingerprint"}}
    expected = qa_fingerprint(body)
    if value["fingerprint"] != expected:
        raise ContractError(f"{label} fingerprint does not match artifact body")


def _require_fingerprint(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ContractError(f"{label} is invalid")


def _require_environment_fingerprint(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _ENVIRONMENT_FINGERPRINT.fullmatch(value):
        raise ContractError(f"{label} is invalid")


def _exact(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(f"{label} fields are invalid")


def _nonempty(value: dict[str, Any], fields: set[str], label: str) -> None:
    for field in fields:
        if not _is_nonempty(value.get(field)):
            raise ContractError(f"{label} {field} is invalid")


def _is_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value: Any, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value) or any(not _is_nonempty(item) for item in value):
        raise ContractError(f"{label} must be {'non-empty ' if nonempty else ''}strings")


def _sorted_unique(value: list[str], label: str) -> None:
    if value != sorted(set(value)):
        raise ContractError(f"{label} must be sorted and unique")
