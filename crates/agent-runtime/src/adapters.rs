use crate::RuntimeError;
use agent_contracts::canonical_sha256;
use serde_json::{Map, Value};
use std::collections::BTreeSet;

const REQUEST_FIELDS: &[&str] = &[
    "schema_version",
    "request_id",
    "invocation_id",
    "plan_id",
    "plan_fingerprint",
    "node_id",
    "capability",
    "provider",
    "binding",
    "task_context",
    "checkpoints",
];
const RESULT_FIELDS: &[&str] = &[
    "schema_version",
    "request_id",
    "invocation_id",
    "plan_fingerprint",
    "node_id",
    "capability",
    "provider",
    "binding",
    "status",
    "evidence",
    "artifacts",
    "failure_attribution",
    "cleanup",
];

/// Freeze one workflow node and caller context into an Adapter Request v1.
///
/// # Errors
/// Returns an error when the plan, node identity, binding, or checkpoint
/// context is malformed.
pub fn build_adapter_request(
    plan: &Value,
    node_id: &str,
    context: &Value,
    invocation_id: &str,
) -> Result<Value, RuntimeError> {
    let plan = value_object(plan, "workflow-plan")?;
    require_version(plan)?;
    require_fields(plan, &["plan_id", "fingerprint", "nodes"], "workflow-plan")?;
    if node_id.is_empty() {
        return contract("adapter-request node_id is invalid");
    }
    let context = value_object(context, "adapter-request context")?;
    if !nonempty(invocation_id) {
        return contract("adapter-request invocation_id must be a non-empty string");
    }
    let checkpoints = context
        .get("checkpoints")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-request context.checkpoints must be an object".to_owned(),
            )
        })?;
    let nodes = plan
        .get("nodes")
        .and_then(Value::as_array)
        .ok_or_else(|| RuntimeError::Contract("workflow-plan nodes must be an array".to_owned()))?;
    let matches = nodes
        .iter()
        .filter(|node| {
            node.as_object()
                .and_then(|node| node.get("id"))
                .and_then(Value::as_str)
                == Some(node_id)
        })
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        return contract(format!(
            "adapter-request node is not uniquely present in plan: {node_id:?}"
        ));
    }
    let node = value_object(matches[0], "workflow-plan.node")?;
    require_fields(
        node,
        &["id", "capability", "provider", "binding"],
        "workflow-plan.node",
    )?;
    let plan_id = required_nonempty(plan, "plan_id", "adapter-request plan")?;
    let fingerprint = required_nonempty(plan, "fingerprint", "adapter-request plan")?;
    let capability = required_nonempty(node, "capability", "adapter-request node")?;
    let provider = required_nonempty(node, "provider", "adapter-request node")?;
    let binding = node.get("binding").ok_or_else(|| {
        RuntimeError::Contract("workflow-plan.node missing required fields: binding".to_owned())
    })?;
    validate_binding(binding, "workflow-plan.node.binding")?;

    let mut identity = Map::new();
    identity.insert("binding".to_owned(), binding.clone());
    identity.insert(
        "capability".to_owned(),
        Value::String(capability.to_owned()),
    );
    identity.insert("checkpoints".to_owned(), Value::Object(checkpoints.clone()));
    identity.insert(
        "invocation_id".to_owned(),
        Value::String(invocation_id.to_owned()),
    );
    identity.insert("node_id".to_owned(), Value::String(node_id.to_owned()));
    identity.insert(
        "plan_fingerprint".to_owned(),
        Value::String(fingerprint.to_owned()),
    );
    identity.insert("plan_id".to_owned(), Value::String(plan_id.to_owned()));
    identity.insert("provider".to_owned(), Value::String(provider.to_owned()));
    identity.insert("schema_version".to_owned(), Value::String("1.0".to_owned()));
    identity.insert("task_context".to_owned(), Value::Object(context.clone()));
    let digest = canonical_sha256(&Value::Object(identity.clone()))?;
    identity.insert(
        "request_id".to_owned(),
        Value::String(format!("adapter-request-{}", &digest[..16])),
    );
    let request = Value::Object(identity);
    validate_adapter_request(&request)?;
    Ok(request)
}

