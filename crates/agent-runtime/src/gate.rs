//! Worktree Session evidence attachment and Final Gate evaluation.

use crate::RuntimeError;
use crate::adapters::{validate_adapter_request, validate_adapter_result};
use crate::sessions::{refresh_session_source_identity, validate_worktree_session_context};
use agent_contracts::canonical_json;
use cap_fs_ext::{DirExt as _, FollowSymlinks, OpenOptionsFollowExt as _};
use cap_std::ambient_authority;
#[cfg(unix)]
use cap_std::fs::OpenOptionsExt as _;
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::Metadata;
use std::io::Read as _;
use std::path::{Component, Path, PathBuf};

struct ArtifactRoot {
    directory: Dir,
    path: PathBuf,
}

/// Attach one completed verification or review Adapter Result to a Session.
///
/// This stores only the frozen Result identity and artifact hashes. The
/// Adapter pair, Run Ledger, and artifact bytes remain authoritative and are
/// revalidated by the Final Gate.
///
/// # Errors
/// Returns an error for stale Session identity, invalid Result contracts,
/// unsupported capabilities, missing artifacts, or non-passed evidence.
pub fn attach_adapter_result(
    context: &mut Value,
    attempt_id: &str,
    request: &Value,
    result: &Value,
) -> Result<(), RuntimeError> {
    validate_worktree_session_context(context)?;
    validate_adapter_result(request, result)?;
    validate_request_session_identity(context, request)?;
    if string(result, "status") != Some("completed") {
        return contract(
            "only completed adapter results can be attached as passed session evidence",
        );
    }
    let artifacts = array(result, "artifacts", "adapter-result")?;
    if artifacts.is_empty() {
        return contract("worktree session evidence requires at least one hashed artifact");
    }
    let capability = required_string(result, "capability", "adapter-result")?;
    let (label, kind) = if capability.starts_with("verification.") {
        ("verification", "validation")
    } else if capability == "review.independent" {
        ("review", "review")
    } else {
        return contract("only verification or review results can be attached to a session gate");
    };
    validate_result_group(context, label, result)?;
    let matching = array(result, "evidence", "adapter-result")?
        .iter()
        .filter(|item| string(item, "kind") == Some(kind))
        .collect::<Vec<_>>();
    if matching.is_empty()
        || matching
            .iter()
            .any(|item| !matches!(string(item, "status"), Some("passed" | "completed")))
    {
        return contract(format!("worktree session {label} evidence is not passed"));
    }

    let mut artifact_hashes = artifacts
        .iter()
        .map(|artifact| {
            Ok(json!({
                "artifact_id": required_string(artifact, "artifact_id", "adapter artifact")?,
                "sha256": required_string(artifact, "sha256", "adapter artifact")?,
                "uri": required_string(artifact, "uri", "adapter artifact")?,
            }))
        })
        .collect::<Result<Vec<_>, RuntimeError>>()?;
    sort_by_string_field(&mut artifact_hashes, "artifact_id");
    let reference = json!({
        "artifact_hashes": artifact_hashes,
        "attempt_id": attempt_id,
        "binding": required_value(result, "binding", "adapter-result")?.clone(),
        "capability": capability,
        "invocation_id": required_string(result, "invocation_id", "adapter-result")?,
        "node_id": required_string(result, "node_id", "adapter-result")?,
        "plan_fingerprint": required_string(result, "plan_fingerprint", "adapter-result")?,
        "provider": required_string(result, "provider", "adapter-result")?,
        "request_id": required_string(result, "request_id", "adapter-result")?,
    });
    let group = context
        .get_mut(label)
        .and_then(Value::as_object_mut)
        .ok_or_else(|| RuntimeError::Contract(format!("session {label} is invalid")))?;
    let references = group
        .get_mut("adapter_result_refs")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| {
            RuntimeError::Contract(format!("session {label} evidence references are invalid"))
        })?;
    references.retain(|item| {
        !(string(item, "attempt_id") == Some(attempt_id)
            && item.get("invocation_id") == reference.get("invocation_id"))
    });
    references.push(reference);
    references.sort_by(|left, right| {
        (
            string(left, "attempt_id").unwrap_or_default(),
            string(left, "invocation_id").unwrap_or_default(),
        )
            .cmp(&(
                string(right, "attempt_id").unwrap_or_default(),
                string(right, "invocation_id").unwrap_or_default(),
            ))
    });
    group.insert("status".to_owned(), Value::String("passed".to_owned()));
    validate_worktree_session_context(context)
}

