//! Durable host-owned handoff transport for external Provider invocations.
//!
//! This module never executes a binding. It atomically publishes one frozen
//! request, grants one time-bounded claim, and accepts one validated result.

use crate::RuntimeError;
use crate::adapters::{build_adapter_request, validate_adapter_request, validate_adapter_result};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256, parse_json};
use agent_engine::{validate_compiled_plan, validate_package_lock, validate_plan_package_lock};
use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt as _};
use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{Read as _, Write as _};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

const RECORD_FIELDS: &[&str] = &[
    "schema_version",
    "transport_id",
    "prepared_at",
    "request",
    "execution_contract",
    "claim",
    "result",
    "submitted_at",
];
const EXECUTION_FIELDS: &[&str] = &[
    "approval",
    "idempotent",
    "max_retries",
    "permission_profile",
    "provider_manifest_digest",
    "resource_keys",
    "side_effects",
    "timeout_seconds",
];
const CLAIM_FIELDS: &[&str] = &["actor_id", "claim_token_sha256", "claimed_at", "deadline"];
const SELECTION_FIELDS: &[&str] = &["schema_version", "plan_fingerprint", "requests"];
const MAX_SELECTION_REQUESTS: usize = 16_384;

/// Atomically publish one Provider Invocation v1 handoff record.
///
/// # Errors
/// Returns an error for plan/request drift, incomplete execution metadata,
/// unsafe persistence, or duplicate request identity.
pub fn prepare_provider_invocation(
    root: &Path,
    plan: &Value,
    node_id: &str,
    context: &Value,
    invocation_id: &str,
    package_lock: Option<&Value>,
) -> Result<Value, RuntimeError> {
    validate_invocation_plan_lock(plan, package_lock)?;
    prepare_provider_invocation_at(
        root,
        plan,
        node_id,
        context,
        invocation_id,
        current_epoch_seconds()?,
    )
}

fn validate_invocation_plan_lock(
    plan: &Value,
    package_lock: Option<&Value>,
) -> Result<(), RuntimeError> {
    validate_compiled_plan(plan)?;
    let frozen = plan
        .get("package_lock_hash")
        .is_some_and(|value| !value.is_null());
    match (frozen, package_lock) {
        (false, None) => Ok(()),
        (false, Some(_)) => {
            contract("workflow plan is not frozen to the supplied package Lockfile")
        }
        (true, None) => contract("locked workflow operation requires the current package Lockfile"),
        (true, Some(package_lock)) => {
            validate_package_lock(package_lock)?;
            validate_plan_package_lock(plan, package_lock)?;
            Ok(())
        }
    }
}

fn prepare_provider_invocation_at(
    root: &Path,
    plan: &Value,
    node_id: &str,
    context: &Value,
    invocation_id: &str,
    prepared_at: u64,
) -> Result<Value, RuntimeError> {
    let request = build_adapter_request(plan, node_id, context, invocation_id)?;
    let nodes = plan
        .get("nodes")
        .and_then(Value::as_array)
        .ok_or_else(|| RuntimeError::Contract("workflow-plan nodes must be an array".to_owned()))?;
    let matches = nodes
        .iter()
        .filter(|node| node.get("id").and_then(Value::as_str) == Some(node_id))
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        return contract(format!(
            "adapter-request node is not uniquely present in plan: {node_id:?}"
        ));
    }
    let execution_contract = freeze_execution_contract(matches[0])?;
    let identity = json!({
        "execution_contract": execution_contract,
        "prepared_at": prepared_at,
        "request": request,
        "schema_version": "1.0",
    });
    let digest = canonical_sha256(&identity)?;
    let record = json!({
        "claim": null,
        "execution_contract": identity["execution_contract"],
        "prepared_at": prepared_at,
        "request": identity["request"],
        "result": null,
        "schema_version": "1.0",
        "submitted_at": null,
        "transport_id": format!("provider-invocation-{}", &digest[..16]),
    });
    validate_provider_invocation(&record)?;
    with_store(root, true, true, |store| {
        store.create(&record)?;
        Ok(record)
    })
}

/// Grant the only claim for one request until its frozen node timeout.
///
/// # Errors
/// Returns an error for invalid actor/token, duplicate/terminal claim, overflow,
/// or unsafe persistence.
pub fn claim_provider_invocation(
    root: &Path,
    request_id: &str,
    actor_id: &str,
    claim_token: &str,
) -> Result<Value, RuntimeError> {
    claim_provider_invocation_at(
        root,
        request_id,
        actor_id,
        claim_token,
        current_epoch_seconds()?,
    )
}

