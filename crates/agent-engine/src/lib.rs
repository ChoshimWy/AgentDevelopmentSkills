//! Deterministic discovery, policy, and planning engine migration path.

mod package_lock;
mod upgrade_contracts;
mod upgrade_plan;

pub use package_lock::{
    diff_package_locks, explain_package_lock, install_plan_identity_hash, resolve_package_lock,
    schema_inventory, validate_install_plan, validate_package_lock, validate_plan_package_lock,
};
pub use upgrade_contracts::{validate_upgrade_conformance_evidence, validate_upgrade_plan};
pub use upgrade_plan::{UpgradePlanRequest, compile_upgrade_plan};

use agent_contracts::{ContractError, canonical_sha256};
use agent_registry::{
    ManifestRegistry, RegistryError, ResolvedBinding, required_platform_capabilities,
};
use serde_json::{Map, Value, json};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use thiserror::Error;

/// Engine validation and compatibility failures.
#[derive(Debug, Error)]
pub enum EngineError {
    #[error(transparent)]
    Contract(#[from] ContractError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Registry(#[from] RegistryError),
    #[error("{0}")]
    Invalid(String),
}

const PLATFORM_TERMS: [(&str, &[&str]); 5] = [
    (
        "apple",
        &["ios", "ipad", "macos", "swift", "xcode", "apple"],
    ),
    ("android", &["android", "kotlin", "gradle", "compose"]),
    (
        "backend",
        &["backend", "server", "api", "database", "后端", "服务端"],
    ),
    (
        "desktop",
        &["desktop", "windows", "linux", "electron", "tauri", "桌面"],
    ),
    ("web", &["web", "frontend", "react", "vue", "网页", "前端"]),
];

const IGNORED_DIRECTORIES: [&str; 12] = [
    ".git",
    ".build",
    ".cache",
    ".gradle",
    ".venv",
    "DerivedData",
    "Pods",
    "build",
    "dist",
    "fixtures",
    "node_modules",
    "testdata",
];
const MODULE_GROUP_DIRECTORIES: [&str; 3] = ["apps", "packages", "services"];
const MAX_REPOSITORY_ENTRIES: usize = 100_000;
const MAX_DISCOVERY_FILES: usize = 100_000;
const MAX_DISCOVERY_MATCH_WORK_UNITS: usize = 10_000_000;
const MAX_DISCOVERY_EVIDENCE: usize = 100_000;
const MAX_DISCOVERY_PATH_BYTES: usize = 4_096;
const MAX_POLICY_LAYERS: usize = 1_024;
const MAX_POLICY_FIELDS: usize = 16_384;
const MAX_POLICY_ITEMS: usize = 16_384;
const MAX_PLAN_NODES: usize = 16_384;
const MAX_PLAN_EDGES: usize = 65_536;

/// Read-only repository discovery engine.
pub struct DiscoveryEngine<'a> {
    registry: &'a ManifestRegistry,
    max_depth: usize,
}

impl<'a> DiscoveryEngine<'a> {
    #[must_use]
    pub const fn new(registry: &'a ManifestRegistry) -> Self {
        Self {
            registry,
            max_depth: 4,
        }
    }

    #[must_use]
    pub const fn with_max_depth(registry: &'a ManifestRegistry, max_depth: usize) -> Self {
        Self {
            registry,
            max_depth,
        }
    }

    /// Discover repository platforms and evidence without executing repository
    /// code or following directory symlinks.
    ///
    /// # Errors
    /// Returns a controlled error for unreadable repositories or malformed
    /// Manifest detection contracts.
    #[allow(clippy::too_many_lines)]
    pub fn discover(
        &self,
        repository: impl AsRef<Path>,
        target_files: &[String],
        changed_files: &[String],
        cwd: Option<&Path>,
    ) -> Result<Value, EngineError> {
        let requested = resolve_existing(repository.as_ref())?;
        let root = repository_root(&requested)?;
        let files = repository_files(&root, self.max_depth)?;
        let mut evidence = Vec::new();
        let mut modules = Vec::new();
        let mut match_work_units = 0_usize;

        for registered in self.registry.manifests() {
            let manifest = registered.value.as_object().ok_or_else(|| {
                EngineError::Invalid("registered manifest must be an object".to_owned())
            })?;
            let platform_id = manifest
                .get("id")
                .and_then(Value::as_str)
                .ok_or_else(|| EngineError::Invalid("manifest id is invalid".to_owned()))?;
            let detection = manifest
                .get("detection")
                .and_then(Value::as_object)
                .ok_or_else(|| EngineError::Invalid("manifest detection is invalid".to_owned()))?;
            let mut matches = Vec::<SignalMatch>::new();
            for level in ["strong", "medium", "weak"] {
                let patterns = detection
                    .get(level)
                    .ok_or_else(|| {
                        EngineError::Invalid(format!("manifest detection {level} is missing"))
                    })
                    .and_then(|value| string_array(value, level))?;
                for pattern in patterns {
                    if pattern.len() > MAX_DISCOVERY_PATH_BYTES {
                        return invalid(format!(
                            "manifest detection pattern exceeds maximum of {MAX_DISCOVERY_PATH_BYTES} bytes"
                        ));
                    }
                    let pattern_characters = pattern.chars().count();
                    for relative in &files {
                        let basename_characters = relative
                            .rsplit_once('/')
                            .map_or(relative.as_str(), |(_, basename)| basename)
                            .chars()
                            .count();
                        let path_characters = relative
                            .chars()
                            .count()
                            .checked_add(basename_characters)
                            .ok_or_else(|| {
                                EngineError::Invalid(
                                    "discovery match work counter overflow".to_owned(),
                                )
                            })?;
                        let work =
                            pattern_characters
                                .checked_mul(path_characters)
                                .ok_or_else(|| {
                                    EngineError::Invalid(
                                        "discovery match work counter overflow".to_owned(),
                                    )
                                })?;
                        match_work_units = match_work_units.checked_add(work).ok_or_else(|| {
                            EngineError::Invalid("discovery match work counter overflow".to_owned())
                        })?;
                        if match_work_units > MAX_DISCOVERY_MATCH_WORK_UNITS {
                            return invalid(format!(
                                "repository discovery exceeds maximum of {MAX_DISCOVERY_MATCH_WORK_UNITS} match work units"
                            ));
                        }
                        if path_matches(relative, &pattern) {
                            if evidence.len() >= MAX_DISCOVERY_EVIDENCE {
                                return invalid(format!(
                                    "repository discovery exceeds maximum of {MAX_DISCOVERY_EVIDENCE} evidence entries"
                                ));
                            }
                            let signal = SignalMatch {
                                level: level.to_owned(),
                                path: relative.clone(),
                                pattern: pattern.clone(),
                            };
                            evidence.push(json!({
                                "kind": "manifest-signal",
                                "level": level,
                                "manifest": platform_id,
                                "path": relative,
                                "pattern": pattern,
                            }));
                            matches.push(signal);
                        }
                    }
                }
            }

            let strong_roots = collapse_roots(
                matches
                    .iter()
                    .filter(|matched| matched.level == "strong")
                    .map(|matched| signal_root(&matched.path, &matched.pattern))
                    .collect(),
            );
            for module_path in strong_roots {
                let local = matches
                    .iter()
                    .filter(|matched| path_contains(&module_path, &matched.path))
                    .collect::<Vec<_>>();
                let levels = local
                    .iter()
                    .map(|matched| matched.level.as_str())
                    .collect::<BTreeSet<_>>();
                let confidence = levels
                    .iter()
                    .map(|level| match *level {
                        "strong" => 0.65,
                        "medium" => 0.25,
                        "weak" => 0.10,
                        _ => 0.0,
                    })
                    .sum::<f64>()
                    .min(1.0);
                let confidence = (confidence * 100.0).round() / 100.0;
                let local_evidence = local
                    .iter()
                    .map(|matched| matched.path.clone())
                    .collect::<BTreeSet<_>>();
                modules.push(json!({
                    "confidence": confidence,
                    "evidence": local_evidence,
                    "path": module_path,
                    "platform": platform_id,
                }));
            }
        }

        modules.sort_by(|left, right| {
            module_sort_key(left)
                .unwrap_or_default()
                .cmp(&module_sort_key(right).unwrap_or_default())
        });
        let platforms = modules
            .iter()
            .filter_map(|module| module.get("platform").and_then(Value::as_str))
            .map(str::to_owned)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        evidence.sort_by(|left, right| {
            evidence_sort_key(left)
                .unwrap_or_default()
                .cmp(&evidence_sort_key(right).unwrap_or_default())
        });
        let ambiguities = discovery_ambiguities(&modules)?;
        let kind = repository_kind(&modules, &platforms)?;
        let explicit = json!({
            "changed_files": sorted_unique_strings(changed_files.iter().map(String::as_str)),
            "cwd": cwd.map_or_else(
                || user_visible_path(&requested).to_string_lossy().into_owned(),
                |path| resolve_existing(path)
                    .map_or_else(
                        |_| path.to_string_lossy().into_owned(),
                        |path| user_visible_path(&path).to_string_lossy().into_owned(),
                    ),
            ),
            "target_files": sorted_unique_strings(target_files.iter().map(String::as_str)),
        });
        let shared_contracts = shared_contracts(&files);
        let target_modules = target_modules(&modules, &explicit, &root, &shared_contracts)?;
        Ok(json!({
            "ambiguities": ambiguities,
            "evidence": evidence,
            "explicit_context": explicit,
            "modules": modules,
            "platforms": platforms,
            "repository": {
                "kind": kind,
                "root": user_visible_path(&root).to_string_lossy(),
            },
            "schema_version": "1.0",
            "shared_contracts": shared_contracts,
            "testing": testing_profile(&files),
            "target_modules": target_modules,
        }))
    }
}

