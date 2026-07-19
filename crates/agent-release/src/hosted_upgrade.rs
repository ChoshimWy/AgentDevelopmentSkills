use agent_contracts::{canonical_json, canonical_sha256, parse_json};
use agent_engine::{validate_upgrade_conformance_evidence, validate_upgrade_source_qualification};
use agent_lifecycle::{
    InstalledSourceSelection, SourceInstallBundle, SourceInstallSelection, SourcePackageSet,
    compile_source_upgrade_bundle_bound, inspect_installed_source_selection,
    inspect_source_upgrade_bound, resolve_source_install_selection, snapshot_source_packages,
    upgrade_source_bundle,
};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::OpenOptions;
use std::io::{Cursor, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};
use tempfile::TempDir;
use zip::ZipArchive;

use crate::ReleaseError;

const MAX_MANIFEST_BYTES: u64 = 1024 * 1024;
const MAX_QUALIFICATION_BYTES: u64 = 16 * 1024 * 1024;
const MAX_SOURCE_BYTES: u64 = 128 * 1024 * 1024;
const MAX_EXPANDED_BYTES: u64 = 256 * 1024 * 1024;
const MAX_ARCHIVE_ENTRIES: usize = 10_000;
const MAX_REDIRECTS: usize = 5;
const DOWNLOAD_TIMEOUT: Duration = Duration::from_mins(1);
const USER_AGENT: &str = "agent-development-skills-upgrade/1.0";
/// Only the repository-owned Pages control plane may select a hosted upgrade.
pub const HOSTED_UPGRADE_MANIFEST_URL: &str =
    "https://choshimwy.github.io/AgentDevelopmentSkills/release-manifest.json";
const SOURCE_REPOSITORY: &str = "https://github.com/ChoshimWy/AgentDevelopmentSkills";
const RELEASE_ASSET_HOST: &str = "release-assets.githubusercontent.com";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReleaseManifest {
    artifacts: Vec<SourceArtifact>,
    asset_base_url: String,
    bootstrap_assets: Vec<FileRecord>,
    channel: String,
    default_engine: String,
    minimum_python: String,
    native_artifacts: Vec<NativeArtifact>,
    native_index_sha256: String,
    product: String,
    schema_version: String,
    source: SourceIdentity,
    upgrade_source_qualification: FileRecord,
    version: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceArtifact {
    entrypoint: String,
    filename: String,
    format: String,
    host_os: Vec<String>,
    id: String,
    root: String,
    sha256: String,
    size: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileRecord {
    filename: String,
    sha256: String,
    size: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeArtifact {
    arch: String,
    filename: String,
    os: String,
    sha256: String,
    size: u64,
    target: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceIdentity {
    dirty: bool,
    repository: String,
    revision: String,
}

/// One authenticated, extracted hosted upgrade source retained in a private workspace.
///
/// The workspace remains alive until this value is dropped. The extracted tree
/// stays private until a controlled Package Lock compiler can bind a candidate
/// directly to this qualified source without exposing a mutable path.
pub struct HostedUpgradeSource {
    manifest: Value,
    qualification: Value,
    source_archive: Vec<u8>,
    source_artifact: SourceArtifact,
    source_root: PathBuf,
    native_launcher: Vec<u8>,
    _workspace: TempDir,
}

/// One candidate compiled only from an authenticated hosted source.
///
/// Source paths and mutable compilation inputs remain private. The exact
/// installed selection, package snapshot, candidate Lock, and release
/// qualification stay alive together so callers cannot bind arbitrary hashes.
pub struct HostedUpgradeCandidate {
    source: HostedUpgradeSource,
    selection: SourceInstallSelection,
    packages: SourcePackageSet,
    bundle: SourceInstallBundle,
    evidence: Value,
    installed: InstalledSourceSelection,
    package_lock_hash: String,
    source_qualification_fingerprint: String,
}

impl HostedUpgradeSource {
    /// Canonical Release Manifest v3 that selected the source.
    #[must_use]
    pub const fn manifest(&self) -> &Value {
        &self.manifest
    }

    /// Release-bound Upgrade Source Qualification v1.
    #[must_use]
    pub const fn qualification(&self) -> &Value {
        &self.qualification
    }

    /// Compile one candidate from this exact qualified source and the installed selection.
    ///
    /// The source archive is independently extracted and compiled a second time.
    /// Both candidate projections must match before Conformance evidence is
    /// bound to the candidate Package Lock.
    ///
    /// # Errors
    /// Returns a fail-closed error for invalid installed state, source drift,
    /// nondeterministic compilation, Schema disagreement, or invalid evidence.
    pub fn compile_candidate(
        self,
        target_root: impl AsRef<Path>,
    ) -> Result<HostedUpgradeCandidate, ReleaseError> {
        let target_root = target_root.as_ref();
        let installed = inspect_installed_source_selection(target_root)?;
        let selection = resolve_source_install_selection(
            self.source_root.join("platforms"),
            installed.platforms(),
            installed.disciplines(),
            installed.runtime_configs(),
            installed.core_only(),
        )?;
        let packages = snapshot_source_packages(&selection)?;
        let bundle = compile_source_upgrade_bundle_bound(
            &selection,
            &packages,
            self.source_root.join("schemas"),
            target_root,
            &installed,
        )?;

        let (verification_workspace, verification_root) =
            extract_archive_workspace(&self.source_archive, &self.source_artifact)?;
        let verification_selection = resolve_source_install_selection(
            verification_root.join("platforms"),
            installed.platforms(),
            installed.disciplines(),
            installed.runtime_configs(),
            installed.core_only(),
        )?;
        let verification_packages = snapshot_source_packages(&verification_selection)?;
        let verification_bundle = compile_source_upgrade_bundle_bound(
            &verification_selection,
            &verification_packages,
            verification_root.join("schemas"),
            target_root,
            &installed,
        )?;
        if bundle.compatibility_projection() != verification_bundle.compatibility_projection()
            || packages.compatibility_projection()
                != verification_packages.compatibility_projection()
        {
            return contract(
                "hosted upgrade candidate is not reproducible from its source archive",
            );
        }
        drop(verification_workspace);

        let evidence = derive_candidate_evidence(&self.qualification, bundle.package_lock())?;
        let package_lock_hash = bundle
            .package_lock()
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ReleaseError::Contract(
                    "compiled candidate Package Lock fingerprint is invalid".to_owned(),
                )
            })?
            .to_owned();
        let source_qualification_fingerprint = self
            .qualification
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                ReleaseError::Contract(
                    "hosted source qualification fingerprint is invalid".to_owned(),
                )
            })?
            .to_owned();
        Ok(HostedUpgradeCandidate {
            source: self,
            selection,
            packages,
            bundle,
            evidence,
            installed,
            package_lock_hash,
            source_qualification_fingerprint,
        })
    }
}

impl HostedUpgradeCandidate {
    /// Exact Package Lock hash compiled from the qualified source.
    #[must_use]
    pub fn package_lock_hash(&self) -> &str {
        &self.package_lock_hash
    }

    /// Source Qualification fingerprint retained with this candidate.
    #[must_use]
    pub fn source_qualification_fingerprint(&self) -> &str {
        &self.source_qualification_fingerprint
    }

