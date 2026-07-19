#![cfg(not(windows))]

use agent_contracts::{canonical_json, canonical_sha256, load_json};
use agent_lifecycle::{
    compile_source_upgrade_bundle, resolve_source_install_selection, snapshot_source_packages,
};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TestRoot(PathBuf);

impl TestRoot {
    fn new() -> Self {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "agent-skills-lifecycle-upgrade-{}-{nonce}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed),
        ));
        std::fs::create_dir(&root).expect("create test root");
        Self(root)
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("workspace root")
        .to_path_buf()
}

fn evidence(package_lock: &Value) -> Value {
    let mut evidence = json!({
        "candidate_package_lock_hash": package_lock["fingerprint"],
        "command_results": [{
            "command": "compatibility-suite",
            "exit_code": 0,
            "stderr_sha256": "2".repeat(64),
            "stdout_sha256": "3".repeat(64),
        }],
        "environment": {"platform": "integration-test", "python": "3.11.0"},
        "manifest_count": 19,
        "negative_contract_count": 16,
        "runner_sha256": "4".repeat(64),
        "schema_inventory_hash": package_lock["schema_inventory"]["content_sha256"],
        "schema_version": "1.0",
        "status": "passed",
        "suite": "agent-skills-release-conformance-v1",
        "suite_definition_hash": "6".repeat(64),
        "test_count": 531,
    });
    let mut stable = evidence.as_object().expect("evidence object").clone();
    stable.insert(
        "command_results".to_owned(),
        json!([{"command": "compatibility-suite", "exit_code": 0}]),
    );
    evidence["attestation_key"] =
        Value::String(canonical_sha256(&Value::Object(stable)).expect("attestation"));
    evidence["fingerprint"] =
        Value::String(canonical_sha256(&evidence).expect("evidence fingerprint"));
    evidence
}

fn upgrade_command(
    binary: &Path,
    platform_root: &Path,
    target: &Path,
    evidence_path: &Path,
    schemas: &Path,
) -> Command {
    upgrade_command_named(
        binary,
        "upgrade",
        platform_root,
        target,
        evidence_path,
        schemas,
    )
}

fn upgrade_command_named(
    binary: &Path,
    command_name: &str,
    platform_root: &Path,
    target: &Path,
    evidence_path: &Path,
    schemas: &Path,
) -> Command {
    let mut command = Command::new(binary);
    command
        .arg(command_name)
        .arg(platform_root)
        .arg(target)
        .arg(evidence_path)
        .arg("--core-only")
        .arg("--schemas")
        .arg(schemas);
    command
}

fn add_plan_approvals(command: &mut Command, plan: &Value) {
    for approval in plan["approvals_required"]
        .as_array()
        .expect("Plan approvals")
    {
        command
            .arg("--approve")
            .arg(approval.as_str().expect("approval string"));
    }
}

struct ChangedUpgradeFixture {
    target: PathBuf,
    evidence_path: PathBuf,
    plan_path: PathBuf,
    binary: PathBuf,
    platform_root: PathBuf,
    schemas: PathBuf,
    current_lock: Value,
    candidate_lock: Value,
    plan: Value,
}

fn prepare_changed_upgrade(root: &TestRoot) -> ChangedUpgradeFixture {
    let repository = repository_root();
    let target = root.0.join("target");
    let evidence_path = root.0.join("evidence.json");
    let plan_path = root.0.join("upgrade-plan.json");
    let binary = PathBuf::from(env!("CARGO_BIN_EXE_agent-skills-rs"));
    let platform_root = repository.join("platforms");
    let schemas = repository.join("schemas");
    let install = Command::new(&binary)
        .arg("lifecycle-install")
        .arg(&platform_root)
        .arg(&target)
        .arg("--platform")
        .arg("desktop")
        .arg("--schemas")
        .arg(&schemas)
        .output()
        .expect("install desktop projection");
    assert!(
        install.status.success(),
        "install failed: {}",
        String::from_utf8_lossy(&install.stderr)
    );
    let current_lock =
        load_json(target.join(".agent-skills/agent-skills.lock")).expect("load current Lock");
    let selection =
        resolve_source_install_selection(&platform_root, &[], &[], &[], true).expect("core");
    let packages = snapshot_source_packages(&selection).expect("snapshot core");
    let candidate =
        compile_source_upgrade_bundle(&selection, &packages, &schemas, &target).expect("candidate");
    assert_eq!(
        candidate
            .package_lock()
            .pointer("/lineage/previous_lock_hash"),
        Some(&current_lock["fingerprint"])
    );
    let candidate_lock = candidate.package_lock().clone();
    std::fs::write(
        &evidence_path,
        canonical_json(&evidence(&candidate_lock)).expect("encode evidence"),
    )
    .expect("write evidence");
    let preview = upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas)
        .arg("--action")
        .arg("partial-uninstall")
        .arg("--removed-platform")
        .arg("desktop")
        .arg("--dry-run")
        .arg("--output")
        .arg(&plan_path)
        .output()
        .expect("preview partial uninstall");
    assert!(
        preview.status.success(),
        "preview failed: {}",
        String::from_utf8_lossy(&preview.stderr)
    );
    let plan: Value = serde_json::from_slice(&preview.stdout).expect("parse changed Plan");
    assert_eq!(plan["status"], "planned");
    ChangedUpgradeFixture {
        target,
        evidence_path,
        plan_path,
        binary,
        platform_root,
        schemas,
        current_lock,
        candidate_lock,
        plan,
    }
}

