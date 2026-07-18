use crate::{
    LifecycleError, MANAGED_DIRECTORY_MODE,
    packages::{CORE_VERSION, SemanticPackage, derive_package_semantics},
    source_install::SourceInstallSelection,
    source_packages::{SourcePackage, SourcePackageSet},
};
use agent_contracts::canonical_sha256;
use agent_engine::{resolve_package_lock, validate_install_plan};
use agent_registry::{ManifestRegistry, RegisteredManifest};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

const MANAGER_ID: &str = "agent-development-skills";

/// Complete native compatibility result for Install Bundle compilation.
///
/// The fields are existing persisted contracts. This typed wrapper and its
/// compatibility projection deliberately do not introduce another schema.
#[derive(Debug, Clone)]
pub struct SourceInstallBundle {
    plan: Value,
    instructions: String,
    package_lock: Value,
}

impl SourceInstallBundle {
    /// Frozen Install Plan v2.
    #[must_use]
    pub fn plan(&self) -> &Value {
        &self.plan
    }

    /// Rendered managed `AGENTS.md` content.
    #[must_use]
    pub fn instructions(&self) -> &str {
        &self.instructions
    }

    /// Persistent package Lockfile.
    #[must_use]
    pub fn package_lock(&self) -> &Value {
        &self.package_lock
    }

    /// Emit the temporary Python/Rust differential projection.
    ///
    /// This wrapper is not persisted and has no schema version.
    #[must_use]
    pub fn compatibility_projection(&self) -> Value {
        json!({
            "instructions": self.instructions,
            "package_lock": self.package_lock,
            "plan": self.plan,
        })
    }
}