    /// Build one hosted approval envelope without exposing source paths or evidence.
    ///
    /// The selected package snapshot is refreshed immediately before planning
    /// and must remain byte-identical to the compiled candidate.
    ///
    /// # Errors
    /// Returns a fail-closed error for source or target drift, invalid
    /// lifecycle scope, or stale candidate evidence.
    pub fn inspect_upgrade(&self, target_root: impl AsRef<Path>) -> Result<Value, ReleaseError> {
        let target_root = target_root.as_ref();
        let refreshed = snapshot_source_packages(&self.selection)?;
        if refreshed.compatibility_projection() != self.packages.compatibility_projection() {
            return contract("hosted upgrade source changed after candidate compilation");
        }
        let upgrade_plan = inspect_source_upgrade_bound(
            &self.bundle,
            &self.packages,
            target_root,
            &self.evidence,
            "upgrade",
            &[],
            &[],
            Some(&self.source.native_launcher),
            &self.installed,
        )?;
        hosted_upgrade_plan(
            &self.source.manifest,
            &self.source.qualification,
            &self.package_lock_hash,
            upgrade_plan,
        )
    }

    /// Apply one exact hosted approval envelope through the native lifecycle.
    ///
    /// The saved envelope, its fingerprint, release source identity, candidate
    /// Package Lock, installed Lock pair, source snapshot, and lifecycle Plan
    /// are all revalidated before publication. The authenticated current-host
    /// executable is supplied to the trusted source Activation handler.
    ///
    /// # Errors
    /// Returns a fail-closed error for stale approval, source/target drift,
    /// altered provenance, incomplete approvals, or lifecycle publication
    /// failure.
    pub fn apply_upgrade(
        &self,
        target_root: impl AsRef<Path>,
        approved_plan: &Value,
        approved_fingerprint: &str,
        approvals: &[String],
    ) -> Result<Value, ReleaseError> {
        validate_hosted_upgrade_plan(approved_plan)?;
        if approved_plan.get("fingerprint").and_then(Value::as_str) != Some(approved_fingerprint) {
            return contract("hosted upgrade apply requires the exact envelope fingerprint");
        }
        let target_root = target_root.as_ref();
        let generated = self.inspect_upgrade(target_root)?;
        if &generated != approved_plan {
            return contract("saved hosted Upgrade Plan is stale or differs from the candidate");
        }
        let current = inspect_installed_source_selection(target_root)?;
        if current != self.installed {
            return contract("installed Lock pair changed after hosted candidate compilation");
        }
        let upgrade_plan = approved_plan.get("upgrade_plan").ok_or_else(|| {
            ReleaseError::Contract("hosted Upgrade Plan payload is missing".to_owned())
        })?;
        upgrade_source_bundle(
            &self.bundle,
            &self.packages,
            target_root,
            &self.evidence,
            upgrade_plan,
            approvals,
            "upgrade",
            &[],
            &[],
            Some(&self.source.native_launcher),
        )
        .map_err(ReleaseError::from)
    }
}

/// Validate the release-provenance envelope used by hosted upgrade approval.
///
/// # Errors
/// Returns a fail-closed error for unknown/missing fields, invalid identities,
/// a malformed lifecycle Plan, or a mismatched envelope fingerprint.
pub fn validate_hosted_upgrade_plan(value: &Value) -> Result<(), ReleaseError> {
    let object = value.as_object().ok_or_else(|| {
        ReleaseError::Contract("hosted Upgrade Plan must be a JSON object".to_owned())
    })?;
    let expected = [
        "candidate_package_lock_hash",
        "fingerprint",
        "manifest_sha256",
        "schema_version",
        "source_qualification_fingerprint",
        "source_revision",
        "upgrade_plan",
    ];
    if object.len() != expected.len() || !expected.iter().all(|field| object.contains_key(*field)) {
        return contract("hosted Upgrade Plan fields are invalid");
    }
    if object.get("schema_version").and_then(Value::as_str) != Some("1.0")
        || ![
            "candidate_package_lock_hash",
            "fingerprint",
            "manifest_sha256",
            "source_qualification_fingerprint",
        ]
        .iter()
        .all(|field| {
            object
                .get(*field)
                .and_then(Value::as_str)
                .is_some_and(is_hash)
        })
        || !object
            .get("source_revision")
            .and_then(Value::as_str)
            .is_some_and(is_revision)
    {
        return contract("hosted Upgrade Plan identity is invalid");
    }
    let upgrade_plan = object.get("upgrade_plan").ok_or_else(|| {
        ReleaseError::Contract("hosted Upgrade Plan payload is missing".to_owned())
    })?;
    agent_engine::validate_upgrade_plan(upgrade_plan)
        .map_err(|error| ReleaseError::Contract(error.to_string()))?;
    if upgrade_plan
        .pointer("/candidate/package_lock_hash")
        .and_then(Value::as_str)
        != object
            .get("candidate_package_lock_hash")
            .and_then(Value::as_str)
    {
        return contract("hosted Upgrade Plan candidate Lock identity mismatch");
    }
    let mut stable = object.clone();
    let fingerprint = stable
        .remove("fingerprint")
        .and_then(|value| value.as_str().map(str::to_owned))
        .ok_or_else(|| {
            ReleaseError::Contract("hosted Upgrade Plan fingerprint is invalid".to_owned())
        })?;
    if canonical_sha256(&Value::Object(stable))? != fingerprint {
        return contract("hosted Upgrade Plan fingerprint mismatch");
    }
    Ok(())
}

fn hosted_upgrade_plan(
    manifest: &Value,
    qualification: &Value,
    package_lock_hash: &str,
    upgrade_plan: Value,
) -> Result<Value, ReleaseError> {
    agent_engine::validate_upgrade_plan(&upgrade_plan)
        .map_err(|error| ReleaseError::Contract(error.to_string()))?;
    let source_revision = manifest
        .pointer("/source/revision")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ReleaseError::Contract("hosted release source revision is invalid".to_owned())
        })?;
    let qualification_fingerprint = qualification
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ReleaseError::Contract("hosted source qualification fingerprint is invalid".to_owned())
        })?;
    let mut object = serde_json::Map::from_iter([
        (
            "candidate_package_lock_hash".to_owned(),
            Value::String(package_lock_hash.to_owned()),
        ),
        (
            "manifest_sha256".to_owned(),
            Value::String(canonical_sha256(manifest)?),
        ),
        ("schema_version".to_owned(), Value::String("1.0".to_owned())),
        (
            "source_qualification_fingerprint".to_owned(),
            Value::String(qualification_fingerprint.to_owned()),
        ),
        (
            "source_revision".to_owned(),
            Value::String(source_revision.to_owned()),
        ),
        ("upgrade_plan".to_owned(), upgrade_plan),
    ]);
    let fingerprint = canonical_sha256(&Value::Object(object.clone()))?;
    object.insert("fingerprint".to_owned(), Value::String(fingerprint));
    let value = Value::Object(object);
    validate_hosted_upgrade_plan(&value)?;
    Ok(value)
}

/// Download, authenticate, and safely extract one hosted Release Manifest v3 source.
///
/// HTTPS is required for the manifest, asset base, and every redirect. Response
/// bodies, archive entry count, expanded bytes, paths, file types, and modes
/// are bounded before the source is retained.
///
/// # Errors
/// Returns a fail-closed error for transport failures, non-canonical or
/// mismatched contracts, insecure URLs, source tampering, or unsafe archives.
pub fn acquire_hosted_upgrade(manifest_url: &str) -> Result<HostedUpgradeSource, ReleaseError> {
    if manifest_url != HOSTED_UPGRADE_MANIFEST_URL {
        return contract("hosted upgrade manifest URL is not the repository-owned control plane");
    }
    let agent = https_agent();
    acquire_with_fetch(manifest_url, |url, maximum| {
        download_bounded(&agent, url, maximum)
    })
}

fn https_agent() -> ureq::Agent {
    let config = ureq::Agent::config_builder()
        .https_only(true)
        .max_redirects(0)
        .build();
    config.into()
}

enum FetchHop {
    Body(Vec<u8>),
    Redirect(String),
}

