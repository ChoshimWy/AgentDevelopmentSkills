//! Parallel native compatibility entry point.

use agent_contracts::{
    MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256, load_json, require_schema_version,
};
use agent_engine::{
    DiscoveryEngine, compile_plan_with_package_lock, diff_package_locks, explain_package_lock,
    resolve_package_lock, resolve_policy, validate_compiled_plan, validate_package_lock,
    validate_plan_package_lock, validate_upgrade_conformance_evidence, validate_upgrade_plan,
};
use agent_lifecycle::{
    LifecycleError, LifecycleWorkspace, compile_source_install_bundle,
    compile_source_upgrade_bundle, compile_upgrade_plan, inspect_doctor_baseline,
    inspect_doctor_report_v1, inspect_source_install, inspect_source_install_with_activation,
    inspect_source_platform_options, inspect_source_upgrade, inspect_uninstall_plan,
    inspect_upgrade_planning_snapshot, install_source_bundle,
    install_source_bundle_with_activation, render_codex_config, resolve_source_install_selection,
    rollback_source_install, snapshot_source_packages, upgrade_source_bundle,
};
use agent_registry::{CORE_VERSION, ManifestRegistry, automatic_recipe_capabilities};
use agent_runtime::{
    attach_adapter_result, build_adapter_request, claim_provider_invocation,
    collect_submitted_results, compile_session_manifest_selection, create_session_worktree,
    evaluate_session_gate, execute_fake_plan, execute_recorded_plan, freeze_checkpoint,
    inspect_provider_invocation, inspect_repository, load_claim_token_file, new_session_context,
    prepare_provider_invocation, refresh_session_source_identity, registry_assert_available,
    registry_attach_and_gate, registry_checkpoint, registry_create, registry_create_active,
    registry_list, registry_load, registry_transition, registry_write,
    remove_created_session_worktree, repository_patch, session_source_identity,
    submit_provider_invocation, transition_session_context, validate_adapter_request,
    validate_adapter_result, validate_worktree_session_context, worktree_status,
};
use clap::{Parser, Subcommand};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

const CLI_WORKER_STACK_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug, Parser)]
#[command(
    name = "agent-skills-rs",
    version,
    about = "Native AgentDevelopmentSkills compatibility CLI"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Parser)]
#[command(
    name = "agent-session",
    version,
    about = "Native Worktree Session lifecycle CLI"
)]
struct AgentSessionCli {
    #[command(subcommand)]
    command: AgentSessionCommand,
}