#[derive(Debug)]
struct SignalMatch {
    level: String,
    path: String,
    pattern: String,
}

fn resolve_existing(path: &Path) -> Result<PathBuf, EngineError> {
    std::fs::canonicalize(path).map_err(|error| {
        EngineError::Invalid(format!(
            "repository path cannot be resolved: {}: {error}",
            path.display()
        ))
    })
}

#[cfg(not(windows))]
fn user_visible_path(path: &Path) -> PathBuf {
    path.to_path_buf()
}

#[cfg(windows)]
fn user_visible_path(path: &Path) -> PathBuf {
    use std::ffi::OsString;
    use std::os::windows::ffi::{OsStrExt as _, OsStringExt as _};
    use std::path::{Component, Prefix};

    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return path.to_path_buf();
    };
    let mut normalized = match prefix.kind() {
        Prefix::VerbatimDisk(drive) => PathBuf::from(format!("{}:", char::from(drive))),
        Prefix::VerbatimUNC(server, share) => {
            let mut value = vec![u16::from(b'\\'), u16::from(b'\\')];
            value.extend(server.encode_wide());
            value.push(u16::from(b'\\'));
            value.extend(share.encode_wide());
            PathBuf::from(OsString::from_wide(&value))
        }
        _ => return path.to_path_buf(),
    };
    for component in components {
        normalized.push(component.as_os_str());
    }
    normalized
}

fn repository_root(requested: &Path) -> Result<PathBuf, EngineError> {
    let current = if requested.is_dir() {
        requested.to_path_buf()
    } else {
        requested
            .parent()
            .ok_or_else(|| EngineError::Invalid("repository path has no parent".to_owned()))?
            .to_path_buf()
    };
    for candidate in current.ancestors() {
        if candidate
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| IGNORED_DIRECTORIES.contains(&name))
        {
            break;
        }
        if candidate.join(".git").exists() {
            return Ok(candidate.to_path_buf());
        }
    }

    let candidates = current.ancestors().collect::<Vec<_>>();
    for candidate in candidates.into_iter().skip(1) {
        if candidate
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| IGNORED_DIRECTORIES.contains(&name))
        {
            break;
        }
        let groups = child_module_groups(candidate)?;
        let Ok(relative) = current.strip_prefix(candidate) else {
            continue;
        };
        let inside_group = relative
            .components()
            .next()
            .and_then(|component| component.as_os_str().to_str())
            .is_some_and(|name| groups.contains(name));
        if inside_group && (groups.len() >= 2 || groups.contains("apps")) {
            return Ok(candidate.to_path_buf());
        }
    }
    Ok(current)
}

fn child_module_groups(root: &Path) -> Result<BTreeSet<String>, EngineError> {
    let mut groups = BTreeSet::new();
    for entry in std::fs::read_dir(root).map_err(|error| {
        EngineError::Invalid(format!(
            "repository directory cannot be read: {}: {error}",
            root.display()
        ))
    })? {
        let entry = entry.map_err(|error| EngineError::Invalid(error.to_string()))?;
        if entry.path().is_dir()
            && let Some(name) = entry.file_name().to_str()
            && MODULE_GROUP_DIRECTORIES.contains(&name)
        {
            groups.insert(name.to_owned());
        }
    }
    Ok(groups)
}

fn repository_files(root: &Path, max_depth: usize) -> Result<Vec<String>, EngineError> {
    let mut found = BTreeSet::new();
    let mut stack = vec![root.to_path_buf()];
    let mut entry_count = 0_usize;
    while let Some(directory) = stack.pop() {
        for entry in std::fs::read_dir(&directory).map_err(|error| {
            EngineError::Invalid(format!(
                "repository directory cannot be read: {}: {error}",
                directory.display()
            ))
        })? {
            let entry = entry.map_err(|error| EngineError::Invalid(error.to_string()))?;
            entry_count = entry_count.checked_add(1).ok_or_else(|| {
                EngineError::Invalid("repository entry counter overflow".to_owned())
            })?;
            if entry_count > MAX_REPOSITORY_ENTRIES {
                return invalid(format!(
                    "repository discovery exceeds maximum of {MAX_REPOSITORY_ENTRIES} directory entries"
                ));
            }
            let path = entry.path();
            let relative = path.strip_prefix(root).map_err(|_| {
                EngineError::Invalid("repository entry escaped discovery root".to_owned())
            })?;
            let parts = relative_components(relative);
            if parts.len() > max_depth
                || parts
                    .iter()
                    .any(|part| IGNORED_DIRECTORIES.contains(&part.as_str()))
            {
                continue;
            }
            let relative_text = parts.join("/");
            if relative_text.len() > MAX_DISCOVERY_PATH_BYTES {
                return invalid(format!(
                    "repository path exceeds maximum of {MAX_DISCOVERY_PATH_BYTES} bytes"
                ));
            }
            let file_type = entry
                .file_type()
                .map_err(|error| EngineError::Invalid(error.to_string()))?;
            if file_type.is_symlink() {
                if path.is_file() || is_xcode_container(&path) {
                    found.insert(relative_text);
                    if found.len() > MAX_DISCOVERY_FILES {
                        return invalid(format!(
                            "repository discovery exceeds maximum of {MAX_DISCOVERY_FILES} files"
                        ));
                    }
                }
                continue;
            }
            if file_type.is_file() || is_xcode_container(&path) {
                found.insert(relative_text.clone());
                if found.len() > MAX_DISCOVERY_FILES {
                    return invalid(format!(
                        "repository discovery exceeds maximum of {MAX_DISCOVERY_FILES} files"
                    ));
                }
            }
            if file_type.is_dir() {
                stack.push(path);
            }
        }
    }
    Ok(found.into_iter().collect())
}

fn relative_components(path: &Path) -> Vec<String> {
    path.components()
        .map(|component| component.as_os_str().to_string_lossy().into_owned())
        .collect()
}

fn is_xcode_container(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|extension| extension.to_str()),
        Some("xcodeproj" | "xcworkspace")
    )
}

fn path_matches(relative: &str, pattern: &str) -> bool {
    let basename = relative
        .rsplit_once('/')
        .map_or(relative, |(_, basename)| basename);
    wildcard_match(relative, pattern) || wildcard_match(basename, pattern)
}

#[derive(Debug)]
enum GlobToken {
    AnySequence,
    AnyCharacter,
    Literal(char),
    CharacterClass {
        negated: bool,
        members: Vec<(char, char)>,
    },
}

fn wildcard_match(value: &str, pattern: &str) -> bool {
    let value = value.chars().collect::<Vec<_>>();
    let pattern = glob_tokens(pattern);
    let mut previous = vec![false; value.len() + 1];
    previous[0] = true;
    for token in pattern {
        let mut current = vec![false; value.len() + 1];
        match token {
            GlobToken::AnySequence => {
                current[0] = previous[0];
                for index in 1..=value.len() {
                    current[index] = previous[index] || current[index - 1];
                }
            }
            GlobToken::AnyCharacter => {
                current[1..].copy_from_slice(&previous[..value.len()]);
            }
            GlobToken::Literal(expected) => {
                for index in 1..=value.len() {
                    current[index] = previous[index - 1] && expected == value[index - 1];
                }
            }
            GlobToken::CharacterClass { negated, members } => {
                for index in 1..=value.len() {
                    let contained = members
                        .iter()
                        .any(|(start, end)| *start <= value[index - 1] && value[index - 1] <= *end);
                    current[index] = previous[index - 1] && (contained != negated);
                }
            }
        }
        previous = current;
    }
    previous[value.len()]
}

fn glob_tokens(pattern: &str) -> Vec<GlobToken> {
    let characters = pattern.chars().collect::<Vec<_>>();
    let mut tokens = Vec::new();
    let mut index = 0_usize;
    while index < characters.len() {
        match characters[index] {
            '*' => {
                if !matches!(tokens.last(), Some(GlobToken::AnySequence)) {
                    tokens.push(GlobToken::AnySequence);
                }
                index += 1;
            }
            '?' => {
                tokens.push(GlobToken::AnyCharacter);
                index += 1;
            }
            '[' => {
                let Some((token, next)) = glob_character_class(&characters, index) else {
                    tokens.push(GlobToken::Literal('['));
                    index += 1;
                    continue;
                };
                tokens.push(token);
                index = next;
            }
            literal => {
                tokens.push(GlobToken::Literal(literal));
                index += 1;
            }
        }
    }
    tokens
}

fn glob_character_class(characters: &[char], start: usize) -> Option<(GlobToken, usize)> {
    let mut cursor = start + 1;
    let negated = characters.get(cursor) == Some(&'!');
    if negated {
        cursor += 1;
    }
    let content_start = cursor;
    if characters.get(cursor) == Some(&']') {
        cursor += 1;
    }
    while cursor < characters.len() && characters[cursor] != ']' {
        cursor += 1;
    }
    if cursor >= characters.len() || cursor == content_start {
        return None;
    }
    let content = &characters[content_start..cursor];
    let mut members = Vec::new();
    let mut index = 0_usize;
    while index < content.len() {
        if index + 2 < content.len() && content[index + 1] == '-' {
            let start = content[index];
            let end = content[index + 2];
            if start <= end {
                members.push((start, end));
            }
            index += 3;
        } else {
            members.push((content[index], content[index]));
            index += 1;
        }
    }
    Some((GlobToken::CharacterClass { negated, members }, cursor + 1))
}

