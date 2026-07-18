use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE, configure_nofollow, load_json_file,
    open_child_directory, open_child_file, packages, post_install, same_content_state_cap,
    same_object_cap, staged_tree,
};
use agent_contracts::canonical_json;
use agent_engine::{install_plan_identity_hash, validate_install_plan, validate_package_lock};
use cap_fs_ext::{FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, OpenOptions};
use serde_json::Value;
use sha2::{Digest as _, Sha256};
use std::collections::BTreeSet;
use std::io::Write as _;

const INSTALL_LOCK: &str = "install-lock.json";
const PACKAGE_LOCK: &str = "agent-skills.lock";

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct ExternalLayout {
    pub(super) activation: bool,
    pub(super) rollback_point: bool,
    pub(super) system_skills: bool,
}

/// An immutable Install Plan and persistent Lockfile pair accepted for staging.
///
/// Construction validates both complete contracts, normalizes the lifecycle
/// projection to `installed`, and binds the pair in both directions through the
/// plan's `package_lock_hash` and the Lockfile's
/// `install_plan_identity_hash`. Plan-owned tree records are intentionally not
/// exposed; staging methods resolve records through this token.
#[derive(Clone, Debug)]
pub struct ValidatedInstallPlan {
    install_plan: Value,
    install_plan_fingerprint: String,
    package_lock: Value,
    package_lock_fingerprint: String,
}

impl ValidatedInstallPlan {
    /// Validate and bind an Install Plan to its persistent package Lockfile.
    ///
    /// A `planned` input is normalized to the equivalent `installed`
    /// projection. The Install Plan fingerprint is status-independent by
    /// contract, so this does not rewrite its frozen identity.
    ///
    /// # Errors
    /// Returns a fail-closed error for malformed, unrelated, or incompletely
    /// anchored contracts.
    pub fn new(mut install_plan: Value, package_lock: Value) -> Result<Self, LifecycleError> {
        validate_install_plan(&install_plan)?;
        validate_package_lock(&package_lock)?;
        let plan = install_plan
            .as_object_mut()
            .ok_or_else(|| LifecycleError::Invalid("Install Plan must be an object".to_owned()))?;
        plan.insert("status".to_owned(), Value::String("installed".to_owned()));
        validate_install_plan(&install_plan)?;
        if install_plan.get("package_lock_hash") != package_lock.get("fingerprint") {
            return invalid("Install Plan package lock hash differs from persistent Lockfile");
        }
        if package_lock
            .get("install_plan_identity_hash")
            .and_then(Value::as_str)
            != Some(install_plan_identity_hash(&install_plan)?.as_str())
        {
            return invalid("persistent Lockfile differs from Install Plan identity");
        }
        let install_plan_fingerprint = install_plan
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid("Install Plan fingerprint is invalid".to_owned())
            })?
            .to_owned();
        let package_lock_fingerprint = package_lock
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid("persistent Lockfile fingerprint is invalid".to_owned())
            })?
            .to_owned();
        Ok(Self {
            install_plan,
            install_plan_fingerprint,
            package_lock,
            package_lock_fingerprint,
        })
    }

    /// Return the frozen Install Plan fingerprint.
    #[must_use]
    pub fn fingerprint(&self) -> &str {
        &self.install_plan_fingerprint
    }

    /// Return the frozen persistent Lockfile fingerprint.
    #[must_use]
    pub fn package_lock_fingerprint(&self) -> &str {
        &self.package_lock_fingerprint
    }

    fn package(&self, package_id: &str) -> Result<&Value, LifecycleError> {
        find_record(&self.install_plan, "packages", "id", package_id, "package")
    }

    fn skill(&self, skill_name: &str) -> Result<&Value, LifecycleError> {
        find_record(&self.install_plan, "skills", "name", skill_name, "Skill")
    }

    fn package_ids(&self) -> Result<BTreeSet<String>, LifecycleError> {
        record_ids(
            &self.install_plan,
            "packages",
            "id",
            "Install Plan packages",
        )
    }

    fn skill_names(&self) -> Result<BTreeSet<String>, LifecycleError> {
        record_ids(&self.install_plan, "skills", "name", "Install Plan Skills")
    }
}

pub(super) fn stage_layout(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
    instructions: &[u8],
) -> Result<(), LifecycleError> {
    let expected_hash = plan
        .install_plan
        .pointer("/instructions/sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("Install Plan instructions are invalid".to_owned())
        })?;
    if bytes_sha256(instructions) != expected_hash {
        return invalid("staged AGENTS.md differs from Install Plan");
    }
    require_empty_directory(stage, "lifecycle stage")?;
    let skills = create_directory(
        stage,
        "skills",
        MANAGED_DIRECTORY_MODE,
        "staged Skills root",
    )?;
    let managed = create_directory(
        stage,
        ".agent-skills",
        MANAGED_DIRECTORY_MODE,
        "staged managed metadata",
    )?;
    create_directory(
        &managed,
        "packages",
        MANAGED_DIRECTORY_MODE,
        "staged packages root",
    )?;
    write_file(
        stage,
        "AGENTS.md",
        instructions,
        MANAGED_FILE_MODE,
        "staged AGENTS.md",
    )?;
    write_file(
        &managed,
        PACKAGE_LOCK,
        &canonical_json(&plan.package_lock)?,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
    )?;
    write_file(
        &managed,
        INSTALL_LOCK,
        &canonical_json(&plan.install_plan)?,
        MANAGED_FILE_MODE,
        "staged Install Lock",
    )?;
    require_empty_directory(&skills, "staged Skills root")
}

pub(super) fn stage_package(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
    package_id: &str,
    source: &Dir,
) -> Result<(), LifecycleError> {
    staged_tree::stage_package(stage, source, plan.package(package_id)?)
}

pub(super) fn stage_skill(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
    skill_name: &str,
    source: &Dir,
) -> Result<(), LifecycleError> {
    staged_tree::stage_skill(stage, source, plan.skill(skill_name)?)
}

