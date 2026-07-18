//! Parallel native compatibility entry point.

use agent_contracts::{canonical_json, canonical_sha256, load_json, require_schema_version};
use agent_engine::{
    DiscoveryEngine, compile_plan_with_package_lock, diff_package_locks, explain_package_lock,
    resolve_package_lock, resolve_policy, validate_compiled_plan, validate_package_lock,
    validate_plan_package_lock,
};
use agent_lifecycle::{inspect_doctor_baseline, inspect_doctor_report_v1};
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
use std::path::PathBuf;

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

#[derive(Debug, Subcommand)]
enum Command {
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

#[allow(clippy::too_many_lines)]
fn run() -> Result<i32, Box<dyn std::error::Error>> {
    match Cli::parse().command {
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