/// Evaluate committed source, latest Run Ledger attempts, Adapter identities,
/// evidence semantics, and artifact bytes.
///
/// # Errors
/// Returns an error when an input contract is malformed. Evidence drift is
/// returned as a valid blocked Gate Result with deterministic diagnostics.
#[allow(clippy::too_many_lines)]
pub fn evaluate_session_gate(
    context: &Value,
    adapter_pairs: &Value,
    ledger: &Value,
    artifact_root: &Path,
) -> Result<Value, RuntimeError> {
    validate_worktree_session_context(context)?;
    crate::validate_run_ledger(ledger)?;
    let mut diagnostics = Vec::<(String, String)>::new();
    let frozen_identity = required_string(
        required_value(context, "source_identity", "worktree-session-context")?,
        "value",
        "session source identity",
    )?
    .to_owned();
    if context
        .pointer("/source_identity/mode")
        .and_then(Value::as_str)
        == Some("committed")
    {
        let mut refreshed = context.clone();
        match refresh_session_source_identity(&mut refreshed) {
            Ok(()) => {
                if refreshed
                    .pointer("/source_identity/value")
                    .and_then(Value::as_str)
                    != Some(frozen_identity.as_str())
                {
                    diagnostic(
                        &mut diagnostics,
                        "source-stale",
                        "Checkpoint source identity changed",
                    );
                }
                let current_checkpoints = repository_checkpoints(context)?;
                let refreshed_checkpoints = repository_checkpoints(&refreshed)?;
                if current_checkpoints != refreshed_checkpoints {
                    diagnostic(
                        &mut diagnostics,
                        "checkpoint-stale",
                        "Checkpoint Commit or tree changed",
                    );
                }
            }
            Err(error) => diagnostic(&mut diagnostics, "source-invalid", error.to_string()),
        }
    } else {
        diagnostic(
            &mut diagnostics,
            "source-not-committed",
            "Final Gate requires committed source identity",
        );
    }

    let pairs = array(adapter_pairs, "", "worktree session adapter pairs")?;
    let mut pair_lookup = BTreeMap::<(String, Option<String>), &Value>::new();
    for pair in pairs {
        let pair_object = pair.as_object().ok_or_else(|| {
            RuntimeError::Contract("worktree session adapter pair fields are invalid".to_owned())
        })?;
        if !exact_fields(pair_object, &["attempt_id", "request", "result"]) {
            return contract("worktree session adapter pair fields are invalid");
        }
        let attempt_id = required_string(pair, "attempt_id", "adapter pair")?.to_owned();
        let invocation = pair
            .get("result")
            .and_then(|result| result.get("invocation_id"))
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
        if pair_lookup.insert((attempt_id, invocation), pair).is_some() {
            return contract("worktree session adapter pairs must be unique");
        }
    }
    let latest_attempts = latest_attempts_by_node(ledger)?;
    let root = open_artifact_root(artifact_root);
    let root_error = root.as_ref().err().map(ToString::to_string);
    let root_state = root.as_ref().map_err(|_| {
        root_error
            .as_deref()
            .unwrap_or("adapter artifact root is missing or unsafe")
    });
    let verification_ids = validate_reference_group(
        context,
        "verification",
        &pair_lookup,
        ledger,
        &latest_attempts,
        root_state,
        &mut diagnostics,
    )?;
    let review_ids = validate_reference_group(
        context,
        "review",
        &pair_lookup,
        ledger,
        &latest_attempts,
        root_state,
        &mut diagnostics,
    )?;

    if context
        .pointer("/verification/status")
        .and_then(Value::as_str)
        != Some("passed")
        || verification_ids.is_empty()
    {
        diagnostic(
            &mut diagnostics,
            "verification-missing",
            "Passed verification evidence is required",
        );
    } else {
        let valid = verification_ids.iter().cloned().collect::<BTreeSet<_>>();
        let capabilities = reference_capabilities(context, "verification", &valid)?;
        let selected = array(context, "selected_platforms", "worktree-session-context")?;
        if selected.is_empty() {
            if !capabilities.contains("verification.git.repository") {
                diagnostic(
                    &mut diagnostics,
                    "verification-git-missing",
                    "Pure Git Sessions require verification.git.repository evidence",
                );
            }
        } else {
            let missing = selected
                .iter()
                .filter_map(Value::as_str)
                .filter(|platform| {
                    !capabilities.iter().any(|capability| {
                        capability.starts_with(&format!("verification.{platform}."))
                    })
                })
                .collect::<Vec<_>>();
            if !missing.is_empty() {
                diagnostic(
                    &mut diagnostics,
                    "verification-platform-missing",
                    format!(
                        "Passed verification evidence is missing for: {}",
                        missing.join(", ")
                    ),
                );
            }
        }
    }
    if context.pointer("/review/status").and_then(Value::as_str) != Some("passed")
        || review_ids.is_empty()
    {
        diagnostic(
            &mut diagnostics,
            "review-missing",
            "Passed independent review evidence is required",
        );
    }

    let mut checkpoint_commits = Map::new();
    for repository in array(context, "repositories", "worktree-session-context")? {
        if let Some(checkpoint) = repository.get("checkpoint").and_then(Value::as_object) {
            checkpoint_commits.insert(
                required_string(repository, "repository_id", "session repository")?.to_owned(),
                Value::String(
                    required_string(
                        checkpoint.get("commit").ok_or_else(|| {
                            RuntimeError::Contract(
                                "session repository checkpoint commit is missing".to_owned(),
                            )
                        })?,
                        "",
                        "session repository checkpoint",
                    )?
                    .to_owned(),
                ),
            );
        }
    }
    let diagnostics_value = diagnostics
        .iter()
        .map(|(code, message)| json!({"code": code, "message": message}))
        .collect::<Vec<_>>();
    let result = json!({
        "checkpoint_commits": checkpoint_commits,
        "diagnostics": diagnostics_value,
        "review_refs": review_ids,
        "schema_version": "1.0",
        "session_id": required_string(context, "session_id", "worktree-session-context")?,
        "source_identity": frozen_identity,
        "status": if diagnostics.is_empty() { "passed" } else { "blocked" },
        "verification_refs": verification_ids,
    });
    validate_worktree_session_gate(&result)?;
    Ok(result)
}