pub(super) fn validate_layout(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
) -> Result<(), LifecycleError> {
    require_names(
        stage,
        &set([".agent-skills", "AGENTS.md", "skills"]),
        "staged managed roots differ from Install Plan",
    )?;
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    require_names(
        &managed,
        &set([INSTALL_LOCK, PACKAGE_LOCK, "packages"]),
        "staged managed metadata contains unverified entries",
    )?;
    open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "staged packages root",
    )?;
    open_child_directory(
        stage,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged Skills root",
    )?;
    if strict_file_hash(stage, "AGENTS.md", MANAGED_FILE_MODE, "staged AGENTS.md")?
        != plan
            .install_plan
            .pointer("/instructions/sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid("Install Plan instructions are invalid".to_owned())
            })?
    {
        return invalid("staged AGENTS.md differs from Install Plan");
    }
    if load_json_file(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "staged Install Lock",
    )? != plan.install_plan
    {
        return invalid("staged Install Lock differs from validated Install Plan");
    }
    if load_json_file(
        &managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
    )? != plan.package_lock
    {
        return invalid("staged persistent Lockfile differs from validated package Lockfile");
    }
    if !strict_child_bytes_equal(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "staged Install Lock",
        &canonical_json(&plan.install_plan)?,
    )? {
        return invalid("staged Install Lock is not canonical");
    }
    if !strict_child_bytes_equal(
        &managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
        &canonical_json(&plan.package_lock)?,
    )? {
        return invalid("staged persistent Lockfile is not canonical");
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
pub(super) fn verify(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
    external: ExternalLayout,
) -> Result<(), LifecycleError> {
    verify_with_scope(stage, plan, external, true)
}

pub(super) fn verify_published(
    target: &Dir,
    plan: &ValidatedInstallPlan,
    external: ExternalLayout,
) -> Result<(), LifecycleError> {
    verify_with_scope(target, plan, external, false)
}

#[allow(clippy::too_many_lines)]
fn verify_with_scope(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
    external: ExternalLayout,
    exact_root: bool,
) -> Result<(), LifecycleError> {
    if exact_root {
        if external == ExternalLayout::default() {
            validate_layout(stage, plan)?;
        } else {
            validate_external_layout(stage, plan, external)?;
        }
    } else {
        validate_published_layout(stage, plan, external)?;
    }
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    let managed_identity = managed.dir_metadata()?;
    let managed_names = managed_names(external);
    require_names(
        &managed,
        &managed_names,
        "staged managed metadata contains unverified entries",
    )?;
    let packages_root = open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "staged packages root",
    )?;
    let packages_identity = packages_root.dir_metadata()?;
    require_names(
        &packages_root,
        &plan.package_ids()?,
        "staged package set differs from Install Plan",
    )?;
    let skills_root = open_child_directory(
        stage,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged Skills root",
    )?;
    let skills_identity = skills_root.dir_metadata()?;
    let skill_names = skill_names(plan, external)?;
    require_names(
        &skills_root,
        &skill_names,
        "staged Skill set differs from Install Plan",
    )?;
    if load_json_file(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "staged Install Lock",
    )? != plan.install_plan
    {
        return invalid("staged Install Lock differs from validated Install Plan");
    }
    if load_json_file(
        &managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
    )? != plan.package_lock
    {
        return invalid("staged persistent Lockfile differs from validated package Lockfile");
    }
    if !strict_child_bytes_equal(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "staged Install Lock",
        &canonical_json(&plan.install_plan)?,
    )? {
        return invalid("staged Install Lock is not canonical");
    }
    if !strict_child_bytes_equal(
        &managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
        &canonical_json(&plan.package_lock)?,
    )? {
        return invalid("staged persistent Lockfile is not canonical");
    }
    for package_id in plan.package_ids()? {
        staged_tree::verify_package(stage, plan.package(&package_id)?)?;
    }
    for skill_name in plan.skill_names()? {
        staged_tree::verify_skill(stage, plan.skill(&skill_name)?)?;
    }

    let mut semantics = None;
    packages::check_package_integrity(
        stage,
        &plan.install_plan,
        &plan.package_lock,
        &mut semantics,
    )?;
    let expected_instructions = semantics
        .as_ref()
        .and_then(|value| value.pointer("/instructions/content"))
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid(
                "staged instruction semantics were not reconstructed".to_owned(),
            )
        })?;
    if !strict_child_bytes_equal(
        stage,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "staged AGENTS.md",
        expected_instructions.as_bytes(),
    )? {
        return invalid("staged AGENTS.md differs from validated Install Plan semantics");
    }
    post_install::check_skill_integrity(stage, &plan.install_plan, semantics.as_ref())?;
    post_install::check_global_instructions(
        stage,
        &plan.install_plan,
        &plan.package_lock,
        semantics.as_ref(),
    )?;
    post_install::check_binding_freeze(&plan.install_plan, &plan.package_lock, semantics.as_ref())?;
    post_install::check_permission_freeze(
        &plan.install_plan,
        &plan.package_lock,
        semantics.as_ref(),
    )?;

    if exact_root {
        require_names(
            stage,
            &set([".agent-skills", "AGENTS.md", "skills"]),
            "staged managed roots changed while verifying",
        )?;
    }
    let final_managed = revalidate_directory(
        stage,
        ".agent-skills",
        &managed_identity,
        MANAGED_DIRECTORY_MODE,
        "staged managed metadata",
    )?;
    require_names(
        &final_managed,
        &managed_names,
        "staged managed metadata changed while verifying",
    )?;
    let final_packages = revalidate_directory(
        &final_managed,
        "packages",
        &packages_identity,
        MANAGED_DIRECTORY_MODE,
        "staged packages root",
    )?;
    require_names(
        &final_packages,
        &plan.package_ids()?,
        "staged package set changed while verifying",
    )?;
    let final_skills = revalidate_directory(
        stage,
        "skills",
        &skills_identity,
        MANAGED_DIRECTORY_MODE,
        "staged Skills root",
    )?;
    require_names(
        &final_skills,
        &skill_names,
        "staged Skill set changed while verifying",
    )?;
    if !strict_child_bytes_equal(
        &final_managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "staged Install Lock",
        &canonical_json(&plan.install_plan)?,
    )? || !strict_child_bytes_equal(
        &final_managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
        &canonical_json(&plan.package_lock)?,
    )? || !strict_child_bytes_equal(
        stage,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "staged AGENTS.md",
        expected_instructions.as_bytes(),
    )? {
        return invalid("staged managed metadata changed while verifying");
    }
    Ok(())
}

fn validate_published_layout(
    target: &Dir,
    plan: &ValidatedInstallPlan,
    external: ExternalLayout,
) -> Result<(), LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "published managed metadata",
    )?;
    require_names(
        &managed,
        &managed_names(external),
        "published managed metadata contains unverified entries",
    )?;
    open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "published packages root",
    )?;
    let skills = open_child_directory(
        target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "published Skills root",
    )?;
    require_names(
        &skills,
        &skill_names(plan, external)?,
        "published Skill set differs from Install Plan",
    )?;
    if strict_file_hash(
        target,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "published AGENTS.md",
    )? != plan
        .install_plan
        .pointer("/instructions/sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("Install Plan instructions are invalid".to_owned())
        })?
    {
        return invalid("published AGENTS.md differs from Install Plan");
    }
    if load_json_file(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "published Install Lock",
    )? != plan.install_plan
        || !strict_child_bytes_equal(
            &managed,
            INSTALL_LOCK,
            MANAGED_FILE_MODE,
            "published Install Lock",
            &canonical_json(&plan.install_plan)?,
        )?
    {
        return invalid("published Install Lock differs from validated Install Plan");
    }
    if load_json_file(
        &managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "published persistent package Lockfile",
    )? != plan.package_lock
        || !strict_child_bytes_equal(
            &managed,
            PACKAGE_LOCK,
            MANAGED_FILE_MODE,
            "published persistent package Lockfile",
            &canonical_json(&plan.package_lock)?,
        )?
    {
        return invalid("published persistent Lockfile differs from validated package Lockfile");
    }
    Ok(())
}

fn validate_external_layout(
    stage: &Dir,
    plan: &ValidatedInstallPlan,
    external: ExternalLayout,
) -> Result<(), LifecycleError> {
    require_names(
        stage,
        &set([".agent-skills", "AGENTS.md", "skills"]),
        "staged managed roots differ from Install Plan",
    )?;
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    require_names(
        &managed,
        &managed_names(external),
        "staged managed metadata contains unverified entries",
    )?;
    open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "staged packages root",
    )?;
    let skills = open_child_directory(
        stage,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged Skills root",
    )?;
    require_names(
        &skills,
        &skill_names(plan, external)?,
        "staged Skill set differs from Install Plan",
    )?;
    if strict_file_hash(stage, "AGENTS.md", MANAGED_FILE_MODE, "staged AGENTS.md")?
        != plan
            .install_plan
            .pointer("/instructions/sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid("Install Plan instructions are invalid".to_owned())
            })?
    {
        return invalid("staged AGENTS.md differs from Install Plan");
    }
    if load_json_file(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "staged Install Lock",
    )? != plan.install_plan
        || !strict_child_bytes_equal(
            &managed,
            INSTALL_LOCK,
            MANAGED_FILE_MODE,
            "staged Install Lock",
            &canonical_json(&plan.install_plan)?,
        )?
    {
        return invalid("staged Install Lock differs from validated Install Plan");
    }
    if load_json_file(
        &managed,
        PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "staged persistent package Lockfile",
    )? != plan.package_lock
        || !strict_child_bytes_equal(
            &managed,
            PACKAGE_LOCK,
            MANAGED_FILE_MODE,
            "staged persistent package Lockfile",
            &canonical_json(&plan.package_lock)?,
        )?
    {
        return invalid("staged persistent Lockfile differs from validated package Lockfile");
    }
    Ok(())
}

