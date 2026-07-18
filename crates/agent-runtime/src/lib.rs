//! Native workflow runtime compatibility primitives.
//!
//! The crate executes deterministic fake adapter outcomes, consumes validated
//! recorded Adapter Results, and owns native Worktree/Session identity and
//! Registry primitives. External provider invocation, Worktree creation, and
//! Final Gate execution remain outside the native boundary while Phase 4 is
//! migrated incrementally.

mod adapters;
mod git_workspace;
mod session_registry;
mod sessions;

pub use adapters::{build_adapter_request, validate_adapter_request, validate_adapter_result};
pub use git_workspace::{
    inspect_repository, repository_patch, resolve_commit, resolve_worktree,
    session_source_identity, worktree_status,
};
pub use session_registry::{
    registry_create, registry_list, registry_load, registry_transition, registry_write,
};
pub use sessions::{
    freeze_checkpoint, new_session_context, refresh_session_source_identity,
    transition_session_context, validate_worktree_session_context,
};

use agent_contracts::{canonical_json, canonical_sha256, parse_json};
use agent_engine::validate_plan_package_lock;
use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions as CapOpenOptions};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{Read as _, Seek as _, SeekFrom, Write as _};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

const MAX_LEDGER_BYTES: usize = 64 * 1024 * 1024;
const MAX_LEDGER_EVENTS: usize = 100_000;
const MAX_RUNTIME_NODES: usize = 16_384;
const MAX_RUNTIME_EDGES: usize = 65_536;

/// Native runtime failures.
#[derive(Debug, Error)]
pub enum RuntimeError {
    #[error("{0}")]
    Contract(String),
    #[error("runtime I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("runtime JSON failed: {0}")]
    Json(#[from] serde_json::Error),
    #[error("runtime contract failed: {0}")]
    SharedContract(#[from] agent_contracts::ContractError),
    #[error("runtime engine contract failed: {0}")]
    Engine(#[from] agent_engine::EngineError),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NodeStatus {
    Pending,
    Ready,
    Running,
    Passed,
    Failed,
    Blocked,
    Skipped,
    Cancelled,
    Stale,
}

impl NodeStatus {
    fn parse(value: &str) -> Result<Self, RuntimeError> {
        match value {
            "pending" => Ok(Self::Pending),
            "ready" => Ok(Self::Ready),
            "running" => Ok(Self::Running),
            "passed" => Ok(Self::Passed),
            "failed" => Ok(Self::Failed),
            "blocked" => Ok(Self::Blocked),
            "skipped" => Ok(Self::Skipped),
            "cancelled" => Ok(Self::Cancelled),
            "stale" => Ok(Self::Stale),
            _ => Err(RuntimeError::Contract(
                "node-attempt status is invalid".to_owned(),
            )),
        }
    }

    const fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Ready => "ready",
            Self::Running => "running",
            Self::Passed => "passed",
            Self::Failed => "failed",
            Self::Blocked => "blocked",
            Self::Skipped => "skipped",
            Self::Cancelled => "cancelled",
            Self::Stale => "stale",
        }
    }
}

fn transition_allowed(current: NodeStatus, target: NodeStatus) -> bool {
    match current {
        NodeStatus::Pending => matches!(
            target,
            NodeStatus::Ready | NodeStatus::Blocked | NodeStatus::Skipped | NodeStatus::Cancelled
        ),
        NodeStatus::Ready => matches!(
            target,
            NodeStatus::Running | NodeStatus::Blocked | NodeStatus::Cancelled | NodeStatus::Stale
        ),
        NodeStatus::Running => matches!(
            target,
            NodeStatus::Passed
                | NodeStatus::Failed
                | NodeStatus::Blocked
                | NodeStatus::Skipped
                | NodeStatus::Cancelled
        ),
        NodeStatus::Passed | NodeStatus::Failed | NodeStatus::Skipped | NodeStatus::Cancelled => {
            target == NodeStatus::Stale
        }
        NodeStatus::Blocked => matches!(target, NodeStatus::Ready | NodeStatus::Stale),
        NodeStatus::Stale => target == NodeStatus::Ready,
    }
}

#[derive(Debug)]
struct IdentityClock {
    seed: String,
    sequence: AtomicU64,
    epoch_micros: u64,
}

impl IdentityClock {
    fn new(seed: Option<&str>) -> Result<Self, RuntimeError> {
        let duration = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| RuntimeError::Contract("system clock precedes Unix epoch".to_owned()))?;
        let epoch_micros = u64::try_from(duration.as_micros()).unwrap_or(u64::MAX);
        let seed = if let Some(seed) = seed {
            if seed.len() != 16
                || !seed
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
            {
                return Err(RuntimeError::Contract(
                    "runtime identity seed must be 16 lowercase hexadecimal characters".to_owned(),
                ));
            }
            seed.to_owned()
        } else {
            canonical_sha256(&json!({
                "epoch_micros": epoch_micros,
                "process_id": std::process::id(),
            }))?[..16]
                .to_owned()
        };
        Ok(Self {
            seed,
            sequence: AtomicU64::new(0),
            epoch_micros,
        })
    }

    fn identifier(&self, prefix: &str) -> String {
        let sequence = self.sequence.fetch_add(1, Ordering::Relaxed);
        format!("{prefix}-{}-{sequence:016x}", self.seed)
    }

    fn timestamp(&self) -> String {
        let offset = self.sequence.fetch_add(1, Ordering::Relaxed);
        format_timestamp(self.epoch_micros.saturating_add(offset))
    }
}

/// Attempt lifecycle implementation shared by the native executor.
#[derive(Debug)]
pub struct NodeStateMachine {
    clock: IdentityClock,
}

impl NodeStateMachine {
    /// Create a state machine with an optional deterministic identity seed.
    ///
    /// # Errors
    /// Returns an error when the supplied seed is not 16 lowercase
    /// hexadecimal characters or the system clock is unavailable.
    pub fn new(identity_seed: Option<&str>) -> Result<Self, RuntimeError> {
        Ok(Self {
            clock: IdentityClock::new(identity_seed)?,
        })
    }

    fn new_attempt(
        &self,
        node_id: &str,
        attempt_number: u64,
        max_retries: u64,
        timeout_seconds: u64,
    ) -> Result<Value, RuntimeError> {
        if node_id.is_empty() || attempt_number < 1 || timeout_seconds == 0 {
            return Err(RuntimeError::Contract(
                "invalid attempt retry or timeout metadata".to_owned(),
            ));
        }
        let now_micros = self
            .clock
            .epoch_micros
            .saturating_add(self.clock.sequence.fetch_add(1, Ordering::Relaxed));
        let now = format_timestamp(now_micros);
        let deadline =
            format_timestamp(now_micros.saturating_add(timeout_seconds.saturating_mul(1_000_000)));
        Ok(json!({
            "attempt_id": self.clock.identifier("attempt"),
            "attempt_number": attempt_number,
            "deadline": deadline,
            "events": [{
                "at": now,
                "from": Value::Null,
                "reason": "created",
                "to": "pending",
            }],
            "max_retries": max_retries,
            "node_id": node_id,
            "schema_version": "1.0",
            "status": "pending",
            "timeout_seconds": timeout_seconds,
        }))
    }

    fn transition(
        &self,
        attempt: &mut Value,
        target: NodeStatus,
        reason: &str,
    ) -> Result<(), RuntimeError> {
        let object = object_mut(attempt, "node-attempt")?;
        let current = NodeStatus::parse(required_str(object, "status", "node-attempt")?)?;
        if !transition_allowed(current, target) {
            return Err(RuntimeError::Contract(format!(
                "illegal node transition: {} -> {}",
                current.as_str(),
                target.as_str()
            )));
        }
        array_mut(object, "events", "node-attempt")?.push(json!({
            "at": self.clock.timestamp(),
            "from": current.as_str(),
            "reason": reason,
            "to": target.as_str(),
        }));
        object.insert(
            "status".to_owned(),
            Value::String(target.as_str().to_owned()),
        );
        Ok(())
    }

    const fn can_auto_retry(idempotent: bool, attempt_count: u64, max_retries: u64) -> bool {
        idempotent && attempt_count <= max_retries
    }
}

#[derive(Debug, Default)]
struct ResourceScheduler {
    owners: BTreeMap<String, String>,
    events: Vec<Value>,
    sequence: u64,
}

impl ResourceScheduler {
    fn acquire(&mut self, attempt_id: &str, resource_keys: &[String]) -> bool {
        let keys = resource_keys.iter().cloned().collect::<BTreeSet<_>>();
        for key in &keys {
            self.event(attempt_id, key, "requested");
        }
        if keys.iter().any(|key| {
            self.owners
                .get(key)
                .is_some_and(|owner| owner != attempt_id)
        }) {
            return false;
        }
        for key in keys {
            self.owners.insert(key.clone(), attempt_id.to_owned());
            self.event(attempt_id, &key, "acquired");
        }
        true
    }

    fn release(&mut self, attempt_id: &str, action: &str) -> Result<(), RuntimeError> {
        if !matches!(action, "released" | "timed-out" | "cancelled") {
            return Err(RuntimeError::Contract(format!(
                "invalid resource release action: {action}"
            )));
        }
        let keys = self
            .owners
            .iter()
            .filter_map(|(key, owner)| (owner == attempt_id).then_some(key.clone()))
            .collect::<Vec<_>>();
        for key in keys {
            self.owners.remove(&key);
            self.event(attempt_id, &key, action);
        }
        Ok(())
    }

    fn seed_sequence(&mut self, next_sequence: u64) -> Result<(), RuntimeError> {
        if !self.events.is_empty() || next_sequence < self.sequence {
            return Err(RuntimeError::Contract(
                "resource sequence can only be seeded before scheduling".to_owned(),
            ));
        }
        self.sequence = next_sequence;
        Ok(())
    }

    fn event(&mut self, attempt_id: &str, resource_key: &str, action: &str) {
        self.events.push(json!({
            "action": action,
            "attempt_id": attempt_id,
            "resource_key": resource_key,
            "schema_version": "1.0",
            "sequence": self.sequence,
        }));
        self.sequence = self.sequence.saturating_add(1);
    }
}

#[derive(Debug, Default)]
struct ApprovalGate;

impl ApprovalGate {
    fn request(
        attempt_id: &str,
        action: &str,
        reason: &str,
        scope: &Value,
    ) -> Result<Value, RuntimeError> {
        Ok(json!({
            "action": action,
            "attempt_id": attempt_id,
            "reason": reason,
            "schema_version": "1.0",
            "scope": scope,
            "scope_hash": canonical_sha256(scope)?,
            "status": "pending",
        }))
    }

    fn decide(record: &mut Value, status: &str, scope: &Value) -> Result<(), RuntimeError> {
        if !matches!(status, "granted" | "denied" | "expired") {
            return Err(RuntimeError::Contract(format!(
                "invalid approval status: {status}"
            )));
        }
        let object = object_mut(record, "approval-record")?;
        if canonical_sha256(scope)? != required_str(object, "scope_hash", "approval-record")? {
            return Err(RuntimeError::Contract(
                "approval scope cannot be expanded or changed".to_owned(),
            ));
        }
        if required_str(object, "status", "approval-record")? != "pending" {
            return Err(RuntimeError::Contract(
                "approval has already been decided".to_owned(),
            ));
        }
        object.insert("status".to_owned(), Value::String(status.to_owned()));
        Ok(())
    }
}

#[derive(Debug)]
struct RunLedger {
    event_count: usize,
    file: Option<std::fs::File>,
    value: Value,
}