#[derive(Debug, Subcommand)]
enum AgentSessionCommand {
    /// Create an isolated Worktree from a stable Commit.
    Create {
        name: String,
        #[arg(long, default_value = ".")]
        repository: PathBuf,
        #[arg(long)]
        project_id: String,
        #[arg(long)]
        session_id: Option<String>,
        #[arg(long)]
        base: Option<String>,
        #[arg(long)]
        base_source: Option<String>,
        #[arg(long)]
        branch: Option<String>,
        #[arg(long)]
        worktree_root: Option<PathBuf>,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long)]
        platform_manifest_root: Option<PathBuf>,
    },
    /// List registered Worktree Sessions.
    List {
        #[arg(long, default_value = ".")]
        repository: PathBuf,
    },
    /// Read or refresh one Worktree Session.
    Inspect {
        session_id: String,
        #[arg(long, default_value = ".")]
        repository: PathBuf,
        #[arg(long)]
        refresh: bool,
    },
    /// Refresh source identity without writing Registry state.
    Fingerprint {
        session_id: String,
        #[arg(long, default_value = ".")]
        repository: PathBuf,
    },
    /// Freeze existing clean HEAD Commits without staging or committing.
    Checkpoint {
        session_id: String,
        #[arg(long, default_value = ".")]
        repository: PathBuf,
    },
    /// Validate Adapter/Ledger evidence against a committed Session.
    Gate {
        session_id: String,
        #[arg(long, default_value = ".")]
        repository: PathBuf,
        #[arg(long = "pair", required = true)]
        pairs: Vec<PathBuf>,
        #[arg(long)]
        ledger: PathBuf,
        #[arg(long)]
        artifact_root: PathBuf,
    },
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Execute the fresh-only production-shaped native source installer.
    Install {
        #[arg(long)]
        source_root: PathBuf,
        #[arg(long)]
        target_root: PathBuf,
        #[arg(long = "platform", required = true)]
        platforms: Vec<String>,
        #[arg(long = "discipline")]
        disciplines: Vec<String>,
        #[arg(long = "runtime-config")]
        runtime_configs: Vec<String>,
        #[arg(long)]
        session_launcher: Option<PathBuf>,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        json: bool,
    },
    /// Emit the canonical JSON representation of an existing JSON artifact.
    Canonicalize { artifact: PathBuf },
    /// Emit the canonical SHA-256 identity of an existing JSON artifact.
    Hash { artifact: PathBuf },
    /// Apply the common version boundary used before typed validation.
    ValidateVersion {
        artifact: PathBuf,
        #[arg(long, default_value = "1.0")]
        expected: String,
    },
    /// Validate and snapshot a manifest registry without executing package code.
    RegistrySnapshot {
        root: PathBuf,
        #[arg(long, default_value = CORE_VERSION)]
        core_version: String,
        #[arg(long = "disable-provider")]
        disabled_providers: Vec<String>,
        #[arg(long = "provider-root")]
        provider_roots: Vec<PathBuf>,
    },
    /// Resolve one capability through the native manifest registry.
    RegistryResolve {
        root: PathBuf,
        capability: String,
        #[arg(long)]
        platform: Option<String>,
        #[arg(long, default_value = CORE_VERSION)]
        core_version: String,
        #[arg(long = "disable-provider")]
        disabled_providers: Vec<String>,
        #[arg(long = "provider-root")]
        provider_roots: Vec<PathBuf>,
    },
    /// Emit the native source-package selection compatibility projection.
    InstallSelection {
        root: PathBuf,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long = "discipline")]
        disciplines: Vec<String>,
        #[arg(long = "runtime-config")]
        runtime_configs: Vec<String>,
        #[arg(long)]
        core_only: bool,
    },
    /// Freeze selected package assets into the native source-snapshot projection.
    InstallSourceSnapshot {
        root: PathBuf,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long = "discipline")]
        disciplines: Vec<String>,
        #[arg(long = "runtime-config")]
        runtime_configs: Vec<String>,
        #[arg(long)]
        core_only: bool,
    },
    /// Compile source packages into Install Plan v2 and package Lockfile contracts.
    InstallBundle {
        root: PathBuf,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long = "discipline")]
        disciplines: Vec<String>,
        #[arg(long = "runtime-config")]
        runtime_configs: Vec<String>,
        #[arg(long)]
        core_only: bool,
        #[arg(long, default_value = "schemas")]
        schemas: PathBuf,
        #[arg(long)]
        previous: Option<PathBuf>,
    },
    /// Execute the fresh-only native source-install compatibility lifecycle.
    LifecycleInstall {
        root: PathBuf,
        target_root: PathBuf,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long = "discipline")]
        disciplines: Vec<String>,
        #[arg(long = "runtime-config")]
        runtime_configs: Vec<String>,
        #[arg(long)]
        core_only: bool,
        #[arg(long, default_value = "schemas")]
        schemas: PathBuf,
        #[arg(long)]
        dry_run: bool,
    },
    /// Plan or execute one approval-bound native source upgrade transaction.
    LifecycleUpgrade {
        root: PathBuf,
        target_root: PathBuf,
        evidence: PathBuf,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long = "discipline")]
        disciplines: Vec<String>,
        #[arg(long = "runtime-config")]
        runtime_configs: Vec<String>,
        #[arg(long)]
        core_only: bool,
        #[arg(long, default_value = "schemas")]
        schemas: PathBuf,
        #[arg(long, default_value = "upgrade")]
        action: String,
        #[arg(long = "removed-platform")]
        removed_platforms: Vec<String>,
        #[arg(long = "removed-runtime-config")]
        removed_runtime_configs: Vec<String>,
        #[arg(long)]
        session_launcher: Option<PathBuf>,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        output: Option<PathBuf>,
        #[arg(long)]
        plan: Option<PathBuf>,
        #[arg(long)]
        approve_plan: Option<String>,
        #[arg(long = "approve")]
        approvals: Vec<String>,
    },
    /// Execute one exact-approval native persistent rollback transaction.
    LifecycleRollback {
        target_root: PathBuf,
        #[arg(long)]
        approve_current_lock: String,
        #[arg(long)]
        approve_rollback_point: String,
    },
    /// Emit the sorted automatic recipe capability closure for target platforms.
    RecipeCapabilities { targets: Vec<String> },
    /// Resolve task policy from an existing project-profile artifact.
    PolicyResolve {
        profile: PathBuf,
        task: String,
        #[arg(long = "explicit-platform")]
        explicit_platforms: Vec<String>,
        #[arg(long)]
        constraints: Option<PathBuf>,
        #[arg(long = "policy-layers")]
        policy_layers: Option<PathBuf>,
    },
    /// Discover repository platforms through the read-only native engine.
    RepositoryDiscover {
        repository: PathBuf,
        #[arg(long, default_value = "platforms")]
        manifests: PathBuf,
        #[arg(long = "target-file")]
        target_files: Vec<String>,
        #[arg(long = "changed-file")]
        changed_files: Vec<String>,
        #[arg(long)]
        cwd: Option<PathBuf>,
        #[arg(long, default_value = CORE_VERSION)]
        core_version: String,
        #[arg(long = "disable-provider")]
        disabled_providers: Vec<String>,
        #[arg(long = "provider-root")]
        provider_roots: Vec<PathBuf>,
    },
    /// Compile a deterministic workflow plan through the native engine.
    PlanCompile {
        profile: PathBuf,
        policy: PathBuf,
        #[arg(long, default_value = "platforms")]
        manifests: PathBuf,
        #[arg(long, default_value = CORE_VERSION)]
        core_version: String,
        #[arg(long = "disable-provider")]
        disabled_providers: Vec<String>,
        #[arg(long = "provider-root")]
        provider_roots: Vec<PathBuf>,
        #[arg(long)]
        lock: Option<PathBuf>,
    },
    /// Resolve an Install Plan v2 into a persistent package Lockfile.
    LockResolve {
        install_plan: PathBuf,
        #[arg(long, default_value = "schemas")]
        schemas: PathBuf,
        #[arg(long)]
        previous: Option<PathBuf>,
        #[arg(long = "source")]
        sources: Vec<String>,
        #[arg(long = "source-base", default_value = ".")]
        source_base: PathBuf,
        #[arg(long = "source-sha256")]
        source_hashes: Vec<String>,
        #[arg(long)]
        output: Option<PathBuf>,
    },
    /// Validate a persistent package Lockfile.
    LockValidate { lockfile: PathBuf },
    /// Diff two persistent package Lockfiles.
    LockDiff { before: PathBuf, after: PathBuf },
    /// Explain one persistent package Lockfile.
    LockExplain { lockfile: PathBuf },
    /// Validate one approval-bound Upgrade Conformance Evidence v1 artifact.
    UpgradeEvidenceValidate { evidence: PathBuf },
    /// Inspect an installed target and compile one approval-bound Upgrade Plan v1.
    UpgradePlanBuild {
        #[arg(long)]
        candidate_install_plan: PathBuf,
        #[arg(long)]
        candidate_package_lock: PathBuf,
        #[arg(long)]
        evidence: PathBuf,
        #[arg(long)]
        target_root: PathBuf,
        #[arg(long, default_value = "upgrade")]
        action: String,
        #[arg(long)]
        session_launcher: Option<PathBuf>,
        #[arg(long = "removed-platform")]
        removed_platforms: Vec<String>,
        #[arg(long = "removed-runtime-config")]
        removed_runtime_configs: Vec<String>,
    },
    /// Validate one approval-bound Upgrade Plan v1 artifact.
    UpgradePlanValidate { plan: PathBuf },
    /// Inspect the read-only native Doctor baseline compatibility boundary.
    DoctorBaseline {
        target_root: PathBuf,
        #[arg(long, default_value = "schemas")]
        schemas: PathBuf,
    },
    /// Emit a complete Doctor Report v1 using an explicit host Python attestation.
    DoctorReport {
        target_root: PathBuf,
        #[arg(long, default_value = "schemas")]
        schemas: PathBuf,
        #[arg(long)]
        python_version: String,
    },
    /// Render the native Codex shared-config compatibility projection.
    CodexConfigRender {
        shared_config: PathBuf,
        agents_path: PathBuf,
        #[arg(long)]
        existing_config: Option<PathBuf>,
    },
    /// Execute the guarded native full-uninstall compatibility path.
    #[command(name = "uninstall", visible_alias = "lifecycle-uninstall")]
    LifecycleUninstall {
        target_root: PathBuf,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        json: bool,
    },
    /// Execute a deterministic native fake-adapter workflow runtime.
    RuntimeExecute {
        plan: PathBuf,
        #[arg(long)]
        behaviors: Option<PathBuf>,
        #[arg(long)]
        approvals: Option<PathBuf>,
        #[arg(long)]
        lock: Option<PathBuf>,
        #[arg(long)]
        ledger: Option<PathBuf>,
        #[arg(long)]
        resume: bool,
        #[arg(long)]
        identity_seed: Option<String>,
    },
    /// Freeze one workflow node into an Adapter Request v1.
    AdapterRequestBuild {
        plan: PathBuf,
        node_id: String,
        context: PathBuf,
        invocation_id: String,
        #[arg(long)]
        lock: Option<PathBuf>,
    },
    /// Validate one frozen Adapter Request v1.
    AdapterRequestValidate { request: PathBuf },
    /// Validate one Adapter Result v1 against its frozen request.
    AdapterResultValidate { request: PathBuf, result: PathBuf },
    /// Publish one frozen Provider Invocation v1 without executing its binding.
    InvocationPrepare {
        root: PathBuf,
        plan: PathBuf,
        node_id: String,
        context: PathBuf,
        invocation_id: String,
        #[arg(long)]
        lock: Option<PathBuf>,
    },
    /// Grant the only time-bounded claim for one Provider invocation.
    InvocationClaim {
        root: PathBuf,
        request_id: String,
        actor_id: String,
        claim_token_file: PathBuf,
    },
    /// Validate and publish one Adapter Result for a live claim.
    InvocationSubmit {
        root: PathBuf,
        request_id: String,
        result: PathBuf,
        claim_token_file: PathBuf,
    },
    /// Inspect one Provider invocation and derive its current state.
    InvocationInspect { root: PathBuf, request_id: String },
    /// Consume recorded Adapter Result v1 objects without invoking Providers.
    RuntimeExecuteRecorded {
        plan: PathBuf,
        results: PathBuf,
        context: PathBuf,
        #[arg(long)]
        lock: Option<PathBuf>,
        #[arg(long)]
        ledger: Option<PathBuf>,
        #[arg(long)]
        resume: bool,
        #[arg(long)]
        identity_seed: Option<String>,
    },
    /// Consume submitted Provider handoffs through the recorded Adapter runtime.
    RuntimeExecuteInvocations {
        plan: PathBuf,
        root: PathBuf,
        context: PathBuf,
        #[arg(long)]
        selection: PathBuf,
        #[arg(long)]
        lock: Option<PathBuf>,
        #[arg(long)]
        ledger: Option<PathBuf>,
        #[arg(long)]
        resume: bool,
        #[arg(long)]
        identity_seed: Option<String>,
    },
    /// Inspect staged, unstaged, and untracked Git Worktree state.
    WorktreeStatus { repository: PathBuf },
    /// Compute one deterministic repository-patch-v1 identity.
    RepositoryPatch {
        repository: PathBuf,
        repository_id: String,
        base_commit: String,
        #[arg(long)]
        checkpoint_commit: Option<String>,
    },
    /// Inspect one repository at a frozen base ref.
    RepositoryInspect {
        repository: PathBuf,
        repository_id: String,
        #[arg(long, default_value = "primary")]
        role: String,
        #[arg(long, default_value = "HEAD")]
        base_ref: String,
        #[arg(long, default_value = "explicit")]
        base_source: String,
        #[arg(long)]
        committed: bool,
    },
    /// Derive session-source-v1 from a repository array.
    SessionSourceIdentity {
        repositories: PathBuf,
        #[arg(long, default_value = "working")]
        mode: String,
    },
    /// Create one isolated Worktree and branch from a frozen Commit.
    SessionWorktreeCreate {
        repository: PathBuf,
        name: String,
        #[arg(long, default_value = "primary")]
        repository_id: String,
        #[arg(long)]
        base_ref: Option<String>,
        #[arg(long)]
        base_source: Option<String>,
        #[arg(long)]
        worktree_root: Option<PathBuf>,
        #[arg(long)]
        branch: Option<String>,
    },
    /// Remove one unchanged Worktree/branch pair created by this workflow.
    SessionWorktreeRemove {
        source_repository: PathBuf,
        repository_record: PathBuf,
    },
    /// Create one Session with exact pre-registration Worktree compensation.
    SessionCreate {
        repository: PathBuf,
        name: String,
        context_input: PathBuf,
        #[arg(long)]
        base_ref: Option<String>,
        #[arg(long)]
        base_source: Option<String>,
        #[arg(long)]
        worktree_root: Option<PathBuf>,
        #[arg(long)]
        branch: Option<String>,
    },
    /// Create one Session from a trusted Manifest-driven Provider closure.
    SessionCreateManifest {
        repository: PathBuf,
        name: String,
        #[arg(long)]
        project_id: String,
        #[arg(long)]
        session_id: Option<String>,
        #[arg(long)]
        created_at: String,
        #[arg(long = "platform")]
        platforms: Vec<String>,
        #[arg(long)]
        manifest_root: Option<PathBuf>,
        #[arg(long)]
        base_ref: Option<String>,
        #[arg(long)]
        base_source: Option<String>,
        #[arg(long)]
        worktree_root: Option<PathBuf>,
        #[arg(long)]
        branch: Option<String>,
    },
    /// Validate one Worktree Session Context v1.
    SessionContextValidate { context: PathBuf },
    /// Create one Session Context from an explicit deterministic envelope.
    SessionContextCreate { input: PathBuf },
    /// Refresh one Session Context from live repository state.
    SessionContextRefresh { context: PathBuf },
    /// Freeze one active Session Context at clean repository checkpoints.
    SessionContextFreeze { context: PathBuf },
    /// Apply one legal non-gated Session lifecycle transition.
    SessionContextTransition { context: PathBuf, target: String },
    /// Create one persistent Worktree Session Registry entry.
    SessionRegistryCreate {
        repository: PathBuf,
        context: PathBuf,
    },
    /// Load one persistent Worktree Session Registry entry.
    SessionRegistryLoad {
        repository: PathBuf,
        session_id: String,
    },
    /// List persistent Worktree Session Registry entries.
    SessionRegistryList { repository: PathBuf },
    /// Replace one persistent Worktree Session Registry entry.
    SessionRegistryWrite {
        repository: PathBuf,
        context: PathBuf,
    },
    /// Apply one persistent non-gated Session lifecycle transition.
    SessionRegistryTransition {
        repository: PathBuf,
        session_id: String,
        target: String,
    },
    /// Freeze one active Registry Session at existing clean HEAD Commits.
    SessionRegistryCheckpoint {
        repository: PathBuf,
        session_id: String,
    },
    /// Attach Adapter pairs, evaluate Final Gate, and persist passed state.
    SessionRegistryGate {
        repository: PathBuf,
        session_id: String,
        adapter_pairs: PathBuf,
        ledger: PathBuf,
        artifact_root: PathBuf,
    },
    /// Attach one Adapter Result reference to a Session Context.
    SessionGateAttach {
        context: PathBuf,
        attempt_id: String,
        request: PathBuf,
        result: PathBuf,
    },
    /// Evaluate one Final Gate without writing Registry state.
    SessionGateEvaluate {
        context: PathBuf,
        adapter_pairs: PathBuf,
        ledger: PathBuf,
        artifact_root: PathBuf,
    },
}

