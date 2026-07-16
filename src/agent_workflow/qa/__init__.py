"""Platform-neutral QA contracts and deterministic coverage planning."""

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
from .workflows import (
    BUG_DIMENSIONS,
    PRD_DIMENSIONS,
    RELEASE_DIMENSIONS,
    aggregate_workflow_results,
    compile_bug_workflow,
    compile_prd_workflow,
    compile_release_workflow,
    validate_compiled_workflow,
    validate_workflow_bundle,
)
from .runtime import (
    FailFixReportGuard,
    evidence_reuse_status,
    qa_execution_identity,
    refresh_regression_set,
    reopen_defect,
)

__all__ = [
    "compile_coverage",
    "aggregate_workflow_results",
    "compile_bug_workflow",
    "compile_prd_workflow",
    "compile_release_workflow",
    "qa_fingerprint",
    "validate_defect_report",
    "validate_compiled_workflow",
    "validate_qa_plan",
    "validate_qa_report",
    "validate_regression_set",
    "validate_test_case",
    "validate_test_result",
    "validate_workflow_bundle",
    "BUG_DIMENSIONS",
    "PRD_DIMENSIONS",
    "RELEASE_DIMENSIONS",
    "FailFixReportGuard",
    "evidence_reuse_status",
    "qa_execution_identity",
    "refresh_regression_set",
    "reopen_defect",
]