impl RunLedger {
    fn new(
        plan_fingerprint: &str,
        package_lock_hash: &str,
        file: Option<std::fs::File>,
        run_id: &str,
    ) -> Self {
        Self {
            event_count: 0,
            file,
            value: json!({
                "adapter_outcomes": [],
                "approval_records": [],
                "artifact_hashes": [],
                "evidence": [],
                "final_status": "active",
                "node_attempts": [],
                "package_lock_hash": package_lock_hash,
                "plan_fingerprint": plan_fingerprint,
                "resolved_policy_hash": "",
                "resource_events": [],
                "run_id": run_id,
                "schema_version": "1.0",
            }),
        }
    }

    #[allow(clippy::too_many_lines)]
    fn append(&mut self, event_type: &str, value: Value) -> Result<(), RuntimeError> {
        if !matches!(
            event_type,
            "adapter-evidence"
                | "adapter-outcome"
                | "approval-record"
                | "artifact-hash"
                | "node-attempt"
                | "resource-event"
                | "run-blocked"
                | "run-finalized"
                | "run-resumed"
                | "run-started"
        ) {
            return Err(RuntimeError::Contract(format!(
                "unknown ledger event type: {event_type}"
            )));
        }
        if self.event_count >= MAX_LEDGER_EVENTS {
            return Err(RuntimeError::Contract(format!(
                "runtime ledger has more than {MAX_LEDGER_EVENTS} events"
            )));
        }
        let run_id =
            required_str(object(&self.value, "run-ledger")?, "run_id", "run-ledger")?.to_owned();
        if let Some(file) = &mut self.file {
            let event = json!({
                "event_type": event_type,
                "run_id": run_id,
                "value": value,
            });
            let encoded = canonical_json(&event)?;
            let metadata = file.metadata()?;
            let max_bytes = u64::try_from(MAX_LEDGER_BYTES).unwrap_or(u64::MAX);
            if metadata
                .len()
                .saturating_add(u64::try_from(encoded.len()).unwrap_or(u64::MAX))
                > max_bytes
            {
                return Err(RuntimeError::Contract(format!(
                    "runtime ledger has more than {MAX_LEDGER_BYTES} bytes"
                )));
            }
            file.seek(SeekFrom::End(0))?;
            file.write_all(&encoded)?;
            file.sync_data()?;
        }
        let ledger = object_mut(&mut self.value, "run-ledger")?;
        match event_type {
            "node-attempt" => {
                let attempt_id = required_str(
                    object(&value, "node-attempt")?,
                    "attempt_id",
                    "node-attempt",
                )?;
                let attempts = array_mut(ledger, "node_attempts", "run-ledger")?;
                if let Some(index) = attempts.iter().position(|attempt| {
                    object(attempt, "node-attempt")
                        .ok()
                        .and_then(|item| item.get("attempt_id"))
                        .and_then(Value::as_str)
                        == Some(attempt_id)
                }) {
                    attempts[index] = value;
                } else {
                    attempts.push(value);
                }
            }
            "resource-event" => {
                array_mut(ledger, "resource_events", "run-ledger")?.push(value);
            }
            "approval-record" => {
                array_mut(ledger, "approval_records", "run-ledger")?.push(value);
            }
            "artifact-hash" => {
                array_mut(ledger, "artifact_hashes", "run-ledger")?.push(value);
            }
            "adapter-evidence" => {
                array_mut(ledger, "evidence", "run-ledger")?.push(value);
            }
            "adapter-outcome" => {
                array_mut(ledger, "adapter_outcomes", "run-ledger")?.push(value);
            }
            "run-finalized" => {
                let status =
                    required_str(object(&value, "run-finalized")?, "status", "run-finalized")?;
                ledger.insert("final_status".to_owned(), Value::String(status.to_owned()));
            }
            "run-started" => {
                if required_str(
                    object(&value, "run-started")?,
                    "plan_fingerprint",
                    "run-started",
                )? != required_str(ledger, "plan_fingerprint", "run-ledger")?
                {
                    return Err(RuntimeError::Contract(
                        "run-started fingerprint does not match ledger".to_owned(),
                    ));
                }
                if contract_optional_ledger_hash(
                    object(&value, "run-started")?,
                    "package_lock_hash",
                    "run-started package_lock_hash",
                )?
                .unwrap_or("")
                    != required_str(ledger, "package_lock_hash", "run-ledger")?
                {
                    return Err(RuntimeError::Contract(
                        "run-started package lock does not match ledger".to_owned(),
                    ));
                }
            }
            "run-resumed" => {
                let resumed = object(&value, "run-resumed")?;
                if contract_optional_ledger_hash(
                    resumed,
                    "package_lock_hash",
                    "run-resumed package_lock_hash",
                )?
                .unwrap_or("")
                    != required_str(ledger, "package_lock_hash", "run-ledger")?
                {
                    return Err(RuntimeError::Contract(
                        "run-resumed package lock does not match ledger".to_owned(),
                    ));
                }
                if required_str(resumed, "plan_fingerprint", "run-resumed")?
                    != required_str(ledger, "plan_fingerprint", "run-ledger")?
                {
                    return Err(RuntimeError::Contract(
                        "run-resumed fingerprint does not match ledger".to_owned(),
                    ));
                }
                ledger.insert(
                    "final_status".to_owned(),
                    Value::String("active".to_owned()),
                );
            }
            "run-blocked" => {}
            _ => unreachable!(),
        }
        self.event_count += 1;
        Ok(())
    }

    fn finalize(mut self, status: &str) -> Result<Value, RuntimeError> {
        self.append("run-finalized", json!({"status": status}))?;
        validate_fake_run_ledger(&self.value)?;
        Ok(self.value)
    }

    #[allow(clippy::too_many_lines)]
    fn replay(mut file: std::fs::File, plan_fingerprint: &str) -> Result<Self, RuntimeError> {
        let metadata = file.metadata()?;
        let max_bytes = u64::try_from(MAX_LEDGER_BYTES).unwrap_or(u64::MAX);
        if metadata.len() > max_bytes {
            return Err(RuntimeError::Contract(format!(
                "runtime ledger has more than {MAX_LEDGER_BYTES} bytes"
            )));
        }
        file.seek(SeekFrom::Start(0))?;
        let mut bytes = Vec::with_capacity(
            usize::try_from(metadata.len())
                .unwrap_or(MAX_LEDGER_BYTES)
                .min(MAX_LEDGER_BYTES),
        );
        std::io::Read::by_ref(&mut file)
            .take(max_bytes.saturating_add(1))
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_LEDGER_BYTES {
            return Err(RuntimeError::Contract(format!(
                "runtime ledger has more than {MAX_LEDGER_BYTES} bytes"
            )));
        }
        let mut events = Vec::new();
        if !bytes.is_empty() {
            let mut lines = bytes.split(|byte| *byte == b'\n').peekable();
            while let Some(line) = lines.next() {
                if line.is_empty() && lines.peek().is_none() && bytes.ends_with(b"\n") {
                    break;
                }
                if events.len() >= MAX_LEDGER_EVENTS {
                    return Err(RuntimeError::Contract(format!(
                        "runtime ledger has more than {MAX_LEDGER_EVENTS} events"
                    )));
                }
                events.push(parse_json(line)?);
            }
        }
        if events.is_empty()
            || events
                .first()
                .and_then(Value::as_object)
                .and_then(|event| event.get("event_type"))
                .and_then(Value::as_str)
                != Some("run-started")
            || events
                .iter()
                .filter(|event| {
                    event
                        .as_object()
                        .and_then(|item| item.get("event_type"))
                        .and_then(Value::as_str)
                        == Some("run-started")
                })
                .count()
                != 1
        {
            return Err(RuntimeError::Contract(
                "runtime ledger must begin with exactly one run-started event".to_owned(),
            ));
        }
        let run_id = events
            .first()
            .and_then(Value::as_object)
            .and_then(|event| event.get("run_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let started = events.first().ok_or_else(|| {
            RuntimeError::Contract("runtime ledger requires run-started".to_owned())
        })?;
        let started_value = object(
            required_value(object(started, "ledger-event")?, "value", "ledger-event")?,
            "run-started",
        )?;
        if required_str(started_value, "plan_fingerprint", "run-started")? != plan_fingerprint {
            return Err(RuntimeError::Contract(
                "cannot resume ledger with a different plan fingerprint".to_owned(),
            ));
        }
        let package_lock_hash = contract_optional_ledger_hash(
            started_value,
            "package_lock_hash",
            "run-started package_lock_hash",
        )?
        .unwrap_or("");
        let mut ledger = Self::new(
            plan_fingerprint,
            package_lock_hash,
            None,
            run_id.as_deref().unwrap_or_default(),
        );
        for event in events {
            let event = object(&event, "ledger-event")?;
            if required_str(event, "run_id", "ledger-event")?
                != required_str(object(&ledger.value, "run-ledger")?, "run_id", "run-ledger")?
            {
                return Err(RuntimeError::Contract(
                    "ledger contains multiple run ids".to_owned(),
                ));
            }
            let event_type = required_str(event, "event_type", "ledger-event")?.to_owned();
            let value = required_value(event, "value", "ledger-event")?.clone();
            ledger.append(&event_type, value)?;
        }
        validate_fake_run_ledger(&ledger.value)?;
        file.seek(SeekFrom::End(0))?;
        ledger.file = Some(file);
        Ok(ledger)
    }
}

/// Execute the native deterministic fake-adapter runtime.
///
/// `behaviors` maps capability IDs to either one terminal behavior string or
/// a sequence consumed across retries. `approval_decisions` maps capability
/// IDs to `granted`, `denied`, or `expired`.
///
/// # Errors
/// Returns an error for invalid plans, lock mismatches, malformed runtime
/// controls, unsafe ledger replay, or an invalid state transition.
#[allow(clippy::too_many_arguments)]
pub fn execute_fake_plan(
    plan: &Value,
    behaviors: Option<&Value>,
    approval_decisions: Option<&Value>,
    package_lock: Option<&Value>,
    ledger_path: Option<&Path>,
    resume: bool,
    identity_seed: Option<&str>,
) -> Result<Value, RuntimeError> {
    execute_plan(
        plan,
        behaviors,
        None,
        None,
        approval_decisions,
        package_lock,
        ledger_path,
        resume,
        identity_seed,
    )
}