fn signal_root(relative: &str, pattern: &str) -> String {
    let path_parts = relative.split('/').collect::<Vec<_>>();
    let pattern_parts = pattern.split('/').collect::<Vec<_>>();
    let literal_multi_segment = pattern_parts.len() > 1
        && !pattern_parts.iter().any(|part| {
            part.chars()
                .any(|character| matches!(character, '*' | '?' | '['))
        });
    if literal_multi_segment {
        let prefix_length = path_parts.len().saturating_sub(pattern_parts.len());
        return if prefix_length == 0 {
            ".".to_owned()
        } else {
            path_parts[..prefix_length].join("/")
        };
    }
    relative
        .rsplit_once('/')
        .map_or_else(|| ".".to_owned(), |(parent, _)| parent.to_owned())
}

fn collapse_roots(roots: Vec<String>) -> Vec<String> {
    let mut roots = roots
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    roots.sort_by_key(|root| (path_depth(root), root.clone()));
    let mut result = Vec::<String>::new();
    for candidate in roots {
        if result.iter().any(|existing| {
            path_contains(existing, &candidate) && !is_structural_child(existing, &candidate)
        }) {
            continue;
        }
        result.push(candidate);
    }
    result.sort();
    result
}

fn is_structural_child(parent: &str, child: &str) -> bool {
    if parent == child || !path_contains(parent, child) {
        return false;
    }
    let parent_parts = if parent == "." {
        Vec::new()
    } else {
        parent.split('/').collect::<Vec<_>>()
    };
    let child_parts = child.split('/').collect::<Vec<_>>();
    let relative = &child_parts[parent_parts.len()..];
    relative.first().is_some_and(|part| {
        MODULE_GROUP_DIRECTORIES.contains(part)
            || parent_parts
                .last()
                .is_some_and(|parent| MODULE_GROUP_DIRECTORIES.contains(parent))
    })
}

fn path_contains(parent: &str, child: &str) -> bool {
    parent == "."
        || parent == child
        || child
            .strip_prefix(parent)
            .is_some_and(|rest| rest.starts_with('/'))
}

fn path_depth(path: &str) -> usize {
    if path == "." {
        0
    } else {
        path.split('/').count()
    }
}

fn module_sort_key(value: &Value) -> Option<(String, String)> {
    Some((
        value.get("path")?.as_str()?.to_owned(),
        value.get("platform")?.as_str()?.to_owned(),
    ))
}

fn evidence_sort_key(value: &Value) -> Option<(String, String, String)> {
    Some((
        value.get("path")?.as_str()?.to_owned(),
        value.get("manifest")?.as_str()?.to_owned(),
        value.get("level")?.as_str()?.to_owned(),
    ))
}

fn repository_kind(modules: &[Value], platforms: &[String]) -> Result<&'static str, EngineError> {
    if modules.is_empty() {
        return Ok("unknown");
    }
    let roots = modules
        .iter()
        .map(|module| {
            module
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| EngineError::Invalid("module path is invalid".to_owned()))
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    Ok(if roots.len() == 1 {
        "single"
    } else if platforms.len() > 1 {
        "monorepo"
    } else {
        "multi-module"
    })
}

fn discovery_ambiguities(modules: &[Value]) -> Result<Vec<Value>, EngineError> {
    let mut by_path = BTreeMap::<String, BTreeSet<String>>::new();
    for module in modules {
        let (path, platform) = module_sort_key(module)
            .ok_or_else(|| EngineError::Invalid("module identity is invalid".to_owned()))?;
        by_path.entry(path).or_default().insert(platform);
    }
    Ok(by_path
        .into_iter()
        .filter_map(|(path, candidates)| {
            let orthogonal = candidates == BTreeSet::from(["desktop".to_owned(), "web".to_owned()]);
            (candidates.len() > 1 && !orthogonal).then(|| {
                json!({
                    "candidates": candidates,
                    "path": path,
                    "reason": "multiple-platform-signals",
                })
            })
        })
        .collect())
}

fn testing_profile(files: &[String]) -> Value {
    let joined = files.join("\n");
    let frameworks = [
        ("XCTest", &["Tests/", ".xctestplan"][..]),
        ("JUnit", &["src/test"][..]),
        ("Playwright", &["playwright.config"][..]),
        ("pytest", &["pytest.ini", "test_"][..]),
    ]
    .into_iter()
    .filter_map(|(framework, markers)| {
        markers
            .iter()
            .any(|marker| joined.contains(marker))
            .then_some(framework)
    })
    .collect::<Vec<_>>();
    json!({"unit": {"available": !frameworks.is_empty(), "frameworks": frameworks}})
}

fn shared_contracts(files: &[String]) -> Vec<Value> {
    let patterns = [
        ("graphql", &["*.graphql", "schema.graphql"][..]),
        (
            "openapi",
            &[
                "openapi.yaml",
                "openapi.yml",
                "openapi.json",
                "swagger.yaml",
                "swagger.json",
            ][..],
        ),
        ("protobuf", &["*.proto"][..]),
    ];
    files
        .iter()
        .filter_map(|relative| {
            patterns.iter().find_map(|(kind, patterns)| {
                patterns
                    .iter()
                    .any(|pattern| path_matches(relative, pattern))
                    .then(|| {
                        json!({
                            "consumer_resolution": "conservative-all-modules",
                            "kind": kind,
                            "path": relative,
                        })
                    })
            })
        })
        .collect()
}

fn target_modules(
    modules: &[Value],
    explicit: &Value,
    root: &Path,
    shared_contracts: &[Value],
) -> Result<Vec<Value>, EngineError> {
    let explicit = object(explicit, "explicit context")?;
    let target_files = explicit.get("target_files").map_or_else(
        || Ok(Vec::new()),
        |value| string_array(value, "target_files"),
    )?;
    let changed_files = explicit.get("changed_files").map_or_else(
        || Ok(Vec::new()),
        |value| string_array(value, "changed_files"),
    )?;
    let mut candidates = if target_files.is_empty() {
        changed_files
    } else {
        target_files
    };
    let contract_paths = shared_contracts
        .iter()
        .filter_map(|contract| contract.get("path").and_then(Value::as_str))
        .collect::<BTreeSet<_>>();
    let normalized = candidates
        .iter()
        .map(|candidate| normalize_candidate(candidate))
        .collect::<Result<BTreeSet<_>, _>>()?;
    if normalized
        .iter()
        .any(|candidate| contract_paths.contains(candidate.as_str()))
    {
        return Ok(modules.to_vec());
    }
    if candidates.is_empty() {
        let cwd = explicit.get("cwd").and_then(Value::as_str).unwrap_or(".");
        let visible_root = user_visible_path(root);
        let relative = Path::new(cwd)
            .strip_prefix(&visible_root)
            .ok()
            .map(relative_components)
            .map(|parts| parts.join("/"))
            .filter(|relative| !relative.is_empty())
            .unwrap_or_else(|| ".".to_owned());
        if relative != "." {
            candidates.push(relative);
        }
    }
    let mut selected = BTreeMap::<(String, String), Value>::new();
    for candidate in candidates {
        let normalized = normalize_candidate(&candidate)?;
        let matching = modules
            .iter()
            .filter(|module| {
                module
                    .get("path")
                    .and_then(Value::as_str)
                    .is_some_and(|path| path_contains(path, &normalized))
            })
            .collect::<Vec<_>>();
        let longest = matching
            .iter()
            .filter_map(|module| module.get("path").and_then(Value::as_str))
            .map(path_depth)
            .max();
        if let Some(longest) = longest {
            for module in matching {
                let key = module_sort_key(module)
                    .ok_or_else(|| EngineError::Invalid("module identity is invalid".to_owned()))?;
                if path_depth(&key.0) == longest {
                    selected.insert(key, module.clone());
                }
            }
        }
    }
    Ok(selected.into_values().collect())
}

fn normalize_candidate(value: &str) -> Result<String, EngineError> {
    if value.len() > MAX_DISCOVERY_PATH_BYTES {
        return invalid(format!(
            "target path exceeds maximum of {MAX_DISCOVERY_PATH_BYTES} bytes"
        ));
    }
    let normalized = value.replace('\\', "/");
    let bytes = normalized.as_bytes();
    if normalized.starts_with('/')
        || normalized.starts_with("//")
        || (bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic())
    {
        return invalid(format!("target path must be repository-relative: {value}"));
    }
    let components = normalized
        .split('/')
        .filter(|component| !component.is_empty() && *component != ".")
        .collect::<Vec<_>>();
    if components.contains(&"..") {
        return invalid(format!(
            "target path cannot escape repository root: {value}"
        ));
    }
    Ok(components.join("/"))
}