/// Validate a Worktree Session Gate Result v1.
///
/// # Errors
/// Returns an error for unknown fields, malformed identities, unsorted
/// references, or inconsistent passed/blocked semantics.
pub fn validate_worktree_session_gate(value: &Value) -> Result<(), RuntimeError> {
    let gate = object(value, "worktree-session-gate")?;
    if !exact_fields(
        gate,
        &[
            "checkpoint_commits",
            "diagnostics",
            "review_refs",
            "schema_version",
            "session_id",
            "source_identity",
            "status",
            "verification_refs",
        ],
    ) {
        return contract("worktree-session-gate fields are invalid");
    }
    if string(value, "schema_version") != Some("1.0") {
        return contract("unsupported schema_version for worktree-session-gate");
    }
    let session_id = required_string(value, "session_id", "worktree-session-gate")?;
    if !valid_identifier(session_id) {
        return contract("worktree-session-gate session_id is invalid");
    }
    let source_identity = required_string(value, "source_identity", "worktree-session-gate")?;
    if !valid_prefixed_hash(source_identity, "session-source:") {
        return contract("worktree-session-gate source_identity is invalid");
    }
    let commits = value
        .get("checkpoint_commits")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            RuntimeError::Contract(
                "worktree-session-gate checkpoint_commits are invalid".to_owned(),
            )
        })?;
    if commits.is_empty()
        || commits.iter().any(|(key, commit)| {
            !valid_identifier(key) || !valid_git_oid(commit.as_str().unwrap_or(""))
        })
    {
        return contract("worktree-session-gate checkpoint_commits are invalid");
    }
    for field in ["verification_refs", "review_refs"] {
        let refs = string_array(value, field, "worktree-session-gate")?;
        if refs.iter().any(String::is_empty) || refs.windows(2).any(|items| items[0] >= items[1]) {
            return contract(format!("worktree-session-gate {field} is invalid"));
        }
    }
    let status = required_string(value, "status", "worktree-session-gate")?;
    let diagnostics = array(value, "diagnostics", "worktree-session-gate")?;
    if !matches!(status, "passed" | "blocked") {
        return contract("worktree-session-gate status or diagnostics are invalid");
    }
    for item in diagnostics {
        let item = object(item, "worktree-session-gate.diagnostic")?;
        if !exact_fields(item, &["code", "message"])
            || item
                .values()
                .any(|field| field.as_str().is_none_or(str::is_empty))
        {
            return contract("worktree-session-gate diagnostic is invalid");
        }
    }
    let verification = array(value, "verification_refs", "worktree-session-gate")?;
    let review = array(value, "review_refs", "worktree-session-gate")?;
    if status == "passed"
        && (!diagnostics.is_empty() || verification.is_empty() || review.is_empty())
    {
        return contract(
            "worktree-session-gate passed result requires evidence and no diagnostics",
        );
    }
    if status == "blocked" && diagnostics.is_empty() {
        return contract("worktree-session-gate blocked result requires diagnostics");
    }
    Ok(())
}

