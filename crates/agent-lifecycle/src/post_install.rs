use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE, open_child_directory,
    open_child_file, packages, same_content_state_cap, same_object_cap,
};
use agent_contracts::{canonical_sha256, json_integer};
use cap_std::fs::Dir;
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Read as _;

const EXTERNAL_SKILL_ROOTS: [&str; 1] = [".system"];

pub(super) fn check_skill_integrity(
    target: &Dir,
    install_lock: &Value,
    installed_semantics: Option<&Value>,
) -> Result<Value, LifecycleError> {
    let semantics = installed_semantics.ok_or_else(|| {
        LifecycleError::Invalid(
            "Skill verification requires rebuilt installed Manifest semantics".to_owned(),
        )
    })?;
    let semantic_fields = ["file_count", "files", "name", "package", "sha256"];
    let rebuilt = project_array_fields(
        semantics,
        "skills",
        &semantic_fields,
        "rebuilt installed Skills",
    )?;
    let locked = project_array_fields(
        install_lock,
        "skills",
        &semantic_fields,
        "Install Lock Skills",
    )?;
    if rebuilt != locked {
        return invalid("locked Skill identities differ from installed Manifests");
    }

    let skills = open_child_directory(
        target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed skills directory",
    )?;
    let skills_identity = skills.dir_metadata()?;
    let (external, actual) = skill_directory_snapshot(&skills)?;
    let records = array_field(install_lock, "skills", "Install Lock Skills")?;
    let mut expected = records
        .iter()
        .map(|record| string_field(record, "name", "Skill name").map(str::to_owned))
        .collect::<Result<Vec<_>, _>>()?;
    expected.sort();
    if actual != expected {
        return invalid("installed Skill set differs from Install Lock");
    }

    let mut identities = Vec::with_capacity(records.len());
    for record in records {
        let name = string_field(record, "name", "Skill name")?;
        let root = open_child_directory(
            &skills,
            name,
            Some(u32_field(record, "root_mode", "Skill root mode")?),
            &format!("Skill {name}"),
        )?;
        identities.push((name.to_owned(), root.dir_metadata()?));
        packages::validate_recorded_tree(
            &root,
            record,
            "sha256",
            &format!("installed Skill content differs: {name}"),
        )?;
    }

    let final_skills = reopen_skills_snapshot(target, &skills_identity, &expected)?;
    for (name, identity) in &identities {
        let current = open_child_directory(&final_skills, name, None, &format!("Skill {name}"))?;
        let current_metadata = current.dir_metadata()?;
        if !same_object_cap(identity, &current_metadata)
            || !same_content_state_cap(identity, &current_metadata)
        {
            return invalid(format!("installed Skill changed while inspecting: {name}"));
        }
    }
    reopen_skills_snapshot(target, &skills_identity, &expected)?;

    Ok(json!({
        "external_roots": external.into_iter().collect::<Vec<_>>(),
        "skill_count": expected.len(),
    }))
}