/// Classify task type, risk, and disciplines with the Python baseline's
/// precedence rules.
#[must_use]
pub fn classify_task(task: &str) -> Value {
    let lowered = task.to_lowercase();
    let task_type = if contains_any(&lowered, &["review", "审查", "评审"]) {
        "review-only"
    } else if contains_any(&lowered, &["测试", "qa", "regression", "回归"])
        && !contains_any(&lowered, &["实现", "修复", "implement", "fix"])
    {
        "qa-only"
    } else if contains_any(&lowered, &["文档", "docs", "document"]) {
        "doc-only"
    } else if contains_any(&lowered, &["调查", "分析", "investigate", "why"]) {
        "investigation"
    } else if contains_any(
        &lowered,
        &["跨平台", "contract", "schema", "migration", "并发", "权限"],
    ) {
        "code-risky"
    } else if contains_any(
        &lowered,
        &["单文件", "小改动", "small change", "code-small"],
    ) {
        "code-small"
    } else {
        "code-medium"
    };
    let risk = match task_type {
        "code-risky" => "high",
        "code-medium" => "medium",
        _ => "low",
    };
    let mut disciplines = BTreeSet::from(["development"]);
    for (discipline, terms) in [
        ("design", &["ui", "design", "figma", "sketch", "设计"][..]),
        ("qa", &["test", "qa", "测试", "回归"]),
        (
            "build",
            &["build setting", "xcconfig", "签名", "archive", "构建配置"],
        ),
        ("debug", &["crash", "崩溃", "调试", "debug", "异常"]),
        (
            "performance",
            &["performance", "性能", "掉帧", "内存", "instruments"],
        ),
        (
            "automation",
            &["automation", "自动化", "simulator", "真机", "设备"],
        ),
        (
            "documentation",
            &["html", "prd", "正式方案", "正式报告", "接口说明", "handoff"],
        ),
    ] {
        if contains_any(&lowered, terms) {
            disciplines.insert(discipline);
        }
    }
    json!({
        "disciplines": disciplines,
        "risk": risk,
        "type": task_type,
    })
}

/// Merge explicit policy layers using field-level strategies.
///
/// # Errors
/// Returns a fail-closed error for malformed layers, unknown strategies, or
/// attempts to override locked fields.
pub fn merge_policy_layers(layers: &[Value]) -> Result<(Value, Vec<Value>), EngineError> {
    if layers.len() > MAX_POLICY_LAYERS {
        return invalid(format!(
            "policy merge exceeds maximum of {MAX_POLICY_LAYERS} layers"
        ));
    }
    let mut result = Map::new();
    let mut locked = BTreeSet::new();
    let mut decisions = Vec::new();
    let mut field_count = 0_usize;
    let mut item_count = 0_usize;
    for layer in layers {
        let layer = object(layer, "policy layer")?;
        let source = layer.get("source").map_or(Ok("unknown"), |value| {
            value.as_str().ok_or_else(|| {
                EngineError::Invalid("policy layer source must be a string".to_owned())
            })
        })?;
        let values = optional_object(layer, "values")?;
        let strategies = optional_object(layer, "strategies")?;
        let sorted_values = values.iter().collect::<BTreeMap<_, _>>();
        for (field, incoming) in sorted_values {
            field_count = field_count
                .checked_add(1)
                .ok_or_else(|| EngineError::Invalid("policy field counter overflow".to_owned()))?;
            if field_count > MAX_POLICY_FIELDS {
                return invalid(format!(
                    "policy merge exceeds maximum of {MAX_POLICY_FIELDS} fields"
                ));
            }
            item_count = item_count
                .checked_add(policy_value_items(incoming)?)
                .ok_or_else(|| EngineError::Invalid("policy item counter overflow".to_owned()))?;
            if item_count > MAX_POLICY_ITEMS {
                return invalid(format!(
                    "policy merge exceeds maximum of {MAX_POLICY_ITEMS} items"
                ));
            }
            let strategy = strategies.get(field).map_or(Ok("replace"), |value| {
                value.as_str().ok_or_else(|| {
                    EngineError::Invalid(format!(
                        "policy merge strategy for {field} must be a string"
                    ))
                })
            })?;
            if !is_merge_strategy(strategy) {
                return invalid(format!("unknown merge strategy: {strategy}"));
            }
            if locked.contains(field)
                && result.get(field).is_some_and(|current| current != incoming)
            {
                return invalid(format!("locked policy field cannot be overridden: {field}"));
            }
            let merged = merge_value(result.get(field), incoming, strategy)?;
            result.insert(field.clone(), merged);
            if strategy == "locked" {
                locked.insert(field.clone());
            }
            decisions.push(json!({
                "confidence": 1.0,
                "decision": format!("merge constraint: {field}"),
                "merge_strategy": strategy,
                "overridden_candidates": [],
                "reason_code": "POLICY_LAYER_MERGE",
                "source": source,
            }));
        }
    }
    Ok((Value::Object(result), decisions))
}

/// Resolve selected platforms, task classification, constraints, decisions,
/// and the canonical policy fingerprint.
///
/// # Errors
/// Returns a fail-closed error for malformed profile or policy inputs.
#[allow(clippy::too_many_lines)]
pub fn resolve_policy(
    profile: &Value,
    task_text: &str,
    explicit_platforms: &[String],
    constraints: Option<&Value>,
    policy_layers: &[Value],
) -> Result<Value, EngineError> {
    let profile = object(profile, "project profile")?;
    let task = classify_task(task_text);
    let explicit = sorted_unique_strings(explicit_platforms.iter().map(String::as_str));
    let inferred = platforms_from_task(task_text);
    let targeted = profile.get("target_modules").map_or_else(
        || Ok(Vec::new()),
        |modules| module_platforms(modules, "project profile target_modules"),
    )?;
    let discovered = profile
        .get("platforms")
        .map_or_else(|| Ok(Vec::new()), |value| string_array(value, "platforms"))?;

    let (selected, source, reason, confidence) = if !explicit.is_empty() {
        (explicit, "user-explicit", "EXPLICIT_PLATFORM_LOCK", 1.0)
    } else if !inferred.is_empty() {
        (inferred, "task-text", "TASK_PLATFORM_MATCH", 0.95)
    } else if !targeted.is_empty() {
        (targeted, "target-files-or-cwd", "TARGET_MODULE_MATCH", 0.9)
    } else {
        let confidence = if discovered.is_empty() { 0.0 } else { 0.8 };
        (
            sorted_unique_strings(discovered.iter().map(String::as_str)),
            "project-profile",
            "DISCOVERY_EVIDENCE",
            confidence,
        )
    };

    let mut decisions = vec![json!({
        "confidence": confidence,
        "decision": format!(
            "select platforms: {}",
            if selected.is_empty() {
                "unknown".to_owned()
            } else {
                selected.join(", ")
            }
        ),
        "merge_strategy": if source == "user-explicit" { "locked" } else { "replace" },
        "overridden_candidates": discovered
            .iter()
            .filter(|platform| !selected.contains(platform))
            .cloned()
            .collect::<BTreeSet<_>>(),
        "reason_code": if selected.is_empty() { "NO_PLATFORM_EVIDENCE" } else { reason },
        "source": source,
    })];

    let ambiguities = profile
        .get("ambiguities")
        .cloned()
        .unwrap_or_else(|| json!([]));
    let unique_target = unique_target_module_count(profile.get("target_modules"))? == 1;
    let unresolved = !explicit_platforms.is_empty() || !platforms_from_task(task_text).is_empty();
    let unresolved_ambiguities = if !unresolved
        && !unique_target
        && ambiguities
            .as_array()
            .is_some_and(|items| !items.is_empty())
    {
        Some(ambiguities)
    } else {
        None
    };

    let (merged, merge_decisions) = merge_policy_layers(policy_layers)?;
    let mut merged = object(&merged, "merged policy constraints")?.clone();
    if let Some(constraints) = constraints {
        for (key, value) in object(constraints, "policy constraints")? {
            merged.insert(key.clone(), value.clone());
        }
    }
    if let Some(ambiguities) = unresolved_ambiguities {
        merged.insert("routing_ambiguities".to_owned(), ambiguities);
        decisions.push(json!({
            "confidence": 0.0,
            "decision": "block automatic platform selection until ambiguity is resolved",
            "merge_strategy": "locked",
            "overridden_candidates": [],
            "reason_code": "UNRESOLVED_PLATFORM_AMBIGUITY",
            "source": "project-profile",
        }));
    }
    decisions.extend(merge_decisions);

    let task = object(&task, "classified task")?;
    let mut value = json!({
        "constraints": merged,
        "decisions": decisions,
        "schema_version": "1.0",
        "selected_platforms": selected,
        "task": {
            "disciplines": task["disciplines"].clone(),
            "risk": task["risk"].clone(),
            "text": task_text,
            "type": task["type"].clone(),
        },
    });
    let fingerprint = canonical_sha256(&value)?;
    value
        .as_object_mut()
        .ok_or_else(|| EngineError::Invalid("resolved policy must be an object".to_owned()))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    validate_resolved_policy(&value)?;
    Ok(value)
}

/// Compile a resolved policy into the deterministic capability DAG used by the
/// Python production baseline.
///
/// # Errors
/// Returns a fail-closed error for malformed policy inputs, registry
/// ambiguities, missing graph references, or dependency cycles.
#[allow(clippy::too_many_lines)]
pub fn compile_plan(
    registry: &ManifestRegistry,
    profile: &Value,
    policy: &Value,
) -> Result<Value, EngineError> {
    compile_plan_with_package_lock(registry, profile, policy, None)
}