/// Validate an Adapter Request v1 without resolving or invoking a Provider.
///
/// # Errors
/// Returns an error when its shape or frozen identity is invalid.
pub fn validate_adapter_request(value: &Value) -> Result<(), RuntimeError> {
    let request = exact_object(value, REQUEST_FIELDS, "adapter-request")?;
    require_version(request)?;
    require_nonempty_strings(
        request,
        &[
            "request_id",
            "invocation_id",
            "plan_id",
            "plan_fingerprint",
            "node_id",
            "capability",
            "provider",
        ],
        "adapter-request",
    )?;
    validate_binding(
        request.get("binding").ok_or_else(|| {
            RuntimeError::Contract("adapter-request binding is missing".to_owned())
        })?,
        "adapter-request binding",
    )?;
    let task_context = request
        .get("task_context")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract("adapter-request task_context must be an object".to_owned())
        })?;
    let checkpoints = request
        .get("checkpoints")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract("adapter-request checkpoints must be an object".to_owned())
        })?;
    if task_context.get("checkpoints") != Some(&Value::Object(checkpoints.clone())) {
        return contract("adapter-request checkpoints do not match task_context");
    }
    let identity = request
        .iter()
        .filter(|(key, _)| key.as_str() != "request_id")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<Map<_, _>>();
    let digest = canonical_sha256(&Value::Object(identity))?;
    let expected = format!("adapter-request-{}", &digest[..16]);
    if request.get("request_id").and_then(Value::as_str) != Some(expected.as_str()) {
        return contract("adapter-request request_id does not match frozen identity");
    }
    Ok(())
}

/// Validate a structured Adapter Result v1 against a frozen request.
///
/// # Errors
/// Returns an error for identity drift, malformed evidence, invalid artifact
/// references, missing verification evidence, or non-independent review.
#[allow(clippy::too_many_lines)]
pub fn validate_adapter_result(request: &Value, result: &Value) -> Result<(), RuntimeError> {
    validate_adapter_request(request)?;
    let request = value_object(request, "adapter-request")?;
    let result = value_object(result, "adapter-result")?;
    let mut allowed = RESULT_FIELDS.to_vec();
    allowed.extend(["no_test_reason", "suggested_validation"]);
    reject_unknown(result, &allowed, "adapter-result")?;
    require_version(result)?;
    require_fields(result, RESULT_FIELDS, "adapter-result")?;
    require_nonempty_strings(
        result,
        &[
            "request_id",
            "invocation_id",
            "plan_fingerprint",
            "node_id",
            "capability",
            "provider",
        ],
        "adapter-result",
    )?;
    validate_binding(
        result.get("binding").ok_or_else(|| {
            RuntimeError::Contract("adapter-result binding is missing".to_owned())
        })?,
        "adapter-result binding",
    )?;
    for field in [
        "request_id",
        "invocation_id",
        "plan_fingerprint",
        "node_id",
        "capability",
        "provider",
        "binding",
    ] {
        if result.get(field) != request.get(field) {
            return contract(format!("adapter-result {field} does not match request"));
        }
    }
    let status = result
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| RuntimeError::Contract("adapter-result status is invalid".to_owned()))?;
    if !["completed", "partial", "blocked", "failed"].contains(&status) {
        return contract("adapter-result status is invalid");
    }

    let attribution = exact_object(
        result.get("failure_attribution").ok_or_else(|| {
            RuntimeError::Contract("adapter-result attribution missing".to_owned())
        })?,
        &["category", "summary"],
        "adapter-result.failure_attribution",
    )?;
    require_nonempty_strings(
        attribution,
        &["category", "summary"],
        "adapter-result.failure_attribution",
    )?;
    let category = attribution
        .get("category")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !["none", "code", "environment", "provider", "contract"].contains(&category) {
        return contract("adapter-result failure attribution category is invalid");
    }
    if matches!(status, "blocked" | "failed") && category == "none" {
        return contract("adapter-result blocked or failed status requires failure attribution");
    }
    validate_cleanup(result.get("cleanup").unwrap_or(&Value::Null))?;
    let artifact_ids = validate_artifacts(result.get("artifacts").unwrap_or(&Value::Null))?;
    let evidence = result
        .get("evidence")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            RuntimeError::Contract("adapter-result evidence must be an array".to_owned())
        })?;
    let evidence_kinds = validate_evidence(evidence, &artifact_ids)?;
    let evidence_statuses = evidence
        .iter()
        .filter_map(|item| item.get("status").and_then(Value::as_str))
        .collect::<BTreeSet<_>>();
    validate_status_consistency(status, &evidence_statuses)?;
    if result
        .get("cleanup")
        .and_then(Value::as_array)
        .is_some_and(|items| {
            items
                .iter()
                .any(|item| item.get("status").and_then(Value::as_str) == Some("failed"))
        })
        && !matches!(status, "blocked" | "failed")
    {
        return contract("adapter-result failed cleanup must block or fail the result");
    }
    let no_test_reason = result
        .get("no_test_reason")
        .filter(|value| !value.is_null());
    let suggested_validation = result
        .get("suggested_validation")
        .filter(|value| !value.is_null());
    if no_test_reason.is_some() != suggested_validation.is_some() {
        return contract(
            "adapter-result no_test_reason and suggested_validation must be provided together",
        );
    }
    let capability = request
        .get("capability")
        .and_then(Value::as_str)
        .unwrap_or("");
    if let (Some(reason), Some(suggestion)) = (no_test_reason, suggested_validation) {
        if !reason.as_str().is_some_and(nonempty) || !suggestion.as_str().is_some_and(nonempty) {
            return contract("adapter-result validation gap fields must be non-empty strings");
        }
        if !capability.starts_with("verification.") {
            return contract(
                "adapter-result validation gap is only valid for verification capabilities",
            );
        }
        if !matches!(status, "partial" | "blocked") {
            return contract("adapter-result validation gap requires partial or blocked status");
        }
    }
    if status == "blocked"
        && no_test_reason.is_none()
        && evidence_statuses.is_disjoint(&BTreeSet::from(["blocked", "failed"]))
    {
        return contract("adapter-result blocked status requires blocked evidence");
    }
    if capability.starts_with("verification.")
        && !evidence_kinds.contains("validation")
        && no_test_reason.is_none()
    {
        return contract("adapter-result verification requires structured validation evidence");
    }
    if capability.starts_with("verification.")
        && capability
            .rsplit_once('.')
            .is_some_and(|(_, suffix)| suffix == "auto")
        && no_test_reason.is_none()
    {
        validate_automatic_verification(evidence)?;
    }
    if capability == "review.independent" || capability.starts_with("review.") {
        validate_review(request, status, evidence, &evidence_kinds)?;
    }
    if evidence.is_empty() && no_test_reason.is_none() {
        return contract("adapter-result requires structured evidence");
    }
    Ok(())
}