fn assert_cli_rollback_round_trip(
    binary: &Path,
    target: &Path,
    current_lock: &Value,
    candidate_lock: &Value,
) {
    let point =
        load_json(target.join(".agent-skills/rollback-point/rollback-point.json")).expect("point");
    let rejected = Command::new(binary)
        .arg("lifecycle-rollback")
        .arg(target)
        .arg("--approve-current-lock")
        .arg("0".repeat(64))
        .arg("--approve-rollback-point")
        .arg(point["fingerprint"].as_str().expect("point fingerprint"))
        .output()
        .expect("reject stale rollback approval");
    assert!(!rejected.status.success());
    assert_eq!(
        load_json(target.join(".agent-skills/agent-skills.lock"))
            .expect("candidate survives rejection"),
        *candidate_lock
    );

    let rollback = Command::new(binary)
        .arg("rollback")
        .arg(target)
        .arg("--approve-current-lock")
        .arg(
            candidate_lock["fingerprint"]
                .as_str()
                .expect("candidate Lock"),
        )
        .arg("--approve-rollback-point")
        .arg(point["fingerprint"].as_str().expect("point fingerprint"))
        .output()
        .expect("execute native rollback");
    assert!(
        rollback.status.success(),
        "rollback failed: {}",
        String::from_utf8_lossy(&rollback.stderr)
    );
    let rollback: Value = serde_json::from_slice(&rollback.stdout).expect("rollback result");
    assert_eq!(rollback["status"], "rolled-back");
    assert_eq!(rollback["restored_lock_hash"], current_lock["fingerprint"]);
    assert_eq!(
        load_json(target.join(".agent-skills/agent-skills.lock")).expect("restored Lock"),
        *current_lock
    );
    assert_eq!(
        rollback["rollback_point"]["package_lock_hash"],
        candidate_lock["fingerprint"]
    );
    assert!(!target.join(".agent-skills-lifecycle.lock").exists());
}

#[test]
fn lifecycle_upgrade_cli_requires_saved_plan_and_exact_fingerprint() {
    let root = TestRoot::new();
    let repository = repository_root();
    let target = root.0.join("target");
    let evidence_path = root.0.join("evidence.json");
    let plan_path = root.0.join("upgrade-plan.json");
    let binary = PathBuf::from(env!("CARGO_BIN_EXE_agent-skills-rs"));
    let platform_root = repository.join("platforms");
    let schemas = repository.join("schemas");

    let install = Command::new(&binary)
        .arg("lifecycle-install")
        .arg(&platform_root)
        .arg(&target)
        .arg("--core-only")
        .arg("--schemas")
        .arg(&schemas)
        .output()
        .expect("run native install");
    assert!(
        install.status.success(),
        "install failed: {}",
        String::from_utf8_lossy(&install.stderr)
    );
    let package_lock =
        load_json(target.join(".agent-skills/agent-skills.lock")).expect("load installed Lock");
    std::fs::write(
        &evidence_path,
        canonical_json(&evidence(&package_lock)).expect("encode evidence"),
    )
    .expect("write evidence");

    let preview = upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas)
        .arg("--dry-run")
        .arg("--output")
        .arg(&plan_path)
        .output()
        .expect("preview native upgrade");
    assert!(
        preview.status.success(),
        "preview failed: {}",
        String::from_utf8_lossy(&preview.stderr)
    );
    let plan: Value = serde_json::from_slice(&preview.stdout).expect("parse preview");
    assert_eq!(plan["status"], "no-change");
    assert_eq!(
        std::fs::read(&plan_path).expect("read saved Plan"),
        preview.stdout
    );

    let missing_approval = upgrade_command_named(
        &binary,
        "lifecycle-upgrade",
        &platform_root,
        &target,
        &evidence_path,
        &schemas,
    )
    .arg("--plan")
    .arg(&plan_path)
    .output()
    .expect("reject missing Plan fingerprint through compatibility alias");
    assert!(!missing_approval.status.success());
    assert!(
        String::from_utf8_lossy(&missing_approval.stderr)
            .contains("requires --plan and --approve-plan")
    );

    let wrong_approval =
        upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas)
            .arg("--plan")
            .arg(&plan_path)
            .arg("--approve-plan")
            .arg("f".repeat(64))
            .output()
            .expect("reject wrong Plan fingerprint");
    assert!(!wrong_approval.status.success());
    assert!(
        String::from_utf8_lossy(&wrong_approval.stderr)
            .contains("requires the exact planned fingerprint")
    );

    let applied = upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas)
        .arg("--plan")
        .arg(&plan_path)
        .arg("--approve-plan")
        .arg(plan["fingerprint"].as_str().expect("Plan fingerprint"))
        .output()
        .expect("apply native no-change upgrade");
    assert!(
        applied.status.success(),
        "apply failed: {}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let result: Value = serde_json::from_slice(&applied.stdout).expect("parse apply result");
    assert_eq!(result["status"], "no-change");
    assert_eq!(
        load_json(target.join(".agent-skills/agent-skills.lock"))
            .expect("load unchanged package Lock"),
        package_lock
    );
    assert!(!target.join(".agent-skills-lifecycle.lock").exists());
}