fn invoked_as_agent_session() -> bool {
    std::env::args_os()
        .next()
        .as_deref()
        .and_then(|executable| Path::new(executable).file_stem())
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case("agent-session"))
}

fn installed_manifest_root() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    let target = executable.parent()?.parent()?;
    let root = target.join(".agent-skills/packages");
    let metadata = std::fs::symlink_metadata(&root).ok()?;
    (!metadata.file_type().is_symlink() && metadata.is_dir()).then_some(root)
}

fn utc_timestamp() -> Result<String, Box<dyn std::error::Error>> {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs();
    let days = i64::try_from(seconds / 86_400)?;
    let day_seconds = seconds % 86_400;
    let hour = day_seconds / 3_600;
    let minute = day_seconds % 3_600 / 60;
    let second = day_seconds % 60;

    // Proleptic Gregorian conversion adapted from the public-domain
    // civil_from_days algorithm by Howard Hinnant.
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    if month <= 2 {
        year += 1;
    }
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}

fn run_agent_session() -> Result<i32, Box<dyn std::error::Error>> {
    match run_agent_session_inner(AgentSessionCli::parse()) {
        Ok(value) => {
            std::io::stdout().write_all(&canonical_json(&value)?)?;
            Ok(0)
        }
        Err(error) => {
            std::io::stderr().write_all(&canonical_json(&json!({
                "error": error.to_string(),
                "schema_version": "1.0",
                "status": "blocked",
            }))?)?;
            Ok(2)
        }
    }
}

#[allow(clippy::too_many_lines)]
fn run_agent_session_inner(cli: AgentSessionCli) -> Result<Value, Box<dyn std::error::Error>> {
    match cli.command {
        AgentSessionCommand::Create {
            name,
            repository,
            project_id,
            session_id,
            base,
            base_source,
            branch,
            worktree_root,
            platforms,
            platform_manifest_root,
        } => {
            let manifest_root = platform_manifest_root.or_else(installed_manifest_root);
            let selection =
                compile_session_manifest_selection(manifest_root.as_deref(), &platforms)?;
            let session_id = session_id.unwrap_or_else(|| name.clone());
            registry_assert_available(&repository, &session_id)?;
            let (record, notice) = create_session_worktree(
                &repository,
                &name,
                "primary",
                base.as_deref(),
                base_source.as_deref(),
                worktree_root.as_deref(),
                branch.as_deref(),
            )?;
            let registration = (|| -> Result<Value, agent_runtime::RuntimeError> {
                let input = json!({
                    "capability_closure": selection["capability_closure"],
                    "created_at": utc_timestamp().map_err(|error| {
                        agent_runtime::RuntimeError::Contract(error.to_string())
                    })?,
                    "dependencies": [],
                    "platform_contexts": selection["platform_contexts"],
                    "project_id": project_id,
                    "repositories": [record.clone()],
                    "selected_platforms": selection["selected_platforms"],
                    "session_id": session_id,
                });
                let context = new_session_context(&input)?;
                registry_create_active(&repository, &context)
            })();
            if let Err(error) = registration {
                if let Err(cleanup_error) = remove_created_session_worktree(&record, &repository) {
                    return Err(format!(
                        "session registration failed ({error}); exact Worktree compensation was blocked ({cleanup_error})"
                    )
                    .into());
                }
                return Err(error.into());
            }
            Ok(json!({
                "notice": notice,
                "operation": "create",
                "schema_version": "1.0",
                "session": registration?,
            }))
        }
        AgentSessionCommand::List { repository } => Ok(json!({
            "schema_version": "1.0",
            "sessions": registry_list(&repository)?,
        })),
        AgentSessionCommand::Inspect {
            session_id,
            repository,
            refresh,
        } => {
            let mut context = registry_load(&repository, &session_id)?;
            if refresh {
                refresh_session_source_identity(&mut context)?;
                validate_worktree_session_context(&context)?;
            }
            Ok(context)
        }
        AgentSessionCommand::Fingerprint {
            session_id,
            repository,
        } => {
            let mut context = registry_load(&repository, &session_id)?;
            refresh_session_source_identity(&mut context)?;
            validate_worktree_session_context(&context)?;
            Ok(context)
        }
        AgentSessionCommand::Checkpoint {
            session_id,
            repository,
        } => Ok(json!({
            "notice": {"commits_created": false, "staging_changed": false},
            "operation": "checkpoint",
            "schema_version": "1.0",
            "session": registry_checkpoint(&repository, &session_id)?,
        })),
        AgentSessionCommand::Gate {
            session_id,
            repository,
            pairs,
            ledger,
            artifact_root,
        } => {
            let pairs = pairs
                .into_iter()
                .map(load_json)
                .collect::<Result<Vec<_>, _>>()?;
            let ledger = load_json(ledger)?;
            Ok(registry_attach_and_gate(
                &repository,
                &session_id,
                &Value::Array(pairs),
                &ledger,
                &artifact_root,
            )?)
        }
    }
}