fn validate_status_consistency(
    status: &str,
    evidence_statuses: &BTreeSet<&str>,
) -> Result<(), RuntimeError> {
    if status == "completed"
        && evidence_statuses
            .iter()
            .any(|item| !matches!(*item, "passed" | "completed"))
    {
        return contract("adapter-result completed status conflicts with evidence status");
    }
    if status == "partial"
        && evidence_statuses
            .iter()
            .any(|item| !matches!(*item, "passed" | "completed" | "partial"))
    {
        return contract("adapter-result partial status conflicts with evidence status");
    }
    if status == "blocked" && evidence_statuses.contains("failed") {
        return contract("adapter-result blocked status conflicts with failed evidence");
    }
    if status == "failed" && !evidence_statuses.contains("failed") {
        return contract("adapter-result failed status requires failed evidence");
    }
    Ok(())
}

fn validate_automatic_verification(evidence: &[Value]) -> Result<(), RuntimeError> {
    let validation = evidence
        .iter()
        .find(|item| item.get("kind").and_then(Value::as_str) == Some("validation"))
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-result verification requires structured validation evidence".to_owned(),
            )
        })?;
    let data = validation
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-result evidence data must be a non-empty object".to_owned(),
            )
        })?;
    let executed = data
        .get("executed_validation")
        .map(|value| validate_successful_entries(value, "executed_validation"))
        .transpose()?
        .unwrap_or(false);
    let accepted = data
        .get("accepted_evidence")
        .map(|value| validate_successful_entries(value, "accepted_evidence"))
        .transpose()?
        .unwrap_or(false);
    if !executed && !accepted {
        return contract(
            "adapter-result automatic verification requires executed_validation or accepted_evidence",
        );
    }
    Ok(())
}