/// Consume recorded Adapter Result v1 objects through the native runtime.
///
/// This function validates and records externally produced results but never
/// invokes a Provider, Skill, command, or package code.
///
/// # Errors
/// Returns an error for invalid Adapter contracts, reused invocation identity,
/// unsafe ledger replay, lock mismatch, or an invalid runtime transition.
#[allow(clippy::too_many_arguments)]
pub fn execute_recorded_plan(
    plan: &Value,
    results: &Value,
    context: &Value,
    package_lock: Option<&Value>,
    ledger_path: Option<&Path>,
    resume: bool,
    identity_seed: Option<&str>,
) -> Result<Value, RuntimeError> {
    execute_plan(
        plan,
        None,
        Some(results),
        Some(context),
        None,
        package_lock,
        ledger_path,
        resume,
        identity_seed,
    )
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)]
fn execute_plan(
    plan: &Value,
    behaviors: Option<&Value>,
    recorded_results: Option<&Value>,
    recorded_context: Option<&Value>,
    approval_decisions: Option<&Value>,
    package_lock: Option<&Value>,
    ledger_path: Option<&Path>,
    resume: bool,
    identity_seed: Option<&str>,
) -> Result<Value, RuntimeError> {
    let plan_object = object(plan, "workflow-plan")?;
    preflight_runtime_plan(plan_object)?;
    let result_map = optional_object(recorded_results, "recorded adapter results")?;
    preflight_recorded_event_budget(plan_object, result_map)?;
    let plan_fingerprint = required_str(plan_object, "fingerprint", "workflow-plan")?;
    let plan_package_lock_hash = contract_optional_hash(
        plan_object,
        "package_lock_hash",
        "workflow-plan package_lock_hash",
    )?;
    match (plan_package_lock_hash, package_lock) {
        (Some(_), None) => {
            return Err(RuntimeError::Contract(
                "locked workflow execution requires the current package Lockfile".to_owned(),
            ));
        }
        (None, Some(_)) => {
            return Err(RuntimeError::Contract(
                "workflow plan is not frozen to the supplied package Lockfile".to_owned(),
            ));
        }
        (Some(_), Some(lock)) => validate_plan_package_lock(plan, lock)?,
        (None, None) => {}
    }
    let behavior_map = optional_object(behaviors, "runtime behaviors")?;
    if behavior_map.is_some() && result_map.is_some() {
        return Err(RuntimeError::Contract(
            "runtime cannot combine fake behaviors with recorded adapter results".to_owned(),
        ));
    }
    if result_map.is_some() && recorded_context.is_none() {
        return Err(RuntimeError::Contract(
            "recorded adapter runtime requires task context".to_owned(),
        ));
    }
    let approval_map = optional_object(approval_decisions, "runtime approval decisions")?;
    validate_behavior_map(behavior_map)?;
    validate_approval_map(approval_map)?;

    let machine = NodeStateMachine::new(identity_seed)?;
    let ledger_file = ledger_path
        .map(|path| open_ledger_handle(path, resume))
        .transpose()?;
    let mut ledger = if resume {
        let mut ledger = RunLedger::replay(
            ledger_file.ok_or_else(|| {
                RuntimeError::Contract("resume requires an existing ledger path".to_owned())
            })?,
            plan_fingerprint,
        )?;
        if required_str(
            object(&ledger.value, "run-ledger")?,
            "package_lock_hash",
            "run-ledger",
        )? != plan_package_lock_hash.unwrap_or("")
        {
            return Err(RuntimeError::Contract(
                "cannot resume ledger with a different package lock".to_owned(),
            ));
        }
        let required_events = runtime_event_budget(plan_object)?
            .saturating_add(recorded_event_budget(plan_object, result_map)?);
        reserve_ledger_events(ledger.event_count, required_events)?;
        ledger.append(
            "run-resumed",
            json!({
                "package_lock_hash": plan_package_lock_hash.unwrap_or(""),
                "plan_fingerprint": plan_fingerprint,
            }),
        )?;
        ledger
    } else {
        let mut ledger = RunLedger::new(
            plan_fingerprint,
            plan_package_lock_hash.unwrap_or(""),
            ledger_file,
            &machine.clock.identifier("run"),
        );
        ledger.append(
            "run-started",
            json!({
                "package_lock_hash": plan_package_lock_hash.unwrap_or(""),
                "plan_fingerprint": plan_fingerprint,
            }),
        )?;
        ledger
    };
    if optional_str(plan_object, "status") == Some("blocked") {
        ledger.append(
            "run-blocked",
            json!({
                "missing_capabilities": plan_object
                    .get("missing_capabilities")
                    .cloned()
                    .unwrap_or_else(|| json!([])),
            }),
        )?;
        return ledger.finalize("blocked");
    }

    let mut scheduler = ResourceScheduler::default();
    let prior_sequences = array(
        object(&ledger.value, "run-ledger")?,
        "resource_events",
        "run-ledger",
    )?
    .iter()
    .filter_map(|event| event.get("sequence").and_then(Value::as_u64))
    .collect::<Vec<_>>();
    scheduler.seed_sequence(
        prior_sequences
            .iter()
            .copied()
            .max()
            .map_or(0, |value| value.saturating_add(1)),
    )?;

    let nodes_array = array(plan_object, "nodes", "workflow-plan")?;
    let mut nodes = BTreeMap::new();
    for node in nodes_array {
        let node_id = required_str(
            object(node, "workflow-plan.node")?,
            "id",
            "workflow-plan.node",
        )?;
        nodes.insert(node_id.to_owned(), node.clone());
    }
    let order = topological(plan)?;
    let mut predecessor_status: BTreeMap<String, Vec<NodeStatus>> = BTreeMap::new();
    let latest = latest_statuses(&ledger.value)?;
    let mut resource_cursor = 0_usize;
    let mut behavior_cursors = BTreeMap::<String, usize>::new();

    for node_id in order {
        let node = nodes
            .get(&node_id)
            .ok_or_else(|| RuntimeError::Contract("workflow-plan node is missing".to_owned()))?;
        let reusable = if let Some(results) = result_map {
            reusable_recorded_status(
                plan,
                node,
                &ledger.value,
                latest.get(&node_id).copied(),
                results,
                recorded_context.ok_or_else(|| {
                    RuntimeError::Contract(
                        "recorded adapter runtime requires task context".to_owned(),
                    )
                })?,
            )?
        } else if latest.get(&node_id) == Some(&NodeStatus::Passed) {
            Some(NodeStatus::Passed)
        } else {
            None
        };
        if let Some(status) = reusable {
            record_successors(plan, &node_id, status, &mut predecessor_status)?;
            continue;
        }
        let incoming = predecessor_status
            .get(&node_id)
            .cloned()
            .unwrap_or_default();
        if incoming
            .iter()
            .any(|status| !matches!(status, NodeStatus::Passed | NodeStatus::Skipped))
        {
            let mut attempt = new_attempt_for_node(&machine, node, &ledger.value)?;
            machine.transition(&mut attempt, NodeStatus::Blocked, "upstream-not-passed")?;
            ledger.append("node-attempt", attempt)?;
            record_successors(plan, &node_id, NodeStatus::Blocked, &mut predecessor_status)?;
            continue;
        }
        let node_object = object(node, "workflow-plan.node")?;
        if node_object
            .get("provider")
            .is_none_or(|value| !json_truthy(value))
        {
            let mut attempt = new_attempt_for_node(&machine, node, &ledger.value)?;
            let target = if node_object.get("mandatory").is_some_and(json_truthy) {
                NodeStatus::Blocked
            } else {
                NodeStatus::Skipped
            };
            machine.transition(&mut attempt, target, "capability-provider-missing")?;
            ledger.append("node-attempt", attempt)?;
            record_successors(plan, &node_id, target, &mut predecessor_status)?;
            continue;
        }

        let (mut attempt, approval_outcome) =
            prepare_approval(&machine, node, &mut ledger, approval_map, resume)?;
        if matches!(approval_outcome.as_str(), "pending" | "denied" | "expired") {
            record_successors(plan, &node_id, NodeStatus::Blocked, &mut predecessor_status)?;
            continue;
        }

        let final_status = loop {
            if required_str(object(&attempt, "node-attempt")?, "status", "node-attempt")?
                == "pending"
            {
                machine.transition(&mut attempt, NodeStatus::Ready, "dependencies-satisfied")?;
            }
            if let Some(results) = result_map
                && let Err(error) = prepare_recorded_adapter(
                    plan,
                    node,
                    &ledger.value,
                    results,
                    recorded_context.ok_or_else(|| {
                        RuntimeError::Contract(
                            "recorded adapter runtime requires task context".to_owned(),
                        )
                    })?,
                )
            {
                machine.transition(
                    &mut attempt,
                    NodeStatus::Blocked,
                    "adapter-contract-invalid",
                )?;
                ledger.append("node-attempt", attempt.clone())?;
                record_adapter_contract_failure(
                    plan,
                    node,
                    &attempt,
                    &mut ledger,
                    results,
                    recorded_context.ok_or_else(|| {
                        RuntimeError::Contract(
                            "recorded adapter runtime requires task context".to_owned(),
                        )
                    })?,
                    &error,
                )?;
                return Err(error);
            }
            let attempt_id = required_str(
                object(&attempt, "node-attempt")?,
                "attempt_id",
                "node-attempt",
            )?
            .to_owned();
            let resource_keys = optional_string_array(node_object, "resource_keys")?;
            if !scheduler.acquire(&attempt_id, &resource_keys) {
                machine.transition(&mut attempt, NodeStatus::Blocked, "resource-unavailable")?;
                ledger.append("node-attempt", attempt)?;
                break NodeStatus::Blocked;
            }
            resource_cursor = flush_resource_events(&scheduler, &mut ledger, resource_cursor)?;
            machine.transition(&mut attempt, NodeStatus::Running, "fake-adapter-started")?;
            let capability = required_str(node_object, "capability", "workflow-plan.node")?;
            let (behavior, target, reason) = if let Some(results) = result_map {
                recorded_adapter_outcome(
                    plan,
                    node,
                    &attempt,
                    &mut ledger,
                    results,
                    recorded_context.ok_or_else(|| {
                        RuntimeError::Contract(
                            "recorded adapter runtime requires task context".to_owned(),
                        )
                    })?,
                )?
            } else {
                let behavior = next_behavior(behavior_map, capability, &mut behavior_cursors)?;
                let (target, reason) = if behavior == "timed-out" {
                    (NodeStatus::Blocked, "fake-adapter-timed-out".to_owned())
                } else {
                    let candidate = NodeStatus::parse(&behavior).unwrap_or(NodeStatus::Failed);
                    let target = if matches!(
                        candidate,
                        NodeStatus::Passed
                            | NodeStatus::Failed
                            | NodeStatus::Blocked
                            | NodeStatus::Skipped
                            | NodeStatus::Cancelled
                    ) {
                        candidate
                    } else {
                        NodeStatus::Failed
                    };
                    (target, format!("fake-adapter-{}", target.as_str()))
                };
                (behavior, target, reason)
            };
            machine.transition(&mut attempt, target, &reason)?;
            let release_action = if behavior == "timed-out" {
                "timed-out"
            } else if target == NodeStatus::Cancelled {
                "cancelled"
            } else {
                "released"
            };
            scheduler.release(&attempt_id, release_action)?;
            resource_cursor = flush_resource_events(&scheduler, &mut ledger, resource_cursor)?;
            ledger.append("node-attempt", attempt.clone())?;
            let attempt_number = required_u64(
                object(&attempt, "node-attempt")?,
                "attempt_number",
                "node-attempt",
            )?;
            let max_retries = optional_u64(node_object, "max_retries").unwrap_or(0);
            let idempotent = node_object.get("idempotent").is_some_and(json_truthy);
            let retryable = target == NodeStatus::Failed || behavior == "timed-out";
            if result_map.is_some()
                || !retryable
                || !NodeStateMachine::can_auto_retry(idempotent, attempt_number, max_retries)
            {
                break target;
            }
            attempt = new_attempt_for_node(&machine, node, &ledger.value)?;
        };
        record_successors(plan, &node_id, final_status, &mut predecessor_status)?;
    }

    let statuses = latest_statuses(&ledger.value)?
        .into_values()
        .collect::<Vec<_>>();
    let final_status = if !statuses.is_empty()
        && statuses
            .iter()
            .all(|status| matches!(status, NodeStatus::Passed | NodeStatus::Skipped))
    {
        if optional_str(plan_object, "status") == Some("degraded")
            || statuses.contains(&NodeStatus::Skipped)
        {
            "partial"
        } else {
            "completed"
        }
    } else if statuses.contains(&NodeStatus::Cancelled) {
        "cancelled"
    } else if statuses.contains(&NodeStatus::Blocked) {
        "blocked"
    } else {
        "partial"
    };
    let final_status = if result_map.is_some() {
        adjust_recorded_final_status(final_status, &ledger.value)?
    } else {
        final_status
    };
    ledger.finalize(final_status)
}

