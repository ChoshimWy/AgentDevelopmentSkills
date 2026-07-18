//! Worktree Session Context v1 validation and lifecycle primitives.

use crate::RuntimeError;
use crate::git_workspace::{inspect_repository, session_source_identity};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::path::{Component, Path};

const CONTEXT_FIELDS: &[&str] = &[
    "schema_version",
    "session_id",
    "project_id",
    "selected_platforms",
    "created_at",
    "repositories",
    "dependencies",
    "source_identity",
    "platform_contexts",
    "capability_closure",
    "verification",
    "review",
    "lifecycle",
];

/// Validate one Worktree Session Context v1.
///
/// # Errors
/// Returns an error for shape, identity, ordering, lifecycle, repository, or
/// evidence-index violations.
#[allow(clippy::too_many_lines)]
pub fn validate_worktree_session_context(value: &Value) -> Result<(), RuntimeError> {
    let context = exact_object(value, CONTEXT_FIELDS, "worktree-session-context")?;
    if string(context, "schema_version") != Some("1.0") {
        return contract("unsupported schema_version");
    }
    for field in ["session_id", "project_id"] {
        if !string(context, field).is_some_and(valid_identifier) {
            return contract(format!("worktree-session-context {field} is invalid"));
        }
    }
    if string(context, "created_at").is_none_or(str::is_empty) {
        return contract("worktree-session-context created_at is invalid");
    }
    let platforms = array(context, "selected_platforms", "worktree-session-context")?;
    let platform_names = string_array(platforms).ok_or_else(|| {
        RuntimeError::Contract(
            "worktree-session-context selected_platforms must be sorted unique platform ids"
                .to_owned(),
        )
    })?;
    if !sorted_unique(&platform_names) || !platform_names.iter().all(|value| valid_platform(value))
    {
        return contract(
            "worktree-session-context selected_platforms must be sorted unique platform ids",
        );
    }
    let platform_contexts = object_field(context, "platform_contexts", "worktree-session-context")?;
    if platform_contexts
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != platform_names.iter().copied().collect()
    {
        return contract(
            "worktree-session-context requires one provider closure per selected platform",
        );
    }
    for provider_context in platform_contexts.values() {
        let provider_context = exact_object(
            provider_context,
            &["provider_id", "bindings", "context"],
            "worktree-session-context.platform_context",
        )?;
        if string(provider_context, "provider_id").is_none_or(str::is_empty) {
            return contract("worktree-session-context platform provider_id is invalid");
        }
        if !provider_context
            .get("context")
            .is_some_and(Value::is_object)
        {
            return contract("worktree-session-context platform binding closure is invalid");
        }
        let bindings = provider_context
            .get("bindings")
            .and_then(Value::as_object)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                RuntimeError::Contract(
                    "worktree-session-context platform binding closure is invalid".to_owned(),
                )
            })?;
        if bindings
            .iter()
            .any(|(capability, binding)| capability.is_empty() || !valid_binding(binding))
        {
            return contract("worktree-session-context platform binding closure is invalid");
        }
    }
    let closure = object_field(context, "capability_closure", "worktree-session-context")?;
    for (capability, provider) in closure {
        let provider = exact_object(
            provider,
            &["provider_id", "binding"],
            "worktree-session-context.capability_closure.provider",
        )?;
        if capability.is_empty()
            || string(provider, "provider_id").is_none_or(str::is_empty)
            || !provider.get("binding").is_some_and(valid_binding)
        {
            return contract("worktree-session-context capability_closure entry is invalid");
        }
    }
    let repositories = array(context, "repositories", "worktree-session-context")?;
    if repositories.is_empty() {
        return contract("worktree-session-context repositories must be a non-empty array");
    }
    let mut repository_ids = Vec::with_capacity(repositories.len());
    let mut worktree_paths = BTreeSet::new();
    let mut common_dirs = BTreeSet::new();
    let mut primary_count = 0_usize;
    for repository in repositories {
        validate_repository(repository)?;
        let repository = object(repository, "worktree-session-context.repository")?;
        let repository_id = required_string(
            repository,
            "repository_id",
            "worktree-session-context.repository",
        )?;
        repository_ids.push(repository_id);
        worktree_paths.insert(required_string(
            repository,
            "worktree_path",
            "worktree-session-context.repository",
        )?);
        common_dirs.insert(required_string(
            repository,
            "git_common_dir",
            "worktree-session-context.repository",
        )?);
        primary_count += usize::from(string(repository, "role") == Some("primary"));
    }
    if !sorted_unique(&repository_ids) {
        return contract(
            "worktree-session-context repositories must be sorted by unique repository_id",
        );
    }
    if worktree_paths.len() != repositories.len() || common_dirs.len() != repositories.len() {
        return contract("worktree-session-context repository paths must be unique");
    }
    if primary_count != 1 {
        return contract("worktree-session-context requires exactly one primary repository");
    }
    validate_dependencies(
        context,
        required_string(context, "session_id", "worktree-session-context")?,
    )?;
    let identity = exact_object(
        required(context, "source_identity", "worktree-session-context")?,
        &["algorithm", "mode", "value"],
        "worktree-session-context.source_identity",
    )?;
    if string(identity, "algorithm") != Some("session-source-v1")
        || !matches!(string(identity, "mode"), Some("working" | "committed"))
    {
        return contract("worktree-session-context source identity metadata is invalid");
    }
    if !string(identity, "value").is_some_and(|value| valid_prefixed_hash(value, "session-source:"))
    {
        return contract("worktree-session-context source identity value is invalid");
    }
    validate_evidence_index(
        required(context, "verification", "worktree-session-context")?,
        "verification",
    )?;
    validate_evidence_index(
        required(context, "review", "worktree-session-context")?,
        "review",
    )?;
    let lifecycle = exact_object(
        required(context, "lifecycle", "worktree-session-context")?,
        &["state"],
        "worktree-session-context.lifecycle",
    )?;
    let state = string(lifecycle, "state").unwrap_or_default();
    if !matches!(
        state,
        "created" | "active" | "checkpointed" | "gated" | "integrated" | "closed" | "blocked"
    ) {
        return contract("worktree-session-context lifecycle state is invalid");
    }
    let mode = required_string(identity, "mode", "worktree-session-context.source_identity")?;
    let any_checkpoint = repositories
        .iter()
        .any(|repository| !repository.get("checkpoint").is_none_or(Value::is_null));
    let missing_checkpoint = repositories
        .iter()
        .any(|repository| repository.get("checkpoint").is_none_or(Value::is_null));
    if matches!(state, "created" | "active") && (mode != "working" || any_checkpoint) {
        return contract(
            "worktree-session-context editable state requires working source identity",
        );
    }
    if (matches!(state, "checkpointed" | "gated" | "integrated" | "closed") || mode == "committed")
        && (mode != "committed" || missing_checkpoint)
    {
        return contract(
            "worktree-session-context committed state requires every repository checkpoint",
        );
    }
    if matches!(state, "gated" | "integrated" | "closed")
        && (evidence_status(context, "verification") != Some("passed")
            || evidence_status(context, "review") != Some("passed"))
    {
        return contract(
            "worktree-session-context gated state requires passed verification and review",
        );
    }
    Ok(())
}

