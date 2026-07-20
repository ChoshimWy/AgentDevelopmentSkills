use super::{
    EXTERNAL_ACTIVATION_LOCK, LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    codex_config::render_codex_config, configure_nofollow, external_stage, load_json_file,
    managed_swap::rename_no_replace, open_child_directory, same_content_state_cap, same_object_cap,
    validate_activation_lock_contract,
};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256, parse_json};
use agent_engine::validate_install_plan;
use cap_fs_ext::{FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, Metadata, OpenOptions};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::io::{Read as _, Write as _};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

pub(super) const ACTIVATION_HANDLER_ID: &str = "core.source-activation.apple-codex-v1";
pub(super) const DEACTIVATION_HANDLER_ID: &str = "core.source-deactivation.apple-codex-v1";
pub(super) const PRESERVE_HANDLER_ID: &str = "core.source-preserve.apple-codex-v1";

static CONFIG_TEMPORARY_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug)]
struct ActivationAsset {
    destination: &'static str,
    mode: u32,
    package: &'static str,
    source: &'static str,
}

impl ActivationAsset {
    const fn new(
        package: &'static str,
        source: &'static str,
        destination: &'static str,
        mode: u32,
    ) -> Self {
        Self {
            destination,
            mode,
            package,
            source,
        }
    }
}

const ACTIVATION_ASSETS: &[ActivationAsset] = &[
    ActivationAsset::new(
        "design",
        "assets/codex/agents/design_researcher.toml",
        "agents/design_researcher.toml",
        0o644,
    ),
    ActivationAsset::new(
        "review",
        "assets/codex/agents/reviewer.toml",
        "agents/reviewer.toml",
        0o644,
    ),
    ActivationAsset::new(
        "workflow",
        "assets/codex/agents/explorer.toml",
        "agents/explorer.toml",
        0o644,
    ),
    ActivationAsset::new(
        "workflow",
        "assets/codex/agents/pm.toml",
        "agents/pm.toml",
        0o644,
    ),
    ActivationAsset::new(
        "workflow",
        "assets/codex/agents/reporter.toml",
        "agents/reporter.toml",
        0o644,
    ),
    ActivationAsset::new(
        "apple",
        "config/codex/templates/agents/builder.toml",
        "agents/builder.toml",
        0o644,
    ),
    ActivationAsset::new(
        "apple",
        "config/codex/templates/agents/docs_researcher.toml",
        "agents/docs_researcher.toml",
        0o644,
    ),
    ActivationAsset::new(
        "apple",
        "config/codex/templates/agents/tester.toml",
        "agents/tester.toml",
        0o644,
    ),
    ActivationAsset::new(
        "apple",
        "config/codex/templates/codex_verify.example.sh",
        "bin/codex_verify",
        0o755,
    ),
    ActivationAsset::new(
        "apple",
        "tools/digest-xcodebuild-log.sh",
        "bin/digest-xcodebuild-log",
        0o755,
    ),
    ActivationAsset::new(
        "apple",
        "config/codex/templates/codex_verify.example.sh",
        "templates/codex_verify.example.sh",
        0o755,
    ),
    ActivationAsset::new(
        "apple",
        "config/codex/templates/ui-smoke.example.yml",
        "templates/ui-smoke.example.yml",
        0o644,
    ),
];

pub(super) const PROFILE_NAMES: &[&str] = &[
    "budget.config.toml",
    "daily.config.toml",
    "deep.config.toml",
    "extreme.config.toml",
    "interactive-fast.config.toml",
    "readonly.config.toml",
];
const SHARED_CONFIG_SOURCE: &str = "assets/codex/codex.shared.toml";
const CLI_DESTINATION: &str = "bin/agent-skills";
const SESSION_DESTINATION: &str = "bin/agent-session";

#[derive(Debug)]
pub(super) struct SourceActivation {
    activation_lock: Option<FileSnapshot>,
    candidate_lock: Vec<u8>,
    candidates: Vec<ActivationCandidate>,
    config: ActivationCandidate,
    created_profile_paths: Vec<String>,
    migration: Option<Value>,
    profiles: Vec<ActivationCandidate>,
    retired: Vec<ActivationRecord>,
    scope: Vec<String>,
    source_assets: Vec<SourceAssetSnapshot>,
}

#[derive(Debug)]
struct ActivationCandidate {
    bytes: Vec<u8>,
    mode: u32,
    path: String,
    preimage: Option<FileSnapshot>,
    preimage_must_match_candidate: bool,
}

#[derive(Debug)]
struct SourceAssetSnapshot {
    package: String,
    snapshot: FileSnapshot,
    source: String,
}

#[derive(Debug)]
pub(super) struct SourceDeactivation {
    activation_lock: Vec<u8>,
    config: ConfigDeactivation,
    records: Vec<ActivationRecord>,
    reported_paths: Vec<String>,
    scope: Vec<String>,
}

#[derive(Debug)]
struct ActivationRecord {
    mode: u32,
    path: String,
    sha256: String,
}

#[derive(Debug)]
enum ConfigDeactivation {
    Missing,
    Preserved(FileSnapshot),
    Replace {
        candidate: Vec<u8>,
        original: FileSnapshot,
    },
}

#[derive(Clone, Debug)]
struct FileSnapshot {
    bytes: Vec<u8>,
    identity: Metadata,
    mode: u32,
}