fn download_bounded(agent: &ureq::Agent, url: &str, maximum: u64) -> Result<Vec<u8>, ReleaseError> {
    follow_redirects(url, maximum, |current, remaining, maximum| {
        let mut response = agent
            .get(current)
            .header("User-Agent", USER_AGENT)
            .config()
            .timeout_global(Some(remaining))
            .build()
            .call()
            .map_err(|error| {
                ReleaseError::Contract(format!("hosted upgrade download failed: {error}"))
            })?;
        if response.status().is_redirection() {
            let location = response
                .headers()
                .get(ureq::http::header::LOCATION)
                .ok_or_else(|| {
                    ReleaseError::Contract("hosted upgrade redirect is missing Location".to_owned())
                })?
                .to_str()
                .map_err(|_| {
                    ReleaseError::Contract("hosted upgrade redirect Location is invalid".to_owned())
                })?;
            return Ok(FetchHop::Redirect(location.to_owned()));
        }
        if response.status() != ureq::http::StatusCode::OK {
            return contract("hosted upgrade response status is invalid");
        }
        let body = response
            .body_mut()
            .with_config()
            .limit(maximum + 1)
            .read_to_vec()
            .map_err(|error| {
                ReleaseError::Contract(format!("hosted upgrade response is invalid: {error}"))
            })?;
        Ok(FetchHop::Body(body))
    })
}

fn follow_redirects(
    requested: &str,
    maximum: u64,
    mut request: impl FnMut(&str, Duration, u64) -> Result<FetchHop, ReleaseError>,
) -> Result<Vec<u8>, ReleaseError> {
    let manifest_request = requested == HOSTED_UPGRADE_MANIFEST_URL;
    let deadline = Instant::now() + DOWNLOAD_TIMEOUT;
    let mut current = requested.to_owned();
    for hop in 0..=MAX_REDIRECTS {
        let uri = current.parse::<ureq::http::Uri>().map_err(|_| {
            ReleaseError::Contract("hosted upgrade redirect URL is invalid".to_owned())
        })?;
        validate_redirect_uri(&uri, manifest_request, hop)?;
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or_else(|| {
                ReleaseError::Contract("hosted upgrade download timed out".to_owned())
            })?;
        match request(&current, remaining, maximum)? {
            FetchHop::Body(bytes) => {
                if bytes.len() as u64 > maximum {
                    return contract("hosted upgrade response exceeds its size limit");
                }
                return Ok(bytes);
            }
            FetchHop::Redirect(location) if hop < MAX_REDIRECTS => current = location,
            FetchHop::Redirect(_) => {
                return contract("hosted upgrade response exceeded its redirect limit");
            }
        }
    }
    contract("hosted upgrade response exceeded its redirect limit")
}

fn validate_redirect_uri(
    uri: &ureq::http::Uri,
    manifest_request: bool,
    index: usize,
) -> Result<(), ReleaseError> {
    let authority = uri.authority().map(ureq::http::uri::Authority::as_str);
    let host = uri.host();
    let query = uri.path_and_query().and_then(|value| value.query());
    if uri.scheme_str() != Some("https")
        || authority.is_none_or(|value| value.is_empty() || value.contains('@'))
        || host.is_none()
        || uri.port_u16().is_some()
        || uri.to_string().contains('#')
    {
        return contract("hosted upgrade redirect contains an insecure URL");
    }
    if manifest_request {
        if host != Some("choshimwy.github.io")
            || uri.path() != "/AgentDevelopmentSkills/release-manifest.json"
            || query.is_some()
        {
            return contract("hosted upgrade manifest redirect left the control plane");
        }
    } else if index == 0 {
        if host != Some("github.com") || query.is_some() {
            return contract("hosted upgrade asset request differs from its frozen URL");
        }
    } else if host != Some("github.com") && host != Some(RELEASE_ASSET_HOST) {
        return contract("hosted upgrade asset redirect left the release origin allowlist");
    } else if host == Some("github.com") && query.is_some() {
        return contract("hosted upgrade GitHub redirect contains an unexpected query");
    }
    Ok(())
}

fn acquire_with_fetch(
    manifest_url: &str,
    mut fetch: impl FnMut(&str, u64) -> Result<Vec<u8>, ReleaseError>,
) -> Result<HostedUpgradeSource, ReleaseError> {
    let manifest_url = secure_url(manifest_url, false)?;
    let manifest_bytes = fetch_bounded(&mut fetch, &manifest_url, MAX_MANIFEST_BYTES, "manifest")?;
    let manifest_value = canonical_value(&manifest_bytes, "release manifest")?;
    let manifest: ReleaseManifest = serde_json::from_value(manifest_value.clone())?;
    validate_manifest(&manifest)?;
    let asset_base = secure_url(&manifest.asset_base_url, true)?;
    let expected_asset_base = format!(
        "{SOURCE_REPOSITORY}/releases/download/v{}/",
        manifest.version
    );
    if asset_base != expected_asset_base {
        return contract("hosted release asset base differs from its repository and version");
    }

    let qualification_url =
        asset_url(&asset_base, &manifest.upgrade_source_qualification.filename)?;
    let qualification_bytes = fetch_exact(
        &mut fetch,
        &qualification_url,
        &manifest.upgrade_source_qualification,
        MAX_QUALIFICATION_BYTES,
        "Upgrade Source Qualification",
    )?;
    let qualification = canonical_value(&qualification_bytes, "Upgrade Source Qualification")?;
    validate_upgrade_source_qualification(&qualification)
        .map_err(|error| ReleaseError::Contract(error.to_string()))?;
    let source = qualification
        .get("source")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            ReleaseError::Contract("Upgrade Source Qualification source is invalid".to_owned())
        })?;
    let artifact = manifest.artifacts.first().ok_or_else(|| {
        ReleaseError::Contract("hosted upgrade source artifact is missing".to_owned())
    })?;
    if source.get("artifact_sha256").and_then(Value::as_str) != Some(&artifact.sha256)
        || source.get("artifact_size").and_then(Value::as_u64) != Some(artifact.size)
        || source.get("revision").and_then(Value::as_str) != Some(&manifest.source.revision)
        || source.get("root").and_then(Value::as_str) != Some(&artifact.root)
    {
        return contract("hosted upgrade source differs from its qualification");
    }

    let source_url = asset_url(&asset_base, &artifact.filename)?;
    let source_record = FileRecord {
        filename: artifact.filename.clone(),
        sha256: artifact.sha256.clone(),
        size: artifact.size,
    };
    let source_bytes = fetch_exact(
        &mut fetch,
        &source_url,
        &source_record,
        MAX_SOURCE_BYTES,
        "source archive",
    )?;
    let native_target = host_native_target()?;
    let native = manifest
        .native_artifacts
        .iter()
        .find(|artifact| artifact.target == native_target)
        .ok_or_else(|| {
            ReleaseError::Contract(
                "hosted release has no native executable for the current host".to_owned(),
            )
        })?;
    let native_url = asset_url(&asset_base, &native.filename)?;
    let native_record = FileRecord {
        filename: native.filename.clone(),
        sha256: native.sha256.clone(),
        size: native.size,
    };
    let native_launcher = fetch_exact(
        &mut fetch,
        &native_url,
        &native_record,
        MAX_SOURCE_BYTES,
        "native executable",
    )?;
    crate::validate_binary_header(&native_launcher, crate::target_descriptor(native_target)?)?;
    let (workspace, source_root) = extract_archive_workspace(&source_bytes, artifact)?;
    Ok(HostedUpgradeSource {
        manifest: manifest_value,
        qualification,
        source_archive: source_bytes,
        source_artifact: artifact.clone(),
        source_root,
        native_launcher,
        _workspace: workspace,
    })
}