fn claim_provider_invocation_at(
    root: &Path,
    request_id: &str,
    actor_id: &str,
    claim_token: &str,
    claimed_at: u64,
) -> Result<Value, RuntimeError> {
    if !nonempty(actor_id) || actor_id.chars().count() > 128 {
        return contract("provider invocation actor_id must be a non-empty string");
    }
    validate_claim_token(claim_token)?;
    with_store(root, false, true, |store| {
        let mut record = store.load(request_id)?;
        if !record.get("result").is_none_or(Value::is_null) {
            return contract("provider invocation is already submitted");
        }
        if !record.get("claim").is_none_or(Value::is_null) {
            let state = provider_invocation_state(&record, claimed_at)?;
            return contract(format!(
                "provider invocation cannot be claimed from {state} state"
            ));
        }
        let requested_resources = record
            .pointer("/execution_contract/resource_keys")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                RuntimeError::Contract("provider-invocation resource_keys is invalid".to_owned())
            })?;
        for existing in store.list()? {
            if existing
                .pointer("/request/request_id")
                .and_then(Value::as_str)
                == Some(request_id)
                || provider_invocation_state(&existing, claimed_at)? != "claimed"
            {
                continue;
            }
            let active = existing
                .pointer("/execution_contract/resource_keys")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    RuntimeError::Contract(
                        "provider-invocation resource_keys is invalid".to_owned(),
                    )
                })?;
            if let Some(conflict) = requested_resources
                .iter()
                .find(|resource| active.contains(resource))
                .and_then(Value::as_str)
            {
                return contract(format!(
                    "provider invocation resource is already claimed: {conflict}"
                ));
            }
        }
        let timeout = required_u64(
            record.pointer("/execution_contract/timeout_seconds"),
            "provider-invocation timeout_seconds is invalid",
        )?;
        let deadline = claimed_at.checked_add(timeout).ok_or_else(|| {
            RuntimeError::Contract("provider invocation claim deadline overflows".to_owned())
        })?;
        let token_digest = canonical_sha256(&Value::String(claim_token.to_owned()))?;
        record
            .as_object_mut()
            .ok_or_else(|| {
                RuntimeError::Contract("provider-invocation must be an object".to_owned())
            })?
            .insert(
                "claim".to_owned(),
                json!({
                    "actor_id": actor_id,
                    "claim_token_sha256": token_digest,
                    "claimed_at": claimed_at,
                    "deadline": deadline,
                }),
            );
        validate_provider_invocation(&record)?;
        store.replace(&record)?;
        Ok(record)
    })
}

/// Validate and atomically publish the terminal result for one live claim.
///
/// # Errors
/// Returns an error for missing/expired ownership, token mismatch, result
/// contract drift, duplicate submission, or unsafe persistence.
pub fn submit_provider_invocation(
    root: &Path,
    request_id: &str,
    result: &Value,
    claim_token: &str,
) -> Result<Value, RuntimeError> {
    submit_provider_invocation_at(
        root,
        request_id,
        result,
        claim_token,
        current_epoch_seconds()?,
    )
}

fn submit_provider_invocation_at(
    root: &Path,
    request_id: &str,
    result: &Value,
    claim_token: &str,
    submitted_at: u64,
) -> Result<Value, RuntimeError> {
    validate_claim_token(claim_token)?;
    with_store(root, false, true, |store| {
        let mut record = store.load(request_id)?;
        if !record.get("result").is_none_or(Value::is_null) {
            return contract("provider invocation is already submitted");
        }
        let claim = record
            .get("claim")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                RuntimeError::Contract(
                    "provider invocation must be claimed before submission".to_owned(),
                )
            })?;
        let expected = canonical_sha256(&Value::String(claim_token.to_owned()))?;
        if claim.get("claim_token_sha256").and_then(Value::as_str) != Some(expected.as_str()) {
            return contract("provider invocation claim token does not match");
        }
        let claimed_at = required_u64(
            claim.get("claimed_at"),
            "provider-invocation claimed_at is invalid",
        )?;
        let deadline = required_u64(
            claim.get("deadline"),
            "provider-invocation deadline is invalid",
        )?;
        if submitted_at < claimed_at {
            return contract("provider invocation submitted_at precedes claim");
        }
        if submitted_at >= deadline {
            return contract("provider invocation claim has expired");
        }
        validate_adapter_result(
            record.get("request").ok_or_else(|| {
                RuntimeError::Contract("provider invocation request is missing".to_owned())
            })?,
            result,
        )?;
        let record_object = record.as_object_mut().ok_or_else(|| {
            RuntimeError::Contract("provider-invocation must be an object".to_owned())
        })?;
        record_object.insert("result".to_owned(), result.clone());
        record_object.insert("submitted_at".to_owned(), Value::from(submitted_at));
        validate_provider_invocation(&record)?;
        store.replace(&record)?;
        Ok(record)
    })
}

/// Inspect one record and derive its current state without mutating it.
///
/// # Errors
/// Returns an error for missing/unsafe persistence or an invalid record.
pub fn inspect_provider_invocation(root: &Path, request_id: &str) -> Result<Value, RuntimeError> {
    inspect_provider_invocation_at(root, request_id, current_epoch_seconds()?)
}

fn inspect_provider_invocation_at(
    root: &Path,
    request_id: &str,
    at: u64,
) -> Result<Value, RuntimeError> {
    let record = with_store(root, false, false, |store| store.load(request_id))?;
    Ok(json!({
        "invocation": record,
        "schema_version": "1.0",
        "state": provider_invocation_state(&record, at)?,
    }))
}

/// Collect submitted results for one Plan for the Recorded Adapter runtime.
///
/// # Errors
/// Returns an error for malformed entries, invalid selection identity, an
/// unsubmitted selected request, or an invalid Plan fingerprint.
pub fn collect_submitted_results(
    root: &Path,
    plan_fingerprint: &str,
    selection: &Value,
) -> Result<Value, RuntimeError> {
    collect_submitted_results_at(root, plan_fingerprint, selection, current_epoch_seconds()?)
}