/// Compile immutable source snapshots into the existing Install Plan v2 and
/// package Lockfile contracts.
///
/// Selection and snapshots must come from the same source state. The compiler
/// independently rebuilds dependency, instruction, Skill, Manifest Registry,
/// capability, permission, asset, and package identities before resolving the
/// Lockfile.
///
/// # Errors
/// Returns a fail-closed error for mismatched source state, missing dependency
/// capabilities, semantic disagreement, malformed schemas, or invalid output.
#[allow(clippy::too_many_lines)]
pub fn compile_source_install_bundle(
    selection: &SourceInstallSelection,
    package_set: &SourcePackageSet,
    schema_root: impl AsRef<Path>,
    previous_lock: Option<&Value>,
) -> Result<SourceInstallBundle, LifecycleError> {
    validate_selection_snapshot_identity(selection, package_set)?;
    validate_source_registry(package_set)?;
    validate_dependency_capabilities(selection, package_set)?;

    let package_records = package_set
        .packages
        .iter()
        .map(package_record)
        .collect::<Result<Vec<_>, _>>()?;
    let semantic_packages = package_set
        .packages
        .iter()
        .map(|package| SemanticPackage {
            fragments: package.fragments.clone(),
            id: package.id.clone(),
            manifest: package.manifest.clone(),
            provider: package.provider.clone(),
            files: package.files.clone(),
        })
        .collect::<Vec<_>>();
    let semantics = derive_package_semantics(&semantic_packages, &package_records)?;
    let dependencies = selection.resolved_dependencies();
    if semantics.get("dependencies") != Some(&Value::Array(dependencies.clone())) {
        return invalid("resolved package dependencies differ from installed Manifest semantics");
    }

    let instructions = required_object(&semantics, "instructions", "installation semantics")?;
    let expected_skills = source_skills(package_set)?;
    if semantics.get("skills") != Some(&Value::Array(expected_skills.clone())) {
        return invalid("selected Skills differ from installed Manifest semantics");
    }
    let instruction_content = required_string(
        &Value::Object(instructions.clone()),
        "content",
        "rendered instructions",
    )?
    .to_owned();

    let mut assets = Vec::new();
    for package in &package_records {
        let package_id = required_string(package, "id", "package record")?;
        let files = package
            .get("files")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                LifecycleError::Invalid("package record files are invalid".to_owned())
            })?;
        assets.extend(files.iter().map(|file| {
            json!({
                "mode": file.get("mode").cloned().unwrap_or(Value::Null),
                "package": package_id,
                "path": file.get("path").cloned().unwrap_or(Value::Null),
                "sha256": file.get("sha256").cloned().unwrap_or(Value::Null),
            })
        }));
    }

    let identities = required_object(
        &semantics,
        "selected_package_identities",
        "installation semantics",
    )?;
    let records_by_id = package_records
        .iter()
        .map(|record| {
            Ok((
                required_string(record, "id", "package record")?.to_owned(),
                record,
            ))
        })
        .collect::<Result<BTreeMap<_, _>, LifecycleError>>()?;
    let selected_packages = package_set
        .packages
        .iter()
        .map(|package| {
            let identity = identities
                .get(&package.id)
                .and_then(Value::as_object)
                .ok_or_else(|| {
                    LifecycleError::Invalid(format!(
                        "selected package identity is missing: {}",
                        package.id
                    ))
                })?;
            let record = records_by_id.get(&package.id).ok_or_else(|| {
                LifecycleError::Invalid(format!("package record is missing: {}", package.id))
            })?;
            let reasons = selection.selection_reasons(&package.id).ok_or_else(|| {
                LifecycleError::Invalid(format!(
                    "package selection reasons are missing: {}",
                    package.id
                ))
            })?;
            let mut selected = identity.clone();
            selected.insert("id".to_owned(), Value::String(package.id.clone()));
            selected.insert("selection_reasons".to_owned(), json!(reasons));
            selected.insert(
                "source_sha256".to_owned(),
                record.get("files_sha256").cloned().unwrap_or(Value::Null),
            );
            Ok(Value::Object(selected))
        })
        .collect::<Result<Vec<_>, LifecycleError>>()?;

    let mut plan = Map::new();
    plan.insert(
        "asset_summary".to_owned(),
        json!({
            "content_sha256": canonical_sha256(&Value::Array(assets.clone()))?,
            "file_count": assets.len(),
            "package_count": package_records.len(),
            "skill_count": expected_skills.len(),
        }),
    );
    plan.insert("assets".to_owned(), Value::Array(assets));
    copy_semantic_field(&mut plan, &semantics, "bindings")?;
    copy_semantic_field(&mut plan, &semantics, "capability_providers")?;
    plan.insert(
        "core_version".to_owned(),
        Value::String(CORE_VERSION.to_owned()),
    );
    plan.insert(
        "instructions".to_owned(),
        json!({
            "fragments": instructions.get("fragments").cloned().unwrap_or(Value::Null),
            "path": "AGENTS.md",
            "rule_trace": instructions.get("rule_trace").cloned().unwrap_or(Value::Null),
            "sha256": instructions.get("sha256").cloned().unwrap_or(Value::Null),
        }),
    );
    plan.insert(
        "lock_schema_version".to_owned(),
        Value::String("2.0".to_owned()),
    );
    plan.insert(
        "managed_roots".to_owned(),
        json!(["AGENTS.md", "skills", ".agent-skills"]),
    );
    plan.insert("manager".to_owned(), Value::String(MANAGER_ID.to_owned()));
    plan.insert("packages".to_owned(), Value::Array(package_records));
    copy_semantic_field(&mut plan, &semantics, "permission_profiles")?;
    plan.insert(
        "resolved_dependencies".to_owned(),
        Value::Array(dependencies),
    );
    plan.insert("schema_version".to_owned(), Value::String("1.0".to_owned()));
    plan.insert(
        "selected_disciplines".to_owned(),
        json!(selection.selected_disciplines()),
    );
    plan.insert(
        "selected_packages".to_owned(),
        Value::Array(selected_packages),
    );
    plan.insert(
        "selected_platforms".to_owned(),
        json!(selection.selected_platforms()),
    );
    plan.insert(
        "selected_runtime_configs".to_owned(),
        json!(selection.selected_runtime_configs()),
    );
    copy_semantic_field(&mut plan, &semantics, "side_effects")?;
    plan.insert("skills".to_owned(), Value::Array(expected_skills));
    plan.insert("status".to_owned(), Value::String("planned".to_owned()));

    let mut initial_identity = plan.clone();
    initial_identity.remove("status");
    plan.insert(
        "fingerprint".to_owned(),
        Value::String(canonical_sha256(&Value::Object(initial_identity))?),
    );
    let initial_plan = Value::Object(plan.clone());
    validate_install_plan(&initial_plan)?;
    let package_lock = resolve_package_lock(
        &initial_plan,
        schema_root,
        None,
        None,
        Path::new("."),
        previous_lock,
    )?;
    plan.insert(
        "package_lock_hash".to_owned(),
        package_lock
            .get("fingerprint")
            .cloned()
            .ok_or_else(|| LifecycleError::Invalid("package Lock fingerprint is missing".into()))?,
    );
    let mut final_identity = plan.clone();
    final_identity.remove("fingerprint");
    final_identity.remove("status");
    plan.insert(
        "fingerprint".to_owned(),
        Value::String(canonical_sha256(&Value::Object(final_identity))?),
    );
    let plan = Value::Object(plan);
    validate_install_plan(&plan)?;

    Ok(SourceInstallBundle {
        plan,
        instructions: instruction_content,
        package_lock,
    })
}