fn extract_archive_workspace(
    source_bytes: &[u8],
    artifact: &SourceArtifact,
) -> Result<(TempDir, PathBuf), ReleaseError> {
    let workspace = tempfile::Builder::new()
        .prefix("agent-hosted-upgrade-")
        .tempdir()?;
    let extracted = workspace.path().join("source");
    std::fs::create_dir(&extracted)?;
    extract_source(source_bytes, &extracted)?;
    let source_root = extracted.join(&artifact.root);
    let entrypoint = source_root.join(&artifact.entrypoint);
    let source_metadata = source_root
        .symlink_metadata()
        .map_err(|_| ReleaseError::Contract("hosted upgrade source root is missing".to_owned()))?;
    let entrypoint_metadata = entrypoint.symlink_metadata().map_err(|_| {
        ReleaseError::Contract("hosted upgrade source entrypoint is missing".to_owned())
    })?;
    if source_metadata.file_type().is_symlink()
        || !source_metadata.is_dir()
        || entrypoint_metadata.file_type().is_symlink()
        || !entrypoint_metadata.is_file()
    {
        return contract("hosted upgrade source root or entrypoint is missing");
    }
    Ok((workspace, source_root))
}

fn validate_manifest(manifest: &ReleaseManifest) -> Result<(), ReleaseError> {
    if manifest.schema_version != "3.0"
        || manifest.product != "agent-development-skills"
        || manifest.default_engine != "rust"
        || !matches!(manifest.channel.as_str(), "stable" | "beta")
        || manifest.source.dirty
        || !is_revision(&manifest.source.revision)
        || manifest.source.repository != SOURCE_REPOSITORY
        || !is_version(&manifest.version)
        || !is_version(&manifest.minimum_python)
        || !is_hash(&manifest.native_index_sha256)
        || manifest.artifacts.len() != 1
        || manifest.bootstrap_assets.is_empty()
        || manifest.native_artifacts.len() != 6
    {
        return contract("hosted release manifest identity is invalid");
    }
    let source = &manifest.artifacts[0];
    let expected_root = format!("agent-development-skills-{}", manifest.version);
    if source.id != "universal-source-bundle"
        || source.filename != format!("{expected_root}.zip")
        || source.format != "zip"
        || source.size == 0
        || source.size > MAX_SOURCE_BYTES
        || !is_hash(&source.sha256)
        || !safe_filename(&source.filename)
        || source.root != expected_root
        || source.entrypoint != "scripts/install_local.py"
        || source.host_os.is_empty()
        || source.host_os.windows(2).any(|pair| pair[0] >= pair[1])
        || !source
            .host_os
            .iter()
            .all(|host| matches!(host.as_str(), "darwin" | "linux" | "windows"))
        || !source.host_os.iter().any(|host| host == release_host_os())
    {
        return contract("hosted release source artifact is invalid for this host");
    }
    validate_file_records(&manifest.bootstrap_assets, 1024 * 1024, false)?;
    if manifest.upgrade_source_qualification.filename != "upgrade-source-qualification.json"
        || manifest.upgrade_source_qualification.size == 0
        || manifest.upgrade_source_qualification.size > MAX_QUALIFICATION_BYTES
        || !is_hash(&manifest.upgrade_source_qualification.sha256)
    {
        return contract("hosted release source qualification record is invalid");
    }
    let mut targets = Vec::new();
    let mut filenames = BTreeSet::new();
    for artifact in &manifest.native_artifacts {
        let expected = native_target_identity(&artifact.target).ok_or_else(|| {
            ReleaseError::Contract("hosted release native target is invalid".to_owned())
        })?;
        if artifact.os != expected.0
            || artifact.arch != expected.1
            || artifact.filename != native_filename(&manifest.version, &artifact.target)
            || artifact.size == 0
            || artifact.size > MAX_SOURCE_BYTES
            || !is_hash(&artifact.sha256)
            || !safe_filename(&artifact.filename)
            || !filenames.insert(artifact.filename.clone())
        {
            return contract("hosted release native artifact record is invalid");
        }
        targets.push(artifact.target.as_str());
    }
    let expected = [
        "aarch64-apple-darwin",
        "aarch64-pc-windows-msvc",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-gnu",
    ];
    if targets != expected {
        return contract("hosted release native target matrix is incomplete");
    }
    Ok(())
}

fn release_host_os() -> &'static str {
    if cfg!(target_os = "macos") {
        "darwin"
    } else {
        std::env::consts::OS
    }
}

fn host_native_target() -> Result<&'static str, ReleaseError> {
    match (std::env::consts::ARCH, std::env::consts::OS) {
        ("aarch64", "macos") => Ok("aarch64-apple-darwin"),
        ("x86_64", "macos") => Ok("x86_64-apple-darwin"),
        ("aarch64", "linux") => Ok("aarch64-unknown-linux-gnu"),
        ("x86_64", "linux") => Ok("x86_64-unknown-linux-gnu"),
        ("aarch64", "windows") => Ok("aarch64-pc-windows-msvc"),
        ("x86_64", "windows") => Ok("x86_64-pc-windows-msvc"),
        (arch, os) => contract(&format!(
            "hosted release does not support current native target {arch}-{os}"
        )),
    }
}

fn native_target_identity(target: &str) -> Option<(&'static str, &'static str)> {
    match target {
        "aarch64-apple-darwin" => Some(("darwin", "aarch64")),
        "aarch64-pc-windows-msvc" => Some(("windows", "aarch64")),
        "aarch64-unknown-linux-gnu" => Some(("linux", "aarch64")),
        "x86_64-apple-darwin" => Some(("darwin", "x86_64")),
        "x86_64-pc-windows-msvc" => Some(("windows", "x86_64")),
        "x86_64-unknown-linux-gnu" => Some(("linux", "x86_64")),
        _ => None,
    }
}

fn native_filename(version: &str, target: &str) -> String {
    let suffix = if target.contains("-windows-") {
        ".exe"
    } else {
        ""
    };
    format!("agent-skills-{version}-{target}{suffix}")
}

fn validate_file_records(
    records: &[FileRecord],
    maximum: u64,
    allow_qualification: bool,
) -> Result<(), ReleaseError> {
    let mut names = BTreeSet::new();
    let mut ordered = Vec::with_capacity(records.len());
    for record in records {
        if record.size == 0
            || record.size > maximum
            || !is_hash(&record.sha256)
            || !safe_filename(&record.filename)
            || (!allow_qualification && record.filename == "upgrade-source-qualification.json")
            || !names.insert(record.filename.clone())
        {
            return contract("hosted release file record is invalid");
        }
        ordered.push(record.filename.as_str());
    }
    if ordered.windows(2).any(|pair| pair[0] >= pair[1]) {
        return contract("hosted release file records must be sorted and unique");
    }
    Ok(())
}

fn fetch_bounded(
    fetch: &mut impl FnMut(&str, u64) -> Result<Vec<u8>, ReleaseError>,
    url: &str,
    maximum: u64,
    label: &str,
) -> Result<Vec<u8>, ReleaseError> {
    let bytes = fetch(url, maximum)?;
    if bytes.is_empty() || bytes.len() as u64 > maximum {
        return contract(&format!("{label} is empty or exceeds its size limit"));
    }
    Ok(bytes)
}

fn fetch_exact(
    fetch: &mut impl FnMut(&str, u64) -> Result<Vec<u8>, ReleaseError>,
    url: &str,
    record: &FileRecord,
    maximum: u64,
    label: &str,
) -> Result<Vec<u8>, ReleaseError> {
    if record.size == 0 || record.size > maximum {
        return contract(&format!("{label} size contract is invalid"));
    }
    let bytes = fetch_bounded(fetch, url, record.size, label)?;
    if bytes.len() as u64 != record.size || sha256(&bytes) != record.sha256 {
        return contract(&format!("{label} differs from its release identity"));
    }
    Ok(bytes)
}