impl SourceActivation {
    pub(super) fn prepare(
        target: &Dir,
        target_path: &Path,
        session_launcher: &[u8],
    ) -> Result<Self, LifecycleError> {
        require_bounded_session_launcher(session_launcher)?;
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        let activation_lock = read_required_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            "source activation Lock",
        )?;
        let lock = parse_json(&activation_lock.bytes)?;
        let (version, values) = validate_activation_lock_contract(&lock)?;
        let current = load_current_activation_preimages(target, values)?;
        let migration = if version == "1.0" {
            Some(activation_migration_report(&lock)?)
        } else {
            None
        };
        Self::prepare_from(
            target,
            target,
            target_path,
            session_launcher,
            Some(activation_lock),
            current,
            migration,
            false,
        )
    }

    pub(super) fn prepare_fresh(
        source: &Dir,
        destination: &Dir,
        target_path: &Path,
        session_launcher: &[u8],
    ) -> Result<Self, LifecycleError> {
        require_bounded_session_launcher(session_launcher)?;
        let managed = open_child_directory(
            source,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "staged managed metadata directory",
        )?;
        require_activation_lock_absent(&managed)?;
        Self::prepare_from(
            source,
            destination,
            target_path,
            session_launcher,
            None,
            BTreeMap::new(),
            None,
            false,
        )
    }

    #[cfg(not(windows))]
    pub(super) fn prepare_legacy_adoption(
        source: &Dir,
        destination: &Dir,
        target_path: &Path,
        session_launcher: &[u8],
    ) -> Result<Self, LifecycleError> {
        require_bounded_session_launcher(session_launcher)?;
        let managed = open_child_directory(
            source,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "staged managed metadata directory",
        )?;
        require_activation_lock_absent(&managed)?;
        Self::prepare_from(
            source,
            destination,
            target_path,
            session_launcher,
            None,
            BTreeMap::new(),
            None,
            true,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn prepare_from(
        source: &Dir,
        destination: &Dir,
        target_path: &Path,
        session_launcher: &[u8],
        activation_lock: Option<FileSnapshot>,
        current: BTreeMap<String, (ActivationRecord, FileSnapshot)>,
        migration: Option<Value>,
        allow_unmanaged_overwrite: bool,
    ) -> Result<Self, LifecycleError> {
        let mut source_assets = Vec::new();
        let candidate_values =
            load_activation_candidate_values(source, session_launcher, &mut source_assets)?;
        let (mut candidates, mut retired) = reconcile_activation_candidates(
            destination,
            candidate_values,
            current,
            allow_unmanaged_overwrite,
        )?;
        let (mut profiles, mut created_profile_paths) =
            prepare_activation_profiles(source, destination, &mut source_assets)?;
        let config =
            prepare_activation_config(source, destination, target_path, &mut source_assets)?;
        candidates.sort_by(|left, right| left.path.cmp(&right.path));
        profiles.sort_by(|left, right| left.path.cmp(&right.path));
        retired.sort_by(|left, right| left.path.cmp(&right.path));
        created_profile_paths.sort();
        let candidate_lock = encode_activation_lock(&candidates)?;
        let migration = match (migration, activation_lock.as_ref()) {
            (Some(migration), _) => Some(migration),
            (None, Some(current)) if current.bytes != candidate_lock => Some(
                activation_state_migration_report(&current.bytes, &candidate_lock)?,
            ),
            (None, _) => None,
        };
        let scope = activation_scope(&candidates, &profiles, &retired, &config);
        let prepared = Self {
            activation_lock,
            candidate_lock,
            candidates,
            config,
            created_profile_paths,
            migration,
            profiles,
            retired,
            scope,
            source_assets,
        };
        prepared.revalidate_from(source, destination)?;
        Ok(prepared)
    }

    pub(super) fn scope(&self) -> &[String] {
        &self.scope
    }

    pub(super) fn preview(&self) -> Value {
        let (updated_files, unchanged_files) = self.candidate_changes();
        json!({
            "config_changed": self.config_changed(),
            "created_profiles": self.created_profile_paths,
            "handler": ACTIVATION_HANDLER_ID,
            "migration": self.migration,
            "retired_files": self.retired.iter()
                .map(|record| record.path.clone())
                .collect::<Vec<_>>(),
            "unchanged_files": unchanged_files,
            "updated_files": updated_files,
        })
    }

    fn candidate_changes(&self) -> (Vec<String>, Vec<String>) {
        let mut updated_files = Vec::new();
        let mut unchanged_files = Vec::new();
        for candidate in &self.candidates {
            let is_unchanged = candidate.preimage.as_ref().is_some_and(|preimage| {
                preimage.bytes == candidate.bytes && preimage.mode == candidate.mode
            });
            if is_unchanged {
                unchanged_files.push(candidate.path.clone());
            } else {
                updated_files.push(candidate.path.clone());
            }
        }
        (updated_files, unchanged_files)
    }

    fn config_changed(&self) -> bool {
        self.config.preimage.as_ref().is_none_or(|preimage| {
            preimage.bytes != self.config.bytes || preimage.mode != self.config.mode
        })
    }

    pub(super) fn revalidate(&self, target: &Dir) -> Result<(), LifecycleError> {
        self.revalidate_from(target, target)
    }

    pub(super) fn revalidate_from(
        &self,
        source: &Dir,
        destination: &Dir,
    ) -> Result<(), LifecycleError> {
        for asset in &self.source_assets {
            let current = read_installed_asset(source, &asset.package, &asset.source)?;
            verify_snapshot_equality(
                &current,
                &asset.snapshot,
                "installed source activation asset",
            )?;
        }
        for candidate in &self.candidates {
            verify_candidate_preimage(destination, candidate)?;
        }
        for profile in &self.profiles {
            verify_candidate_preimage(destination, profile)?;
        }
        for record in &self.retired {
            verify_activation_file(destination, &record.path, record.mode, &record.sha256)?;
        }
        verify_candidate_preimage(destination, &self.config)?;
        let managed = open_child_directory(
            source,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        if let Some(expected) = self.activation_lock.as_ref() {
            let current = read_required_file(
                &managed,
                EXTERNAL_ACTIVATION_LOCK,
                Some(MANAGED_FILE_MODE),
                "source activation Lock",
            )?;
            verify_snapshot_equality(&current, expected, "source activation Lock")
        } else {
            require_activation_lock_absent(&managed)
        }
    }

    pub(super) fn apply_with_hook(
        self,
        target: &Dir,
        target_path: &Path,
        scratch: &Dir,
        scratch_path: &Path,
        mut handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        self.revalidate(target)?;
        let retired_files = self
            .retired
            .iter()
            .map(|record| record.path.clone())
            .collect::<Vec<_>>();
        for record in &self.retired {
            quarantine_activation_record(target, target_path, scratch, scratch_path, record)?;
            handler_hook(&record.path, "retired-file-removed")?;
        }

        let (updated_files, unchanged_files) = self.candidate_changes();
        for candidate in &self.candidates {
            let unchanged = candidate.preimage.as_ref().is_some_and(|preimage| {
                preimage.bytes == candidate.bytes && preimage.mode == candidate.mode
            });
            if !unchanged {
                publish_activation_candidate(
                    target,
                    target_path,
                    scratch,
                    scratch_path,
                    candidate,
                )?;
                handler_hook(&candidate.path, "managed-file-published")?;
            }
        }

        for profile in &self.profiles {
            if profile.preimage.is_none() {
                publish_activation_candidate(target, target_path, scratch, scratch_path, profile)?;
                handler_hook(&profile.path, "profile-created")?;
            }
        }

        let config_changed = self.config_changed();
        if config_changed {
            publish_activation_candidate(target, target_path, scratch, scratch_path, &self.config)?;
            handler_hook(&self.config.path, "config-published")?;
        }

        let lock_candidate = ActivationCandidate {
            bytes: self.candidate_lock.clone(),
            mode: MANAGED_FILE_MODE,
            path: format!(".agent-skills/{EXTERNAL_ACTIVATION_LOCK}"),
            preimage: self.activation_lock.clone(),
            preimage_must_match_candidate: false,
        };
        publish_activation_candidate(target, target_path, scratch, scratch_path, &lock_candidate)?;
        handler_hook(EXTERNAL_ACTIVATION_LOCK, "activation-lock-published")?;
        verify_activation_output(target, &self)?;
        Ok(json!({
            "config_changed": config_changed,
            "created_profiles": self.created_profile_paths,
            "handler": ACTIVATION_HANDLER_ID,
            "migration": self.migration,
            "retired_files": retired_files,
            "unchanged_files": unchanged_files,
            "updated_files": updated_files,
        }))
    }
}

impl SourceDeactivation {
    pub(super) fn prepare_for_uninstall(
        target: &Dir,
        target_path: &Path,
    ) -> Result<Option<Self>, LifecycleError> {
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        let install_lock = load_json_file(
            &managed,
            "install-lock.json",
            MANAGED_FILE_MODE,
            "source Install Lock",
        )?;
        validate_install_plan(&install_lock)?;
        if install_lock.get("status").and_then(Value::as_str) != Some("installed") {
            return invalid("source uninstall requires an installed Install Lock");
        }
        let activation_owned = install_lock
            .get("selected_runtime_configs")
            .and_then(Value::as_array)
            .is_some_and(|values| values.iter().any(|value| value.as_str() == Some("codex")))
            || install_lock
                .get("selected_packages")
                .and_then(Value::as_array)
                .is_some_and(|values| {
                    values
                        .iter()
                        .any(|value| value.get("id").and_then(Value::as_str) == Some("codex"))
                });
        let activation_present = match managed.symlink_metadata(EXTERNAL_ACTIVATION_LOCK) {
            Ok(_) => true,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
            Err(error) => return Err(error.into()),
        };
        match (activation_owned, activation_present) {
            (true, false) => {
                return invalid("managed source install is missing its activation Lock");
            }
            (false, true) => {
                return invalid("non-activated install must not contain an activation Lock");
            }
            (false, false) => return Ok(None),
            (true, true) => {}
        }
        let prepared = Self::prepare(target, target_path)?;
        prepared.require_supported_uninstall_paths()?;
        Ok(Some(prepared))
    }

    pub(super) fn prepare(target: &Dir, target_path: &Path) -> Result<Self, LifecycleError> {
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        let activation_lock = read_required_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            "source activation Lock",
        )?
        .bytes;
        if activation_lock.len() > MAX_CONTRACT_JSON_BYTES {
            return Err(agent_contracts::ContractError::InputTooLarge {
                maximum: MAX_CONTRACT_JSON_BYTES,
            }
            .into());
        }
        let lock = parse_json(&activation_lock)?;
        let (_, values) = validate_activation_lock_contract(&lock)?;
        let mut records = Vec::with_capacity(values.len());
        let mut reported_paths = Vec::with_capacity(values.len());
        for value in values {
            let path = value
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_error("source activation Lock path is invalid"))?
                .to_owned();
            let mode = value
                .get("mode")
                .and_then(Value::as_u64)
                .and_then(|mode| u32::try_from(mode).ok())
                .ok_or_else(|| invalid_error("source activation Lock mode is invalid"))?;
            let sha256 = value
                .get("sha256")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_error("source activation Lock hash is invalid"))?
                .to_owned();
            verify_activation_file(target, &path, mode, &sha256)?;
            reported_paths.push(path.clone());
            records.push(ActivationRecord { mode, path, sha256 });
        }
        records.sort_by(|left, right| left.path.cmp(&right.path));
        let config = prepare_config_deactivation(target, target_path)?;
        let mut scope = records
            .iter()
            .map(|record| record.path.clone())
            .collect::<Vec<_>>();
        scope.push("config.toml".to_owned());
        scope.sort();
        scope.dedup();
        Ok(Self {
            activation_lock,
            config,
            records,
            reported_paths,
            scope,
        })
    }

    pub(super) fn scope(&self) -> &[String] {
        &self.scope
    }

    pub(super) fn uninstall_report_fields(&self) -> (&[String], &'static str) {
        let config_action = match &self.config {
            ConfigDeactivation::Missing => "missing",
            ConfigDeactivation::Preserved(_) => "preserved",
            ConfigDeactivation::Replace { .. } => "removed-managed-instructions-path",
        };
        (&self.reported_paths, config_action)
    }

    pub(super) fn apply_with_hook(
        self,
        target: &Dir,
        scratch: &Dir,
        mut handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        self.revalidate(target)?;
        let result = self.apply_external_mutation(target, scratch, &mut handler_hook)?;
        self.verify_uninstall_output(target)?;
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        let current = read_required_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            "source activation Lock",
        )?;
        if current.bytes != self.activation_lock {
            return invalid("source activation Lock changed during deactivation");
        }
        managed.remove_file(EXTERNAL_ACTIVATION_LOCK)?;
        Ok(result)
    }

    pub(super) fn apply_after_managed_backup(
        &self,
        target: &Dir,
        backup: &Dir,
        scratch: &Dir,
        mut handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        self.revalidate_external(target)?;
        let managed = open_child_directory(
            backup,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "uninstall recovery metadata",
        )?;
        self.verify_activation_lock_in(&managed, "uninstall recovery Activation Lock")?;
        let result = self.apply_external_mutation(target, scratch, &mut handler_hook)?;
        self.verify_uninstall_output(target)?;
        self.verify_activation_lock_in(&managed, "uninstall recovery Activation Lock")?;
        Ok(result)
    }

    pub(super) fn revalidate(&self, target: &Dir) -> Result<(), LifecycleError> {
        self.revalidate_external(target)?;
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        self.verify_activation_lock_in(&managed, "source activation Lock")
    }

    fn revalidate_external(&self, target: &Dir) -> Result<(), LifecycleError> {
        for record in &self.records {
            verify_activation_file(target, &record.path, record.mode, &record.sha256)?;
        }
        match &self.config {
            ConfigDeactivation::Missing => {
                if target.symlink_metadata("config.toml").is_ok() {
                    return invalid("config.toml changed before source deactivation");
                }
            }
            ConfigDeactivation::Preserved(snapshot)
            | ConfigDeactivation::Replace {
                original: snapshot, ..
            } => verify_file_snapshot(target, "config.toml", snapshot, "config.toml")?,
        }
        Ok(())
    }

    fn verify_activation_lock_in(&self, managed: &Dir, label: &str) -> Result<(), LifecycleError> {
        if read_required_file(
            managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            label,
        )?
        .bytes
            != self.activation_lock
        {
            return invalid("source activation Lock changed before deactivation");
        }
        Ok(())
    }

    fn require_supported_uninstall_paths(&self) -> Result<(), LifecycleError> {
        let actual = self
            .records
            .iter()
            .map(|record| record.path.as_str())
            .collect::<BTreeSet<_>>();
        let baseline = ACTIVATION_ASSETS
            .iter()
            .map(|asset| asset.destination)
            .collect::<BTreeSet<_>>();
        let mut session_enabled = baseline.clone();
        session_enabled.insert(SESSION_DESTINATION);
        let mut native_cli_enabled = session_enabled.clone();
        native_cli_enabled.insert(CLI_DESTINATION);
        if actual != baseline && actual != session_enabled && actual != native_cli_enabled {
            return invalid("activation Lock does not cover the supported managed file set");
        }
        Ok(())
    }

    fn apply_external_mutation(
        &self,
        target: &Dir,
        scratch: &Dir,
        handler_hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        for record in &self.records {
            remove_activation_file(target, record)?;
            handler_hook(&record.path, "owned-file-removed")?;
        }
        let config_action = match &self.config {
            ConfigDeactivation::Missing => "missing",
            ConfigDeactivation::Preserved(_) => "preserved",
            ConfigDeactivation::Replace {
                candidate,
                original,
            } => {
                replace_config(target, scratch, original, candidate, handler_hook)?;
                "removed-managed-instructions-path"
            }
        };
        Ok(json!({
            "config_action": config_action,
            "handler": DEACTIVATION_HANDLER_ID,
            "removed_files": self.reported_paths,
        }))
    }

    pub(super) fn verify_uninstall_output(&self, target: &Dir) -> Result<(), LifecycleError> {
        for record in &self.records {
            if read_optional_relative_file(target, &record.path, None, "removed activation file")?
                .is_some()
            {
                return invalid(format!(
                    "activated file remains after uninstall: {}",
                    record.path
                ));
            }
        }
        match &self.config {
            ConfigDeactivation::Missing => match target.symlink_metadata("config.toml") {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
                Ok(_) => return invalid("config.toml appeared during uninstall"),
            },
            ConfigDeactivation::Preserved(snapshot) => {
                verify_file_snapshot(target, "config.toml", snapshot, "preserved config.toml")?;
            }
            ConfigDeactivation::Replace {
                candidate,
                original,
            } => {
                let current = read_required_file(
                    target,
                    "config.toml",
                    Some(original.mode),
                    "uninstall config.toml",
                )?;
                if current.bytes != *candidate {
                    return invalid("config.toml differs from the uninstall candidate");
                }
            }
        }
        Ok(())
    }
}

