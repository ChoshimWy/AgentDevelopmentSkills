"""Deterministic Phase 4 PRD, Bug and Release QA workflow compilers."""

from __future__ import annotations

from typing import Any

from ..models import ContractError, require_version
from ..ledger_status import current_validation_status
from .contracts import (
    qa_fingerprint,
    validate_defect_report,
    validate_qa_plan,
    validate_qa_report,
    validate_regression_set,
    validate_test_case,
    validate_test_result,
)
from .coverage import compile_coverage


PRD_DIMENSIONS = (
    "accessibility", "boundary", "compatibility", "exception", "offline", "permission",
)
BUG_DIMENSIONS = ("fix-verification", "regression-impact", "reproduction")
RELEASE_DIMENSIONS = ("change-inventory", "compatibility", "known-issues", "risk-sampling")
WORKFLOW_OUTCOMES = {"passed", "failed", "blocked", "partial", "cancelled"}
COMPILED_WORKFLOW_FIELDS = {
    "schema_version", "workflow_id", "workflow_kind", "plan", "test_cases",
    "traceability", "known_issues", "fingerprint",
}
EXECUTION_FIELDS = {
    "schema_version", "test_results", "defect_reports", "regression_set",
    "verification", "qa_evidence_refs", "workflow_plan", "run_ledger", "evaluated_on",
    "declared_gaps", "blockers", "waivers", "residual_risks",
}


def compile_prd_workflow(request: dict[str, Any]) -> dict[str, Any]:
    """Compile traceable requirements into the six mandatory PRD dimensions."""

    requirements = request.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ContractError("prd workflow requirements must be non-empty")
    normalized: list[dict[str, Any]] = []
    for requirement in requirements:
        if not isinstance(requirement, dict) or set(requirement) != {"id", "title", "acceptance_criteria"}:
            raise ContractError("prd workflow requirement fields are invalid")
        _nonempty(requirement, ("id", "title"), "prd workflow requirement")
        criteria = _sorted_strings(requirement["acceptance_criteria"], "prd acceptance_criteria", nonempty=True)
        normalized.append({**requirement, "acceptance_criteria": criteria})
    normalized.sort(key=lambda item: item["id"])
    if [item["id"] for item in normalized] != sorted({item["id"] for item in normalized}):
        raise ContractError("prd workflow requirement ids must be unique")
    subjects = [
        {
            "acceptance": criterion,
            "dimensions": list(PRD_DIMENSIONS),
            "id": requirement["id"],
            "title": requirement["title"],
        }
        for requirement in normalized
        for criterion in requirement["acceptance_criteria"]
    ]
    return _compile_plan(request, "prd", subjects)


def compile_bug_workflow(request: dict[str, Any]) -> dict[str, Any]:
    """Compile reproduction, fix verification and regression ownership for one bug."""

    defect = request.get("defect")
    required = {"id", "title", "expected", "actual", "reproduction_steps", "owner", "severity", "priority"}
    if not isinstance(defect, dict) or set(defect) != required:
        raise ContractError("bug workflow defect fields are invalid")
    _nonempty(defect, required - {"reproduction_steps"}, "bug workflow defect")
    _ordered_strings(defect["reproduction_steps"], "bug reproduction_steps", nonempty=True)
    subject = {
        "acceptance": f"Expected {defect['expected']}; reported {defect['actual']}",
        "dimensions": list(BUG_DIMENSIONS),
        "id": defect["id"],
        "title": defect["title"],
    }
    return _compile_plan(request, "bug", [subject])


def compile_release_workflow(request: dict[str, Any]) -> dict[str, Any]:
    """Compile change inventory, risk sampling, compatibility and known issues."""

    changes = request.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ContractError("release workflow changes must be non-empty")
    subjects: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"id", "title", "acceptance_criteria"}:
            raise ContractError("release workflow change fields are invalid")
        _nonempty(change, ("id", "title", "acceptance_criteria"), "release workflow change")
        subjects.append({
            "acceptance": change["acceptance_criteria"],
            "dimensions": list(RELEASE_DIMENSIONS),
            "id": change["id"],
            "title": change["title"],
        })
    subjects.sort(key=lambda item: item["id"])
    if [item["id"] for item in subjects] != sorted({item["id"] for item in subjects}):
        raise ContractError("release workflow change ids must be unique")
    known_issues = _sorted_strings(request.get("known_issues", []), "release known_issues")
    return _compile_plan(request, "release", subjects, known_issues=known_issues)