fn canonical_value(bytes: &[u8], label: &str) -> Result<Value, ReleaseError> {
    let value = parse_json(bytes)?;
    if canonical_json(&value)? != bytes {
        return contract(&format!("{label} must use canonical JSON encoding"));
    }
    Ok(value)
}

fn derive_candidate_evidence(
    qualification: &Value,
    package_lock: &Value,
) -> Result<Value, ReleaseError> {
    validate_upgrade_source_qualification(qualification)
        .map_err(|error| ReleaseError::Contract(error.to_string()))?;
    let candidate_hash = package_lock
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ReleaseError::Contract("candidate Package Lock fingerprint is invalid".to_owned())
        })?;
    let schema_inventory_hash = package_lock
        .get("schema_inventory")
        .and_then(|value| value.get("content_sha256"))
        .and_then(Value::as_str)
        .ok_or_else(|| {
            ReleaseError::Contract("candidate Schema inventory hash is invalid".to_owned())
        })?;
    if qualification
        .get("schema_inventory_hash")
        .and_then(Value::as_str)
        != Some(schema_inventory_hash)
    {
        return contract(
            "qualified source Schema inventory differs from the candidate Package Lock",
        );
    }
    let source = qualification.as_object().ok_or_else(|| {
        ReleaseError::Contract("Upgrade Source Qualification is invalid".to_owned())
    })?;
    let mut evidence = serde_json::Map::new();
    for field in [
        "command_results",
        "environment",
        "manifest_count",
        "negative_contract_count",
        "runner_sha256",
        "schema_inventory_hash",
        "schema_version",
        "status",
        "suite",
        "suite_definition_hash",
        "test_count",
    ] {
        evidence.insert(
            field.to_owned(),
            source.get(field).cloned().ok_or_else(|| {
                ReleaseError::Contract(format!("Upgrade Source Qualification {field} is missing"))
            })?,
        );
    }
    evidence.insert(
        "candidate_package_lock_hash".to_owned(),
        Value::String(candidate_hash.to_owned()),
    );
    let stable_results = evidence
        .get("command_results")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            ReleaseError::Contract(
                "Upgrade Source Qualification command results are invalid".to_owned(),
            )
        })?
        .iter()
        .map(|result| {
            serde_json::json!({
                "command": result.get("command").cloned().unwrap_or(Value::Null),
                "exit_code": result.get("exit_code").cloned().unwrap_or(Value::Null),
            })
        })
        .collect::<Vec<_>>();
    let mut stable = evidence.clone();
    stable.insert("command_results".to_owned(), Value::Array(stable_results));
    evidence.insert(
        "attestation_key".to_owned(),
        Value::String(canonical_sha256(&Value::Object(stable))?),
    );
    let fingerprint = canonical_sha256(&Value::Object(evidence.clone()))?;
    evidence.insert("fingerprint".to_owned(), Value::String(fingerprint));
    let evidence = Value::Object(evidence);
    validate_upgrade_conformance_evidence(&evidence)
        .map_err(|error| ReleaseError::Contract(error.to_string()))?;
    Ok(evidence)
}

fn secure_url(value: &str, directory: bool) -> Result<String, ReleaseError> {
    let uri = value
        .parse::<ureq::http::Uri>()
        .map_err(|_| ReleaseError::Contract("hosted upgrade URL is invalid".to_owned()))?;
    let authority = uri.authority().map(ureq::http::uri::Authority::as_str);
    let path = uri.path_and_query();
    if uri.scheme_str() != Some("https")
        || authority.is_none_or(|value| value.is_empty() || value.contains('@'))
        || uri.port_u16().is_some()
        || path.is_none_or(|value| value.query().is_some())
        || value.contains('#')
        || directory != uri.path().ends_with('/')
    {
        return contract("hosted upgrade URL must be an exact credential-free HTTPS URL");
    }
    Ok(value.to_owned())
}

fn asset_url(base: &str, filename: &str) -> Result<String, ReleaseError> {
    if !safe_filename(filename) {
        return contract("hosted upgrade asset filename is unsafe");
    }
    secure_url(&format!("{base}{filename}"), false)
}

fn extract_source(bytes: &[u8], destination: &Path) -> Result<(), ReleaseError> {
    extract_source_with_limit(bytes, destination, MAX_EXPANDED_BYTES)
}

fn extract_source_with_limit(
    bytes: &[u8],
    destination: &Path,
    maximum_expanded_bytes: u64,
) -> Result<(), ReleaseError> {
    let mut archive = ZipArchive::new(Cursor::new(bytes))
        .map_err(|error| ReleaseError::Contract(format!("source archive is invalid: {error}")))?;
    if archive.is_empty() || archive.len() > MAX_ARCHIVE_ENTRIES {
        return contract("source archive entry count is invalid");
    }
    let mut names = BTreeSet::new();
    let mut portable_names = BTreeSet::new();
    let mut expanded = 0_u64;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|error| {
            ReleaseError::Contract(format!("source archive entry is invalid: {error}"))
        })?;
        let raw_name = entry.name().trim_end_matches('/');
        if raw_name.is_empty() || !raw_name.is_ascii() || !safe_relative(raw_name) {
            return contract("source archive contains an unsafe path");
        }
        let path = PathBuf::from(raw_name);
        let portable = raw_name.to_ascii_lowercase();
        if !names.insert(raw_name.to_owned()) || !portable_names.insert(portable) {
            return contract("source archive contains a duplicate portable path");
        }
        let mode = entry
            .unix_mode()
            .unwrap_or(if entry.is_dir() { 0o040_755 } else { 0o100_644 });
        let file_type = mode & 0o170_000;
        let permissions = mode & 0o777;
        if !matches!(file_type, 0 | 0o040_000 | 0o100_000)
            || !matches!(permissions, 0 | 0o644 | 0o755)
            || (file_type == 0o040_000 && !entry.is_dir())
            || (file_type == 0o100_000 && entry.is_dir())
            || (entry.is_dir() && entry.size() != 0)
        {
            return contract("source archive contains an unsupported file type or mode");
        }
        let declared_size = entry.size();
        let remaining = maximum_expanded_bytes
            .checked_sub(expanded)
            .ok_or_else(|| {
                ReleaseError::Contract("source archive expands beyond its size limit".to_owned())
            })?;
        if declared_size > remaining {
            return contract("source archive expands beyond its size limit");
        }
        let target = destination.join(&path);
        if entry.is_dir() {
            std::fs::create_dir_all(&target)?;
            continue;
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)?;
        let mut bounded = Read::by_ref(&mut entry).take(declared_size + 1);
        let copied = std::io::copy(&mut bounded, &mut output)?;
        if copied != declared_size {
            return contract("source archive entry exceeded or differed from its declared size");
        }
        expanded = expanded
            .checked_add(copied)
            .ok_or_else(|| ReleaseError::Contract("source archive size overflow".to_owned()))?;
        output.flush()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(&target, std::fs::Permissions::from_mode(permissions))?;
        }
    }
    Ok(())
}

fn safe_filename(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value.is_ascii()
        && !value.contains(['/', '\\', ':'])
        && safe_component(value)
}

fn safe_component(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 128
        || !value.is_ascii()
        || value.ends_with('.')
        || value.ends_with(' ')
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
    {
        return false;
    }
    let stem = value
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    !matches!(stem.as_str(), "aux" | "con" | "nul" | "prn")
        && !(stem.len() == 4
            && (stem.starts_with("com") || stem.starts_with("lpt"))
            && stem.as_bytes()[3].is_ascii_digit()
            && stem.as_bytes()[3] != b'0')
}

fn safe_relative(value: &str) -> bool {
    if value.is_empty() || value.contains('\\') || value.contains(':') {
        return false;
    }
    let path = Path::new(value);
    !path.is_absolute()
        && path.components().all(|component| match component {
            Component::Normal(value) => value.to_str().is_some_and(safe_component),
            _ => false,
        })
}