fn require_bounded_session_launcher(session_launcher: &[u8]) -> Result<(), LifecycleError> {
    if session_launcher.len() > MAX_CONTRACT_JSON_BYTES {
        return Err(agent_contracts::ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    Ok(())
}

fn load_current_activation_preimages(
    target: &Dir,
    values: &[Value],
) -> Result<BTreeMap<String, (ActivationRecord, FileSnapshot)>, LifecycleError> {
    let mut current = BTreeMap::new();
    for value in values {
        let record = activation_record(value)?;
        let snapshot = read_required_relative_file(
            target,
            &record.path,
            Some(record.mode),
            "managed activation preimage",
        )?;
        if bytes_sha256(&snapshot.bytes) != record.sha256 {
            return invalid(format!(
                "managed activation preimage differs from Lock: {}",
                record.path
            ));
        }
        current.insert(record.path.clone(), (record, snapshot));
    }
    Ok(current)
}

fn load_activation_candidate_values(
    target: &Dir,
    session_launcher: &[u8],
    source_assets: &mut Vec<SourceAssetSnapshot>,
) -> Result<BTreeMap<String, (Vec<u8>, u32)>, LifecycleError> {
    let mut values = BTreeMap::new();
    for asset in ACTIVATION_ASSETS {
        let snapshot = read_installed_asset(target, asset.package, asset.source)?;
        source_assets.push(SourceAssetSnapshot {
            package: asset.package.to_owned(),
            snapshot: snapshot.clone(),
            source: asset.source.to_owned(),
        });
        if values
            .insert(asset.destination.to_owned(), (snapshot.bytes, asset.mode))
            .is_some()
        {
            return invalid("native source activation asset destinations must be unique");
        }
    }
    for destination in [CLI_DESTINATION, SESSION_DESTINATION] {
        if values
            .insert(destination.to_owned(), (session_launcher.to_vec(), 0o755))
            .is_some()
        {
            return invalid("native source activation asset destinations must be unique");
        }
    }
    Ok(values)
}

fn reconcile_activation_candidates(
    target: &Dir,
    values: BTreeMap<String, (Vec<u8>, u32)>,
    mut current: BTreeMap<String, (ActivationRecord, FileSnapshot)>,
    allow_unmanaged_overwrite: bool,
) -> Result<(Vec<ActivationCandidate>, Vec<ActivationRecord>), LifecycleError> {
    let mut candidates = Vec::with_capacity(values.len());
    for (path, (bytes, mode)) in values {
        let (preimage, preimage_must_match_candidate) =
            if let Some((_, snapshot)) = current.remove(&path) {
                (Some(snapshot), false)
            } else {
                let required_mode = (!allow_unmanaged_overwrite).then_some(mode);
                let snapshot =
                    read_optional_relative_file(target, &path, required_mode, "activation file")?;
                if !allow_unmanaged_overwrite
                    && let Some(snapshot) = snapshot.as_ref()
                    && snapshot.bytes != bytes
                {
                    return invalid(format!(
                        "refusing to overwrite unmanaged activation destination: {path}"
                    ));
                }
                let adopted = snapshot.is_some() && !allow_unmanaged_overwrite;
                (snapshot, adopted)
            };
        candidates.push(ActivationCandidate {
            bytes,
            mode,
            path,
            preimage,
            preimage_must_match_candidate,
        });
    }
    let retired = current.into_values().map(|(record, _)| record).collect();
    Ok((candidates, retired))
}

fn prepare_activation_profiles(
    source_root: &Dir,
    destination: &Dir,
    source_assets: &mut Vec<SourceAssetSnapshot>,
) -> Result<(Vec<ActivationCandidate>, Vec<String>), LifecycleError> {
    let mut profiles = Vec::with_capacity(PROFILE_NAMES.len());
    let mut created = Vec::new();
    for name in PROFILE_NAMES {
        let source = format!("assets/codex/profiles/{name}");
        let snapshot = read_installed_asset(source_root, "codex", &source)?;
        source_assets.push(SourceAssetSnapshot {
            package: "codex".to_owned(),
            snapshot: snapshot.clone(),
            source,
        });
        let preimage = read_optional_relative_file(destination, name, None, "Codex profile")?;
        if preimage.is_none() {
            created.push((*name).to_owned());
        }
        profiles.push(ActivationCandidate {
            bytes: snapshot.bytes,
            mode: MANAGED_FILE_MODE,
            path: (*name).to_owned(),
            preimage,
            preimage_must_match_candidate: false,
        });
    }
    Ok((profiles, created))
}

fn prepare_activation_config(
    source: &Dir,
    destination: &Dir,
    target_path: &Path,
    source_assets: &mut Vec<SourceAssetSnapshot>,
) -> Result<ActivationCandidate, LifecycleError> {
    let shared = read_installed_asset(source, "codex", SHARED_CONFIG_SOURCE)?;
    source_assets.push(SourceAssetSnapshot {
        package: "codex".to_owned(),
        snapshot: shared.clone(),
        source: SHARED_CONFIG_SOURCE.to_owned(),
    });
    let preimage = read_optional_file(destination, "config.toml", None, "config.toml")?;
    let agents_path = target_path
        .join("AGENTS.md")
        .to_str()
        .ok_or_else(|| invalid_error("source activation target path must be valid UTF-8"))?
        .to_owned();
    let bytes = render_codex_config(
        preimage.as_ref().map(|snapshot| snapshot.bytes.as_slice()),
        &shared.bytes,
        &agents_path,
    )?;
    Ok(ActivationCandidate {
        bytes,
        mode: preimage
            .as_ref()
            .map_or(MANAGED_FILE_MODE, |snapshot| snapshot.mode),
        path: "config.toml".to_owned(),
        preimage,
        preimage_must_match_candidate: false,
    })
}

fn encode_activation_lock(candidates: &[ActivationCandidate]) -> Result<Vec<u8>, LifecycleError> {
    Ok(canonical_json(&json!({
        "files": candidates.iter().map(|candidate| json!({
            "mode": candidate.mode,
            "path": candidate.path,
            "sha256": bytes_sha256(&candidate.bytes),
        })).collect::<Vec<_>>(),
        "handler": ACTIVATION_HANDLER_ID,
        "manager": "agent-development-skills",
        "schema_version": "2.0",
    }))?)
}

fn activation_scope(
    candidates: &[ActivationCandidate],
    profiles: &[ActivationCandidate],
    retired: &[ActivationRecord],
    config: &ActivationCandidate,
) -> Vec<String> {
    let mut scope = BTreeSet::new();
    scope.extend(candidates.iter().map(|candidate| candidate.path.clone()));
    scope.extend(profiles.iter().map(|profile| profile.path.clone()));
    scope.extend(retired.iter().map(|record| record.path.clone()));
    scope.insert(config.path.clone());
    scope.into_iter().collect()
}

fn verify_activation_output(
    target: &Dir,
    activation: &SourceActivation,
) -> Result<(), LifecycleError> {
    for candidate in &activation.candidates {
        let current = read_required_relative_file(
            target,
            &candidate.path,
            Some(candidate.mode),
            "published activation file",
        )?;
        if current.bytes != candidate.bytes {
            return invalid(format!(
                "published activation file differs: {}",
                candidate.path
            ));
        }
    }
    for profile in &activation.profiles {
        let current = read_required_relative_file(
            target,
            &profile.path,
            if profile.preimage.is_none() {
                Some(profile.mode)
            } else {
                None
            },
            "published Codex profile",
        )?;
        if let Some(preimage) = profile.preimage.as_ref() {
            verify_snapshot_equality(&current, preimage, "preserved Codex profile")?;
        } else if current.bytes != profile.bytes {
            return invalid(format!("created Codex profile differs: {}", profile.path));
        }
    }
    for record in &activation.retired {
        if read_optional_relative_file(target, &record.path, None, "retired activation file")?
            .is_some()
        {
            return invalid(format!("retired activation file remains: {}", record.path));
        }
    }
    let config = read_required_file(
        target,
        "config.toml",
        Some(activation.config.mode),
        "published config.toml",
    )?;
    if config.bytes != activation.config.bytes {
        return invalid("published config.toml differs from activation candidate");
    }
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let lock = read_required_file(
        &managed,
        EXTERNAL_ACTIVATION_LOCK,
        Some(MANAGED_FILE_MODE),
        "published source activation Lock",
    )?;
    if lock.bytes != activation.candidate_lock {
        return invalid("published source activation Lock differs from candidate");
    }
    let parsed = parse_json(&lock.bytes)?;
    validate_activation_lock_contract(&parsed)?;
    Ok(())
}

fn quarantine_activation_record(
    target: &Dir,
    target_path: &Path,
    scratch: &Dir,
    scratch_path: &Path,
    record: &ActivationRecord,
) -> Result<(), LifecycleError> {
    verify_activation_file(target, &record.path, record.mode, &record.sha256)?;
    let (parent, parent_path, name) =
        open_existing_relative_parent(target, target_path, &record.path, "activation file")?;
    let quarantine = allocate_quarantine_name();
    rename_no_replace(
        &parent,
        &parent_path,
        name,
        scratch,
        scratch_path,
        &quarantine,
    )
    .map_err(|error| {
        LifecycleError::Invalid(format!(
            "could not quarantine retired activation file {}: {error}",
            record.path
        ))
    })?;
    if parent.symlink_metadata(name).is_ok() {
        return invalid(format!(
            "retired activation file remains after quarantine: {}",
            record.path
        ));
    }
    let quarantined = read_required_file(
        scratch,
        &quarantine,
        Some(record.mode),
        "quarantined activation file",
    )?;
    if bytes_sha256(&quarantined.bytes) != record.sha256 {
        return invalid(format!(
            "quarantined activation file differs: {}",
            record.path
        ));
    }
    Ok(())
}

fn publish_activation_candidate(
    target: &Dir,
    target_path: &Path,
    scratch: &Dir,
    scratch_path: &Path,
    candidate: &ActivationCandidate,
) -> Result<(), LifecycleError> {
    verify_candidate_preimage(target, candidate)?;
    let (parent, parent_path, name) = ensure_relative_parent(
        target,
        target_path,
        &candidate.path,
        "source activation destination",
    )?;
    let (temporary, prepared) =
        prepare_activation_temporary(scratch, candidate.mode, &candidate.bytes)?;
    quarantine_candidate_preimage(
        &parent,
        &parent_path,
        name,
        scratch,
        scratch_path,
        &temporary,
        candidate,
    )?;
    verify_publication_temporary(scratch, &temporary, &prepared, candidate)?;
    publish_prepared_candidate(
        &parent,
        &parent_path,
        name,
        scratch,
        scratch_path,
        &temporary,
        &prepared,
        candidate,
    )
}

#[allow(clippy::too_many_arguments)]
fn quarantine_candidate_preimage(
    parent: &Dir,
    parent_path: &Path,
    name: &str,
    scratch: &Dir,
    scratch_path: &Path,
    temporary: &str,
    candidate: &ActivationCandidate,
) -> Result<(), LifecycleError> {
    if let Some(preimage) = candidate.preimage.as_ref() {
        let current = read_required_file(
            parent,
            name,
            Some(preimage.mode),
            "source activation destination",
        )?;
        verify_snapshot_equality(&current, preimage, "source activation destination")?;
        let quarantine = allocate_quarantine_name();
        if let Err(error) = rename_no_replace(
            parent,
            parent_path,
            name,
            scratch,
            scratch_path,
            &quarantine,
        ) {
            return cleanup_temporary_after_error(
                scratch,
                temporary,
                LifecycleError::Invalid(format!(
                    "could not quarantine source activation destination {}: {error}",
                    candidate.path
                )),
            );
        }
        let quarantined = read_required_file(
            scratch,
            &quarantine,
            Some(preimage.mode),
            "quarantined activation preimage",
        )?;
        if !same_object_cap(&quarantined.identity, &preimage.identity)
            || quarantined.identity.nlink() != 1
            || quarantined.bytes != preimage.bytes
            || quarantined.mode != preimage.mode
        {
            return invalid("quarantined activation preimage differs after rename");
        }
    } else {
        match parent.symlink_metadata(name) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return cleanup_temporary_after_error(scratch, temporary, error.into());
            }
            Ok(_) => {
                return cleanup_temporary_after_error(
                    scratch,
                    temporary,
                    invalid_error(format!(
                        "source activation destination appeared before publication: {}",
                        candidate.path
                    )),
                );
            }
        }
    }
    Ok(())
}