fn validate_successful_entries(value: &Value, field: &str) -> Result<bool, RuntimeError> {
    let entries = value.as_array().ok_or_else(|| {
        RuntimeError::Contract(format!(
            "adapter-result automatic verification {field} must be an array"
        ))
    })?;
    if entries.is_empty() {
        return Ok(false);
    }
    for entry in entries {
        let entry = entry.as_object().ok_or_else(|| {
            RuntimeError::Contract(format!(
                "adapter-result automatic verification {field} entries must be objects"
            ))
        })?;
        let kind = entry.get("kind").and_then(Value::as_str);
        if kind.is_none_or(|kind| {
            !nonempty(kind) || ["affected-tests", "route", "test-selection"].contains(&kind)
        }) {
            return contract(format!(
                "adapter-result automatic verification {field} contains selection-only or invalid evidence"
            ));
        }
        if !matches!(
            entry.get("status").and_then(Value::as_str),
            Some("passed" | "completed")
        ) {
            return contract(format!(
                "adapter-result automatic verification {field} requires successful evidence"
            ));
        }
    }
    Ok(true)
}

fn validate_review(
    request: &Map<String, Value>,
    result_status: &str,
    evidence: &[Value],
    evidence_kinds: &BTreeSet<String>,
) -> Result<(), RuntimeError> {
    if !evidence_kinds.contains("review") {
        return contract("adapter-result review requires structured review evidence");
    }
    let review = evidence
        .iter()
        .find(|item| item.get("kind").and_then(Value::as_str) == Some("review"))
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-result review requires structured review evidence".to_owned(),
            )
        })?;
    let data = review
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-result evidence data must be a non-empty object".to_owned(),
            )
        })?;
    let reviewer = data.get("reviewer_actor").and_then(Value::as_str);
    let implementer = data.get("implementation_actor").and_then(Value::as_str);
    if reviewer.is_none_or(|value| !nonempty(value))
        || implementer.is_none_or(|value| !nonempty(value))
    {
        return contract("adapter-result review actor identities are required");
    }
    if reviewer == implementer {
        return contract("adapter-result reviewer must be independent from implementation actor");
    }
    let actors = request
        .get("task_context")
        .and_then(Value::as_object)
        .and_then(|context| context.get("actors"))
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-request review requires orchestrator-frozen actors".to_owned(),
            )
        })?;
    if actors.get("implementation_actor").and_then(Value::as_str) != implementer
        || actors.get("reviewer_actor").and_then(Value::as_str) != reviewer
    {
        return contract("adapter-result review actors do not match orchestrator-frozen actors");
    }
    if actors.get("implementation_actor") == actors.get("reviewer_actor") {
        return contract("adapter-request reviewer must be independent from implementation actor");
    }
    let blocking = data
        .get("blocking_issues")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "adapter-result review blocking_issues must be an array".to_owned(),
            )
        })?;
    let review_status = review.get("status").and_then(Value::as_str).unwrap_or("");
    if !blocking.is_empty()
        && (!matches!(result_status, "blocked" | "failed")
            || !matches!(review_status, "blocked" | "failed"))
    {
        return contract("adapter-result review blocking issues must block the result");
    }
    if blocking.is_empty() && result_status == "completed" && review_status != "passed" {
        return contract("adapter-result successful review evidence must be passed");
    }
    Ok(())
}

fn validate_artifacts(value: &Value) -> Result<BTreeSet<String>, RuntimeError> {
    let artifacts = value.as_array().ok_or_else(|| {
        RuntimeError::Contract("adapter-result artifacts must be an array".to_owned())
    })?;
    let mut identifiers = BTreeSet::new();
    for artifact in artifacts {
        let artifact = exact_object(
            artifact,
            &["artifact_id", "kind", "sha256", "uri"],
            "adapter-result.artifact",
        )?;
        require_nonempty_strings(
            artifact,
            &["artifact_id", "kind", "sha256", "uri"],
            "adapter-result.artifact",
        )?;
        let identifier = artifact
            .get("artifact_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !identifiers.insert(identifier.to_owned()) {
            return contract("adapter-result artifact ids must be unique");
        }
        let kind = artifact.get("kind").and_then(Value::as_str).unwrap_or("");
        if ![
            "structured-report",
            "test-report",
            "review-report",
            "delivery-report",
            "diagnostics",
            "raw-log",
            "other",
        ]
        .contains(&kind)
        {
            return contract("adapter-result artifact kind is invalid");
        }
        let digest = artifact.get("sha256").and_then(Value::as_str).unwrap_or("");
        if digest.len() != 64
            || !digest
                .as_bytes()
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        {
            return contract("adapter-result artifact sha256 is invalid");
        }
    }
    Ok(identifiers)
}