fn collect_submitted_results_at(
    root: &Path,
    plan_fingerprint: &str,
    selection: &Value,
    at: u64,
) -> Result<Value, RuntimeError> {
    if !nonempty(plan_fingerprint) {
        return contract("provider invocation plan_fingerprint is invalid");
    }
    validate_provider_invocation_selection(selection)?;
    if selection.get("plan_fingerprint").and_then(Value::as_str) != Some(plan_fingerprint) {
        return contract("provider invocation selection plan_fingerprint does not match Plan");
    }
    let selected = selection
        .get("requests")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract("provider-invocation-selection requests are invalid".to_owned())
        })?;
    let records = match with_store(root, false, true, Store::list) {
        Ok(records) => records,
        Err(RuntimeError::Io(error))
            if error.kind() == std::io::ErrorKind::NotFound && selected.is_empty() =>
        {
            Vec::new()
        }
        Err(RuntimeError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
            return contract("provider invocation selection request is not submitted");
        }
        Err(error) => return Err(error),
    };
    let mut records_by_id = BTreeMap::new();
    for record in records {
        if record
            .pointer("/request/plan_fingerprint")
            .and_then(Value::as_str)
            != Some(plan_fingerprint)
        {
            continue;
        }
        let request_id = record
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RuntimeError::Contract("provider invocation request_id is invalid".to_owned())
            })?
            .to_owned();
        if records_by_id.insert(request_id, record).is_some() {
            return contract("provider invocation request identity is duplicated");
        }
    }
    let mut results = Map::new();
    for (node_id, request_id) in selected {
        let Some(record) = request_id
            .as_str()
            .and_then(|request_id| records_by_id.get(request_id))
        else {
            return contract(format!(
                "provider invocation selection request is not submitted for node: {node_id}"
            ));
        };
        if record.pointer("/request/node_id").and_then(Value::as_str) != Some(node_id.as_str())
            || provider_invocation_state(record, at)? != "submitted"
        {
            return contract(format!(
                "provider invocation selection request is not submitted for node: {node_id}"
            ));
        }
        results.insert(
            node_id.to_owned(),
            record.get("result").cloned().ok_or_else(|| {
                RuntimeError::Contract("provider invocation result is missing".to_owned())
            })?,
        );
    }
    Ok(Value::Object(results))
}

/// Read one small no-follow bearer-token file without logging its contents.
///
/// # Errors
/// Returns an error for symlinks/reparse points, replacement races, oversized
/// input, invalid UTF-8, multiline input, or a short token.
pub fn load_claim_token_file(path: &Path) -> Result<String, RuntimeError> {
    let before = std::fs::symlink_metadata(path)?;
    if before.file_type().is_symlink() || !before.is_file() {
        return contract("provider invocation claim token file is unsafe");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt as _;
        if before.mode() & 0o077 != 0 {
            return contract("provider invocation claim token file permissions are too broad");
        }
    }
    if before.len() > 4096 {
        return contract("provider invocation claim token file is too large");
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    configure_std_nofollow(&mut options);
    let mut file = options.open(path)?;
    let opened = file.metadata()?;
    if metadata_identity_std(&before) != metadata_identity_std(&opened) {
        return contract("provider invocation claim token file changed while opening");
    }
    let mut bytes = Vec::with_capacity(usize::try_from(opened.len()).unwrap_or(4096));
    std::io::Read::by_ref(&mut file)
        .take(4097)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 4096 {
        return contract("provider invocation claim token file is too large");
    }
    let after = std::fs::symlink_metadata(path)?;
    if after.file_type().is_symlink()
        || !after.is_file()
        || metadata_identity_std(&opened) != metadata_identity_std(&after)
    {
        return contract("provider invocation claim token file changed while reading");
    }
    let token = std::str::from_utf8(&bytes).map_err(|_| {
        RuntimeError::Contract("provider invocation claim token file is not UTF-8".to_owned())
    })?;
    let token = token
        .strip_suffix("\r\n")
        .or_else(|| token.strip_suffix('\n'))
        .unwrap_or(token)
        .to_owned();
    if token.contains(['\r', '\n']) {
        return contract("provider invocation claim token file must contain one line");
    }
    validate_claim_token(&token)?;
    Ok(token)
}

/// Derive prepared/claimed/expired/submitted from persisted data.
///
/// # Errors
/// Returns an error when the record is invalid.
pub fn provider_invocation_state(record: &Value, at: u64) -> Result<&'static str, RuntimeError> {
    validate_provider_invocation(record)?;
    if !record.get("result").is_none_or(Value::is_null) {
        return Ok("submitted");
    }
    let Some(claim) = record.get("claim").and_then(Value::as_object) else {
        return Ok("prepared");
    };
    let deadline = required_u64(
        claim.get("deadline"),
        "provider-invocation deadline is invalid",
    )?;
    Ok(if at >= deadline { "expired" } else { "claimed" })
}