fn open_ledger_handle(path: &Path, resume: bool) -> Result<std::fs::File, RuntimeError> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let parent = absolute
        .parent()
        .ok_or_else(|| RuntimeError::Contract("runtime ledger path has no parent".to_owned()))?;
    let file_name = absolute
        .file_name()
        .ok_or_else(|| RuntimeError::Contract("runtime ledger filename is invalid".to_owned()))?;
    let mut current = PathBuf::new();
    for component in parent.components() {
        match component {
            Component::Prefix(prefix) => current.push(prefix.as_os_str()),
            Component::RootDir => current.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                return Err(RuntimeError::Contract(
                    "runtime ledger parent traversal is forbidden".to_owned(),
                ));
            }
            Component::Normal(name) => {
                current.push(name);
                match std::fs::symlink_metadata(&current) {
                    Ok(metadata) => {
                        if metadata.file_type().is_symlink() && current.components().count() <= 2 {
                            current = std::fs::canonicalize(&current)?;
                        } else if metadata.file_type().is_symlink() || !metadata.is_dir() {
                            return Err(RuntimeError::Contract(
                                "runtime ledger parent must contain only real directories"
                                    .to_owned(),
                            ));
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        return Err(RuntimeError::Contract(
                            "runtime ledger parent must already exist".to_owned(),
                        ));
                    }
                    Err(error) => return Err(RuntimeError::Io(error)),
                }
            }
        }
    }
    let before = std::fs::metadata(parent)?;
    let directory = Dir::open_ambient_dir(parent, ambient_authority())?;
    let after = std::fs::metadata(parent)?;
    if !same_file_identity(&before, &after) {
        return Err(RuntimeError::Contract(
            "runtime ledger parent changed while acquiring its directory handle".to_owned(),
        ));
    }
    let mut options = CapOpenOptions::new();
    options.read(true).write(true);
    let file = if resume {
        let metadata = directory.symlink_metadata(file_name)?;
        if metadata.is_symlink() || !metadata.is_file() {
            return Err(RuntimeError::Contract(
                "runtime ledger path must be a regular file".to_owned(),
            ));
        }
        directory.open_with(file_name, &options)?
    } else {
        match directory.symlink_metadata(file_name) {
            Ok(metadata) if metadata.is_symlink() || !metadata.is_file() => {
                return Err(RuntimeError::Contract(
                    "runtime ledger path must be a regular file".to_owned(),
                ));
            }
            Ok(_) => {
                return Err(RuntimeError::Contract(
                    "new runtime ledger path already exists".to_owned(),
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(RuntimeError::Io(error)),
        }
        options.create_new(true);
        directory.open_with(file_name, &options).map_err(|error| {
            if error.kind() == std::io::ErrorKind::AlreadyExists {
                RuntimeError::Contract("new runtime ledger path already exists".to_owned())
            } else {
                RuntimeError::Io(error)
            }
        })?
    };
    let file = file.into_std();
    ensure_opened_ledger_identity(&file, &absolute)?;
    file.try_lock().map_err(|_| {
        RuntimeError::Contract("runtime ledger is already owned by another process".to_owned())
    })?;
    Ok(file)
}

fn ensure_opened_ledger_identity(
    file: &std::fs::File,
    absolute: &Path,
) -> Result<(), RuntimeError> {
    let opened = file.metadata()?;
    let current = std::fs::symlink_metadata(absolute)?;
    if current.file_type().is_symlink()
        || !current.is_file()
        || !same_file_identity(&opened, &current)
    {
        return Err(RuntimeError::Contract(
            "runtime ledger path changed while acquiring its file handle".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn same_file_identity(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt as _;
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(windows)]
fn same_file_identity(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt as _;
    left.volume_serial_number() == right.volume_serial_number()
        && left.file_index() == right.file_index()
}

#[cfg(not(any(unix, windows)))]
fn same_file_identity(_left: &std::fs::Metadata, _right: &std::fs::Metadata) -> bool {
    false
}

fn prepare_approval(
    machine: &NodeStateMachine,
    node: &Value,
    ledger: &mut RunLedger,
    approval_decisions: Option<&Map<String, Value>>,
    resume: bool,
) -> Result<(Value, String), RuntimeError> {
    let node_object = object(node, "workflow-plan.node")?;
    let approval = node_object
        .get("approval")
        .filter(|value| json_truthy(value));
    let node_id = required_str(node_object, "id", "workflow-plan.node")?;
    let previous_attempt = latest_attempt(&ledger.value, node_id)?.cloned();
    let previous_record = previous_attempt
        .as_ref()
        .and_then(|attempt| approval_for_attempt(&ledger.value, attempt).ok().flatten())
        .cloned();
    let capability = required_str(node_object, "capability", "workflow-plan.node")?;
    let decision = approval_decisions
        .and_then(|decisions| decisions.get(capability))
        .and_then(Value::as_str);

    if let (Some(approval), true, Some(previous_attempt), Some(previous_record)) =
        (approval, resume, previous_attempt, previous_record)
        && required_str(
            object(&previous_attempt, "node-attempt")?,
            "status",
            "node-attempt",
        )? == "blocked"
    {
        let mut attempt = previous_attempt;
        let mut record = previous_record;
        let scope = object(approval, "workflow-plan.node.approval")?
            .get("scope")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if required_str(
            object(&record, "approval-record")?,
            "status",
            "approval-record",
        )? == "pending"
            && let Some(decision) = decision
        {
            ApprovalGate::decide(&mut record, decision, &scope)?;
            ledger.append("approval-record", record.clone())?;
        }
        let status = required_str(
            object(&record, "approval-record")?,
            "status",
            "approval-record",
        )?
        .to_owned();
        if status == "granted" {
            machine.transition(
                &mut attempt,
                NodeStatus::Ready,
                "approval-granted-on-resume",
            )?;
        }
        return Ok((attempt, status));
    }

    let mut attempt = new_attempt_for_node(machine, node, &ledger.value)?;
    machine.transition(&mut attempt, NodeStatus::Ready, "dependencies-satisfied")?;
    let Some(approval) = approval else {
        return Ok((attempt, "not-required".to_owned()));
    };
    let approval = object(approval, "workflow-plan.node.approval")?;
    let scope = approval.get("scope").cloned().unwrap_or_else(|| json!({}));
    let attempt_id = required_str(
        object(&attempt, "node-attempt")?,
        "attempt_id",
        "node-attempt",
    )?;
    let mut record = ApprovalGate::request(
        attempt_id,
        required_str(approval, "action", "workflow-plan.node.approval")?,
        required_str(approval, "reason", "workflow-plan.node.approval")?,
        &scope,
    )?;
    if let Some(decision) = decision {
        ApprovalGate::decide(&mut record, decision, &scope)?;
    }
    ledger.append("approval-record", record.clone())?;
    let status = required_str(
        object(&record, "approval-record")?,
        "status",
        "approval-record",
    )?
    .to_owned();
    if status != "granted" {
        machine.transition(
            &mut attempt,
            NodeStatus::Blocked,
            &format!("approval-{status}"),
        )?;
        ledger.append("node-attempt", attempt.clone())?;
    }
    Ok((attempt, status))
}

fn new_attempt_for_node(
    machine: &NodeStateMachine,
    node: &Value,
    ledger: &Value,
) -> Result<Value, RuntimeError> {
    let node = object(node, "workflow-plan.node")?;
    let node_id = required_str(node, "id", "workflow-plan.node")?;
    let attempts = array(object(ledger, "run-ledger")?, "node_attempts", "run-ledger")?;
    let count = attempts
        .iter()
        .filter(|attempt| {
            attempt
                .as_object()
                .and_then(|item| item.get("node_id"))
                .and_then(Value::as_str)
                == Some(node_id)
        })
        .count();
    let existing_ids = attempts
        .iter()
        .filter_map(|attempt| attempt.get("attempt_id").and_then(Value::as_str))
        .collect::<BTreeSet<_>>();
    loop {
        let attempt = machine.new_attempt(
            node_id,
            u64::try_from(count).unwrap_or(u64::MAX).saturating_add(1),
            optional_u64(node, "max_retries").unwrap_or(0),
            optional_u64(node, "timeout_seconds").unwrap_or(300),
        )?;
        let attempt_id = required_str(
            object(&attempt, "node-attempt")?,
            "attempt_id",
            "node-attempt",
        )?;
        if !existing_ids.contains(attempt_id) {
            return Ok(attempt);
        }
    }
}

fn next_behavior(
    behaviors: Option<&Map<String, Value>>,
    capability: &str,
    cursors: &mut BTreeMap<String, usize>,
) -> Result<String, RuntimeError> {
    let Some(configured) = behaviors.and_then(|items| items.get(capability)) else {
        return Ok("passed".to_owned());
    };
    if let Some(value) = configured.as_str() {
        return Ok(value.to_owned());
    }
    let values = configured.as_array().ok_or_else(|| {
        RuntimeError::Contract("runtime behavior must be a string or string array".to_owned())
    })?;
    if values.is_empty() {
        return Ok("passed".to_owned());
    }
    let cursor = cursors.entry(capability.to_owned()).or_default();
    let index = (*cursor).min(values.len() - 1);
    *cursor = cursor.saturating_add(1);
    values[index].as_str().map(str::to_owned).ok_or_else(|| {
        RuntimeError::Contract("runtime behavior entries must be strings".to_owned())
    })
}

fn prepare_recorded_adapter(
    plan: &Value,
    node: &Value,
    ledger: &Value,
    results: &Map<String, Value>,
    context: &Value,
) -> Result<(), RuntimeError> {
    let node = object(node, "workflow-plan.node")?;
    if optional_str(node, "provider") == Some("core") {
        return Ok(());
    }
    let node_id = required_str(node, "id", "workflow-plan.node")?;
    let Some(result) = results.get(node_id) else {
        return Ok(());
    };
    let result_object = result.as_object().ok_or_else(|| {
        RuntimeError::Contract("recorded adapter result must be an object".to_owned())
    })?;
    let invocation_id = result_object
        .get("invocation_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            RuntimeError::Contract("recorded adapter result invocation_id is required".to_owned())
        })?;
    let request = build_adapter_request(plan, node_id, context, invocation_id)?;
    validate_adapter_result(&request, result)?;
    let outcomes = array(
        object(ledger, "run-ledger")?,
        "adapter_outcomes",
        "run-ledger",
    )?;
    let request_id = required_str(
        object(&request, "adapter-request")?,
        "request_id",
        "adapter-request",
    )?;
    if outcomes.iter().any(|outcome| {
        outcome.get("request_id").and_then(Value::as_str) == Some(request_id)
            || outcome.get("invocation_id").and_then(Value::as_str) == Some(invocation_id)
    }) {
        return Err(RuntimeError::Contract(
            "recorded adapter result has already been consumed by an attempt".to_owned(),
        ));
    }
    Ok(())
}

fn reusable_recorded_status(
    plan: &Value,
    node: &Value,
    ledger: &Value,
    latest_status: Option<NodeStatus>,
    results: &Map<String, Value>,
    context: &Value,
) -> Result<Option<NodeStatus>, RuntimeError> {
    let node = object(node, "workflow-plan.node")?;
    if optional_str(node, "provider") == Some("core") {
        return Ok((latest_status == Some(NodeStatus::Passed)).then_some(NodeStatus::Passed));
    }
    let node_id = required_str(node, "id", "workflow-plan.node")?;
    let Some(result) = results.get(node_id).and_then(Value::as_object) else {
        return Ok(None);
    };
    let Some(invocation_id) = result
        .get("invocation_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return Ok(None);
    };
    let request = build_adapter_request(plan, node_id, context, invocation_id)?;
    let Some(attempt) = latest_attempt(ledger, node_id)? else {
        return Ok(None);
    };
    let attempt_id = required_str(
        object(attempt, "node-attempt")?,
        "attempt_id",
        "node-attempt",
    )?;
    let outcomes = array(
        object(ledger, "run-ledger")?,
        "adapter_outcomes",
        "run-ledger",
    )?;
    let Some(outcome) = outcomes
        .iter()
        .rev()
        .find(|outcome| outcome.get("attempt_id").and_then(Value::as_str) == Some(attempt_id))
    else {
        return Ok(None);
    };
    let request_id = required_str(
        object(&request, "adapter-request")?,
        "request_id",
        "adapter-request",
    )?;
    if outcome.get("request_id").and_then(Value::as_str) != Some(request_id)
        || outcome.get("status") != result.get("status")
    {
        return Ok(None);
    }
    let result_status = result.get("status").and_then(Value::as_str);
    Ok(match (latest_status, result_status) {
        (Some(NodeStatus::Passed), Some("completed")) => Some(NodeStatus::Passed),
        (Some(NodeStatus::Skipped), Some("partial")) => Some(NodeStatus::Skipped),
        (Some(NodeStatus::Blocked), Some("partial")) => Some(NodeStatus::Blocked),
        _ => None,
    })
}

#[allow(clippy::too_many_lines)]
fn recorded_adapter_outcome(
    plan: &Value,
    node: &Value,
    attempt: &Value,
    ledger: &mut RunLedger,
    results: &Map<String, Value>,
    context: &Value,
) -> Result<(String, NodeStatus, String), RuntimeError> {
    let node = object(node, "workflow-plan.node")?;
    if optional_str(node, "provider") == Some("core") {
        return Ok((
            "passed".to_owned(),
            NodeStatus::Passed,
            "fake-adapter-passed".to_owned(),
        ));
    }
    let node_id = required_str(node, "id", "workflow-plan.node")?;
    let Some(result) = results.get(node_id) else {
        return Ok((
            "blocked".to_owned(),
            NodeStatus::Blocked,
            "fake-adapter-blocked".to_owned(),
        ));
    };
    let result_object = object(result, "recorded adapter result")?;
    let invocation_id = required_str(result_object, "invocation_id", "adapter-result")?;
    let request = build_adapter_request(plan, node_id, context, invocation_id)?;
    validate_adapter_result(&request, result)?;
    let request_object = object(&request, "adapter-request")?;
    let attempt_id = required_str(
        object(attempt, "node-attempt")?,
        "attempt_id",
        "node-attempt",
    )?;
    let provider = required_str(node, "provider", "workflow-plan.node")?;
    let result_status = required_str(result_object, "status", "adapter-result")?;
    ledger.append(
        "adapter-outcome",
        json!({
            "attempt_id": attempt_id,
            "cleanup": required_value(result_object, "cleanup", "adapter-result")?.clone(),
            "failure_attribution": required_value(
                result_object,
                "failure_attribution",
                "adapter-result",
            )?.clone(),
            "invocation_id": invocation_id,
            "node_id": node_id,
            "provider": provider,
            "request_id": required_str(
                request_object,
                "request_id",
                "adapter-request",
            )?,
            "status": result_status,
        }),
    )?;
    for artifact in array(result_object, "artifacts", "adapter-result")? {
        let mut recorded = object(artifact, "adapter-result.artifact")?.clone();
        recorded.insert(
            "attempt_id".to_owned(),
            Value::String(attempt_id.to_owned()),
        );
        recorded.insert("node_id".to_owned(), Value::String(node_id.to_owned()));
        ledger.append("artifact-hash", Value::Object(recorded))?;
    }
    for evidence in array(result_object, "evidence", "adapter-result")? {
        let mut recorded = object(evidence, "adapter-result.evidence")?.clone();
        recorded.insert(
            "attempt_id".to_owned(),
            Value::String(attempt_id.to_owned()),
        );
        recorded.insert("node_id".to_owned(), Value::String(node_id.to_owned()));
        recorded.insert("provider".to_owned(), Value::String(provider.to_owned()));
        ledger.append("adapter-evidence", Value::Object(recorded))?;
    }
    let no_test_reason = result_object
        .get("no_test_reason")
        .filter(|value| !value.is_null());
    if let Some(reason) = no_test_reason {
        ledger.append(
            "adapter-evidence",
            json!({
                "attempt_id": attempt_id,
                "node_id": node_id,
                "provider": provider,
                "kind": "validation",
                "status": result_status,
                "summary": reason.clone(),
                "data": {
                    "suggested_validation": required_value(
                        result_object,
                        "suggested_validation",
                        "adapter-result",
                    )?.clone(),
                },
                "artifact_ids": [],
            }),
        )?;
    }
    let capability = required_str(node, "capability", "workflow-plan.node")?;
    let target = if result_status == "partial"
        && no_test_reason.is_some()
        && capability
            .rsplit_once('.')
            .is_some_and(|(_, suffix)| suffix == "auto")
    {
        NodeStatus::Skipped
    } else {
        match result_status {
            "completed" => NodeStatus::Passed,
            "partial" | "blocked" => NodeStatus::Blocked,
            "failed" => NodeStatus::Failed,
            _ => {
                return Err(RuntimeError::Contract(
                    "adapter-result status is invalid".to_owned(),
                ));
            }
        }
    };
    Ok((
        target.as_str().to_owned(),
        target,
        format!("fake-adapter-{}", target.as_str()),
    ))
}

#[allow(clippy::too_many_arguments)]
fn record_adapter_contract_failure(
    plan: &Value,
    node: &Value,
    attempt: &Value,
    ledger: &mut RunLedger,
    results: &Map<String, Value>,
    context: &Value,
    error: &RuntimeError,
) -> Result<(), RuntimeError> {
    let node = object(node, "workflow-plan.node")?;
    let node_id = required_str(node, "id", "workflow-plan.node")?;
    let attempt_id = required_str(
        object(attempt, "node-attempt")?,
        "attempt_id",
        "node-attempt",
    )?;
    let submitted_invocation_id = results
        .get(node_id)
        .and_then(Value::as_object)
        .and_then(|result| result.get("invocation_id"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("unavailable");
    let invocation_id = format!("contract-failure-{attempt_id}");
    let request_id = build_adapter_request(plan, node_id, context, &invocation_id)
        .ok()
        .and_then(|request| {
            request
                .get("request_id")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_else(|| format!("adapter-request-unavailable-{attempt_id}"));
    ledger.append(
        "adapter-outcome",
        json!({
            "attempt_id": attempt_id,
            "cleanup": [],
            "failure_attribution": {
                "category": "contract",
                "summary": format!(
                    "{error}; submitted_invocation_id={submitted_invocation_id}"
                ),
            },
            "invocation_id": invocation_id,
            "node_id": node_id,
            "provider": optional_str(node, "provider").unwrap_or("unknown-provider"),
            "request_id": request_id,
            "status": "blocked",
        }),
    )
}

fn adjust_recorded_final_status<'a>(
    status: &'a str,
    ledger: &Value,
) -> Result<&'a str, RuntimeError> {
    if status != "blocked" {
        return Ok(status);
    }
    let ledger_object = object(ledger, "run-ledger")?;
    let mut latest = BTreeMap::<String, &Value>::new();
    for attempt in array(ledger_object, "node_attempts", "run-ledger")? {
        let node_id = required_str(object(attempt, "node-attempt")?, "node_id", "node-attempt")?;
        latest.insert(node_id.to_owned(), attempt);
    }
    let outcomes = array(ledger_object, "adapter_outcomes", "run-ledger")?;
    let mut has_current_partial = false;
    for attempt in latest.values() {
        let attempt_object = object(attempt, "node-attempt")?;
        if required_str(attempt_object, "status", "node-attempt")? != "blocked" {
            continue;
        }
        let attempt_id = required_str(attempt_object, "attempt_id", "node-attempt")?;
        let outcome = outcomes
            .iter()
            .find(|outcome| outcome.get("attempt_id").and_then(Value::as_str) == Some(attempt_id));
        if outcome
            .and_then(|item| item.get("status"))
            .and_then(Value::as_str)
            == Some("partial")
        {
            has_current_partial = true;
            continue;
        }
        let reason = array(attempt_object, "events", "node-attempt")?
            .last()
            .and_then(|event| event.get("reason"))
            .and_then(Value::as_str);
        if reason == Some("upstream-not-passed") {
            continue;
        }
        return Ok(status);
    }
    Ok(if has_current_partial {
        "partial"
    } else {
        status
    })
}

fn topological(plan: &Value) -> Result<Vec<String>, RuntimeError> {
    let plan = object(plan, "workflow-plan")?;
    let mut ids = BTreeSet::new();
    for node in array(plan, "nodes", "workflow-plan")? {
        ids.insert(
            required_str(
                object(node, "workflow-plan.node")?,
                "id",
                "workflow-plan.node",
            )?
            .to_owned(),
        );
    }
    let mut incoming = ids
        .iter()
        .map(|id| (id.clone(), 0_u64))
        .collect::<BTreeMap<_, _>>();
    let mut outgoing = BTreeMap::<String, Vec<String>>::new();
    for edge in array(plan, "edges", "workflow-plan")? {
        let edge = object(edge, "workflow-plan.edge")?;
        let source = required_str(edge, "from", "workflow-plan.edge")?;
        let target = required_str(edge, "to", "workflow-plan.edge")?;
        let count = incoming.get_mut(target).ok_or_else(|| {
            RuntimeError::Contract("workflow-plan edge references unknown node".to_owned())
        })?;
        if !ids.contains(source) {
            return Err(RuntimeError::Contract(
                "workflow-plan edge references unknown node".to_owned(),
            ));
        }
        *count = count.saturating_add(1);
        outgoing
            .entry(source.to_owned())
            .or_default()
            .push(target.to_owned());
    }
    let mut queue = ids
        .iter()
        .filter(|id| incoming.get(*id) == Some(&0))
        .cloned()
        .collect::<Vec<_>>();
    queue.sort();
    let mut queue = VecDeque::from(queue);
    let mut result = Vec::new();
    while let Some(node_id) = queue.pop_front() {
        result.push(node_id.clone());
        let mut targets = outgoing.get(&node_id).cloned().unwrap_or_default();
        targets.sort();
        for target in targets {
            let count = incoming.get_mut(&target).ok_or_else(|| {
                RuntimeError::Contract("workflow-plan edge references unknown node".to_owned())
            })?;
            *count = count.saturating_sub(1);
            if *count == 0 {
                queue.push_back(target);
            }
        }
    }
    Ok(result)
}

fn record_successors(
    plan: &Value,
    node_id: &str,
    status: NodeStatus,
    destination: &mut BTreeMap<String, Vec<NodeStatus>>,
) -> Result<(), RuntimeError> {
    for edge in array(object(plan, "workflow-plan")?, "edges", "workflow-plan")? {
        let edge = object(edge, "workflow-plan.edge")?;
        if required_str(edge, "from", "workflow-plan.edge")? == node_id {
            destination
                .entry(required_str(edge, "to", "workflow-plan.edge")?.to_owned())
                .or_default()
                .push(status);
        }
    }
    Ok(())
}

fn latest_statuses(ledger: &Value) -> Result<BTreeMap<String, NodeStatus>, RuntimeError> {
    let mut latest = BTreeMap::new();
    for attempt in array(object(ledger, "run-ledger")?, "node_attempts", "run-ledger")? {
        let attempt = object(attempt, "node-attempt")?;
        latest.insert(
            required_str(attempt, "node_id", "node-attempt")?.to_owned(),
            NodeStatus::parse(required_str(attempt, "status", "node-attempt")?)?,
        );
    }
    Ok(latest)
}

fn latest_attempt<'a>(ledger: &'a Value, node_id: &str) -> Result<Option<&'a Value>, RuntimeError> {
    Ok(
        array(object(ledger, "run-ledger")?, "node_attempts", "run-ledger")?
            .iter()
            .rev()
            .find(|attempt| {
                attempt
                    .as_object()
                    .and_then(|item| item.get("node_id"))
                    .and_then(Value::as_str)
                    == Some(node_id)
            }),
    )
}

fn approval_for_attempt<'a>(
    ledger: &'a Value,
    attempt: &Value,
) -> Result<Option<&'a Value>, RuntimeError> {
    let attempt_id = required_str(
        object(attempt, "node-attempt")?,
        "attempt_id",
        "node-attempt",
    )?;
    Ok(array(
        object(ledger, "run-ledger")?,
        "approval_records",
        "run-ledger",
    )?
    .iter()
    .rev()
    .find(|record| {
        record
            .as_object()
            .and_then(|item| item.get("attempt_id"))
            .and_then(Value::as_str)
            == Some(attempt_id)
    }))
}

fn flush_resource_events(
    scheduler: &ResourceScheduler,
    ledger: &mut RunLedger,
    cursor: usize,
) -> Result<usize, RuntimeError> {
    for event in &scheduler.events[cursor..] {
        ledger.append("resource-event", event.clone())?;
    }
    Ok(scheduler.events.len())
}

fn validate_behavior_map(value: Option<&Map<String, Value>>) -> Result<(), RuntimeError> {
    if let Some(value) = value {
        if value.len() > MAX_RUNTIME_NODES {
            return Err(RuntimeError::Contract(format!(
                "runtime behaviors exceed maximum {MAX_RUNTIME_NODES}"
            )));
        }
        for (capability, behavior) in value {
            if capability.is_empty()
                || !(behavior.is_string()
                    || behavior
                        .as_array()
                        .is_some_and(|items| items.iter().all(Value::is_string)))
            {
                return Err(RuntimeError::Contract(
                    "runtime behaviors must map non-empty capabilities to strings or string arrays"
                        .to_owned(),
                ));
            }
        }
    }
    Ok(())
}

fn validate_approval_map(value: Option<&Map<String, Value>>) -> Result<(), RuntimeError> {
    if let Some(value) = value {
        if value.len() > MAX_RUNTIME_NODES {
            return Err(RuntimeError::Contract(format!(
                "runtime approval decisions exceed maximum {MAX_RUNTIME_NODES}"
            )));
        }
        for (capability, decision) in value {
            if capability.is_empty()
                || !decision
                    .as_str()
                    .is_some_and(|status| matches!(status, "granted" | "denied" | "expired"))
            {
                return Err(RuntimeError::Contract(
                    "runtime approval decisions are invalid".to_owned(),
                ));
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn preflight_runtime_plan(plan: &Map<String, Value>) -> Result<(), RuntimeError> {
    let nodes = array(plan, "nodes", "workflow-plan")?;
    let edges = array(plan, "edges", "workflow-plan")?;
    if nodes.len() > MAX_RUNTIME_NODES {
        return Err(RuntimeError::Contract(format!(
            "workflow runtime nodes exceed maximum {MAX_RUNTIME_NODES}"
        )));
    }
    if edges.len() > MAX_RUNTIME_EDGES {
        return Err(RuntimeError::Contract(format!(
            "workflow runtime edges exceed maximum {MAX_RUNTIME_EDGES}"
        )));
    }
    let mut ids = BTreeSet::new();
    for node in nodes {
        let node_id = required_str(
            object(node, "workflow-plan.node")?,
            "id",
            "workflow-plan.node",
        )?;
        if node_id.is_empty() || !ids.insert(node_id.to_owned()) {
            return Err(RuntimeError::Contract(
                "workflow runtime node ids must be non-empty and unique".to_owned(),
            ));
        }
    }
    let mut incoming = ids
        .iter()
        .map(|node_id| (node_id.clone(), 0_usize))
        .collect::<BTreeMap<_, _>>();
    let mut outgoing = BTreeMap::<String, Vec<String>>::new();
    for edge in edges {
        let edge = object(edge, "workflow-plan.edge")?;
        let source = required_str(edge, "from", "workflow-plan.edge")?;
        let target = required_str(edge, "to", "workflow-plan.edge")?;
        if !ids.contains(source) || !ids.contains(target) {
            return Err(RuntimeError::Contract(
                "workflow runtime edge references unknown node".to_owned(),
            ));
        }
        *incoming.get_mut(target).ok_or_else(|| {
            RuntimeError::Contract("workflow runtime edge references unknown node".to_owned())
        })? += 1;
        outgoing
            .entry(source.to_owned())
            .or_default()
            .push(target.to_owned());
    }
    let mut queue = VecDeque::from(
        ids.iter()
            .filter(|node_id| incoming.get(*node_id) == Some(&0))
            .cloned()
            .collect::<Vec<_>>(),
    );
    let mut visited = 0_usize;
    while let Some(node_id) = queue.pop_front() {
        visited += 1;
        let mut targets = outgoing.get(&node_id).cloned().unwrap_or_default();
        targets.sort();
        for target in targets {
            let count = incoming.get_mut(&target).ok_or_else(|| {
                RuntimeError::Contract("workflow runtime edge references unknown node".to_owned())
            })?;
            *count = count.saturating_sub(1);
            if *count == 0 {
                queue.push_back(target);
            }
        }
    }
    if visited != ids.len() {
        return Err(RuntimeError::Contract(
            "workflow runtime contains dependency cycle".to_owned(),
        ));
    }
    let projected_events = runtime_event_budget(plan)?;
    if projected_events > MAX_LEDGER_EVENTS {
        return Err(RuntimeError::Contract(format!(
            "workflow runtime projected events exceed maximum {MAX_LEDGER_EVENTS}"
        )));
    }
    Ok(())
}

fn runtime_event_budget(plan: &Map<String, Value>) -> Result<usize, RuntimeError> {
    let mut projected_events = if optional_str(plan, "status") == Some("blocked") {
        3_usize
    } else {
        2_usize
    };
    let nodes = array(plan, "nodes", "workflow-plan")?;
    for node in nodes {
        let node = object(node, "workflow-plan.node")?;
        if node
            .get("max_retries")
            .is_some_and(|value| value.as_u64().is_none())
            || node
                .get("timeout_seconds")
                .is_some_and(|value| value.as_u64().is_none_or(|seconds| seconds == 0))
        {
            return Err(RuntimeError::Contract(
                "workflow runtime retry or timeout metadata is invalid".to_owned(),
            ));
        }
        let retries = optional_u64(node, "max_retries").unwrap_or(0);
        let attempts = if node.get("idempotent").is_some_and(json_truthy) {
            retries.saturating_add(1)
        } else {
            1
        };
        let attempts = usize::try_from(attempts).unwrap_or(usize::MAX);
        let resources = optional_string_array(node, "resource_keys")?
            .into_iter()
            .collect::<BTreeSet<_>>()
            .len();
        let per_attempt = resources.saturating_mul(3).saturating_add(1);
        projected_events = projected_events.saturating_add(attempts.saturating_mul(per_attempt));
        if node.get("approval").is_some_and(json_truthy) {
            projected_events = projected_events.saturating_add(2);
        }
    }
    Ok(projected_events)
}

fn recorded_event_budget(
    plan: &Map<String, Value>,
    results: Option<&Map<String, Value>>,
) -> Result<usize, RuntimeError> {
    let Some(results) = results else {
        return Ok(0);
    };
    let mut projected = 0_usize;
    for node in array(plan, "nodes", "workflow-plan")? {
        let node = object(node, "workflow-plan.node")?;
        if optional_str(node, "provider") == Some("core") {
            continue;
        }
        let node_id = required_str(node, "id", "workflow-plan.node")?;
        let Some(result) = results.get(node_id) else {
            continue;
        };
        projected = projected.saturating_add(1);
        if let Some(result) = result.as_object() {
            projected = projected.saturating_add(
                result
                    .get("artifacts")
                    .and_then(Value::as_array)
                    .map_or(0, Vec::len),
            );
            projected = projected.saturating_add(
                result
                    .get("evidence")
                    .and_then(Value::as_array)
                    .map_or(0, Vec::len),
            );
            if result
                .get("no_test_reason")
                .is_some_and(|value| !value.is_null())
            {
                projected = projected.saturating_add(1);
            }
        }
    }
    Ok(projected)
}

fn preflight_recorded_event_budget(
    plan: &Map<String, Value>,
    results: Option<&Map<String, Value>>,
) -> Result<(), RuntimeError> {
    let projected =
        runtime_event_budget(plan)?.saturating_add(recorded_event_budget(plan, results)?);
    if projected > MAX_LEDGER_EVENTS {
        return Err(RuntimeError::Contract(format!(
            "workflow runtime projected events exceed maximum {MAX_LEDGER_EVENTS}"
        )));
    }
    Ok(())
}

fn reserve_ledger_events(
    current_events: usize,
    required_events: usize,
) -> Result<(), RuntimeError> {
    if current_events.saturating_add(required_events) > MAX_LEDGER_EVENTS {
        return Err(RuntimeError::Contract(format!(
            "workflow runtime cannot reserve {required_events} events within maximum {MAX_LEDGER_EVENTS}"
        )));
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_fake_run_ledger(value: &Value) -> Result<(), RuntimeError> {
    let ledger = object(value, "run-ledger")?;
    if optional_str(ledger, "schema_version") != Some("1.0") {
        return Err(RuntimeError::Contract(
            "unsupported schema_version for run-ledger".to_owned(),
        ));
    }
    for field in ["run_id", "plan_fingerprint", "final_status"] {
        if required_str(ledger, field, "run-ledger")?.is_empty() {
            return Err(RuntimeError::Contract(
                "run-ledger identity or status is invalid".to_owned(),
            ));
        }
    }
    if !matches!(
        required_str(ledger, "final_status", "run-ledger")?,
        "active" | "completed" | "partial" | "blocked" | "cancelled"
    ) || contract_optional_ledger_hash(
        ledger,
        "package_lock_hash",
        "run-ledger package_lock_hash",
    )?
    .unwrap_or("")
        != optional_str(ledger, "package_lock_hash").unwrap_or("")
    {
        return Err(RuntimeError::Contract(
            "run-ledger status or package lock is invalid".to_owned(),
        ));
    }
    let attempts = array(ledger, "node_attempts", "run-ledger")?;
    let mut attempt_ids = BTreeSet::new();
    let mut attempt_keys = BTreeSet::new();
    let mut attempts_by_id = BTreeMap::new();
    let mut last_attempt_number = BTreeMap::<String, u64>::new();
    for attempt in attempts {
        validate_node_attempt(attempt)?;
        let attempt = object(attempt, "node-attempt")?;
        let attempt_id = required_str(attempt, "attempt_id", "node-attempt")?;
        let node_id = required_str(attempt, "node_id", "node-attempt")?;
        let number = required_u64(attempt, "attempt_number", "node-attempt")?;
        if !attempt_ids.insert(attempt_id.to_owned()) {
            return Err(RuntimeError::Contract(
                "run-ledger attempt ids must be globally unique".to_owned(),
            ));
        }
        if !attempt_keys.insert((node_id.to_owned(), number)) {
            return Err(RuntimeError::Contract(
                "run-ledger attempt numbers must be unique per node".to_owned(),
            ));
        }
        if last_attempt_number
            .insert(node_id.to_owned(), number)
            .is_some_and(|previous| number <= previous)
        {
            return Err(RuntimeError::Contract(
                "node attempt numbers must be strictly monotonic".to_owned(),
            ));
        }
        attempts_by_id.insert(attempt_id.to_owned(), node_id.to_owned());
    }
    let mut sequences = Vec::new();
    for event in array(ledger, "resource_events", "run-ledger")? {
        let event = object(event, "resource-event")?;
        require_exact_fields(
            event,
            &[
                "action",
                "attempt_id",
                "resource_key",
                "schema_version",
                "sequence",
            ],
            "resource-event",
        )?;
        if optional_str(event, "schema_version") != Some("1.0")
            || required_str(event, "resource_key", "resource-event")?.is_empty()
        {
            return Err(RuntimeError::Contract(
                "resource-event fields are invalid".to_owned(),
            ));
        }
        let attempt_id = required_str(event, "attempt_id", "resource-event")?;
        if !attempts_by_id.contains_key(attempt_id) {
            return Err(RuntimeError::Contract(
                "resource-event references unknown attempt".to_owned(),
            ));
        }
        if !optional_str(event, "action").is_some_and(|action| {
            matches!(
                action,
                "requested" | "acquired" | "released" | "timed-out" | "cancelled"
            )
        }) {
            return Err(RuntimeError::Contract(
                "resource-event action is invalid".to_owned(),
            ));
        }
        sequences.push(required_u64(event, "sequence", "resource-event")?);
    }
    if sequences.windows(2).any(|items| items[0] >= items[1]) {
        return Err(RuntimeError::Contract(
            "resource-event sequences must be increasing and unique".to_owned(),
        ));
    }
    for record in array(ledger, "approval_records", "run-ledger")? {
        let record = object(record, "approval-record")?;
        require_exact_fields(
            record,
            &[
                "action",
                "attempt_id",
                "reason",
                "schema_version",
                "scope",
                "scope_hash",
                "status",
            ],
            "approval-record",
        )?;
        if optional_str(record, "schema_version") != Some("1.0")
            || required_str(record, "action", "approval-record")?.is_empty()
            || required_str(record, "reason", "approval-record")?.is_empty()
        {
            return Err(RuntimeError::Contract(
                "approval-record fields are invalid".to_owned(),
            ));
        }
        let scope = required_value(record, "scope", "approval-record")?;
        if !scope.is_object()
            || required_str(record, "scope_hash", "approval-record")? != canonical_sha256(scope)?
        {
            return Err(RuntimeError::Contract(
                "approval-record scope hash is invalid".to_owned(),
            ));
        }
        let attempt_id = required_str(record, "attempt_id", "approval-record")?;
        if !attempts_by_id.contains_key(attempt_id) {
            return Err(RuntimeError::Contract(
                "approval-record references unknown attempt".to_owned(),
            ));
        }
        if !optional_str(record, "status").is_some_and(|status| {
            matches!(
                status,
                "pending" | "granted" | "denied" | "expired" | "revoked"
            )
        }) {
            return Err(RuntimeError::Contract(
                "approval-record status is invalid".to_owned(),
            ));
        }
    }
    validate_adapter_collections(ledger, attempts, &attempts_by_id)?;
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_adapter_collections(
    ledger: &Map<String, Value>,
    attempts: &[Value],
    attempts_by_id: &BTreeMap<String, String>,
) -> Result<(), RuntimeError> {
    let artifacts = array(ledger, "artifact_hashes", "run-ledger")?;
    let outcomes = array(ledger, "adapter_outcomes", "run-ledger")?;
    let evidence = array(ledger, "evidence", "run-ledger")?;
    let mut artifact_keys = BTreeSet::<(String, String)>::new();
    for artifact in artifacts {
        let artifact = object(artifact, "run-ledger.artifact-hash")?;
        require_exact_fields(
            artifact,
            &[
                "artifact_id",
                "attempt_id",
                "kind",
                "node_id",
                "sha256",
                "uri",
            ],
            "run-ledger.artifact-hash",
        )?;
        validate_attempt_node_reference(artifact, attempts_by_id, "artifact-hash")?;
        for field in ["artifact_id", "kind", "sha256", "uri"] {
            if required_str(artifact, field, "run-ledger.artifact-hash")?.is_empty() {
                return Err(RuntimeError::Contract(
                    "run-ledger.artifact-hash fields are invalid".to_owned(),
                ));
            }
        }
        if !matches!(
            required_str(artifact, "kind", "run-ledger.artifact-hash")?,
            "structured-report"
                | "test-report"
                | "review-report"
                | "delivery-report"
                | "diagnostics"
                | "raw-log"
                | "other"
        ) || contract_optional_hash(artifact, "sha256", "artifact sha256")?.is_none()
        {
            return Err(RuntimeError::Contract(
                "run-ledger artifact-hash kind or sha256 is invalid".to_owned(),
            ));
        }
        let key = (
            required_str(artifact, "attempt_id", "run-ledger.artifact-hash")?.to_owned(),
            required_str(artifact, "artifact_id", "run-ledger.artifact-hash")?.to_owned(),
        );
        if !artifact_keys.insert(key) {
            return Err(RuntimeError::Contract(
                "run-ledger artifact ids must be unique per attempt".to_owned(),
            ));
        }
    }

    let mut outcome_attempts = BTreeSet::new();
    let mut request_ids = BTreeSet::new();
    let mut invocation_ids = BTreeSet::new();
    let mut outcome_providers = BTreeMap::<String, String>::new();
    let mut outcome_statuses = BTreeMap::<String, String>::new();
    for outcome in outcomes {
        let outcome = object(outcome, "run-ledger.adapter-outcome")?;
        require_exact_fields(
            outcome,
            &[
                "attempt_id",
                "cleanup",
                "failure_attribution",
                "invocation_id",
                "node_id",
                "provider",
                "request_id",
                "status",
            ],
            "run-ledger.adapter-outcome",
        )?;
        validate_attempt_node_reference(outcome, attempts_by_id, "adapter-outcome")?;
        let attempt_id = required_str(outcome, "attempt_id", "run-ledger.adapter-outcome")?;
        let status = required_str(outcome, "status", "run-ledger.adapter-outcome")?;
        if !matches!(status, "completed" | "partial" | "blocked" | "failed")
            || !outcome_attempts.insert(attempt_id.to_owned())
        {
            return Err(RuntimeError::Contract(
                "run-ledger adapter-outcome status or attempt is invalid".to_owned(),
            ));
        }
        for field in ["provider", "request_id", "invocation_id"] {
            if required_str(outcome, field, "run-ledger.adapter-outcome")?.is_empty() {
                return Err(RuntimeError::Contract(
                    "run-ledger.adapter-outcome fields are invalid".to_owned(),
                ));
            }
        }
        if !request_ids
            .insert(required_str(outcome, "request_id", "run-ledger.adapter-outcome")?.to_owned())
            || !invocation_ids.insert(
                required_str(outcome, "invocation_id", "run-ledger.adapter-outcome")?.to_owned(),
            )
        {
            return Err(RuntimeError::Contract(
                "run-ledger adapter request and invocation ids must be unique".to_owned(),
            ));
        }
        let attempt_status = attempt_status(attempts, attempt_id)?;
        let status_matches = match status {
            "completed" => attempt_status == "passed",
            "partial" => matches!(attempt_status, "blocked" | "skipped"),
            "blocked" => attempt_status == "blocked",
            "failed" => attempt_status == "failed",
            _ => false,
        };
        if !status_matches {
            return Err(RuntimeError::Contract(
                "run-ledger adapter-outcome status conflicts with node attempt".to_owned(),
            ));
        }
        let attribution = object(
            required_value(outcome, "failure_attribution", "run-ledger.adapter-outcome")?,
            "run-ledger.adapter-outcome.failure-attribution",
        )?;
        require_exact_fields(
            attribution,
            &["category", "summary"],
            "run-ledger.adapter-outcome.failure-attribution",
        )?;
        let category = required_str(
            attribution,
            "category",
            "run-ledger.adapter-outcome.failure-attribution",
        )?;
        if !matches!(
            category,
            "none" | "code" | "environment" | "provider" | "contract"
        ) || required_str(
            attribution,
            "summary",
            "run-ledger.adapter-outcome.failure-attribution",
        )?
        .is_empty()
            || matches!(status, "blocked" | "failed") && category == "none"
        {
            return Err(RuntimeError::Contract(
                "run-ledger adapter-outcome failure attribution is invalid".to_owned(),
            ));
        }
        let cleanup = outcome
            .get("cleanup")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                RuntimeError::Contract("run-ledger adapter-outcome cleanup is invalid".to_owned())
            })?;
        let mut cleanup_failed = false;
        for item in cleanup {
            let item = object(item, "run-ledger.adapter-outcome.cleanup")?;
            require_exact_fields(
                item,
                &["detail", "resource", "status"],
                "run-ledger.adapter-outcome.cleanup",
            )?;
            let cleanup_status =
                required_str(item, "status", "run-ledger.adapter-outcome.cleanup")?;
            let detail = required_str(item, "detail", "run-ledger.adapter-outcome.cleanup")?;
            let resource = required_str(item, "resource", "run-ledger.adapter-outcome.cleanup")?;
            if !matches!(cleanup_status, "not-required" | "completed" | "failed")
                || detail.is_empty()
                || resource.is_empty()
            {
                return Err(RuntimeError::Contract(
                    "run-ledger adapter-outcome cleanup entry is invalid".to_owned(),
                ));
            }
            cleanup_failed |= cleanup_status == "failed";
        }
        if cleanup_failed && !matches!(status, "blocked" | "failed") {
            return Err(RuntimeError::Contract(
                "run-ledger failed cleanup must block or fail the outcome".to_owned(),
            ));
        }
        let provider = required_str(outcome, "provider", "run-ledger.adapter-outcome")?.to_owned();
        outcome_providers.insert(attempt_id.to_owned(), provider);
        outcome_statuses.insert(attempt_id.to_owned(), status.to_owned());
    }

    let mut evidence_statuses = BTreeMap::<String, BTreeSet<String>>::new();
    for item in evidence {
        let item = object(item, "run-ledger.evidence")?;
        require_exact_fields(
            item,
            &[
                "artifact_ids",
                "attempt_id",
                "data",
                "kind",
                "node_id",
                "provider",
                "status",
                "summary",
            ],
            "run-ledger.evidence",
        )?;
        validate_attempt_node_reference(item, attempts_by_id, "adapter-evidence")?;
        let attempt_id = required_str(item, "attempt_id", "run-ledger.evidence")?;
        if !outcome_attempts.contains(attempt_id)
            || outcome_providers.get(attempt_id).map(String::as_str)
                != Some(required_str(item, "provider", "run-ledger.evidence")?)
        {
            return Err(RuntimeError::Contract(
                "run-ledger adapter-evidence outcome or provider is invalid".to_owned(),
            ));
        }
        let kind = required_str(item, "kind", "run-ledger.evidence")?;
        let status = required_str(item, "status", "run-ledger.evidence")?;
        if !matches!(kind, "validation" | "review" | "delivery" | "diagnostic")
            || !matches!(
                status,
                "passed" | "completed" | "partial" | "blocked" | "failed"
            )
            || required_str(item, "summary", "run-ledger.evidence")?.is_empty()
            || item
                .get("data")
                .and_then(Value::as_object)
                .is_none_or(Map::is_empty)
        {
            return Err(RuntimeError::Contract(
                "run-ledger adapter-evidence payload is invalid".to_owned(),
            ));
        }
        let references = item
            .get("artifact_ids")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                RuntimeError::Contract(
                    "run-ledger adapter-evidence artifact ids are invalid".to_owned(),
                )
            })?;
        let mut unique = BTreeSet::new();
        for reference in references {
            let reference = reference
                .as_str()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| {
                    RuntimeError::Contract(
                        "run-ledger adapter-evidence artifact ids are invalid".to_owned(),
                    )
                })?;
            if !unique.insert(reference)
                || !artifact_keys.contains(&(attempt_id.to_owned(), reference.to_owned()))
            {
                return Err(RuntimeError::Contract(
                    "run-ledger adapter-evidence references unknown or duplicate artifact"
                        .to_owned(),
                ));
            }
        }
        evidence_statuses
            .entry(attempt_id.to_owned())
            .or_default()
            .insert(status.to_owned());
    }
    for (attempt_id, outcome_status) in outcome_statuses {
        let statuses = evidence_statuses
            .get(&attempt_id)
            .cloned()
            .unwrap_or_default();
        let conflicts = match outcome_status.as_str() {
            "completed" => statuses
                .iter()
                .any(|status| !matches!(status.as_str(), "passed" | "completed")),
            "partial" => statuses
                .iter()
                .any(|status| !matches!(status.as_str(), "passed" | "completed" | "partial")),
            "blocked" => statuses.contains("failed"),
            "failed" => !statuses.contains("failed"),
            _ => true,
        };
        if conflicts {
            return Err(RuntimeError::Contract(
                "run-ledger adapter-outcome conflicts with evidence status".to_owned(),
            ));
        }
        if outcome_status == "partial" && attempt_status(attempts, &attempt_id)? == "skipped" {
            let has_validation_gap = evidence.iter().any(|item| {
                item.as_object().is_some_and(|item| {
                    optional_str(item, "attempt_id") == Some(attempt_id.as_str())
                        && optional_str(item, "kind") == Some("validation")
                        && optional_str(item, "status") == Some("partial")
                        && item
                            .get("data")
                            .and_then(Value::as_object)
                            .and_then(|data| data.get("suggested_validation"))
                            .and_then(Value::as_str)
                            .is_some_and(|value| !value.is_empty())
                })
            });
            if !has_validation_gap {
                return Err(RuntimeError::Contract(
                    "run-ledger skipped partial outcome requires validation gap evidence"
                        .to_owned(),
                ));
            }
        }
    }
    Ok(())
}

fn validate_attempt_node_reference(
    value: &Map<String, Value>,
    attempts_by_id: &BTreeMap<String, String>,
    label: &str,
) -> Result<(), RuntimeError> {
    let attempt_id = required_str(value, "attempt_id", label)?;
    let node_id = required_str(value, "node_id", label)?;
    if attempts_by_id.get(attempt_id).map(String::as_str) != Some(node_id) {
        return Err(RuntimeError::Contract(format!(
            "{label} references unknown attempt or mismatched node"
        )));
    }
    Ok(())
}

fn attempt_status<'a>(attempts: &'a [Value], attempt_id: &str) -> Result<&'a str, RuntimeError> {
    attempts
        .iter()
        .find(|attempt| attempt.get("attempt_id").and_then(Value::as_str) == Some(attempt_id))
        .and_then(|attempt| attempt.get("status"))
        .and_then(Value::as_str)
        .ok_or_else(|| {
            RuntimeError::Contract("adapter outcome references unknown attempt".to_owned())
        })
}

fn require_exact_fields(
    object: &Map<String, Value>,
    fields: &[&str],
    label: &str,
) -> Result<(), RuntimeError> {
    let expected = fields.iter().copied().collect::<BTreeSet<_>>();
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    if actual != expected {
        return Err(RuntimeError::Contract(format!(
            "{label} fields are invalid"
        )));
    }
    Ok(())
}

fn validate_node_attempt(value: &Value) -> Result<(), RuntimeError> {
    let attempt = object(value, "node-attempt")?;
    if optional_str(attempt, "schema_version") != Some("1.0") {
        return Err(RuntimeError::Contract(
            "unsupported schema_version for node-attempt".to_owned(),
        ));
    }
    let attempt_number = required_u64(attempt, "attempt_number", "node-attempt")?;
    let timeout_seconds = required_u64(attempt, "timeout_seconds", "node-attempt")?;
    let _max_retries = required_u64(attempt, "max_retries", "node-attempt")?;
    if attempt_number < 1 || timeout_seconds == 0 {
        return Err(RuntimeError::Contract(
            "node-attempt retry or timeout metadata is invalid".to_owned(),
        ));
    }
    for field in ["attempt_id", "node_id", "deadline"] {
        if required_str(attempt, field, "node-attempt")?.is_empty() {
            return Err(RuntimeError::Contract(
                "node-attempt identity or deadline is invalid".to_owned(),
            ));
        }
    }
    let events = array(attempt, "events", "node-attempt")?;
    let first = events.first().ok_or_else(|| {
        RuntimeError::Contract("node-attempt must start with a pending creation event".to_owned())
    })?;
    let first = object(first, "node-attempt.event")?;
    if !first.get("from").is_some_and(Value::is_null)
        || optional_str(first, "to") != Some("pending")
    {
        return Err(RuntimeError::Contract(
            "node-attempt must start with a pending creation event".to_owned(),
        ));
    }
    let mut previous: Option<NodeStatus> = None;
    for (index, event) in events.iter().enumerate() {
        let event = object(event, "node-attempt.event")?;
        for field in ["at", "from", "to", "reason"] {
            if !event.contains_key(field) {
                return Err(RuntimeError::Contract(format!(
                    "node-attempt.event missing required fields: {field}"
                )));
            }
        }
        if required_str(event, "at", "node-attempt.event")?.is_empty()
            || required_str(event, "reason", "node-attempt.event")?.is_empty()
        {
            return Err(RuntimeError::Contract(
                "node-attempt event metadata is invalid".to_owned(),
            ));
        }
        let target = NodeStatus::parse(required_str(event, "to", "node-attempt.event")?)?;
        if index > 0 {
            let current = previous.ok_or_else(|| {
                RuntimeError::Contract("node-attempt event transition is invalid".to_owned())
            })?;
            if optional_str(event, "from") != Some(current.as_str())
                || !transition_allowed(current, target)
            {
                return Err(RuntimeError::Contract(
                    "node-attempt event transition is invalid".to_owned(),
                ));
            }
        }
        previous = Some(target);
    }
    if previous.map(NodeStatus::as_str) != Some(required_str(attempt, "status", "node-attempt")?) {
        return Err(RuntimeError::Contract(
            "node-attempt final event does not match status".to_owned(),
        ));
    }
    Ok(())
}

fn optional_object<'a>(
    value: Option<&'a Value>,
    label: &str,
) -> Result<Option<&'a Map<String, Value>>, RuntimeError> {
    value.map(|value| object(value, label)).transpose()
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, RuntimeError> {
    value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))
}