/// Construct and refresh one deterministic Session Context from an explicit
/// creation envelope. `created_at` is required by this parallel compatibility
/// API so tests and callers can freeze identity.
///
/// # Errors
/// Returns an error for invalid input or live repository drift.
pub fn new_session_context(input: &Value) -> Result<Value, RuntimeError> {
    let input = input.as_object().ok_or_else(|| {
        RuntimeError::Contract("session context input must be an object".to_owned())
    })?;
    let allowed = [
        "session_id",
        "project_id",
        "repositories",
        "selected_platforms",
        "platform_contexts",
        "capability_closure",
        "dependencies",
        "created_at",
    ];
    if input.keys().any(|field| !allowed.contains(&field.as_str())) {
        return contract("session context input fields are invalid");
    }
    let session_id = required_string(input, "session_id", "session context input")?;
    let project_id = required_string(input, "project_id", "session context input")?;
    let created_at = required_string(input, "created_at", "session context input")?;
    let repositories = input
        .get("repositories")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            RuntimeError::Contract("session context repositories are required".to_owned())
        })?;
    let mut repositories = repositories.clone();
    repositories.sort_by(|left, right| {
        left.get("repository_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .cmp(
                right
                    .get("repository_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
    });
    let mut selected_platforms = input
        .get("selected_platforms")
        .cloned()
        .unwrap_or_else(|| json!([]))
        .as_array()
        .cloned()
        .ok_or_else(|| RuntimeError::Contract("selected platforms must be an array".to_owned()))?;
    let original_platform_count = selected_platforms.len();
    selected_platforms.sort_by(|left, right| left.as_str().cmp(&right.as_str()));
    selected_platforms.dedup();
    if selected_platforms.len() != original_platform_count {
        return contract("selected platforms must be unique");
    }
    let mut dependencies = input
        .get("dependencies")
        .cloned()
        .unwrap_or_else(|| json!([]))
        .as_array()
        .cloned()
        .ok_or_else(|| {
            RuntimeError::Contract("session dependencies must be an array".to_owned())
        })?;
    dependencies.sort_by(|left, right| {
        left.get("session_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .cmp(
                right
                    .get("session_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
    });
    let mut context = json!({
        "created_at": created_at,
        "capability_closure": input.get("capability_closure").cloned().unwrap_or_else(|| json!({})),
        "dependencies": dependencies,
        "lifecycle": {"state": "created"},
        "platform_contexts": input.get("platform_contexts").cloned().unwrap_or_else(|| json!({})),
        "project_id": project_id,
        "repositories": repositories,
        "review": {"adapter_result_refs": [], "status": "pending"},
        "schema_version": "1.0",
        "selected_platforms": selected_platforms,
        "session_id": session_id,
        "source_identity": {"algorithm": "session-source-v1", "mode": "working", "value": ""},
        "verification": {"adapter_result_refs": [], "status": "pending"},
    });
    refresh_session_source_identity(&mut context)?;
    validate_worktree_session_context(&context)?;
    Ok(context)
}

/// Refresh repository patches and source identity from live Worktrees.
///
/// # Errors
/// Returns an error for invalid context structure, repository drift, or Git
/// failures.
pub fn refresh_session_source_identity(context: &mut Value) -> Result<(), RuntimeError> {
    let context_object = context.as_object().ok_or_else(|| {
        RuntimeError::Contract("worktree-session-context must be an object".to_owned())
    })?;
    let mode = context_object
        .get("source_identity")
        .and_then(Value::as_object)
        .and_then(|identity| identity.get("mode"))
        .and_then(Value::as_str)
        .unwrap_or("working")
        .to_owned();
    if !matches!(mode.as_str(), "working" | "committed") {
        return contract("session source identity mode is invalid");
    }
    let repositories = context_object
        .get("repositories")
        .and_then(Value::as_array)
        .ok_or_else(|| RuntimeError::Contract("session repositories must be an array".to_owned()))?
        .clone();
    let mut refreshed = Vec::with_capacity(repositories.len());
    for repository in repositories {
        let repository = object(&repository, "session repository")?;
        let base = object(
            required(repository, "base", "session repository")?,
            "session repository base",
        )?;
        let base_reference = required_string(base, "commit", "session repository base")?;
        let base_source = required_string(base, "source", "session repository base")?;
        let original_ref = required_string(base, "ref", "session repository base")?;
        let worktree = required_string(repository, "worktree_path", "session repository")?;
        let repository_id = required_string(repository, "repository_id", "session repository")?;
        let role = required_string(repository, "role", "session repository")?;
        let mut record = inspect_repository(
            Path::new(worktree),
            repository_id,
            role,
            base_reference,
            base_source,
            mode == "committed",
        )?;
        let record_base = record
            .get_mut("base")
            .and_then(Value::as_object_mut)
            .ok_or_else(|| {
                RuntimeError::Contract("inspected repository base is missing".to_owned())
            })?;
        record_base.insert("ref".to_owned(), Value::String(original_ref.to_owned()));
        refreshed.push(record);
    }
    refreshed.sort_by(|left, right| {
        left.get("repository_id")
            .and_then(Value::as_str)
            .cmp(&right.get("repository_id").and_then(Value::as_str))
    });
    let identity = session_source_identity(&Value::Array(refreshed.clone()), &mode)?;
    let context_object = context.as_object_mut().ok_or_else(|| {
        RuntimeError::Contract("worktree-session-context must be an object".to_owned())
    })?;
    context_object.insert("repositories".to_owned(), Value::Array(refreshed));
    context_object.insert(
        "source_identity".to_owned(),
        json!({
            "algorithm": "session-source-v1",
            "mode": mode,
            "value": identity,
        }),
    );
    Ok(())
}

/// Freeze a clean active Session at committed repository checkpoints.
///
/// # Errors
/// Returns an error unless the context is active and every repository has a
/// clean committed identity.
pub fn freeze_checkpoint(context: &Value) -> Result<Value, RuntimeError> {
    if context.pointer("/lifecycle/state").and_then(Value::as_str) != Some("active") {
        return contract("checkpoint requires an active worktree session");
    }
    let mut candidate = context.clone();
    candidate
        .pointer_mut("/source_identity/mode")
        .ok_or_else(|| RuntimeError::Contract("session source identity is missing".to_owned()))?
        .clone_from(&Value::String("committed".to_owned()));
    refresh_session_source_identity(&mut candidate)?;
    let candidate_object = candidate.as_object_mut().ok_or_else(|| {
        RuntimeError::Contract("worktree-session-context must be an object".to_owned())
    })?;
    candidate_object.insert(
        "verification".to_owned(),
        json!({"adapter_result_refs": [], "status": "pending"}),
    );
    candidate_object.insert(
        "review".to_owned(),
        json!({"adapter_result_refs": [], "status": "pending"}),
    );
    candidate_object.insert("lifecycle".to_owned(), json!({"state": "checkpointed"}));
    validate_worktree_session_context(&candidate)?;
    Ok(candidate)
}

/// Apply one legal non-gated Session lifecycle transition.
///
/// # Errors
/// Returns an error for illegal transitions or refresh failures.
pub fn transition_session_context(context: &Value, target: &str) -> Result<Value, RuntimeError> {
    validate_worktree_session_context(context)?;
    let current = context
        .pointer("/lifecycle/state")
        .and_then(Value::as_str)
        .ok_or_else(|| RuntimeError::Contract("session lifecycle is missing".to_owned()))?;
    if !legal_transition(current, target) {
        return contract(format!(
            "illegal worktree session transition: {current} -> {target}"
        ));
    }
    if target == "gated" {
        return contract("use evaluate_and_gate for the gated transition");
    }
    let mut candidate = context.clone();
    if target == "active"
        && candidate
            .pointer("/source_identity/mode")
            .and_then(Value::as_str)
            == Some("committed")
    {
        let source_mode = candidate
            .pointer_mut("/source_identity/mode")
            .ok_or_else(|| {
                RuntimeError::Contract("session source identity is missing".to_owned())
            })?;
        source_mode.clone_from(&Value::String("working".to_owned()));
        refresh_session_source_identity(&mut candidate)?;
        let candidate_object = candidate.as_object_mut().ok_or_else(|| {
            RuntimeError::Contract("worktree-session-context must be an object".to_owned())
        })?;
        candidate_object.insert(
            "verification".to_owned(),
            json!({"adapter_result_refs": [], "status": "pending"}),
        );
        candidate_object.insert(
            "review".to_owned(),
            json!({"adapter_result_refs": [], "status": "pending"}),
        );
    }
    let lifecycle = candidate
        .pointer_mut("/lifecycle/state")
        .ok_or_else(|| RuntimeError::Contract("session lifecycle is missing".to_owned()))?;
    lifecycle.clone_from(&Value::String(target.to_owned()));
    validate_worktree_session_context(&candidate)?;
    Ok(candidate)
}

#[allow(clippy::too_many_lines)]
fn validate_repository(value: &Value) -> Result<(), RuntimeError> {
    let repository = exact_object(
        value,
        &[
            "repository_id",
            "role",
            "branch",
            "worktree_path",
            "git_common_dir",
            "base",
            "checkpoint",
            "change_set",
        ],
        "worktree-session-context.repository",
    )?;
    if !string(repository, "repository_id").is_some_and(valid_identifier) {
        return contract("worktree-session-context repository_id is invalid");
    }
    if !matches!(string(repository, "role"), Some("primary" | "dependency")) {
        return contract("worktree-session-context repository role is invalid");
    }
    if repository
        .get("branch")
        .is_some_and(|value| !value.is_null() && value.as_str().is_none_or(str::is_empty))
    {
        return contract("worktree-session-context repository branch is invalid");
    }
    for field in ["worktree_path", "git_common_dir"] {
        if !string(repository, field).is_some_and(|value| Path::new(value).is_absolute()) {
            return contract(format!(
                "worktree-session-context repository {field} must be absolute"
            ));
        }
    }
    let base = exact_object(
        required(repository, "base", "worktree-session-context.repository")?,
        &["ref", "commit", "source", "dirty_worktree_inherited"],
        "worktree-session-context.repository.base",
    )?;
    if string(base, "ref").is_none_or(str::is_empty)
        || !string(base, "commit").is_some_and(valid_git_oid)
        || !matches!(
            string(base, "source"),
            Some("explicit" | "integration-checkpoint" | "stacked-checkpoint" | "clean-head")
        )
        || base
            .get("dirty_worktree_inherited")
            .and_then(Value::as_bool)
            != Some(false)
    {
        return contract("worktree-session-context repository base is invalid");
    }
    if let Some(checkpoint) = repository
        .get("checkpoint")
        .filter(|value| !value.is_null())
    {
        let checkpoint = exact_object(
            checkpoint,
            &["commit", "tree"],
            "worktree-session-context.repository.checkpoint",
        )?;
        if !["commit", "tree"]
            .iter()
            .all(|field| string(checkpoint, field).is_some_and(valid_git_oid))
        {
            return contract("worktree-session-context repository checkpoint is invalid");
        }
    }
    let change_set = exact_object(
        required(
            repository,
            "change_set",
            "worktree-session-context.repository",
        )?,
        &[
            "algorithm",
            "patch_hash",
            "changed_files",
            "untracked_files",
        ],
        "worktree-session-context.repository.change_set",
    )?;
    if string(change_set, "algorithm") != Some("repository-patch-v1") {
        return contract("worktree-session-context repository patch algorithm is invalid");
    }
    if !string(change_set, "patch_hash")
        .is_some_and(|value| valid_prefixed_hash(value, "repository-patch:"))
    {
        return contract("worktree-session-context repository patch hash is invalid");
    }
    for field in ["changed_files", "untracked_files"] {
        let paths = array(
            change_set,
            field,
            "worktree-session-context.repository.change_set",
        )?;
        let paths = string_array(paths).ok_or_else(|| {
            RuntimeError::Contract(format!(
                "worktree-session-context repository {field} is invalid"
            ))
        })?;
        if !sorted_unique(&paths) || !paths.iter().all(|path| safe_relative_path(path)) {
            return contract(format!(
                "worktree-session-context repository {field} is invalid"
            ));
        }
    }
    Ok(())
}

fn validate_dependencies(
    context: &Map<String, Value>,
    current_session: &str,
) -> Result<(), RuntimeError> {
    let dependencies = array(context, "dependencies", "worktree-session-context")?;
    let mut ids = Vec::with_capacity(dependencies.len());
    for dependency in dependencies {
        let dependency = exact_object(
            dependency,
            &["session_id", "dependency_type", "required_source_identity"],
            "worktree-session-context.dependency",
        )?;
        let session_id = string(dependency, "session_id").unwrap_or_default();
        if !valid_identifier(session_id)
            || session_id == current_session
            || string(dependency, "dependency_type") != Some("stacked")
            || !string(dependency, "required_source_identity")
                .is_some_and(|value| valid_prefixed_hash(value, "session-source:"))
        {
            return contract("worktree-session-context dependency is invalid");
        }
        ids.push(session_id);
    }
    if !sorted_unique(&ids) {
        return contract("worktree-session-context dependencies must be sorted and unique");
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_evidence_index(value: &Value, label: &str) -> Result<(), RuntimeError> {
    let evidence = exact_object(
        value,
        &["adapter_result_refs", "status"],
        &format!("worktree-session-context.{label}"),
    )?;
    if !matches!(
        string(evidence, "status"),
        Some("pending" | "passed" | "stale" | "blocked")
    ) {
        return contract(format!("worktree-session-context {label} is invalid"));
    }
    let references = array(
        evidence,
        "adapter_result_refs",
        &format!("worktree-session-context.{label}"),
    )?;
    let mut identities = Vec::with_capacity(references.len());
    for reference in references {
        let reference = exact_object(
            reference,
            &[
                "attempt_id",
                "request_id",
                "invocation_id",
                "plan_fingerprint",
                "node_id",
                "capability",
                "provider",
                "binding",
                "artifact_hashes",
            ],
            &format!("worktree-session-context.{label}.adapter_result_ref"),
        )?;
        for field in [
            "attempt_id",
            "request_id",
            "invocation_id",
            "plan_fingerprint",
            "node_id",
            "capability",
            "provider",
        ] {
            if string(reference, field).is_none_or(str::is_empty) {
                return contract(format!(
                    "worktree-session-context {label} adapter reference is invalid"
                ));
            }
        }
        if !reference.get("binding").is_some_and(valid_binding) {
            return contract(format!(
                "worktree-session-context {label} binding is invalid"
            ));
        }
        let artifacts = array(
            reference,
            "artifact_hashes",
            &format!("worktree-session-context.{label}.adapter_result_ref"),
        )?;
        if artifacts.is_empty() {
            return contract(format!(
                "worktree-session-context {label} adapter reference requires artifacts"
            ));
        }
        let mut artifact_ids = Vec::with_capacity(artifacts.len());
        for artifact in artifacts {
            let artifact = exact_object(
                artifact,
                &["artifact_id", "sha256", "uri"],
                &format!("worktree-session-context.{label}.artifact"),
            )?;
            if string(artifact, "artifact_id").is_none_or(str::is_empty)
                || string(artifact, "uri").is_none_or(str::is_empty)
                || !string(artifact, "sha256").is_some_and(valid_hash)
            {
                return contract(format!(
                    "worktree-session-context {label} artifact is invalid"
                ));
            }
            artifact_ids.push(required_string(
                artifact,
                "artifact_id",
                &format!("worktree-session-context.{label}.artifact"),
            )?);
        }
        if !sorted_unique(&artifact_ids) {
            return contract(format!(
                "worktree-session-context {label} artifact ids must be sorted and unique"
            ));
        }
        identities.push((
            required_string(
                reference,
                "attempt_id",
                &format!("worktree-session-context.{label}.adapter_result_ref"),
            )?,
            required_string(
                reference,
                "invocation_id",
                &format!("worktree-session-context.{label}.adapter_result_ref"),
            )?,
        ));
    }
    if identities.windows(2).any(|items| items[0] >= items[1]) {
        return contract(format!(
            "worktree-session-context {label} adapter refs must be sorted and unique"
        ));
    }
    Ok(())
}

fn legal_transition(current: &str, target: &str) -> bool {
    match current {
        "created" => matches!(target, "active" | "blocked"),
        "active" => matches!(target, "checkpointed" | "blocked"),
        "checkpointed" => matches!(target, "active" | "gated" | "blocked"),
        "gated" => matches!(target, "integrated" | "blocked"),
        "integrated" => matches!(target, "closed" | "blocked"),
        "blocked" => matches!(target, "active" | "closed"),
        _ => false,
    }
}

fn valid_binding(value: &Value) -> bool {
    let Some(binding) = value.as_object() else {
        return false;
    };
    let fields = binding.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let without_mode = ["kind", "name"].into_iter().collect::<BTreeSet<_>>();
    let with_mode = ["kind", "mode", "name"]
        .into_iter()
        .collect::<BTreeSet<_>>();
    (fields == without_mode || fields == with_mode)
        && matches!(
            string(binding, "kind"),
            Some("skill" | "agent" | "script" | "tool")
        )
        && string(binding, "name").is_some_and(|value| !value.is_empty())
        && (!binding.contains_key("mode")
            || string(binding, "mode").is_some_and(|value| !value.is_empty()))
}

fn safe_relative_path(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('/')
        && !value.contains('\\')
        && Path::new(value).components().all(|component| {
            !matches!(
                component,
                Component::CurDir
                    | Component::ParentDir
                    | Component::RootDir
                    | Component::Prefix(_)
            )
        })
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

fn valid_platform(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.as_bytes()[0].is_ascii_lowercase()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

fn valid_git_oid(value: &str) -> bool {
    matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_prefixed_hash(value: &str, prefix: &str) -> bool {
    value.strip_prefix(prefix).is_some_and(valid_hash)
}

fn sorted_unique<T: Ord>(values: &[T]) -> bool {
    values.windows(2).all(|items| items[0] < items[1])
}

fn evidence_status<'a>(context: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    context
        .get(field)
        .and_then(Value::as_object)
        .and_then(|value| string(value, "status"))
}

fn string_array(values: &[Value]) -> Option<Vec<&str>> {
    values.iter().map(Value::as_str).collect()
}

fn exact_object<'a>(
    value: &'a Value,
    fields: &[&str],
    label: &str,
) -> Result<&'a Map<String, Value>, RuntimeError> {
    let object = value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} fields are invalid")))?;
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let expected = fields.iter().copied().collect::<BTreeSet<_>>();
    if actual != expected {
        return contract(format!("{label} fields are invalid"));
    }
    Ok(object)
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, RuntimeError> {
    value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))
}

fn object_field<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a Map<String, Value>, RuntimeError> {
    required(object, field, label)?
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} must be an object")))
}

fn array<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a [Value], RuntimeError> {
    required(object, field, label)?
        .as_array()
        .map(Vec::as_slice)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} must be an array")))
}

fn required<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a Value, RuntimeError> {
    object
        .get(field)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))
}

fn required_string<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a str, RuntimeError> {
    string(object, field)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))
}

fn string<'a>(object: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    object.get(field).and_then(Value::as_str)
}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}