fn validate_reference_group(
    context: &Value,
    label: &str,
    pairs: &BTreeMap<(String, Option<String>), &Value>,
    ledger: &Value,
    latest_attempts: &BTreeMap<String, String>,
    artifact_root: Result<&ArtifactRoot, &str>,
    diagnostics: &mut Vec<(String, String)>,
) -> Result<Vec<String>, RuntimeError> {
    let group = required_value(context, label, "worktree-session-context")?;
    let references = array(group, "adapter_result_refs", "session evidence group")?;
    let mut valid = Vec::new();
    for reference in references {
        let attempt = required_string(reference, "attempt_id", "adapter reference")?;
        let invocation = required_string(reference, "invocation_id", "adapter reference")?;
        let identity = format!("{attempt}:{invocation}");
        let key = (attempt.to_owned(), Some(invocation.to_owned()));
        let Some(pair) = pairs.get(&key) else {
            diagnostic(
                diagnostics,
                format!("{label}-pair-missing"),
                format!("Missing Adapter pair for {identity}"),
            );
            continue;
        };
        let result = (|| {
            let request = required_value(pair, "request", "adapter pair")?;
            let result = required_value(pair, "result", "adapter pair")?;
            validate_adapter_result(request, result)?;
            validate_result_group(context, label, result)?;
            validate_request_session_identity(context, request)?;
            validate_reference_identity(reference, request, result)?;
            validate_ledger_link(reference, result, ledger, latest_attempts)?;
            let artifact_root =
                artifact_root.map_err(|error| RuntimeError::Contract(error.to_owned()))?;
            validate_artifact_bytes(
                array(reference, "artifact_hashes", "adapter reference")?,
                artifact_root,
            )
        })();
        match result {
            Ok(()) => valid.push(identity),
            Err(error) => diagnostic(
                diagnostics,
                format!("{label}-invalid"),
                format!("{identity}: {error}"),
            ),
        }
    }
    valid.sort();
    Ok(valid)
}

