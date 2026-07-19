use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE, open_child_directory,
    open_child_file, open_root_directory,
};
use agent_engine::{DiscoveryEngine, compile_plan_with_package_lock, resolve_policy};
use agent_registry::{CORE_VERSION, ManifestRegistry};
use agent_runtime::{build_adapter_request, execute_recorded_plan};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::path::Path;

const IMPLEMENTATION_ACTOR: &str = "source-upgrade-smoke-builder";
const REVIEWER_ACTOR: &str = "source-upgrade-smoke-reviewer";

/// Exercise the newly published installed registry through the native workflow
/// control plane before source Activation is allowed to mutate external state.
///
/// The smoke remains inside the caller's `PublishedInstall` rollback window.
/// It loads only installed manifests, performs read-only discovery against a
/// private fixture, compiles a package-Lock-bound ready Plan, validates every
/// Skill binding, consumes contract-valid recorded Adapter results, and
/// requires independent review plus delivery reporting to complete.
pub(super) fn run_installed_workflow_smoke(
    target: &Path,
    package_lock: &Value,
) -> Result<Value, LifecycleError> {
    let registry = ManifestRegistry::from_directory(
        target.join(".agent-skills/packages"),
        &BTreeSet::new(),
        CORE_VERSION,
    )?;
    let fixture = tempfile::Builder::new()
        .prefix("source-upgrade-smoke-")
        .tempdir()?;
    std::fs::create_dir(fixture.path().join("App.xcodeproj"))?;
    std::fs::write(
        fixture.path().join("App.xcodeproj/project.pbxproj"),
        b"// fixture\n",
    )?;
    std::fs::write(fixture.path().join("Podfile"), b"platform :ios, '16.0'\n")?;
    let profile = DiscoveryEngine::new(&registry).discover(fixture.path(), &[], &[], None)?;
    let explicit_platforms = vec!["apple".to_owned()];
    let policy = resolve_policy(
        &profile,
        "实现 iOS 功能并补充测试",
        &explicit_platforms,
        None,
        &[],
    )?;
    let plan = compile_plan_with_package_lock(&registry, &profile, &policy, Some(package_lock))?;
    if plan.get("status").and_then(Value::as_str) != Some("ready") {
        return invalid("installed source upgrade smoke did not produce a ready Plan");
    }
    validate_skill_bindings(target, &plan)?;

    let context = json!({
        "actors": {
            "implementation_actor": IMPLEMENTATION_ACTOR,
            "reviewer_actor": REVIEWER_ACTOR,
        },
        "checkpoints": {
            "CP0": "completed",
            "CP1": "in_progress",
            "CP2": "pending",
            "CP3": "pending",
        },
        "target_modules": profile.get("target_modules").cloned().unwrap_or_else(|| json!([])),
        "task": policy.get("task").cloned().unwrap_or(Value::Null),
        "user_constraints": ["narrow verification", "independent reviewer"],
    });
    let mut results = Map::new();
    for node in plan.get("nodes").and_then(Value::as_array).ok_or_else(|| {
        LifecycleError::Invalid("installed source upgrade Plan nodes are invalid".to_owned())
    })? {
        if node.pointer("/binding/kind").and_then(Value::as_str) == Some("tool") {
            continue;
        }
        let node_id = node.get("id").and_then(Value::as_str).ok_or_else(|| {
            LifecycleError::Invalid("installed source upgrade Plan node id is invalid".to_owned())
        })?;
        results.insert(node_id.to_owned(), smoke_result(&plan, node, &context)?);
    }
    let ledger = execute_recorded_plan(
        &plan,
        &Value::Object(results),
        &context,
        Some(package_lock),
        None,
        false,
        Some("50ce000000000001"),
    )
    .map_err(|error| runtime_error(&error))?;
    if ledger.get("final_status").and_then(Value::as_str) != Some("completed") {
        return invalid("installed source upgrade smoke did not complete");
    }
    let evidence = ledger
        .get("evidence")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            LifecycleError::Invalid("installed source upgrade smoke evidence is invalid".to_owned())
        })?;
    let review_passed = evidence.iter().any(|item| {
        item.get("kind").and_then(Value::as_str) == Some("review")
            && item.get("status").and_then(Value::as_str) == Some("passed")
    });
    let report_completed = evidence.iter().any(|item| {
        item.get("node_id").and_then(Value::as_str) == Some("report")
            && item.get("kind").and_then(Value::as_str) == Some("delivery")
            && item.get("status").and_then(Value::as_str) == Some("completed")
    });
    if !review_passed || !report_completed {
        return invalid(
            "installed source upgrade smoke did not complete independent review and delivery reporting",
        );
    }
    Ok(json!({
        "final_status": "completed",
        "plan_fingerprint": plan.get("fingerprint").cloned().unwrap_or(Value::Null),
        "plan_status": "ready",
        "review_status": "passed",
        "status": "passed",
    }))
}

