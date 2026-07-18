use crate::{
    INSTALL_BACKUP_PREFIX, INSTALL_STAGE_PREFIX, LIFECYCLE_LOCK_DIRECTORY, LifecycleError,
    LifecycleLock, LifecycleWorkspace, UNINSTALL_BACKUP_PREFIX, ValidatedInstallPlan,
    ignored_os_metadata, open_root_directory, source_bundle::SourceInstallBundle,
    source_packages::SourcePackageSet, transaction_lock,
};
use agent_engine::validate_install_plan;
use cap_std::fs::Dir;
use serde_json::Value;
use std::path::Path;

const MANAGED_ROOTS: [&str; 3] = ["AGENTS.md", "skills", ".agent-skills"];

/// Validate a fresh native source installation without writing the target.
///
/// The returned value is the existing planned Install Plan projection, matching
/// the Python source install dry-run contract. This compatibility slice
/// intentionally rejects replacement of an existing managed installation;
/// replacement must use the approval-bound upgrade lifecycle.
///
/// # Errors
/// Returns a fail-closed error for an unrelated bundle/snapshot pair, invalid
/// contracts, unsafe or occupied targets, or visible lifecycle recovery state.
pub fn inspect_source_install(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
) -> Result<Value, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    inspect_fresh_target(target_root.as_ref())?;
    Ok(bundle.plan().clone())
}

/// Stage, verify, atomically publish, and commit a fresh native source install.
///
/// Package and Skill sources are reopened through directory capabilities and
/// copied only when every file matches the Install Plan. All managed roots,
/// Lockfiles, preserved external state, and rebuilt semantics are verified
/// before and after publication. A failed or dropped publication removes the
/// new roots and retains recovery evidence if cleanup cannot complete.
///
/// This first production-shaped compatibility slice does not run source
/// activation and does not replace an existing install. Those operations
/// remain separate approval-bound lifecycle steps.
///
/// # Errors
/// Returns a fail-closed error for invalid contracts, source drift, unsafe
/// targets, occupied managed roots, staging disagreement, publication races,
/// or incomplete rollback/cleanup.
pub fn install_source_bundle(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
) -> Result<Value, LifecycleError> {
    Ok(install_source_bundle_with_options(
        bundle,
        package_set,
        target_root.as_ref(),
        false,
        None,
        |_| Ok(()),
    )?
    .install_plan)
}

/// Install and activate a fresh source bundle with one frozen native session launcher.
///
/// Apple selections stage the launcher and every external activation preimage
/// under the same rollback contract as the managed roots. Non-Apple selections
/// complete the managed installation without external activation. This entry
/// point remains fresh-only; replacement and legacy adoption must use the
/// approval-bound compatibility path.
///
/// # Errors
/// Returns a fail-closed error for missing Apple launcher bytes, invalid source
/// or target state, activation conflicts, publication drift, or incomplete
/// rollback/cleanup.
pub fn install_source_bundle_with_activation(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    session_launcher: &[u8],
) -> Result<Value, LifecycleError> {
    let outcome = install_source_bundle_with_options(
        bundle,
        package_set,
        target_root.as_ref(),
        true,
        Some(session_launcher),
        |_| Ok(()),
    )?;
    Ok(serde_json::json!({
        "activation": outcome.activation,
        "install_plan": outcome.install_plan,
    }))
}

#[derive(Debug)]
struct SourceInstallOutcome {
    activation: Option<Value>,
    install_plan: Value,
}