fn managed_names(external: ExternalLayout) -> BTreeSet<String> {
    let mut names = set([INSTALL_LOCK, PACKAGE_LOCK, "packages"]);
    if external.activation {
        names.insert("activation-lock.json".to_owned());
    }
    if external.rollback_point {
        names.insert("rollback-point".to_owned());
    }
    names
}

fn skill_names(
    plan: &ValidatedInstallPlan,
    external: ExternalLayout,
) -> Result<BTreeSet<String>, LifecycleError> {
    let mut names = plan.skill_names()?;
    if external.system_skills {
        names.insert(".system".to_owned());
    }
    Ok(names)
}

fn find_record<'a>(
    value: &'a Value,
    field: &str,
    key: &str,
    expected: &str,
    label: &str,
) -> Result<&'a Value, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .and_then(|records| {
            records
                .iter()
                .find(|record| record.get(key).and_then(Value::as_str) == Some(expected))
        })
        .ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "{label} is not selected by validated Install Plan: {expected}"
            ))
        })
}

fn record_ids(
    value: &Value,
    field: &str,
    key: &str,
    label: &str,
) -> Result<BTreeSet<String>, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} are invalid")))?
        .iter()
        .map(|record| {
            record
                .get(key)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| LifecycleError::Invalid(format!("{label} are invalid")))
        })
        .collect()
}

fn set<const N: usize>(items: [&str; N]) -> BTreeSet<String> {
    items.into_iter().map(str::to_owned).collect()
}

fn require_names(
    directory: &Dir,
    expected: &BTreeSet<String>,
    message: &str,
) -> Result<(), LifecycleError> {
    let actual = directory
        .entries()?
        .map(|entry| entry.map(|entry| entry.file_name().to_string_lossy().into_owned()))
        .collect::<Result<BTreeSet<_>, _>>()?;
    if &actual != expected {
        return invalid(message);
    }
    Ok(())
}

fn require_empty_directory(directory: &Dir, label: &str) -> Result<(), LifecycleError> {
    if directory.entries()?.next().transpose()?.is_some() {
        return invalid(format!("{label} must be empty"));
    }
    Ok(())
}

fn revalidate_directory(
    parent: &Dir,
    name: &str,
    original: &cap_std::fs::Metadata,
    mode: u32,
    label: &str,
) -> Result<Dir, LifecycleError> {
    let current = open_child_directory(parent, name, Some(mode), label)?;
    let metadata = current.dir_metadata()?;
    if !same_object_cap(original, &metadata) || !same_content_state_cap(original, &metadata) {
        return invalid(format!("{label} changed while verifying"));
    }
    Ok(current)
}