def validate_compiled_workflow(value: dict[str, Any]) -> None:
    """Validate a planning-only workflow artifact with no execution claims."""

    if not isinstance(value, dict) or set(value) != COMPILED_WORKFLOW_FIELDS:
        raise ContractError("compiled-qa-workflow fields are invalid")
    require_version(value)
    _nonempty(value, ("workflow_id",), "compiled-qa-workflow")
    if value["workflow_kind"] not in {"prd", "bug", "release"}:
        raise ContractError("compiled-qa-workflow workflow_kind is invalid")
    validate_qa_plan(value["plan"])
    if value["plan"]["workflow_kind"] != value["workflow_kind"]:
        raise ContractError("compiled-qa-workflow plan kind is inconsistent")
    if value["plan"]["status"] != "planned" or value["plan"]["verification"]["status"] != "pending":
        raise ContractError("compiled-qa-workflow cannot claim execution status")
    if value["plan"]["verification"]["evidence_refs"] or value["plan"]["blockers"]:
        raise ContractError("compiled-qa-workflow cannot contain execution evidence or blockers")
    _sorted_strings(value["known_issues"], "compiled-qa-workflow known_issues")
    if value["workflow_kind"] != "release" and value["known_issues"]:
        raise ContractError("non-release compiled workflow cannot carry known issues")

    cases = value["test_cases"]
    if not isinstance(cases, list) or not cases:
        raise ContractError("compiled-qa-workflow test_cases must be non-empty")
    case_ids: list[str] = []
    for case in cases:
        validate_test_case(case)
        case_ids.append(case["case_id"])
    if case_ids != sorted(set(case_ids)):
        raise ContractError("compiled-qa-workflow case ids must be sorted and unique")

    traced_cases: set[str] = set()
    refs: list[str] = []
    if not isinstance(value["traceability"], list) or not value["traceability"]:
        raise ContractError("compiled-qa-workflow traceability must be non-empty")
    for row in value["traceability"]:
        if not isinstance(row, dict) or set(row) != {"requirement_ref", "case_refs", "coverage_dimensions"}:
            raise ContractError("compiled-qa-workflow traceability row is invalid")
        _nonempty(row, ("requirement_ref",), "compiled-qa-workflow traceability")
        row_cases = _sorted_strings(row["case_refs"], "compiled-qa-workflow case_refs", nonempty=True)
        _sorted_strings(row["coverage_dimensions"], "compiled-qa-workflow coverage_dimensions", nonempty=True)
        if set(row_cases) - set(case_ids):
            raise ContractError("compiled-qa-workflow traceability references unknown cases")
        traced_cases.update(row_cases)
        refs.append(row["requirement_ref"])
    if refs != sorted(set(refs)) or traced_cases != set(case_ids):
        raise ContractError("compiled-qa-workflow traceability is incomplete")
    _validate_body_fingerprint(value, COMPILED_WORKFLOW_FIELDS, "compiled-qa-workflow")