fn install_source_bundle_with_options(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: &Path,
    activate_source: bool,
    session_launcher: Option<&[u8]>,
    mut before_lock: impl FnMut(&Path) -> Result<(), LifecycleError>,
) -> Result<SourceInstallOutcome, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    let activate_apple = activate_source && selected_platforms(bundle.plan())?.contains("apple");
    if activate_apple && session_launcher.is_none() {
        return invalid("native Apple source install requires a frozen session launcher");
    }
    before_lock(target_root)?;
    let lock = LifecycleLock::acquire(target_root)?;
    let locked_target = lock.target_directory()?;
    inspect_fresh_target_directory(&locked_target, true)?;
    let plan = ValidatedInstallPlan::new(bundle.plan().clone(), bundle.package_lock().clone())?;
    let mut installed = bundle.plan().clone();
    installed
        .as_object_mut()
        .ok_or_else(|| LifecycleError::Invalid("Install Plan must be an object".to_owned()))?
        .insert("status".to_owned(), Value::String("installed".to_owned()));
    validate_install_plan(&installed)?;

    let mut workspace = LifecycleWorkspace::from_lock(lock)?;
    workspace.stage_install_layout(&plan, bundle.instructions().as_bytes())?;
    for ((package_id, root), package) in bundle.package_roots().iter().zip(&package_set.packages) {
        if package_id != &package.id {
            return invalid("source package order differs from Install Bundle");
        }
        let package_source =
            open_root_directory(root, None, &format!("source package {package_id}"))?;
        workspace.stage_plan_package(&plan, package_id, &package_source)?;
        for skill in &package.skills {
            let skill_source = open_root_directory(
                &root.join(&skill.relative_root),
                None,
                &format!("source Skill {}", skill.name),
            )?;
            workspace.stage_plan_skill(&plan, &skill.name, &skill_source)?;
        }
    }
    workspace.stage_external_state(&plan)?;
    if activate_apple {
        workspace.stage_fresh_source_activation(
            &plan,
            session_launcher.ok_or_else(|| {
                LifecycleError::Invalid(
                    "native Apple source install requires a frozen session launcher".to_owned(),
                )
            })?,
        )?;
    }
    workspace.verify_staged_install(&plan)?;
    let mut published = workspace.publish_staged_install(&plan)?;
    if !activate_apple {
        published.verify(&plan)?;
    }
    let activation = if activate_apple {
        Some(
            published.apply_source_activation(session_launcher.ok_or_else(|| {
                LifecycleError::Invalid(
                    "native Apple source install requires a frozen session launcher".to_owned(),
                )
            })?)?,
        )
    } else {
        None
    };
    published.verify(&plan)?;
    published.commit(&plan)?;
    Ok(SourceInstallOutcome {
        activation,
        install_plan: installed,
    })
}

fn selected_platforms(plan: &Value) -> Result<std::collections::BTreeSet<&str>, LifecycleError> {
    plan.get("selected_platforms")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("Install Plan platforms are invalid".to_owned()))?
        .iter()
        .map(|value| {
            value.as_str().ok_or_else(|| {
                LifecycleError::Invalid("Install Plan platform is invalid".to_owned())
            })
        })
        .collect()
}

fn validate_source_install_inputs(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
) -> Result<(), LifecycleError> {
    ValidatedInstallPlan::new(bundle.plan().clone(), bundle.package_lock().clone())?;
    if bundle.package_roots() != package_set.package_roots {
        return invalid("source package snapshots differ from compiled Install Bundle roots");
    }
    let bundle_ids = bundle
        .package_roots()
        .iter()
        .map(|(identifier, _)| identifier.as_str())
        .collect::<Vec<_>>();
    let package_ids = package_set
        .packages
        .iter()
        .map(|package| package.id.as_str())
        .collect::<Vec<_>>();
    if bundle_ids != package_ids {
        return invalid("source package snapshots differ from compiled Install Bundle closure");
    }
    Ok(())
}

fn inspect_fresh_target(target_root: &Path) -> Result<(), LifecycleError> {
    let Some(target) = transaction_lock::inspect_optional_target(target_root)? else {
        return Ok(());
    };
    inspect_fresh_target_directory(&target, false)
}

fn inspect_fresh_target_directory(
    target: &Dir,
    allow_owned_lock: bool,
) -> Result<(), LifecycleError> {
    let mut occupied = Vec::new();
    for name in MANAGED_ROOTS {
        match target.symlink_metadata(name) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
            Ok(_) => occupied.push(name),
        }
    }
    if !occupied.is_empty() {
        return invalid(format!(
            "refusing to overwrite unmanaged or modified install roots: {}",
            occupied.join(", ")
        ));
    }
    reject_recovery_state(target, allow_owned_lock)
}