#[test]
fn lifecycle_upgrade_cli_applies_changed_partial_uninstall_and_rejects_drift() {
    let root = TestRoot::new();
    let fixture = prepare_changed_upgrade(&root);
    let ChangedUpgradeFixture {
        target,
        evidence_path,
        plan_path,
        binary,
        platform_root,
        schemas,
        current_lock,
        candidate_lock,
        plan,
    } = fixture;

    let install_lock_path = target.join(".agent-skills/install-lock.json");
    let install_lock_bytes = std::fs::read(&install_lock_path).expect("read Install Lock");
    let mut drifted_install_lock: Value =
        serde_json::from_slice(&install_lock_bytes).expect("parse Install Lock");
    drifted_install_lock["status"] = Value::String("planned".to_owned());
    std::fs::write(
        &install_lock_path,
        canonical_json(&drifted_install_lock).expect("encode drifted Install Lock"),
    )
    .expect("write drifted Install Lock");
    let mut drifted = upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas);
    drifted
        .arg("--action")
        .arg("partial-uninstall")
        .arg("--removed-platform")
        .arg("desktop")
        .arg("--plan")
        .arg(&plan_path)
        .arg("--approve-plan")
        .arg(plan["fingerprint"].as_str().expect("Plan fingerprint"));
    add_plan_approvals(&mut drifted, &plan);
    let drifted = drifted.output().expect("reject changed current Lock");
    assert!(!drifted.status.success());
    std::fs::write(&install_lock_path, install_lock_bytes).expect("restore Install Lock");

    let omitted_removal =
        upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas)
            .arg("--action")
            .arg("partial-uninstall")
            .arg("--plan")
            .arg(&plan_path)
            .arg("--approve-plan")
            .arg(plan["fingerprint"].as_str().expect("Plan fingerprint"))
            .output()
            .expect("reject omitted removal");
    assert!(!omitted_removal.status.success());
    assert_eq!(
        load_json(target.join(".agent-skills/agent-skills.lock"))
            .expect("current Lock survives rejection"),
        current_lock
    );

    let mut apply = upgrade_command(&binary, &platform_root, &target, &evidence_path, &schemas);
    apply
        .arg("--action")
        .arg("partial-uninstall")
        .arg("--removed-platform")
        .arg("desktop")
        .arg("--plan")
        .arg(&plan_path)
        .arg("--approve-plan")
        .arg(plan["fingerprint"].as_str().expect("Plan fingerprint"));
    add_plan_approvals(&mut apply, &plan);
    let applied = apply.output().expect("apply partial uninstall");
    assert!(
        applied.status.success(),
        "apply failed: {}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let result: Value = serde_json::from_slice(&applied.stdout).expect("parse apply result");
    assert_eq!(result["status"], "partially-uninstalled");
    assert_eq!(
        load_json(target.join(".agent-skills/agent-skills.lock")).expect("load upgraded Lock"),
        candidate_lock
    );
    assert!(
        target
            .join(".agent-skills/rollback-point/rollback-point.json")
            .is_file()
    );
    assert!(!target.join(".agent-skills-lifecycle.lock").exists());

    assert_cli_rollback_round_trip(&binary, &target, &current_lock, &candidate_lock);
}