fn validate_result_group(context: &Value, label: &str, result: &Value) -> Result<(), RuntimeError> {
    let capability = required_string(result, "capability", "adapter-result")?;
    let frozen = context
        .get("capability_closure")
        .and_then(Value::as_object)
        .and_then(|closure| closure.get(capability))
        .and_then(Value::as_object);
    if frozen.and_then(|provider| provider.get("provider_id")) != result.get("provider")
        || frozen.and_then(|provider| provider.get("binding")) != result.get("binding")
    {
        return contract(
            "Adapter Result is outside the frozen capability/provider/binding closure",
        );
    }
    let expected = if label == "verification" {
        if capability == "verification.git.repository" {
            if !array(context, "selected_platforms", "worktree-session-context")?.is_empty() {
                return contract(
                    "generic Git verification cannot replace selected-platform verification",
                );
            }
            true
        } else {
            let parts = capability.split('.').collect::<Vec<_>>();
            if parts.len() < 3
                || parts[0] != "verification"
                || !array(context, "selected_platforms", "worktree-session-context")?
                    .iter()
                    .any(|item| item.as_str() == Some(parts[1]))
            {
                return contract("verification capability does not belong to a selected platform");
            }
            context
                .pointer(&format!("/platform_contexts/{}/bindings", parts[1]))
                .and_then(Value::as_object)
                .and_then(|bindings| bindings.get(capability))
                == result.get("binding")
        }
    } else {
        capability == "review.independent"
    };
    let kind = if label == "verification" {
        "validation"
    } else {
        "review"
    };
    let evidence = array(result, "evidence", "adapter-result")?
        .iter()
        .filter(|item| string(item, "kind") == Some(kind))
        .collect::<Vec<_>>();
    if !expected
        || evidence.is_empty()
        || evidence
            .iter()
            .any(|item| !matches!(string(item, "status"), Some("passed" | "completed")))
    {
        return contract(format!("Adapter Result is not passed {label} evidence"));
    }
    Ok(())
}

fn validate_request_session_identity(context: &Value, request: &Value) -> Result<(), RuntimeError> {
    validate_adapter_request(request)?;
    let Some(session) = request
        .get("task_context")
        .and_then(Value::as_object)
        .and_then(|task| task.get("worktree_session"))
        .and_then(Value::as_object)
    else {
        return contract("adapter request lacks frozen worktree session identity");
    };
    if !exact_fields(session, &["session_id", "source_identity"]) {
        return contract("adapter request lacks frozen worktree session identity");
    }
    if session.get("session_id") != context.get("session_id")
        || session.get("source_identity") != context.pointer("/source_identity/value")
    {
        return contract("adapter request worktree session identity is stale");
    }
    Ok(())
}

fn validate_reference_identity(
    reference: &Value,
    request: &Value,
    result: &Value,
) -> Result<(), RuntimeError> {
    for field in [
        "request_id",
        "invocation_id",
        "plan_fingerprint",
        "node_id",
        "capability",
        "provider",
        "binding",
    ] {
        if reference.get(field) != result.get(field) || result.get(field) != request.get(field) {
            return contract(format!("adapter reference {field} identity mismatch"));
        }
    }
    let mut artifacts = array(result, "artifacts", "adapter-result")?
        .iter()
        .map(|item| {
            Ok(json!({
                "artifact_id": required_string(item, "artifact_id", "adapter artifact")?,
                "sha256": required_string(item, "sha256", "adapter artifact")?,
                "uri": required_string(item, "uri", "adapter artifact")?,
            }))
        })
        .collect::<Result<Vec<_>, RuntimeError>>()?;
    sort_by_string_field(&mut artifacts, "artifact_id");
    if reference.get("artifact_hashes") != Some(&Value::Array(artifacts)) {
        return contract("adapter reference artifact identity mismatch");
    }
    Ok(())
}