fn validate_skill_bindings(target: &Path, plan: &Value) -> Result<(), LifecycleError> {
    let target = open_root_directory(target, None, "installed source upgrade smoke target")?;
    let skills = open_child_directory(
        &target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "installed source upgrade Skills",
    )?;
    let nodes = plan.get("nodes").and_then(Value::as_array).ok_or_else(|| {
        LifecycleError::Invalid("installed source upgrade Plan nodes are invalid".to_owned())
    })?;
    let mut missing = Vec::new();
    for node in nodes {
        if node.pointer("/binding/kind").and_then(Value::as_str) != Some("skill") {
            continue;
        }
        let name = node
            .pointer("/binding/name")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid(
                    "installed source upgrade Skill binding name is invalid".to_owned(),
                )
            })?;
        let Ok(skill) = open_child_directory(
            &skills,
            name,
            Some(MANAGED_DIRECTORY_MODE),
            &format!("installed source upgrade Skill {name}"),
        ) else {
            missing.push(name.to_owned());
            continue;
        };
        if open_child_file(
            &skill,
            "SKILL.md",
            MANAGED_FILE_MODE,
            &format!("installed source upgrade Skill {name} entrypoint"),
        )
        .is_err()
        {
            missing.push(name.to_owned());
        }
    }
    missing.sort();
    missing.dedup();
    if !missing.is_empty() {
        return invalid(format!(
            "installed source upgrade smoke references missing Skills: {}",
            missing.join(", ")
        ));
    }
    Ok(())
}

fn smoke_result(plan: &Value, node: &Value, context: &Value) -> Result<Value, LifecycleError> {
    let node_id = node.get("id").and_then(Value::as_str).ok_or_else(|| {
        LifecycleError::Invalid("installed source upgrade Plan node id is invalid".to_owned())
    })?;
    let request = build_adapter_request(
        plan,
        node_id,
        context,
        &format!("source-upgrade-smoke-{node_id}-1"),
    )
    .map_err(|error| runtime_error(&error))?;
    let capability = node
        .get("capability")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid(
                "installed source upgrade Plan capability is invalid".to_owned(),
            )
        })?;
    let (kind, status, data) = if capability.starts_with("verification.") {
        let data = if capability
            .rsplit_once('.')
            .is_some_and(|(_, suffix)| suffix == "auto")
        {
            json!({
                "executed_validation": [{
                    "kind": "installed-contract-smoke",
                    "status": "passed",
                }],
                "level": "lint",
            })
        } else {
            json!({"level": "affected-tests", "tests": 1})
        };
        ("validation", "passed", data)
    } else if capability.starts_with("review.") {
        (
            "review",
            "passed",
            json!({
                "blocking_issues": [],
                "implementation_actor": IMPLEMENTATION_ACTOR,
                "reviewer_actor": REVIEWER_ACTOR,
            }),
        )
    } else if capability.starts_with("implementation.") {
        (
            "delivery",
            "completed",
            json!({"changed_files": ["Fixture.swift"]}),
        )
    } else if capability.starts_with("reporting.") || capability.starts_with("report.") {
        ("delivery", "completed", json!({"acceptance_matrix": []}))
    } else {
        (
            "diagnostic",
            "completed",
            json!({"checkpoint": "CP0", "scope": "apple"}),
        )
    };
    Ok(json!({
        "artifacts": [],
        "binding": request.get("binding").cloned().unwrap_or(Value::Null),
        "capability": request.get("capability").cloned().unwrap_or(Value::Null),
        "cleanup": [],
        "evidence": [{
            "artifact_ids": [],
            "data": data,
            "kind": kind,
            "status": status,
            "summary": format!("{kind} structured evidence"),
        }],
        "failure_attribution": {"category": "none", "summary": "no failure"},
        "invocation_id": request.get("invocation_id").cloned().unwrap_or(Value::Null),
        "node_id": request.get("node_id").cloned().unwrap_or(Value::Null),
        "plan_fingerprint": request.get("plan_fingerprint").cloned().unwrap_or(Value::Null),
        "provider": request.get("provider").cloned().unwrap_or(Value::Null),
        "request_id": request.get("request_id").cloned().unwrap_or(Value::Null),
        "schema_version": "1.0",
        "status": "completed",
    }))
}

fn runtime_error(error: &agent_runtime::RuntimeError) -> LifecycleError {
    LifecycleError::Invalid(format!(
        "installed source upgrade smoke runtime failed: {error}"
    ))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}