#[allow(clippy::too_many_lines)]
pub(super) fn check_global_instructions(
    target: &Dir,
    install_lock: &Value,
    package_lock: &Value,
    installed_semantics: Option<&Value>,
) -> Result<Value, LifecycleError> {
    let semantics = installed_semantics.ok_or_else(|| {
        LifecycleError::Invalid(
            "AGENTS verification requires rebuilt installed Manifest semantics".to_owned(),
        )
    })?;
    let instructions = object_field(install_lock, "instructions", "Install Lock instructions")?;
    if instructions.get("path").and_then(Value::as_str) != Some("AGENTS.md") {
        return invalid("Install Lock does not select the unique global AGENTS.md path");
    }
    let installed_hash =
        packages::hash_child_file(target, "AGENTS.md", MANAGED_FILE_MODE, "global AGENTS.md")?;
    let locked_hash = string_map_field(instructions, "sha256", "AGENTS hash")?;
    if installed_hash != locked_hash {
        return invalid("global AGENTS.md content differs from Install Lock");
    }
    let persistent_instructions =
        object_field(package_lock, "instructions", "persistent instructions")?;
    if persistent_instructions
        .get("sha256")
        .and_then(Value::as_str)
        != Some(locked_hash)
    {
        return invalid("global AGENTS.md hash differs between Lockfiles");
    }
    let rule_trace = instructions
        .get("rule_trace")
        .ok_or_else(|| LifecycleError::Invalid("AGENTS rule trace is invalid".to_owned()))?;
    if persistent_instructions
        .get("rule_trace_sha256")
        .and_then(Value::as_str)
        != Some(canonical_sha256(rule_trace)?.as_str())
    {
        return invalid("AGENTS rule trace differs from persistent Lockfile");
    }
    let expected = object_field(semantics, "instructions", "rebuilt installed instructions")?;
    if instructions.get("fragments") != expected.get("fragments")
        || instructions.get("rule_trace") != expected.get("rule_trace")
        || instructions.get("sha256") != expected.get("sha256")
    {
        return invalid("global AGENTS semantics differ from installed Manifests");
    }
    let expected_content = string_map_field(expected, "content", "rebuilt AGENTS content")?;
    if !child_bytes_equal(
        target,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "global AGENTS.md",
        expected_content.as_bytes(),
    )? {
        return invalid("global AGENTS semantics differ from installed Manifests");
    }

    let package_positions = array_field(install_lock, "packages", "Install Lock packages")?
        .iter()
        .enumerate()
        .map(|(index, package)| {
            Ok((
                string_field(package, "id", "Install Lock package id")?.to_owned(),
                index,
            ))
        })
        .collect::<Result<BTreeMap<_, _>, LifecycleError>>()?;
    let fragments = map_array_field(instructions, "fragments", "AGENTS fragments")?;
    let mut keyed_fragments = Vec::with_capacity(fragments.len());
    for fragment in fragments {
        let package = string_field(fragment, "package", "instruction fragment package")?;
        let position = package_positions.get(package).copied().ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "instruction fragment package is not installed: {package}"
            ))
        })?;
        let order = fragment
            .get("order")
            .and_then(json_integer)
            .ok_or_else(|| {
                LifecycleError::Invalid("instruction fragment order is invalid".to_owned())
            })?;
        let id = string_field(fragment, "id", "instruction fragment id")?;
        keyed_fragments.push((position, order, id.to_owned(), fragment.clone()));
    }
    keyed_fragments
        .sort_by(|left, right| (&left.0, &left.1, &left.2).cmp(&(&right.0, &right.1, &right.2)));
    if fragments
        != keyed_fragments
            .iter()
            .map(|(_, _, _, fragment)| fragment.clone())
            .collect::<Vec<_>>()
    {
        return invalid("AGENTS instruction fragment order is not canonical");
    }

    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let packages_root = open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "installed package directory",
    )?;
    let records = array_field(install_lock, "packages", "Install Lock packages")?
        .iter()
        .map(|record| {
            Ok((
                string_field(record, "id", "Install Lock package id")?.to_owned(),
                record,
            ))
        })
        .collect::<Result<BTreeMap<_, _>, LifecycleError>>()?;
    for fragment in fragments {
        let id = string_field(fragment, "id", "instruction fragment id")?;
        let package = string_field(fragment, "package", "instruction fragment package")?;
        let record = records.get(package).copied().ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "AGENTS instruction fragment is missing or unsafe: {id}"
            ))
        })?;
        let installed_package = open_child_directory(
            &packages_root,
            package,
            Some(u32_field(record, "root_mode", "package root mode")?),
            &format!("package {package}"),
        )?;
        let path = string_field(fragment, "path", "instruction fragment path")?;
        let bytes = packages::read_recorded_bytes(
            &installed_package,
            record,
            path,
            "installed instruction fragment",
        )
        .map_err(|_| {
            LifecycleError::Invalid(format!(
                "AGENTS instruction fragment is missing or unsafe: {id}"
            ))
        })?;
        let text = String::from_utf8(bytes).map_err(|_| {
            LifecycleError::Invalid(format!("AGENTS instruction fragment differs: {id}"))
        })?;
        let normalized = format!("{}\n", packages::python_trim(&text));
        if bytes_sha256(normalized.as_bytes())
            != string_field(fragment, "sha256", "instruction fragment hash")?
        {
            return invalid(format!("AGENTS instruction fragment differs: {id}"));
        }
    }

    Ok(json!({
        "fragment_count": fragments.len(),
        "path": "AGENTS.md",
        "sha256": locked_hash,
    }))
}

pub(super) fn check_binding_freeze(
    install_lock: &Value,
    package_lock: &Value,
    installed_semantics: Option<&Value>,
) -> Result<Value, LifecycleError> {
    let semantics = installed_semantics.ok_or_else(|| {
        LifecycleError::Invalid(
            "Binding verification requires rebuilt installed Manifest semantics".to_owned(),
        )
    })?;
    let bindings = install_lock
        .get("bindings")
        .ok_or_else(|| LifecycleError::Invalid("Install Lock bindings are invalid".to_owned()))?;
    let bindings_sha256 =
        string_field(package_lock, "bindings_sha256", "persistent Binding digest")?;
    if canonical_sha256(bindings)? != bindings_sha256 {
        return invalid("Capability Binding digest differs from Install Lock");
    }
    if package_lock.get("capability_providers") != install_lock.get("capability_providers") {
        return invalid("Capability Provider closure differs between Lockfiles");
    }
    if install_lock.get("bindings") != semantics.get("bindings")
        || install_lock.get("capability_providers") != semantics.get("capability_providers")
    {
        return invalid("Capability Binding semantics differ from installed Manifests");
    }
    Ok(json!({
        "binding_count": bindings.as_object().map_or(0, Map::len),
        "bindings_sha256": bindings_sha256,
    }))
}