/// Validate one persisted Provider Invocation v1 record.
///
/// # Errors
/// Returns an error for shape, frozen identity, deadline, ownership, request,
/// or result drift.
#[allow(clippy::too_many_lines)]
pub fn validate_provider_invocation(value: &Value) -> Result<(), RuntimeError> {
    let record = exact_object(value, RECORD_FIELDS, "provider-invocation")?;
    if record.get("schema_version").and_then(Value::as_str) != Some("1.0") {
        return contract("unsupported schema_version");
    }
    let transport_id = record
        .get("transport_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !prefixed_hex(transport_id, "provider-invocation-", 16) {
        return contract("provider-invocation transport_id is invalid");
    }
    let prepared_at = required_u64(
        record.get("prepared_at"),
        "provider-invocation prepared_at is invalid",
    )?;
    let request = record.get("request").ok_or_else(|| {
        RuntimeError::Contract("provider invocation request is missing".to_owned())
    })?;
    validate_adapter_request(request)?;
    let execution = exact_object(
        record.get("execution_contract").ok_or_else(|| {
            RuntimeError::Contract("provider invocation execution_contract is missing".to_owned())
        })?,
        EXECUTION_FIELDS,
        "provider-invocation execution_contract",
    )?;
    if !execution.get("idempotent").is_some_and(Value::is_boolean) {
        return contract("provider-invocation idempotent is invalid");
    }
    let _ = required_u64(
        execution.get("max_retries"),
        "provider-invocation max_retries is invalid",
    )?;
    let timeout = required_u64(
        execution.get("timeout_seconds"),
        "provider-invocation timeout_seconds is invalid",
    )?;
    if timeout == 0 {
        return contract("provider-invocation timeout_seconds is invalid");
    }
    for field in ["permission_profile", "provider_manifest_digest"] {
        if execution
            .get(field)
            .and_then(Value::as_str)
            .is_none_or(|item| !nonempty(item))
        {
            return contract(format!("provider-invocation {field} is invalid"));
        }
    }
    for field in ["resource_keys", "side_effects"] {
        validate_sorted_strings(execution.get(field), field)?;
    }
    if !execution.get("approval").is_none_or(Value::is_null) {
        return contract(
            "approval-bound provider invocation requires a runtime-granted attempt proof",
        );
    }
    let identity = json!({
        "execution_contract": Value::Object(execution.clone()),
        "prepared_at": prepared_at,
        "request": request,
        "schema_version": "1.0",
    });
    let digest = canonical_sha256(&identity)?;
    if transport_id != format!("provider-invocation-{}", &digest[..16]) {
        return contract("provider-invocation transport_id does not match frozen identity");
    }

    let claim = record.get("claim").unwrap_or(&Value::Null);
    let result = record.get("result").unwrap_or(&Value::Null);
    let submitted_at = record.get("submitted_at").unwrap_or(&Value::Null);
    if claim.is_null() {
        if !result.is_null() || !submitted_at.is_null() {
            return contract("provider-invocation result requires a claim");
        }
        return Ok(());
    }
    let claim = exact_object(claim, CLAIM_FIELDS, "provider-invocation claim")?;
    if claim
        .get("actor_id")
        .and_then(Value::as_str)
        .is_none_or(|item| !nonempty(item) || item.chars().count() > 128)
        || !claim
            .get("claim_token_sha256")
            .and_then(Value::as_str)
            .is_some_and(|item| is_lower_hex(item, 64))
    {
        return contract("provider-invocation claim identity is invalid");
    }
    let claimed_at = required_u64(
        claim.get("claimed_at"),
        "provider-invocation claimed_at is invalid",
    )?;
    let deadline = required_u64(
        claim.get("deadline"),
        "provider-invocation deadline is invalid",
    )?;
    if claimed_at < prepared_at || claimed_at.checked_add(timeout) != Some(deadline) {
        return contract("provider-invocation claim deadline is invalid");
    }
    if result.is_null() {
        if !submitted_at.is_null() {
            return contract("provider-invocation submitted_at requires a result");
        }
        return Ok(());
    }
    validate_adapter_result(request, result)?;
    let submitted_at = required_u64(
        Some(submitted_at),
        "provider-invocation submitted_at is invalid",
    )?;
    if submitted_at < claimed_at || submitted_at >= deadline {
        return contract("provider-invocation submission timestamp is outside the claim");
    }
    Ok(())
}

/// Validate the explicit request selection consumed by the recorded runtime.
///
/// # Errors
/// Returns an error for shape, Plan identity, request identity, or duplicate
/// request selections.
pub fn validate_provider_invocation_selection(value: &Value) -> Result<(), RuntimeError> {
    let selection = exact_object(value, SELECTION_FIELDS, "provider-invocation-selection")?;
    if selection.get("schema_version").and_then(Value::as_str) != Some("1.0") {
        return contract("unsupported schema_version");
    }
    if selection
        .get("plan_fingerprint")
        .and_then(Value::as_str)
        .is_none_or(|value| !nonempty(value))
    {
        return contract("provider-invocation-selection plan_fingerprint is invalid");
    }
    let requests = selection
        .get("requests")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract("provider-invocation-selection requests are invalid".to_owned())
        })?;
    if requests.len() > MAX_SELECTION_REQUESTS
        || requests.iter().any(|(node_id, request_id)| {
            !nonempty(node_id)
                || request_id
                    .as_str()
                    .is_none_or(|value| !prefixed_hex(value, "adapter-request-", 16))
        })
    {
        return contract("provider-invocation-selection requests are invalid");
    }
    let unique = requests
        .values()
        .filter_map(Value::as_str)
        .collect::<BTreeSet<_>>();
    if unique.len() != requests.len() {
        return contract("provider-invocation-selection request ids must be unique");
    }
    Ok(())
}

fn freeze_execution_contract(node: &Value) -> Result<Value, RuntimeError> {
    let node = node
        .as_object()
        .ok_or_else(|| RuntimeError::Contract("workflow-plan.node must be an object".to_owned()))?;
    for field in EXECUTION_FIELDS {
        if !node.contains_key(*field) {
            return contract(format!(
                "workflow-plan.node missing required fields: {field}"
            ));
        }
    }
    if !node.get("approval").is_none_or(Value::is_null) {
        return contract(
            "approval-bound provider invocation requires a runtime-granted attempt proof",
        );
    }
    let mut resources = string_array(node.get("resource_keys"), "resource_keys")?;
    resources.sort();
    resources.dedup();
    let original_resources = node
        .get("resource_keys")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    if resources.len() != original_resources {
        return contract("provider-invocation resource_keys must be unique strings");
    }
    let mut effects = string_array(node.get("side_effects"), "side_effects")?;
    effects.sort();
    effects.dedup();
    let original_effects = node
        .get("side_effects")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    if effects.len() != original_effects {
        return contract("provider-invocation side_effects must be unique strings");
    }
    let value = json!({
        "approval": node["approval"],
        "idempotent": node["idempotent"],
        "max_retries": node["max_retries"],
        "permission_profile": node["permission_profile"],
        "provider_manifest_digest": node["provider_manifest_digest"],
        "resource_keys": resources,
        "side_effects": effects,
        "timeout_seconds": node["timeout_seconds"],
    });
    let probe = json!({
        "claim": null,
        "execution_contract": value,
        "prepared_at": 0,
        "request": null,
        "result": null,
        "schema_version": "1.0",
        "submitted_at": null,
        "transport_id": "provider-invocation-0000000000000000",
    });
    let execution = probe
        .get("execution_contract")
        .cloned()
        .ok_or_else(|| RuntimeError::Contract("execution contract is missing".to_owned()))?;
    if !execution["idempotent"].is_boolean()
        || required_u64(
            execution.get("max_retries"),
            "provider-invocation max_retries is invalid",
        )
        .is_err()
        || required_u64(
            execution.get("timeout_seconds"),
            "provider-invocation timeout_seconds is invalid",
        )? == 0
    {
        return contract("provider-invocation execution contract is invalid");
    }
    for field in ["permission_profile", "provider_manifest_digest"] {
        if execution
            .get(field)
            .and_then(Value::as_str)
            .is_none_or(|item| !nonempty(item))
        {
            return contract(format!("provider-invocation {field} is invalid"));
        }
    }
    if !execution["approval"].is_null() {
        return contract(
            "approval-bound provider invocation requires a runtime-granted attempt proof",
        );
    }
    Ok(execution)
}

