//! Native release-artifact contracts for the Rust distribution matrix.

mod hosted_upgrade;

pub use hosted_upgrade::{
    HOSTED_UPGRADE_MANIFEST_URL, HostedUpgradeCandidate, HostedUpgradeSource,
    acquire_hosted_upgrade, validate_hosted_upgrade_plan,
};

use agent_contracts::{canonical_json, canonical_sha256, parse_json};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use thiserror::Error;

const PRODUCT: &str = "agent-development-skills";
const SCHEMA_VERSION: &str = "1.0";
const PROFILE: &str = "release";
const KIND: &str = "native-binary";
const MAX_BINARY_BYTES: u64 = 128 * 1024 * 1024;
const MAX_RECORD_BYTES: u64 = 1024 * 1024;
const MAX_SMOKE_OUTPUT_BYTES: u64 = 64 * 1024;
const SMOKE_TIMEOUT: Duration = Duration::from_secs(10);
const EXPECTED_RUSTC_PREFIX: &str = "rustc 1.97.1 ";

/// Native targets shipped by one complete public release.
pub const RELEASE_TARGETS: [&str; 6] = [
    "aarch64-apple-darwin",
    "aarch64-pc-windows-msvc",
    "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "x86_64-unknown-linux-gnu",
];

/// One target-specific native executable and its build identity.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeArtifactRecord {
    pub arch: String,
    pub cargo_lock_sha256: String,
    pub filename: String,
    pub fingerprint: String,
    pub kind: String,
    pub os: String,
    pub profile: String,
    pub rustc_version: String,
    pub schema_version: String,
    pub sha256: String,
    pub size: u64,
    pub smoke_output: String,
    pub smoke_status: String,
    pub source_revision: String,
    pub target: String,
    pub version: String,
}

/// Complete target matrix consumed by release qualification.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeArtifactIndex {
    pub artifacts: Vec<NativeArtifactRecord>,
    pub fingerprint: String,
    pub product: String,
    pub schema_version: String,
    pub source_revision: String,
    pub target_set_sha256: String,
    pub version: String,
}