/// Compile a deterministic workflow plan and optionally freeze it to a
/// validated package Lockfile.
///
/// # Errors
/// Returns a fail-closed error for malformed policy or Lockfile inputs,
/// registry ambiguities, missing graph references, or dependency cycles.
#[allow(clippy::too_many_lines)]
pub fn compile_plan_with_package_lock(
    registry: &ManifestRegistry,
    profile: &Value,
    policy: &Value,
    package_lock: Option<&Value>,
) -> Result<Value, EngineError> {
    validate_resolved_policy(policy)?;
    let policy_object = object(policy, "resolved policy")?;
    let task = object(
        policy_object
            .get("task")
            .ok_or_else(|| EngineError::Invalid("resolved policy task is missing".to_owned()))?,
        "resolved policy task",
    )?;
    let task_type = task
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| EngineError::Invalid("resolved policy task type is invalid".to_owned()))?;
    let disciplines = task.get("disciplines").map_or_else(
        || Ok(Vec::new()),
        |value| string_array(value, "task disciplines"),
    )?;
    let selected_platforms = policy_object.get("selected_platforms").map_or_else(
        || Ok(Vec::new()),
        |value| string_array(value, "selected_platforms"),
    )?;
    if disciplines.len() > MAX_POLICY_ITEMS {
        return invalid(format!(
            "plan disciplines exceed maximum of {MAX_POLICY_ITEMS} items"
        ));
    }
    if selected_platforms.len() > MAX_POLICY_ITEMS {
        return invalid(format!(
            "plan selected platforms exceed maximum of {MAX_POLICY_ITEMS} items"
        ));
    }
    let constraints = policy_object.get("constraints").map_or_else(
        || Ok(empty_object()),
        |value| object(value, "policy constraints"),
    )?;

    let mut nodes = Vec::<Value>::new();
    let mut edges = Vec::<Value>::new();
    let mut missing = Vec::<String>::new();
    let mut bootstrap_required = Vec::<Value>::new();
    let mut routing_blocked = false;
    if requires_platform(task_type) && selected_platforms.is_empty() {
        missing.push("routing.platform-selection".to_owned());
        routing_blocked = true;
    }
    if constraints
        .get("routing_ambiguities")
        .is_some_and(python_truthy)
    {
        missing.push("routing.ambiguity-resolution".to_owned());
        routing_blocked = true;
    }

    let intent = registry.resolve_binding("core.intent-lock", None)?;
    nodes.push(plan_node(
        "intent",
        "core.intent-lock",
        true,
        intent.as_ref(),
    )?);
    let mut previous_ids = vec!["intent".to_owned()];

    if let Some(analysis) = registry.resolve_binding("workflow.analysis", None)? {
        nodes.push(plan_node(
            "workflow-analysis",
            "workflow.analysis",
            true,
            Some(&analysis),
        )?);
        edges.push(json!({"from": "intent", "to": "workflow-analysis"}));
        previous_ids = vec!["workflow-analysis".to_owned()];
    }
    if task_type.starts_with("code")
        && let Some(orchestration) = registry.resolve_binding("workflow.orchestration", None)?
    {
        nodes.push(plan_node(
            "workflow-orchestration",
            "workflow.orchestration",
            true,
            Some(&orchestration),
        )?);
        for source in &previous_ids {
            edges.push(json!({"from": source, "to": "workflow-orchestration"}));
        }
        previous_ids = vec!["workflow-orchestration".to_owned()];
    }

    let qa_requested = disciplines.iter().any(|discipline| discipline == "qa");
    let mut qa_provider_available = true;
    for capability in [
        "qa.plan.compile",
        "qa.coverage.compile",
        "qa.contract.validate",
        "qa.report.aggregate",
    ] {
        if registry.resolve_binding(capability, None)?.is_none() {
            qa_provider_available = false;
        }
    }
    let mut has_automatic_verification = false;
    for platform in &selected_platforms {
        if registry
            .resolve_binding(&format!("verification.{platform}.auto"), Some(platform))?
            .is_some()
        {
            has_automatic_verification = true;
        }
    }
    let qa_needed = qa_requested && (qa_provider_available || !has_automatic_verification);
    if qa_needed {
        for (node_id, capability) in [
            ("qa-plan", "qa.plan.compile"),
            ("qa-coverage", "qa.coverage.compile"),
            ("qa-design", "qa.contract.validate"),
        ] {
            let resolution = registry.resolve_binding(capability, None)?;
            if resolution.is_none() {
                missing.push(capability.to_owned());
            }
            nodes.push(plan_node(node_id, capability, true, resolution.as_ref())?);
            for source in sorted_unique_strings(previous_ids.iter().map(String::as_str)) {
                edges.push(json!({"from": source, "to": node_id}));
            }
            previous_ids = vec![node_id.to_owned()];
        }
    }

    let platform_branch_roots = sorted_unique_strings(previous_ids.iter().map(String::as_str));
    let mut platform_tails = Vec::<String>::new();
    for platform in &selected_platforms {
        let disciplines = disciplines.iter().map(String::as_str).collect();
        let capabilities = required_platform_capabilities(platform, task_type, &disciplines);
        let bootstrap = registry.bootstrap_requirement(platform)?;
        if !capabilities.is_empty()
            && let Some(bootstrap) = bootstrap
        {
            bootstrap_required.push(bootstrap);
        }
        let mut platform_previous = platform_branch_roots.clone();
        for (index, capability) in capabilities.iter().enumerate() {
            let node_id = format!("{platform}-{}", index + 1);
            let resolution = registry.resolve_binding(capability, Some(platform))?;
            if resolution.is_none() {
                missing.push(capability.clone());
            }
            nodes.push(plan_node(&node_id, capability, true, resolution.as_ref())?);
            for source in &platform_previous {
                edges.push(json!({"from": source, "to": node_id}));
            }
            platform_previous = vec![node_id];
        }
        platform_tails.extend(platform_previous);
    }
    previous_ids = if platform_tails.is_empty() {
        platform_branch_roots
    } else {
        sorted_unique_strings(platform_tails.iter().map(String::as_str))
    };

    if task_type.starts_with("code") || task_type == "review-only" {
        let mut extension_ids = Vec::new();
        for platform in &selected_platforms {
            let capability = format!("review.{platform}.static");
            let Some(resolution) = registry.resolve_binding(&capability, Some(platform))? else {
                continue;
            };
            let node_id = format!("review-{platform}");
            nodes.push(plan_node(&node_id, &capability, true, Some(&resolution))?);
            for source in sorted_unique_strings(previous_ids.iter().map(String::as_str)) {
                edges.push(json!({"from": source, "to": node_id}));
            }
            extension_ids.push(node_id);
        }
        if !extension_ids.is_empty() {
            previous_ids = extension_ids;
        }
    }

    if qa_needed {
        let mut post_qa = Vec::new();
        if task_type == "qa-only" {
            post_qa.extend([
                ("qa-triage", "qa.contract.validate"),
                ("qa-regression", "qa.contract.validate"),
            ]);
        }
        post_qa.push(("qa-report", "qa.report.aggregate"));
        for (node_id, capability) in post_qa {
            let resolution = registry.resolve_binding(capability, None)?;
            if resolution.is_none() {
                missing.push(capability.to_owned());
            }
            nodes.push(plan_node(node_id, capability, true, resolution.as_ref())?);
            for source in sorted_unique_strings(previous_ids.iter().map(String::as_str)) {
                edges.push(json!({"from": source, "to": node_id}));
            }
            previous_ids = vec![node_id.to_owned()];
        }
    }

    let review_platform = if selected_platforms.len() == 1 {
        selected_platforms[0].as_str()
    } else {
        "*"
    };
    let review = registry.resolve_binding("review.independent", Some(review_platform))?;
    if review.is_none() {
        missing.push("review.independent".to_owned());
    }
    nodes.push(plan_node(
        "review",
        "review.independent",
        task_type.starts_with("code") || task_type == "review-only",
        review.as_ref(),
    )?);
    for source in sorted_unique_strings(previous_ids.iter().map(String::as_str)) {
        if source != "review" {
            edges.push(json!({"from": source, "to": "review"}));
        }
    }
    let reporting = match registry.resolve_binding("reporting.delivery", None)? {
        Some(reporting) => Some(reporting),
        None => registry.resolve_binding("report.apple.delivery", Some(review_platform))?,
    };
    if let Some(reporting) = reporting {
        nodes.push(plan_node(
            "report",
            &reporting.capability_id,
            true,
            Some(&reporting),
        )?);
        edges.push(json!({"from": "review", "to": "report"}));
    }
    validate_plan_graph_limits(&nodes, &edges)?;
    topological_order(&nodes, &edges)?;

    let missing_set = missing.iter().cloned().collect::<BTreeSet<_>>();
    let provider_blocked = nodes.iter().any(|node| {
        node.get("mandatory").and_then(Value::as_bool) == Some(true)
            && node
                .get("capability")
                .and_then(Value::as_str)
                .is_some_and(|capability| missing_set.contains(capability))
    });
    let status = if routing_blocked || provider_blocked || !bootstrap_required.is_empty() {
        "blocked"
    } else if !missing.is_empty() {
        "degraded"
    } else {
        "ready"
    };
    edges.sort_by(|left, right| edge_sort_key(left).cmp(&edge_sort_key(right)));
    bootstrap_required.sort_by(|left, right| {
        left.get("platform")
            .and_then(Value::as_str)
            .cmp(&right.get("platform").and_then(Value::as_str))
    });
    let mut content = json!({
        "edges": edges,
        "missing_capabilities": missing_set,
        "nodes": nodes,
        "profile_fingerprint": canonical_sha256(profile)?,
        "policy_fingerprint": policy_object
            .get("fingerprint")
            .and_then(Value::as_str)
            .map_or_else(|| canonical_sha256(policy), |value| Ok(value.to_owned()))?,
        "registry_fingerprint": registry.digest()?,
        "schema_version": "1.0",
        "status": status,
        "workflow": workflow_contract(task_type, &disciplines),
    });
    if !bootstrap_required.is_empty() {
        content
            .as_object_mut()
            .ok_or_else(|| EngineError::Invalid("plan content must be an object".to_owned()))?
            .insert(
                "bootstrap_required".to_owned(),
                Value::Array(bootstrap_required),
            );
    }
    if let Some(package_lock) = package_lock {
        validate_package_lock(package_lock)?;
        let lock_hash = package_lock
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EngineError::Invalid("agent-skills-lock fingerprint is invalid".to_owned())
            })?;
        content
            .as_object_mut()
            .ok_or_else(|| EngineError::Invalid("plan content must be an object".to_owned()))?
            .insert(
                "package_lock_hash".to_owned(),
                Value::String(lock_hash.to_owned()),
            );
    }
    let fingerprint = canonical_sha256(&content)?;
    let mut plan = content
        .as_object()
        .ok_or_else(|| EngineError::Invalid("plan content must be an object".to_owned()))?
        .clone();
    plan.insert("fingerprint".to_owned(), Value::String(fingerprint.clone()));
    plan.insert(
        "plan_id".to_owned(),
        Value::String(format!("plan-{}", &fingerprint[..12])),
    );
    let plan = Value::Object(plan);
    validate_compiled_plan(&plan)?;
    if let Some(package_lock) = package_lock {
        validate_plan_package_lock(&plan, package_lock)?;
    }
    Ok(plan)
}

