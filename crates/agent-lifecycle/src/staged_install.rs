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
pub(super) fn verify(stage: &Dir, plan: &ValidatedInstallPlan) -> Result<(), LifecycleError> {
    validate_layout(stage, plan)?;
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    let managed_identity = managed.dir_metadata()?;
    require_names(
        &managed,
        &set([INSTALL_LOCK, PACKAGE_LOCK, "packages"]),
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
    require_names(
        &skills_root,
        &plan.skill_names()?,
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

    require_names(
        stage,
        &set([".agent-skills", "AGENTS.md", "skills"]),
        "staged managed roots changed while verifying",
    )?;
    let final_managed = revalidate_directory(
        stage,
        ".agent-skills",
        &managed_identity,
        MANAGED_DIRECTORY_MODE,
        "staged managed metadata",
    )?;
    require_names(
        &final_managed,
        &set([INSTALL_LOCK, PACKAGE_LOCK, "packages"]),
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
        &plan.skill_names()?,
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
    use crate::LifecycleWorkspace;
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
            .verify_staged_install(&fixture.token)
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
            std::fs::hard_link(
                workspace.stage_path().join(relative),
                fixture
                    .root
                    .join(format!("alias-{}", relative.replace(['/', '.'], "-"))),
            )
            .expect("create post-stage hard-link alias");
            let error = workspace
                .verify_staged_install(&fixture.token)
                .expect_err("managed file alias must fail");
            assert!(
                error.to_string().contains("has an unsafe hard-link alias"),
                "{relative}: {error}"
            );
            drop(source);
            workspace.cleanup().expect("cleanup workspace");
        }
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