def aggregate_workflow_results(compiled: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    """Aggregate caller-supplied execution artifacts; never manufacture evidence or outcomes."""

    # Import lazily because the shared contract registry imports QA validators.
    from ..contracts import validate_run_ledger, validate_workflow_plan

    validate_compiled_workflow(compiled)
    if not isinstance(execution, dict) or set(execution) != EXECUTION_FIELDS:
        raise ContractError("qa workflow execution fields are invalid")
    require_version(execution)

    workflow_plan = execution["workflow_plan"]
    run_ledger = execution["run_ledger"]
    validate_workflow_plan(workflow_plan)
    validate_run_ledger(run_ledger)
    if run_ledger["plan_fingerprint"] != workflow_plan["fingerprint"]:
        raise ContractError("qa workflow ledger references a different workflow plan")
    ledger = {
        "run_id": run_ledger["run_id"],
        "workflow_plan_fingerprint": workflow_plan["fingerprint"],
        "workflow_plan_id": workflow_plan["plan_id"],
    }

    plan = compiled["plan"]
    cases = compiled["test_cases"]
    case_by_id = {case["case_id"]: case for case in cases}
    environment_fingerprints = {item["fingerprint"] for item in plan["environments"]}
    results = execution["test_results"]
    if not isinstance(results, list) or not results:
        raise ContractError("qa workflow execution test_results must be non-empty")
    result_ids: list[str] = []
    result_case_ids: list[str] = []
    for result in results:
        validate_test_result(result)
        case = case_by_id.get(result["case_id"])
        if case is None or result["plan_fingerprint"] != plan["fingerprint"]:
            raise ContractError("qa workflow execution result linkage is invalid")
        if result["environment_fingerprint"] not in environment_fingerprints:
            raise ContractError("qa workflow execution result environment is outside the frozen plan")
        if result["test_data_fingerprint"] != qa_fingerprint(case["test_data"]):
            raise ContractError("qa workflow execution result test data is stale")
        result_ids.append(result["result_id"])
        result_case_ids.append(result["case_id"])
    if result_ids != sorted(set(result_ids)) or result_case_ids != sorted(case_by_id):
        raise ContractError("qa workflow execution must provide one sorted result per frozen case")

    defects = execution["defect_reports"]
    if not isinstance(defects, list):
        raise ContractError("qa workflow execution defect_reports must be an array")
    defect_ids: list[str] = []
    for defect in defects:
        validate_defect_report(defect)
        if defect["environment_fingerprint"] not in environment_fingerprints:
            raise ContractError("qa workflow execution defect environment is outside the frozen plan")
        if set(defect["fix_verification_result_refs"]) - set(result_ids):
            raise ContractError("qa workflow execution defect fix result is unknown")
        if set(defect["regression_case_refs"]) - set(case_by_id):
            raise ContractError("qa workflow execution defect regression case is unknown")
        defect_ids.append(defect["defect_id"])
    if defect_ids != sorted(set(defect_ids)):
        raise ContractError("qa workflow execution defect ids must be sorted and unique")
    for result in results:
        if set(result["defect_refs"]) - set(defect_ids):
            raise ContractError("qa workflow execution result defect is unknown")

    regression = execution["regression_set"]
    if regression is not None:
        validate_regression_set(regression)
        if set(regression["case_refs"]) - set(case_by_id) or set(regression["defect_refs"]) - set(defect_ids):
            raise ContractError("qa workflow execution regression linkage is invalid")
        if set(regression["environment_fingerprints"]) - environment_fingerprints:
            raise ContractError("qa workflow execution regression environment is outside the frozen plan")
    if compiled["workflow_kind"] == "bug" and regression is None:
        raise ContractError("bug workflow execution requires a regression set")

    result_by_id = {result["result_id"]: result for result in results}
    for defect in defects:
        if defect["status"] in {"verified", "closed"}:
            if any(result_by_id[result_id]["status"] != "passed" for result_id in defect["fix_verification_result_refs"]):
                raise ContractError("closed defect fix verification must reference passed results")
            if (
                regression is None
                or regression["status"] != "current"
                or defect["defect_id"] not in regression["defect_refs"]
                or set(defect["regression_case_refs"]) - set(regression["case_refs"])
                or defect["fingerprint"] not in regression["source_fingerprints"]
            ):
                raise ContractError("closed defect requires current regression ownership")

    verification = execution["verification"]
    if not isinstance(verification, dict) or set(verification) != {"level", "status", "evidence_refs"}:
        raise ContractError("qa workflow execution verification fields are invalid")
    if verification["status"] == "passed" and not verification["evidence_refs"]:
        raise ContractError("passed verification requires caller-supplied evidence")
    ledger_validation_status = current_validation_status(run_ledger)
    if verification["status"] != ledger_validation_status:
        raise ContractError("qa workflow verification conflicts with the current run ledger")

    unresolved_defects = [
        defect for defect in defects if defect["status"] in {"open", "fixed", "reopened", "blocked"}
    ]
    artifact_blockers = [
        f"blocked-defect:{defect['defect_id']}" for defect in unresolved_defects if defect["status"] == "blocked"
    ]
    artifact_gaps = [
        f"unresolved-defect:{defect['defect_id']}" for defect in unresolved_defects if defect["status"] != "blocked"
    ]
    if regression is not None and regression["status"] == "blocked":
        artifact_blockers.append(f"blocked-regression-set:{regression['set_id']}")
    elif regression is not None and regression["status"] == "stale":
        artifact_gaps.append(f"stale-regression-set:{regression['set_id']}")

    declared_gaps = _sorted_strings(execution["declared_gaps"], "qa workflow execution declared_gaps")
    supplied_blockers = _sorted_strings(execution["blockers"], "qa workflow execution blockers")
    result_blockers = sorted({blocker for result in results for blocker in result["blockers"]})
    blockers = sorted(set(supplied_blockers + result_blockers + artifact_blockers))
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("passed", "failed", "blocked", "skipped", "cancelled")
    }
    unexecuted = [f"unexecuted:{result['case_id']}" for result in results if result["status"] in {"skipped", "cancelled"}]
    gaps = sorted(set(declared_gaps + unexecuted + artifact_gaps))
    if counts["cancelled"]:
        status = "cancelled"
    elif blockers or counts["blocked"]:
        status = "blocked"
    elif counts["failed"]:
        status = "failed"
    elif counts["skipped"] or gaps:
        status = "partial"
    else:
        status = "passed"

    if status == "blocked" and not blockers:
        raise ContractError("blocked QA aggregation requires explicit blockers")
    if status == "partial" and not gaps:
        raise ContractError("partial QA aggregation requires explicit gaps")

    qa_evidence = execution["qa_evidence_refs"]
    waivers = execution["waivers"]
    if not isinstance(waivers, list):
        raise ContractError("qa workflow execution waivers must be an array")
    residual_risks = sorted(set(_sorted_strings(execution["residual_risks"], "qa workflow execution residual_risks") + compiled["known_issues"] + gaps))
    recommendation = "not-applicable"
    if compiled["workflow_kind"] == "release":
        if status == "passed" and verification["status"] == "passed" and not residual_risks and not waivers:
            recommendation = "go"
        elif status in {"passed", "partial"} and verification["status"] in {"passed", "partial"}:
            if not waivers or not residual_risks:
                raise ContractError("conditional release requires caller-supplied waivers and residual risks")
            recommendation = "conditional-go"
        else:
            recommendation = "no-go"

    report = _identified("report_id", f"QAR-{compiled['workflow_id']}", {
        "blockers": blockers,
        "delivery_context": {
            "run_id": ledger["run_id"],
            "workflow_plan_fingerprint": ledger["workflow_plan_fingerprint"],
            "workflow_plan_id": ledger["workflow_plan_id"],
        },
        "evaluated_on": execution["evaluated_on"],
        "evidence_refs": qa_evidence,
        "plan_fingerprint": plan["fingerprint"],
        "quality": {
            "blocked": counts["blocked"],
            "cancelled": counts["cancelled"],
            "coverage_level": plan["coverage"]["compiled_level"],
            "defect_refs": defect_ids,
            "executed": counts["passed"] + counts["failed"] + counts["blocked"],
            "failed": counts["failed"],
            "gaps": gaps,
            "passed": counts["passed"],
            "skipped": counts["skipped"],
            "total": len(results),
        },
        "release_recommendation": recommendation,
        "residual_risks": residual_risks,
        "schema_version": "1.0",
        "status": status,
        "verification": verification,
        "waivers": waivers,
        "workflow_kind": compiled["workflow_kind"],
    })
    next_actions = {
        "passed": ["none"],
        "failed": ["fix", "rerun-regression"],
        "blocked": ["collect-evidence", "resolve-platform-blocker"],
        "partial": ["close-coverage-gaps"],
        "cancelled": ["confirm-cleanup", "reschedule"],
    }[status]
    bundle = {
        "blockers": blockers,
        "defect_reports": defects,
        "gaps": gaps,
        "ledger_identity": ledger,
        "next_actions": next_actions,
        "plan": plan,
        "regression_set": regression,
        "report": report,
        "schema_version": "1.0",
        "status": status,
        "test_cases": cases,
        "test_results": results,
        "traceability": compiled["traceability"],
        "workflow_id": compiled["workflow_id"],
        "workflow_kind": compiled["workflow_kind"],
    }
    bundle["fingerprint"] = qa_fingerprint(bundle)
    validate_workflow_bundle(bundle)
    return bundle