fn is_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_revision(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_version(value: &str) -> bool {
    let parts = value.split('.').collect::<Vec<_>>();
    matches!(parts.len(), 2 | 3)
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.bytes().all(|byte| byte.is_ascii_digit())
                && (part == &"0" || !part.starts_with('0'))
        })
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn contract<T>(message: &str) -> Result<T, ReleaseError> {
    Err(ReleaseError::Contract(message.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use agent_contracts::{canonical_json, canonical_sha256};
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::path::Path;
    use zip::write::SimpleFileOptions;

    fn source_archive(root: &str) -> Vec<u8> {
        let cursor = Cursor::new(Vec::new());
        let mut writer = zip::ZipWriter::new(cursor);
        writer
            .add_directory(
                format!("{root}/"),
                SimpleFileOptions::default().unix_permissions(0o755),
            )
            .unwrap();
        writer
            .add_directory(
                format!("{root}/scripts/"),
                SimpleFileOptions::default().unix_permissions(0o755),
            )
            .unwrap();
        writer
            .start_file(
                format!("{root}/scripts/install_local.py"),
                SimpleFileOptions::default()
                    .compression_method(zip::CompressionMethod::Deflated)
                    .unix_permissions(0o644),
            )
            .unwrap();
        writer.write_all(b"print('fixture')\n").unwrap();
        writer.finish().unwrap().into_inner()
    }

    fn fake_native_binary(target: &str) -> Vec<u8> {
        match target {
            "aarch64-apple-darwin" => [
                vec![0xcf, 0xfa, 0xed, 0xfe],
                0x0100_000cu32.to_le_bytes().to_vec(),
            ]
            .concat(),
            "x86_64-apple-darwin" => [
                vec![0xcf, 0xfa, 0xed, 0xfe],
                0x0100_0007u32.to_le_bytes().to_vec(),
            ]
            .concat(),
            "aarch64-unknown-linux-gnu" | "x86_64-unknown-linux-gnu" => {
                let mut bytes = vec![0_u8; 20];
                bytes[..4].copy_from_slice(b"\x7fELF");
                bytes[4] = 2;
                bytes[5] = 1;
                let machine = if target.starts_with("aarch64") {
                    183_u16
                } else {
                    62_u16
                };
                bytes[18..20].copy_from_slice(&machine.to_le_bytes());
                bytes
            }
            "aarch64-pc-windows-msvc" | "x86_64-pc-windows-msvc" => {
                let mut bytes = vec![0_u8; 70];
                bytes[..2].copy_from_slice(b"MZ");
                bytes[60..64].copy_from_slice(&64_u32.to_le_bytes());
                bytes[64..68].copy_from_slice(b"PE\0\0");
                let machine = if target.starts_with("aarch64") {
                    0xaa64_u16
                } else {
                    0x8664_u16
                };
                bytes[68..70].copy_from_slice(&machine.to_le_bytes());
                bytes
            }
            _ => panic!("unsupported test target"),
        }
    }

    fn repository_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("workspace root")
            .to_path_buf()
    }

    fn repository_source_archive(root: &str) -> Vec<u8> {
        let repository = repository_root();
        let cursor = Cursor::new(Vec::new());
        let mut writer = zip::ZipWriter::new(cursor);
        writer
            .add_directory(
                format!("{root}/"),
                SimpleFileOptions::default().unix_permissions(0o755),
            )
            .unwrap();
        for relative in ["disciplines", "platforms", "runtime-configs", "schemas"] {
            append_repository_tree(&mut writer, &repository, Path::new(relative), root);
        }
        append_repository_tree(
            &mut writer,
            &repository,
            Path::new("scripts/install_local.py"),
            root,
        );
        writer.finish().unwrap().into_inner()
    }

    fn append_repository_tree(
        writer: &mut zip::ZipWriter<Cursor<Vec<u8>>>,
        repository: &Path,
        relative: &Path,
        archive_root: &str,
    ) {
        let path = repository.join(relative);
        let metadata = path.symlink_metadata().unwrap();
        assert!(!metadata.file_type().is_symlink());
        let archive_path = format!("{archive_root}/{}", relative.to_string_lossy());
        if metadata.is_dir() {
            writer
                .add_directory(
                    format!("{archive_path}/"),
                    SimpleFileOptions::default().unix_permissions(0o755),
                )
                .unwrap();
            let mut children = path
                .read_dir()
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap();
            children.sort_by_key(std::fs::DirEntry::file_name);
            for child in children {
                append_repository_tree(
                    writer,
                    repository,
                    &relative.join(child.file_name()),
                    archive_root,
                );
            }
        } else {
            writer
                .start_file(
                    archive_path,
                    SimpleFileOptions::default()
                        .compression_method(zip::CompressionMethod::Deflated)
                        .unix_permissions(0o644),
                )
                .unwrap();
            writer.write_all(&std::fs::read(path).unwrap()).unwrap();
        }
    }

    fn qualification(source: &[u8], root: &str) -> Value {
        let mut value = json!({
            "command_results": [{
                "command": "compatibility-suite",
                "exit_code": 0,
                "stderr_sha256": "2".repeat(64),
                "stdout_sha256": "3".repeat(64),
            }],
            "environment": {"platform": "unit-test", "python": "3.11.0"},
            "manifest_count": 19,
            "negative_contract_count": 16,
            "runner_sha256": "4".repeat(64),
            "schema_inventory_hash": "5".repeat(64),
            "schema_version": "1.0",
            "source": {
                "artifact_sha256": sha256(source),
                "artifact_size": source.len(),
                "revision": "b".repeat(40),
                "root": root,
            },
            "source_materials_sha256": "c".repeat(64),
            "status": "passed",
            "suite": "agent-skills-release-conformance-v1",
            "suite_definition_hash": "6".repeat(64),
            "test_count": 531,
        });
        let mut stable = value.as_object().unwrap().clone();
        stable.insert(
            "command_results".to_owned(),
            json!([{"command": "compatibility-suite", "exit_code": 0}]),
        );
        value["attestation_key"] = Value::String(canonical_sha256(&Value::Object(stable)).unwrap());
        value["fingerprint"] = Value::String(canonical_sha256(&value).unwrap());
        value
    }

    fn bind_qualification_schema(qualification: &mut Value, schema_hash: &str) {
        qualification["schema_inventory_hash"] = Value::String(schema_hash.to_owned());
        qualification
            .as_object_mut()
            .unwrap()
            .remove("attestation_key");
        qualification.as_object_mut().unwrap().remove("fingerprint");
        let mut stable = qualification.as_object().unwrap().clone();
        stable.insert(
            "command_results".to_owned(),
            json!([{"command": "compatibility-suite", "exit_code": 0}]),
        );
        qualification["attestation_key"] =
            Value::String(canonical_sha256(&Value::Object(stable)).unwrap());
        qualification["fingerprint"] = Value::String(canonical_sha256(qualification).unwrap());
    }

    fn fixture() -> (Vec<u8>, BTreeMap<String, Vec<u8>>, String) {
        let root = "agent-development-skills-0.2.0";
        let source = source_archive(root);
        let qualification = canonical_json(&qualification(&source, root)).unwrap();
        let qualification_record = json!({
            "filename": "upgrade-source-qualification.json",
            "sha256": sha256(&qualification),
            "size": qualification.len(),
        });
        let targets = [
            ("aarch64", "darwin", "aarch64-apple-darwin"),
            ("aarch64", "windows", "aarch64-pc-windows-msvc"),
            ("aarch64", "linux", "aarch64-unknown-linux-gnu"),
            ("x86_64", "darwin", "x86_64-apple-darwin"),
            ("x86_64", "windows", "x86_64-pc-windows-msvc"),
            ("x86_64", "linux", "x86_64-unknown-linux-gnu"),
        ];
        let current_target = host_native_target().unwrap();
        let current_native = fake_native_binary(current_target);
        let native = targets
            .iter()
            .map(|(arch, os, target)| {
                let (sha256, size) = if *target == current_target {
                    (sha256(&current_native), current_native.len() as u64)
                } else {
                    ("7".repeat(64), 1024)
                };
                json!({
                    "arch": arch,
                    "filename": native_filename("0.2.0", target),
                    "os": os,
                    "sha256": sha256,
                    "size": size,
                    "target": target,
                })
            })
            .collect::<Vec<_>>();
        let manifest = canonical_json(&json!({
            "artifacts": [{
                "entrypoint": "scripts/install_local.py",
                "filename": "agent-development-skills-0.2.0.zip",
                "format": "zip",
                "host_os": [release_host_os()],
                "id": "universal-source-bundle",
                "root": root,
                "sha256": sha256(&source),
                "size": source.len(),
            }],
            "asset_base_url": "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/",
            "bootstrap_assets": [{
                "filename": "install.sh",
                "sha256": "8".repeat(64),
                "size": 1024,
            }],
            "channel": "beta",
            "default_engine": "rust",
            "minimum_python": "3.11",
            "native_artifacts": native,
            "native_index_sha256": "9".repeat(64),
            "product": "agent-development-skills",
            "schema_version": "3.0",
            "source": {
                "dirty": false,
                "repository": SOURCE_REPOSITORY,
                "revision": "b".repeat(40),
            },
            "upgrade_source_qualification": qualification_record,
            "version": "0.2.0",
        }))
        .unwrap();
        let mut assets = BTreeMap::new();
        assets.insert(
            "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/upgrade-source-qualification.json".to_owned(),
            qualification,
        );
        assets.insert(
            "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/agent-development-skills-0.2.0.zip".to_owned(),
            source,
        );
        assets.insert(
            format!(
                "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/{}",
                native_filename("0.2.0", current_target)
            ),
            current_native,
        );
        (manifest, assets, HOSTED_UPGRADE_MANIFEST_URL.to_owned())
    }

    #[test]
    fn hosted_source_is_exactly_bound_and_safely_extracted() {
        let (manifest, mut assets, manifest_url) = fixture();
        assets.insert(manifest_url.clone(), manifest);
        let acquired = acquire_with_fetch(&manifest_url, |url, maximum| {
            let bytes = assets
                .get(url)
                .cloned()
                .ok_or_else(|| ReleaseError::Contract("unexpected URL".to_owned()))?;
            assert!(bytes.len() as u64 <= maximum);
            Ok(bytes)
        })
        .unwrap();
        assert_eq!(acquired.manifest()["schema_version"], "3.0");
        validate_upgrade_source_qualification(acquired.qualification()).unwrap();
    }

    #[test]
    fn hosted_source_rejects_tamper_and_insecure_urls() {
        let (manifest, mut assets, manifest_url) = fixture();
        assets.insert(manifest_url.clone(), manifest);
        assets
            .get_mut("https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/agent-development-skills-0.2.0.zip")
            .unwrap()
            .push(0);
        assert!(acquire_with_fetch(&manifest_url, |url, _| Ok(assets[url].clone())).is_err());
        assert!(
            acquire_with_fetch(
                "http://pages.example.invalid/release-manifest.json",
                |_, _| unreachable!(),
            )
            .is_err()
        );
        assert!(acquire_hosted_upgrade("https://127.0.0.1/release-manifest.json").is_err());
    }

    #[test]
    fn redirect_policy_rejects_private_credentials_and_unfrozen_queries() {
        let github = "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/source.zip"
            .parse()
            .unwrap();
        validate_redirect_uri(&github, false, 0).unwrap();
        let release_asset =
            "https://release-assets.githubusercontent.com/asset?sp=read&sig=fixture"
                .parse()
                .unwrap();
        validate_redirect_uri(&release_asset, false, 1).unwrap();
        for value in [
            "https://127.0.0.1/internal",
            "https://169.254.169.254/metadata",
            "https://user@github.com/asset",
            "https://github.com:8443/asset",
            "https://github.com/asset?redirect=private",
            "http://github.com/asset",
        ] {
            if let Ok(uri) = value.parse::<ureq::http::Uri>() {
                assert!(validate_redirect_uri(&uri, false, 1).is_err(), "{value}");
            }
        }
    }

    #[test]
    fn redirect_transport_validates_each_location_before_requesting_it() {
        let asset = "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/source.zip";
        for location in [
            "https://127.0.0.1/internal",
            "https://169.254.169.254/metadata",
            "https://user@github.com/asset",
            "https://github.com:8443/asset",
            "https://github.com/asset?redirect=private",
            "http://github.com/asset",
            "/relative-location",
        ] {
            let mut calls = 0;
            let result = follow_redirects(asset, 16, |_, _, _| {
                calls += 1;
                Ok(if calls == 1 {
                    FetchHop::Redirect(location.to_owned())
                } else {
                    FetchHop::Body(b"must-not-be-requested".to_vec())
                })
            });
            assert!(result.is_err(), "{location}");
            assert_eq!(calls, 1, "{location}");
        }

        let mut calls = 0;
        let body = follow_redirects(asset, 16, |_, remaining, _| {
            calls += 1;
            assert!(!remaining.is_zero());
            Ok(if calls == 1 {
                FetchHop::Redirect(
                    "https://release-assets.githubusercontent.com/asset?sp=read&sig=fixture"
                        .to_owned(),
                )
            } else {
                FetchHop::Body(b"qualified".to_vec())
            })
        })
        .unwrap();
        assert_eq!(body, b"qualified");
        assert_eq!(calls, 2);
    }

    #[test]
    fn manifest_rejects_unknown_hosts_and_noncanonical_native_filenames() {
        let (manifest, _, _) = fixture();
        let mut value = parse_json(&manifest).unwrap();
        value["artifacts"][0]["host_os"] = json!(["darwin", "private-os"]);
        let manifest: ReleaseManifest = serde_json::from_value(value).unwrap();
        assert!(validate_manifest(&manifest).is_err());

        let (manifest, _, _) = fixture();
        let mut value = parse_json(&manifest).unwrap();
        value["native_artifacts"][1]["filename"] =
            Value::String("agent-skills-0.2.0-aarch64-pc-windows-msvc".to_owned());
        let manifest: ReleaseManifest = serde_json::from_value(value).unwrap();
        assert!(validate_manifest(&manifest).is_err());
    }

    #[test]
    fn qualification_binds_only_the_compiled_candidate_and_schema_inventory() {
        let source = source_archive("agent-development-skills-0.2.0");
        let qualification = qualification(&source, "agent-development-skills-0.2.0");
        let package_lock = json!({
            "fingerprint": "d".repeat(64),
            "schema_inventory": {"content_sha256": "5".repeat(64)},
        });
        let evidence = derive_candidate_evidence(&qualification, &package_lock).unwrap();
        assert_eq!(
            evidence["candidate_package_lock_hash"],
            package_lock["fingerprint"]
        );
        assert_eq!(
            evidence["schema_inventory_hash"],
            package_lock["schema_inventory"]["content_sha256"]
        );
        validate_upgrade_conformance_evidence(&evidence).unwrap();

        let mut mismatched = package_lock;
        mismatched["schema_inventory"]["content_sha256"] = Value::String("e".repeat(64));
        assert!(derive_candidate_evidence(&qualification, &mismatched).is_err());
    }

    #[test]
    fn hosted_candidate_infers_installed_selection_and_compiles_twice() {
        let repository = repository_root();
        let current_selection =
            resolve_source_install_selection(repository.join("platforms"), &[], &[], &[], true)
                .unwrap();
        let current_packages = snapshot_source_packages(&current_selection).unwrap();
        let current_bundle = agent_lifecycle::compile_source_install_bundle(
            &current_selection,
            &current_packages,
            repository.join("schemas"),
            None,
        )
        .unwrap();
        let target = tempfile::tempdir().unwrap();
        let installed_root = target.path().join("installed");
        agent_lifecycle::install_source_bundle(&current_bundle, &current_packages, &installed_root)
            .unwrap();

        let root = "agent-development-skills-0.2.0";
        let source_archive = repository_source_archive(root);
        let source_artifact = SourceArtifact {
            entrypoint: "scripts/install_local.py".to_owned(),
            filename: "agent-development-skills-0.2.0.zip".to_owned(),
            format: "zip".to_owned(),
            host_os: vec![release_host_os().to_owned()],
            id: "universal-source-bundle".to_owned(),
            root: root.to_owned(),
            sha256: sha256(&source_archive),
            size: source_archive.len() as u64,
        };
        let (workspace, source_root) =
            extract_archive_workspace(&source_archive, &source_artifact).unwrap();
        let mut qualification = qualification(&source_archive, root);
        let schema_hash = current_bundle.package_lock()["schema_inventory"]["content_sha256"]
            .as_str()
            .unwrap();
        bind_qualification_schema(&mut qualification, schema_hash);
        let hosted = HostedUpgradeSource {
            manifest: json!({
                "schema_version": "3.0",
                "source": {"revision": "b".repeat(40)},
            }),
            qualification,
            source_archive,
            source_artifact,
            source_root,
            native_launcher: fake_native_binary(host_native_target().unwrap()),
            _workspace: workspace,
        };

        let candidate = hosted.compile_candidate(&installed_root).unwrap();
        assert_eq!(
            candidate.package_lock_hash(),
            current_bundle.package_lock()["fingerprint"]
                .as_str()
                .unwrap()
        );
        let plan = candidate.inspect_upgrade(&installed_root).unwrap();
        assert_eq!(plan["upgrade_plan"]["status"], "no-change");
        assert_eq!(
            plan["upgrade_plan"]["candidate"]["package_lock_hash"],
            current_bundle.package_lock()["fingerprint"]
        );
        validate_hosted_upgrade_plan(&plan).unwrap();
        let fingerprint = plan["fingerprint"].as_str().unwrap();
        let result = candidate
            .apply_upgrade(&installed_root, &plan, fingerprint, &[])
            .unwrap();
        assert_eq!(result["status"], "no-change");

        let mut tampered = plan.clone();
        tampered["source_revision"] = Value::String("c".repeat(40));
        assert!(
            candidate
                .apply_upgrade(&installed_root, &tampered, fingerprint, &[])
                .is_err()
        );
        let mut cross_bound = plan.clone();
        cross_bound["candidate_package_lock_hash"] = Value::String("e".repeat(64));
        cross_bound.as_object_mut().unwrap().remove("fingerprint");
        cross_bound["fingerprint"] = Value::String(canonical_sha256(&cross_bound).unwrap());
        assert!(validate_hosted_upgrade_plan(&cross_bound).is_err());

        let replacement = target.path().join("replacement");
        let desktop_selection = resolve_source_install_selection(
            repository.join("platforms"),
            &["desktop".to_owned()],
            &[],
            &[],
            false,
        )
        .unwrap();
        let desktop_packages = snapshot_source_packages(&desktop_selection).unwrap();
        let desktop_bundle = agent_lifecycle::compile_source_install_bundle(
            &desktop_selection,
            &desktop_packages,
            repository.join("schemas"),
            None,
        )
        .unwrap();
        agent_lifecycle::install_source_bundle(&desktop_bundle, &desktop_packages, &replacement)
            .unwrap();
        std::fs::rename(&installed_root, target.path().join("retired")).unwrap();
        std::fs::rename(&replacement, &installed_root).unwrap();
        assert!(candidate.inspect_upgrade(&installed_root).is_err());
    }

    #[test]
    fn hosted_source_rejects_native_executable_tamper() {
        let (manifest, mut assets, manifest_url) = fixture();
        assets.insert(manifest_url.clone(), manifest);
        let native_url = format!(
            "https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v0.2.0/{}",
            native_filename("0.2.0", host_native_target().unwrap())
        );
        assets.get_mut(&native_url).unwrap().push(0);
        assert!(acquire_with_fetch(&manifest_url, |url, _| Ok(assets[url].clone())).is_err());
    }

    #[test]
    fn hosted_plan_schema_reuses_upgrade_plan_and_runtime_rejects_malformed_inner_plan() {
        let schema = parse_json(
            &std::fs::read(repository_root().join("schemas/hosted-upgrade-plan-v1.schema.json"))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            schema.pointer("/properties/upgrade_plan/$ref"),
            Some(&Value::String(
                "https://agent-workflow.local/schemas/upgrade-plan-v1.schema.json".to_owned()
            ))
        );
        let mut malformed = json!({
            "candidate_package_lock_hash": "1".repeat(64),
            "manifest_sha256": "2".repeat(64),
            "schema_version": "1.0",
            "source_qualification_fingerprint": "3".repeat(64),
            "source_revision": "4".repeat(40),
            "upgrade_plan": {},
        });
        malformed["fingerprint"] = Value::String(canonical_sha256(&malformed).unwrap());
        assert!(validate_hosted_upgrade_plan(&malformed).is_err());
    }

    #[test]
    fn archive_rejects_traversal_and_portable_collisions() {
        let cursor = Cursor::new(Vec::new());
        let mut writer = zip::ZipWriter::new(cursor);
        writer
            .start_file("../escape", SimpleFileOptions::default())
            .unwrap();
        writer.write_all(b"escape").unwrap();
        let traversal = writer.finish().unwrap().into_inner();
        let output = tempfile::tempdir().unwrap();
        assert!(extract_source(&traversal, output.path()).is_err());

        let cursor = Cursor::new(Vec::new());
        let mut writer = zip::ZipWriter::new(cursor);
        writer
            .start_file("root/File.txt", SimpleFileOptions::default())
            .unwrap();
        writer.write_all(b"one").unwrap();
        writer
            .start_file("root/file.txt", SimpleFileOptions::default())
            .unwrap();
        writer.write_all(b"two").unwrap();
        let collision = writer.finish().unwrap().into_inner();
        let output = tempfile::tempdir().unwrap();
        assert!(extract_source(&collision, output.path()).is_err());
    }

    #[test]
    fn archive_actual_output_is_bounded_when_declared_size_is_forged() {
        let cursor = Cursor::new(Vec::new());
        let mut writer = zip::ZipWriter::new(cursor);
        writer
            .start_file(
                "root/bomb.txt",
                SimpleFileOptions::default()
                    .compression_method(zip::CompressionMethod::Deflated)
                    .unix_permissions(0o644),
            )
            .unwrap();
        writer.write_all(&vec![b'a'; 1024 * 1024]).unwrap();
        let mut archive = writer.finish().unwrap().into_inner();
        let central = archive
            .windows(4)
            .position(|window| window == b"PK\x01\x02")
            .unwrap();
        archive[central + 24..central + 28].copy_from_slice(&1_u32.to_le_bytes());
        let local = archive
            .windows(4)
            .position(|window| window == b"PK\x03\x04")
            .unwrap();
        archive[local + 22..local + 26].copy_from_slice(&1_u32.to_le_bytes());

        let output = tempfile::tempdir().unwrap();
        assert!(extract_source_with_limit(&archive, output.path(), 16).is_err());
        let written = output.path().join("root/bomb.txt");
        if let Ok(metadata) = written.metadata() {
            assert!(metadata.len() <= 2);
        }
    }
}