fn validate_selection_snapshot_identity(
    selection: &SourceInstallSelection,
    package_set: &SourcePackageSet,
) -> Result<(), LifecycleError> {
    let selected = selection
        .package_roots()
        .iter()
        .map(|(identifier, _)| identifier.as_str())
        .collect::<Vec<_>>();
    let snapshotted = package_set
        .packages
        .iter()
        .map(|package| package.id.as_str())
        .collect::<Vec<_>>();
    if selected != snapshotted {
        return invalid("source package snapshots differ from selected package closure");
    }
    if selection.package_roots() != package_set.package_roots {
        return invalid("source package snapshots differ from selected package roots");
    }
    for package in &package_set.packages {
        if selection.manifest_digest(&package.id) != Some(package.manifest_digest.as_str()) {
            return invalid(format!(
                "source package snapshot Manifest differs from selection: {}",
                package.id
            ));
        }
    }
    Ok(())
}

fn validate_source_registry(package_set: &SourcePackageSet) -> Result<(), LifecycleError> {
    let mut entries = Vec::new();
    for package in &package_set.packages {
        entries.push(RegisteredManifest {
            path: PathBuf::from(&package.id).join("manifest.json"),
            value: package.manifest.clone(),
            digest: package.manifest_digest.clone(),
        });
        if let Some(provider) = &package.provider {
            let relative = package
                .manifest
                .get("installation")
                .and_then(Value::as_object)
                .and_then(|installation| installation.get("provider_manifest"))
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    LifecycleError::Invalid(format!(
                        "source package Provider path is missing: {}",
                        package.id
                    ))
                })?;
            entries.push(RegisteredManifest {
                path: PathBuf::from(&package.id).join(relative),
                value: provider.clone(),
                digest: package.provider_digest.clone().ok_or_else(|| {
                    LifecycleError::Invalid(format!(
                        "source package Provider digest is missing: {}",
                        package.id
                    ))
                })?,
            });
        }
    }
    ManifestRegistry::new(entries, CORE_VERSION)?;
    Ok(())
}

fn validate_dependency_capabilities(
    selection: &SourceInstallSelection,
    package_set: &SourcePackageSet,
) -> Result<(), LifecycleError> {
    let packages = package_set
        .packages
        .iter()
        .map(|package| (package.id.as_str(), package))
        .collect::<BTreeMap<_, _>>();
    for dependency in selection.resolved_dependencies() {
        let consumer = required_string(&dependency, "from", "resolved dependency")?;
        let target_id = required_string(&dependency, "to", "resolved dependency")?;
        let target = packages.get(target_id).ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "dependency package snapshot is missing: {target_id}"
            ))
        })?;
        let source = target.provider.as_ref().unwrap_or(&target.manifest);
        let provided = source
            .get("capabilities")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                LifecycleError::Invalid(format!(
                    "dependency package capabilities are invalid: {target_id}"
                ))
            })?
            .iter()
            .map(|capability| required_string(capability, "id", "package capability"))
            .collect::<Result<BTreeSet<_>, _>>()?;
        let required = dependency
            .get("required_capabilities")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                LifecycleError::Invalid("resolved dependency capabilities are invalid".to_owned())
            })?;
        let missing = required
            .iter()
            .map(|value| {
                value.as_str().ok_or_else(|| {
                    LifecycleError::Invalid("resolved dependency capability is invalid".to_owned())
                })
            })
            .collect::<Result<BTreeSet<_>, _>>()?
            .difference(&provided)
            .copied()
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            return invalid(format!(
                "package {consumer} dependency {target_id} is missing capabilities: {}",
                missing.join(", ")
            ));
        }
    }
    Ok(())
}

fn package_record(package: &SourcePackage) -> Result<Value, LifecycleError> {
    Ok(json!({
        "directories": package.directories,
        "file_count": package.files.len(),
        "files": package.files,
        "files_sha256": canonical_sha256(&Value::Array(package.files.clone()))?,
        "id": package.id,
        "manifest_sha256": package.manifest_digest,
        "provider_manifest_sha256": package.provider_digest,
        "root_mode": MANAGED_DIRECTORY_MODE,
    }))
}