def validate_workflow_bundle(value: dict[str, Any]) -> None:
    """Validate artifact contracts and cross-artifact traceability."""

    fields = {
        "schema_version", "workflow_id", "workflow_kind", "status", "plan", "test_cases",
        "test_results", "defect_reports", "regression_set", "traceability", "gaps",
        "blockers", "next_actions", "report", "ledger_identity", "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError("qa-workflow-bundle fields are invalid")
    require_version(value)
    _nonempty(value, ("workflow_id",), "qa-workflow-bundle")
    if value["workflow_kind"] not in {"prd", "bug", "release"}:
        raise ContractError("qa-workflow-bundle workflow_kind is invalid")
    if value["status"] not in WORKFLOW_OUTCOMES:
        raise ContractError("qa-workflow-bundle status is invalid")
    validate_qa_plan(value["plan"])
    validate_qa_report(value["report"])
    if value["plan"]["workflow_kind"] != value["workflow_kind"] or value["report"]["workflow_kind"] != value["workflow_kind"]:
        raise ContractError("qa-workflow-bundle workflow kind linkage is invalid")
    if value["report"]["plan_fingerprint"] != value["plan"]["fingerprint"]:
        raise ContractError("qa-workflow-bundle report references a stale plan")
    if value["report"]["status"] != value["status"]:
        raise ContractError("qa-workflow-bundle status differs from report")
    ledger = value["ledger_identity"]
    if not isinstance(ledger, dict) or set(ledger) != {"run_id", "workflow_plan_id", "workflow_plan_fingerprint"}:
        raise ContractError("qa-workflow-bundle ledger identity fields are invalid")
    _nonempty(ledger, ("run_id", "workflow_plan_id"), "qa-workflow-bundle ledger identity")
    _require_sha256(ledger["workflow_plan_fingerprint"], "qa-workflow-bundle workflow plan fingerprint")
    if value["report"]["delivery_context"] != {key: ledger[key] for key in ("run_id", "workflow_plan_id", "workflow_plan_fingerprint")}:
        raise ContractError("qa-workflow-bundle report ledger identity is inconsistent")

    cases = value["test_cases"]
    results = value["test_results"]
    defects = value["defect_reports"]
    if not isinstance(cases, list) or not cases or not isinstance(results, list) or not isinstance(defects, list):
        raise ContractError("qa-workflow-bundle artifact arrays are invalid")
    case_ids: list[str] = []
    for case in cases:
        validate_test_case(case)
        case_ids.append(case["case_id"])
    result_ids: list[str] = []
    defect_ids: list[str] = []
    for result in results:
        validate_test_result(result)
        if result["case_id"] not in case_ids or result["plan_fingerprint"] != value["plan"]["fingerprint"]:
            raise ContractError("qa-workflow-bundle result linkage is invalid")
        result_ids.append(result["result_id"])
    for defect in defects:
        validate_defect_report(defect)
        if set(defect["fix_verification_result_refs"]) - set(result_ids):
            raise ContractError("qa-workflow-bundle defect fix result is unknown")
        if set(defect["regression_case_refs"]) - set(case_ids):
            raise ContractError("qa-workflow-bundle defect regression case is unknown")
        defect_ids.append(defect["defect_id"])
    for result in results:
        if set(result["defect_refs"]) - set(defect_ids):
            raise ContractError("qa-workflow-bundle result defect is unknown")

    if case_ids != sorted(set(case_ids)) or result_ids != sorted(set(result_ids)) or defect_ids != sorted(set(defect_ids)):
        raise ContractError("qa-workflow-bundle artifact ids must be sorted and unique")
    if value["regression_set"] is not None:
        validate_regression_set(value["regression_set"])
        if set(value["regression_set"]["case_refs"]) - set(case_ids):
            raise ContractError("qa-workflow-bundle regression case is unknown")
        if set(value["regression_set"]["defect_refs"]) - set(defect_ids):
            raise ContractError("qa-workflow-bundle regression defect is unknown")
    if value["report"]["quality"]["defect_refs"] != defect_ids:
        raise ContractError("qa-workflow-bundle report must disclose every defect")
    result_by_id = {result["result_id"]: result for result in results}
    regression = value["regression_set"]
    for defect in defects:
        if defect["status"] in {"verified", "closed"}:
            if any(result_by_id[result_id]["status"] != "passed" for result_id in defect["fix_verification_result_refs"]):
                raise ContractError("qa-workflow-bundle closed defect fix verification is not passed")
            if (
                regression is None
                or regression["status"] != "current"
                or defect["defect_id"] not in regression["defect_refs"]
                or set(defect["regression_case_refs"]) - set(regression["case_refs"])
                or defect["fingerprint"] not in regression["source_fingerprints"]
            ):
                raise ContractError("qa-workflow-bundle closed defect lacks current regression ownership")
    if value["status"] == "passed":
        if any(defect["status"] not in {"verified", "closed"} for defect in defects):
            raise ContractError("passed qa-workflow-bundle cannot hide unresolved defects")
        if value["workflow_kind"] == "bug" and (regression is None or regression["status"] != "current"):
            raise ContractError("passed bug workflow requires a current regression set")

    traced_cases: set[str] = set()
    if not isinstance(value["traceability"], list) or not value["traceability"]:
        raise ContractError("qa-workflow-bundle traceability must be non-empty")
    refs: list[str] = []
    for row in value["traceability"]:
        if not isinstance(row, dict) or set(row) != {"requirement_ref", "case_refs", "coverage_dimensions"}:
            raise ContractError("qa-workflow-bundle traceability row is invalid")
        _nonempty(row, ("requirement_ref",), "qa-workflow-bundle traceability")
        row_cases = _sorted_strings(row["case_refs"], "qa-workflow-bundle case_refs", nonempty=True)
        _sorted_strings(row["coverage_dimensions"], "qa-workflow-bundle coverage_dimensions", nonempty=True)
        if set(row_cases) - set(case_ids):
            raise ContractError("qa-workflow-bundle traceability references unknown cases")
        traced_cases.update(row_cases)
        refs.append(row["requirement_ref"])
    if refs != sorted(set(refs)) or traced_cases != set(case_ids):
        raise ContractError("qa-workflow-bundle traceability is incomplete")
    gaps = _sorted_strings(value["gaps"], "qa-workflow-bundle gaps")
    blockers = _sorted_strings(value["blockers"], "qa-workflow-bundle blockers")
    _sorted_strings(value["next_actions"], "qa-workflow-bundle next_actions", nonempty=True)
    if value["status"] == "passed" and (gaps or blockers or any(result["status"] != "passed" for result in results)):
        raise ContractError("passed qa-workflow-bundle cannot hide unexecuted coverage")
    if value["status"] == "partial" and not gaps:
        raise ContractError("partial qa-workflow-bundle requires explicit gaps")
    if value["status"] == "blocked" and not blockers:
        raise ContractError("blocked qa-workflow-bundle requires blockers")
    body = {key: value[key] for key in fields - {"fingerprint"}}
    if value["fingerprint"] != qa_fingerprint(body):
        raise ContractError("qa-workflow-bundle fingerprint does not match artifact body")


def _compile_plan(
    request: dict[str, Any],
    workflow_kind: str,
    subjects: list[dict[str, Any]],
    *,
    known_issues: list[str] | None = None,
) -> dict[str, Any]:
    require_version(request)
    forbidden = {
        "outcome", "test_results", "defect_reports", "verification", "evidence_refs",
        "report", "release_recommendation", "blockers", "gaps", "waivers",
    }
    if forbidden & set(request):
        raise ContractError(f"{workflow_kind} workflow compiler accepts planning inputs only")
    workflow_id = request.get("workflow_id")
    objective = request.get("objective")
    _nonempty(request, ("workflow_id", "objective"), f"{workflow_kind} workflow")
    risks = _normalize_risks(request.get("risks", []))
    environments = _normalize_environments(request.get("environments"))
    scope = request.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"included", "excluded"}:
        raise ContractError(f"{workflow_kind} workflow scope is invalid")
    scope = {
        "excluded": _sorted_strings(scope["excluded"], f"{workflow_kind} scope excluded"),
        "included": _sorted_strings(scope["included"], f"{workflow_kind} scope included", nonempty=True),
    }
    coverage = compile_coverage(
        risks,
        workflow_kind=workflow_kind,
        requested_level=request.get("requested_level", "targeted"),
    )
    plan = _identified("plan_id", f"QAP-{workflow_id}", {
        "blockers": [],
        "coverage": coverage,
        "entry_criteria": _sorted_strings(request.get("entry_criteria", ["scope-frozen"]), "qa entry_criteria", nonempty=True),
        "environments": environments,
        "exit_criteria": _sorted_strings(request.get("exit_criteria", ["evidence-reviewed", "risks-disclosed"]), "qa exit_criteria", nonempty=True),
        "objective": objective,
        "risks": risks,
        "schema_version": "1.0",
        "scope": scope,
        "status": "planned",
        "verification": {
            "evidence_refs": [],
            "level": request.get("verification_level", "affected-tests"),
            "status": "pending",
        },
        "workflow_kind": workflow_kind,
    })

    cases: list[dict[str, Any]] = []
    trace_by_ref: dict[str, dict[str, set[str]]] = {}
    for subject in subjects:
        for dimension in subject["dimensions"]:
            case_id = f"TC-{workflow_id}-{len(cases) + 1:02d}"
            risk_refs = sorted(
                risk["id"] for risk in risks
                if not risk["requirement_refs"] or subject["id"] in risk["requirement_refs"]
            )
            cases.append(_identified("case_id", case_id, {
                "automation_suitability": "medium" if dimension in {"accessibility", "known-issues", "permission"} else "high",
                "coverage_tags": sorted({dimension, workflow_kind}),
                "expected_results": [subject["acceptance"]],
                "preconditions": ["environment-fingerprint-frozen", "scope-frozen"],
                "requirement_refs": [subject["id"]],
                "risk_refs": risk_refs,
                "schema_version": "1.0",
                "status": "active",
                "steps": [{"action": f"Exercise {dimension} behavior for {subject['title']}.", "expected": subject["acceptance"], "number": 1}],
                "test_data": {"dimension": dimension, "subject": subject["id"]},
                "title": f"{subject['title']} — {dimension}",
            }))
            row = trace_by_ref.setdefault(subject["id"], {"case_refs": set(), "coverage_dimensions": set()})
            row["case_refs"].add(case_id)
            row["coverage_dimensions"].add(dimension)
    compiled = {
        "known_issues": known_issues or [],
        "plan": plan,
        "schema_version": "1.0",
        "test_cases": cases,
        "traceability": [
            {
                "case_refs": sorted(row["case_refs"]),
                "coverage_dimensions": sorted(row["coverage_dimensions"]),
                "requirement_ref": requirement_ref,
            }
            for requirement_ref, row in sorted(trace_by_ref.items())
        ],
        "workflow_id": workflow_id,
        "workflow_kind": workflow_kind,
    }
    compiled["fingerprint"] = qa_fingerprint(compiled)
    validate_compiled_workflow(compiled)
    return compiled