fn validate_evidence(
    evidence: &[Value],
    artifact_ids: &BTreeSet<String>,
) -> Result<BTreeSet<String>, RuntimeError> {
    let mut kinds = BTreeSet::new();
    for item in evidence {
        let item = exact_object(
            item,
            &["kind", "status", "summary", "data", "artifact_ids"],
            "adapter-result.evidence",
        )?;
        require_nonempty_strings(
            item,
            &["kind", "status", "summary"],
            "adapter-result.evidence",
        )?;
        let kind = item.get("kind").and_then(Value::as_str).unwrap_or("");
        if !["validation", "review", "delivery", "diagnostic"].contains(&kind) {
            return contract("adapter-result evidence kind is invalid");
        }
        let status = item.get("status").and_then(Value::as_str).unwrap_or("");
        if !["passed", "completed", "partial", "blocked", "failed"].contains(&status) {
            return contract("adapter-result evidence status is invalid");
        }
        if item
            .get("data")
            .and_then(Value::as_object)
            .is_none_or(Map::is_empty)
        {
            return contract("adapter-result evidence data must be a non-empty object");
        }
        let references = item
            .get("artifact_ids")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                RuntimeError::Contract(
                    "adapter-result evidence artifact_ids must be strings".to_owned(),
                )
            })?;
        let mut unique = BTreeSet::new();
        for reference in references {
            let reference = reference
                .as_str()
                .filter(|item| nonempty(item))
                .ok_or_else(|| {
                    RuntimeError::Contract(
                        "adapter-result evidence artifact_ids must be strings".to_owned(),
                    )
                })?;
            if !unique.insert(reference.to_owned()) {
                return contract("adapter-result evidence artifact_ids must be unique");
            }
        }
        let unknown = unique.difference(artifact_ids).cloned().collect::<Vec<_>>();
        if !unknown.is_empty() {
            return contract(format!(
                "adapter-result evidence references unknown artifacts: {}",
                unknown.join(", ")
            ));
        }
        kinds.insert(kind.to_owned());
    }
    Ok(kinds)
}

fn validate_cleanup(value: &Value) -> Result<(), RuntimeError> {
    let cleanup = value.as_array().ok_or_else(|| {
        RuntimeError::Contract("adapter-result cleanup must be an array".to_owned())
    })?;
    for item in cleanup {
        let item = exact_object(
            item,
            &["resource", "status", "detail"],
            "adapter-result.cleanup",
        )?;
        require_nonempty_strings(
            item,
            &["resource", "status", "detail"],
            "adapter-result.cleanup",
        )?;
        if !matches!(
            item.get("status").and_then(Value::as_str),
            Some("not-required" | "completed" | "failed")
        ) {
            return contract("adapter-result cleanup status is invalid");
        }
    }
    Ok(())
}

fn validate_binding(value: &Value, kind: &str) -> Result<(), RuntimeError> {
    let binding = value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{kind} must be an object")))?;
    require_fields(binding, &["kind", "name"], kind)?;
    reject_unknown(binding, &["kind", "name", "mode"], kind)?;
    if !matches!(
        binding.get("kind").and_then(Value::as_str),
        Some("skill" | "agent" | "script" | "tool")
    ) {
        return contract(format!("{kind} kind is invalid"));
    }
    if binding
        .get("name")
        .and_then(Value::as_str)
        .is_none_or(|value| !nonempty(value))
    {
        return contract(format!("{kind} name is invalid"));
    }
    if binding
        .get("mode")
        .is_some_and(|value| value.as_str().is_none_or(|value| !nonempty(value)))
    {
        return contract(format!("{kind} mode is invalid"));
    }
    Ok(())
}

fn require_version(value: &Map<String, Value>) -> Result<(), RuntimeError> {
    if value.get("schema_version").and_then(Value::as_str) != Some("1.0") {
        return contract("unsupported schema_version");
    }
    Ok(())
}

fn required_nonempty<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    kind: &str,
) -> Result<&'a str, RuntimeError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| RuntimeError::Contract(format!("{kind} {field} is invalid")))
}