struct Store {
    directory: Dir,
    _lock: Option<File>,
}

impl Store {
    fn create(&self, record: &Value) -> Result<(), RuntimeError> {
        let request_id = record
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RuntimeError::Contract("provider invocation request_id is invalid".to_owned())
            })?;
        let file_name = record_file_name(request_id)?;
        if self.entry_exists(&file_name)? {
            return contract("provider invocation already exists");
        }
        self.atomic_write(&file_name, record, false)
    }

    fn replace(&self, record: &Value) -> Result<(), RuntimeError> {
        let request_id = record
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RuntimeError::Contract("provider invocation request_id is invalid".to_owned())
            })?;
        self.atomic_write(&record_file_name(request_id)?, record, true)
    }

    fn load(&self, request_id: &str) -> Result<Value, RuntimeError> {
        let file_name = record_file_name(request_id)?;
        let value = self.read_entry(&file_name).map_err(|error| match error {
            RuntimeError::Io(ref source) if source.kind() == std::io::ErrorKind::NotFound => {
                RuntimeError::Contract(format!("provider invocation does not exist: {request_id}"))
            }
            other => other,
        })?;
        validate_provider_invocation(&value)?;
        if value.pointer("/request/request_id").and_then(Value::as_str) != Some(request_id) {
            return contract("provider invocation filename identity mismatch");
        }
        Ok(value)
    }

    fn list(&self) -> Result<Vec<Value>, RuntimeError> {
        let mut names = Vec::new();
        for entry in self.directory.entries()? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_str().ok_or_else(|| {
                RuntimeError::Contract("provider invocation filename is not valid UTF-8".to_owned())
            })?;
            #[allow(
                clippy::case_sensitive_file_extension_comparisons,
                reason = "Invocation store owns the exact lowercase .json contract suffix"
            )]
            if name.starts_with("adapter-request-") && name.ends_with(".json") {
                names.push(name.to_owned());
            }
        }
        names.sort();
        let mut records = Vec::with_capacity(names.len());
        for name in names {
            let metadata = self.directory.symlink_metadata(&name)?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return contract(format!("provider invocation entry is unsafe: {name}"));
            }
            let value = self.read_entry(&name)?;
            validate_provider_invocation(&value)?;
            let expected = name.strip_suffix(".json").unwrap_or_default();
            if value.pointer("/request/request_id").and_then(Value::as_str) != Some(expected) {
                return contract("provider invocation filename identity mismatch");
            }
            records.push(value);
        }
        Ok(records)
    }

    fn entry_exists(&self, file_name: &str) -> Result<bool, RuntimeError> {
        match self.directory.symlink_metadata(file_name) {
            Ok(_) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(RuntimeError::Io(error)),
        }
    }

    fn read_entry(&self, file_name: &str) -> Result<Value, RuntimeError> {
        let metadata = self.directory.symlink_metadata(file_name)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return contract(format!("provider invocation entry is unsafe: {file_name}"));
        }
        #[cfg(unix)]
        if cap_mode(&metadata) & 0o077 != 0 {
            return contract("provider invocation entry permissions are too broad");
        }
        if metadata.len() > MAX_CONTRACT_JSON_BYTES as u64 {
            return contract(format!(
                "contract input has more than {MAX_CONTRACT_JSON_BYTES} bytes"
            ));
        }
        let mut options = OpenOptions::new();
        options.read(true);
        configure_private_nofollow(&mut options);
        let mut file = self.directory.open_with(file_name, &options)?.into_std();
        let opened = file.metadata()?;
        #[cfg(unix)]
        if std_mode(&opened) & 0o077 != 0 {
            return contract("provider invocation entry permissions are too broad");
        }
        if metadata_identity_cap(&metadata) != metadata_identity_std(&opened) {
            return contract("provider invocation entry changed while opening");
        }
        let mut bytes =
            Vec::with_capacity(usize::try_from(metadata.len()).unwrap_or(MAX_CONTRACT_JSON_BYTES));
        std::io::Read::by_ref(&mut file)
            .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_CONTRACT_JSON_BYTES {
            return contract(format!(
                "contract input has more than {MAX_CONTRACT_JSON_BYTES} bytes"
            ));
        }
        let current = self.directory.symlink_metadata(file_name)?;
        if current.file_type().is_symlink()
            || !current.is_file()
            || metadata_identity_std(&opened) != metadata_identity_cap(&current)
        {
            return contract("provider invocation entry changed while reading");
        }
        Ok(parse_json(&bytes)?)
    }

    fn atomic_write(
        &self,
        file_name: &str,
        value: &Value,
        replace: bool,
    ) -> Result<(), RuntimeError> {
        let bytes = canonical_json(value)?;
        ensure_record_size(&bytes, MAX_CONTRACT_JSON_BYTES)?;
        let temporary = format!(
            ".{file_name}.{}.{}.tmp",
            std::process::id(),
            TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        );
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        configure_private_nofollow(&mut options);
        let mut temporary_file = self
            .directory
            .open_with(&temporary, &options)
            .map_err(RuntimeError::Io)?
            .into_std();
        let write_result = (|| -> Result<(), RuntimeError> {
            temporary_file.write_all(&bytes)?;
            temporary_file.sync_all()?;
            match self.directory.symlink_metadata(file_name) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() || !metadata.is_file() {
                        return contract("provider invocation destination is unsafe");
                    }
                    if !replace {
                        return contract("provider invocation destination already exists");
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    if replace {
                        return contract("provider invocation destination is missing");
                    }
                }
                Err(error) => return Err(RuntimeError::Io(error)),
            }
            self.directory
                .rename(&temporary, &self.directory, file_name)?;
            self.directory.open(".")?.into_std().sync_all()?;
            Ok(())
        })();
        if write_result.is_err() {
            let _ = self.directory.remove_file(&temporary);
        }
        write_result
    }
}