def _identified(identity_field: str, identity: str, body: dict[str, Any]) -> dict[str, Any]:
    value = {identity_field: identity, **body}
    return {**value, "fingerprint": qa_fingerprint(value)}


def _validate_body_fingerprint(value: dict[str, Any], fields: set[str], label: str) -> None:
    _require_qa_fingerprint(value.get("fingerprint"), f"{label} fingerprint")
    body = {key: value[key] for key in fields - {"fingerprint"}}
    if value["fingerprint"] != qa_fingerprint(body):
        raise ContractError(f"{label} fingerprint does not match artifact body")


def _require_qa_fingerprint(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("qa-v1:") or len(value) != 70:
        raise ContractError(f"{label} is invalid")


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ContractError(f"{label} is invalid")
    digest = value.removeprefix("qa-v1:")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ContractError(f"{label} is invalid")


def _normalize_risks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError("qa workflow risks must be an array")
    risks: list[dict[str, Any]] = []
    fields = {"id", "title", "likelihood", "impact", "categories", "requirement_refs"}
    for risk in value:
        if not isinstance(risk, dict) or set(risk) != fields:
            raise ContractError("qa workflow risk fields are invalid")
        risks.append({
            **risk,
            "categories": _sorted_strings(risk["categories"], "qa workflow risk categories", nonempty=True),
            "requirement_refs": _sorted_strings(risk["requirement_refs"], "qa workflow risk requirement_refs"),
        })
    risks.sort(key=lambda item: item["id"])
    return risks


def _normalize_environments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ContractError("qa workflow environments must be non-empty")
    environments: list[dict[str, Any]] = []
    for environment in value:
        if not isinstance(environment, dict) or set(environment) != {"id", "platform", "attributes", "fingerprint"}:
            raise ContractError("qa workflow environment fields are invalid")
        _nonempty(environment, ("id", "platform"), "qa workflow environment")
        if not isinstance(environment["attributes"], dict) or not environment["attributes"]:
            raise ContractError("qa workflow environment attributes are invalid")
        environments.append(dict(environment))
    environments.sort(key=lambda item: item["id"])
    if [item["id"] for item in environments] != sorted({item["id"] for item in environments}):
        raise ContractError("qa workflow environment ids must be unique")
    return environments


def _sorted_strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{label} must be {'non-empty ' if nonempty else ''}strings")
    return sorted(set(value))


def _ordered_strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{label} must be {'non-empty ' if nonempty else ''}strings")
    return list(value)


def _nonempty(value: dict[str, Any], fields: Any, label: str) -> None:
    for field in fields:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ContractError(f"{label} {field} is invalid")