#[allow(clippy::too_many_lines)]
fn run() -> Result<i32, Box<dyn std::error::Error>> {
    if invoked_as_agent_session() {
        return run_agent_session();
    }
    match Cli::parse().command {
        Command::Install {
            source_root,
            target_root,
            platforms,
            disciplines,
            mut runtime_configs,
            session_launcher,
            dry_run,
            json,
        } => {
            if platforms.iter().any(|platform| platform == "apple")
                && !runtime_configs.iter().any(|runtime| runtime == "codex")
            {
                runtime_configs.push("codex".to_owned());
            }
            runtime_configs.sort();
            runtime_configs.dedup();
            let platform_root = source_root.join("platforms");
            let platform_options = inspect_source_platform_options(&platform_root)?;
            let selection = resolve_source_install_selection(
                &platform_root,
                &platforms,
                &disciplines,
                &runtime_configs,
                false,
            )?;
            let packages = snapshot_source_packages(&selection)?;
            let bundle = compile_source_install_bundle(
                &selection,
                &packages,
                source_root.join("schemas"),
                None,
            )?;
            let launcher = if platforms.iter().any(|platform| platform == "apple") {
                let path = session_launcher
                    .as_deref()
                    .ok_or("native Apple source install requires --session-launcher")?;
                Some(read_frozen_executable(path)?)
            } else {
                None
            };
            if inspect_source_platform_options(&platform_root)? != platform_options {
                return Err("source platform inventory changed while preparing install".into());
            }
            let outcome = if dry_run {
                inspect_source_install_with_activation(
                    &bundle,
                    &packages,
                    &target_root,
                    launcher.as_deref().unwrap_or_default(),
                )?
            } else {
                install_source_bundle_with_activation(
                    &bundle,
                    &packages,
                    &target_root,
                    launcher.as_deref().unwrap_or_default(),
                )?
            };
            if dry_run && inspect_source_platform_options(&platform_root)? != platform_options {
                return Err("source platform inventory changed while previewing install".into());
            }
            let report = native_install_report(&outcome, &platform_options, dry_run)?;
            if json {
                std::io::stdout().write_all(&canonical_json(&report)?)?;
            } else {
                print!("{}", native_install_human_report(&report)?);
            }
        }
        Command::Canonicalize { artifact } => {
            let value = load_json(artifact)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::Hash { artifact } => {
            let value = load_json(artifact)?;
            println!("{}", canonical_sha256(&value)?);
        }
        Command::ValidateVersion { artifact, expected } => {
            let value = load_json(artifact)?;
            require_schema_version(&value, &expected)?;
            println!("{}", canonical_sha256(&value)?);
        }
        Command::RegistrySnapshot {
            root,
            core_version,
            disabled_providers,
            provider_roots,
        } => {
            let disabled = disabled_providers.into_iter().collect::<BTreeSet<_>>();
            let registry = ManifestRegistry::from_directory_with_provider_roots(
                root,
                &provider_roots,
                &disabled,
                &core_version,
            )?;
            print!(
                "{}",
                String::from_utf8(canonical_json(&registry.snapshot()?)?)?
            );
        }
        Command::RegistryResolve {
            root,
            capability,
            platform,
            core_version,
            disabled_providers,
            provider_roots,
        } => {
            let disabled = disabled_providers.into_iter().collect::<BTreeSet<_>>();
            let registry = ManifestRegistry::from_directory_with_provider_roots(
                root,
                &provider_roots,
                &disabled,
                &core_version,
            )?;
            let resolved = registry.resolve_binding(&capability, platform.as_deref())?;
            let value = serde_json::to_value(resolved)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::InstallSelection {
            root,
            platforms,
            disciplines,
            runtime_configs,
            core_only,
        } => {
            let selection = resolve_source_install_selection(
                root,
                &platforms,
                &disciplines,
                &runtime_configs,
                core_only,
            )?;
            print!(
                "{}",
                String::from_utf8(canonical_json(&selection.compatibility_projection())?)?
            );
        }
        Command::InstallSourceSnapshot {
            root,
            platforms,
            disciplines,
            runtime_configs,
            core_only,
        } => {
            let selection = resolve_source_install_selection(
                root,
                &platforms,
                &disciplines,
                &runtime_configs,
                core_only,
            )?;
            let packages = snapshot_source_packages(&selection)?;
            print!(
                "{}",
                String::from_utf8(canonical_json(&packages.compatibility_projection())?)?
            );
        }
        Command::InstallBundle {
            root,
            platforms,
            disciplines,
            runtime_configs,
            core_only,
            schemas,
            previous,
        } => {
            let selection = resolve_source_install_selection(
                root,
                &platforms,
                &disciplines,
                &runtime_configs,
                core_only,
            )?;
            let packages = snapshot_source_packages(&selection)?;
            let previous = previous.map(load_json).transpose()?;
            let bundle =
                compile_source_install_bundle(&selection, &packages, schemas, previous.as_ref())?;
            print!(
                "{}",
                String::from_utf8(canonical_json(&bundle.compatibility_projection())?)?
            );
        }
        Command::LifecycleInstall {
            root,
            target_root,
            platforms,
            disciplines,
            runtime_configs,
            core_only,
            schemas,
            dry_run,
        } => {
            let selection = resolve_source_install_selection(
                root,
                &platforms,
                &disciplines,
                &runtime_configs,
                core_only,
            )?;
            let packages = snapshot_source_packages(&selection)?;
            let bundle = compile_source_install_bundle(&selection, &packages, schemas, None)?;
            let result = if dry_run {
                inspect_source_install(&bundle, &packages, target_root)?
            } else {
                install_source_bundle(&bundle, &packages, target_root)?
            };
            print!("{}", String::from_utf8(canonical_json(&result)?)?);
        }
        Command::LifecycleUpgrade {
            root,
            target_root,
            evidence,
            platforms,
            disciplines,
            runtime_configs,
            core_only,
            schemas,
            action,
            removed_platforms,
            removed_runtime_configs,
            session_launcher,
            dry_run,
            output,
            plan,
            approve_plan,
            approvals,
        } => {
            let selection = resolve_source_install_selection(
                root,
                &platforms,
                &disciplines,
                &runtime_configs,
                core_only,
            )?;
            let packages = snapshot_source_packages(&selection)?;
            let bundle =
                compile_source_upgrade_bundle(&selection, &packages, schemas, &target_root)?;
            let evidence = load_json(evidence)?;
            let session_launcher = session_launcher
                .as_deref()
                .map(read_frozen_executable)
                .transpose()?;
            let generated = inspect_source_upgrade(
                &bundle,
                &packages,
                &target_root,
                &evidence,
                &action,
                &removed_platforms,
                &removed_runtime_configs,
                session_launcher.as_deref(),
            )?;
            if dry_run {
                if plan.is_some() || approve_plan.is_some() || !approvals.is_empty() {
                    return Err("lifecycle-upgrade --dry-run does not accept approvals".into());
                }
                let encoded = canonical_json(&generated)?;
                if let Some(output) = output {
                    std::fs::write(output, &encoded)?;
                }
                print!("{}", String::from_utf8(encoded)?);
            } else {
                if output.is_some() {
                    return Err("lifecycle-upgrade apply does not accept --output".into());
                }
                let approved = load_json(
                    plan.ok_or("lifecycle-upgrade apply requires --plan and --approve-plan")?,
                )?;
                let approve_plan = approve_plan
                    .ok_or("lifecycle-upgrade apply requires --plan and --approve-plan")?;
                if approved.get("fingerprint").and_then(Value::as_str)
                    != Some(approve_plan.as_str())
                {
                    return Err(
                        "lifecycle-upgrade apply requires the exact planned fingerprint".into(),
                    );
                }
                if approved != generated {
                    return Err(
                        "saved lifecycle-upgrade Plan is stale or differs from the current candidate"
                            .into(),
                    );
                }
                let mut result = upgrade_source_bundle(
                    &bundle,
                    &packages,
                    &target_root,
                    &evidence,
                    &approved,
                    &approvals,
                    &action,
                    &removed_platforms,
                    &removed_runtime_configs,
                    session_launcher.as_deref(),
                )?;
                if action == "partial-uninstall"
                    && result.get("status").and_then(Value::as_str) == Some("upgraded")
                {
                    result["status"] = Value::String("partially-uninstalled".to_owned());
                }
                print!("{}", String::from_utf8(canonical_json(&result)?)?);
            }
        }
        Command::LifecycleRollback {
            target_root,
            approve_current_lock,
            approve_rollback_point,
        } => {
            let result = rollback_source_install(
                target_root,
                &approve_current_lock,
                &approve_rollback_point,
            )?;
            print!("{}", String::from_utf8(canonical_json(&result)?)?);
        }
        Command::RecipeCapabilities { targets } => {
            let targets = targets.into_iter().collect::<BTreeSet<_>>();
            let value = serde_json::to_value(automatic_recipe_capabilities(&targets))?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::PolicyResolve {
            profile,
            task,
            explicit_platforms,
            constraints,
            policy_layers,
        } => {
            let profile = load_json(profile)?;
            let constraints = constraints.map(load_json).transpose()?;
            let policy_layers = policy_layers
                .map(load_json)
                .transpose()?
                .unwrap_or_else(|| serde_json::json!([]));
            let policy_layers = policy_layers
                .as_array()
                .ok_or("policy layers must be an array")?;
            let value = resolve_policy(
                &profile,
                &task,
                &explicit_platforms,
                constraints.as_ref(),
                policy_layers,
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::RepositoryDiscover {
            repository,
            manifests,
            target_files,
            changed_files,
            cwd,
            core_version,
            disabled_providers,
            provider_roots,
        } => {
            let disabled = disabled_providers.into_iter().collect::<BTreeSet<_>>();
            let registry = ManifestRegistry::from_directory_with_provider_roots(
                manifests,
                &provider_roots,
                &disabled,
                &core_version,
            )?;
            let value = DiscoveryEngine::new(&registry).discover(
                repository,
                &target_files,
                &changed_files,
                cwd.as_deref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::PlanCompile {
            profile,
            policy,
            manifests,
            core_version,
            disabled_providers,
            provider_roots,
            lock,
        } => {
            let profile = load_json(profile)?;
            let policy = load_json(policy)?;
            let disabled = disabled_providers.into_iter().collect::<BTreeSet<_>>();
            let registry = ManifestRegistry::from_directory_with_provider_roots(
                manifests,
                &provider_roots,
                &disabled,
                &core_version,
            )?;
            let package_lock = lock.map(load_json).transpose()?;
            let value = compile_plan_with_package_lock(
                &registry,
                &profile,
                &policy,
                package_lock.as_ref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::LockResolve {
            install_plan,
            schemas,
            previous,
            sources,
            source_base,
            source_hashes,
            output,
        } => {
            let install_plan = load_json(install_plan)?;
            let previous = previous.map(load_json).transpose()?;
            let sources = parse_lock_sources(&sources)?;
            let source_hashes = parse_source_hashes(&source_hashes)?;
            let value = resolve_package_lock(
                &install_plan,
                schemas,
                Some(&sources),
                Some(&source_hashes),
                source_base,
                previous.as_ref(),
            )?;
            let encoded = canonical_json(&value)?;
            if let Some(output) = output {
                std::fs::write(output, &encoded)?;
            }
            print!("{}", String::from_utf8(encoded)?);
        }
        Command::LockValidate { lockfile } => {
            let value = load_json(lockfile)?;
            validate_package_lock(&value)?;
            let result = json!({
                "lock_hash": value.get("fingerprint").cloned().unwrap_or(Value::Null),
                "status": "passed",
            });
            print!("{}", String::from_utf8(canonical_json(&result)?)?);
        }
        Command::LockDiff { before, after } => {
            let before = load_json(before)?;
            let after = load_json(after)?;
            let value = diff_package_locks(&before, &after)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::LockExplain { lockfile } => {
            let value = load_json(lockfile)?;
            let value = explain_package_lock(&value)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::UpgradeEvidenceValidate { evidence } => {
            let value = load_json(evidence)?;
            validate_upgrade_conformance_evidence(&value)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::UpgradePlanBuild {
            candidate_install_plan,
            candidate_package_lock,
            evidence,
            target_root,
            action,
            session_launcher,
            removed_platforms,
            removed_runtime_configs,
        } => {
            let candidate_install_plan = load_json(candidate_install_plan)?;
            let candidate_package_lock = load_json(candidate_package_lock)?;
            let evidence = load_json(evidence)?;
            let session_launcher = session_launcher
                .as_deref()
                .map(read_frozen_executable)
                .transpose()?;
            let snapshot = inspect_upgrade_planning_snapshot(
                &target_root,
                &candidate_install_plan,
                &candidate_package_lock,
                &action,
                &removed_platforms,
                &removed_runtime_configs,
                session_launcher.as_deref(),
            )?;
            let value = compile_upgrade_plan(
                &snapshot,
                &action,
                &candidate_install_plan,
                &candidate_package_lock,
                &evidence,
                &removed_platforms,
                &removed_runtime_configs,
            )?;
            let encoded = canonical_json(&value)?;
            if encoded.len() > MAX_CONTRACT_JSON_BYTES {
                return Err(
                    format!("Upgrade Plan has more than {MAX_CONTRACT_JSON_BYTES} bytes").into(),
                );
            }
            print!("{}", String::from_utf8(encoded)?);
        }
        Command::UpgradePlanValidate { plan } => {
            let value = load_json(plan)?;
            validate_upgrade_plan(&value)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::DoctorBaseline {
            target_root,
            schemas,
        } => {
            let value = inspect_doctor_baseline(target_root, schemas)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
            if value
                .get("checks")
                .and_then(Value::as_array)
                .is_some_and(|checks| {
                    checks
                        .iter()
                        .any(|check| check.get("status").and_then(Value::as_str) == Some("failed"))
                })
            {
                return Ok(2);
            }
        }
        Command::DoctorReport {
            target_root,
            schemas,
            python_version,
        } => {
            let value = inspect_doctor_report_v1(target_root, schemas, &python_version)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
            if value.get("status").and_then(Value::as_str) == Some("blocked") {
                return Ok(2);
            }
        }
        Command::CodexConfigRender {
            shared_config,
            agents_path,
            existing_config,
        } => {
            let shared = read_bounded_codex_config(&shared_config, "shared Codex config")?;
            let existing = existing_config
                .as_deref()
                .map(|path| read_bounded_codex_config(path, "existing Codex config"))
                .transpose()?;
            let agents_path = agents_path.to_str().ok_or(
                "Codex config agents path must be valid UTF-8 for compatibility rendering",
            )?;
            let rendered = render_codex_config(existing.as_deref(), &shared, agents_path)?;
            std::io::stdout().write_all(&rendered)?;
        }
        Command::LifecycleUninstall {
            target_root,
            platforms,
            dry_run,
            json,
        } => {
            let result = if dry_run {
                inspect_uninstall_plan(&target_root, &platforms)
            } else {
                (|| -> Result<Value, LifecycleError> {
                    let workspace = LifecycleWorkspace::begin_existing(&target_root)?;
                    let published = workspace.publish_uninstall_for_platforms(&platforms)?;
                    published.commit()
                })()
            };
            match result {
                Ok(value) => write_uninstall_report(&value, json)?,
                Err(error) => {
                    write_uninstall_error(&error, json)?;
                    return Ok(2);
                }
            }
        }
        Command::RuntimeExecute {
            plan,
            behaviors,
            approvals,
            lock,
            ledger,
            resume,
            identity_seed,
        } => {
            let plan = load_json(plan)?;
            let behaviors = behaviors.map(load_json).transpose()?;
            let approvals = approvals.map(load_json).transpose()?;
            let lock = lock.map(load_json).transpose()?;
            let value = execute_fake_plan(
                &plan,
                behaviors.as_ref(),
                approvals.as_ref(),
                lock.as_ref(),
                ledger.as_deref(),
                resume,
                identity_seed.as_deref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::AdapterRequestBuild {
            plan,
            node_id,
            context,
            invocation_id,
            lock,
        } => {
            let plan = load_json(plan)?;
            let _ = validate_workflow_plan_and_lock(&plan, lock.as_deref())?;
            let value =
                build_adapter_request(&plan, &node_id, &load_json(context)?, &invocation_id)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::AdapterRequestValidate { request } => {
            let value = load_json(request)?;
            validate_adapter_request(&value)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::AdapterResultValidate { request, result } => {
            let request = load_json(request)?;
            let result = load_json(result)?;
            validate_adapter_result(&request, &result)?;
            print!("{}", String::from_utf8(canonical_json(&result)?)?);
        }
        Command::InvocationPrepare {
            root,
            plan,
            node_id,
            context,
            invocation_id,
            lock,
        } => {
            let plan = load_json(plan)?;
            let lock = validate_workflow_plan_and_lock(&plan, lock.as_deref())?;
            let value = prepare_provider_invocation(
                &root,
                &plan,
                &node_id,
                &load_json(context)?,
                &invocation_id,
                lock.as_ref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::InvocationClaim {
            root,
            request_id,
            actor_id,
            claim_token_file,
        } => {
            let value = claim_provider_invocation(
                &root,
                &request_id,
                &actor_id,
                &load_claim_token_file(&claim_token_file)?,
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::InvocationSubmit {
            root,
            request_id,
            result,
            claim_token_file,
        } => {
            let value = submit_provider_invocation(
                &root,
                &request_id,
                &load_json(result)?,
                &load_claim_token_file(&claim_token_file)?,
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::InvocationInspect { root, request_id } => {
            let value = inspect_provider_invocation(&root, &request_id)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::RuntimeExecuteRecorded {
            plan,
            results,
            context,
            lock,
            ledger,
            resume,
            identity_seed,
        } => {
            let plan = load_json(plan)?;
            let results = load_json(results)?;
            let context = load_json(context)?;
            let lock = lock.map(load_json).transpose()?;
            let value = execute_recorded_plan(
                &plan,
                &results,
                &context,
                lock.as_ref(),
                ledger.as_deref(),
                resume,
                identity_seed.as_deref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::RuntimeExecuteInvocations {
            plan,
            root,
            context,
            selection,
            lock,
            ledger,
            resume,
            identity_seed,
        } => {
            let plan = load_json(plan)?;
            let lock = validate_workflow_plan_and_lock(&plan, lock.as_deref())?;
            let results = collect_submitted_results(
                &root,
                plan.get("fingerprint")
                    .and_then(Value::as_str)
                    .ok_or("workflow plan fingerprint is required")?,
                &load_json(selection)?,
            )?;
            let context = load_json(context)?;
            let value = execute_recorded_plan(
                &plan,
                &results,
                &context,
                lock.as_ref(),
                ledger.as_deref(),
                resume,
                identity_seed.as_deref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::WorktreeStatus { repository } => {
            let value = worktree_status(&repository)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::RepositoryPatch {
            repository,
            repository_id,
            base_commit,
            checkpoint_commit,
        } => {
            let value = repository_patch(
                &repository,
                &repository_id,
                &base_commit,
                checkpoint_commit.as_deref(),
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::RepositoryInspect {
            repository,
            repository_id,
            role,
            base_ref,
            base_source,
            committed,
        } => {
            let value = inspect_repository(
                &repository,
                &repository_id,
                &role,
                &base_ref,
                &base_source,
                committed,
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionSourceIdentity { repositories, mode } => {
            let repositories = load_json(repositories)?;
            let value = Value::String(session_source_identity(&repositories, &mode)?);
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionWorktreeCreate {
            repository,
            name,
            repository_id,
            base_ref,
            base_source,
            worktree_root,
            branch,
        } => {
            let (record, notice) = create_session_worktree(
                &repository,
                &name,
                &repository_id,
                base_ref.as_deref(),
                base_source.as_deref(),
                worktree_root.as_deref(),
                branch.as_deref(),
            )?;
            let value = json!({"notice": notice, "repository": record});
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionWorktreeRemove {
            source_repository,
            repository_record,
        } => {
            let record = load_json(repository_record)?;
            remove_created_session_worktree(&record, &source_repository)?;
            let value = json!({"removed": true});
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionCreate {
            repository,
            name,
            context_input,
            base_ref,
            base_source,
            worktree_root,
            branch,
        } => {
            let mut input = load_json(context_input)?;
            let session_id = input
                .get("session_id")
                .and_then(Value::as_str)
                .ok_or("session context input session_id is required")?
                .to_owned();
            registry_assert_available(&repository, &session_id)?;
            let (record, notice) = create_session_worktree(
                &repository,
                &name,
                "primary",
                base_ref.as_deref(),
                base_source.as_deref(),
                worktree_root.as_deref(),
                branch.as_deref(),
            )?;
            let registration = (|| -> Result<Value, agent_runtime::RuntimeError> {
                let input_object = input.as_object_mut().ok_or_else(|| {
                    agent_runtime::RuntimeError::Contract(
                        "session context input must be an object".to_owned(),
                    )
                })?;
                input_object.insert(
                    "repositories".to_owned(),
                    Value::Array(vec![record.clone()]),
                );
                let context = new_session_context(&input)?;
                registry_create_active(&repository, &context)
            })();
            if let Err(error) = registration {
                if let Err(cleanup_error) = remove_created_session_worktree(&record, &repository) {
                    return Err(format!(
                        "session registration failed ({error}); exact Worktree compensation was blocked ({cleanup_error})"
                    )
                    .into());
                }
                return Err(error.into());
            }
            let session = registration?;
            let value = json!({
                "notice": notice,
                "operation": "create",
                "schema_version": "1.0",
                "session": session,
            });
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionCreateManifest {
            repository,
            name,
            project_id,
            session_id,
            created_at,
            platforms,
            manifest_root,
            base_ref,
            base_source,
            worktree_root,
            branch,
        } => {
            let selection =
                compile_session_manifest_selection(manifest_root.as_deref(), &platforms)?;
            let session_id = session_id.unwrap_or_else(|| name.clone());
            registry_assert_available(&repository, &session_id)?;
            let (record, notice) = create_session_worktree(
                &repository,
                &name,
                "primary",
                base_ref.as_deref(),
                base_source.as_deref(),
                worktree_root.as_deref(),
                branch.as_deref(),
            )?;
            let registration = (|| -> Result<Value, agent_runtime::RuntimeError> {
                let input = json!({
                    "capability_closure": selection["capability_closure"],
                    "created_at": created_at,
                    "dependencies": [],
                    "platform_contexts": selection["platform_contexts"],
                    "project_id": project_id,
                    "repositories": [record.clone()],
                    "selected_platforms": selection["selected_platforms"],
                    "session_id": session_id,
                });
                let context = new_session_context(&input)?;
                registry_create_active(&repository, &context)
            })();
            if let Err(error) = registration {
                if let Err(cleanup_error) = remove_created_session_worktree(&record, &repository) {
                    return Err(format!(
                        "session registration failed ({error}); exact Worktree compensation was blocked ({cleanup_error})"
                    )
                    .into());
                }
                return Err(error.into());
            }
            let session = registration?;
            let value = json!({
                "notice": notice,
                "operation": "create",
                "schema_version": "1.0",
                "session": session,
            });
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionContextValidate { context } => {
            let context = load_json(context)?;
            validate_worktree_session_context(&context)?;
            print!("{}", String::from_utf8(canonical_json(&context)?)?);
        }
        Command::SessionContextCreate { input } => {
            let input = load_json(input)?;
            let value = new_session_context(&input)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionContextRefresh { context } => {
            let mut context = load_json(context)?;
            refresh_session_source_identity(&mut context)?;
            validate_worktree_session_context(&context)?;
            print!("{}", String::from_utf8(canonical_json(&context)?)?);
        }
        Command::SessionContextFreeze { context } => {
            let context = load_json(context)?;
            let value = freeze_checkpoint(&context)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionContextTransition { context, target } => {
            let context = load_json(context)?;
            let value = transition_session_context(&context, &target)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryCreate {
            repository,
            context,
        } => {
            let context = load_json(context)?;
            let value = registry_create(&repository, &context)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryLoad {
            repository,
            session_id,
        } => {
            let value = registry_load(&repository, &session_id)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryList { repository } => {
            let value = registry_list(&repository)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryWrite {
            repository,
            context,
        } => {
            let context = load_json(context)?;
            let value = registry_write(&repository, &context)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryTransition {
            repository,
            session_id,
            target,
        } => {
            let value = registry_transition(&repository, &session_id, &target)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryCheckpoint {
            repository,
            session_id,
        } => {
            let session = registry_checkpoint(&repository, &session_id)?;
            let value = json!({
                "notice": {"commits_created": false, "staging_changed": false},
                "operation": "checkpoint",
                "schema_version": "1.0",
                "session": session,
            });
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionRegistryGate {
            repository,
            session_id,
            adapter_pairs,
            ledger,
            artifact_root,
        } => {
            let pairs = load_json(adapter_pairs)?;
            let ledger = load_json(ledger)?;
            let value = registry_attach_and_gate(
                &repository,
                &session_id,
                &pairs,
                &ledger,
                &artifact_root,
            )?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::SessionGateAttach {
            context,
            attempt_id,
            request,
            result,
        } => {
            let mut context = load_json(context)?;
            let request = load_json(request)?;
            let result = load_json(result)?;
            attach_adapter_result(&mut context, &attempt_id, &request, &result)?;
            print!("{}", String::from_utf8(canonical_json(&context)?)?);
        }
        Command::SessionGateEvaluate {
            context,
            adapter_pairs,
            ledger,
            artifact_root,
        } => {
            let context = load_json(context)?;
            let pairs = load_json(adapter_pairs)?;
            let ledger = load_json(ledger)?;
            let value = evaluate_session_gate(&context, &pairs, &ledger, &artifact_root)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
    }
    Ok(0)
}

fn write_uninstall_report(
    report: &Value,
    json_output: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    if json_output {
        std::io::stdout().write_all(&canonical_json(report)?)?;
    } else {
        print!("{}", uninstall_human_report(report)?);
    }
    Ok(())
}

fn write_uninstall_error(
    error: &LifecycleError,
    json_output: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    if json_output {
        std::io::stderr().write_all(&canonical_json(&json!({
            "error": error.to_string(),
            "status": "blocked",
        }))?)?;
    } else {
        eprintln!("✗ Agent Development Skills 卸载未完成\n\n  原因：{error}\n");
    }
    Ok(())
}

fn uninstall_human_report(report: &Value) -> Result<String, Box<dyn std::error::Error>> {
    let status = report
        .get("status")
        .and_then(Value::as_str)
        .ok_or("uninstall report status must be a string")?;
    let dry_run = status == "planned";
    let platforms = report
        .get("selected_platforms")
        .and_then(Value::as_array)
        .ok_or("uninstall report selected_platforms must be an array")?
        .iter()
        .map(|value| {
            value
                .as_str()
                .ok_or("uninstall report platform must be a string")
        })
        .collect::<Result<Vec<_>, _>>()?;
    let managed_roots = report
        .get("managed_roots")
        .and_then(Value::as_array)
        .ok_or("uninstall report managed_roots must be an array")?;
    let activated_files = report
        .get("activated_files")
        .and_then(Value::as_array)
        .ok_or("uninstall report activated_files must be an array")?;
    let config_action = report
        .get("config_action")
        .and_then(Value::as_str)
        .ok_or("uninstall report config_action must be a string")?;
    let preserved_profiles = report
        .get("preserved_profiles")
        .and_then(Value::as_array)
        .ok_or("uninstall report preserved_profiles must be an array")?;
    let preserved_system_skills = report
        .get("preserved_system_skills")
        .and_then(Value::as_bool)
        .ok_or("uninstall report preserved_system_skills must be a boolean")?;
    let title = if dry_run {
        "◇ Agent Development Skills 卸载预览"
    } else {
        "✓ Agent Development Skills 卸载完成"
    };
    let selected = if platforms.is_empty() {
        "全部受管内容".to_owned()
    } else {
        platforms.join("、")
    };
    let mut lines = vec![
        title.to_owned(),
        String::new(),
        format!("  平台：{selected}"),
        format!("  受管根：{} 个", managed_roots.len()),
        format!("  激活文件：{} 个", activated_files.len()),
        format!("  config.toml：{config_action}"),
    ];
    if !preserved_profiles.is_empty() {
        lines.push(format!(
            "  保留本机 Profiles：{} 个",
            preserved_profiles.len()
        ));
    }
    if preserved_system_skills {
        lines.push("  保留 Codex 系统 Skills：是".to_owned());
    }
    lines.push("  旧 iOSAgentSkills 软链：未恢复（安装时未创建持久备份）".to_owned());
    if dry_run {
        lines.extend([
            String::new(),
            "未写入任何文件；移除 --dry-run 后执行卸载。".to_owned(),
        ]);
    }
    Ok(lines.join("\n") + "\n")
}

fn read_bounded_codex_config(
    path: &Path,
    label: &str,
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let file = std::fs::File::open(path)?;
    let length = file.metadata()?.len();
    if length > MAX_CONTRACT_JSON_BYTES as u64 {
        return Err(format!("{label} has more than {MAX_CONTRACT_JSON_BYTES} bytes").into());
    }
    let capacity = usize::try_from(length)
        .unwrap_or(MAX_CONTRACT_JSON_BYTES)
        .min(MAX_CONTRACT_JSON_BYTES);
    let mut bytes = Vec::with_capacity(capacity);
    file.take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_CONTRACT_JSON_BYTES {
        return Err(format!("{label} has more than {MAX_CONTRACT_JSON_BYTES} bytes").into());
    }
    Ok(bytes)
}

fn read_frozen_executable(path: &Path) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    if path.is_symlink() {
        return Err("native session launcher must not be a symlink".into());
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt as _;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_CONTRACT_JSON_BYTES as u64
    {
        return Err("native session launcher is missing, empty, or exceeds its size limit".into());
    }
    let expected = usize::try_from(metadata.len())
        .map_err(|_| "native session launcher size cannot be represented")?;
    let mut bytes = Vec::with_capacity(expected);
    file.take(MAX_CONTRACT_JSON_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() != expected {
        return Err("native session launcher changed while it was being read".into());
    }
    Ok(bytes)
}

fn native_install_report(
    outcome: &Value,
    platform_options: &[Value],
    dry_run: bool,
) -> Result<Value, Box<dyn std::error::Error>> {
    let plan = outcome
        .get("install_plan")
        .and_then(Value::as_object)
        .ok_or("native install outcome has no Install Plan")?;
    let selected_platforms = plan
        .get("selected_platforms")
        .cloned()
        .ok_or("native Install Plan has no selected platforms")?;
    let selected_runtime_configs = plan
        .get("selected_runtime_configs")
        .cloned()
        .ok_or("native Install Plan has no selected runtime configs")?;
    let selected_packages = plan
        .get("selected_packages")
        .and_then(Value::as_array)
        .ok_or("native Install Plan has no selected packages")?
        .iter()
        .map(|record| {
            record
                .get("id")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or("native Install Plan package id is invalid")
        })
        .collect::<Result<Vec<_>, _>>()?;
    let skill_count = plan
        .get("skills")
        .and_then(Value::as_array)
        .ok_or("native Install Plan skills are invalid")?
        .len();
    let activation = native_activation_report(outcome.get("activation").unwrap_or(&Value::Null))?;
    let target_root = outcome
        .get("target_root")
        .and_then(Value::as_str)
        .ok_or("native install outcome has no target root")?;
    let mut report = json!({
        "activation": activation,
        "engine": "rust",
        "persistent_backup": false,
        "platform_options": platform_options,
        "schema_version": "1.0",
        "selected_packages": selected_packages,
        "selected_platforms": selected_platforms,
        "selected_runtime_configs": selected_runtime_configs,
        "skill_count": skill_count,
        "status": if dry_run { "planned" } else { "installed" },
        "target_root": target_root,
    });
    let report = report
        .as_object_mut()
        .ok_or("native install report must be an object")?;
    if dry_run {
        report.insert(
            "would_remove_legacy_symlinks".to_owned(),
            serde_json::json!([]),
        );
    } else {
        report.insert(
            "post_install_validation".to_owned(),
            json!({
                "kind": "native-lifecycle-transaction",
                "status": "passed",
            }),
        );
        report.insert(
            "preserved_system_skills".to_owned(),
            Value::Bool(Path::new(target_root).join("skills/.system").is_dir()),
        );
        report.insert("removed_legacy_symlinks".to_owned(), serde_json::json!([]));
    }
    Ok(Value::Object(report.clone()))
}

fn native_activation_report(activation: &Value) -> Result<Value, Box<dyn std::error::Error>> {
    let Some(record) = activation.as_object() else {
        return Ok(json!({
            "config_changed": false,
            "managed_file_updates": [],
            "managed_files_unchanged": [],
            "profile_creates": [],
            "profile_preserves": [],
        }));
    };
    let updated = record
        .get("updated_files")
        .cloned()
        .ok_or("native activation report has no updated files")?;
    let unchanged = record
        .get("unchanged_files")
        .cloned()
        .unwrap_or_else(|| serde_json::json!([]));
    let created = record
        .get("created_profiles")
        .cloned()
        .ok_or("native activation report has no created profiles")?;
    let created_names = created
        .as_array()
        .ok_or("native activation created profiles are invalid")?
        .iter()
        .filter_map(Value::as_str)
        .collect::<BTreeSet<_>>();
    let preserved = [
        "budget.config.toml",
        "daily.config.toml",
        "deep.config.toml",
        "extreme.config.toml",
        "interactive-fast.config.toml",
        "readonly.config.toml",
    ]
    .into_iter()
    .filter(|name| !created_names.contains(name))
    .collect::<Vec<_>>();
    Ok(json!({
        "config_changed": record.get("config_changed").cloned().unwrap_or(Value::Bool(false)),
        "managed_file_updates": updated,
        "managed_files_unchanged": unchanged,
        "profile_creates": created,
        "profile_preserves": preserved,
    }))
}

fn native_install_human_report(report: &Value) -> Result<String, Box<dyn std::error::Error>> {
    let platforms = report
        .get("selected_platforms")
        .and_then(Value::as_array)
        .ok_or("native install report platforms are invalid")?
        .iter()
        .map(|value| value.as_str().ok_or("native install platform is invalid"))
        .collect::<Result<Vec<_>, _>>()?
        .join("、");
    let target = report
        .get("target_root")
        .and_then(Value::as_str)
        .ok_or("native install target is invalid")?;
    if report.get("status").and_then(Value::as_str) == Some("planned") {
        let activation = report
            .get("activation")
            .and_then(Value::as_object)
            .ok_or("native install preview activation is invalid")?;
        let updates = activation
            .get("managed_file_updates")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        let unchanged = activation
            .get("managed_files_unchanged")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        let creates = activation
            .get("profile_creates")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        let preserves = activation
            .get("profile_preserves")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        let config = if activation
            .get("config_changed")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            "将合并更新"
        } else {
            "已一致"
        };
        return Ok(format!(
            "◇ {platforms} 平台安装预览\n\n变更摘要\n  ↻ config.toml：{config}\n  ↻ 受管文件：将更新 {updates} 个\n  ✓ 其余已一致：{unchanged} 个\n  ↻ Profiles：将创建 {creates} 个，保留 {preserves} 个\n  • 持久备份：不创建（按当前安装策略）\n\n未写入目标目录：{target}\n确认后移除 --dry-run，重新执行原命令。\n\n自动化场景：添加 --json 获取 canonical JSON。\n"
        ));
    }
    Ok(format!(
        "✓ {platforms} 平台安装完成\n\n  Rust 原生事务：passed\n  安装态验证：passed\n  目标目录：{target}\n\n自动化场景：添加 --json 获取 canonical JSON。\n"
    ))
}

fn parse_lock_sources(values: &[String]) -> Result<Map<String, Value>, Box<dyn std::error::Error>> {
    let mut sources = BTreeMap::new();
    for value in values {
        let (package_id, uri) = value
            .split_once('=')
            .filter(|(package_id, uri)| !package_id.is_empty() && !uri.is_empty())
            .ok_or("--source must use PACKAGE=URI")?;
        if sources.contains_key(package_id) {
            return Err(format!("duplicate --source package: {package_id}").into());
        }
        let kind = if uri.starts_with("registry://") {
            "local-registry"
        } else if uri.starts_with("./") {
            "relative-path"
        } else if uri.starts_with("https://") {
            "https"
        } else {
            return Err(format!("unsupported --source URI: {package_id}").into());
        };
        sources.insert(package_id.to_owned(), json!({"kind": kind, "uri": uri}));
    }
    Ok(sources.into_iter().collect())
}

fn validate_workflow_plan_and_lock(
    plan: &Value,
    lock_path: Option<&std::path::Path>,
) -> Result<Option<Value>, Box<dyn std::error::Error>> {
    require_schema_version(plan, "1.0")?;
    validate_compiled_plan(plan)?;
    let frozen = plan
        .get("package_lock_hash")
        .is_some_and(|value| !value.is_null());
    match (frozen, lock_path) {
        (false, None) => Ok(None),
        (false, Some(_)) => {
            Err("workflow plan is not frozen to the supplied package Lockfile".into())
        }
        (true, None) => {
            Err("locked workflow operation requires the current package Lockfile".into())
        }
        (true, Some(path)) => {
            let package_lock = load_json(path)?;
            validate_package_lock(&package_lock)?;
            validate_plan_package_lock(plan, &package_lock)?;
            Ok(Some(package_lock))
        }
    }
}

fn parse_source_hashes(
    values: &[String],
) -> Result<Map<String, Value>, Box<dyn std::error::Error>> {
    let mut hashes = BTreeMap::new();
    for value in values {
        let (package_id, digest) = value
            .split_once('=')
            .filter(|(package_id, digest)| !package_id.is_empty() && !digest.is_empty())
            .ok_or("--source-sha256 must use PACKAGE=SHA256")?;
        if hashes.contains_key(package_id) {
            return Err(format!("duplicate --source-sha256 package: {package_id}").into());
        }
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
        {
            return Err(format!("invalid --source-sha256 digest: {package_id}").into());
        }
        hashes.insert(package_id.to_owned(), Value::String(digest.to_owned()));
    }
    Ok(hashes.into_iter().collect())
}

fn main() {
    let worker = match std::thread::Builder::new()
        .name("agent-skills-cli".to_owned())
        .stack_size(CLI_WORKER_STACK_BYTES)
        .spawn(|| match run() {
            Ok(exit_code) => exit_code,
            Err(error) => {
                eprintln!("{error}");
                2
            }
        }) {
        Ok(worker) => worker,
        Err(error) => {
            eprintln!("failed to start CLI worker: {error}");
            std::process::exit(2);
        }
    };
    let exit_code = match worker.join() {
        Ok(exit_code) => exit_code,
        Err(payload) => std::panic::resume_unwind(payload),
    };
    if exit_code != 0 {
        std::process::exit(exit_code);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uninstall_human_output_matches_source_cli_for_preview_and_success() {
        let mut report = json!({
            "activated_files": ["bin/tool"],
            "config_action": "removed-managed-instructions-path",
            "legacy_links_restored": false,
            "managed_roots": ["AGENTS.md", "skills", ".agent-skills"],
            "preserved_profiles": ["readonly.config.toml"],
            "preserved_system_skills": true,
            "removed_packages": ["core"],
            "schema_version": "1.0",
            "selected_platforms": ["apple"],
            "status": "planned",
            "target_root": "/tmp/.codex",
        });
        assert_eq!(
            uninstall_human_report(&report).expect("render preview"),
            concat!(
                "◇ Agent Development Skills 卸载预览\n",
                "\n",
                "  平台：apple\n",
                "  受管根：3 个\n",
                "  激活文件：1 个\n",
                "  config.toml：removed-managed-instructions-path\n",
                "  保留本机 Profiles：1 个\n",
                "  保留 Codex 系统 Skills：是\n",
                "  旧 iOSAgentSkills 软链：未恢复（安装时未创建持久备份）\n",
                "\n",
                "未写入任何文件；移除 --dry-run 后执行卸载。\n",
            )
        );

        report["status"] = Value::String("uninstalled".to_owned());
        assert_eq!(
            uninstall_human_report(&report).expect("render success"),
            concat!(
                "✓ Agent Development Skills 卸载完成\n",
                "\n",
                "  平台：apple\n",
                "  受管根：3 个\n",
                "  激活文件：1 个\n",
                "  config.toml：removed-managed-instructions-path\n",
                "  保留本机 Profiles：1 个\n",
                "  保留 Codex 系统 Skills：是\n",
                "  旧 iOSAgentSkills 软链：未恢复（安装时未创建持久备份）\n",
            )
        );
    }
}