pub(super) fn check_permission_freeze(
    install_lock: &Value,
    package_lock: &Value,
    installed_semantics: Option<&Value>,
) -> Result<Value, LifecycleError> {
    let semantics = installed_semantics.ok_or_else(|| {
        LifecycleError::Invalid(
            "Permission verification requires rebuilt installed Manifest semantics".to_owned(),
        )
    })?;
    if package_lock.get("permission_profiles") != install_lock.get("permission_profiles") {
        return invalid("permission profile set differs between Lockfiles");
    }
    let permission_profiles = array_field(
        install_lock,
        "permission_profiles",
        "installed permission profiles",
    )?;
    let allowed = permission_profiles
        .iter()
        .map(|value| {
            value.as_str().ok_or_else(|| {
                LifecycleError::Invalid("installed permission profile is invalid".to_owned())
            })
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    let providers = object_field(
        install_lock,
        "capability_providers",
        "installed Capability Providers",
    )?;
    let mut capability_permissions = Map::new();
    for (capability, provider) in providers {
        let permission = string_field(provider, "permission_profile", "Capability permission")?;
        if !allowed.contains(permission) {
            return invalid(
                "Capability Provider requests a permission outside the installed profile set",
            );
        }
        capability_permissions.insert(capability.clone(), Value::String(permission.to_owned()));
    }
    let expected_profiles = semantics.get("permission_profiles").ok_or_else(|| {
        LifecycleError::Invalid("rebuilt permission profiles are missing".to_owned())
    })?;
    let expected_providers = object_field(
        semantics,
        "capability_providers",
        "rebuilt Capability Providers",
    )?;
    let expected_permissions = expected_providers
        .iter()
        .map(|(capability, provider)| {
            Ok((
                capability.clone(),
                Value::String(
                    string_field(
                        provider,
                        "permission_profile",
                        "rebuilt Capability permission",
                    )?
                    .to_owned(),
                ),
            ))
        })
        .collect::<Result<Map<_, _>, LifecycleError>>()?;
    if install_lock.get("permission_profiles") != Some(expected_profiles)
        || capability_permissions != expected_permissions
    {
        return invalid("Capability permission semantics differ from installed Manifests");
    }
    Ok(json!({
        "capability_permissions": capability_permissions,
        "permission_profiles": permission_profiles,
    }))
}

fn reopen_skills_snapshot(
    target: &Dir,
    original: &cap_std::fs::Metadata,
    expected: &[String],
) -> Result<Dir, LifecycleError> {
    let skills = open_child_directory(
        target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed skills directory",
    )?;
    let current = skills.dir_metadata()?;
    if !same_object_cap(original, &current) || !same_content_state_cap(original, &current) {
        return invalid("managed skills directory changed while inspecting");
    }
    let (_, actual) = skill_directory_snapshot(&skills)?;
    if actual != expected {
        return invalid("installed Skill set differs from Install Lock");
    }
    Ok(skills)
}

fn skill_directory_snapshot(
    skills: &Dir,
) -> Result<(BTreeSet<String>, Vec<String>), LifecycleError> {
    let mut external = BTreeSet::new();
    let mut actual = Vec::new();
    for entry in skills.entries()? {
        let entry = entry?;
        let name = entry.file_name();
        let name_text = name.to_string_lossy().into_owned();
        let metadata = skills.symlink_metadata(&name)?;
        if EXTERNAL_SKILL_ROOTS.contains(&name_text.as_str())
            && !metadata.file_type().is_symlink()
            && metadata.is_dir()
        {
            external.insert(name_text);
            continue;
        }
        if super::ignored_os_metadata(skills, &name)? {
            continue;
        }
        actual.push(name_text);
    }
    actual.sort();
    Ok((external, actual))
}

fn project_array_fields(
    value: &Value,
    field: &str,
    fields: &[&str],
    label: &str,
) -> Result<Vec<Value>, LifecycleError> {
    array_field(value, field, label)?
        .iter()
        .map(|item| {
            let mut projected = Map::new();
            for field in fields {
                projected.insert(
                    (*field).to_owned(),
                    item.get(*field).cloned().ok_or_else(|| {
                        LifecycleError::Invalid(format!("{label} field is missing: {field}"))
                    })?,
                );
            }
            Ok(Value::Object(projected))
        })
        .collect()
}

pub(super) fn child_bytes_equal(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
    expected: &[u8],
) -> Result<bool, LifecycleError> {
    let mut file = open_child_file(parent, name, mode, label)?;
    let opened = file.metadata()?;
    let mut offset = 0_usize;
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    let mut equal = usize::try_from(opened.len()).ok() == Some(expected.len());
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        if offset
            .checked_add(count)
            .is_none_or(|end| end > expected.len())
            || expected.get(offset..offset + count) != Some(&buffer[..count])
        {
            equal = false;
        }
        offset = offset.saturating_add(count);
    }
    let after = file.metadata()?;
    let current = open_child_file(parent, name, mode, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(equal && offset == expected.len())
}

fn bytes_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn array_field<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a [Value], LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn object_field<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a Map<String, Value>, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_object)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn map_array_field<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a [Value], LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn string_field<'a>(value: &'a Value, field: &str, label: &str) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn string_map_field<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn u32_field(value: &Value, field: &str, label: &str) -> Result<u32, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}