fn object_mut<'a>(
    value: &'a mut Value,
    label: &str,
) -> Result<&'a mut Map<String, Value>, RuntimeError> {
    value
        .as_object_mut()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))
}

fn array<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a Vec<Value>, RuntimeError> {
    object
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} must be an array")))
}

fn array_mut<'a>(
    object: &'a mut Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a mut Vec<Value>, RuntimeError> {
    object
        .get_mut(field)
        .and_then(Value::as_array_mut)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} must be an array")))
}

fn required_value<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a Value, RuntimeError> {
    object
        .get(field)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} missing required fields: {field}")))
}

fn required_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a str, RuntimeError> {
    required_value(object, field, label)?
        .as_str()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} must be a string")))
}

fn optional_str<'a>(object: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    object.get(field).and_then(Value::as_str)
}

fn contract_optional_hash<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<Option<&'a str>, RuntimeError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value))
            if value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')) =>
        {
            Ok(Some(value))
        }
        Some(_) => Err(RuntimeError::Contract(format!("{label} is invalid"))),
    }
}

fn contract_optional_ledger_hash<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<Option<&'a str>, RuntimeError> {
    if object.get(field).and_then(Value::as_str) == Some("") {
        return Ok(None);
    }
    contract_optional_hash(object, field, label)
}

