//! Parallel native compatibility entry point.

use agent_contracts::{canonical_json, canonical_sha256, load_json, require_schema_version};
use agent_engine::{
    DiscoveryEngine, compile_plan_with_package_lock, diff_package_locks, explain_package_lock,
    resolve_package_lock, resolve_policy, validate_package_lock,
};
use agent_registry::{CORE_VERSION, ManifestRegistry, automatic_recipe_capabilities};
use agent_runtime::{
    build_adapter_request, execute_fake_plan, execute_recorded_plan, freeze_checkpoint,
    inspect_repository, new_session_context, refresh_session_source_identity, registry_create,
    registry_list, registry_load, registry_transition, registry_write, repository_patch,
    session_source_identity, transition_session_context, validate_adapter_request,
    validate_adapter_result, validate_worktree_session_context, worktree_status,
};
use clap::{Parser, Subcommand};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

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
    },
    /// Validate one frozen Adapter Request v1.
    AdapterRequestValidate { request: PathBuf },
    /// Validate one Adapter Result v1 against its frozen request.
    AdapterResultValidate { request: PathBuf, result: PathBuf },
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
}

#[allow(clippy::too_many_lines)]
fn run() -> Result<(), Box<dyn std::error::Error>> {
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
        } => {
            let value = build_adapter_request(
                &load_json(plan)?,
                &node_id,
                &load_json(context)?,
                &invocation_id,
            )?;
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
    }
    Ok(())
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
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