fn with_store<T>(
    root: &Path,
    create_root: bool,
    lock: bool,
    operation: impl FnOnce(&Store) -> Result<T, RuntimeError>,
) -> Result<T, RuntimeError> {
    ensure_private_root(root, create_root)?;
    let metadata = std::fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return contract("provider invocation root is unsafe");
    }
    #[cfg(unix)]
    if std_mode(&metadata) & 0o077 != 0 {
        return contract("provider invocation root permissions are too broad");
    }
    let directory = Dir::open_ambient_dir(root, ambient_authority())?;
    if metadata_identity_std(&metadata) != metadata_identity_cap(&directory.dir_metadata()?) {
        return contract("provider invocation root changed while opening");
    }
    let lock_file = if lock {
        let lock_name = ".invocations.lock";
        match directory.symlink_metadata(lock_name) {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() || !metadata.is_file() {
                    return contract("provider invocation lock is unsafe");
                }
                #[cfg(unix)]
                if cap_mode(&metadata) & 0o077 != 0 {
                    return contract("provider invocation lock permissions are too broad");
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(RuntimeError::Io(error)),
        }
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true);
        configure_private_nofollow(&mut options);
        let file = directory.open_with(lock_name, &options)?.into_std();
        let opened = file.metadata()?;
        let current = directory.symlink_metadata(lock_name)?;
        #[cfg(unix)]
        if std_mode(&opened) & 0o077 != 0 || cap_mode(&current) & 0o077 != 0 {
            return contract("provider invocation lock permissions are too broad");
        }
        if current.file_type().is_symlink()
            || !current.is_file()
            || metadata_identity_std(&opened) != metadata_identity_cap(&current)
        {
            return contract("provider invocation lock is unsafe");
        }
        file.lock()
            .map_err(|_| RuntimeError::Contract("provider invocation lock failed".to_owned()))?;
        Some(file)
    } else {
        None
    };
    operation(&Store {
        directory,
        _lock: lock_file,
    })
}

fn ensure_private_root(root: &Path, create: bool) -> Result<(), RuntimeError> {
    match std::fs::symlink_metadata(root) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return contract("provider invocation root is unsafe");
            }
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && create => {
            std::fs::create_dir_all(root)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt as _;
                std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o700))?;
            }
            Ok(())
        }
        Err(error) => Err(RuntimeError::Io(error)),
    }
}

fn record_file_name(request_id: &str) -> Result<String, RuntimeError> {
    if !prefixed_hex(request_id, "adapter-request-", 16) {
        return contract("provider invocation request_id is invalid");
    }
    Ok(format!("{request_id}.json"))
}

fn exact_object<'a>(
    value: &'a Value,
    fields: &[&str],
    label: &str,
) -> Result<&'a Map<String, Value>, RuntimeError> {
    let object = value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))?;
    let expected = fields
        .iter()
        .copied()
        .collect::<std::collections::BTreeSet<_>>();
    let actual = object
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    if actual != expected {
        return contract(format!("{label} fields are invalid"));
    }
    Ok(object)
}

fn string_array(value: Option<&Value>, field: &str) -> Result<Vec<String>, RuntimeError> {
    let items = value
        .and_then(Value::as_array)
        .ok_or_else(|| RuntimeError::Contract(format!("provider-invocation {field} is invalid")))?;
    items
        .iter()
        .map(|item| {
            item.as_str()
                .filter(|item| nonempty(item))
                .map(str::to_owned)
                .ok_or_else(|| {
                    RuntimeError::Contract(format!(
                        "provider-invocation {field} must be unique strings"
                    ))
                })
        })
        .collect()
}

fn validate_sorted_strings(value: Option<&Value>, field: &str) -> Result<(), RuntimeError> {
    let values = string_array(value, field)?;
    let strings = values.iter().map(String::as_str).collect::<Vec<_>>();
    let mut sorted = strings.clone();
    sorted.sort_unstable();
    sorted.dedup();
    if strings != sorted {
        return contract(format!(
            "provider-invocation {field} must be sorted unique strings"
        ));
    }
    Ok(())
}

fn required_u64(value: Option<&Value>, message: &str) -> Result<u64, RuntimeError> {
    value
        .and_then(Value::as_u64)
        .ok_or_else(|| RuntimeError::Contract(message.to_owned()))
}

fn validate_claim_token(value: &str) -> Result<(), RuntimeError> {
    let count = value.chars().count();
    if !(32..=4096).contains(&count) {
        return contract(
            "provider invocation claim token must contain between 32 and 4096 characters",
        );
    }
    Ok(())
}

fn ensure_record_size(bytes: &[u8], maximum: usize) -> Result<(), RuntimeError> {
    if bytes.len() > maximum {
        return contract(format!("contract input has more than {maximum} bytes"));
    }
    Ok(())
}