fn validate_ledger_link(
    reference: &Value,
    result: &Value,
    ledger: &Value,
    latest_attempts: &BTreeMap<String, String>,
) -> Result<(), RuntimeError> {
    let attempt_id = required_string(reference, "attempt_id", "adapter reference")?;
    if ledger.get("plan_fingerprint") != reference.get("plan_fingerprint") {
        return contract("run ledger plan fingerprint does not match Adapter evidence");
    }
    let node_id = required_string(reference, "node_id", "adapter reference")?;
    if latest_attempts.get(node_id).map(String::as_str) != Some(attempt_id) {
        return contract("adapter evidence does not belong to the latest node attempt");
    }
    let outcomes = array(ledger, "adapter_outcomes", "run-ledger")?
        .iter()
        .filter(|item| {
            string(item, "attempt_id") == Some(attempt_id)
                && item.get("invocation_id") == reference.get("invocation_id")
        })
        .collect::<Vec<_>>();
    if outcomes.len() != 1 {
        return contract("adapter evidence is not uniquely linked to a ledger outcome");
    }
    let outcome = outcomes[0];
    for field in ["node_id", "provider", "request_id", "invocation_id"] {
        if outcome.get(field) != reference.get(field) {
            return contract(format!("ledger outcome {field} mismatch"));
        }
    }
    if string(outcome, "status") != Some("completed")
        || string(result, "status") != Some("completed")
    {
        return contract("ledger outcome is not completed");
    }
    let mut ledger_artifacts = array(ledger, "artifact_hashes", "run-ledger")?
        .iter()
        .filter(|item| {
            string(item, "attempt_id") == Some(attempt_id)
                && string(item, "node_id") == Some(node_id)
        })
        .map(|item| {
            Ok(json!({
                "artifact_id": required_string(item, "artifact_id", "ledger artifact")?,
                "sha256": required_string(item, "sha256", "ledger artifact")?,
                "uri": required_string(item, "uri", "ledger artifact")?,
            }))
        })
        .collect::<Result<Vec<_>, RuntimeError>>()?;
    sort_by_string_field(&mut ledger_artifacts, "artifact_id");
    if reference.get("artifact_hashes") != Some(&Value::Array(ledger_artifacts)) {
        return contract("ledger artifact hashes do not match the Adapter Result");
    }
    let mut result_evidence = array(result, "evidence", "adapter-result")?
        .iter()
        .map(canonical_json)
        .collect::<Result<Vec<_>, _>>()?;
    result_evidence.sort();
    let mut ledger_evidence = array(ledger, "evidence", "run-ledger")?
        .iter()
        .filter(|item| {
            string(item, "attempt_id") == Some(attempt_id)
                && string(item, "node_id") == Some(node_id)
        })
        .map(|item| {
            canonical_json(&json!({
                "artifact_ids": item.get("artifact_ids").cloned().unwrap_or(Value::Null),
                "data": item.get("data").cloned().unwrap_or(Value::Null),
                "kind": item.get("kind").cloned().unwrap_or(Value::Null),
                "status": item.get("status").cloned().unwrap_or(Value::Null),
                "summary": item.get("summary").cloned().unwrap_or(Value::Null),
            }))
        })
        .collect::<Result<Vec<_>, _>>()?;
    ledger_evidence.sort();
    if result_evidence.is_empty() || result_evidence != ledger_evidence {
        return contract("run ledger and Adapter Result evidence semantics differ");
    }
    Ok(())
}