fn require_nonempty_strings(
    value: &Map<String, Value>,
    fields: &[&str],
    kind: &str,
) -> Result<(), RuntimeError> {
    for field in fields {
        if value
            .get(*field)
            .and_then(Value::as_str)
            .is_none_or(|value| !nonempty(value))
        {
            return contract(format!("{kind} {field} must be a non-empty string"));
        }
    }
    Ok(())
}

fn exact_object<'a>(
    value: &'a Value,
    fields: &[&str],
    kind: &str,
) -> Result<&'a Map<String, Value>, RuntimeError> {
    let object = value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{kind} must be an object")))?;
    require_fields(object, fields, kind)?;
    reject_unknown(object, fields, kind)?;
    Ok(object)
}

fn value_object<'a>(value: &'a Value, kind: &str) -> Result<&'a Map<String, Value>, RuntimeError> {
    value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{kind} must be an object")))
}

fn require_fields(
    value: &Map<String, Value>,
    fields: &[&str],
    kind: &str,
) -> Result<(), RuntimeError> {
    let missing = fields
        .iter()
        .filter(|field| !value.contains_key(**field))
        .copied()
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return contract(format!(
            "{kind} missing required fields: {}",
            missing.join(", ")
        ));
    }
    Ok(())
}

fn reject_unknown(
    value: &Map<String, Value>,
    fields: &[&str],
    kind: &str,
) -> Result<(), RuntimeError> {
    let allowed = fields.iter().copied().collect::<BTreeSet<_>>();
    let unknown = value
        .keys()
        .filter(|field| !allowed.contains(field.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return contract(format!("{kind} has unknown fields: {}", unknown.join(", ")));
    }
    Ok(())
}

fn nonempty(value: &str) -> bool {
    !value.trim().is_empty()
}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture() -> (Value, Value, Value) {
        let plan = json!({
            "schema_version": "1.0",
            "plan_id": "plan",
            "fingerprint": "fingerprint",
            "nodes": [{
                "id": "verify",
                "capability": "verification.fixture",
                "provider": "fixture",
                "binding": {"kind": "skill", "name": "verify"}
            }]
        });
        let context = json!({
            "checkpoints": {"CP0": "completed"},
            "actors": {
                "implementation_actor": "builder",
                "reviewer_actor": "reviewer"
            }
        });
        let request = build_adapter_request(&plan, "verify", &context, "invocation")
            .expect("request should build");
        (plan, context, request)
    }

    #[test]
    fn request_identity_is_frozen_and_revalidated() {
        let (_, _, mut request) = fixture();
        validate_adapter_request(&request).expect("request should validate");
        request["request_id"] = json!("tampered");
        assert!(validate_adapter_request(&request).is_err());
    }

    #[test]
    fn result_requires_structured_artifact_references() {
        let (_, _, request) = fixture();
        let mut result = json!({
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "invocation_id": request["invocation_id"],
            "plan_fingerprint": request["plan_fingerprint"],
            "node_id": request["node_id"],
            "capability": request["capability"],
            "provider": request["provider"],
            "binding": request["binding"],
            "status": "completed",
            "failure_attribution": {"category": "none", "summary": "ok"},
            "cleanup": [],
            "evidence": [{
                "kind": "validation",
                "status": "passed",
                "summary": "passed",
                "data": {"tests": 1},
                "artifact_ids": ["report"]
            }],
            "artifacts": [{
                "artifact_id": "report",
                "kind": "test-report",
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "uri": "artifacts/report.json"
            }]
        });
        validate_adapter_result(&request, &result).expect("result should validate");
        result["evidence"][0]["artifact_ids"] = json!(["missing"]);
        assert!(validate_adapter_result(&request, &result).is_err());
    }

    #[test]
    fn explicit_null_validation_gap_matches_absent_fields() {
        let (_, _, request) = fixture();
        let result = json!({
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "invocation_id": request["invocation_id"],
            "plan_fingerprint": request["plan_fingerprint"],
            "node_id": request["node_id"],
            "capability": request["capability"],
            "provider": request["provider"],
            "binding": request["binding"],
            "status": "completed",
            "failure_attribution": {"category": "none", "summary": "ok"},
            "cleanup": [],
            "evidence": [{
                "kind": "validation",
                "status": "passed",
                "summary": "passed",
                "data": {"tests": 1},
                "artifact_ids": []
            }],
            "artifacts": [],
            "no_test_reason": null,
            "suggested_validation": null
        });
        validate_adapter_result(&request, &result).expect("null gap should be absent");
    }
}