fn plan_node(
    node_id: &str,
    capability: &str,
    mandatory: bool,
    resolution: Option<&ResolvedBinding>,
) -> Result<Value, EngineError> {
    let contract = resolution
        .map(|resolution| object(&resolution.contract, "capability contract"))
        .transpose()?;
    let idempotent = contract
        .and_then(|contract| contract.get("idempotent"))
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let default_permission = if capability == "core.intent-lock" {
        "repository-read-only"
    } else {
        "project-read-execute"
    };
    Ok(json!({
        "approval": null,
        "binding": resolution.map_or(Value::Null, |resolution| resolution.binding.clone()),
        "capability": capability,
        "id": node_id,
        "idempotent": idempotent,
        "mandatory": mandatory,
        "max_retries": i32::from(idempotent),
        "permission_profile": contract
            .and_then(|contract| contract.get("permission_profile"))
            .cloned()
            .unwrap_or_else(|| json!(default_permission)),
        "provider": resolution.map_or(Value::Null, |resolution| json!(resolution.provider_id)),
        "provider_manifest_digest": resolution
            .map_or(Value::Null, |resolution| json!(resolution.manifest_digest)),
        "resource_keys": contract
            .and_then(|contract| contract.get("concurrency_keys"))
            .cloned()
            .unwrap_or_else(|| json!([])),
        "side_effects": contract
            .and_then(|contract| contract.get("side_effects"))
            .cloned()
            .unwrap_or_else(|| json!([])),
        "status": "pending",
        "timeout_seconds": 300,
    }))
}

fn requires_platform(task_type: &str) -> bool {
    task_type.starts_with("code") || matches!(task_type, "qa-only" | "investigation")
}

fn workflow_contract(task_type: &str, disciplines: &[String]) -> Value {
    let (mut roles, independent_review) = if task_type.starts_with("code") {
        (vec!["explorer", "builder", "reporter", "reviewer"], true)
    } else if task_type == "review-only" {
        (vec!["reviewer", "reporter"], true)
    } else if task_type == "qa-only" {
        (
            vec!["explorer", "test-executor", "reporter", "reviewer"],
            false,
        )
    } else {
        (vec!["explorer", "builder", "reporter"], false)
    };
    if disciplines.iter().any(|discipline| discipline == "qa") {
        let mut expanded = roles[..roles.len() - 1].to_vec();
        expanded.extend([
            "case-designer",
            "test-executor",
            "triage",
            "regression-owner",
        ]);
        expanded.push(roles[roles.len() - 1]);
        let mut seen = BTreeSet::new();
        roles = expanded
            .into_iter()
            .filter(|role| seen.insert(*role))
            .collect();
    }
    json!({
        "checkpoints": ["CP0", "CP1", "CP2", "CP3"],
        "independent_review": independent_review,
        "roles": roles,
    })
}

fn edge_sort_key(value: &Value) -> (Option<&str>, Option<&str>) {
    (
        value.get("from").and_then(Value::as_str),
        value.get("to").and_then(Value::as_str),
    )
}

fn topological_order(nodes: &[Value], edges: &[Value]) -> Result<Vec<String>, EngineError> {
    let node_id_values = nodes
        .iter()
        .map(|node| {
            node.get("id")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid("plan node id is invalid".to_owned()))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let node_ids = node_id_values.iter().cloned().collect::<BTreeSet<_>>();
    if node_ids.len() != node_id_values.len() {
        return invalid("workflow-plan node ids must be present and unique");
    }
    let mut incoming = node_ids
        .iter()
        .map(|node_id| (node_id.clone(), 0_usize))
        .collect::<BTreeMap<_, _>>();
    let mut outgoing = BTreeMap::<String, Vec<String>>::new();
    for edge in edges {
        let from = edge
            .get("from")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("plan edge source is invalid".to_owned()))?;
        let to = edge
            .get("to")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("plan edge target is invalid".to_owned()))?;
        if !node_ids.contains(from) || !node_ids.contains(to) {
            return invalid("edge references unknown node");
        }
        *incoming
            .get_mut(to)
            .ok_or_else(|| EngineError::Invalid("plan edge target is unknown".to_owned()))? += 1;
        outgoing
            .entry(from.to_owned())
            .or_default()
            .push(to.to_owned());
    }
    let mut queue = incoming
        .iter()
        .filter_map(|(node_id, count)| (*count == 0).then_some(node_id.clone()))
        .collect::<std::collections::VecDeque<_>>();
    let mut result = Vec::new();
    while let Some(node_id) = queue.pop_front() {
        result.push(node_id.clone());
        let mut targets = outgoing.remove(&node_id).unwrap_or_default();
        targets.sort();
        for target in targets {
            let count = incoming
                .get_mut(&target)
                .ok_or_else(|| EngineError::Invalid("plan edge target is unknown".to_owned()))?;
            *count -= 1;
            if *count == 0 {
                queue.push_back(target);
            }
        }
    }
    if result.len() != node_ids.len() {
        return invalid("workflow plan contains dependency cycle");
    }
    Ok(result)
}

fn validate_plan_graph_limits(nodes: &[Value], edges: &[Value]) -> Result<(), EngineError> {
    if nodes.len() > MAX_PLAN_NODES {
        return invalid(format!(
            "workflow plan exceeds maximum of {MAX_PLAN_NODES} nodes"
        ));
    }
    if edges.len() > MAX_PLAN_EDGES {
        return invalid(format!(
            "workflow plan exceeds maximum of {MAX_PLAN_EDGES} edges"
        ));
    }
    Ok(())
}

/// Validate one compiled Workflow Plan identity and dependency graph.
///
/// # Errors
/// Returns an error for malformed graph data, limit violations, cycles, or a
/// plan identity that does not match its canonical content.
pub fn validate_compiled_plan(value: &Value) -> Result<(), EngineError> {
    let plan = object(value, "workflow-plan")?;
    let nodes = plan
        .get("nodes")
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid("workflow-plan nodes must be an array".to_owned()))?;
    let edges = plan
        .get("edges")
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid("workflow-plan edges must be an array".to_owned()))?;
    validate_plan_graph_limits(nodes, edges)?;
    topological_order(nodes, edges)?;
    let fingerprint = plan
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| EngineError::Invalid("workflow-plan fingerprint is invalid".to_owned()))?;
    let plan_id = plan
        .get("plan_id")
        .and_then(Value::as_str)
        .ok_or_else(|| EngineError::Invalid("workflow-plan plan_id is invalid".to_owned()))?;
    let mut content = plan.clone();
    content.remove("fingerprint");
    content.remove("plan_id");
    let expected = canonical_sha256(&Value::Object(content))?;
    if fingerprint != expected {
        return invalid("workflow-plan fingerprint mismatch");
    }
    if plan_id != format!("plan-{}", &expected[..12]) {
        return invalid("workflow-plan id mismatch");
    }
    Ok(())
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_i64().map_or_else(
            || {
                value
                    .as_u64()
                    .map_or_else(|| value.as_f64() != Some(0.0), |value| value != 0)
            },
            |value| value != 0,
        ),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn merge_value(
    current: Option<&Value>,
    incoming: &Value,
    strategy: &str,
) -> Result<Value, EngineError> {
    if !is_merge_strategy(strategy) {
        return invalid(format!("unknown merge strategy: {strategy}"));
    }
    if current.is_none() || matches!(strategy, "replace" | "locked") {
        return Ok(incoming.clone());
    }
    let current = current
        .ok_or_else(|| EngineError::Invalid("policy merge current value is missing".to_owned()))?;
    match strategy {
        "append" => {
            let mut merged = value_as_list(current)?;
            merged.extend(value_as_list(incoming)?);
            if merged.len() > MAX_POLICY_ITEMS {
                return invalid(format!(
                    "policy append exceeds maximum of {MAX_POLICY_ITEMS} items"
                ));
            }
            Ok(Value::Array(merged))
        }
        "union" | "intersect" => scalar_set_merge(current, incoming, strategy),
        "deny-wins" => match (current.as_bool(), incoming.as_bool()) {
            (Some(current), Some(incoming)) => Ok(Value::Bool(current && incoming)),
            _ => invalid("deny-wins requires boolean values"),
        },
        _ => invalid(format!("unknown merge strategy: {strategy}")),
    }
}