fn current_epoch_seconds() -> Result<u64, RuntimeError> {
    Ok(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| RuntimeError::Contract("system time precedes Unix epoch".to_owned()))?
        .as_secs())
}

fn nonempty(value: &str) -> bool {
    !value.trim().is_empty()
}

fn prefixed_hex(value: &str, prefix: &str, digits: usize) -> bool {
    value
        .strip_prefix(prefix)
        .is_some_and(|suffix| is_lower_hex(suffix, digits))
}

fn is_lower_hex(value: &str, digits: usize) -> bool {
    value.len() == digits
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(unix)]
fn std_mode(metadata: &std::fs::Metadata) -> u32 {
    use std::os::unix::fs::MetadataExt as _;
    metadata.mode()
}

#[cfg(unix)]
fn cap_mode(metadata: &cap_std::fs::Metadata) -> u32 {
    use cap_std::fs::MetadataExt as _;
    metadata.mode()
}

#[cfg(unix)]
fn metadata_identity_std(metadata: &std::fs::Metadata) -> (u64, u64, u32, u64, i64, i64, i64, i64) {
    use std::os::unix::fs::MetadataExt as _;
    (
        metadata.dev(),
        metadata.ino(),
        metadata.mode(),
        metadata.size(),
        metadata.mtime(),
        metadata.mtime_nsec(),
        metadata.ctime(),
        metadata.ctime_nsec(),
    )
}

#[cfg(unix)]
fn metadata_identity_cap(
    metadata: &cap_std::fs::Metadata,
) -> (u64, u64, u32, u64, i64, i64, i64, i64) {
    use cap_std::fs::MetadataExt as _;
    (
        metadata.dev(),
        metadata.ino(),
        metadata.mode(),
        metadata.size(),
        metadata.mtime(),
        metadata.mtime_nsec(),
        metadata.ctime(),
        metadata.ctime_nsec(),
    )
}

#[cfg(not(unix))]
fn metadata_identity_std(
    metadata: &std::fs::Metadata,
) -> (u64, bool, Option<std::time::SystemTime>) {
    (
        metadata.len(),
        metadata.permissions().readonly(),
        metadata.modified().ok(),
    )
}

#[cfg(not(unix))]
fn metadata_identity_cap(
    metadata: &cap_std::fs::Metadata,
) -> (u64, bool, Option<std::time::SystemTime>) {
    (
        metadata.len(),
        metadata.permissions().readonly(),
        metadata
            .modified()
            .ok()
            .map(cap_std::time::SystemTime::into_std),
    )
}

#[cfg(unix)]
fn configure_private_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    options
        .follow(FollowSymlinks::No)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(windows)]
fn configure_private_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    options
        .follow(FollowSymlinks::No)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(unix, windows)))]
fn configure_private_nofollow(options: &mut OpenOptions) {
    options.follow(FollowSymlinks::No);
}

#[cfg(unix)]
fn configure_std_nofollow(options: &mut std::fs::OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt as _;
    options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(windows)]
fn configure_std_nofollow(options: &mut std::fs::OpenOptions) {
    use std::os::windows::fs::OpenOptionsExt as _;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(unix, windows)))]