fn verify_publication_temporary(
    scratch: &Dir,
    temporary: &str,
    prepared: &FileSnapshot,
    candidate: &ActivationCandidate,
) -> Result<(), LifecycleError> {
    let current_temporary = read_required_file(
        scratch,
        temporary,
        Some(candidate.mode),
        "activation publication temporary file",
    )?;
    if !same_object_cap(&prepared.identity, &current_temporary.identity)
        || !same_content_state_cap(&prepared.identity, &current_temporary.identity)
        || current_temporary.identity.nlink() != 1
        || current_temporary.bytes != candidate.bytes
    {
        return cleanup_temporary_after_error(
            scratch,
            temporary,
            invalid_error("activation publication temporary changed before rename"),
        );
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn publish_prepared_candidate(
    parent: &Dir,
    parent_path: &Path,
    name: &str,
    scratch: &Dir,
    scratch_path: &Path,
    temporary: &str,
    prepared: &FileSnapshot,
    candidate: &ActivationCandidate,
) -> Result<(), LifecycleError> {
    rename_no_replace(scratch, scratch_path, temporary, parent, parent_path, name).map_err(
        |error| {
            LifecycleError::Invalid(format!(
                "could not publish source activation destination {}: {error}",
                candidate.path
            ))
        },
    )?;
    let published = read_required_file(
        parent,
        name,
        Some(candidate.mode),
        "published source activation destination",
    )?;
    if !same_object_cap(&prepared.identity, &published.identity)
        || published.identity.nlink() != 1
        || published.bytes != candidate.bytes
    {
        return invalid(format!(
            "published source activation destination differs: {}",
            candidate.path
        ));
    }
    Ok(())
}

fn prepare_activation_temporary(
    scratch: &Dir,
    mode: u32,
    bytes: &[u8],
) -> Result<(String, FileSnapshot), LifecycleError> {
    for _ in 0..128_u64 {
        let name = allocate_temporary_name();
        let mut options = OpenOptions::new();
        options
            .write(true)
            .create_new(true)
            .follow(FollowSymlinks::No);
        configure_nofollow(&mut options);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        match scratch.open_with(&name, &options) {
            Ok(mut file) => {
                let opened = file.metadata()?;
                let result = (|| {
                    if opened.nlink() != 1 || !mode_matches(file_mode(&opened), Some(0o600)) {
                        return invalid(
                            "activation publication temporary is not private or has an alias",
                        );
                    }
                    file.write_all(bytes)?;
                    #[cfg(unix)]
                    {
                        use cap_std::fs::{Permissions, PermissionsExt as _};
                        file.set_permissions(Permissions::from_mode(mode))?;
                    }
                    file.sync_all()?;
                    let completed = file.metadata()?;
                    if !same_object_cap(&opened, &completed)
                        || completed.nlink() != 1
                        || !mode_matches(file_mode(&completed), Some(mode))
                    {
                        return invalid("activation publication temporary changed while writing");
                    }
                    drop(file);
                    let reopened = read_required_file(
                        scratch,
                        &name,
                        Some(mode),
                        "activation publication temporary file",
                    )?;
                    if !same_object_cap(&completed, &reopened.identity)
                        || !same_content_state_cap(&completed, &reopened.identity)
                        || reopened.identity.nlink() != 1
                        || reopened.bytes != bytes
                    {
                        return invalid("activation publication temporary changed after writing");
                    }
                    Ok(reopened)
                })();
                return match result {
                    Ok(prepared) => Ok((name, prepared)),
                    Err(error) => cleanup_temporary_after_error(scratch, &name, error),
                };
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
    }
    invalid("could not allocate activation publication temporary file")
}

fn ensure_relative_parent<'a>(
    root: &Dir,
    root_path: &Path,
    relative: &'a str,
    label: &str,
) -> Result<(Dir, PathBuf, &'a str), LifecycleError> {
    let (parents, name) = relative_file_parts(relative, label)?;
    let mut directory = root.try_clone()?;
    let mut directory_path = root_path.to_path_buf();
    for parent in parents {
        directory = match directory.symlink_metadata(parent) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!("{label} parent is unsafe: {relative}"));
            }
            Ok(_) => open_child_directory(&directory, parent, None, label)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                external_stage::create_directory(
                    &directory,
                    OsStr::new(parent),
                    Some(MANAGED_DIRECTORY_MODE),
                    "source activation directory",
                )?
            }
            Err(error) => return Err(error.into()),
        };
        directory_path.push(parent);
    }
    Ok((directory, directory_path, name))
}