fn value_as_list(value: &Value) -> Result<Vec<Value>, EngineError> {
    if let Some(value) = value.as_array() {
        return Ok(value.clone());
    }
    match value {
        Value::String(value) => Ok(value.chars().map(|item| json!(item.to_string())).collect()),
        Value::Object(value) => Ok(value.keys().map(|item| json!(item)).collect()),
        _ => invalid("policy list merge requires an iterable value"),
    }
}

fn policy_value_items(value: &Value) -> Result<usize, EngineError> {
    let mut count = 0_usize;
    let mut stack = vec![value];
    while let Some(value) = stack.pop() {
        count = count
            .checked_add(1)
            .ok_or_else(|| EngineError::Invalid("policy item counter overflow".to_owned()))?;
        if count > MAX_POLICY_ITEMS {
            return invalid(format!(
                "policy exceeds maximum of {MAX_POLICY_ITEMS} items"
            ));
        }
        match value {
            Value::Array(values) => stack.extend(values),
            Value::Object(values) => stack.extend(values.values()),
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
        }
    }
    Ok(count)
}

fn is_merge_strategy(strategy: &str) -> bool {
    matches!(
        strategy,
        "replace" | "append" | "union" | "intersect" | "deny-wins" | "locked"
    )
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum ScalarKey {
    Null,
    Number(PythonNumber),
    String(String),
}

#[derive(Clone, Debug)]
enum PythonNumber {
    Integer(DecimalKey),
    Float(f64),
}

impl PartialEq for PythonNumber {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for PythonNumber {}

impl PartialOrd for PythonNumber {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for PythonNumber {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self, other) {
            (Self::Integer(left), Self::Integer(right)) => left.cmp(right),
            (Self::Float(left), Self::Float(right)) => {
                left.partial_cmp(right).unwrap_or(Ordering::Equal)
            }
            (Self::Integer(left), Self::Float(right)) => integer_float_cmp(left, *right),
            (Self::Float(left), Self::Integer(right)) => integer_float_cmp(right, *left).reverse(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DecimalKey {
    negative: bool,
    digits: String,
    scale: i64,
}

impl Ord for DecimalKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.digits == "0" && other.digits == "0" {
            return Ordering::Equal;
        }
        if self.negative != other.negative {
            return if self.negative {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        let magnitude = decimal_magnitude_cmp(self, other);
        if self.negative {
            magnitude.reverse()
        } else {
            magnitude
        }
    }
}

impl PartialOrd for DecimalKey {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

fn decimal_magnitude_cmp(left: &DecimalKey, right: &DecimalKey) -> Ordering {
    let left_position = i64::try_from(left.digits.len())
        .unwrap_or(i64::MAX)
        .saturating_add(left.scale);
    let right_position = i64::try_from(right.digits.len())
        .unwrap_or(i64::MAX)
        .saturating_add(right.scale);
    match left_position.cmp(&right_position) {
        Ordering::Equal => {
            let maximum = left.digits.len().max(right.digits.len());
            for index in 0..maximum {
                let left_digit = left.digits.as_bytes().get(index).copied().unwrap_or(b'0');
                let right_digit = right.digits.as_bytes().get(index).copied().unwrap_or(b'0');
                match left_digit.cmp(&right_digit) {
                    Ordering::Equal => {}
                    ordering => return ordering,
                }
            }
            Ordering::Equal
        }
        ordering => ordering,
    }
}

fn decimal_key(value: &str) -> Result<DecimalKey, EngineError> {
    let (negative, unsigned) = value
        .strip_prefix('-')
        .map_or((false, value), |unsigned| (true, unsigned));
    let (mantissa, exponent) = if let Some(index) = unsigned.find(['e', 'E']) {
        let exponent = unsigned[index + 1..].parse::<i64>().map_err(|_| {
            EngineError::Invalid("policy numeric set value has invalid exponent".to_owned())
        })?;
        (&unsigned[..index], exponent)
    } else {
        (unsigned, 0_i64)
    };
    let (integer, fraction) = mantissa
        .split_once('.')
        .map_or((mantissa, ""), |parts| parts);
    let mut digits = format!("{integer}{fraction}")
        .trim_start_matches('0')
        .to_owned();
    if digits.is_empty() {
        return Ok(DecimalKey {
            negative: false,
            digits: "0".to_owned(),
            scale: 0,
        });
    }
    let mut scale =
        exponent
            .checked_sub(i64::try_from(fraction.len()).map_err(|_| {
                EngineError::Invalid("policy numeric set value is too long".to_owned())
            })?)
            .ok_or_else(|| {
                EngineError::Invalid("policy numeric set exponent is outside bounds".to_owned())
            })?;
    while digits.ends_with('0') {
        digits.pop();
        scale = scale.checked_add(1).ok_or_else(|| {
            EngineError::Invalid("policy numeric set exponent is outside bounds".to_owned())
        })?;
    }
    Ok(DecimalKey {
        negative,
        digits,
        scale,
    })
}

fn integer_float_cmp(integer: &DecimalKey, float: f64) -> Ordering {
    if float == f64::INFINITY {
        return Ordering::Less;
    }
    if float == f64::NEG_INFINITY {
        return Ordering::Greater;
    }
    let truncated = float.trunc();
    let truncated_key = decimal_key(&format!("{truncated:.0}")).unwrap_or(DecimalKey {
        negative: false,
        digits: "0".to_owned(),
        scale: 0,
    });
    match integer.cmp(&truncated_key) {
        Ordering::Equal if float.fract() > 0.0 => Ordering::Less,
        Ordering::Equal if float.fract() < 0.0 => Ordering::Greater,
        ordering => ordering,
    }
}

fn scalar_key(value: &Value) -> Result<ScalarKey, EngineError> {
    match value {
        Value::Null => Ok(ScalarKey::Null),
        Value::Bool(value) => Ok(ScalarKey::Number(PythonNumber::Integer(DecimalKey {
            negative: false,
            digits: if *value { "1" } else { "0" }.to_owned(),
            scale: 0,
        }))),
        Value::Number(value) => {
            let text = value.to_string();
            let number = if text.contains(['.', 'e', 'E']) {
                let parsed = text.parse::<f64>().map_err(|_| {
                    EngineError::Invalid("policy numeric set value is invalid".to_owned())
                })?;
                if !parsed.is_finite() {
                    return invalid("policy numeric set value must be finite");
                }
                PythonNumber::Float(parsed)
            } else {
                PythonNumber::Integer(decimal_key(&text)?)
            };
            Ok(ScalarKey::Number(number))
        }
        Value::String(value) => Ok(ScalarKey::String(value.clone())),
        Value::Array(_) | Value::Object(_) => {
            invalid("policy set merge requires hashable scalar values")
        }
    }
}

fn scalar_class(key: &ScalarKey) -> u8 {
    match key {
        ScalarKey::Null => 0,
        ScalarKey::Number(_) => 1,
        ScalarKey::String(_) => 2,
    }
}

fn scalar_map(value: &Value) -> Result<BTreeMap<ScalarKey, Value>, EngineError> {
    let values = value_as_list(value)?;
    if values.len() > MAX_POLICY_ITEMS {
        return invalid(format!(
            "policy set merge exceeds maximum of {MAX_POLICY_ITEMS} items"
        ));
    }
    let mut class = None;
    let mut result = BTreeMap::new();
    for value in values {
        let key = scalar_key(&value)?;
        let next_class = scalar_class(&key);
        if class.is_some_and(|current| current != next_class) {
            return invalid("policy set values cannot be sorted together");
        }
        class = Some(next_class);
        result.entry(key).or_insert(value);
    }
    Ok(result)
}

fn scalar_set_merge(
    current: &Value,
    incoming: &Value,
    strategy: &str,
) -> Result<Value, EngineError> {
    let current = scalar_map(current)?;
    let incoming = scalar_map(incoming)?;
    let current_class = current.keys().next().map(scalar_class);
    let incoming_class = incoming.keys().next().map(scalar_class);
    if current_class.is_some() && incoming_class.is_some() && current_class != incoming_class {
        return invalid("policy set values cannot be sorted together");
    }
    let values = if strategy == "union" {
        let mut merged = current;
        for (key, value) in incoming {
            merged.entry(key).or_insert(value);
        }
        merged.into_values().collect()
    } else {
        current
            .into_iter()
            .filter_map(|(key, value)| incoming.contains_key(&key).then_some(value))
            .collect()
    };
    Ok(Value::Array(values))
}

fn platforms_from_task(task: &str) -> Vec<String> {
    let lowered = task.to_lowercase();
    PLATFORM_TERMS
        .iter()
        .filter_map(|(platform, terms)| {
            contains_any(&lowered, terms).then_some((*platform).to_owned())
        })
        .collect()
}

fn contains_any(value: &str, terms: &[&str]) -> bool {
    terms.iter().any(|term| value.contains(term))
}

fn module_platforms(value: &Value, label: &str) -> Result<Vec<String>, EngineError> {
    let modules = value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an array")))?;
    let mut platforms = BTreeSet::new();
    for module in modules {
        let module = object(module, "project profile module")?;
        let platform = module
            .get("platform")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EngineError::Invalid("project profile module platform is invalid".to_owned())
            })?;
        platforms.insert(platform.to_owned());
    }
    Ok(platforms.into_iter().collect())
}

fn unique_target_module_count(value: Option<&Value>) -> Result<usize, EngineError> {
    let Some(value) = value else {
        return Ok(0);
    };
    let modules = value.as_array().ok_or_else(|| {
        EngineError::Invalid("project profile target_modules must be an array".to_owned())
    })?;
    let mut identities = BTreeSet::new();
    for module in modules {
        let module = object(module, "project profile target module")?;
        identities.insert(canonical_sha256(&json!([
            module.get("path").cloned().unwrap_or(Value::Null),
            module.get("platform").cloned().unwrap_or(Value::Null),
        ]))?);
    }
    Ok(identities.len())
}

fn validate_resolved_policy(value: &Value) -> Result<(), EngineError> {
    let value = object(value, "resolved-policy")?;
    let fingerprint = value
        .get("fingerprint")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| EngineError::Invalid("resolved-policy fingerprint is invalid".to_owned()))?;
    let constraints_value = value.get("constraints").ok_or_else(|| {
        EngineError::Invalid("resolved-policy constraints are missing".to_owned())
    })?;
    let constraints = constraints_value.as_object().ok_or_else(|| {
        EngineError::Invalid("resolved-policy constraints are invalid".to_owned())
    })?;
    if constraints.len() > MAX_POLICY_FIELDS {
        return invalid(format!(
            "resolved-policy constraints exceed maximum of {MAX_POLICY_FIELDS} fields"
        ));
    }
    policy_value_items(constraints_value)?;
    let selected = value
        .get("selected_platforms")
        .ok_or_else(|| {
            EngineError::Invalid("resolved-policy selected_platforms are missing".to_owned())
        })
        .and_then(|value| string_array(value, "selected_platforms"))?;
    if selected.len() > MAX_POLICY_ITEMS {
        return invalid(format!(
            "resolved-policy selected platforms exceed maximum of {MAX_POLICY_ITEMS} items"
        ));
    }
    if selected.iter().collect::<BTreeSet<_>>().len() != selected.len() {
        return invalid("resolved-policy selected platforms must be unique");
    }
    let task = value
        .get("task")
        .ok_or_else(|| EngineError::Invalid("resolved-policy task is missing".to_owned()))
        .and_then(|value| object(value, "resolved-policy task"))?;
    for field in ["text", "type", "risk"] {
        if task.get(field).and_then(Value::as_str).is_none() {
            return invalid(format!("resolved-policy task {field} is invalid"));
        }
    }
    let disciplines = task
        .get("disciplines")
        .ok_or_else(|| {
            EngineError::Invalid("resolved-policy task disciplines are missing".to_owned())
        })
        .and_then(|value| string_array(value, "task disciplines"))?;
    if disciplines.len() > MAX_POLICY_ITEMS {
        return invalid(format!(
            "resolved-policy disciplines exceed maximum of {MAX_POLICY_ITEMS} items"
        ));
    }
    let decisions = value
        .get("decisions")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            EngineError::Invalid("resolved-policy decisions must be an array".to_owned())
        })?;
    if decisions.len() > MAX_POLICY_FIELDS {
        return invalid(format!(
            "resolved-policy decisions exceed maximum of {MAX_POLICY_FIELDS} items"
        ));
    }
    for decision in decisions {
        let decision = object(decision, "policy decision")?;
        for field in ["decision", "reason_code", "source"] {
            if decision.get(field).and_then(Value::as_str).is_none() {
                return invalid(format!("policy decision {field} is invalid"));
            }
        }
        let confidence = decision
            .get("confidence")
            .and_then(Value::as_f64)
            .ok_or_else(|| EngineError::Invalid("decision confidence is invalid".to_owned()))?;
        if !(0.0..=1.0).contains(&confidence) {
            return invalid("decision confidence is invalid");
        }
        let strategy = decision
            .get("merge_strategy")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("decision merge strategy is invalid".to_owned()))?;
        if !is_merge_strategy(strategy) {
            return invalid("decision merge strategy is invalid");
        }
        string_array(
            decision.get("overridden_candidates").ok_or_else(|| {
                EngineError::Invalid("decision overridden_candidates are missing".to_owned())
            })?,
            "decision overridden_candidates",
        )?;
    }
    let mut content = value.clone();
    content.remove("fingerprint");
    if canonical_sha256(&Value::Object(content))? != fingerprint {
        return invalid("resolved-policy fingerprint mismatch");
    }
    Ok(())
}