fn required_u64(
    object: &Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<u64, RuntimeError> {
    required_value(object, field, label)?
        .as_u64()
        .ok_or_else(|| {
            RuntimeError::Contract(format!("{label} {field} must be a non-negative integer"))
        })
}

fn optional_u64(object: &Map<String, Value>, field: &str) -> Option<u64> {
    object.get(field).and_then(Value::as_u64)
}

fn optional_string_array(
    object: &Map<String, Value>,
    field: &str,
) -> Result<Vec<String>, RuntimeError> {
    let Some(value) = object.get(field) else {
        return Ok(Vec::new());
    };
    let values = value.as_array().ok_or_else(|| {
        RuntimeError::Contract(format!("workflow-plan.node {field} must be an array"))
    })?;
    values
        .iter()
        .map(|value| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                RuntimeError::Contract(format!(
                    "workflow-plan.node {field} entries must be strings"
                ))
            })
        })
        .collect()
}

fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => {
            value.as_i64().is_some_and(|value| value != 0)
                || value.as_u64().is_some_and(|value| value != 0)
                || value.as_f64().is_some_and(|value| value != 0.0)
        }
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn format_timestamp(epoch_micros: u64) -> String {
    let seconds = epoch_micros / 1_000_000;
    let micros = epoch_micros % 1_000_000;
    let days = i64::try_from(seconds / 86_400).unwrap_or(i64::MAX);
    let second_of_day = seconds % 86_400;
    let (year, month, day) = civil_from_days(days);
    let hour = second_of_day / 3_600;
    let minute = (second_of_day % 3_600) / 60;
    let second = second_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}+00:00")
}

fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan() -> Value {
        json!({
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "review"},
            ],
            "fingerprint": "plan",
            "missing_capabilities": [],
            "nodes": [
                {
                    "capability": "implementation.fixture",
                    "id": "a",
                    "idempotent": false,
                    "mandatory": true,
                    "max_retries": 0,
                    "provider": "fixture",
                    "resource_keys": ["repository"],
                    "timeout_seconds": 60
                },
                {
                    "capability": "verification.fixture",
                    "id": "b",
                    "idempotent": true,
                    "mandatory": true,
                    "max_retries": 1,
                    "provider": "fixture",
                    "resource_keys": ["build"],
                    "timeout_seconds": 60
                },
                {
                    "capability": "review.independent",
                    "id": "review",
                    "idempotent": false,
                    "mandatory": true,
                    "max_retries": 0,
                    "provider": "fixture",
                    "resource_keys": [],
                    "timeout_seconds": 60
                }
            ],
            "plan_id": "fixture",
            "schema_version": "1.0",
            "status": "ready",
        })
    }

    #[test]
    fn fake_runtime_retries_idempotent_failure_and_releases_resources() {
        let ledger = execute_fake_plan(
            &plan(),
            Some(&json!({"verification.fixture": ["failed", "passed"]})),
            None,
            None,
            None,
            false,
            Some("0123456789abcdef"),
        )
        .expect("runtime should complete");
        assert_eq!(ledger["final_status"], "completed");
        let attempts = ledger["node_attempts"]
            .as_array()
            .expect("attempts")
            .iter()
            .filter(|attempt| attempt["node_id"] == "b")
            .collect::<Vec<_>>();
        assert_eq!(attempts.len(), 2);
        assert_eq!(attempts[0]["status"], "failed");
        assert_eq!(attempts[1]["status"], "passed");
        assert_eq!(
            ledger["resource_events"]
                .as_array()
                .expect("events")
                .last()
                .expect("event")["action"],
            "released"
        );
    }

    #[test]
    fn fake_runtime_blocks_downstream_after_failure() {
        let ledger = execute_fake_plan(
            &plan(),
            Some(&json!({"implementation.fixture": "failed"})),
            None,
            None,
            None,
            false,
            Some("0123456789abcdef"),
        )
        .expect("runtime should finish blocked");
        assert_eq!(ledger["final_status"], "blocked");
        let statuses = ledger["node_attempts"]
            .as_array()
            .expect("attempts")
            .iter()
            .map(|attempt| {
                (
                    attempt["node_id"].as_str().expect("node").to_owned(),
                    attempt["status"].as_str().expect("status").to_owned(),
                )
            })
            .collect::<BTreeMap<_, _>>();
        assert_eq!(statuses["a"], "failed");
        assert_eq!(statuses["b"], "blocked");
        assert_eq!(statuses["review"], "blocked");
    }

    #[test]
    fn timestamp_conversion_matches_unix_epoch() {
        assert_eq!(format_timestamp(0), "1970-01-01T00:00:00.000000+00:00");
        assert_eq!(
            format_timestamp(1_704_067_200_123_456),
            "2024-01-01T00:00:00.123456+00:00"
        );
    }

    #[test]
    fn resume_reserves_all_projected_events_before_appending() {
        let error = reserve_ledger_events(99_999, 2).expect_err("budget must fail closed");
        assert!(
            error
                .to_string()
                .contains("cannot reserve 2 events within maximum 100000")
        );
        reserve_ledger_events(99_998, 2).expect("exact ledger boundary should be accepted");

        let mut blocked_plan = plan();
        blocked_plan["status"] = json!("blocked");
        let blocked_budget =
            runtime_event_budget(blocked_plan.as_object().expect("plan object")).expect("budget");
        let ready_plan = plan();
        let ready_budget =
            runtime_event_budget(ready_plan.as_object().expect("plan object")).expect("budget");
        assert_eq!(blocked_budget, ready_budget + 1);
    }
}