fn source_skills(package_set: &SourcePackageSet) -> Result<Vec<Value>, LifecycleError> {
    let mut names = BTreeSet::new();
    let mut skills = Vec::new();
    for package in &package_set.packages {
        for skill in &package.skills {
            if !names.insert(skill.name.as_str()) {
                return invalid(format!("skill name conflict: {}", skill.name));
            }
            skills.push(json!({
                "directories": skill.directories,
                "file_count": skill.files.len(),
                "files": skill.files,
                "name": skill.name,
                "package": package.id,
                "root_mode": MANAGED_DIRECTORY_MODE,
                "sha256": canonical_sha256(&Value::Array(skill.files.clone()))?,
            }));
        }
    }
    Ok(skills)
}

fn copy_semantic_field(
    plan: &mut Map<String, Value>,
    semantics: &Value,
    field: &str,
) -> Result<(), LifecycleError> {
    plan.insert(
        field.to_owned(),
        semantics.get(field).cloned().ok_or_else(|| {
            LifecycleError::Invalid(format!("installation semantics missing field: {field}"))
        })?,
    );
    Ok(())
}

fn required_object<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a Map<String, Value>, LifecycleError> {
    value.get(field).and_then(Value::as_object).ok_or_else(|| {
        LifecycleError::Invalid(format!("{label} field is missing or invalid: {field}"))
    })
}

fn required_string<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a str, LifecycleError> {
    value.get(field).and_then(Value::as_str).ok_or_else(|| {
        LifecycleError::Invalid(format!("{label} field is missing or invalid: {field}"))
    })
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{resolve_source_install_selection, snapshot_source_packages};
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct TemporaryRoot(PathBuf);

    impl TemporaryRoot {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "agent-source-bundle-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            Self(path)
        }
    }

    impl Drop for TemporaryRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn repository_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("workspace root")
            .to_path_buf()
    }

    #[test]
    fn compiles_core_bundle_into_valid_existing_contracts() {
        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve core");
        let packages = snapshot_source_packages(&selection).expect("snapshot core");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile core bundle");

        validate_install_plan(bundle.plan()).expect("valid Install Plan");
        assert_eq!(
            bundle.plan()["package_lock_hash"],
            bundle.package_lock()["fingerprint"]
        );
        assert!(
            bundle
                .instructions()
                .starts_with("<!-- agent-development-skills:managed instructions-v1 -->")
        );
    }

    #[test]
    fn rejects_snapshot_from_a_different_selection() {
        let root = repository_root();
        let core = resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
            .expect("resolve core");
        let apple = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &[],
            &[],
            false,
        )
        .expect("resolve apple");
        let packages = snapshot_source_packages(&apple).expect("snapshot apple");
        let error = compile_source_install_bundle(&core, &packages, root.join("schemas"), None)
            .expect_err("selection mismatch");
        assert!(
            error
                .to_string()
                .contains("snapshots differ from selected package closure")
        );
    }

    #[test]
    fn rejects_same_manifest_snapshot_from_a_different_source_root() {
        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve repository core");
        let fixture = TemporaryRoot::new();
        let fixture_core = fixture.0.join("platforms/core");
        std::fs::create_dir_all(fixture_core.join("agent-instructions"))
            .expect("create fixture package");
        std::fs::copy(
            root.join("platforms/core/manifest.json"),
            fixture_core.join("manifest.json"),
        )
        .expect("copy identical Manifest");
        let fragment = std::fs::read(root.join("platforms/core/agent-instructions/global.md"))
            .expect("read repository fragment");
        let mut changed_fragment = fragment;
        changed_fragment.extend_from_slice(b"\nDifferent source asset.\n");
        std::fs::write(
            fixture_core.join("agent-instructions/global.md"),
            changed_fragment,
        )
        .expect("write changed source asset");
        let other_selection =
            resolve_source_install_selection(fixture.0.join("platforms"), &[], &[], &[], true)
                .expect("resolve fixture core");
        let other_packages =
            snapshot_source_packages(&other_selection).expect("snapshot fixture core");

        let error =
            compile_source_install_bundle(&selection, &other_packages, root.join("schemas"), None)
                .expect_err("source root mismatch");
        assert!(
            error
                .to_string()
                .contains("snapshots differ from selected package roots")
        );
    }
}