fn open_existing_relative_parent<'a>(
    root: &Dir,
    root_path: &Path,
    relative: &'a str,
    label: &str,
) -> Result<(Dir, PathBuf, &'a str), LifecycleError> {
    let (parents, name) = relative_file_parts(relative, label)?;
    let mut directory = root.try_clone()?;
    let mut directory_path = root_path.to_path_buf();
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
        directory_path.push(parent);
    }
    Ok((directory, directory_path, name))
}

fn allocate_temporary_name() -> String {
    format!(
        ".agent-source-activation-temp-{}-{}",
        std::process::id(),
        CONFIG_TEMPORARY_ID.fetch_add(1, Ordering::Relaxed)
    )
}

fn allocate_quarantine_name() -> String {
    format!(
        ".agent-source-activation-old-{}-{}",
        std::process::id(),
        CONFIG_TEMPORARY_ID.fetch_add(1, Ordering::Relaxed)
    )
}

fn activation_record(value: &Value) -> Result<ActivationRecord, LifecycleError> {
    Ok(ActivationRecord {
        mode: value
            .get("mode")
            .and_then(Value::as_u64)
            .and_then(|mode| u32::try_from(mode).ok())
            .ok_or_else(|| invalid_error("source activation Lock mode is invalid"))?,
        path: value
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_error("source activation Lock path is invalid"))?
            .to_owned(),
        sha256: value
            .get("sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_error("source activation Lock hash is invalid"))?
            .to_owned(),
    })
}

fn activation_migration_report(source: &Value) -> Result<Value, LifecycleError> {
    let mut migrated = source.clone();
    let object = migrated
        .as_object_mut()
        .ok_or_else(|| invalid_error("source activation Lock must be an object"))?;
    object.insert("schema_version".to_owned(), Value::String("2.0".to_owned()));
    object.insert(
        "handler".to_owned(),
        Value::String(ACTIVATION_HANDLER_ID.to_owned()),
    );
    validate_activation_lock_contract(&migrated)?;
    let mut report = json!({
        "after_sha256": canonical_sha256(&migrated)?,
        "artifact": "activation-lock",
        "before_sha256": canonical_sha256(source)?,
        "from_version": "1.0",
        "lossless": true,
        "schema_version": "1.0",
        "status": "applied",
        "steps": [{
            "changes": ["add:/handler", "replace:/schema_version"],
            "from_version": "1.0",
            "lossless": true,
            "to_version": "2.0",
        }],
        "to_version": "2.0",
    });
    let fingerprint = canonical_sha256(&report)?;
    report["fingerprint"] = Value::String(fingerprint);
    Ok(report)
}

fn activation_state_migration_report(before: &[u8], after: &[u8]) -> Result<Value, LifecycleError> {
    let before_sha256 = bytes_sha256(before);
    let after_sha256 = bytes_sha256(after);
    if before_sha256 == after_sha256 {
        return invalid("source activation state migration requires a changed candidate");
    }
    let mut report = json!({
        "after_sha256": after_sha256,
        "artifact": "source-activation-state",
        "before_sha256": before_sha256,
        "from_version": before_sha256,
        "lossless": true,
        "schema_version": "1.0",
        "status": "applied",
        "steps": [{
            "changes": ["replace:/records"],
            "from_version": before_sha256,
            "lossless": true,
            "to_version": after_sha256,
        }],
        "to_version": after_sha256,
    });
    let fingerprint = canonical_sha256(&report)?;
    report["fingerprint"] = Value::String(fingerprint);
    Ok(report)
}

fn read_installed_asset(
    target: &Dir,
    package: &str,
    source: &str,
) -> Result<FileSnapshot, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let packages = open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "installed packages directory",
    )?;
    let package = open_child_directory(&packages, package, None, "installed activation package")?;
    read_required_relative_file(&package, source, None, "installed source activation asset")
}

fn read_optional_relative_file(
    root: &Dir,
    relative: &str,
    mode: Option<u32>,
    label: &str,
) -> Result<Option<FileSnapshot>, LifecycleError> {
    let (parts, name) = relative_file_parts(relative, label)?;
    let mut directory = root.try_clone()?;
    for parent in parts {
        match directory.symlink_metadata(parent) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!("{label} parent is unsafe: {relative}"));
            }
            Ok(_) => {
                directory = open_child_directory(&directory, parent, None, label)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        }
    }
    read_optional_file(&directory, name, mode, label)
}

fn read_required_relative_file(
    root: &Dir,
    relative: &str,
    mode: Option<u32>,
    label: &str,
) -> Result<FileSnapshot, LifecycleError> {
    let (parent, name) = open_relative_parent(root, relative, label)?;
    read_required_file(&parent, name, mode, label)
}

fn relative_file_parts<'a>(
    relative: &'a str,
    label: &str,
) -> Result<(Vec<&'a str>, &'a str), LifecycleError> {
    let path = Path::new(relative);
    if path.is_absolute() {
        return invalid(format!("{label} must be a package-relative path"));
    }
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => parts.push(
                part.to_str()
                    .ok_or_else(|| invalid_error(format!("{label} path is invalid")))?,
            ),
            Component::CurDir => {}
            Component::ParentDir | Component::Prefix(_) | Component::RootDir => {
                return invalid(format!("{label} must be a package-relative path"));
            }
        }
    }
    let (name, parents) = parts
        .split_last()
        .ok_or_else(|| invalid_error(format!("{label} must be a package-relative path")))?;
    Ok((parents.to_vec(), name))
}

fn verify_candidate_preimage(
    target: &Dir,
    candidate: &ActivationCandidate,
) -> Result<(), LifecycleError> {
    if let Some(expected) = candidate.preimage.as_ref() {
        let current = read_required_relative_file(
            target,
            &candidate.path,
            if candidate.preimage_must_match_candidate {
                Some(candidate.mode)
            } else {
                None
            },
            "source activation destination",
        )?;
        verify_snapshot_equality(&current, expected, "source activation destination")?;
        if candidate.preimage_must_match_candidate && current.bytes != candidate.bytes {
            return invalid(format!(
                "unmanaged activation destination changed: {}",
                candidate.path
            ));
        }
    } else if read_optional_relative_file(
        target,
        &candidate.path,
        None,
        "source activation destination",
    )?
    .is_some()
    {
        return invalid(format!(
            "source activation destination appeared after preflight: {}",
            candidate.path
        ));
    }
    Ok(())
}

fn require_activation_lock_absent(managed: &Dir) -> Result<(), LifecycleError> {
    match managed.symlink_metadata(EXTERNAL_ACTIVATION_LOCK) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
        Ok(_) => invalid("fresh source activation requires an absent Activation Lock"),
    }
}