fn configure_std_nofollow(_options: &mut std::fs::OpenOptions) {}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);
    const TOKEN: &str = "provider-invocation-test-token-00000001";

    fn root() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "agent-provider-invocation-{}-{}",
            std::process::id(),
            TEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn plan(approval: &Value) -> Value {
        json!({
            "fingerprint": "plan-fingerprint",
            "nodes": [{
                "approval": approval,
                "binding": {"kind": "tool", "name": "fixture"},
                "capability": "implementation.fixture",
                "id": "node-1",
                "idempotent": true,
                "max_retries": 1,
                "permission_profile": "project-write",
                "provider": "fixture-provider",
                "provider_manifest_digest": "a".repeat(64),
                "resource_keys": ["repository-write:fixture"],
                "side_effects": ["project-files"],
                "timeout_seconds": 10
            }],
            "plan_id": "plan-fixture",
            "schema_version": "1.0"
        })
    }

    fn context() -> Value {
        json!({"checkpoints": {"CP0": "completed"}})
    }

    fn result(record: &Value) -> Value {
        let request = record.get("request").expect("request");
        json!({
            "artifacts": [],
            "binding": request["binding"],
            "capability": request["capability"],
            "cleanup": [],
            "evidence": [{
                "artifact_ids": [],
                "data": {"changed_files": ["fixture"]},
                "kind": "delivery",
                "status": "completed",
                "summary": "completed"
            }],
            "failure_attribution": {"category": "none", "summary": "none"},
            "invocation_id": request["invocation_id"],
            "node_id": request["node_id"],
            "plan_fingerprint": request["plan_fingerprint"],
            "provider": request["provider"],
            "request_id": request["request_id"],
            "schema_version": "1.0",
            "status": "completed"
        })
    }

    #[test]
    fn deadline_is_exact_and_expired_resources_can_recover_with_new_request() {
        let root = root();
        let first = prepare_provider_invocation_at(
            &root,
            &plan(&Value::Null),
            "node-1",
            &context(),
            "invocation-1",
            100,
        )
        .expect("prepare first");
        let first_id = first
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .expect("request id");
        let claimed =
            claim_provider_invocation_at(&root, first_id, "host-1", TOKEN, 110).expect("claim");
        assert_eq!(
            claimed.pointer("/claim/deadline").and_then(Value::as_u64),
            Some(120)
        );
        assert_eq!(
            provider_invocation_state(&claimed, 120).expect("state"),
            "expired"
        );
        assert!(matches!(
            submit_provider_invocation_at(&root, first_id, &result(&first), TOKEN, 120),
            Err(RuntimeError::Contract(message)) if message.contains("expired")
        ));

        let second = prepare_provider_invocation_at(
            &root,
            &plan(&Value::Null),
            "node-1",
            &context(),
            "invocation-2",
            100,
        )
        .expect("prepare second");
        let second_id = second
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .expect("request id");
        assert!(matches!(
            claim_provider_invocation_at(&root, second_id, "host-2", TOKEN, 119),
            Err(RuntimeError::Contract(message)) if message.contains("resource is already claimed")
        ));
        claim_provider_invocation_at(&root, second_id, "host-2", TOKEN, 120)
            .expect("expired resource is released");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn approval_bound_plan_cannot_publish_without_runtime_proof() {
        let root = root();
        let error = prepare_provider_invocation_at(
            &root,
            &plan(&json!({
                "action": "write",
                "reason": "approval required",
                "scope": {}
            })),
            "node-1",
            &context(),
            "approval-invocation",
            100,
        )
        .expect_err("approval must fail closed");
        assert!(error.to_string().contains("runtime-granted attempt proof"));
        assert!(!root.exists());
    }

    #[test]
    fn missing_read_root_is_not_created_and_size_guard_fails_closed() {
        let root = root();
        let selection = json!({
            "plan_fingerprint": "plan-fingerprint",
            "requests": {},
            "schema_version": "1.0",
        });
        assert_eq!(
            collect_submitted_results_at(&root, "plan-fingerprint", &selection, 100)
                .expect("empty selection"),
            json!({})
        );
        assert!(!root.exists());
        assert!(matches!(
            inspect_provider_invocation_at(
                &root,
                "adapter-request-0000000000000000",
                100
            ),
            Err(RuntimeError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound
        ));
        assert!(!root.exists());
        assert!(matches!(
            ensure_record_size(b"12345", 4),
            Err(RuntimeError::Contract(message)) if message.contains("more than 4 bytes")
        ));
    }

    #[test]
    fn submitted_result_requires_exact_selection() {
        let root = root();
        let prepared = prepare_provider_invocation_at(
            &root,
            &plan(&Value::Null),
            "node-1",
            &context(),
            "selection-invocation",
            100,
        )
        .expect("prepare");
        let request_id = prepared
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .expect("request id");
        claim_provider_invocation_at(&root, request_id, "host", TOKEN, 110).expect("claim");
        submit_provider_invocation_at(&root, request_id, &result(&prepared), TOKEN, 111)
            .expect("submit");
        let selection = json!({
            "plan_fingerprint": "plan-fingerprint",
            "requests": {"node-1": request_id},
            "schema_version": "1.0",
        });
        let selected = collect_submitted_results_at(&root, "plan-fingerprint", &selection, 111)
            .expect("selected");
        assert_eq!(
            selected
                .pointer("/node-1/request_id")
                .and_then(Value::as_str),
            Some(request_id)
        );
        let retry = prepare_provider_invocation_at(
            &root,
            &plan(&Value::Null),
            "node-1",
            &context(),
            "selection-retry",
            100,
        )
        .expect("prepare retry");
        let retry_id = retry
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .expect("retry request id");
        claim_provider_invocation_at(&root, retry_id, "retry-host", TOKEN, 112)
            .expect("claim retry");
        submit_provider_invocation_at(&root, retry_id, &result(&retry), TOKEN, 113)
            .expect("submit retry");
        let retry_selection = json!({
            "plan_fingerprint": "plan-fingerprint",
            "requests": {"node-1": retry_id},
            "schema_version": "1.0",
        });
        let selected_retry =
            collect_submitted_results_at(&root, "plan-fingerprint", &retry_selection, 113)
                .expect("selected retry");
        assert_eq!(
            selected_retry
                .pointer("/node-1/request_id")
                .and_then(Value::as_str),
            Some(retry_id)
        );
        let selected_original =
            collect_submitted_results_at(&root, "plan-fingerprint", &selection, 113)
                .expect("selected original");
        assert_eq!(
            selected_original
                .pointer("/node-1/request_id")
                .and_then(Value::as_str),
            Some(request_id)
        );
        let drifted = json!({
            "plan_fingerprint": "plan-fingerprint",
            "requests": {"node-1": "adapter-request-0000000000000000"},
            "schema_version": "1.0",
        });
        assert!(matches!(
            collect_submitted_results_at(&root, "plan-fingerprint", &drifted, 111),
            Err(RuntimeError::Contract(message)) if message.contains("not submitted")
        ));
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn broad_root_and_lock_permissions_are_rejected() {
        use std::os::unix::fs::PermissionsExt as _;

        let root = root();
        std::fs::create_dir_all(&root).expect("root");
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o770))
            .expect("chmod root");
        assert!(matches!(
            prepare_provider_invocation_at(
                &root,
                &plan(&Value::Null),
                "node-1",
                &context(),
                "permission-invocation",
                100,
            ),
            Err(RuntimeError::Contract(message)) if message.contains("root permissions")
        ));
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700))
            .expect("chmod root");
        let prepared = prepare_provider_invocation_at(
            &root,
            &plan(&Value::Null),
            "node-1",
            &context(),
            "permission-invocation",
            100,
        )
        .expect("prepare");
        std::fs::set_permissions(
            root.join(".invocations.lock"),
            std::fs::Permissions::from_mode(0o660),
        )
        .expect("chmod lock");
        let request_id = prepared
            .pointer("/request/request_id")
            .and_then(Value::as_str)
            .expect("request id");
        assert!(matches!(
            claim_provider_invocation_at(&root, request_id, "host", TOKEN, 110),
            Err(RuntimeError::Contract(message)) if message.contains("lock permissions")
        ));
        let _ = std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700));
        let _ = std::fs::remove_dir_all(root);
    }
}