fn validate_artifact_bytes(
    artifacts: &[Value],
    artifact_root: &ArtifactRoot,
) -> Result<(), RuntimeError> {
    for artifact in artifacts {
        let artifact_id = required_string(artifact, "artifact_id", "adapter artifact")?;
        let uri = required_string(artifact, "uri", "adapter artifact")?;
        let raw = uri.strip_prefix("file://").unwrap_or(uri);
        let path = Path::new(raw);
        let candidate = if path.is_absolute() {
            path.to_path_buf()
        } else {
            artifact_root.path.join(path)
        };
        let normalized = lexical_absolute(&candidate)?;
        let relative = normalized.strip_prefix(&artifact_root.path).map_err(|_| {
            RuntimeError::Contract("adapter artifact escapes the allowed artifact root".to_owned())
        })?;
        let components = relative
            .components()
            .map(|component| {
                let Component::Normal(part) = component else {
                    return contract("adapter artifact escapes the allowed artifact root");
                };
                Ok(part.to_owned())
            })
            .collect::<Result<Vec<_>, RuntimeError>>()?;
        let Some((file_name, directory_parts)) = components.split_last() else {
            return contract(format!(
                "adapter artifact is missing or unsafe: {artifact_id}"
            ));
        };
        let mut directory = artifact_root.directory.try_clone()?;
        for part in directory_parts {
            directory = directory.open_dir_nofollow(part).map_err(|_| {
                RuntimeError::Contract(format!(
                    "adapter artifact is missing or unsafe: {artifact_id}"
                ))
            })?;
        }
        let mut options = OpenOptions::new();
        options.read(true).follow(FollowSymlinks::No);
        #[cfg(unix)]
        options.custom_flags(libc::O_NONBLOCK);
        let mut file = directory
            .open_with(file_name, &options)
            .map(cap_std::fs::File::into_std)
            .map_err(|_| {
                RuntimeError::Contract(format!(
                    "adapter artifact is missing or unsafe: {artifact_id}"
                ))
            })?;
        let opened = file.metadata()?;
        if !opened.is_file() {
            return contract(format!(
                "adapter artifact is missing or unsafe: {artifact_id}"
            ));
        }
        let mut digest = Sha256::new();
        let mut buffer = vec![0_u8; 1024 * 1024];
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            digest.update(&buffer[..count]);
        }
        let reopened = directory
            .open_with(file_name, &options)
            .map(cap_std::fs::File::into_std)
            .map_err(|_| {
                RuntimeError::Contract(format!(
                    "adapter artifact changed while hashing: {artifact_id}"
                ))
            })?;
        if metadata_identity(&opened) != metadata_identity(&reopened.metadata()?) {
            return contract(format!(
                "adapter artifact changed while hashing: {artifact_id}"
            ));
        }
        let expected = required_string(artifact, "sha256", "adapter artifact")?;
        if format!("{:x}", digest.finalize()) != expected {
            return contract(format!("adapter artifact hash mismatch: {artifact_id}"));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn open_artifact_root(path: &Path) -> Result<ArtifactRoot, RuntimeError> {
    let input = lexical_absolute(path)?;
    let before = std::fs::symlink_metadata(&input).map_err(|_| {
        RuntimeError::Contract(format!(
            "adapter artifact root is missing or unsafe: {}",
            input.display()
        ))
    })?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return contract(format!(
            "adapter artifact root is missing or unsafe: {}",
            input.display()
        ));
    }
    let path = std::fs::canonicalize(&input).map_err(|_| {
        RuntimeError::Contract(format!(
            "adapter artifact root is missing or unsafe: {}",
            input.display()
        ))
    })?;
    let mut directory = Dir::open_ambient_dir("/", ambient_authority())?;
    for component in path.components() {
        match component {
            Component::RootDir | Component::CurDir => {}
            Component::Normal(part) => {
                directory = directory.open_dir_nofollow(part).map_err(|_| {
                    RuntimeError::Contract(format!(
                        "adapter artifact root is missing or unsafe: {}",
                        path.display()
                    ))
                })?;
            }
            Component::ParentDir | Component::Prefix(_) => {
                return contract(format!(
                    "adapter artifact root is missing or unsafe: {}",
                    path.display()
                ));
            }
        }
    }
    let opened = directory.try_clone()?.into_std_file().metadata()?;
    let after = std::fs::symlink_metadata(&input).map_err(|_| {
        RuntimeError::Contract(format!(
            "adapter artifact root changed while opening: {}",
            input.display()
        ))
    })?;
    if after.file_type().is_symlink()
        || metadata_identity(&before) != metadata_identity(&opened)
        || metadata_identity(&opened) != metadata_identity(&after)
    {
        return contract(format!(
            "adapter artifact root changed while opening: {}",
            input.display()
        ));
    }
    Ok(ArtifactRoot { directory, path })
}

#[cfg(not(unix))]
fn open_artifact_root(path: &Path) -> Result<ArtifactRoot, RuntimeError> {
    let path = lexical_absolute(path)?;
    let before = std::fs::symlink_metadata(&path)?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return contract(format!(
            "adapter artifact root is missing or unsafe: {}",
            path.display()
        ));
    }
    let directory = Dir::open_ambient_dir(&path, ambient_authority())?;
    let opened = directory.try_clone()?.into_std_file().metadata()?;
    let after = std::fs::symlink_metadata(&path)?;
    if after.file_type().is_symlink()
        || metadata_identity(&before) != metadata_identity(&opened)
        || metadata_identity(&opened) != metadata_identity(&after)
    {
        return contract(format!(
            "adapter artifact root changed while opening: {}",
            path.display()
        ));
    }
    Ok(ArtifactRoot { directory, path })
}