fn sorted_unique_strings<'a>(values: impl Iterator<Item = &'a str>) -> Vec<String> {
    values
        .map(str::to_owned)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn string_array(value: &Value, label: &str) -> Result<Vec<String>, EngineError> {
    value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an array")))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid(format!("{label} must contain strings")))
        })
        .collect()
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, EngineError> {
    value
        .as_object()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an object")))
}

fn optional_object<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<&'a Map<String, Value>, EngineError> {
    object.get(field).map_or_else(
        || Ok(empty_object()),
        |value| {
            value
                .as_object()
                .ok_or_else(|| EngineError::Invalid(format!("{field} must be an object")))
        },
    )
}

fn empty_object() -> &'static Map<String, Value> {
    static EMPTY: std::sync::OnceLock<Map<String, Value>> = std::sync::OnceLock::new();
    EMPTY.get_or_init(Map::new)
}

fn invalid<T>(message: impl Into<String>) -> Result<T, EngineError> {
    Err(EngineError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    #[cfg(windows)]
    use super::user_visible_path;
    use super::{
        classify_task, merge_policy_layers, normalize_candidate, resolve_policy, wildcard_match,
    };
    use serde_json::json;
    #[cfg(windows)]
    use std::path::PathBuf;

    #[test]
    fn task_classification_preserves_precedence_and_disciplines() {
        assert_eq!(
            classify_task("实现 Figma 页面并补充 QA 测试"),
            json!({
                "disciplines": ["design", "development", "qa"],
                "risk": "medium",
                "type": "code-medium",
            })
        );
        assert_eq!(
            classify_task("iOS 单文件 contract 小改动")["type"],
            "code-risky"
        );
        assert_eq!(classify_task("只做 QA 回归")["type"], "qa-only");
    }

    #[test]
    fn policy_merge_and_resolution_are_deterministic() {
        let layers = vec![
            json!({
                "source": "core",
                "strategies": {"network": "deny-wins", "tags": "union"},
                "values": {"network": true, "tags": ["core"]},
            }),
            json!({
                "source": "project",
                "strategies": {"network": "deny-wins", "tags": "union"},
                "values": {"network": false, "tags": ["project"]},
            }),
        ];
        let (merged, decisions) = merge_policy_layers(&layers).unwrap();
        assert_eq!(
            merged,
            json!({"network": false, "tags": ["core", "project"]})
        );
        assert_eq!(decisions.len(), 4);
        assert!(
            merge_policy_layers(&[
                json!({
                    "source": "user",
                    "strategies": {"device": "locked"},
                    "values": {"device": "real"},
                }),
                json!({
                    "source": "project",
                    "values": {"device": "simulator"},
                }),
            ])
            .unwrap_err()
            .to_string()
            .contains("locked policy field")
        );

        let profile = json!({"platforms": ["web"]});
        let policy = resolve_policy(&profile, "修复 iOS 页面", &[], None, &[]).unwrap();
        assert_eq!(policy["selected_platforms"], json!(["apple"]));
        assert_eq!(policy["task"]["type"], "code-medium");
    }

    #[test]
    fn policy_merge_rejects_unknown_strategy_and_supports_numeric_sets() {
        let unknown = vec![json!({
            "source": "project",
            "strategies": {"tags": "bogus"},
            "values": {"tags": ["project"]},
        })];
        assert!(
            merge_policy_layers(&unknown)
                .unwrap_err()
                .to_string()
                .contains("unknown merge strategy")
        );
        let numeric = vec![
            json!({
                "strategies": {"values": "union"},
                "values": {"values": [2, 1]},
            }),
            json!({
                "strategies": {"values": "union"},
                "values": {"values": [3, 2]},
            }),
        ];
        assert_eq!(
            merge_policy_layers(&numeric).unwrap().0,
            json!({"values": [1, 2, 3]})
        );
    }

    #[test]
    fn glob_character_classes_and_target_path_boundary_are_explicit() {
        assert!(wildcard_match("App1.xcodeproj", "App[0-9].xcodeproj"));
        assert!(wildcard_match("Appx.xcodeproj", "App[!0-9].xcodeproj"));
        assert!(!wildcard_match("App1.xcodeproj", "App[!0-9].xcodeproj"));
        assert_eq!(
            normalize_candidate("./apps/ios/Foo.swift").unwrap(),
            "apps/ios/Foo.swift"
        );
        assert!(normalize_candidate("../../apps/ios/Foo.swift").is_err());
        assert!(normalize_candidate("/apps/ios/Foo.swift").is_err());
        assert!(normalize_candidate(r"C:\apps\ios\Foo.swift").is_err());
    }

    #[cfg(windows)]
    #[test]
    fn windows_verbatim_paths_match_user_visible_python_paths() {
        assert_eq!(
            user_visible_path(&PathBuf::from(r"\\?\C:\agent\repo")),
            PathBuf::from(r"C:\agent\repo")
        );
        assert_eq!(
            user_visible_path(&PathBuf::from(r"\\?\UNC\server\share\agent")),
            PathBuf::from(r"\\server\share\agent")
        );
    }
}