fn verify_snapshot_equality(
    current: &FileSnapshot,
    expected: &FileSnapshot,
    label: &str,
) -> Result<(), LifecycleError> {
    if !same_object_cap(&current.identity, &expected.identity)
        || !same_content_state_cap(&current.identity, &expected.identity)
        || current.bytes != expected.bytes
        || current.mode != expected.mode
    {
        return invalid(format!("{label} changed after preflight"));
    }
    Ok(())
}

fn bytes_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn prepare_config_deactivation(
    target: &Dir,
    target_path: &Path,
) -> Result<ConfigDeactivation, LifecycleError> {
    let Some(original) = read_optional_file(target, "config.toml", None, "config.toml")? else {
        return Ok(ConfigDeactivation::Missing);
    };
    let text = std::str::from_utf8(&original.bytes)
        .map_err(|_| invalid_error("config.toml must be valid UTF-8 TOML"))?;
    let parsed = text
        .parse::<toml::Value>()
        .map_err(|_| invalid_error("config.toml must be valid UTF-8 TOML"))?;
    let expected_instructions = target_path.join("AGENTS.md");
    let expected_instructions = expected_instructions
        .to_str()
        .ok_or_else(|| invalid_error("source deactivation target path must be valid UTF-8"))?;
    if parsed
        .get("model_instructions_file")
        .and_then(toml::Value::as_str)
        != Some(expected_instructions)
    {
        return Ok(ConfigDeactivation::Preserved(original));
    }
    let (candidate, matches) = remove_root_assignment(text);
    if matches != 1 {
        return invalid("managed model_instructions_file must be one root-level assignment");
    }
    let reparsed = candidate
        .parse::<toml::Value>()
        .map_err(|_| invalid_error("targeted config deactivation is not valid TOML"))?;
    let mut expected = parsed;
    let expected_table = expected
        .as_table_mut()
        .ok_or_else(|| invalid_error("config.toml root must be a TOML table"))?;
    expected_table.remove("model_instructions_file");
    if reparsed != expected {
        return invalid("targeted config deactivation changed unmanaged values");
    }
    Ok(ConfigDeactivation::Replace {
        candidate: candidate.into_bytes(),
        original,
    })
}

fn remove_root_assignment(text: &str) -> (String, usize) {
    let mut candidate = String::with_capacity(text.len());
    let mut matches = 0_usize;
    let mut root = true;
    for line in text.split_inclusive('\n') {
        let trimmed = line.trim_start_matches([' ', '\t']);
        if root && trimmed.starts_with('[') {
            root = false;
        }
        if root && !trimmed.starts_with('#') && is_model_instructions_assignment(trimmed) {
            matches += 1;
        } else {
            candidate.push_str(line);
        }
    }
    (candidate, matches)
}

fn is_model_instructions_assignment(line: &str) -> bool {
    [
        "model_instructions_file",
        "\"model_instructions_file\"",
        "'model_instructions_file'",
    ]
    .iter()
    .any(|key| {
        line.strip_prefix(key)
            .is_some_and(|suffix| suffix.trim_start_matches([' ', '\t']).starts_with('='))
    })
}

fn verify_activation_file(
    target: &Dir,
    relative: &str,
    mode: u32,
    expected_sha256: &str,
) -> Result<(), LifecycleError> {
    let (parent, name) = open_relative_parent(target, relative, "activated file")?;
    let snapshot = read_required_file(&parent, name, Some(mode), "activated file")?;
    if format!("{:x}", Sha256::digest(&snapshot.bytes)) != expected_sha256 {
        return invalid(format!(
            "activated file preimage differs from Lock: {relative}"
        ));
    }
    Ok(())
}

fn remove_activation_file(target: &Dir, record: &ActivationRecord) -> Result<(), LifecycleError> {
    verify_activation_file(target, &record.path, record.mode, &record.sha256)?;
    let (parent, name) = open_relative_parent(target, &record.path, "activated file")?;
    parent.remove_file(name)?;
    Ok(())
}

fn replace_config(
    target: &Dir,
    scratch: &Dir,
    original: &FileSnapshot,
    candidate: &[u8],
    handler_hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    verify_file_snapshot(target, "config.toml", original, "config.toml")?;
    let (temporary, prepared) = prepare_config_temporary(scratch, original, candidate)?;
    let result = (|| {
        verify_file_snapshot(target, "config.toml", original, "config.toml")?;
        let current_temporary = read_required_file(
            scratch,
            &temporary,
            Some(original.mode),
            "config replacement temporary file",
        )?;
        if !same_object_cap(&prepared.identity, &current_temporary.identity)
            || !same_content_state_cap(&prepared.identity, &current_temporary.identity)
            || current_temporary.identity.nlink() != 1
            || current_temporary.bytes != candidate
        {
            return invalid("config replacement temporary file changed before publication");
        }
        handler_hook(&temporary, "config-temporary-prepared")?;
        scratch.rename(&temporary, target, "config.toml")?;
        let current =
            read_required_file(target, "config.toml", Some(original.mode), "config.toml")?;
        if !same_object_cap(&prepared.identity, &current.identity)
            || current.identity.nlink() != 1
            || current.bytes != candidate
        {
            return invalid("config.toml replacement differs after source deactivation");
        }
        Ok(())
    })();
    if let Err(error) = result {
        return cleanup_temporary_after_error(scratch, &temporary, error);
    }
    Ok(())
}