/// Errors raised while freezing or validating native release artifacts.
#[derive(Debug, Error)]
pub enum ReleaseError {
    #[error("native release contract is invalid: {0}")]
    Contract(String),
    #[error("native release artifact cannot be read or written: {0}")]
    Io(#[from] std::io::Error),
    #[error("native release JSON is invalid: {0}")]
    Json(#[from] serde_json::Error),
    #[error("native release canonical JSON failed: {0}")]
    Canonical(#[from] agent_contracts::ContractError),
    #[error("native release lifecycle candidate is invalid: {0}")]
    Lifecycle(#[from] agent_lifecycle::LifecycleError),
}

/// Freeze a target-specific executable and its canonical record into a new directory.
///
/// # Errors
/// Returns an error for unsupported targets, mismatched binary headers, unsafe
/// output paths, invalid build identities, or I/O failures.
pub fn record_native_artifact(
    binary: &Path,
    cargo_lock: &Path,
    target: &str,
    source_revision: &str,
    output: &Path,
) -> Result<NativeArtifactRecord, ReleaseError> {
    record_native_artifact_with_runner(
        binary,
        cargo_lock,
        target,
        source_revision,
        output,
        smoke_native_binary,
    )
}

fn record_native_artifact_with_runner<F>(
    binary: &Path,
    cargo_lock: &Path,
    target: &str,
    source_revision: &str,
    output: &Path,
    smoke_runner: F,
) -> Result<NativeArtifactRecord, ReleaseError>
where
    F: FnOnce(&Path) -> Result<String, ReleaseError>,
{
    let descriptor = target_descriptor(target)?;
    validate_source_revision(source_revision)?;
    let rustc_version = env!("AGENT_RELEASE_RUSTC_VERSION");
    validate_rustc_version(rustc_version)?;
    let binary_bytes = read_bounded(binary, MAX_BINARY_BYTES, "native executable")?;
    validate_binary_header(&binary_bytes, descriptor)?;
    let lock_bytes = read_bounded(cargo_lock, MAX_RECORD_BYTES, "Cargo.lock")?;
    let staging = create_private_staging_directory(output)?;
    let filename = native_filename(env!("CARGO_PKG_VERSION"), target);
    let artifact_path = staging.path().join(&filename);
    write_executable_new(&artifact_path, &binary_bytes)?;
    let smoke_output = smoke_runner(&artifact_path)?;
    let verified_binary =
        read_bounded(&artifact_path, MAX_BINARY_BYTES, "staged native executable")?;
    if verified_binary != binary_bytes {
        return Err(ReleaseError::Contract(
            "staged native executable changed while its smoke test was running".to_owned(),
        ));
    }
    let mut record = NativeArtifactRecord {
        arch: descriptor.arch.to_owned(),
        cargo_lock_sha256: sha256_bytes(&lock_bytes),
        filename,
        fingerprint: String::new(),
        kind: KIND.to_owned(),
        os: descriptor.os.to_owned(),
        profile: PROFILE.to_owned(),
        rustc_version: rustc_version.to_owned(),
        schema_version: SCHEMA_VERSION.to_owned(),
        sha256: sha256_bytes(&binary_bytes),
        size: u64::try_from(binary_bytes.len())
            .map_err(|_| ReleaseError::Contract("native executable size overflow".to_owned()))?,
        smoke_output,
        smoke_status: "passed".to_owned(),
        source_revision: source_revision.to_owned(),
        target: target.to_owned(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
    };
    record.fingerprint = record_fingerprint(&record)?;
    let record_path = staging.path().join("native-artifact-record.json");
    write_new(
        &record_path,
        &canonical_json(&serde_json::to_value(&record)?)?,
    )?;
    publish_staging_directory(staging.path(), output)?;
    Ok(record)
}

/// Merge six target records and executables into a canonical native release index.
///
/// # Errors
/// Returns an error if any record, executable, source identity, version, target
/// set, or fingerprint differs from the frozen contract.
pub fn merge_native_artifacts(
    record_paths: &[PathBuf],
    output: &Path,
) -> Result<NativeArtifactIndex, ReleaseError> {
    if record_paths.len() != RELEASE_TARGETS.len() {
        return Err(ReleaseError::Contract(format!(
            "native release requires exactly {} target records",
            RELEASE_TARGETS.len()
        )));
    }
    let mut loaded = Vec::with_capacity(record_paths.len());
    for path in record_paths {
        let raw = read_bounded(path, MAX_RECORD_BYTES, "native artifact record")?;
        let value = parse_json(&raw)?;
        if canonical_json(&value)? != raw {
            return Err(ReleaseError::Contract(format!(
                "native artifact record is not canonical JSON: {}",
                path.display()
            )));
        }
        let record: NativeArtifactRecord = serde_json::from_value(value)?;
        validate_record(&record)?;
        let parent = path.parent().ok_or_else(|| {
            ReleaseError::Contract("native artifact record has no parent directory".to_owned())
        })?;
        let binary = read_bounded(
            &parent.join(&record.filename),
            MAX_BINARY_BYTES,
            "native executable",
        )?;
        validate_binary_header(&binary, target_descriptor(&record.target)?)?;
        if record.size != binary.len() as u64 || record.sha256 != sha256_bytes(&binary) {
            return Err(ReleaseError::Contract(format!(
                "native executable differs from record: {}",
                record.filename
            )));
        }
        loaded.push((record, binary));
    }
    loaded.sort_by(|left, right| left.0.target.cmp(&right.0.target));
    let observed: Vec<&str> = loaded
        .iter()
        .map(|(record, _)| record.target.as_str())
        .collect();
    if observed != RELEASE_TARGETS {
        return Err(ReleaseError::Contract(
            "native release target matrix is incomplete or duplicated".to_owned(),
        ));
    }
    let source_revisions: BTreeSet<String> = loaded
        .iter()
        .map(|(record, _)| record.source_revision.clone())
        .collect();
    let versions: BTreeSet<String> = loaded
        .iter()
        .map(|(record, _)| record.version.clone())
        .collect();
    let cargo_locks: BTreeSet<String> = loaded
        .iter()
        .map(|(record, _)| record.cargo_lock_sha256.clone())
        .collect();
    if source_revisions.len() != 1 || versions.len() != 1 || cargo_locks.len() != 1 {
        return Err(ReleaseError::Contract(
            "native release records do not share one source, version, and Cargo.lock".to_owned(),
        ));
    }
    create_output_directory(output)?;
    for (record, binary) in &loaded {
        write_new(&output.join(&record.filename), binary)?;
    }
    let artifacts: Vec<NativeArtifactRecord> =
        loaded.into_iter().map(|(record, _)| record).collect();
    let targets = Value::Array(
        RELEASE_TARGETS
            .iter()
            .map(|target| Value::String((*target).to_owned()))
            .collect(),
    );
    let mut index = NativeArtifactIndex {
        artifacts,
        fingerprint: String::new(),
        product: PRODUCT.to_owned(),
        schema_version: SCHEMA_VERSION.to_owned(),
        source_revision: source_revisions
            .iter()
            .next()
            .ok_or_else(|| {
                ReleaseError::Contract("native release source identity is missing".to_owned())
            })?
            .clone(),
        target_set_sha256: canonical_sha256(&targets)?,
        version: versions
            .iter()
            .next()
            .ok_or_else(|| ReleaseError::Contract("native release version is missing".to_owned()))?
            .clone(),
    };
    index.fingerprint = index_fingerprint(&index)?;
    write_new(
        &output.join("native-artifacts.json"),
        &canonical_json(&serde_json::to_value(&index)?)?,
    )?;
    Ok(index)
}

/// Validate a canonical native artifact index and the adjacent executables.
///
/// # Errors
/// Returns an error if the index or any executable differs from the complete
/// matrix contract.
pub fn verify_native_artifacts(
    index_path: &Path,
    artifacts_dir: &Path,
) -> Result<NativeArtifactIndex, ReleaseError> {
    let raw = read_bounded(index_path, MAX_RECORD_BYTES, "native artifact index")?;
    let value = parse_json(&raw)?;
    if canonical_json(&value)? != raw {
        return Err(ReleaseError::Contract(
            "native artifact index is not canonical JSON".to_owned(),
        ));
    }
    let index: NativeArtifactIndex = serde_json::from_value(value)?;
    validate_index(&index)?;
    for record in &index.artifacts {
        let bytes = read_bounded(
            &artifacts_dir.join(&record.filename),
            MAX_BINARY_BYTES,
            "native executable",
        )?;
        validate_binary_header(&bytes, target_descriptor(&record.target)?)?;
        if record.size != bytes.len() as u64 || record.sha256 != sha256_bytes(&bytes) {
            return Err(ReleaseError::Contract(format!(
                "native executable differs from index: {}",
                record.filename
            )));
        }
    }
    Ok(index)
}

fn validate_index(index: &NativeArtifactIndex) -> Result<(), ReleaseError> {
    if index.schema_version != SCHEMA_VERSION
        || index.product != PRODUCT
        || index.version != env!("CARGO_PKG_VERSION")
    {
        return Err(ReleaseError::Contract(
            "native artifact index product, version, or schema is invalid".to_owned(),
        ));
    }
    validate_source_revision(&index.source_revision)?;
    if index.artifacts.len() != RELEASE_TARGETS.len() {
        return Err(ReleaseError::Contract(
            "native artifact index target count is invalid".to_owned(),
        ));
    }
    let mut targets = Vec::with_capacity(index.artifacts.len());
    for record in &index.artifacts {
        validate_record(record)?;
        if record.source_revision != index.source_revision || record.version != index.version {
            return Err(ReleaseError::Contract(
                "native artifact index and record identities differ".to_owned(),
            ));
        }
        targets.push(record.target.as_str());
    }
    if targets != RELEASE_TARGETS {
        return Err(ReleaseError::Contract(
            "native artifact index targets must be sorted and complete".to_owned(),
        ));
    }
    let target_value = Value::Array(
        RELEASE_TARGETS
            .iter()
            .map(|target| Value::String((*target).to_owned()))
            .collect(),
    );
    if index.target_set_sha256 != canonical_sha256(&target_value)?
        || index.fingerprint != index_fingerprint(index)?
    {
        return Err(ReleaseError::Contract(
            "native artifact index fingerprint is invalid".to_owned(),
        ));
    }
    Ok(())
}

fn validate_record(record: &NativeArtifactRecord) -> Result<(), ReleaseError> {
    let descriptor = target_descriptor(&record.target)?;
    validate_source_revision(&record.source_revision)?;
    validate_rustc_version(&record.rustc_version)?;
    let expected_smoke = format!("agent-skills-rs {}\n", env!("CARGO_PKG_VERSION"));
    if record.schema_version != SCHEMA_VERSION
        || record.kind != KIND
        || record.profile != PROFILE
        || record.version != env!("CARGO_PKG_VERSION")
        || record.os != descriptor.os
        || record.arch != descriptor.arch
        || record.filename != native_filename(&record.version, &record.target)
        || record.size == 0
        || record.size > MAX_BINARY_BYTES
        || record.smoke_status != "passed"
        || record.smoke_output != expected_smoke
        || !valid_sha256(&record.sha256)
        || !valid_sha256(&record.cargo_lock_sha256)
        || record.fingerprint != record_fingerprint(record)?
    {
        return Err(ReleaseError::Contract(format!(
            "native artifact record is invalid: {}",
            record.target
        )));
    }
    Ok(())
}

fn record_fingerprint(record: &NativeArtifactRecord) -> Result<String, ReleaseError> {
    let mut value = serde_json::to_value(record)?;
    value
        .as_object_mut()
        .ok_or_else(|| ReleaseError::Contract("native record root is invalid".to_owned()))?
        .remove("fingerprint");
    Ok(canonical_sha256(&value)?)
}

fn index_fingerprint(index: &NativeArtifactIndex) -> Result<String, ReleaseError> {
    let mut value = serde_json::to_value(index)?;
    value
        .as_object_mut()
        .ok_or_else(|| ReleaseError::Contract("native index root is invalid".to_owned()))?
        .remove("fingerprint");
    Ok(canonical_sha256(&value)?)
}

#[derive(Clone, Copy)]
struct TargetDescriptor {
    arch: &'static str,
    format: BinaryFormat,
    os: &'static str,
}

#[derive(Clone, Copy)]
enum BinaryFormat {
    Elf { machine: u16 },
    MachO { cpu_type: u32 },
    Pe { machine: u16 },
}

fn target_descriptor(target: &str) -> Result<TargetDescriptor, ReleaseError> {
    match target {
        "aarch64-apple-darwin" => Ok(TargetDescriptor {
            arch: "aarch64",
            format: BinaryFormat::MachO {
                cpu_type: 0x0100_000c,
            },
            os: "darwin",
        }),
        "x86_64-apple-darwin" => Ok(TargetDescriptor {
            arch: "x86_64",
            format: BinaryFormat::MachO {
                cpu_type: 0x0100_0007,
            },
            os: "darwin",
        }),
        "aarch64-unknown-linux-gnu" => Ok(TargetDescriptor {
            arch: "aarch64",
            format: BinaryFormat::Elf { machine: 183 },
            os: "linux",
        }),
        "x86_64-unknown-linux-gnu" => Ok(TargetDescriptor {
            arch: "x86_64",
            format: BinaryFormat::Elf { machine: 62 },
            os: "linux",
        }),
        "aarch64-pc-windows-msvc" => Ok(TargetDescriptor {
            arch: "aarch64",
            format: BinaryFormat::Pe { machine: 0xaa64 },
            os: "windows",
        }),
        "x86_64-pc-windows-msvc" => Ok(TargetDescriptor {
            arch: "x86_64",
            format: BinaryFormat::Pe { machine: 0x8664 },
            os: "windows",
        }),
        _ => Err(ReleaseError::Contract(format!(
            "unsupported native release target: {target}"
        ))),
    }
}

fn validate_binary_header(bytes: &[u8], descriptor: TargetDescriptor) -> Result<(), ReleaseError> {
    let matches = match descriptor.format {
        BinaryFormat::Elf { machine } => {
            bytes.len() >= 20
                && bytes[..4] == *b"\x7fELF"
                && bytes[4] == 2
                && bytes[5] == 1
                && u16::from_le_bytes([bytes[18], bytes[19]]) == machine
        }
        BinaryFormat::MachO { cpu_type } => {
            bytes.len() >= 8
                && bytes[..4] == [0xcf, 0xfa, 0xed, 0xfe]
                && u32::from_le_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]) == cpu_type
        }
        BinaryFormat::Pe { machine } => {
            if bytes.len() < 64 || bytes[..2] != *b"MZ" {
                false
            } else {
                let offset =
                    u32::from_le_bytes([bytes[60], bytes[61], bytes[62], bytes[63]]) as usize;
                offset.checked_add(6).is_some_and(|end| end <= bytes.len())
                    && bytes[offset..offset + 4] == *b"PE\0\0"
                    && u16::from_le_bytes([bytes[offset + 4], bytes[offset + 5]]) == machine
            }
        }
    };
    if !matches {
        return Err(ReleaseError::Contract(
            "native executable header differs from its declared target".to_owned(),
        ));
    }
    Ok(())
}

fn read_bounded(path: &Path, maximum: u64, label: &str) -> Result<Vec<u8>, ReleaseError> {
    if path.is_symlink() {
        return Err(ReleaseError::Contract(format!(
            "{label} must not be a symlink"
        )));
    }
    let mut file = open_read_nofollow(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > maximum
    {
        return Err(ReleaseError::Contract(format!(
            "{label} is missing, empty, or exceeds its size limit"
        )));
    }
    let capacity = usize::try_from(metadata.len())
        .map_err(|_| ReleaseError::Contract(format!("{label} size cannot be represented")))?;
    let mut bytes = Vec::with_capacity(capacity);
    Read::by_ref(&mut file)
        .take(maximum + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != metadata.len() || bytes.len() as u64 > maximum {
        return Err(ReleaseError::Contract(format!(
            "{label} changed while it was being read"
        )));
    }
    Ok(bytes)
}

fn open_read_nofollow(path: &Path) -> Result<File, std::io::Error> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt as _;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    options.open(path)
}

fn create_output_directory(output: &Path) -> Result<(), ReleaseError> {
    if output.is_symlink() || output.exists() {
        return Err(ReleaseError::Contract(
            "native release output must not already exist".to_owned(),
        ));
    }
    std::fs::create_dir(output)?;
    Ok(())
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), ReleaseError> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

fn write_executable_new(path: &Path, bytes: &[u8]) -> Result<(), ReleaseError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.mode(0o700);
    }
    let mut file = options.open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

fn create_private_staging_directory(output: &Path) -> Result<tempfile::TempDir, ReleaseError> {
    if output.is_symlink() || output.exists() {
        return Err(ReleaseError::Contract(
            "native release output must not already exist".to_owned(),
        ));
    }
    let parent = output.parent().ok_or_else(|| {
        ReleaseError::Contract("native release output has no parent directory".to_owned())
    })?;
    if parent.is_symlink() || !parent.is_dir() {
        return Err(ReleaseError::Contract(
            "native release output parent must be an existing directory".to_owned(),
        ));
    }
    tempfile::Builder::new()
        .prefix(".agent-release-stage-")
        .tempdir_in(parent)
        .map_err(ReleaseError::Io)
}

#[cfg(any(target_vendor = "apple", target_os = "linux"))]
fn publish_staging_directory(source: &Path, destination: &Path) -> Result<(), ReleaseError> {
    rustix::fs::renameat_with(
        rustix::fs::CWD,
        source,
        rustix::fs::CWD,
        destination,
        rustix::fs::RenameFlags::NOREPLACE,
    )
    .map_err(std::io::Error::from)?;
    Ok(())
}

#[cfg(windows)]
fn publish_staging_directory(source: &Path, destination: &Path) -> Result<(), ReleaseError> {
    renamore::rename_exclusive(source, destination)?;
    Ok(())
}

#[cfg(not(any(target_vendor = "apple", target_os = "linux", windows)))]
fn publish_staging_directory(_source: &Path, _destination: &Path) -> Result<(), ReleaseError> {
    Err(ReleaseError::Contract(
        "atomic no-replace native release publication is unsupported on this platform".to_owned(),
    ))
}

fn native_filename(version: &str, target: &str) -> String {
    let suffix = if target.contains("-windows-") {
        ".exe"
    } else {
        ""
    };
    format!("agent-skills-{version}-{target}{suffix}")
}

fn validate_source_revision(value: &str) -> Result<(), ReleaseError> {
    if value.len() != 40
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(ReleaseError::Contract(
            "native release source revision must be a full lowercase Git commit".to_owned(),
        ));
    }
    Ok(())
}

fn smoke_native_binary(binary: &Path) -> Result<String, ReleaseError> {
    let mut child = Command::new(binary)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| {
            ReleaseError::Contract(format!(
                "native executable smoke test cannot start: {error}"
            ))
        })?;
    let stdout = child.stdout.take().ok_or_else(|| {
        ReleaseError::Contract("native executable smoke stdout is unavailable".to_owned())
    })?;
    let stderr = child.stderr.take().ok_or_else(|| {
        ReleaseError::Contract("native executable smoke stderr is unavailable".to_owned())
    })?;
    let stdout_worker = std::thread::spawn(move || {
        let mut value = Vec::new();
        stdout
            .take(MAX_SMOKE_OUTPUT_BYTES + 1)
            .read_to_end(&mut value)
            .map(|_| value)
    });
    let stderr_worker = std::thread::spawn(move || {
        let mut value = Vec::new();
        stderr
            .take(MAX_SMOKE_OUTPUT_BYTES + 1)
            .read_to_end(&mut value)
            .map(|_| value)
    });
    let deadline = Instant::now() + SMOKE_TIMEOUT;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = stdout_worker.join();
            let _ = stderr_worker.join();
            return Err(ReleaseError::Contract(
                "native executable smoke test timed out".to_owned(),
            ));
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    let stdout = stdout_worker
        .join()
        .map_err(|_| ReleaseError::Contract("native smoke stdout worker panicked".to_owned()))??;
    let stderr = stderr_worker
        .join()
        .map_err(|_| ReleaseError::Contract("native smoke stderr worker panicked".to_owned()))??;
    let expected = format!("agent-skills-rs {}\n", env!("CARGO_PKG_VERSION"));
    if !status.success()
        || stdout.len() as u64 > MAX_SMOKE_OUTPUT_BYTES
        || stderr.len() as u64 > MAX_SMOKE_OUTPUT_BYTES
        || !stderr.is_empty()
        || stdout != expected.as_bytes()
    {
        return Err(ReleaseError::Contract(
            "native executable smoke result differs from its package contract".to_owned(),
        ));
    }
    String::from_utf8(stdout)
        .map_err(|_| ReleaseError::Contract("native smoke output is not UTF-8".to_owned()))
}

fn validate_rustc_version(value: &str) -> Result<(), ReleaseError> {
    if !value.starts_with(EXPECTED_RUSTC_PREFIX) || value.contains(['\r', '\n']) {
        return Err(ReleaseError::Contract(
            "native release rustc version differs from the pinned toolchain".to_owned(),
        ));
    }
    Ok(())
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn sha256_bytes(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_root(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "agent-release-{label}-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir(&path).expect("create temporary root");
        path
    }

    fn fake_binary(target: &str) -> Vec<u8> {
        let descriptor = target_descriptor(target).expect("target descriptor");
        match descriptor.format {
            BinaryFormat::Elf { machine } => {
                let mut bytes = vec![0_u8; 64];
                bytes[..4].copy_from_slice(b"\x7fELF");
                bytes[4] = 2;
                bytes[5] = 1;
                bytes[18..20].copy_from_slice(&machine.to_le_bytes());
                bytes
            }
            BinaryFormat::MachO { cpu_type } => {
                let mut bytes = vec![0_u8; 32];
                bytes[..4].copy_from_slice(&[0xcf, 0xfa, 0xed, 0xfe]);
                bytes[4..8].copy_from_slice(&cpu_type.to_le_bytes());
                bytes
            }
            BinaryFormat::Pe { machine } => {
                let mut bytes = vec![0_u8; 128];
                bytes[..2].copy_from_slice(b"MZ");
                bytes[60..64].copy_from_slice(&64_u32.to_le_bytes());
                bytes[64..68].copy_from_slice(b"PE\0\0");
                bytes[68..70].copy_from_slice(&machine.to_le_bytes());
                bytes
            }
        }
    }

    fn write_record_fixture(root: &Path, target: &str) -> PathBuf {
        let directory = root.join(target);
        std::fs::create_dir(&directory).expect("create target fixture");
        let filename = native_filename(env!("CARGO_PKG_VERSION"), target);
        let binary = fake_binary(target);
        std::fs::write(directory.join(&filename), &binary).expect("write binary");
        let descriptor = target_descriptor(target).expect("target descriptor");
        let mut record = NativeArtifactRecord {
            arch: descriptor.arch.to_owned(),
            cargo_lock_sha256: "1".repeat(64),
            filename,
            fingerprint: String::new(),
            kind: KIND.to_owned(),
            os: descriptor.os.to_owned(),
            profile: PROFILE.to_owned(),
            rustc_version: "rustc 1.97.1 (fixture 2026-01-01)".to_owned(),
            schema_version: SCHEMA_VERSION.to_owned(),
            sha256: sha256_bytes(&binary),
            size: binary.len() as u64,
            smoke_output: format!("agent-skills-rs {}\n", env!("CARGO_PKG_VERSION")),
            smoke_status: "passed".to_owned(),
            source_revision: "1".repeat(40),
            target: target.to_owned(),
            version: env!("CARGO_PKG_VERSION").to_owned(),
        };
        record.fingerprint = record_fingerprint(&record).expect("record fingerprint");
        let path = directory.join("native-artifact-record.json");
        std::fs::write(
            &path,
            canonical_json(&serde_json::to_value(record).expect("record value"))
                .expect("canonical record"),
        )
        .expect("write record");
        path
    }

    #[test]
    fn complete_matrix_merges_and_verifies() {
        let root = temporary_root("merge");
        let records: Vec<PathBuf> = RELEASE_TARGETS
            .iter()
            .map(|target| write_record_fixture(&root, target))
            .collect();
        let output = root.join("output");
        let index = merge_native_artifacts(&records, &output).expect("merge matrix");
        assert_eq!(index.artifacts.len(), RELEASE_TARGETS.len());
        assert_eq!(
            verify_native_artifacts(&output.join("native-artifacts.json"), &output)
                .expect("verify matrix"),
            index
        );
        std::fs::remove_dir_all(root).expect("remove fixture");
    }

    #[test]
    fn duplicate_target_and_header_mismatch_fail_closed() {
        let root = temporary_root("negative");
        let mut records: Vec<PathBuf> = RELEASE_TARGETS
            .iter()
            .map(|target| write_record_fixture(&root, target))
            .collect();
        records[1] = records[0].clone();
        let error = merge_native_artifacts(&records, &root.join("duplicate"))
            .expect_err("duplicate target must fail");
        assert!(error.to_string().contains("incomplete or duplicated"));

        let binary = root.join(RELEASE_TARGETS[0]).join(native_filename(
            env!("CARGO_PKG_VERSION"),
            RELEASE_TARGETS[0],
        ));
        std::fs::write(&binary, fake_binary(RELEASE_TARGETS[5])).expect("replace binary");
        let error = merge_native_artifacts(
            &RELEASE_TARGETS
                .iter()
                .map(|target| root.join(target).join("native-artifact-record.json"))
                .collect::<Vec<_>>(),
            &root.join("mismatch"),
        )
        .expect_err("header mismatch must fail");
        assert!(error.to_string().contains("header"));
        std::fs::remove_dir_all(root).expect("remove fixture");
    }

    #[test]
    fn source_path_aba_cannot_change_staged_smoke_identity() {
        let root = temporary_root("source-aba");
        let source = root.join("source-binary");
        let original = fake_binary("aarch64-apple-darwin");
        let replacement = fake_binary("x86_64-apple-darwin");
        std::fs::write(&source, &original).expect("write original source");
        let cargo_lock = root.join("Cargo.lock");
        std::fs::write(&cargo_lock, b"fixture lock").expect("write Cargo.lock");
        let output = root.join("output");

        let record = record_native_artifact_with_runner(
            &source,
            &cargo_lock,
            "aarch64-apple-darwin",
            &"1".repeat(40),
            &output,
            |staged| {
                assert_ne!(staged, source);
                std::fs::write(&source, &replacement).expect("replace source before smoke");
                assert_eq!(
                    read_bounded(staged, MAX_BINARY_BYTES, "staged fixture")
                        .expect("read staged fixture"),
                    original
                );
                std::fs::write(&source, &original).expect("restore source after smoke");
                Ok(format!("agent-skills-rs {}\n", env!("CARGO_PKG_VERSION")))
            },
        )
        .expect("record frozen artifact");

        assert_eq!(
            std::fs::read(output.join(&record.filename)).expect("read published artifact"),
            original
        );
        assert_eq!(record.sha256, sha256_bytes(&original));
        assert!(!root.read_dir().expect("read fixture root").any(|entry| {
            entry
                .expect("read fixture entry")
                .file_name()
                .to_string_lossy()
                .starts_with(".agent-release-stage-")
        }));
        std::fs::remove_dir_all(root).expect("remove fixture");
    }
}