fn reject_recovery_state(target: &Dir, allow_owned_lock: bool) -> Result<(), LifecycleError> {
    let mut recovery = Vec::new();
    for entry in target.entries()? {
        let entry = entry?;
        if ignored_os_metadata(target, &entry.file_name())? {
            continue;
        }
        let name = entry.file_name();
        let name = name.to_str().ok_or_else(|| {
            LifecycleError::Invalid("install target contains a non-UTF-8 entry".to_owned())
        })?;
        if (name == LIFECYCLE_LOCK_DIRECTORY && !allow_owned_lock)
            || name.starts_with(INSTALL_STAGE_PREFIX)
            || name.starts_with(INSTALL_BACKUP_PREFIX)
            || name.starts_with(UNINSTALL_BACKUP_PREFIX)
        {
            recovery.push(name.to_owned());
        }
    }
    recovery.sort();
    if !recovery.is_empty() {
        return invalid(format!(
            "lifecycle recovery state requires attention: {}",
            recovery.join(", ")
        ));
    }
    Ok(())
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        compile_source_install_bundle, resolve_source_install_selection, snapshot_source_packages,
    };
    use agent_contracts::load_json;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "agent-native-install-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&root).expect("create install fixture");
            Self { root }
        }

        fn target(&self) -> PathBuf {
            self.root.join("target")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    fn repository_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("workspace root")
            .to_path_buf()
    }

    fn core_bundle() -> (SourceInstallBundle, SourcePackageSet) {
        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve core");
        let packages = snapshot_source_packages(&selection).expect("snapshot core");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile core bundle");
        (bundle, packages)
    }

    #[test]
    fn dry_run_is_read_only_and_fresh_install_commits_exact_plan() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (bundle, packages) = core_bundle();

        let preview =
            inspect_source_install(&bundle, &packages, &target).expect("inspect fresh install");
        assert_eq!(preview, *bundle.plan());
        assert!(!target.exists());

        let installed =
            install_source_bundle(&bundle, &packages, &target).expect("install fresh bundle");
        assert_eq!(installed["status"], "installed");
        assert_eq!(
            load_json(target.join(".agent-skills/install-lock.json")).expect("load Install Lock"),
            installed
        );
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock")).expect("load package Lock"),
            *bundle.package_lock()
        );
        assert_eq!(
            std::fs::read_to_string(target.join("AGENTS.md")).expect("read AGENTS"),
            bundle.instructions()
        );
        assert!(
            std::fs::read_dir(&target)
                .expect("read target")
                .all(|entry| {
                    let name = entry.expect("target entry").file_name();
                    let name = name.to_string_lossy();
                    !name.starts_with(INSTALL_STAGE_PREFIX)
                        && !name.starts_with(INSTALL_BACKUP_PREFIX)
                        && name != LIFECYCLE_LOCK_DIRECTORY
                })
        );
    }

    #[test]
    fn occupied_managed_root_is_rejected_without_mutation() {
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::create_dir(&target).expect("create target");
        std::fs::write(target.join("AGENTS.md"), b"unmanaged\n").expect("write unmanaged root");
        let (bundle, packages) = core_bundle();

        let error = inspect_source_install(&bundle, &packages, &target)
            .expect_err("occupied preview fails");
        assert!(error.to_string().contains("refusing to overwrite"));
        let error =
            install_source_bundle(&bundle, &packages, &target).expect_err("occupied install fails");
        assert!(error.to_string().contains("refusing to overwrite"));
        assert_eq!(
            std::fs::read(target.join("AGENTS.md")).expect("read unmanaged root"),
            b"unmanaged\n"
        );
        assert_eq!(std::fs::read_dir(&target).expect("read target").count(), 1);
    }

    #[test]
    fn recovery_residue_injected_before_lock_is_rejected_under_lock() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (bundle, packages) = core_bundle();
        let residue = format!("{INSTALL_STAGE_PREFIX}interrupted");

        let error = install_source_bundle_with_options(
            &bundle,
            &packages,
            &target,
            false,
            None,
            |target| {
                std::fs::create_dir(target)?;
                std::fs::create_dir(target.join(&residue))?;
                Ok(())
            },
        )
        .expect_err("locked preflight rejects recovery residue");
        assert!(
            error
                .to_string()
                .contains("lifecycle recovery state requires attention")
        );
        assert!(target.join(&residue).is_dir());
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        for root in MANAGED_ROOTS {
            assert!(!target.join(root).exists());
        }
    }

    #[test]
    fn fresh_apple_install_activates_the_same_frozen_native_launcher() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let root = repository_root();
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &[],
            &["codex".to_owned()],
            false,
        )
        .expect("resolve Apple source selection");
        let packages = snapshot_source_packages(&selection).expect("snapshot Apple packages");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile Apple bundle");
        let launcher = b"frozen native session launcher\n";

        let outcome = install_source_bundle_with_activation(&bundle, &packages, &target, launcher)
            .expect("install activated Apple bundle");
        assert_eq!(outcome["install_plan"]["status"], "installed");
        assert_eq!(
            outcome["activation"]["handler"],
            "core.source-activation.apple-codex-v1"
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-session")).expect("read installed launcher"),
            launcher
        );
        assert!(target.join(".agent-skills/activation-lock.json").is_file());
    }
}