fn latest_attempts_by_node(ledger: &Value) -> Result<BTreeMap<String, String>, RuntimeError> {
    let mut latest = BTreeMap::<String, (u64, String)>::new();
    for attempt in array(ledger, "node_attempts", "run-ledger")? {
        let number = attempt
            .get("attempt_number")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                RuntimeError::Contract("run ledger node attempt identity is invalid".to_owned())
            })?;
        let node_id = required_string(attempt, "node_id", "node-attempt")?;
        let attempt_id = required_string(attempt, "attempt_id", "node-attempt")?;
        if latest
            .get(node_id)
            .is_none_or(|(current, _)| number > *current)
        {
            latest.insert(node_id.to_owned(), (number, attempt_id.to_owned()));
        }
    }
    Ok(latest
        .into_iter()
        .map(|(node, (_, attempt))| (node, attempt))
        .collect())
}

fn reference_capabilities(
    context: &Value,
    label: &str,
    valid: &BTreeSet<String>,
) -> Result<BTreeSet<String>, RuntimeError> {
    Ok(array(
        required_value(context, label, "worktree-session-context")?,
        "adapter_result_refs",
        "session evidence group",
    )?
    .iter()
    .filter(|item| {
        let identity = format!(
            "{}:{}",
            string(item, "attempt_id").unwrap_or_default(),
            string(item, "invocation_id").unwrap_or_default()
        );
        valid.contains(&identity)
    })
    .filter_map(|item| string(item, "capability").map(ToOwned::to_owned))
    .collect())
}

fn repository_checkpoints(context: &Value) -> Result<Vec<Value>, RuntimeError> {
    Ok(array(context, "repositories", "worktree-session-context")?
        .iter()
        .map(|item| item.get("checkpoint").cloned().unwrap_or(Value::Null))
        .collect())
}

fn lexical_absolute(path: &Path) -> Result<PathBuf, RuntimeError> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::Prefix(prefix) => normalized.push(prefix.as_os_str()),
            Component::RootDir => normalized.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() {
                    return contract("adapter artifact escapes the allowed artifact root");
                }
            }
            Component::Normal(part) => normalized.push(part),
        }
    }
    Ok(normalized)
}

fn diagnostic(
    diagnostics: &mut Vec<(String, String)>,
    code: impl Into<String>,
    message: impl Into<String>,
) {
    let item = (code.into(), message.into());
    if !diagnostics.contains(&item) {
        diagnostics.push(item);
    }
}

fn sort_by_string_field(values: &mut [Value], field: &str) {
    values.sort_by(|left, right| {
        string(left, field)
            .unwrap_or_default()
            .cmp(string(right, field).unwrap_or_default())
    });
}

fn string_array(value: &Value, field: &str, label: &str) -> Result<Vec<String>, RuntimeError> {
    array(value, field, label)?
        .iter()
        .map(|item| {
            item.as_str()
                .map(ToOwned::to_owned)
                .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is invalid")))
        })
        .collect()
}

fn array<'a>(value: &'a Value, field: &str, label: &str) -> Result<&'a [Value], RuntimeError> {
    let candidate = if field.is_empty() {
        value
    } else {
        value
            .get(field)
            .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))?
    };
    candidate
        .as_array()
        .map(Vec::as_slice)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} must be an array")))
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, RuntimeError> {
    value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))
}

fn required_value<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a Value, RuntimeError> {
    if field.is_empty() {
        return Ok(value);
    }
    value
        .get(field)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))
}

fn required_string<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a str, RuntimeError> {
    required_value(value, field, label)?
        .as_str()
        .filter(|item| !item.is_empty())
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))
}

fn string<'a>(value: &'a Value, field: &str) -> Option<&'a str> {
    value.get(field).and_then(Value::as_str)
}

fn exact_fields(value: &Map<String, Value>, fields: &[&str]) -> bool {
    value.len() == fields.len() && fields.iter().all(|field| value.contains_key(*field))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value != "."
        && value != ".."
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        })
}

fn valid_git_oid(value: &str) -> bool {
    matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_prefixed_hash(value: &str, prefix: &str) -> bool {
    value.strip_prefix(prefix).is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
}

#[cfg(unix)]
fn metadata_identity(metadata: &Metadata) -> (u64, u64, u32, u64, i64, i64, i64, i64) {
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

#[cfg(not(unix))]
fn metadata_identity(metadata: &Metadata) -> (u64, bool, Option<std::time::SystemTime>) {
    (
        metadata.len(),
        metadata.permissions().readonly(),
        metadata.modified().ok(),
    )
}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}