fn strict_file_hash(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<String, LifecycleError> {
    let original = single_link_file_metadata(parent, name, mode, label)?;
    let hash = packages::hash_child_file(parent, name, mode, label)?;
    let current = single_link_file_metadata(parent, name, mode, label)?;
    if !same_object_cap(&original, &current) || !same_content_state_cap(&original, &current) {
        return invalid(format!("{label} changed while hashing"));
    }
    Ok(hash)
}

fn strict_child_bytes_equal(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
    expected: &[u8],
) -> Result<bool, LifecycleError> {
    let original = single_link_file_metadata(parent, name, mode, label)?;
    let equal = post_install::child_bytes_equal(parent, name, mode, label, expected)?;
    let current = single_link_file_metadata(parent, name, mode, label)?;
    if !same_object_cap(&original, &current) || !same_content_state_cap(&original, &current) {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(equal)
}

fn single_link_file_metadata(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<cap_std::fs::Metadata, LifecycleError> {
    let metadata = open_child_file(parent, name, mode, label)?.metadata()?;
    if metadata.nlink() != 1 {
        return invalid(format!("{label} has an unsafe hard-link alias"));
    }
    Ok(metadata)
}

fn create_directory(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<Dir, LifecycleError> {
    let result = {
        #[cfg(all(unix, not(target_os = "wasi")))]
        {
            use cap_std::fs::{DirBuilder, DirBuilderExt as _};

            let mut builder = DirBuilder::new();
            builder.mode(mode);
            parent.create_dir_with(name, &builder)
        }
        #[cfg(any(not(unix), target_os = "wasi"))]
        {
            parent.create_dir(name)
        }
    };
    match result {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            return invalid(format!("{label} already exists"));
        }
        Err(error) => return Err(error.into()),
    }
    let directory = open_child_directory(parent, name, None, label)?;
    let original = directory.dir_metadata()?;
    #[cfg(unix)]
    {
        use cap_std::fs::{Permissions, PermissionsExt as _};
        directory.set_permissions(".", Permissions::from_mode(mode))?;
    }
    let current = open_child_directory(parent, name, Some(mode), label)?.dir_metadata()?;
    if !same_object_cap(&original, &current) {
        return invalid(format!("{label} changed while creating"));
    }
    Ok(directory)
}

fn write_file(
    parent: &Dir,
    name: &str,
    bytes: &[u8],
    mode: u32,
    label: &str,
) -> Result<(), LifecycleError> {
    #[cfg(not(unix))]
    let _ = mode;
    let mut options = OpenOptions::new();
    options
        .write(true)
        .create_new(true)
        .follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt as _;
        options.mode(mode);
    }
    let mut file = parent.open_with(name, &options).map_err(|error| {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            LifecycleError::Invalid(format!("{label} already exists"))
        } else {
            error.into()
        }
    })?;
    let original = file.metadata()?;
    file.write_all(bytes)?;
    file.flush()?;
    #[cfg(unix)]
    {
        use cap_std::fs::{Permissions, PermissionsExt as _};
        file.set_permissions(Permissions::from_mode(mode))?;
    }
    let completed = file.metadata()?;
    let current = open_child_file(parent, name, mode, label)?.metadata()?;
    if !same_object_cap(&original, &completed)
        || !same_object_cap(&original, &current)
        || !same_content_state_cap(&completed, &current)
        || completed.nlink() != 1
        || current.nlink() != 1
    {
        return invalid(format!("{label} changed while writing"));
    }
    Ok(())
}

fn bytes_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{LIFECYCLE_LOCK_DIRECTORY, LifecycleWorkspace};
    use agent_contracts::{canonical_sha256, load_json};
    use agent_engine::resolve_package_lock;
    use cap_std::ambient_authority;
    use serde_json::json;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);
    const INSTRUCTIONS: &str = concat!(
        "<!-- agent-development-skills:managed instructions-v1 -->\n",
        "# 全局 Agent Instructions\n\n",
        "> 此文件由 `agent-skills install` 确定性生成；请在源 Fragment 中修改。\n\n"
    );

    struct Fixture {
        root: PathBuf,
        source: PathBuf,
        token: ValidatedInstallPlan,
    }

    impl Fixture {
        fn new() -> Self {
            Self::with_version("0.1.0")
        }

        #[allow(clippy::too_many_lines)]
        fn with_version(version: &str) -> Self {
            let root = temporary_path("fixture");
            let source = root.join("source");
            std::fs::create_dir_all(&source).expect("create source");
            let manifest = json!({
                "bindings": {},
                "capabilities": [],
                "detection": {"medium": [], "strong": [], "weak": []},
                "id": "core",
                "installation": {
                    "asset_roots": [],
                    "instruction_fragments": [],
                    "skill_roots": [],
                },
                "kind": "adapter",
                "package_requires": [],
                "schema_version": "1.0",
                "version": version,
            });
            let manifest_bytes = canonical_json(&manifest).expect("encode manifest");
            std::fs::write(source.join("manifest.json"), &manifest_bytes).expect("write manifest");
            let file = json!({
                "mode": 0o644,
                "path": "manifest.json",
                "sha256": bytes_sha256(&manifest_bytes),
            });
            let files = json!([file]);
            let files_sha256 = canonical_sha256(&files).expect("hash package files");
            let manifest_sha256 = canonical_sha256(&manifest).expect("hash manifest");
            let assets = json!([{
                "mode": 0o644,
                "package": "core",
                "path": "manifest.json",
                "sha256": bytes_sha256(&manifest_bytes),
            }]);
            let mut plan = json!({
                "asset_summary": {
                    "content_sha256": canonical_sha256(&assets).expect("hash assets"),
                    "file_count": 1,
                    "package_count": 1,
                    "skill_count": 0,
                },
                "assets": assets,
                "bindings": {},
                "capability_providers": {},
                "core_version": env!("CARGO_PKG_VERSION"),
                "fingerprint": Value::Null,
                "instructions": {
                    "fragments": [],
                    "path": "AGENTS.md",
                    "rule_trace": [],
                    "sha256": bytes_sha256(INSTRUCTIONS.as_bytes()),
                },
                "lock_schema_version": "2.0",
                "managed_roots": ["AGENTS.md", "skills", ".agent-skills"],
                "manager": "agent-development-skills",
                "package_lock_hash": Value::Null,
                "packages": [{
                    "directories": [],
                    "file_count": 1,
                    "files": files,
                    "files_sha256": files_sha256,
                    "id": "core",
                    "manifest_sha256": manifest_sha256,
                    "provider_manifest_sha256": Value::Null,
                    "root_mode": 0o755,
                }],
                "permission_profiles": [],
                "resolved_dependencies": [],
                "schema_version": "1.0",
                "selected_disciplines": [],
                "selected_packages": [{
                    "core_compatibility": format!("=={}", env!("CARGO_PKG_VERSION")),
                    "id": "core",
                    "kind": "core",
                    "provider_compatibility": Value::Null,
                    "provider_version": Value::Null,
                    "selection_reasons": ["core"],
                    "source_sha256": files_sha256,
                    "version": version,
                }],
                "selected_platforms": [],
                "selected_runtime_configs": [],
                "side_effects": [],
                "skills": [],
                "status": "planned",
            });
            refresh_plan_fingerprint(&mut plan);
            validate_install_plan(&plan).expect("validate unbound plan");
            let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../..")
                .canonicalize()
                .expect("canonical repository");
            let package_lock =
                resolve_package_lock(&plan, repository.join("schemas"), None, None, &root, None)
                    .expect("resolve package lock");
            plan["package_lock_hash"] = package_lock["fingerprint"].clone();
            refresh_plan_fingerprint(&mut plan);
            let token = ValidatedInstallPlan::new(plan, package_lock).expect("bind validated plan");
            Self {
                root,
                source,
                token,
            }
        }

        fn target(&self) -> PathBuf {
            self.root.join("target")
        }

        fn source_directory(&self) -> Dir {
            Dir::open_ambient_dir(&self.source, ambient_authority()).expect("open source")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    fn stage_complete_managed_layout(fixture: &Fixture) -> (Dir, LifecycleWorkspace) {
        let source = fixture.source_directory();
        let mut workspace =
            LifecycleWorkspace::begin(fixture.target()).expect("begin lifecycle workspace");
        workspace
            .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
            .expect("stage managed layout");
        workspace
            .stage_plan_package(&fixture.token, "core", &source)
            .expect("stage plan package");
        (source, workspace)
    }

    fn materialize_current_install(fixture: &Fixture) {
        let target = fixture.target();
        let managed = target.join(".agent-skills");
        let package = managed.join("packages/core");
        std::fs::create_dir_all(&package).expect("create installed package");
        std::fs::create_dir_all(target.join("skills")).expect("create installed Skills root");
        std::fs::copy(
            fixture.source.join("manifest.json"),
            package.join("manifest.json"),
        )
        .expect("copy installed Manifest");
        std::fs::write(target.join("AGENTS.md"), INSTRUCTIONS.as_bytes())
            .expect("write installed AGENTS");
        std::fs::write(
            managed.join(INSTALL_LOCK),
            canonical_json(&fixture.token.install_plan).expect("encode installed Install Lock"),
        )
        .expect("write installed Install Lock");
        std::fs::write(
            managed.join(PACKAGE_LOCK),
            canonical_json(&fixture.token.package_lock).expect("encode installed package Lockfile"),
        )
        .expect("write installed package Lockfile");
        for directory in [&target, &managed, &managed.join("packages"), &package] {
            set_mode(directory, MANAGED_DIRECTORY_MODE);
        }
        set_mode(&target.join("skills"), MANAGED_DIRECTORY_MODE);
        for file in [
            target.join("AGENTS.md"),
            managed.join(INSTALL_LOCK),
            managed.join(PACKAGE_LOCK),
            package.join("manifest.json"),
        ] {
            set_mode(&file, MANAGED_FILE_MODE);
        }
    }

    fn write_activation_fixture(fixture: &Fixture, expected_hash: &str) -> Vec<u8> {
        let target = fixture.target();
        std::fs::create_dir_all(target.join(".agent-skills"))
            .expect("create target managed metadata");
        std::fs::create_dir_all(target.join("bin")).expect("create activation parent");
        std::fs::write(target.join("bin/tool"), b"external tool\n").expect("write activated file");
        set_mode(&target.join(".agent-skills"), 0o755);
        set_mode(&target.join("bin/tool"), 0o755);
        let lock = json!({
            "schema_version": "2.0",
            "manager": "agent-development-skills",
            "handler": "core.source-activation.apple-codex-v1",
            "files": [{
                "path": "bin/tool",
                "mode": 0o755,
                "sha256": expected_hash,
            }],
        });
        let bytes = serde_json::to_vec_pretty(&lock).expect("encode Activation Lock");
        let path = target.join(".agent-skills/activation-lock.json");
        std::fs::write(&path, &bytes).expect("write Activation Lock");
        set_mode(&path, 0o644);
        bytes
    }

    #[cfg(unix)]
    fn set_mode(path: &Path, mode: u32) {
        use std::os::unix::fs::PermissionsExt as _;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))
            .expect("set test mode");
    }

    #[cfg(not(unix))]
    fn set_mode(_path: &Path, _mode: u32) {}

    #[test]
    fn validated_plan_stages_and_verifies_complete_managed_layout() {
        let fixture = Fixture::new();
        let source = fixture.source_directory();
        let mut workspace = LifecycleWorkspace::begin(fixture.target()).expect("begin workspace");
        workspace
            .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
            .expect("stage layout");
        workspace
            .stage_plan_package(&fixture.token, "core", &source)
            .expect("stage plan package");
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .verify_staged_install(&fixture.token)
            .expect("verify complete stage");
        let install_lock = load_json(
            workspace
                .stage_path()
                .join(".agent-skills/install-lock.json"),
        )
        .expect("load staged Install Lock");
        assert_eq!(
            install_lock.get("status").and_then(Value::as_str),
            Some("installed")
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn validated_plan_rejects_an_individually_valid_unrelated_lock_anchor() {
        let fixture = Fixture::new();
        let mut plan = fixture.token.install_plan.clone();
        plan["package_lock_hash"] = Value::String("0".repeat(64));
        refresh_plan_fingerprint(&mut plan);
        validate_install_plan(&plan).expect("mutated plan remains structurally valid");
        assert_eq!(
            ValidatedInstallPlan::new(plan, fixture.token.package_lock.clone())
                .expect_err("unrelated lock must fail")
                .to_string(),
            "Install Plan package lock hash differs from persistent Lockfile"
        );
    }

    #[test]
    fn validated_plan_rejects_a_lock_with_a_different_plan_identity() {
        let fixture = Fixture::new();
        let mut package_lock = fixture.token.package_lock.clone();
        package_lock["install_plan_identity_hash"] = Value::String("0".repeat(64));
        refresh_package_lock_fingerprint(&mut package_lock);
        validate_package_lock(&package_lock).expect("mutated lock remains structurally valid");
        let mut plan = fixture.token.install_plan.clone();
        plan["package_lock_hash"] = package_lock["fingerprint"].clone();
        refresh_plan_fingerprint(&mut plan);
        validate_install_plan(&plan).expect("reanchored plan remains structurally valid");
        assert_eq!(
            ValidatedInstallPlan::new(plan, package_lock)
                .expect_err("unrelated plan identity must fail")
                .to_string(),
            "persistent Lockfile differs from Install Plan identity"
        );
    }

    #[test]
    fn plan_bound_staging_rejects_unknown_records_and_extra_external_roots() {
        let fixture = Fixture::new();
        let source = fixture.source_directory();
        let mut workspace = LifecycleWorkspace::begin(fixture.target()).expect("begin workspace");
        workspace
            .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
            .expect("stage layout");
        assert_eq!(
            workspace
                .stage_plan_package(&fixture.token, "forged", &source)
                .expect_err("unknown package must fail")
                .to_string(),
            "package is not selected by validated Install Plan: forged"
        );
        workspace
            .stage_plan_package(&fixture.token, "core", &source)
            .expect("stage package");
        std::fs::create_dir(workspace.stage_path().join("skills/.system"))
            .expect("inject external root");
        let error = workspace
            .stage_external_state(&fixture.token)
            .expect_err("unfrozen external root must fail");
        assert_eq!(
            error.to_string(),
            "staged Skill set differs from Install Plan"
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn plan_bound_staging_requires_layout_and_its_frozen_token() {
        let fixture = Fixture::new();
        let different = Fixture::with_version("0.1.1");
        let source = fixture.source_directory();
        let mut workspace = LifecycleWorkspace::begin(fixture.target()).expect("begin workspace");
        assert_eq!(
            workspace
                .stage_plan_package(&fixture.token, "core", &source)
                .expect_err("layout must precede plan staging")
                .to_string(),
            "lifecycle workspace has no staged Install Plan layout"
        );
        workspace
            .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
            .expect("stage layout");
        assert_eq!(
            workspace
                .stage_plan_package(&different.token, "core", &source)
                .expect_err("different plan token must fail")
                .to_string(),
            "validated Install Plan differs from staged workspace identity"
        );
        workspace
            .stage_plan_package(&fixture.token, "core", &source)
            .expect("matching token stages package");
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .verify_staged_install(&fixture.token)
            .expect("matching token verifies");
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn complete_stage_verification_rejects_lockfile_tampering() {
        let fixture = Fixture::new();
        let source = fixture.source_directory();
        let mut workspace = LifecycleWorkspace::begin(fixture.target()).expect("begin workspace");
        workspace
            .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
            .expect("stage layout");
        workspace
            .stage_plan_package(&fixture.token, "core", &source)
            .expect("stage package");
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let lock_path = workspace
            .stage_path()
            .join(".agent-skills/agent-skills.lock");
        let mut package_lock = load_json(&lock_path).expect("load package lock");
        package_lock["lineage"]["previous_lock_hash"] = Value::String("0".repeat(64));
        std::fs::write(
            &lock_path,
            canonical_json(&package_lock).expect("encode tampered lock"),
        )
        .expect("tamper package lock");
        let error = workspace
            .verify_staged_install(&fixture.token)
            .expect_err("tampered lock must fail");
        assert_eq!(
            error.to_string(),
            "staged persistent Lockfile differs from validated package Lockfile"
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn complete_stage_verification_rejects_noncanonical_lockfile_bytes() {
        let fixture = Fixture::new();
        let source = fixture.source_directory();
        let mut workspace = LifecycleWorkspace::begin(fixture.target()).expect("begin workspace");
        workspace
            .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
            .expect("stage layout");
        workspace
            .stage_plan_package(&fixture.token, "core", &source)
            .expect("stage package");
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let lock_path = workspace
            .stage_path()
            .join(".agent-skills/agent-skills.lock");
        let package_lock = load_json(&lock_path).expect("load package lock");
        let noncanonical =
            serde_json::to_vec_pretty(&package_lock).expect("encode noncanonical lock");
        std::fs::write(&lock_path, noncanonical).expect("rewrite package lock");
        let error = workspace
            .verify_staged_install(&fixture.token)
            .expect_err("noncanonical lock must fail");
        assert_eq!(
            error.to_string(),
            "staged persistent Lockfile is not canonical"
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn complete_stage_verification_rejects_post_stage_managed_file_aliases() {
        for relative in [
            "AGENTS.md",
            ".agent-skills/install-lock.json",
            ".agent-skills/agent-skills.lock",
        ] {
            let fixture = Fixture::new();
            let source = fixture.source_directory();
            let mut workspace =
                LifecycleWorkspace::begin(fixture.target()).expect("begin workspace");
            workspace
                .stage_install_layout(&fixture.token, INSTRUCTIONS.as_bytes())
                .expect("stage layout");
            workspace
                .stage_plan_package(&fixture.token, "core", &source)
                .expect("stage package");
            workspace
                .stage_external_state(&fixture.token)
                .expect("stage empty external state");
            let alias = fixture
                .root
                .join(format!("alias-{}", relative.replace(['/', '.'], "-")));
            std::fs::hard_link(workspace.stage_path().join(relative), &alias)
                .expect("create post-stage hard-link alias");
            let error = workspace
                .verify_staged_install(&fixture.token)
                .expect_err("managed file alias must fail");
            assert!(
                error.to_string().contains("has an unsafe hard-link alias"),
                "{relative}: {error}"
            );
            std::fs::remove_file(alias).expect("remove post-stage hard-link alias");
            drop(source);
            workspace.cleanup().expect("cleanup workspace");
        }
    }

    #[test]
    fn external_system_tree_is_preserved_without_following_symlinks() {
        let fixture = Fixture::new();
        let system = fixture.target().join("skills/.system");
        std::fs::create_dir_all(system.join("nested")).expect("create external .system tree");
        std::fs::write(system.join("nested/tool"), b"system tool\n")
            .expect("write external .system file");
        #[cfg(unix)]
        std::os::unix::fs::symlink("../missing-target", system.join("tool-link"))
            .expect("create external .system symlink");
        set_mode(&system, 0o555);
        set_mode(&system.join("nested"), 0o555);
        set_mode(&system.join("nested/tool"), 0o751);

        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage external .system tree");
        workspace
            .verify_staged_install(&fixture.token)
            .expect("verify external .system tree");
        let staged_system = workspace.stage_path().join("skills/.system");
        assert_eq!(
            std::fs::read(staged_system.join("nested/tool")).expect("read staged external file"),
            b"system tool\n"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                std::fs::read_link(staged_system.join("tool-link")).expect("read staged symlink"),
                PathBuf::from("../missing-target")
            );
            assert_eq!(
                std::fs::metadata(&staged_system)
                    .expect("inspect staged .system root")
                    .permissions()
                    .mode()
                    & 0o7777,
                0o555
            );
            assert_eq!(
                std::fs::metadata(staged_system.join("nested/tool"))
                    .expect("inspect staged external file")
                    .permissions()
                    .mode()
                    & 0o7777,
                0o751
            );
        }
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn external_system_source_and_stage_drift_are_rejected() {
        for drift_stage in [false, true] {
            let fixture = Fixture::new();
            let system = fixture.target().join("skills/.system");
            std::fs::create_dir_all(&system).expect("create external .system tree");
            std::fs::write(system.join("state"), b"before\n").expect("write external state");
            let (source, mut workspace) = stage_complete_managed_layout(&fixture);
            workspace
                .stage_external_state(&fixture.token)
                .expect("stage external state");
            let changed = if drift_stage {
                workspace.stage_path().join("skills/.system/state")
            } else {
                system.join("state")
            };
            std::fs::write(changed, b"after\n").expect("tamper external state");
            let error = workspace
                .verify_staged_install(&fixture.token)
                .expect_err("external state drift must fail");
            assert!(
                error.to_string().contains(if drift_stage {
                    "staged .system Skills differ"
                } else {
                    "target .system Skills changed"
                }),
                "{error}"
            );
            drop(source);
            workspace.cleanup().expect("cleanup workspace");
        }
    }

    #[test]
    fn staged_external_system_files_reject_hard_link_aliases() {
        let fixture = Fixture::new();
        let system = fixture.target().join("skills/.system");
        std::fs::create_dir_all(&system).expect("create external .system tree");
        std::fs::write(system.join("state"), b"state\n").expect("write external state");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage external state");
        let alias = fixture.root.join("external-state-alias");
        std::fs::hard_link(workspace.stage_path().join("skills/.system/state"), &alias)
            .expect("create staged external hard-link alias");
        let error = workspace
            .verify_staged_install(&fixture.token)
            .expect_err("staged external alias must fail");
        assert!(
            error.to_string().contains("unsafe hard-link alias"),
            "{error}"
        );
        std::fs::remove_file(alias).expect("remove staged external hard-link alias");
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn valid_activation_lock_is_preserved_byte_for_byte() {
        let fixture = Fixture::new();
        let expected_hash = bytes_sha256(b"external tool\n");
        let activation_bytes = write_activation_fixture(&fixture, &expected_hash);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage Activation state");
        workspace
            .verify_staged_install(&fixture.token)
            .expect("verify Activation state");
        assert_eq!(
            std::fs::read(
                workspace
                    .stage_path()
                    .join(".agent-skills/activation-lock.json")
            )
            .expect("read staged Activation Lock"),
            activation_bytes
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn invalid_activation_contract_is_rejected_before_preservation() {
        let fixture = Fixture::new();
        write_activation_fixture(&fixture, &"0".repeat(64));
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        let error = workspace
            .stage_external_state(&fixture.token)
            .expect_err("invalid activated file hash must fail");
        assert_eq!(error.to_string(), "activated file differs: bin/tool");
        assert!(
            !workspace
                .stage_path()
                .join(".agent-skills/activation-lock.json")
                .exists()
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn external_state_freeze_is_ordered_and_single_use() {
        let fixture = Fixture::new();
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        assert_eq!(
            workspace
                .stage_external_state(&fixture.token)
                .expect_err("second external freeze must fail")
                .to_string(),
            "lifecycle workspace external state is already staged"
        );
        assert_eq!(
            workspace
                .stage_plan_package(&fixture.token, "core", &source)
                .expect_err("plan trees must freeze before external state")
                .to_string(),
            "lifecycle workspace external state is already staged"
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn intact_install_is_frozen_as_a_verified_rollback_point() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let fingerprint = workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        assert_eq!(fingerprint.len(), 64);
        workspace
            .verify_staged_install(&fixture.token)
            .expect("verify rollback-bearing stage");
        let point = load_json(
            workspace
                .stage_path()
                .join(".agent-skills/rollback-point/rollback-point.json"),
        )
        .expect("load rollback point");
        assert_eq!(
            point.get("fingerprint").and_then(Value::as_str),
            Some(fingerprint.as_str())
        );
        assert_eq!(
            std::fs::read(
                workspace
                    .stage_path()
                    .join(".agent-skills/rollback-point/packages/core/manifest.json")
            )
            .expect("read rollback package"),
            std::fs::read(fixture.source.join("manifest.json")).expect("read source Manifest")
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn rollback_point_freezes_external_files_and_absent_state() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        std::fs::create_dir_all(fixture.target().join("config")).expect("create external parent");
        std::fs::write(fixture.target().join("config/state"), b"before\n")
            .expect("write external state");
        set_mode(&fixture.target().join("config"), 0o750);
        set_mode(&fixture.target().join("config/state"), 0o640);
        let paths = vec!["config/missing".to_owned(), "config/state".to_owned()];
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &paths)
            .expect("stage external rollback state");
        let rollback = workspace.stage_path().join(".agent-skills/rollback-point");
        assert_eq!(
            std::fs::read(rollback.join("external-files/config/state"))
                .expect("read rollback external file"),
            b"before\n"
        );
        let state =
            load_json(rollback.join("external-state.json")).expect("load rollback external state");
        assert_eq!(
            state.pointer("/entries/0/state").and_then(Value::as_str),
            Some("absent")
        );
        assert_eq!(
            state.pointer("/entries/1/state").and_then(Value::as_str),
            Some("file")
        );
        std::fs::write(fixture.target().join("config/state"), b"after\n")
            .expect("drift external source");
        assert!(
            workspace
                .verify_staged_install(&fixture.token)
                .expect_err("external rollback source drift must fail")
                .to_string()
                .contains("rollback point source installation changed")
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn rollback_point_binds_activation_to_external_snapshot() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let expected_hash = bytes_sha256(b"external tool\n");
        let activation = write_activation_fixture(&fixture, &expected_hash);
        let paths = vec!["bin/tool".to_owned()];
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage Activation state");
        workspace
            .stage_rollback_point(&fixture.token, &paths)
            .expect("stage activation rollback point");
        let rollback = workspace.stage_path().join(".agent-skills/rollback-point");
        assert_eq!(
            std::fs::read(rollback.join("activation-lock.json"))
                .expect("read rollback Activation Lock"),
            activation
        );
        assert_eq!(
            std::fs::read(rollback.join("external-files/bin/tool"))
                .expect("read rollback activated file"),
            b"external tool\n"
        );
        workspace
            .verify_staged_install(&fixture.token)
            .expect("verify activation rollback point");
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn rollback_point_rejects_managed_source_drift_after_staging() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        std::fs::write(fixture.target().join("AGENTS.md"), b"changed\n")
            .expect("drift current AGENTS");
        let error = workspace
            .verify_staged_install(&fixture.token)
            .expect_err("managed rollback source drift must fail");
        assert!(error.to_string().contains("AGENTS"), "{error}");
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn rollback_point_rejects_unsorted_or_managed_external_paths() {
        for paths in [
            vec!["z/state".to_owned(), "a/state".to_owned()],
            vec!["AGENTS.md".to_owned()],
            vec![".agent-skills/state".to_owned()],
        ] {
            let fixture = Fixture::new();
            materialize_current_install(&fixture);
            let (source, mut workspace) = stage_complete_managed_layout(&fixture);
            workspace
                .stage_external_state(&fixture.token)
                .expect("stage empty external state");
            assert!(
                workspace
                    .stage_rollback_point(&fixture.token, &paths)
                    .is_err(),
                "{paths:?}"
            );
            drop(source);
            workspace.cleanup().expect("cleanup workspace");
        }
    }

    #[test]
    fn rollback_point_is_single_use_and_tamper_evident() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        assert_eq!(
            workspace
                .stage_rollback_point(&fixture.token, &[])
                .expect_err("second rollback point must fail")
                .to_string(),
            "lifecycle workspace rollback point is already staged"
        );
        std::fs::write(
            workspace
                .stage_path()
                .join(".agent-skills/rollback-point/external-state.json"),
            b"{}\n",
        )
        .expect("tamper rollback contract");
        assert!(
            workspace
                .verify_staged_install(&fixture.token)
                .expect_err("rollback tamper must fail")
                .to_string()
                .contains("rollback point external state")
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[test]
    fn rollback_point_rejects_post_stage_hard_link_aliases() {
        for (relative, external_paths) in [
            (
                ".agent-skills/rollback-point/AGENTS.md",
                Vec::<String>::new(),
            ),
            (
                ".agent-skills/rollback-point/packages/core/manifest.json",
                Vec::<String>::new(),
            ),
            (
                ".agent-skills/rollback-point/external-files/config/state",
                vec!["config/state".to_owned()],
            ),
        ] {
            let fixture = Fixture::new();
            materialize_current_install(&fixture);
            if !external_paths.is_empty() {
                std::fs::create_dir_all(fixture.target().join("config"))
                    .expect("create external parent");
                std::fs::write(fixture.target().join("config/state"), b"state\n")
                    .expect("write external state");
            }
            let (source, mut workspace) = stage_complete_managed_layout(&fixture);
            workspace
                .stage_external_state(&fixture.token)
                .expect("stage empty external state");
            workspace
                .stage_rollback_point(&fixture.token, &external_paths)
                .expect("stage rollback point");
            let alias = fixture.root.join(format!(
                "rollback-alias-{}",
                relative.replace(['/', '.'], "-")
            ));
            std::fs::hard_link(workspace.stage_path().join(relative), &alias)
                .expect("create rollback hard-link alias");
            let error = workspace
                .verify_staged_install(&fixture.token)
                .expect_err("rollback hard-link alias must fail");
            assert!(
                error.to_string().contains("unsafe hard-link alias"),
                "{relative}: {error}"
            );
            std::fs::remove_file(alias).expect("remove rollback hard-link alias");
            drop(source);
            workspace.cleanup().expect("cleanup workspace");
        }
    }

    #[test]
    fn fresh_managed_roots_publish_verify_and_commit_without_touching_unrelated_files() {
        let fixture = Fixture::new();
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        std::fs::write(fixture.target().join("user-note"), b"unmanaged\n")
            .expect("write unrelated target file");
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish fresh managed roots");
        assert_eq!(
            published.target().expect("published target"),
            fixture.target().canonicalize().expect("canonical target")
        );
        assert!(fixture.target().join("AGENTS.md").is_file());
        assert!(fixture.target().join("skills").is_dir());
        assert!(fixture.target().join(".agent-skills").is_dir());
        assert_eq!(
            std::fs::read(fixture.target().join("user-note")).expect("read unrelated target file"),
            b"unmanaged\n"
        );
        published
            .verify(&fixture.token)
            .expect("verify published install");
        published
            .commit(&fixture.token)
            .expect("commit published install");
        assert!(!stage.exists());
        assert!(!backup.exists());
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn dropping_uncommitted_fresh_publication_removes_managed_roots() {
        let fixture = Fixture::new();
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish fresh managed roots");
        drop(published);
        for name in ["AGENTS.md", "skills", ".agent-skills"] {
            assert!(!fixture.target().join(name).exists(), "{name}");
        }
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn published_upgrade_can_restore_all_previous_managed_roots() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        assert!(backup.join("AGENTS.md").is_file());
        assert!(backup.join("skills").is_dir());
        assert!(backup.join(".agent-skills").is_dir());
        assert!(
            fixture
                .target()
                .join(".agent-skills/rollback-point")
                .is_dir()
        );
        published
            .rollback()
            .expect("restore previous managed roots");
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read restored AGENTS"),
            original_agents
        );
        assert!(
            !fixture
                .target()
                .join(".agent-skills/rollback-point")
                .exists()
        );
        assert!(!stage.exists());
        assert!(!backup.exists());
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn dropping_uncommitted_publication_restores_previous_install() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        assert!(
            fixture
                .target()
                .join(".agent-skills/rollback-point")
                .is_dir()
        );
        drop(published);
        assert!(
            !fixture
                .target()
                .join(".agent-skills/rollback-point")
                .exists()
        );
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn published_content_tamper_makes_commit_roll_back() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        std::fs::write(fixture.target().join("AGENTS.md"), b"tampered\n")
            .expect("tamper published AGENTS");
        let error = published
            .commit(&fixture.token)
            .expect_err("tampered publication must not commit");
        assert!(error.to_string().contains("AGENTS"), "{error}");
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md"))
                .expect("read automatically restored AGENTS"),
            original_agents
        );
        assert!(
            !fixture
                .target()
                .join(".agent-skills/rollback-point")
                .exists()
        );
        drop(source);
    }

    #[test]
    fn partial_publication_failure_reverses_completed_moves() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let error = workspace
            .publish_staged_install_with_hook(&fixture.token, |name, phase| {
                if name == "skills" && phase == "publish" {
                    return Err(LifecycleError::Invalid(
                        "injected partial publication failure".to_owned(),
                    ));
                }
                Ok(())
            })
            .expect_err("partial publication must fail");
        assert!(
            error
                .to_string()
                .contains("injected partial publication failure"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md"))
                .expect("read restored original AGENTS"),
            original_agents
        );
        assert!(fixture.target().join("skills").is_dir());
        assert!(fixture.target().join(".agent-skills").is_dir());
        assert!(
            !fixture
                .target()
                .join(".agent-skills/rollback-point")
                .exists()
        );
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn partial_backup_failure_restores_moved_old_roots() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let error = workspace
            .publish_staged_install_with_hook(&fixture.token, |name, phase| {
                if name == "skills" && phase == "backup" {
                    return Err(LifecycleError::Invalid(
                        "injected partial backup failure".to_owned(),
                    ));
                }
                Ok(())
            })
            .expect_err("partial backup must fail");
        assert!(
            error
                .to_string()
                .contains("injected partial backup failure"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md"))
                .expect("read restored original AGENTS"),
            original_agents
        );
        assert!(fixture.target().join("skills").is_dir());
        assert!(fixture.target().join(".agent-skills").is_dir());
        assert!(
            !fixture
                .target()
                .join(".agent-skills/rollback-point")
                .exists()
        );
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn post_rename_drift_preserves_the_recorded_backup_root() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let backup = workspace.backup_path();
        let target = fixture.target();
        let error = workspace
            .publish_staged_install_with_hook(&fixture.token, |name, phase| {
                if name == "AGENTS.md" && phase == "backup-after-rename" {
                    std::fs::write(target.join("AGENTS.md"), b"racing replacement\n")
                        .expect("reoccupy post-rename source");
                    set_mode(&target.join("AGENTS.md"), MANAGED_FILE_MODE);
                }
                Ok(())
            })
            .expect_err("post-rename source drift must fail closed");
        assert!(error.to_string().contains("backup preserved"), "{error}");
        assert_eq!(
            std::fs::read(backup.join("AGENTS.md")).expect("read preserved original AGENTS"),
            original_agents
        );
        assert_eq!(
            std::fs::read(target.join("AGENTS.md")).expect("read racing replacement"),
            b"racing replacement\n"
        );
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn published_identity_replacement_preserves_recovery_backup() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        let backup = published.backup_path().expect("published backup path");
        let displaced = fixture.target().join("displaced-published-agents");
        std::fs::rename(fixture.target().join("AGENTS.md"), &displaced)
            .expect("displace published AGENTS");
        std::fs::write(fixture.target().join("AGENTS.md"), INSTRUCTIONS.as_bytes())
            .expect("replace published AGENTS");
        set_mode(&fixture.target().join("AGENTS.md"), MANAGED_FILE_MODE);

        let error = published
            .rollback()
            .expect_err("identity replacement must block rollback");
        assert!(error.to_string().contains("backup preserved"), "{error}");
        assert!(backup.join("AGENTS.md").is_file());
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        std::fs::remove_file(displaced).expect("remove displaced published AGENTS");
        drop(source);
    }

    #[test]
    fn recovery_backup_content_tamper_never_replaces_published_roots() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        let backup = published.backup_path().expect("published backup path");
        std::fs::write(backup.join("AGENTS.md"), b"tampered backup\n")
            .expect("tamper recovery backup");

        let error = published
            .rollback()
            .expect_err("tampered backup must not be restored");
        assert!(error.to_string().contains("backup preserved"), "{error}");
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read still-published AGENTS"),
            INSTRUCTIONS.as_bytes()
        );
        assert!(
            fixture
                .target()
                .join(".agent-skills/rollback-point")
                .is_dir()
        );
        assert!(backup.join("AGENTS.md").is_file());
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn recovery_time_tamper_reinstates_the_published_install() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        let backup = published.backup_path().expect("published backup path");
        let error = published
            .rollback_with_hook(|name, phase| {
                if name == ".agent-skills" && phase == "restore" {
                    std::fs::write(backup.join("AGENTS.md"), b"recovery-time tamper\n")
                        .expect("tamper backup after its complete verification");
                    set_mode(&backup.join("AGENTS.md"), MANAGED_FILE_MODE);
                }
                Ok(())
            })
            .expect_err("recovery-time tamper must fail closed");
        assert!(
            error
                .to_string()
                .contains("original publication was reinstated"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md"))
                .expect("read reinstated published AGENTS"),
            INSTRUCTIONS.as_bytes()
        );
        assert!(
            fixture
                .target()
                .join(".agent-skills/rollback-point")
                .is_dir()
        );
        assert_eq!(
            std::fs::read(backup.join("AGENTS.md")).expect("read preserved tampered backup"),
            b"recovery-time tamper\n"
        );
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn unpublished_stage_tamper_does_not_block_intact_backup_restore() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let stage = workspace.stage_path();
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        published
            .rollback_with_hook(|name, phase| {
                if name == "AGENTS.md" && phase == "unpublish-after-rename" {
                    std::fs::write(stage.join("AGENTS.md"), b"tampered unpublished stage\n")
                        .expect("tamper unpublished new AGENTS");
                    set_mode(&stage.join("AGENTS.md"), MANAGED_FILE_MODE);
                }
                Ok(())
            })
            .expect("intact backup should restore despite discarded stage drift");
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read restored old AGENTS"),
            original_agents
        );
        assert!(!stage.exists());
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn tampered_new_stage_is_not_reinstated_after_old_restore_drift() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &[])
            .expect("stage rollback point");
        let stage = workspace.stage_path();
        let published = workspace
            .publish_staged_install(&fixture.token)
            .expect("publish managed upgrade");
        let backup = published.backup_path().expect("published backup path");
        let error = published
            .rollback_with_hook(|name, phase| {
                if name == "AGENTS.md" && phase == "unpublish-after-rename" {
                    std::fs::write(stage.join("AGENTS.md"), b"tampered unpublished stage\n")
                        .expect("tamper unpublished new AGENTS");
                    set_mode(&stage.join("AGENTS.md"), MANAGED_FILE_MODE);
                }
                if name == ".agent-skills" && phase == "restore" {
                    std::fs::write(backup.join("AGENTS.md"), b"tampered recovery AGENTS\n")
                        .expect("tamper old AGENTS during recovery");
                    set_mode(&backup.join("AGENTS.md"), MANAGED_FILE_MODE);
                }
                Ok(())
            })
            .expect_err("neither tampered tree may be accepted as reinstated");
        assert!(
            error
                .to_string()
                .contains("verify staged publication before failed-recovery reinstatement"),
            "{error}"
        );
        assert!(
            !error
                .to_string()
                .contains("original publication was reinstated after failed recovery"),
            "{error}"
        );
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn occupied_private_backup_is_rejected_before_any_root_move() {
        let fixture = Fixture::new();
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        std::fs::write(workspace.backup_path().join("collision"), b"occupied\n")
            .expect("occupy recovery backup");
        let error = workspace
            .publish_staged_install(&fixture.token)
            .expect_err("occupied backup must fail");
        assert!(error.to_string().contains("backup is not empty"), "{error}");
        for name in ["AGENTS.md", "skills", ".agent-skills"] {
            assert!(!fixture.target().join(name).exists(), "{name}");
        }
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn no_replace_publication_preserves_a_racing_destination() {
        let fixture = Fixture::new();
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let target = fixture.target();
        let error = workspace
            .publish_staged_install_with_hook(&fixture.token, |name, phase| {
                if name == "AGENTS.md" && phase == "publish-before-rename" {
                    std::fs::write(target.join("AGENTS.md"), b"racing owner\n")
                        .expect("create racing destination");
                    set_mode(&target.join("AGENTS.md"), MANAGED_FILE_MODE);
                }
                Ok(())
            })
            .expect_err("no-replace publication must reject a racing destination");
        assert!(error.to_string().contains("could not move"), "{error}");
        assert_eq!(
            std::fs::read(target.join("AGENTS.md")).expect("read racing destination"),
            b"racing owner\n"
        );
        assert!(!target.join("skills").exists());
        assert!(!target.join(".agent-skills").exists());
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[test]
    fn replacing_existing_roots_requires_staged_rollback_evidence() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let original_agents =
            std::fs::read(fixture.target().join("AGENTS.md")).expect("read original AGENTS");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        let error = workspace
            .publish_staged_install(&fixture.token)
            .expect_err("existing install without rollback point must fail");
        assert!(
            error
                .to_string()
                .contains("requires a verified rollback point"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(fixture.target().join("AGENTS.md"))
                .expect("read untouched original AGENTS"),
            original_agents
        );
        assert!(!fixture.target().join(LIFECYCLE_LOCK_DIRECTORY).exists());
        drop(source);
    }

    #[cfg(windows)]
    #[allow(clippy::permissions_set_readonly_false)]
    #[test]
    fn windows_readonly_external_snapshot_is_preserved_and_cleaned() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        let external = fixture.target().join("config/state");
        std::fs::create_dir_all(external.parent().expect("external parent"))
            .expect("create external parent");
        std::fs::write(&external, b"readonly\n").expect("write external file");
        let mut permissions = std::fs::metadata(&external)
            .expect("inspect external file")
            .permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(&external, permissions).expect("make external file readonly");
        let paths = vec!["config/state".to_owned()];
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        workspace
            .stage_rollback_point(&fixture.token, &paths)
            .expect("stage readonly rollback point");
        let staged_file = workspace
            .stage_path()
            .join(".agent-skills/rollback-point/external-files/config/state");
        assert!(
            std::fs::metadata(&staged_file)
                .expect("inspect staged external file")
                .permissions()
                .readonly()
        );
        let external_state = load_json(
            workspace
                .stage_path()
                .join(".agent-skills/rollback-point/external-state.json"),
        )
        .expect("load external state");
        assert_eq!(
            external_state
                .pointer("/entries/0/mode")
                .and_then(Value::as_u64),
            Some(0o444)
        );
        workspace
            .verify_staged_install(&fixture.token)
            .expect("verify readonly rollback point");
        let stage_path = workspace.stage_path();
        drop(source);
        workspace.cleanup().expect("cleanup readonly workspace");
        assert!(!stage_path.exists());
        let mut permissions = std::fs::metadata(&external)
            .expect("inspect source external file")
            .permissions();
        permissions.set_readonly(false);
        std::fs::set_permissions(&external, permissions).expect("restore source permissions");
    }

    #[test]
    fn corrupt_existing_rollback_point_rejects_new_snapshot() {
        let fixture = Fixture::new();
        materialize_current_install(&fixture);
        std::fs::create_dir(fixture.target().join(".agent-skills/rollback-point"))
            .expect("create corrupt rollback point");
        set_mode(
            &fixture.target().join(".agent-skills/rollback-point"),
            MANAGED_DIRECTORY_MODE,
        );
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        workspace
            .stage_external_state(&fixture.token)
            .expect("stage empty external state");
        assert!(
            workspace
                .stage_rollback_point(&fixture.token, &[])
                .expect_err("corrupt source rollback point must fail")
                .to_string()
                .contains("rollback point")
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_external_system_root_is_rejected() {
        let fixture = Fixture::new();
        std::fs::create_dir_all(fixture.target().join("skills"))
            .expect("create target Skills root");
        std::fs::create_dir_all(fixture.root.join("external-system"))
            .expect("create symlink target");
        std::os::unix::fs::symlink(
            fixture.root.join("external-system"),
            fixture.target().join("skills/.system"),
        )
        .expect("create unsafe .system symlink");
        let (source, mut workspace) = stage_complete_managed_layout(&fixture);
        let error = workspace
            .stage_external_state(&fixture.token)
            .expect_err("symlinked .system must fail");
        assert_eq!(
            error.to_string(),
            "target .system Skills is missing or unsafe"
        );
        drop(source);
        workspace.cleanup().expect("cleanup workspace");
    }

    fn refresh_plan_fingerprint(plan: &mut Value) {
        let mut identity = plan.as_object().expect("plan object").clone();
        identity.remove("fingerprint");
        identity.remove("status");
        plan["fingerprint"] =
            Value::String(canonical_sha256(&Value::Object(identity)).expect("hash plan"));
    }

    fn refresh_package_lock_fingerprint(package_lock: &mut Value) {
        let mut identity = package_lock
            .as_object()
            .expect("package lock object")
            .clone();
        identity.remove("fingerprint");
        package_lock["fingerprint"] =
            Value::String(canonical_sha256(&Value::Object(identity)).expect("hash package lock"));
    }

    fn temporary_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agent-lifecycle-staged-install-{label}-{}-{}",
            std::process::id(),
            TEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }
}