fn prepare_config_temporary(
    scratch: &Dir,
    original: &FileSnapshot,
    candidate: &[u8],
) -> Result<(String, FileSnapshot), LifecycleError> {
    let mut temporary = None;
    for attempt in 0..128_u64 {
        let name = format!(
            ".config.toml.agent-skills-{}-{}-{}",
            std::process::id(),
            CONFIG_TEMPORARY_ID.fetch_add(1, Ordering::Relaxed),
            attempt
        );
        let mut options = OpenOptions::new();
        options
            .write(true)
            .create_new(true)
            .follow(FollowSymlinks::No);
        configure_nofollow(&mut options);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        match scratch.open_with(&name, &options) {
            Ok(mut file) => {
                let opened = file.metadata()?;
                let prepared = (|| {
                    if opened.nlink() != 1 || !mode_matches(file_mode(&opened), Some(0o600)) {
                        return invalid(
                            "config replacement temporary file is not private or has a hard-link alias",
                        );
                    }
                    file.write_all(candidate)?;
                    #[cfg(unix)]
                    {
                        use cap_std::fs::{Permissions, PermissionsExt as _};
                        file.set_permissions(Permissions::from_mode(original.mode))?;
                    }
                    file.sync_all()?;
                    let completed = file.metadata()?;
                    if !same_object_cap(&opened, &completed)
                        || completed.nlink() != 1
                        || !mode_matches(file_mode(&completed), Some(original.mode))
                    {
                        return invalid("config replacement temporary file changed while writing");
                    }
                    drop(file);
                    let reopened = read_required_file(
                        scratch,
                        &name,
                        Some(original.mode),
                        "config replacement temporary file",
                    )?;
                    if !same_object_cap(&completed, &reopened.identity)
                        || !same_content_state_cap(&completed, &reopened.identity)
                        || reopened.identity.nlink() != 1
                        || reopened.bytes != candidate
                    {
                        return invalid("config replacement temporary file changed after writing");
                    }
                    Ok(reopened)
                })();
                match prepared {
                    Ok(prepared) => temporary = Some((name, prepared)),
                    Err(error) => return cleanup_temporary_after_error(scratch, &name, error),
                }
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
    }
    temporary.ok_or_else(|| invalid_error("could not allocate config replacement"))
}

fn cleanup_temporary_after_error<T>(
    scratch: &Dir,
    temporary: &str,
    error: LifecycleError,
) -> Result<T, LifecycleError> {
    match scratch.remove_file(temporary) {
        Ok(()) => Err(error),
        Err(cleanup) if cleanup.kind() == std::io::ErrorKind::NotFound => Err(error),
        Err(cleanup) => invalid(format!(
            "{error}; config replacement temporary cleanup is incomplete: {cleanup}"
        )),
    }
}

fn verify_file_snapshot(
    parent: &Dir,
    name: &str,
    expected: &FileSnapshot,
    label: &str,
) -> Result<(), LifecycleError> {
    let current = read_required_file(parent, name, Some(expected.mode), label)?;
    if !same_object_cap(&expected.identity, &current.identity)
        || !same_content_state_cap(&expected.identity, &current.identity)
        || expected.bytes != current.bytes
    {
        return invalid(format!("{label} changed before source deactivation"));
    }
    Ok(())
}

fn read_optional_file(
    parent: &Dir,
    name: &str,
    mode: Option<u32>,
    label: &str,
) -> Result<Option<FileSnapshot>, LifecycleError> {
    match parent.symlink_metadata(name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            invalid(format!("{label} must be a regular file"))
        }
        Ok(_) => read_required_file(parent, name, mode, label).map(Some),
    }
}

fn read_required_file(
    parent: &Dir,
    name: &str,
    expected_mode: Option<u32>,
    label: &str,
) -> Result<FileSnapshot, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| invalid_error(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return invalid(format!("{label} is missing or unsafe"));
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    let mut file = parent
        .open_with(name, &options)
        .map_err(|_| invalid_error(format!("{label} is missing or unsafe")))?;
    let identity = file.metadata()?;
    let mode = file_mode(&identity);
    if !mode_matches(mode, expected_mode) {
        return invalid(format!("{label} mode is not canonical"));
    }
    if identity.len() > MAX_CONTRACT_JSON_BYTES as u64 {
        return Err(agent_contracts::ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    let capacity = usize::try_from(identity.len())
        .unwrap_or(MAX_CONTRACT_JSON_BYTES)
        .min(MAX_CONTRACT_JSON_BYTES);
    let mut bytes = Vec::with_capacity(capacity);
    (&mut file)
        .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_CONTRACT_JSON_BYTES {
        return Err(agent_contracts::ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    let after = file.metadata()?;
    let reopened = parent
        .open_with(name, &options)
        .and_then(|file| file.metadata())
        .map_err(|_| invalid_error(format!("{label} changed while reading")))?;
    if !same_object_cap(&identity, &after)
        || !same_object_cap(&identity, &reopened)
        || !same_content_state_cap(&identity, &after)
        || !same_content_state_cap(&identity, &reopened)
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(FileSnapshot {
        bytes,
        identity,
        mode,
    })
}

fn open_relative_parent<'a>(
    root: &Dir,
    relative: &'a str,
    label: &str,
) -> Result<(Dir, &'a str), LifecycleError> {
    let path = Path::new(relative);
    if path.is_absolute() {
        return invalid(format!("{label} must be a package-relative path"));
    }
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => parts.push(
                part.to_str()
                    .ok_or_else(|| invalid_error(format!("{label} path is invalid")))?,
            ),
            Component::CurDir => {}
            Component::ParentDir | Component::Prefix(_) | Component::RootDir => {
                return invalid(format!("{label} must be a package-relative path"));
            }
        }
    }
    let (name, parents) = parts
        .split_last()
        .ok_or_else(|| invalid_error(format!("{label} must be a package-relative path")))?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
    }
    Ok((directory, name))
}

#[cfg(unix)]
fn file_mode(metadata: &Metadata) -> u32 {
    use cap_std::fs::MetadataExt as _;
    metadata.mode() & 0o777
}

#[cfg(not(unix))]
fn file_mode(_metadata: &Metadata) -> u32 {
    MANAGED_FILE_MODE
}

#[cfg(unix)]
fn mode_matches(actual: u32, expected: Option<u32>) -> bool {
    expected.is_none_or(|expected| expected == actual)
}

#[cfg(not(unix))]
fn mode_matches(_actual: u32, _expected: Option<u32>) -> bool {
    true
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(invalid_error(message))
}

fn invalid_error(message: impl Into<String>) -> LifecycleError {
    LifecycleError::Invalid(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use cap_std::ambient_authority;
    use std::sync::atomic::AtomicU64;

    static TEST_ID: AtomicU64 = AtomicU64::new(0);

    fn set_mode(path: &Path, mode: u32) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))
                .expect("set fixture mode");
        }
        #[cfg(not(unix))]
        let _ = (path, mode);
    }

    fn write_fixture_file(root: &Path, relative: &str, bytes: &[u8], mode: u32) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("fixture file parent"))
            .expect("create fixture parent");
        std::fs::write(&path, bytes).expect("write fixture file");
        set_mode(&path, mode);
        let mut parent = path.parent();
        while let Some(directory) = parent {
            if !directory.starts_with(root) {
                break;
            }
            set_mode(directory, MANAGED_DIRECTORY_MODE);
            if directory == root {
                break;
            }
            parent = directory.parent();
        }
    }

    fn activation_fixture() -> (PathBuf, Dir, Dir) {
        let target_path = std::env::temp_dir().join(format!(
            "agent-source-activation-{}-{}",
            std::process::id(),
            TEST_ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&target_path).expect("create activation target");
        set_mode(&target_path, MANAGED_DIRECTORY_MODE);
        for asset in ACTIVATION_ASSETS {
            write_fixture_file(
                &target_path,
                &format!(".agent-skills/packages/{}/{}", asset.package, asset.source),
                format!("# {}/{}\n", asset.package, asset.source).as_bytes(),
                asset.mode,
            );
        }
        for name in PROFILE_NAMES {
            write_fixture_file(
                &target_path,
                &format!(".agent-skills/packages/codex/assets/codex/profiles/{name}"),
                format!("# managed profile {name}\n").as_bytes(),
                MANAGED_FILE_MODE,
            );
        }
        write_fixture_file(
            &target_path,
            ".agent-skills/packages/codex/assets/codex/codex.shared.toml",
            b"model = \"shared\"\n[features]\nmanaged = true\n",
            MANAGED_FILE_MODE,
        );
        write_fixture_file(
            &target_path,
            "agents/reviewer.toml",
            b"# old reviewer\n",
            MANAGED_FILE_MODE,
        );
        write_fixture_file(&target_path, "bin/retired", b"retired\n", 0o755);
        let design = ACTIVATION_ASSETS
            .iter()
            .find(|asset| asset.destination == "agents/design_researcher.toml")
            .expect("design asset");
        write_fixture_file(
            &target_path,
            design.destination,
            format!("# {}/{}\n", design.package, design.source).as_bytes(),
            design.mode,
        );
        write_fixture_file(
            &target_path,
            "readonly.config.toml",
            b"# user profile\n",
            0o600,
        );
        write_fixture_file(
            &target_path,
            "config.toml",
            b"model = \"local\"\n[features]\nlocal = true\n",
            0o640,
        );
        let lock = json!({
            "files": [
                {
                    "mode": MANAGED_FILE_MODE,
                    "path": "agents/reviewer.toml",
                    "sha256": bytes_sha256(b"# old reviewer\n"),
                },
                {
                    "mode": 0o755,
                    "path": "bin/retired",
                    "sha256": bytes_sha256(b"retired\n"),
                },
            ],
            "manager": "agent-development-skills",
            "schema_version": "1.0",
        });
        write_fixture_file(
            &target_path,
            ".agent-skills/activation-lock.json",
            &canonical_json(&lock).expect("encode fixture lock"),
            MANAGED_FILE_MODE,
        );
        std::fs::create_dir(target_path.join(".scratch")).expect("create fixture scratch");
        set_mode(&target_path.join(".scratch"), 0o700);
        let target_path = target_path
            .canonicalize()
            .expect("canonical fixture target");
        let target =
            Dir::open_ambient_dir(&target_path, ambient_authority()).expect("open fixture target");
        let scratch = Dir::open_ambient_dir(target_path.join(".scratch"), ambient_authority())
            .expect("open fixture scratch");
        (target_path, target, scratch)
    }

    #[test]
    fn root_assignment_removal_preserves_every_other_byte() {
        let input = concat!(
            "# heading\n",
            "\"model_instructions_file\" = \"/tmp/AGENTS.md\" # owned\n",
            "model = \"gpt\"\n",
            "[features]\n",
            "model_instructions_file = \"nested\"\n",
        );
        let (candidate, matches) = remove_root_assignment(input);
        assert_eq!(matches, 1);
        assert_eq!(
            candidate,
            concat!(
                "# heading\n",
                "model = \"gpt\"\n",
                "[features]\n",
                "model_instructions_file = \"nested\"\n",
            )
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_contract_target_matches_normal_and_explicit_verbatim_config() {
        let target_path = std::env::temp_dir().join(format!(
            "agent-source-config-deactivation-{}-{}",
            std::process::id(),
            TEST_ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&target_path).expect("create config target");
        let operational_path = target_path.canonicalize().expect("canonical config target");
        let visible_path = crate::transaction_lock::strip_verbatim_prefix(&operational_path);
        assert_ne!(
            operational_path, visible_path,
            "Windows canonical paths should retain a verbatim operational form"
        );
        let target = Dir::open_ambient_dir(&operational_path, ambient_authority())
            .expect("open config target");
        for contract_path in [&visible_path, &operational_path] {
            let instructions = contract_path
                .join("AGENTS.md")
                .to_string_lossy()
                .replace('\\', "\\\\");
            std::fs::write(
                target_path.join("config.toml"),
                format!("model_instructions_file = \"{instructions}\"\nmodel = \"local\"\n"),
            )
            .expect("write managed config");
            let prepared = prepare_config_deactivation(&target, contract_path)
                .expect("prepare config deactivation");
            let ConfigDeactivation::Replace {
                candidate,
                original,
            } = prepared
            else {
                panic!("contract-matching managed config must be removed");
            };
            assert_eq!(candidate, b"model = \"local\"\n");
            assert!(
                std::str::from_utf8(&original.bytes)
                    .expect("config UTF-8")
                    .contains("model_instructions_file")
            );
        }

        drop(target);
        std::fs::remove_dir_all(&target_path).expect("remove config target");
    }

    #[test]
    fn all_owned_preimages_are_revalidated_before_the_first_delete() {
        let target_path = std::env::temp_dir().join(format!(
            "agent-source-deactivation-{}-{}",
            std::process::id(),
            TEST_ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&target_path).expect("create target");
        std::fs::create_dir(target_path.join(".agent-skills")).expect("create managed root");
        std::fs::create_dir(target_path.join("bin")).expect("create bin");
        std::fs::write(target_path.join("bin/a"), b"a\n").expect("write first asset");
        std::fs::write(target_path.join("bin/b"), b"b\n").expect("write second asset");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            for path in [target_path.join(".agent-skills"), target_path.join("bin")] {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
                    .expect("set directory mode");
            }
            for path in [target_path.join("bin/a"), target_path.join("bin/b")] {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
                    .expect("set file mode");
            }
        }
        let lock = json!({
            "files": [
                {"mode": 0o755, "path": "bin/a", "sha256": format!("{:x}", Sha256::digest(b"a\n"))},
                {"mode": 0o755, "path": "bin/b", "sha256": format!("{:x}", Sha256::digest(b"b\n"))},
            ],
            "handler": "core.source-activation.apple-codex-v1",
            "manager": "agent-development-skills",
            "schema_version": "2.0",
        });
        let lock_path = target_path.join(".agent-skills/activation-lock.json");
        std::fs::write(
            &lock_path,
            agent_contracts::canonical_json(&lock).expect("encode lock"),
        )
        .expect("write lock");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(&lock_path, std::fs::Permissions::from_mode(0o644))
                .expect("set lock mode");
        }
        let target_path = target_path.canonicalize().expect("canonical target");
        let target = Dir::open_ambient_dir(&target_path, ambient_authority()).expect("open target");
        let prepared =
            SourceDeactivation::prepare(&target, &target_path).expect("prepare deactivation");
        std::fs::write(target_path.join("bin/b"), b"changed\n").expect("drift second asset");
        let error = prepared
            .apply_with_hook(&target, &target, |_, _| Ok(()))
            .expect_err("drift must fail before deletion");
        assert!(
            error
                .to_string()
                .contains("activated file preimage differs"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(target_path.join("bin/a")).expect("read first asset"),
            b"a\n"
        );
        assert!(lock_path.exists());
        drop(target);
        std::fs::remove_dir_all(&target_path).expect("remove target");
    }

    #[test]
    fn source_activation_publishes_owned_assets_profiles_config_and_lock_last() {
        let (target_path, target, scratch) = activation_fixture();
        let prepared =
            SourceActivation::prepare(&target, &target_path, b"native session launcher\n")
                .expect("prepare source activation");
        assert!(prepared.scope().contains(&"bin/retired".to_owned()));
        assert!(prepared.scope().contains(&"config.toml".to_owned()));
        let mut phases = Vec::new();
        let result = prepared
            .apply_with_hook(
                &target,
                &target_path,
                &scratch,
                &target_path.join(".scratch"),
                |path, phase| {
                    phases.push((path.to_owned(), phase.to_owned()));
                    Ok(())
                },
            )
            .expect("apply source activation");
        assert_eq!(
            phases.last().map(|(_, phase)| phase.as_str()),
            Some("activation-lock-published")
        );
        assert_eq!(
            result.get("handler").and_then(Value::as_str),
            Some(ACTIVATION_HANDLER_ID)
        );
        assert_eq!(
            result.get("migration"),
            Some(&json!({
                "after_sha256": "55e815d59b610a7d020af1cd26bca7c3688d8eb55431660f1f9079f055d308ee",
                "artifact": "activation-lock",
                "before_sha256": "bc1bf4345c71c173e38ce260a5a93495cbaf0e7b46ae17af2e79ca2659fbf407",
                "fingerprint": "6767c3fe97f75c96902515ba01585083be77ce3ef368a8aa3917e6503713ff6d",
                "from_version": "1.0",
                "lossless": true,
                "schema_version": "1.0",
                "status": "applied",
                "steps": [{
                    "changes": ["add:/handler", "replace:/schema_version"],
                    "from_version": "1.0",
                    "lossless": true,
                    "to_version": "2.0",
                }],
                "to_version": "2.0",
            }))
        );
        assert!(!target_path.join("bin/retired").exists());
        assert_eq!(
            std::fs::read(target_path.join("bin/agent-session"))
                .expect("read native session launcher"),
            b"native session launcher\n"
        );
        assert_eq!(
            std::fs::read(target_path.join("bin/agent-skills")).expect("read native lifecycle CLI"),
            b"native session launcher\n"
        );
        assert_eq!(
            std::fs::read(target_path.join("readonly.config.toml"))
                .expect("read preserved user profile"),
            b"# user profile\n"
        );
        let config = std::fs::read_to_string(target_path.join("config.toml"))
            .expect("read activated config");
        assert!(config.contains("model = \"local\""));
        assert!(config.contains("managed = true"));
        assert!(config.contains("model_instructions_file"));
        let lock = parse_json(
            &std::fs::read(target_path.join(".agent-skills/activation-lock.json"))
                .expect("read activated Lock"),
        )
        .expect("parse activated Lock");
        assert_eq!(
            validate_activation_lock_contract(&lock)
                .expect("validate activated Lock")
                .0,
            "2.0"
        );
        drop(scratch);
        drop(target);
        std::fs::remove_dir_all(&target_path).expect("remove activation fixture");
    }

    #[test]
    fn fresh_source_activation_reads_stage_assets_and_destination_preimages_separately() {
        let (source_path, source, scratch) = activation_fixture();
        std::fs::remove_file(source_path.join(".agent-skills/activation-lock.json"))
            .expect("remove source Activation Lock");
        let destination_path = source_path.with_extension("fresh-target");
        std::fs::create_dir(&destination_path).expect("create fresh destination");
        std::fs::create_dir(destination_path.join(".agent-skills"))
            .expect("create published managed metadata");
        set_mode(&destination_path, MANAGED_DIRECTORY_MODE);
        set_mode(
            &destination_path.join(".agent-skills"),
            MANAGED_DIRECTORY_MODE,
        );
        write_fixture_file(
            &destination_path,
            "readonly.config.toml",
            b"# fresh user profile\n",
            0o600,
        );
        write_fixture_file(
            &destination_path,
            "config.toml",
            b"model = \"fresh-local\"\n",
            0o600,
        );
        let destination_path = destination_path
            .canonicalize()
            .expect("canonical fresh destination");
        let destination = Dir::open_ambient_dir(&destination_path, ambient_authority())
            .expect("open fresh destination");

        let prepared = SourceActivation::prepare_fresh(
            &source,
            &destination,
            &destination_path,
            b"fresh native session launcher\n",
        )
        .expect("prepare fresh source activation");
        assert!(prepared.scope().contains(&"config.toml".to_owned()));
        assert!(prepared.scope().contains(&"bin/agent-skills".to_owned()));
        assert!(prepared.scope().contains(&"bin/agent-session".to_owned()));
        assert!(prepared.migration.is_none());
        std::fs::rename(
            source_path.join(".agent-skills/packages"),
            destination_path.join(".agent-skills/packages"),
        )
        .expect("publish staged package assets");
        let result = prepared
            .apply_with_hook(
                &destination,
                &destination_path,
                &scratch,
                &source_path.join(".scratch"),
                |_, _| Ok(()),
            )
            .expect("apply fresh source activation");

        assert_eq!(
            result.get("migration"),
            Some(&Value::Null),
            "fresh activation must not report a Lock migration"
        );
        assert_eq!(
            std::fs::read(destination_path.join("bin/agent-session"))
                .expect("read fresh session launcher"),
            b"fresh native session launcher\n"
        );
        assert_eq!(
            std::fs::read(destination_path.join("bin/agent-skills"))
                .expect("read fresh native lifecycle CLI"),
            b"fresh native session launcher\n"
        );
        assert_eq!(
            std::fs::read(destination_path.join("readonly.config.toml"))
                .expect("read preserved fresh profile"),
            b"# fresh user profile\n"
        );
        let config = std::fs::read_to_string(destination_path.join("config.toml"))
            .expect("read fresh config");
        assert!(config.contains("model = \"fresh-local\""));
        assert!(config.contains("model_instructions_file"));
        assert!(
            destination_path
                .join(".agent-skills/activation-lock.json")
                .is_file()
        );

        drop(destination);
        drop(scratch);
        drop(source);
        std::fs::remove_dir_all(&destination_path).expect("remove fresh destination");
        std::fs::remove_dir_all(&source_path).expect("remove activation source");
    }

    #[test]
    fn source_activation_revalidates_every_preimage_before_retiring_files() {
        let (target_path, target, scratch) = activation_fixture();
        let prepared =
            SourceActivation::prepare(&target, &target_path, b"native session launcher\n")
                .expect("prepare source activation");
        std::fs::write(target_path.join("config.toml"), b"model = \"drift\"\n")
            .expect("drift config");
        let error = prepared
            .apply_with_hook(
                &target,
                &target_path,
                &scratch,
                &target_path.join(".scratch"),
                |_, _| Ok(()),
            )
            .expect_err("drift must fail before writes");
        assert!(
            error.to_string().contains("changed after preflight"),
            "{error}"
        );
        assert!(target_path.join("bin/retired").is_file());
        let lock = parse_json(
            &std::fs::read(target_path.join(".agent-skills/activation-lock.json"))
                .expect("read original Lock"),
        )
        .expect("parse original Lock");
        assert_eq!(
            lock.get("schema_version").and_then(Value::as_str),
            Some("1.0")
        );
        drop(scratch);
        drop(target);
        std::fs::remove_dir_all(&target_path).expect("remove activation fixture");
    }
}
