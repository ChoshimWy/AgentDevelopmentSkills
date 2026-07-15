#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage:
  ./codex_verify.sh -- <xcodebuild args...>
  ./codex_verify.sh --repo-root <repo-root> -- <xcodebuild args...>
  ./codex_verify.sh --build-check <build-check.sh> <repo-root> [build-check args...]
  ./codex_verify.sh --force --build-check <build-check.sh> <repo-root> [build-check args...]
  ./codex_verify.sh --no-cache --build-check <build-check.sh> <repo-root> [build-check args...]
  ./codex_verify.sh --worktree-session-request <request.json> --build-check <build-check.sh> <repo-root> [build-check args...]
  ./codex_verify.sh --queue-status
  ./codex_verify.sh --queue-status --json
  ./codex_verify.sh --queue-doctor [--repair] [--delete-invalid]
  ~/.codex/bin/codex_verify --repo-root <repo-root> -- <xcodebuild args...>

Purpose:
  Route local project-environment validation into a shared build-queue daemon.
  The daemon executes queued jobs one by one and always uses the system
  DerivedData root: ~/Library/Developer/Xcode/DerivedData

Recommended:
  - Preferred: keep this script in the target Xcode project root as ./codex_verify.sh
  - Fallback: install it globally as ~/.codex/bin/codex_verify
  - Ask all agents to use one of the two entrypoints instead of裸跑 xcodebuild
  - Let iOSAgentSkills ios-verification delegate into the project wrapper first,
    then fall back to the global wrapper automatically
  - For targeted XCTest, prefer --build-check and pass only selectors/actions
    (for example -only-testing:Bundle/Class/test test); keep workspace,
    scheme, and destination selection in the script layer.

Notes:
  - Legacy public overrides XCODE_DERIVED_DATA_MODE / XCODE_DERIVED_DATA_SEED_MODE /
    XCODE_DERIVED_DATA_REFRESH / CODEX_DERIVED_DATA_SLOT are no longer supported.
  - The build-queue daemon is started automatically on first use.
  - Queue jobs are consumed only after an atomic ready marker and compatible
    queue/job schema have been published. Missing or unknown states are invalid,
    never implicitly queued.
  - --queue-doctor is read-only by default. --repair quarantines unsafe legacy
    records; add --delete-invalid only for explicit destructive cleanup.
  - Identical in-flight fingerprints attach to one queue job. Successful
    matching fingerprints are reused unless --force/--no-cache is set.
  - --worktree-session-request freezes the committed multi-Worktree source,
    destination, test plan and target identities. The wrapper validates it at
    submission and the daemon validates it again immediately before execution.
  - The wrapper prints a compact agent-summary.json by default. Set
    CODEX_VERIFY_STREAM_LOG=1 only when raw log streaming is explicitly needed.
EOF
}

die() {
  echo "[codex_verify] $*" >&2
  exit 1
}

timestamp_now() {
  date '+%Y-%m-%d %H:%M:%S %z'
}

seconds_now() {
  date '+%s'
}

trim_trailing_space() {
  sed 's/[[:space:]]*$//'
}

resolve_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve())
PY
}

absolute_path_without_resolving_symlinks() {
  python3 - "$1" <<'PY'
from pathlib import Path
import os, sys

print(os.path.abspath(Path(sys.argv[1]).expanduser()))
PY
}

resolve_worktree_session_helper() {
  local candidate
  for candidate in \
    "${CODEX_WORKTREE_SESSION_HELPER:-}" \
    "$SCRIPT_DIR/../skills/ios-verification/scripts/worktree_session.py" \
    "$HOME/.codex/skills/ios-verification/scripts/worktree_session.py"
  do
    [[ -n "$candidate" ]] || continue
    if [[ -f "$candidate" && ! -L "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

request_json_field() {
  local json_text="$1"
  local field_name="$2"
  printf '%s' "$json_text" | python3 -c '
import json, sys
value = json.load(sys.stdin)
field = value.get(sys.argv[1]) if isinstance(value, dict) else None
if not isinstance(field, str) or not field:
    raise SystemExit(65)
print(field)
' "$field_name"
}

validate_worktree_session_request() {
  local request_path="$1"
  local destination="$2"
  local test_plan="$3"
  local validation_root="${4:-$REPO_ROOT}"
  local helper
  local arguments
  helper="$(resolve_worktree_session_helper)" || die "Apple Worktree Session helper is unavailable"
  arguments=(validate-daemon-request --request "$request_path" --repo-root "$validation_root")
  if [[ -n "$destination" ]]; then
    arguments+=(--destination "$destination")
  fi
  if [[ -n "$test_plan" ]]; then
    arguments+=(--test-plan "$test_plan")
  fi
  PYTHONPATH="${PYTHONPATH:-}" python3 "$helper" "${arguments[@]}"
}

resolve_repo_entry_path() {
  python3 - "$REPO_ROOT" "$1" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
value = Path(sys.argv[2]).expanduser()
if not value.is_absolute():
    value = root / value
print(value.resolve())
PY
}

read_env_file_value() {
  local key="$1"
  [[ -f "$XCODE_ENV_FILE" ]] || return 0
  (
    set -a
    # shellcheck source=/dev/null
    source "$XCODE_ENV_FILE"
    eval 'printf "%s" "${'"$key"':-}"'
  )
}

env_or_file_value() {
  local key="$1"
  if [[ ${!key+x} == x ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  read_env_file_value "$key" || true
}

join_quoted_command() {
  local out=''
  local part
  for part in "$@"; do
    out+=$(printf '%q ' "$part")
  done
  printf '%s' "$out" | trim_trailing_space
}

sanitize_token() {
  local value="$1"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  value="$(printf '%s' "$value" | tr -cs '[:alnum:]._-+' '-')"
  value="${value#-}"
  value="${value%-}"
  if [[ -z "$value" ]]; then
    value='unknown'
  fi
  printf '%s' "$value"
}

queue_root() {
  printf '%s' "${CODEX_BUILD_QUEUE_ROOT:-/tmp/codex-build-queue}"
}

compute_request_fingerprint() {
  python3 - "$REPO_ROOT" "$MODE" "$META_WORKSPACE" "$META_PROJECT" "$META_SCHEME" "$META_CONFIGURATION" "$META_DESTINATION" "$META_ACTION" "$COMMAND_PREVIEW" "${WORKTREE_SESSION_REQUEST_SHA256:-}" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()


identity_errors = []


def git(*args: str, text: bool = False):
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=text, check=False)
    if completed.returncode:
        identity_errors.append(f"git {' '.join(args)}")
        return "" if text else b""
    return completed.stdout


def tool_output(*args: str) -> str:
    completed = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode:
        identity_errors.append(" ".join(args))
        return ""
    return completed.stdout.strip()


status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
untracked = []
for entry in status.split(b"\0"):
    if not entry.startswith(b"?? "):
        continue
    raw = entry[3:].decode("utf-8", errors="surrogateescape")
    path = (root / raw).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        continue
    untracked.append({
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
    })

config_inputs = []
tracked_config = git(
    "ls-files",
    "-z",
    "--",
    "Package.resolved",
    "**/Package.resolved",
    "Podfile.lock",
    "**/Podfile.lock",
    "Pods/Manifest.lock",
    "**/*.xcconfig",
)
config_paths = {
    entry.decode("utf-8", errors="surrogateescape")
    for entry in tracked_config.split(b"\0")
    if entry
}
if (root / ".codex/xcodebuild.env").is_file():
    config_paths.add(".codex/xcodebuild.env")
digest_script = os.environ.get("CODEX_XCODEBUILD_DIGEST_SCRIPT", "")
if digest_script:
    digest_path = Path(digest_script).expanduser().resolve()
    if digest_path.is_file():
        config_inputs.append({"path": str(digest_path), "sha256": hashlib.sha256(digest_path.read_bytes()).hexdigest()})
for raw in sorted(config_paths):
    path = (root / raw).resolve()
    if not path.is_file():
        continue
    config_inputs.append({"path": raw, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

payload = {
    "repo_root": str(root),
    "head": git("rev-parse", "HEAD", text=True).strip(),
    "tracked_diff_sha256": hashlib.sha256(git("diff", "--binary", "HEAD")).hexdigest(),
    "untracked": sorted(untracked, key=lambda item: item["path"]),
    "configuration_inputs": config_inputs,
    "mode": sys.argv[2],
    "workspace": sys.argv[3],
    "project": sys.argv[4],
    "scheme": sys.argv[5],
    "configuration": sys.argv[6],
    "destination": sys.argv[7],
    "action": sys.argv[8],
    "command": sys.argv[9],
    "worktree_session_request_sha256": sys.argv[10],
    "developer_dir": os.environ.get("DEVELOPER_DIR", ""),
    "xcode_environment": {key: value for key, value in os.environ.items() if key.startswith("XCODE_")},
    "xcode_version": tool_output("xcodebuild", "-version"),
    "sdk_version": tool_output("xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"),
}
encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
fingerprint = hashlib.sha256(encoded).hexdigest()
if identity_errors:
    nonce = hashlib.sha256(f"{time.time_ns()}:{os.getpid()}".encode()).hexdigest()[:16]
    print(f"volatile-{fingerprint}-{nonce}")
else:
    print(fingerprint)
PY
}

job_state() {
  local job_dir="$1"
  if [[ -f "$job_dir/state" ]]; then
    tr -d '\r\n' <"$job_dir/state"
  else
    printf '%s' 'invalid'
  fi
}

set_job_state() {
  local job_dir="$1"
  local state="$2"
  local temporary="$job_dir/.state.$$.$RANDOM.tmp"
  printf '%s\n' "$state" >"$temporary"
  mv "$temporary" "$job_dir/state"
}

ensure_queue_metadata() {
  python3 - "$QUEUE_ROOT" "$QUEUE_SCHEMA_VERSION" "$QUEUE_GENERATION_ID" <<'PY'
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
expected = {
    "generation_id": sys.argv[3],
    "producer": "codex_verify",
    "schema_version": sys.argv[2],
}
canonical = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
if root.is_symlink():
    raise SystemExit("queue root is unsafe")
root.mkdir(parents=True, exist_ok=True)
path = root / "queue-meta.json"
if path.is_symlink():
    raise SystemExit("queue metadata is unsafe")
if path.exists():
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"queue metadata is invalid: {error}")
    if value != expected or raw != canonical:
        raise SystemExit("queue schema or generation is incompatible")
else:
    unsafe: list[str] = []
    pid_path = root / "daemon.pid"
    if pid_path.is_file() and not pid_path.is_symlink():
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        if raw_pid.isdigit():
            try:
                os.kill(int(raw_pid), 0)
                unsafe.append("live legacy daemon")
            except OSError:
                pass
    if (root / "active_job").exists() or (root / "active_job").is_symlink():
        unsafe.append("legacy active_job record")
    staging = root / "staging"
    if staging.is_dir() and any(staging.iterdir()):
        unsafe.append("legacy staging entries")
    jobs = root / "jobs"
    if jobs.is_dir():
        for job in jobs.iterdir():
            if job.is_symlink() or not job.is_dir():
                unsafe.append("unsafe legacy job entry")
                break
            state_path = job / "state"
            try:
                state = state_path.read_text(encoding="utf-8").strip() if state_path.is_file() and not state_path.is_symlink() else ""
            except (OSError, UnicodeError):
                state = ""
            if state not in {"succeeded", "failed"}:
                unsafe.append("legacy nonterminal or invalid job")
                break
    if unsafe:
        raise SystemExit(
            "queue metadata is missing while legacy runtime state exists: "
            + ", ".join(sorted(set(unsafe)))
            + "; run --queue-doctor --repair before publishing new work"
        )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(canonical, encoding="utf-8")
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    if path.read_text(encoding="utf-8") != canonical:
        raise SystemExit("queue schema or generation changed during initialization")
PY
}

queue_maintenance() {
  local operation="$1"
  local invalid_policy="${2:-${CODEX_VERIFY_QUEUE_INVALID_POLICY:-quarantine}}"
  python3 - "$QUEUE_ROOT" "$QUEUE_SCHEMA_VERSION" "$QUEUE_GENERATION_ID" \
    "${CODEX_VERIFY_QUEUE_TTL_SECONDS:-86400}" "$operation" "$invalid_policy" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time

root = Path(sys.argv[1])
schema_version = sys.argv[2]
generation_id = sys.argv[3]
try:
    ttl_seconds = int(sys.argv[4])
except ValueError:
    raise SystemExit("CODEX_VERIFY_QUEUE_TTL_SECONDS must be an integer")
operation = sys.argv[5]
invalid_policy = sys.argv[6]
if operation not in {"doctor", "repair", "startup"}:
    raise SystemExit("invalid queue maintenance operation")
if invalid_policy not in {"quarantine", "delete"}:
    raise SystemExit("invalid queue repair policy")
if ttl_seconds <= 0:
    raise SystemExit("CODEX_VERIFY_QUEUE_TTL_SECONDS must be positive")

mutate = operation in {"repair", "startup"}
jobs = root / "jobs"
staging = root / "staging"
slots = root / "derived-data-slots"
quarantine = root / "quarantine"
metadata_path = root / "queue-meta.json"
now = int(time.time())
timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
issues: list[dict[str, object]] = []
actions: list[dict[str, object]] = []


def canonical_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def pid_alive(raw: str | None) -> bool:
    if raw is None or not raw.isdigit():
        return False
    try:
        os.kill(int(raw), 0)
        return True
    except (OSError, ValueError):
        return False


def canonical_json(path: Path) -> dict[str, object] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        return value if isinstance(value, dict) and raw == canonical else None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def controlled_job(raw: str | None) -> Path | None:
    if not raw:
        return None
    candidate = Path(raw)
    try:
        if not candidate.is_absolute() or candidate.is_symlink() or candidate.parent.resolve() != jobs.resolve():
            return None
    except OSError:
        return None
    return candidate if candidate.is_dir() else None


def daemon_identity() -> dict[str, object] | None:
    owner = canonical_json(root / "daemon-owner.json")
    heartbeat = canonical_json(root / "daemon-heartbeat.json")
    raw_pid = text(root / "daemon.pid")
    if owner is None or heartbeat is None or raw_pid is None or not raw_pid.isdigit():
        return None
    identity = {
        "generation_id": generation_id,
        "pid": int(raw_pid),
        "queue_root": str(root),
        "schema_version": "1.0",
        "token": owner.get("token"),
    }
    if owner != identity or not isinstance(identity["token"], str) or len(identity["token"]) != 64:
        return None
    updated = heartbeat.get("updated_at_epoch")
    if heartbeat.get("identity") != identity or not isinstance(updated, int) or abs(now - updated) > 15:
        return None
    return identity if pid_alive(raw_pid) else None


def record(code: str, path: Path, detail: str) -> None:
    issues.append({"code": code, "detail": detail, "path": str(path)})


def remove_or_quarantine(path: Path, reason: str, category: str) -> None:
    if invalid_policy == "delete":
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
        actions.append({"action": "deleted", "category": category, "path": str(path), "reason": reason})
        return
    destination_root = quarantine / timestamp / category
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / path.name
    suffix = 0
    while destination.exists() or destination.is_symlink():
        suffix += 1
        destination = destination_root / f"{path.name}-{suffix}"
    os.replace(path, destination)
    canonical_write(
        destination_root / f"{destination.name}.quarantine.json",
        {
            "original_path": str(path),
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "schema_version": "1.0",
        },
    )
    actions.append(
        {"action": "quarantined", "category": category, "path": str(path), "reason": reason, "target": str(destination)}
    )


live_daemon_identity = daemon_identity()
if mutate and live_daemon_identity is not None:
    print(json.dumps({
        "actions": [], "daemon": {"identity": live_daemon_identity, "running": True},
        "generation_id": generation_id, "healthy": False,
        "issues": [{"code": "repair-refused-daemon-running", "detail": "stop the validated daemon before mutating queue history", "path": str(root)}],
        "queue_root": str(root), "schema_version": schema_version,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    raise SystemExit(75)
raw_daemon_pid = text(root / "daemon.pid")
if mutate and pid_alive(raw_daemon_pid) and live_daemon_identity is None:
    print(json.dumps({
        "actions": [], "daemon": {"pid": raw_daemon_pid, "running": False},
        "generation_id": generation_id, "healthy": False,
        "issues": [{"code": "repair-refused-untrusted-live-pid", "detail": "a live PID exists without a valid daemon generation/token/heartbeat identity", "path": str(root / "daemon.pid")}],
        "queue_root": str(root), "schema_version": schema_version,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    raise SystemExit(75)


if mutate:
    root.mkdir(parents=True, exist_ok=True)
    jobs.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    slots.mkdir(parents=True, exist_ok=True)

expected_metadata = {
    "generation_id": generation_id,
    "producer": "codex_verify",
    "schema_version": schema_version,
}
metadata: object | None = None
if metadata_path.is_symlink():
    record("queue-metadata-unsafe", metadata_path, "queue metadata must not be a symlink")
elif metadata_path.is_file():
    try:
        metadata_raw = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        record("queue-metadata-invalid", metadata_path, "queue metadata is not canonical JSON")
    if metadata is not None:
        expected_raw = json.dumps(expected_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        if metadata != expected_metadata:
            record("queue-metadata-incompatible", metadata_path, "queue schema or generation is incompatible")
        elif metadata_raw != expected_raw:
            record("queue-metadata-invalid", metadata_path, "queue metadata is not canonical JSON")
else:
    record("queue-metadata-missing", metadata_path, "queue metadata is missing")
    if mutate:
        canonical_write(metadata_path, expected_metadata)
        actions.append({"action": "initialized", "category": "queue", "path": str(metadata_path)})

incompatible = any(item["code"] in {"queue-metadata-unsafe", "queue-metadata-invalid", "queue-metadata-incompatible"} for item in issues)
if incompatible and mutate:
    report = {
        "actions": actions,
        "generation_id": generation_id,
        "healthy": False,
        "issues": issues,
        "queue_root": str(root),
        "schema_version": schema_version,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    raise SystemExit(65)


required_files = (
    "mode", "repo_root", "created_at_epoch", "command.args0", "request_fingerprint",
    "request_fingerprint_cacheable", "ready", "job_schema_version", "queue_generation_id",
    "job-manifest.json", "job_manifest_sha256",
)


def publication_contract_error(job: Path) -> str | None:
    import hashlib
    import stat
    manifest_path = job / "job-manifest.json"
    digest_path = job / "job_manifest_sha256"
    try:
        if manifest_path.is_symlink() or digest_path.is_symlink():
            return "publication manifest is unsafe"
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest_path.read_text(encoding="utf-8").strip():
            return "publication manifest digest changed"
        manifest = json.loads(raw)
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if raw != canonical or manifest.get("schema_version") != "1.0" or not isinstance(manifest.get("files"), list):
            return "publication manifest is invalid"
        seen: set[str] = set()
        for item in manifest["files"]:
            if not isinstance(item, dict) or set(item) != {"mode", "path", "sha256", "size"}:
                return "publication manifest inventory is invalid"
            name = item["path"]
            if not isinstance(name, str) or not name or name in seen or Path(name).is_absolute() or ".." in Path(name).parts:
                return "publication manifest path is invalid"
            seen.add(name)
            path = job / name
            if path.is_symlink() or not path.is_file():
                return f"published sidecar is missing or unsafe: {name}"
            data = path.read_bytes()
            mode = path.stat().st_mode & 0o777
            if item != {"mode": mode, "path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}:
                return f"published sidecar changed: {name}"
        if "command.args0" not in seen:
            return "command.args0 is absent from publication manifest"
        args_raw = (job / "command.args0").read_bytes()
        if not args_raw or not args_raw.endswith(b"\0"):
            return "command.args0 is not NUL terminated"
        parts = args_raw[:-1].split(b"\0")
        if not parts or any(not part for part in parts):
            return "command.args0 contains an empty argument"
        args = [part.decode("utf-8") for part in parts]
        mode_name = text(job / "mode")
        if mode_name == "build-check" and (len(args) < 3 or args[0] != "bash"):
            return "build-check command shape is invalid"
        if mode_name == "xcodebuild" and (not args or args[0] != "xcodebuild"):
            return "xcodebuild command shape is invalid"
        if mode_name not in {"build-check", "xcodebuild"}:
            return "job mode is invalid"
    except (OSError, UnicodeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return "publication manifest cannot be validated"
    return None


def queued_contract_error(job: Path) -> str | None:
    for name in required_files:
        candidate = job / name
        if candidate.is_symlink() or not candidate.is_file():
            return f"required publication file is missing or unsafe: {name}"
    if text(job / "ready") != "true":
        return "ready marker is not true"
    if text(job / "job_schema_version") != schema_version:
        return "job schema version is incompatible"
    if text(job / "queue_generation_id") != generation_id:
        return "job queue generation is incompatible"
    publication_error = publication_contract_error(job)
    if publication_error is not None:
        return publication_error
    epoch = text(job / "created_at_epoch")
    if epoch is None or not epoch.isdigit():
        return "created_at_epoch is invalid"
    age = now - int(epoch)
    if age > ttl_seconds:
        return f"queued job expired after {age} seconds"
    if age < -300:
        return "queued job creation time is in the future"
    if (job / "command.args0").stat().st_size == 0:
        return "command.args0 is empty"
    repo_root = text(job / "repo_root")
    if repo_root is None or not Path(repo_root).is_absolute():
        return "repo_root is not absolute"
    if (job / "worktree-session-request.json").exists():
        for name in (
            "worktree_session_request_sha256", "worktree_session_id", "worktree_session_source_identity",
            "worktree_session_derived_data_slot", "worktree_session_test_plan", "worktree_session_artifact_namespace",
        ):
            candidate = job / name
            if candidate.is_symlink() or not candidate.is_file():
                return f"Worktree Session publication file is missing or unsafe: {name}"
    return None


daemon_pid_raw = text(root / "daemon.pid")
daemon_owner = daemon_identity()
daemon_is_alive = daemon_owner is not None
if pid_alive(daemon_pid_raw) and not daemon_is_alive:
    record("daemon-identity-invalid", root / "daemon.pid", "live PID is not bound to a valid generation/token/heartbeat identity")
active_path = root / "active_job"
active_raw = text(active_path)
active_job = controlled_job(active_raw)
if active_raw and active_job is None:
    record("active-job-unsafe", active_path, "active_job must identify a direct real child of the queue jobs directory")

state_counts: dict[str, int] = {}
job_entries = sorted(jobs.iterdir(), key=lambda item: item.name) if jobs.is_dir() else []
for job in job_entries:
    if job.is_symlink() or not job.is_dir():
        record("job-entry-unsafe", job, "job entry must be a real directory")
        if mutate:
            remove_or_quarantine(job, "job entry must be a real directory", "jobs")
        continue
    state = text(job / "state")
    normalized = state if state in {"queued", "running", "succeeded", "failed"} else "invalid"
    state_counts[normalized] = state_counts.get(normalized, 0) + 1
    if state is None:
        reason = "job state is missing or unsafe"
        record("job-state-missing", job, reason)
        if mutate:
            remove_or_quarantine(job, reason, "jobs")
    elif state not in {"queued", "running", "succeeded", "failed"}:
        reason = f"unknown job state: {state}"
        record("job-state-unknown", job, reason)
        if mutate:
            remove_or_quarantine(job, reason, "jobs")
    elif state == "queued":
        reason = queued_contract_error(job)
        if reason is not None:
            record("queued-job-invalid", job, reason)
            if mutate:
                remove_or_quarantine(job, reason, "jobs")
    elif state == "running":
        publication_error = publication_contract_error(job)
        owned = (
            daemon_is_alive and active_job == job and publication_error is None
            and text(job / "runner_pid") == str(daemon_owner["pid"])
            and text(job / "runner_token") == daemon_owner["token"]
        )
        if owned:
            continue
        reason = "running job has no live daemon owner during reconciliation"
        record("running-job-interrupted", job, reason)
        if mutate:
            (job / "job.log").open("a", encoding="utf-8").write("[codex_verify] job interrupted during queue reconciliation\n")
            (job / "exit_code").write_text("70\n", encoding="utf-8")
            (job / "finished_at").write_text(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z") + "\n", encoding="utf-8")
            (job / "state").write_text("failed\n", encoding="utf-8")
            actions.append({"action": "failed", "category": "jobs", "path": str(job), "reason": reason})

staging_entries = sorted(staging.iterdir(), key=lambda item: item.name) if staging.is_dir() else []
for entry in staging_entries:
    try:
        age = now - int(entry.lstat().st_mtime)
    except OSError:
        continue
    if age <= ttl_seconds:
        continue
    reason = f"staging entry expired after {age} seconds"
    record("staging-entry-stale", entry, reason)
    if mutate:
        remove_or_quarantine(entry, reason, "staging")

for lease in (sorted(slots.glob("**/lease.lockdir")) if slots.is_dir() else []):
    owner_raw = text(lease / "job_dir")
    owner = controlled_job(owner_raw)
    if owner_raw and owner is None:
        record("slot-lease-owner-unsafe", lease, "slot lease owner must be a direct real child of the queue jobs directory")
    owner_running = owner is not None and text(owner / "state") == "running" and daemon_is_alive and active_job == owner
    if owner_running:
        continue
    reason = "slot lease has no running owner"
    record("slot-lease-stale", lease, reason)
    if mutate:
        shutil.rmtree(lease)
        actions.append({"action": "deleted", "category": "leases", "path": str(lease), "reason": reason})

for path_name, code in (("daemon.pid", "daemon-pid-stale"),):
    path = root / path_name
    if path.exists() and not pid_alive(text(path)):
        reason = "recorded process is not running"
        record(code, path, reason)
        if mutate:
            path.unlink(missing_ok=True)
            actions.append({"action": "deleted", "category": "runtime", "path": str(path), "reason": reason})

active = active_path
if active.exists():
    if active_job is None or text(active_job / "state") != "running" or not daemon_is_alive:
        reason = "active_job does not identify a running job"
        record("active-job-stale", active, reason)
        if mutate:
            active.unlink(missing_ok=True)
            actions.append({"action": "deleted", "category": "runtime", "path": str(active), "reason": reason})

start_lock = root / "start.lockdir"
if start_lock.exists() and not pid_alive(text(start_lock / "owner.pid")):
    reason = "start lock owner is not running"
    record("start-lock-stale", start_lock, reason)
    if mutate:
        shutil.rmtree(start_lock)
        actions.append({"action": "deleted", "category": "runtime", "path": str(start_lock), "reason": reason})

report = {
    "actions": actions,
    "daemon": {"identity": daemon_owner, "pid": text(root / "daemon.pid"), "running": daemon_is_alive},
    "generation_id": generation_id,
    "healthy": not issues if not mutate else not incompatible,
    "issues": issues,
    "queue_root": str(root),
    "repaired": mutate and bool(actions),
    "schema_version": schema_version,
    "state_counts": state_counts,
}
print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
if operation == "doctor" and issues:
    raise SystemExit(2)
PY
}

job_ready_for_execution() {
  local job_dir="$1"
  local created_at_epoch age ttl_seconds required
  [[ "$(job_state "$job_dir")" == 'queued' ]] || return 1
  validate_job_publication "$job_dir" >/dev/null 2>&1 || return 1
  [[ -f "$job_dir/ready" && ! -L "$job_dir/ready" && "$(cat "$job_dir/ready")" == 'true' ]] || return 1
  [[ -f "$job_dir/job_schema_version" && ! -L "$job_dir/job_schema_version" ]] || return 1
  [[ "$(cat "$job_dir/job_schema_version")" == "$QUEUE_SCHEMA_VERSION" ]] || return 1
  [[ -f "$job_dir/queue_generation_id" && ! -L "$job_dir/queue_generation_id" ]] || return 1
  [[ "$(cat "$job_dir/queue_generation_id")" == "$QUEUE_GENERATION_ID" ]] || return 1
  for required in mode repo_root created_at_epoch command.args0 request_fingerprint request_fingerprint_cacheable; do
    [[ -f "$job_dir/$required" && ! -L "$job_dir/$required" ]] || return 1
  done
  [[ -s "$job_dir/command.args0" ]] || return 1
  created_at_epoch="$(cat "$job_dir/created_at_epoch")"
  [[ "$created_at_epoch" =~ ^[0-9]+$ ]] || return 1
  ttl_seconds="${CODEX_VERIFY_QUEUE_TTL_SECONDS:-86400}"
  [[ "$ttl_seconds" =~ ^[0-9]+$ && "$ttl_seconds" -gt 0 ]] || return 1
  age=$(( $(seconds_now) - created_at_epoch ))
  [[ "$age" -le "$ttl_seconds" && "$age" -ge -300 ]] || return 1
}

write_text_file() {
  local target="$1"
  local body="$2"
  printf '%s\n' "$body" >"$target"
}

write_args_file() {
  local target="$1"
  shift
  : >"$target"
  while [[ $# -gt 0 ]]; do
    printf '%s\0' "$1" >>"$target"
    shift
  done
}

write_job_publication_manifest() {
  local job_dir="$1"
  python3 - "$job_dir" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

job = Path(sys.argv[1])
names = [
    "job_schema_version", "queue_generation_id", "mode", "repo_root", "created_at", "created_at_epoch",
    "submitter.txt", "workspace", "project", "scheme", "configuration", "destination", "action",
    "command_preview", "request_fingerprint", "request_fingerprint_cacheable", "log_path", "env.sh",
    "command.args0", "xcode-entry-sanity.json",
]
if (job / "worktree-session-request.json").is_file():
    names.extend([
        "worktree-session-request.json", "worktree_session_request_sha256", "worktree_session_id",
        "worktree_session_source_identity", "worktree_session_derived_data_slot", "worktree_session_test_plan",
        "worktree_session_artifact_namespace",
    ])


def snapshot(name: str) -> dict[str, object]:
    path = job / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"publication input is missing or unsafe: {name}")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"publication input is not regular: {name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        opened = os.fstat(stream.fileno())
    after = path.lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise SystemExit(f"publication input changed while hashing: {name}")
    return {"mode": opened.st_mode & 0o777, "path": name, "sha256": digest.hexdigest(), "size": opened.st_size}


payload = {"files": [snapshot(name) for name in sorted(names)], "schema_version": "1.0"}
raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
manifest = job / "job-manifest.json"
manifest.write_text(raw, encoding="utf-8")
(job / "job_manifest_sha256").write_text(hashlib.sha256(raw.encode("utf-8")).hexdigest() + "\n", encoding="utf-8")
PY
}

validate_job_publication() {
  local job_dir="$1"
  python3 - "$job_dir" "$QUEUE_SCHEMA_VERSION" "$QUEUE_GENERATION_ID" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

job = Path(sys.argv[1])
schema = sys.argv[2]
generation = sys.argv[3]
if job.is_symlink() or not job.is_dir():
    raise SystemExit(1)
for name in ("job-manifest.json", "job_manifest_sha256", "job_schema_version", "queue_generation_id", "ready"):
    path = job / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(1)
if (job / "ready").read_text(encoding="utf-8").strip() != "true":
    raise SystemExit(1)
if (job / "job_schema_version").read_text(encoding="utf-8").strip() != schema:
    raise SystemExit(1)
if (job / "queue_generation_id").read_text(encoding="utf-8").strip() != generation:
    raise SystemExit(1)
raw = (job / "job-manifest.json").read_bytes()
if hashlib.sha256(raw).hexdigest() != (job / "job_manifest_sha256").read_text(encoding="utf-8").strip():
    raise SystemExit(1)
try:
    manifest = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit(1)
if manifest.get("schema_version") != "1.0" or not isinstance(manifest.get("files"), list):
    raise SystemExit(1)
canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
if raw != canonical:
    raise SystemExit(1)
seen = set()
for item in manifest["files"]:
    if not isinstance(item, dict) or set(item) != {"mode", "path", "sha256", "size"}:
        raise SystemExit(1)
    name = item["path"]
    if not isinstance(name, str) or not name or name in seen or Path(name).is_absolute() or ".." in Path(name).parts:
        raise SystemExit(1)
    seen.add(name)
    path = job / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(1)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(1)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        opened = os.fstat(stream.fileno())
    after = path.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise SystemExit(1)
    if item != {"mode": opened.st_mode & 0o777, "path": name, "sha256": digest.hexdigest(), "size": opened.st_size}:
        raise SystemExit(1)

args_path = job / "command.args0"
if "command.args0" not in seen:
    raise SystemExit(1)
args_raw = args_path.read_bytes()
if not args_raw or not args_raw.endswith(b"\0"):
    raise SystemExit(1)
parts = args_raw[:-1].split(b"\0")
if not parts or any(not part for part in parts):
    raise SystemExit(1)
try:
    args = [part.decode("utf-8") for part in parts]
except UnicodeDecodeError:
    raise SystemExit(1)
mode = (job / "mode").read_text(encoding="utf-8").strip()
if mode == "build-check":
    if len(args) < 3 or args[0] != "bash":
        raise SystemExit(1)
elif mode == "xcodebuild":
    if not args or args[0] != "xcodebuild":
        raise SystemExit(1)
else:
    raise SystemExit(1)
PY
}

write_xcode_entry_sanity() {
  local job_dir="$1"
  python3 - "$job_dir" "$REPO_ROOT" "$META_WORKSPACE" "$META_PROJECT" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

job_dir = Path(sys.argv[1])
repo_root = Path(sys.argv[2]).resolve()
workspace = sys.argv[3].strip()
project = sys.argv[4].strip()


def non_auto(value: str) -> str:
    return "" if value in {"", "auto", "Debug(auto)", "build(auto)"} else value


def normalize(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def rel(path: Path) -> str | None:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return None


def entry_payload(value: str, kind: str) -> dict[str, object] | None:
    value = non_auto(value)
    if not value:
        return None
    path = normalize(value)
    payload: dict[str, object] = {
        "kind": kind,
        "configured_value": value,
        "absolute_path": str(path),
        "relative_path": rel(path),
        "cwd": str(repo_root),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
    }
    if kind == "workspace":
        contents = path / "contents.xcworkspacedata"
        payload["contents_xcworkspacedata_exists"] = contents.exists()
        payload["usable_by_cli_sanity"] = path.is_dir() and contents.exists()
    elif kind == "project":
        project_file = path / "project.pbxproj"
        payload["project_pbxproj_exists"] = project_file.exists()
        payload["usable_by_cli_sanity"] = path.is_dir() and project_file.exists()
    return payload


payload = {
    "cwd": str(repo_root),
    "workspace": entry_payload(workspace, "workspace"),
    "project": entry_payload(project, "project"),
}
(job_dir / "xcode-entry-sanity.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
}

READ_ARGS_RESULT=()
read_args_file() {
  local target="$1"
  local value=''
  READ_ARGS_RESULT=()
  while IFS= read -r -d '' value; do
    READ_ARGS_RESULT+=("$value")
  done <"$target"
}

append_log_line() {
  local target="$1"
  local line="$2"
  printf '%s\n' "$line" >>"$target"
}

resolve_digest_script() {
  local candidate
  for candidate in \
    "${CODEX_XCODEBUILD_DIGEST_SCRIPT:-}" \
    "$REPO_ROOT/tools/digest-xcodebuild-log.sh" \
    "$SCRIPT_DIR/digest-xcodebuild-log" \
    "$SCRIPT_DIR/digest-xcodebuild-log.sh" \
    "$HOME/.codex/bin/digest-xcodebuild-log"
  do
    [[ -n "$candidate" ]] || continue
    if [[ -f "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

write_minimal_verification_report() {
  local job_dir="$1"
  local status="$2"
  python3 - "$job_dir" "$status" <<'PY'
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

job_dir = Path(sys.argv[1])
status_code = int(sys.argv[2])
raw_log = job_dir / "job.log"
diagnostics_path = job_dir / "diagnostics.json"
summary_path = job_dir / "build-summary.txt"
report_path = job_dir / "verification-report.json"

text = raw_log.read_text(encoding="utf-8", errors="replace") if raw_log.exists() else ""
fingerprint = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
state = "passed" if status_code == 0 else "failed"
summary = "Verification succeeded." if status_code == 0 else "Verification failed; no digest script was available, inspect build-summary.txt before raw logs."
generated_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

diagnostics = {
    "status": state,
    "mode": (job_dir / "mode").read_text().strip() if (job_dir / "mode").exists() else "auto",
    "fingerprint": fingerprint,
    "cached": False,
    "summary": summary,
    "finished_at": generated_at,
    "diagnostics": [],
    "first_blocking_error": None,
    "failed_tests": [],
    "warnings_count": 0,
    "artifacts": {
        "diagnostics_json": str(diagnostics_path),
        "build_summary": str(summary_path),
        "verification_report": str(report_path),
        "raw_log": str(raw_log),
    },
    "next_action": "Read build-summary.txt. Only inspect the raw log if compact evidence is insufficient.",
    "raw_log_policy": "forbidden_by_default",
    "needs_raw_log": status_code != 0,
}
diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
summary_path.write_text(summary + "\n", encoding="utf-8")
report = {
    "status": state,
    "mode": diagnostics["mode"],
    "fingerprint": fingerprint,
    "cached": False,
    "summary": summary,
    "first_blocking_error": None,
    "failed_tests": [],
    "warnings_count": 0,
    "artifact_paths": diagnostics["artifacts"],
    "suggested_next_action": diagnostics["next_action"],
    "raw_log_policy": "forbidden_by_default",
    "needs_raw_log": status_code != 0,
    "generated_at": generated_at,
}
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

write_agent_summary() {
  local job_dir="$1"
  python3 - "$job_dir" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

job_dir = Path(sys.argv[1])
report_path = job_dir / "verification-report.json"
summary_path = job_dir / "agent-summary.json"


def read_text(name: str) -> str:
    path = job_dir / name
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""


def load_report() -> dict:
    if not report_path.exists():
        return {
            "status": "blocked",
            "summary": "verification-report.json missing",
            "artifact_paths": {"raw_log": str(job_dir / "job.log")},
            "needs_raw_log": False,
        }
    try:
        value = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("top-level value must be an object")
        return value
    except Exception as exc:
        return {
            "status": "blocked",
            "summary": f"verification-report.json unreadable: {exc}",
            "artifact_paths": {"verification_report": str(report_path), "raw_log": str(job_dir / "job.log")},
            "needs_raw_log": False,
        }


def load_json_file(name: str) -> dict:
    path = job_dir / name
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def extract_only_testing(command_preview: str) -> list[str]:
    try:
        parts = shlex.split(command_preview)
    except ValueError:
        parts = command_preview.split()
    selectors: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part.startswith("-only-testing:"):
            selector = part.split(":", 1)[1]
            if selector:
                selectors.append(selector)
        elif part == "-only-testing" and index + 1 < len(parts):
            selectors.append(parts[index + 1])
            index += 1
        index += 1
    return selectors


def destination_type(destination: str) -> str:
    normalized = destination.lower()
    if not normalized:
        return "unknown"
    if "simulator" in normalized:
        return "simulator"
    if "generic/platform=ios" in normalized:
        return "generic_ios"
    if normalized.startswith("id="):
        return "physical_device"
    if "macos" in normalized:
        return "macos"
    return "unknown"


def verification_level(action: str, only_testing: list[str], report: dict) -> str:
    ui_smoke = report.get("ui_smoke")
    if isinstance(ui_smoke, dict) and ui_smoke.get("executed"):
        return "ui"
    if only_testing:
        return "unit"
    normalized = action.lower()
    if normalized in {"test", "test-without-building"}:
        return "unit"
    if normalized in {"archive", "exportarchive"}:
        return "full"
    return "build"


def non_auto(value: object) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "auto", "Debug(auto)", "build(auto)"} else text


def workspace_candidates(repo_root: Path) -> list[Path]:
    return sorted(
        path
        for path in repo_root.rglob("*.xcworkspace")
        if "Pods" not in path.parts
        and "project.xcworkspace" not in str(path)
        and len(path.relative_to(repo_root).parts) <= 3
    )


def project_candidates(repo_root: Path) -> list[Path]:
    return sorted(
        path
        for path in repo_root.rglob("*.xcodeproj")
        if "Pods" not in path.parts and len(path.relative_to(repo_root).parts) <= 3
    )


def scheme_paths(repo_root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for path in sorted(repo_root.rglob("*.xcscheme")):
        if "Pods" in path.parts:
            continue
        paths.setdefault(path.stem, path)
    return paths


def is_ui_test_name(name: str) -> bool:
    return bool(re.search(r"(?:^|[_-])UITESTS?$", name, re.IGNORECASE) or re.search(r"UITests?$", name, re.IGNORECASE))


def is_unit_test_name(name: str) -> bool:
    return bool(
        (re.search(r"(?:^|[_-])TESTS$", name, re.IGNORECASE) or re.search(r"(?<!UI)Tests$", name, re.IGNORECASE))
        and not is_ui_test_name(name)
    )


def is_generic_test_scheme(name: str) -> bool:
    return bool(re.search(r"(?:^|[_-])TEST$", name, re.IGNORECASE) and not is_ui_test_name(name))


def iter_scheme_testables(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    names: list[str] = []
    for reference in root.findall(".//TestAction//TestableReference//BuildableReference"):
        for key in ("BuildableName", "BlueprintName"):
            value = reference.get(key)
            if value:
                name = Path(value).stem
                if name not in names:
                    names.append(name)
    return names


def scheme_has_unit_tests(path: Path | None) -> bool:
    return any(is_unit_test_name(name) for name in iter_scheme_testables(path))


def scheme_has_ui_tests(path: Path | None) -> bool:
    return any(is_ui_test_name(name) for name in iter_scheme_testables(path))


def scheme_reason(name: str, path: Path | None, source: str) -> str:
    if source == "xcodebuild-args-or-env":
        return "metadata captured by codex_verify wrapper"
    if scheme_has_unit_tests(path):
        return "scheme has unit test binding"
    if is_unit_test_name(name):
        return "scheme name matches unit test pattern"
    if is_generic_test_scheme(name):
        return "scheme name matches generic test pattern"
    if scheme_has_ui_tests(path):
        return "scheme has UI test binding"
    if is_ui_test_name(name):
        return "scheme name matches UI test pattern"
    return "fallback shared scheme"


def scheme_sort_key(name: str, path: Path | None) -> tuple[int, str]:
    if scheme_has_unit_tests(path):
        return (0, name.lower())
    if is_unit_test_name(name):
        return (1, name.lower())
    if is_generic_test_scheme(name):
        return (2, name.lower())
    if scheme_has_ui_tests(path):
        return (3, name.lower())
    if is_ui_test_name(name):
        return (4, name.lower())
    return (5, name.lower())


def fallback_project_selection(repo_root: Path, workspace: str, project: str) -> dict:
    workspaces = workspace_candidates(repo_root)
    projects = project_candidates(repo_root)
    if workspace:
        value = workspace
        source = "xcodebuild-args-or-env"
        reason = "metadata captured by codex_verify wrapper"
    elif workspaces:
        value = str(workspaces[0].relative_to(repo_root))
        source = "auto_discovered"
        reason = ".xcworkspace preferred over .xcodeproj" if projects else ".xcworkspace auto discovered"
    else:
        value = project or (str(projects[0].relative_to(repo_root)) if projects else None)
        source = "xcodebuild-args-or-env" if project else "auto_discovered"
        reason = "metadata captured by codex_verify wrapper" if project else "no .xcworkspace found; using .xcodeproj"
    return {
        "type": "workspace" if value and str(value).endswith(".xcworkspace") else "project",
        "value": value,
        "source": source,
        "reason": reason,
        "workspace_candidates": [str(path.relative_to(repo_root)) for path in workspaces],
        "project_candidates": [str(path.relative_to(repo_root)) for path in projects],
    }


def fallback_scheme_selection(repo_root: Path, scheme: str) -> dict:
    paths = scheme_paths(repo_root)
    selected = scheme
    source = "xcodebuild-args-or-env" if selected else "auto_discovered"
    if not selected and paths:
        selected = sorted(paths.keys(), key=lambda name: scheme_sort_key(name, paths.get(name)))[0]
    selected_path = paths.get(selected)
    testables = iter_scheme_testables(selected_path)
    return {
        "scheme": selected,
        "source": source,
        "reason": scheme_reason(selected, selected_path, source) if selected else "no shared scheme metadata available",
        "testables": testables,
        "has_unit_tests": any(is_unit_test_name(name) for name in testables),
        "has_ui_tests": any(is_ui_test_name(name) for name in testables),
        "scheme_path": str(selected_path.relative_to(repo_root)) if selected_path else None,
        "candidate_schemes": sorted(paths.keys()),
    }


report = load_report()
command_preview = read_text("command_preview")
only_testing = report.get("only_testing") or extract_only_testing(command_preview)
destination = read_text("destination")
baseline = report.get("baseline") if isinstance(report.get("baseline"), dict) else {}
project_selection = report.get("project_selection") if isinstance(report.get("project_selection"), dict) else None
scheme_selection = report.get("scheme_selection") if isinstance(report.get("scheme_selection"), dict) else None
repo_root = Path(non_auto(read_text("repo_root")) or ".").resolve()
metadata_workspace = non_auto(read_text("workspace"))
metadata_project = non_auto(read_text("project"))
metadata_scheme = non_auto(read_text("scheme"))
workspace_or_project = metadata_workspace or metadata_project or non_auto(baseline.get("workspace_or_project")) or (
    project_selection.get("value") if project_selection else None
)
scheme = metadata_scheme or non_auto(baseline.get("scheme")) or (
    scheme_selection.get("scheme") if scheme_selection else None
)
effective_project_selection = project_selection or fallback_project_selection(repo_root, metadata_workspace, metadata_project)
effective_scheme_selection = scheme_selection or fallback_scheme_selection(repo_root, metadata_scheme)
workspace_or_project = workspace_or_project or effective_project_selection.get("value")
scheme = scheme or effective_scheme_selection.get("scheme")
configuration = non_auto(read_text("configuration")) or non_auto(baseline.get("configuration")) or "Debug"
action = non_auto(read_text("action")) or non_auto(baseline.get("action")) or str(report.get("mode") or "build")
artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), dict) else {}
artifact_paths = {
    **artifact_paths,
    "agent_summary": str(summary_path),
    "verification_report": str(report_path),
    "diagnostics_json": str(job_dir / "diagnostics.json"),
    "build_summary": str(job_dir / "build-summary.txt"),
    "environment_sanity": str(job_dir / "xcode-entry-sanity.json"),
    "raw_log": str(job_dir / "job.log"),
}
worktree_session_request = load_json_file("worktree-session-request.json")
if worktree_session_request:
    artifact_paths["worktree_session_request"] = str(job_dir / "worktree-session-request.json")
    artifact_paths["worktree_session_artifacts"] = str(job_dir / "worktree-session-artifacts.json")
environment_sanity = report.get("environment_sanity") if isinstance(report.get("environment_sanity"), dict) else load_json_file("xcode-entry-sanity.json")
artifact_hashes = {}
for name, raw_path in artifact_paths.items():
    if name in {"agent_summary", "raw_log"} or not raw_path:
        continue
    path = Path(str(raw_path))
    if path.is_file():
        artifact_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()

summary = {
    "schema_version": 1,
    "producer": "codex_verify_agent_summary",
    "status": report.get("status", "unknown"),
    "verification_level": verification_level(read_text("action") or str(report.get("mode", "")), list(only_testing), report),
    "route": "codex_verify -> build-queue daemon -> xcodebuild",
    "repo_root": str(repo_root),
    "workspace_or_project": workspace_or_project,
    "project_selection": effective_project_selection,
    "scheme": scheme,
    "scheme_selection": effective_scheme_selection,
    "environment_sanity": environment_sanity,
    "configuration": configuration,
    "action": action,
    "destination": {
        "type": baseline.get("destination_type") or destination_type(destination or str(baseline.get("destination", ""))),
        "value": destination or baseline.get("destination"),
        "selected_device_reason": baseline.get("selected_device_reason"),
    },
    "only_testing": list(only_testing),
    "executed_command": command_preview,
    "executed_commands": [command_preview] if command_preview else [],
    "queue_job_id": job_dir.name,
    "queue_job_dir": str(job_dir),
    "request_fingerprint": read_text("request_fingerprint"),
    "request_fingerprint_cacheable": read_text("request_fingerprint_cacheable") == "1",
    "worktree_session": (
        {
            "session_id": worktree_session_request.get("session_id"),
            "source_identity": worktree_session_request.get("source_identity"),
            "attempt_id": worktree_session_request.get("attempt_id"),
            "environment_fingerprint": worktree_session_request.get("environment_fingerprint"),
            "derived_data_slot": worktree_session_request.get("derived_data_slot"),
            "artifact_namespace": worktree_session_request.get("artifact_namespace"),
            "artifact_directory": read_text("worktree_session_artifact_directory"),
            "test_plan": worktree_session_request.get("test_plan"),
            "target_fingerprints": worktree_session_request.get("target_fingerprints"),
            "request_sha256": read_text("worktree_session_request_sha256"),
            "artifact_manifest_sha256": artifact_hashes.get("worktree_session_artifacts"),
            "daemon_validated": read_text("worktree_session_request_validated") == "true",
            "identity_assurance": {
                "source_identity": "daemon-recomputed-pre-locked-post",
                "destination_and_test_plan": "request-command-bound",
                "environment_and_targets": "caller-frozen-not-recomputed",
            },
        }
        if worktree_session_request
        else None
    ),
    "fingerprint": report.get("fingerprint"),
    "cached": report.get("cached", False),
    "summary": report.get("summary"),
    "first_blocking_error": report.get("first_blocking_error"),
    "failed_tests": report.get("failed_tests", []),
    "warnings_count": report.get("warnings_count", 0),
    "ui_smoke": report.get("ui_smoke"),
    "artifact_paths": artifact_paths,
    "artifact_hashes": artifact_hashes,
    "raw_log_policy": report.get("raw_log_policy", "forbidden_by_default"),
    "needs_raw_log": report.get("needs_raw_log", False),
    "next_action": report.get("suggested_next_action") or report.get("next_action"),
}
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

job_artifacts_are_valid() {
  local job_dir="$1"
  local exit_code="$2"
  local expected_request_fingerprint="${3:-}"
  python3 - "$job_dir" "$exit_code" "$expected_request_fingerprint" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

job_dir = Path(sys.argv[1])
exit_code = int(sys.argv[2])
expected_request_fingerprint = sys.argv[3]


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


try:
    report = load_object(job_dir / "verification-report.json")
    summary = load_object(job_dir / "agent-summary.json")
    expected_statuses = {"passed"} if exit_code == 0 else {"failed", "blocked"}
    if report.get("status") not in expected_statuses or summary.get("status") not in expected_statuses:
        raise ValueError("structured artifact status does not match process exit status")
    request_fingerprint = (job_dir / "request_fingerprint").read_text(encoding="utf-8").strip()
    if expected_request_fingerprint and request_fingerprint != expected_request_fingerprint:
        raise ValueError("queue request fingerprint mismatch")
    if summary.get("request_fingerprint") != request_fingerprint:
        raise ValueError("agent summary request fingerprint mismatch")
    artifact_paths = summary.get("artifact_paths")
    artifact_hashes = summary.get("artifact_hashes")
    if not isinstance(artifact_paths, dict) or not isinstance(artifact_hashes, dict):
        raise ValueError("artifact paths and hashes are required")
    for name in ("verification_report", "diagnostics_json", "build_summary"):
        raw_path = artifact_paths.get(name)
        expected_hash = artifact_hashes.get(name)
        if not isinstance(raw_path, str) or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"missing valid structured artifact identity: {name}")
        path = Path(raw_path)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"structured artifact changed or disappeared: {name}")
    worktree_session = summary.get("worktree_session")
    if worktree_session is not None:
        if not isinstance(worktree_session, dict) or worktree_session.get("daemon_validated") is not True:
            raise ValueError("Worktree Session request was not validated by the daemon")
        request_path = artifact_paths.get("worktree_session_request")
        request_hash = artifact_hashes.get("worktree_session_request")
        if request_hash != worktree_session.get("request_sha256"):
            raise ValueError("Worktree Session request digest differs from structured summary")
        expected_request_path = job_dir / "worktree-session-request.json"
        if not isinstance(request_path, str) or Path(request_path) != expected_request_path:
            raise ValueError("Worktree Session request artifact path escaped the queue job")
        if not expected_request_path.is_file():
            raise ValueError("Worktree Session request artifact is missing")
        if hashlib.sha256(expected_request_path.read_bytes()).hexdigest() != request_hash:
            raise ValueError("Worktree Session request artifact changed after validation")
        manifest_path = artifact_paths.get("worktree_session_artifacts")
        manifest_hash = artifact_hashes.get("worktree_session_artifacts")
        expected_manifest_path = job_dir / "worktree-session-artifacts.json"
        if not isinstance(manifest_path, str) or Path(manifest_path) != expected_manifest_path:
            raise ValueError("Worktree Session artifact manifest path escaped the queue job")
        if manifest_hash != worktree_session.get("artifact_manifest_sha256"):
            raise ValueError("Worktree Session artifact manifest digest differs from structured summary")
        if not expected_manifest_path.is_file() or hashlib.sha256(expected_manifest_path.read_bytes()).hexdigest() != manifest_hash:
            raise ValueError("Worktree Session artifact manifest changed after validation")
        manifest = load_object(expected_manifest_path)
        if manifest.get("request_sha256") != request_hash:
            raise ValueError("Worktree Session artifact manifest request identity changed")
        if manifest.get("artifact_namespace") != worktree_session.get("artifact_namespace"):
            raise ValueError("Worktree Session artifact manifest namespace changed")
        artifact_root = Path(str(manifest.get("artifact_directory", "")))
        expected_artifact_root = job_dir.parent.parent / "artifacts" / str(worktree_session.get("artifact_namespace", "")) / job_dir.name
        if artifact_root != expected_artifact_root or str(artifact_root) != worktree_session.get("artifact_directory") or not artifact_root.is_dir() or artifact_root.is_symlink():
            raise ValueError("Worktree Session artifact directory is missing or changed")
        current_files = []
        for child in sorted(artifact_root.rglob("*")):
            if child.is_symlink():
                raise ValueError("Worktree Session artifact directory contains a symlink")
            if child.is_dir():
                continue
            before = child.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Worktree Session artifact directory contains an unsupported entry")
            descriptor = os.open(child, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                opened = os.fstat(stream.fileno())
            after = child.lstat()
            identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
            if identity(before) != identity(opened) or identity(opened) != identity(after):
                raise ValueError("Worktree Session artifact changed while validating cache")
            current_files.append({
                "mode": opened.st_mode & 0o777,
                "path": child.relative_to(artifact_root).as_posix(),
                "sha256": digest.hexdigest(),
                "size": opened.st_size,
            })
        if manifest.get("files") != current_files:
            raise ValueError("Worktree Session artifact directory changed after execution")
except (OSError, ValueError, json.JSONDecodeError) as exc:
    print(f"[codex_verify] invalid structured artifacts: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

generate_verification_artifacts() {
  local job_dir="$1"
  local status="$2"
  local digest_script=''
  local diagnostics_path="$job_dir/diagnostics.json"
  local summary_path="$job_dir/build-summary.txt"
  local report_path="$job_dir/verification-report.json"

  if digest_script="$(resolve_digest_script)"; then
    CODEX_VERIFY_MODE="$(cat "$job_dir/mode" 2>/dev/null || printf '%s' auto)" \
      CODEX_VERIFY_EXIT_CODE="$status" \
      bash "$digest_script" "$job_dir/job.log" "$diagnostics_path" "$summary_path" "$report_path" \
      >"$job_dir/digest.log" 2>&1 || true
  fi

  if [[ ! -f "$report_path" ]]; then
    write_minimal_verification_report "$job_dir" "$status"
  fi
  write_agent_summary "$job_dir"

  write_text_file "$job_dir/diagnostics_path" "$diagnostics_path"
  write_text_file "$job_dir/build_summary_path" "$summary_path"
  write_text_file "$job_dir/verification_report_path" "$report_path"
  write_text_file "$job_dir/agent_summary_path" "$job_dir/agent-summary.json"
  write_text_file "$QUEUE_ROOT/latest_job" "$job_dir"
  write_text_file "$QUEUE_ROOT/latest_verification_report" "$report_path"
  write_text_file "$QUEUE_ROOT/latest_agent_summary" "$job_dir/agent-summary.json"
}

print_verification_report() {
  local job_dir="$1"
  local reuse_kind="${2:-new}"
  local summary_path="$job_dir/agent-summary.json"
  if [[ -f "$summary_path" ]]; then
    python3 - "$summary_path" "$reuse_kind" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
reuse = sys.argv[2]
summary["request_reuse"] = reuse
summary["attached_to_in_flight"] = reuse == "attached"
if reuse == "cached":
    summary["cached"] = True
    summary["source_queue_job_id"] = summary.get("queue_job_id")
print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
  else
    echo "{\"status\":\"blocked\",\"summary\":\"agent-summary.json missing\",\"artifact_paths\":{\"raw_log\":\"$job_dir/job.log\"},\"needs_raw_log\":false}"
  fi
}

file_mtime_seconds() {
  local target="$1"
  stat -f %m "$target" 2>/dev/null || echo 0
}

daemon_pid() {
  [[ -f "$DAEMON_PID_FILE" ]] || return 0
  tr -d '\n' <"$DAEMON_PID_FILE"
}

daemon_running() {
  python3 - "$QUEUE_ROOT" "$QUEUE_SCHEMA_VERSION" "$QUEUE_GENERATION_ID" \
    "${CODEX_VERIFY_DAEMON_HEARTBEAT_TTL_SECONDS:-15}" <<'PY'
import json, os, sys, time
from pathlib import Path

root = Path(sys.argv[1])
schema, generation = sys.argv[2], sys.argv[3]
try:
    ttl = int(sys.argv[4])
except ValueError:
    raise SystemExit(1)
if ttl <= 0:
    raise SystemExit(1)

def canonical(path: Path):
    if path.is_symlink() or not path.is_file():
        raise SystemExit(1)
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if raw != expected or not isinstance(value, dict):
        raise SystemExit(1)
    return value

try:
    meta = canonical(root / "queue-meta.json")
    owner = canonical(root / "daemon-owner.json")
    heartbeat = canonical(root / "daemon-heartbeat.json")
    pid_raw = (root / "daemon.pid").read_text(encoding="utf-8").strip()
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
expected_meta = {"generation_id": generation, "producer": "codex_verify", "schema_version": schema}
if meta != expected_meta or not pid_raw.isdigit():
    raise SystemExit(1)
pid = int(pid_raw)
token = owner.get("token")
identity = {"generation_id": generation, "pid": pid, "queue_root": str(root), "schema_version": "1.0", "token": token}
if owner != identity or not isinstance(token, str) or len(token) != 64:
    raise SystemExit(1)
if heartbeat.get("identity") != identity or not isinstance(heartbeat.get("updated_at_epoch"), int):
    raise SystemExit(1)
if abs(int(time.time()) - heartbeat["updated_at_epoch"]) > ttl:
    raise SystemExit(1)
try:
    os.kill(pid, 0)
except OSError:
    raise SystemExit(1)
PY
}

controlled_job_dir() {
  python3 - "$JOBS_DIR" "$1" <<'PY'
from pathlib import Path
import sys
jobs = Path(sys.argv[1]).resolve()
candidate = Path(sys.argv[2])
if not candidate.is_absolute() or candidate.is_symlink() or candidate.parent.resolve() != jobs or not candidate.is_dir():
    raise SystemExit(1)
print(candidate)
PY
}

daemon_token() {
  daemon_running >/dev/null 2>&1 || return 1
  python3 - "$DAEMON_OWNER_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["token"])
PY
}

job_running_is_owned() {
  local job_dir="$1" active token pid
  validate_job_publication "$job_dir" >/dev/null 2>&1 || return 1
  [[ "$(job_state "$job_dir")" == 'running' ]] || return 1
  daemon_running >/dev/null 2>&1 || return 1
  active="$(cat "$ACTIVE_JOB_FILE" 2>/dev/null || true)"
  [[ "$(controlled_job_dir "$active" 2>/dev/null || true)" == "$job_dir" ]] || return 1
  token="$(daemon_token)" || return 1
  pid="$(daemon_pid)"
  [[ "$(cat "$job_dir/runner_token" 2>/dev/null || true)" == "$token" ]] || return 1
  [[ "$(cat "$job_dir/runner_pid" 2>/dev/null || true)" == "$pid" ]] || return 1
}

write_daemon_identity() {
  local token="$1"
  python3 - "$QUEUE_ROOT" "$QUEUE_GENERATION_ID" "$$" "$token" <<'PY'
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
identity = {
    "generation_id": sys.argv[2],
    "pid": int(sys.argv[3]),
    "queue_root": str(root),
    "schema_version": "1.0",
    "token": sys.argv[4],
}
def write(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(raw, encoding="utf-8")
    os.replace(temporary, path)
write(root / "daemon-owner.json", identity)
write(root / "daemon-heartbeat.json", {"identity": identity, "updated_at_epoch": int(time.time())})
PY
  printf '%s\n' "$$" >"$DAEMON_PID_FILE"
}

write_daemon_heartbeat() {
  local token="$1"
  python3 - "$QUEUE_ROOT" "$QUEUE_GENERATION_ID" "$$" "$token" <<'PY'
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
identity = {"generation_id": sys.argv[2], "pid": int(sys.argv[3]), "queue_root": str(root), "schema_version": "1.0", "token": sys.argv[4]}
value = {"identity": identity, "updated_at_epoch": int(time.time())}
path = root / "daemon-heartbeat.json"
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
}

daemon_heartbeat_loop() {
  local token="$1"
  while kill -0 "$$" 2>/dev/null; do
    write_daemon_heartbeat "$token" || exit 1
    sleep "${CODEX_VERIFY_DAEMON_HEARTBEAT_INTERVAL_SECONDS:-2}"
  done
}

load_metadata_from_xcode_args() {
  local args=("$@")
  local index=0
  while [[ $index -lt ${#args[@]} ]]; do
    local arg="${args[$index]}"
    case "$arg" in
      -workspace)
        ((index += 1))
        META_WORKSPACE="${args[$index]:-}"
        ;;
      -project)
        ((index += 1))
        META_PROJECT="${args[$index]:-}"
        ;;
      -scheme)
        ((index += 1))
        META_SCHEME="${args[$index]:-}"
        META_SCHEME_FROM_ARGS='1'
        ;;
      -destination)
        ((index += 1))
        META_DESTINATION="${args[$index]:-}"
        ;;
      -configuration)
        ((index += 1))
        META_CONFIGURATION="${args[$index]:-}"
        ;;
      build|build-for-testing|test|test-without-building|archive|analyze|clean)
        META_ACTION="$arg"
        ;;
      -exportArchive)
        META_ACTION='exportArchive'
        ;;
    esac
    ((index += 1))
  done
}

normalize_xcodebuild_entry_args() {
  [[ "$MODE" == 'xcodebuild' ]] || return 0
  local normalized=("${COMMAND[@]}")
  local index=0 arg value absolute
  while [[ $index -lt ${#normalized[@]} ]]; do
    arg="${normalized[$index]}"
    case "$arg" in
      -workspace|-project)
        if [[ $((index + 1)) -lt ${#normalized[@]} ]]; then
          value="${normalized[$((index + 1))]}"
          absolute="$(resolve_repo_entry_path "$value")"
          if [[ -d "$absolute" ]]; then
            normalized[$((index + 1))]="$absolute"
            if [[ "$arg" == '-workspace' ]]; then
              META_WORKSPACE="$absolute"
            else
              META_PROJECT="$absolute"
            fi
          fi
        fi
        ;;
    esac
    ((index += 1))
  done
  COMMAND=("${normalized[@]}")
}

assert_configured_scheme_exists() {
  [[ "$MODE" == 'xcodebuild' && "${META_SCHEME_FROM_ARGS:-0}" == '1' ]] || return 0
  local scheme="${META_SCHEME:-}"
  [[ -n "$scheme" && "$scheme" != 'auto' ]] || return 0

  python3 - "$REPO_ROOT" "$scheme" <<'PY'
from __future__ import annotations

from pathlib import Path
import sys

repo_root = Path(sys.argv[1]).resolve()
scheme = sys.argv[2].strip()

schemes = sorted(
    {
        path.stem
        for path in repo_root.rglob("*.xcscheme")
        if "Pods" not in path.parts
    }
)

# Some repositories intentionally keep schemes unshared. In that case the
# wrapper cannot prove validity without invoking xcodebuild, so preserve the
# historical behavior and let xcodebuild report the project-specific error.
if schemes and scheme not in schemes:
    print(
        f"[codex_verify] invalid scheme '{scheme}' is not a shared scheme under {repo_root}",
        file=sys.stderr,
    )
    print(f"[codex_verify] available schemes: {', '.join(schemes)}", file=sys.stderr)
    print(
        "[codex_verify] let the script decide scheme/destination via --build-check, "
        "or fix XCODE_SCHEME in .codex/xcodebuild.env; do not hand-splice a non-existent scheme.",
        file=sys.stderr,
    )
    sys.exit(66)
PY
}

load_metadata_defaults() {
  META_WORKSPACE="${META_WORKSPACE:-$(read_env_file_value XCODE_WORKSPACE)}"
  META_PROJECT="${META_PROJECT:-$(read_env_file_value XCODE_PROJECT)}"
  META_SCHEME="${META_SCHEME:-$(read_env_file_value XCODE_SCHEME)}"
  META_CONFIGURATION="${META_CONFIGURATION:-$(read_env_file_value XCODE_CONFIGURATION)}"
  META_ACTION="${META_ACTION:-$(read_env_file_value XCODE_ACTION)}"

  if [[ -z "${META_DESTINATION:-}" ]]; then
    local explicit_destination explicit_device_id explicit_device_name
    explicit_destination="$(read_env_file_value XCODE_DESTINATION)"
    explicit_device_id="$(read_env_file_value XCODE_DEVICE_ID)"
    explicit_device_name="$(read_env_file_value XCODE_DEVICE_NAME)"
    if [[ -n "$explicit_destination" ]]; then
      META_DESTINATION="$explicit_destination"
    elif [[ -n "$explicit_device_id" ]]; then
      META_DESTINATION="id=$explicit_device_id"
    elif [[ -n "$explicit_device_name" ]]; then
      META_DESTINATION="name=$explicit_device_name"
    else
      META_DESTINATION='auto(connected-device-preferred)'
    fi
  fi

  META_WORKSPACE="${META_WORKSPACE:-auto}"
  META_PROJECT="${META_PROJECT:-auto}"
  META_SCHEME="${META_SCHEME:-auto}"
  META_CONFIGURATION="${META_CONFIGURATION:-Debug(auto)}"
  META_ACTION="${META_ACTION:-build(auto)}"
}

assert_no_legacy_derived_data_settings() {
  local key value
  for key in \
    XCODE_DERIVED_DATA_MODE \
    XCODE_DERIVED_DATA_SEED_MODE \
    XCODE_DERIVED_DATA_REFRESH \
    CODEX_DERIVED_DATA_SLOT
  do
    value="$(env_or_file_value "$key")"
    if [[ -n "$value" ]]; then
      die "legacy $key is no longer supported; build-queue daemon now always uses $SYSTEM_DERIVED_DATA_HOME. Remove $key from environment or $XCODE_ENV_FILE"
    fi
  done
}

build_owner_body() {
  cat <<EOF
pid=$$
user=${USER:-unknown}
host=$(hostname -s 2>/dev/null || hostname)
repo_root=$REPO_ROOT
mode=$MODE
workspace=${META_WORKSPACE}
project=${META_PROJECT}
scheme=${META_SCHEME}
configuration=${META_CONFIGURATION}
destination=${META_DESTINATION}
action=${META_ACTION}
derived_data_path=$SYSTEM_DERIVED_DATA_HOME
submitted_at=$(timestamp_now)
command=$COMMAND_PREVIEW
wrapper=$(resolve_path "${BASH_SOURCE[0]}")
EOF
}

queue_start_lock_is_stale() {
  [[ -d "$START_LOCK_DIR" ]] || return 1
  local owner_pid owner_file waited now age
  owner_file="$START_LOCK_DIR/owner.pid"
  owner_pid=''
  if [[ -f "$owner_file" ]]; then
    owner_pid="$(tr -d '\n' <"$owner_file")"
  fi
  if [[ -n "$owner_pid" ]] && kill -0 "$owner_pid" 2>/dev/null; then
    return 1
  fi
  now="$(seconds_now)"
  age=$(( now - $(file_mtime_seconds "$START_LOCK_DIR") ))
  [[ $age -ge 15 ]]
}

acquire_start_lock() {
  mkdir -p "$QUEUE_ROOT"
  while ! mkdir "$START_LOCK_DIR" 2>/dev/null; do
    if queue_start_lock_is_stale; then
      rm -rf "$START_LOCK_DIR"
      continue
    fi
    if daemon_running; then
      return 1
    fi
    sleep 1
  done
  printf '%s\n' "$$" >"$START_LOCK_DIR/owner.pid"
  return 0
}

release_start_lock() {
  rm -rf "$START_LOCK_DIR"
}

adopt_daemon_start_lock() {
  local token="$1" published_token=''
  if [[ -f "$START_LOCK_DIR/daemon-token" && ! -L "$START_LOCK_DIR/daemon-token" ]]; then
    published_token="$(cat "$START_LOCK_DIR/daemon-token" 2>/dev/null || true)"
  fi
  if [[ -n "$published_token" ]]; then
    [[ "$published_token" == "$token" ]] || return 1
    mkdir "$START_LOCK_DIR/daemon-adopt.lockdir" 2>/dev/null || return 1
  else
    acquire_start_lock || return 1
    if daemon_running; then
      release_start_lock
      return 1
    fi
    write_text_file "$START_LOCK_DIR/daemon-token" "$token"
    mkdir "$START_LOCK_DIR/daemon-adopt.lockdir" 2>/dev/null || {
      release_start_lock
      return 1
    }
  fi
  write_text_file "$START_LOCK_DIR/owner.pid" "$$"
}

ensure_daemon_running() {
  local daemon_pid_value start_pid started_at daemon_start_token
  mkdir -p "$QUEUE_ROOT" "$JOBS_DIR"
  if daemon_running; then
    return 0
  fi
  daemon_pid_value="$(daemon_pid)"
  if [[ -n "$daemon_pid_value" ]] && kill -0 "$daemon_pid_value" 2>/dev/null; then
    die "daemon pid is live but its generation/token/heartbeat identity is invalid; repair the shared queue offline"
  fi

  if ! acquire_start_lock; then
    return 0
  fi

  if daemon_running; then
    release_start_lock
    return 0
  fi

  if ! queue_maintenance startup "${CODEX_VERIFY_QUEUE_INVALID_POLICY:-quarantine}" >>"$DAEMON_STDOUT_LOG" 2>&1; then
    release_start_lock
    die "shared queue reconciliation failed at $QUEUE_ROOT; run --queue-doctor for details"
  fi

  started_at="$(timestamp_now)"
  daemon_start_token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  write_text_file "$START_LOCK_DIR/daemon-token" "$daemon_start_token"
  CODEX_VERIFY_DAEMON_TOKEN="$daemon_start_token" nohup bash "$SCRIPT_PATH" --daemon >>"$DAEMON_STDOUT_LOG" 2>&1 &
  start_pid=$!

  local waited=0
  while [[ $waited -lt 30 ]]; do
    daemon_pid_value="$(daemon_pid)"
    if daemon_running && [[ "$(daemon_token)" == "$daemon_start_token" ]]; then
      release_start_lock
      return 0
    fi
    if ! kill -0 "$start_pid" 2>/dev/null; then
      break
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done

  release_start_lock
  die "failed to start build-queue daemon at $QUEUE_ROOT (started_at=$started_at)"
}

generate_job_id() {
  python3 - "$$" "$RANDOM" <<'PY'
import sys, time

pid = sys.argv[1]
rand = sys.argv[2]
print(f"{time.time_ns()}-{pid}-{rand}")
PY
}

write_env_snapshot() {
  local target="$1"
  local key value
  : >"$target"
  for key in PATH DEVELOPER_DIR LANG LC_ALL; do
    if [[ ${!key+x} == x ]]; then
      printf 'export %s=%q\n' "$key" "${!key}" >>"$target"
    fi
  done
  for key in $(compgen -v | LC_ALL=C sort); do
    case "$key" in
      XCODE_*|CODEX_WORKTREE_SESSION_ID|CODEX_WORKTREE_SESSION_ATTEMPT_ID|CODEX_WORKTREE_SESSION_SOURCE_IDENTITY|CODEX_WORKTREE_SESSION_ENVIRONMENT_FINGERPRINT|CODEX_WORKTREE_SESSION_DERIVED_DATA_SLOT|CODEX_WORKTREE_SESSION_ARTIFACT_NAMESPACE|CODEX_WORKTREE_SESSION_REQUEST_SHA256)
        value="${!key}"
        printf 'export %s=%q\n' "$key" "$value" >>"$target"
        ;;
    esac
  done
}

queue_job() {
  local job_id job_dir staging_dir owner_body
  job_id="$(generate_job_id)"
  job_dir="$JOBS_DIR/$job_id"
  staging_dir="$STAGING_DIR/$job_id.$$"
  mkdir -p "$staging_dir"

  owner_body="$(build_owner_body)"
  write_text_file "$staging_dir/job_schema_version" "$QUEUE_SCHEMA_VERSION"
  write_text_file "$staging_dir/queue_generation_id" "$QUEUE_GENERATION_ID"
  write_text_file "$staging_dir/mode" "$MODE"
  write_text_file "$staging_dir/repo_root" "$REPO_ROOT"
  write_text_file "$staging_dir/created_at" "$(timestamp_now)"
  write_text_file "$staging_dir/created_at_epoch" "$(seconds_now)"
  write_text_file "$staging_dir/submitter.txt" "$owner_body"
  write_text_file "$staging_dir/workspace" "$META_WORKSPACE"
  write_text_file "$staging_dir/project" "$META_PROJECT"
  write_text_file "$staging_dir/scheme" "$META_SCHEME"
  write_text_file "$staging_dir/configuration" "$META_CONFIGURATION"
  write_text_file "$staging_dir/destination" "$META_DESTINATION"
  write_text_file "$staging_dir/action" "$META_ACTION"
  write_text_file "$staging_dir/command_preview" "$COMMAND_PREVIEW"
  write_text_file "$staging_dir/request_fingerprint" "$REQUEST_FINGERPRINT"
  write_text_file "$staging_dir/request_fingerprint_cacheable" "$REQUEST_CACHEABLE"
  if [[ -n "${WORKTREE_SESSION_REQUEST_SHA256:-}" ]]; then
    printf '%s\n' "$WORKTREE_SESSION_REQUEST_CANONICAL" >"$staging_dir/worktree-session-request.json"
    write_text_file "$staging_dir/worktree_session_request_sha256" "$WORKTREE_SESSION_REQUEST_SHA256"
    write_text_file "$staging_dir/worktree_session_id" "$WORKTREE_SESSION_ID"
    write_text_file "$staging_dir/worktree_session_source_identity" "$WORKTREE_SESSION_SOURCE_IDENTITY"
    write_text_file "$staging_dir/worktree_session_derived_data_slot" "$WORKTREE_SESSION_DERIVED_DATA_SLOT"
    write_text_file "$staging_dir/worktree_session_test_plan" "$WORKTREE_SESSION_TEST_PLAN"
    write_text_file "$staging_dir/worktree_session_artifact_namespace" "$WORKTREE_SESSION_ARTIFACT_NAMESPACE"
  fi
  write_text_file "$staging_dir/log_path" "$job_dir/job.log"
  write_env_snapshot "$staging_dir/env.sh"
  write_args_file "$staging_dir/command.args0" "${COMMAND[@]}"
  write_xcode_entry_sanity "$staging_dir"
  write_job_publication_manifest "$staging_dir"
  write_text_file "$staging_dir/ready" 'true'
  set_job_state "$staging_dir" 'queued'
  mv "$staging_dir" "$job_dir"

  JOB_ID="$job_id"
  JOB_DIR="$job_dir"
  JOB_LOG_FILE="$job_dir/job.log"
}

acquire_request_lock() {
  local waited=0 owner_pid=''
  while ! mkdir "$REQUEST_LOCK_DIR" 2>/dev/null; do
    owner_pid="$(cat "$REQUEST_LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$owner_pid" ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
      rm -rf "$REQUEST_LOCK_DIR"
      continue
    fi
    sleep 1
    waited=$(( waited + 1 ))
    [[ $waited -lt 30 ]] || die "timed out acquiring verification fingerprint lock: $REQUEST_FINGERPRINT"
  done
  write_text_file "$REQUEST_LOCK_DIR/pid" "$$"
}

release_request_lock() {
  rm -rf "$REQUEST_LOCK_DIR"
}

find_matching_job() {
  local job_dir state
  MATCHING_JOB_DIR=''
  MATCHING_JOB_KIND=''
  for job_dir in $(find "$JOBS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort -r); do
    [[ -f "$job_dir/request_fingerprint" ]] || continue
    [[ "$(cat "$job_dir/request_fingerprint")" == "$REQUEST_FINGERPRINT" ]] || continue
    state="$(job_state "$job_dir")"
    case "$state" in
      queued)
        job_ready_for_execution "$job_dir" || continue
        MATCHING_JOB_DIR="$job_dir"
        MATCHING_JOB_KIND='attached'
        return 0
        ;;
      running)
        job_running_is_owned "$job_dir" || continue
        MATCHING_JOB_DIR="$job_dir"
        MATCHING_JOB_KIND='attached'
        return 0
        ;;
      succeeded)
        if [[ "$NO_CACHE" != '1' && "$FORCE_REVERIFY" != '1' ]] \
          && validate_job_publication "$job_dir" >/dev/null 2>&1 \
          && job_artifacts_are_valid "$job_dir" "$(cat "$job_dir/exit_code" 2>/dev/null || printf '%s' '1')" "$REQUEST_FINGERPRINT"; then
          MATCHING_JOB_DIR="$job_dir"
          MATCHING_JOB_KIND='cached'
          return 0
        fi
        return 1
        ;;
      failed)
        # The newest terminal result is authoritative. Never fall back to an
        # older success after a forced revalidation failed.
        return 1
        ;;
    esac
  done
  return 1
}

queue_or_reuse_job() {
  acquire_request_lock
  if find_matching_job; then
    JOB_DIR="$MATCHING_JOB_DIR"
    JOB_ID="$(basename "$JOB_DIR")"
    JOB_LOG_FILE="$JOB_DIR/job.log"
    JOB_REUSE_KIND="$MATCHING_JOB_KIND"
  else
    JOB_REUSE_KIND='new'
    queue_job
  fi
  release_request_lock
}

job_summary_line() {
  local job_dir="$1"
  local job_id repo_root mode workspace scheme destination state
  job_id="$(basename "$job_dir")"
  repo_root="$(cat "$job_dir/repo_root" 2>/dev/null || printf '%s' '-')"
  mode="$(cat "$job_dir/mode" 2>/dev/null || printf '%s' '-')"
  workspace="$(cat "$job_dir/workspace" 2>/dev/null || printf '%s' '-')"
  scheme="$(cat "$job_dir/scheme" 2>/dev/null || printf '%s' '-')"
  destination="$(cat "$job_dir/destination" 2>/dev/null || printf '%s' '-')"
  state="$(job_state "$job_dir")"
  printf '%s | %s | mode=%s | workspace=%s | scheme=%s | destination=%s | state=%s\n' \
    "$job_id" "$repo_root" "$mode" "$workspace" "$scheme" "$destination" "$state"
}

job_matches_filter() {
  local job_dir="$1"
  if [[ -z "$STATUS_FILTER_REPO_ROOT" ]]; then
    return 0
  fi
  [[ -f "$job_dir/repo_root" ]] || return 1
  [[ "$(cat "$job_dir/repo_root")" == "$STATUS_FILTER_REPO_ROOT" ]]
}

queue_status() {
  local active_job active_summary pending_count

  echo "Queue root: $QUEUE_ROOT"
  if daemon_running; then
    echo "Daemon: running pid=$(daemon_pid)"
  else
    echo "Daemon: not running"
  fi
  echo "DerivedData: system default ($SYSTEM_DERIVED_DATA_HOME)"

  active_job=''
  if [[ -f "$ACTIVE_JOB_FILE" ]]; then
    active_job="$(cat "$ACTIVE_JOB_FILE" 2>/dev/null || true)"
    active_job="$(controlled_job_dir "$active_job" 2>/dev/null || true)"
  fi
  if [[ -n "$active_job" && -d "$active_job" ]] && job_matches_filter "$active_job"; then
    active_summary="$(job_summary_line "$active_job")"
    echo "Active:"
    echo "  $active_summary"
    if [[ -f "$active_job/log_path" ]]; then
      echo "  log_file=$(cat "$active_job/log_path")"
    fi
  else
    echo "Active: none"
  fi

  pending_count=0
  echo "Pending:"
  local job_dir
  for job_dir in $(find "$JOBS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort); do
    if job_ready_for_execution "$job_dir" && job_matches_filter "$job_dir"; then
      pending_count=$(( pending_count + 1 ))
      echo "  $pending_count. $(job_summary_line "$job_dir")"
    fi
  done
  if [[ $pending_count -eq 0 ]]; then
    echo "  none"
  fi
}

queue_position() {
  local target_dir="$1"
  local position=0
  local job_dir
  for job_dir in $(find "$JOBS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort); do
    if job_ready_for_execution "$job_dir"; then
      position=$(( position + 1 ))
      if [[ "$job_dir" == "$target_dir" ]]; then
        printf '%s' "$position"
        return 0
      fi
    fi
  done
  printf '%s' '0'
}

active_job_summary() {
  if [[ -f "$ACTIVE_JOB_FILE" ]]; then
    local active_job
    active_job="$(cat "$ACTIVE_JOB_FILE" 2>/dev/null || true)"
    active_job="$(controlled_job_dir "$active_job" 2>/dev/null || true)"
    if [[ -n "$active_job" && -d "$active_job" ]]; then
      job_summary_line "$active_job"
      return 0
    fi
  fi
  printf '%s' 'none'
}

wait_for_job() {
  local last_notice='' state position active_summary exit_code tail_pid=''
  while true; do
    if ! daemon_running; then
      ensure_daemon_running
    fi
    state="$(job_state "$JOB_DIR")"
    case "$state" in
      queued)
        position="$(queue_position "$JOB_DIR")"
        active_summary="$(active_job_summary)"
        if [[ "$last_notice" != "queued:$position:$active_summary" ]]; then
          echo "[codex_verify] queued job=$JOB_ID position=$position active=$active_summary log_file=$JOB_LOG_FILE" >&2
          last_notice="queued:$position:$active_summary"
        fi
        sleep 1
        ;;
      running)
        if [[ -z "$last_notice" || "$last_notice" != "running" ]]; then
          echo "[codex_verify] running job=$JOB_ID log_file=$JOB_LOG_FILE derived_data_path=$SYSTEM_DERIVED_DATA_HOME" >&2
          last_notice="running"
        fi
        if [[ "${CODEX_VERIFY_STREAM_LOG:-0}" == '1' && -z "$tail_pid" ]]; then
          tail -n +1 -F "$JOB_LOG_FILE" 2>/dev/null &
          tail_pid=$!
        fi
        sleep 1
        ;;
      succeeded|failed)
        if [[ -n "$tail_pid" ]]; then
          kill "$tail_pid" 2>/dev/null || true
          wait "$tail_pid" 2>/dev/null || true
        fi
        exit_code="$(cat "$JOB_DIR/exit_code" 2>/dev/null || printf '%s' '1')"
        print_verification_report "$JOB_DIR" "$JOB_REUSE_KIND"
        echo "[codex_verify] finished job=$JOB_ID state=$state status=$exit_code agent_summary=$JOB_DIR/agent-summary.json report=$JOB_DIR/verification-report.json log_file=$JOB_LOG_FILE" >&2
        return "$exit_code"
        ;;
      invalid|*)
        if [[ -n "$tail_pid" ]]; then
          kill "$tail_pid" 2>/dev/null || true
          wait "$tail_pid" 2>/dev/null || true
        fi
        echo "[codex_verify] job=$JOB_ID became invalid, missing, or quarantined before a terminal result; run --queue-doctor --json" >&2
        return 70
        ;;
    esac
  done
}

mark_running_jobs_failed_on_recovery() {
  local job_dir
  for job_dir in $(find "$JOBS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort); do
    if [[ "$(job_state "$job_dir")" == 'running' ]]; then
      append_log_line "$job_dir/job.log" "[codex_verify] job interrupted: daemon restarted before completion"
      write_text_file "$job_dir/exit_code" "1"
      write_text_file "$job_dir/finished_at" "$(timestamp_now)"
      set_job_state "$job_dir" 'failed'
      release_derived_data_slot_lease "$job_dir"
    fi
  done
  rm -f "$ACTIVE_JOB_FILE"
}

next_queued_job() {
  local job_dir
  NEXT_QUEUED_JOB=''
  for job_dir in $(find "$JOBS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort); do
    if [[ "$(job_state "$job_dir")" == 'queued' ]]; then
      if job_ready_for_execution "$job_dir"; then
        NEXT_QUEUED_JOB="$job_dir"
        return 0
      fi
      quarantine_invalid_job "$job_dir" "queued job failed immutable publication/TTL validation" \
        >>"$DAEMON_STDOUT_LOG" 2>&1 || return 1
    fi
  done
  return 1
}

quarantine_invalid_job() {
  local job_dir="$1" reason="$2"
  python3 - "$JOBS_DIR" "$QUEUE_ROOT/quarantine" "$job_dir" "$reason" \
    "${CODEX_VERIFY_QUEUE_INVALID_POLICY:-quarantine}" <<'PY'
from datetime import datetime, timezone
import json, os, shutil, sys
from pathlib import Path
jobs = Path(sys.argv[1]).resolve()
quarantine = Path(sys.argv[2])
job = Path(sys.argv[3])
reason = sys.argv[4]
policy = sys.argv[5]
if not job.is_absolute() or job.is_symlink() or job.parent.resolve() != jobs or not job.is_dir():
    raise SystemExit(65)
if policy == "delete":
    shutil.rmtree(job)
    raise SystemExit(0)
if policy != "quarantine":
    raise SystemExit(65)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target_root = quarantine / stamp / "jobs"
target_root.mkdir(parents=True, exist_ok=True)
target = target_root / job.name
suffix = 0
while target.exists():
    suffix += 1
    target = target_root / f"{job.name}-{suffix}"
os.replace(job, target)
record = {"original_path": str(job), "quarantined_at": datetime.now(timezone.utc).isoformat(), "reason": reason, "schema_version": "1.0"}
(target_root / f"{target.name}.quarantine.json").write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
PY
}

validate_queued_worktree_session() {
  local job_dir="$1"
  local repo_root="$2"
  local phase="${3:-pre}"
  local request_path="$job_dir/worktree-session-request.json"
  [[ -f "$request_path" ]] || return 0
  local expected_hash actual_hash destination test_plan validated_request
  expected_hash="$(cat "$job_dir/worktree_session_request_sha256" 2>/dev/null || true)"
  actual_hash="$(python3 - "$request_path" <<'PY'
import hashlib, sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
  [[ -n "$expected_hash" && "$actual_hash" == "$expected_hash" ]] || {
    append_log_line "$job_dir/job.log" "[codex_verify] Worktree Session request digest changed before execution"
    return 1
  }
  destination="$(cat "$job_dir/destination")"
  test_plan="$(cat "$job_dir/worktree_session_test_plan")"
  validated_request="$(
    validate_worktree_session_request "$request_path" "$destination" "$test_plan" "$repo_root"
  )" || return 1
  [[ "$(request_json_field "$validated_request" session_id)" == "$(cat "$job_dir/worktree_session_id")" \
    && "$(request_json_field "$validated_request" source_identity)" == "$(cat "$job_dir/worktree_session_source_identity")" \
    && "$(request_json_field "$validated_request" derived_data_slot)" == "$(cat "$job_dir/worktree_session_derived_data_slot")" \
    && "$(request_json_field "$validated_request" test_plan)" == "$test_plan" \
    && "$(request_json_field "$validated_request" artifact_namespace)" == "$(cat "$job_dir/worktree_session_artifact_namespace")" ]] \
    || return 1
  write_text_file "$job_dir/worktree_session_request_validated_$phase" 'true'
  if [[ "$phase" == 'post' ]]; then
    write_text_file "$job_dir/worktree_session_request_validated" 'true'
  fi
}

acquire_derived_data_slot_lease() {
  local job_dir="$1"
  [[ -f "$job_dir/worktree_session_derived_data_slot" ]] || return 0
  local slot slot_dir lease_dir owner
  slot="$(cat "$job_dir/worktree_session_derived_data_slot")"
  [[ "$slot" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 1
  slot_dir="$DERIVED_DATA_SLOTS_DIR/$slot"
  lease_dir="$slot_dir/lease.lockdir"
  mkdir -p "$slot_dir"
  if [[ -d "$lease_dir" ]]; then
    owner="$(cat "$lease_dir/job_dir" 2>/dev/null || true)"
    owner="$(controlled_job_dir "$owner" 2>/dev/null || true)"
    if [[ -z "$owner" || "$(job_state "$owner")" != 'running' ]] || ! job_running_is_owned "$owner"; then
      rm -rf "$lease_dir"
    fi
  fi
  mkdir "$lease_dir" 2>/dev/null || return 1
  write_text_file "$lease_dir/job_dir" "$job_dir"
  write_text_file "$job_dir/worktree_session_slot_lease" "$lease_dir"
}

release_derived_data_slot_lease() {
  local job_dir="$1"
  local lease_dir owner slot expected_lease
  lease_dir="$(cat "$job_dir/worktree_session_slot_lease" 2>/dev/null || true)"
  [[ -n "$lease_dir" && -d "$lease_dir" ]] || return 0
  slot="$(cat "$job_dir/worktree_session_derived_data_slot" 2>/dev/null || true)"
  [[ "$slot" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 1
  expected_lease="$DERIVED_DATA_SLOTS_DIR/$slot/lease.lockdir"
  [[ "$lease_dir" == "$expected_lease" && ! -L "$lease_dir" ]] || return 1
  owner="$(cat "$lease_dir/job_dir" 2>/dev/null || true)"
  if [[ "$owner" == "$job_dir" ]]; then
    rm -rf "$lease_dir"
  fi
}

write_session_artifact_manifest() {
  local job_dir="$1"
  [[ -f "$job_dir/worktree_session_artifact_directory" ]] || return 0
  python3 - "$job_dir" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

job_dir = Path(sys.argv[1])
artifact_root = Path((job_dir / "worktree_session_artifact_directory").read_text(encoding="utf-8").strip())
if artifact_root.is_symlink() or not artifact_root.is_dir():
    raise SystemExit("Worktree Session artifact directory is missing or unsafe")


def snapshot(path: Path) -> dict[str, object]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"unsupported artifact entry: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        opened = os.fstat(stream.fileno())
    after = path.lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise RuntimeError(f"artifact changed while hashing: {path}")
    return {
        "mode": opened.st_mode & 0o777,
        "path": path.relative_to(artifact_root).as_posix(),
        "sha256": digest.hexdigest(),
        "size": opened.st_size,
    }


files = []
for child in sorted(artifact_root.rglob("*")):
    if child.is_symlink():
        raise RuntimeError(f"artifact directory contains a symlink: {child}")
    if child.is_dir():
        continue
    files.append(snapshot(child))
payload = {
    "artifact_directory": str(artifact_root),
    "artifact_namespace": (job_dir / "worktree_session_artifact_namespace").read_text(encoding="utf-8").strip(),
    "files": files,
    "request_sha256": (job_dir / "worktree_session_request_sha256").read_text(encoding="utf-8").strip(),
    "schema_version": "1.0",
}
(job_dir / "worktree-session-artifacts.json").write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
}

run_job() {
  local job_dir="$1"
  local repo_root mode started_at status session_artifact_directory
  local job_id
  job_id="$(basename "$job_dir")"
  if ! validate_job_publication "$job_dir" >/dev/null 2>&1; then
    quarantine_invalid_job "$job_dir" "publication changed before daemon claim"
    return 0
  fi
  repo_root="$(cat "$job_dir/repo_root")"
  mode="$(cat "$job_dir/mode")"
  started_at="$(timestamp_now)"

  write_text_file "$job_dir/runner_pid" "$$"
  write_text_file "$job_dir/runner_token" "$DAEMON_TOKEN"
  write_text_file "$ACTIVE_JOB_FILE" "$job_dir"
  write_text_file "$job_dir/started_at" "$started_at"
  set_job_state "$job_dir" 'running'

  if ! job_running_is_owned "$job_dir"; then
    append_log_line "$job_dir/job.log" "[codex_verify] daemon claim identity validation failed"
    write_text_file "$job_dir/exit_code" '70'
    write_text_file "$job_dir/finished_at" "$(timestamp_now)"
    set_job_state "$job_dir" 'failed'
    rm -f "$ACTIVE_JOB_FILE"
    return 0
  fi

  read_args_file "$job_dir/command.args0"
  append_log_line "$job_dir/job.log" "[codex_verify] job_id=$job_id"
  append_log_line "$job_dir/job.log" "[codex_verify] repo_root=$repo_root"
  append_log_line "$job_dir/job.log" "[codex_verify] mode=$mode"
  append_log_line "$job_dir/job.log" "[codex_verify] workspace=$(cat "$job_dir/workspace") project=$(cat "$job_dir/project") scheme=$(cat "$job_dir/scheme") destination=$(cat "$job_dir/destination")"
  append_log_line "$job_dir/job.log" "[codex_verify] action=$(cat "$job_dir/action") configuration=$(cat "$job_dir/configuration")"
  append_log_line "$job_dir/job.log" "[codex_verify] derived_data_path=$SYSTEM_DERIVED_DATA_HOME"
  if [[ -f "$job_dir/worktree-session-request.json" ]]; then
    append_log_line "$job_dir/job.log" "[codex_verify] worktree_session_id=$(cat "$job_dir/worktree_session_id") source_identity=$(cat "$job_dir/worktree_session_source_identity") derived_data_slot=$(cat "$job_dir/worktree_session_derived_data_slot") test_plan=$(cat "$job_dir/worktree_session_test_plan")"
  fi
  append_log_line "$job_dir/job.log" "[codex_verify] started_at=$started_at"
  append_log_line "$job_dir/job.log" "[codex_verify] command=$(cat "$job_dir/command_preview")"

  if ! validate_queued_worktree_session "$job_dir" "$repo_root" pre >>"$job_dir/job.log" 2>&1; then
    status=64
    append_log_line "$job_dir/job.log" "[codex_verify] Apple Worktree Session request validation blocked execution"
    write_text_file "$job_dir/finished_at" "$(timestamp_now)"
    generate_verification_artifacts "$job_dir" "$status"
    write_text_file "$job_dir/exit_code" "$status"
    set_job_state "$job_dir" 'failed'
    release_derived_data_slot_lease "$job_dir"
    rm -f "$ACTIVE_JOB_FILE"
    return 0
  fi
  if ! acquire_derived_data_slot_lease "$job_dir"; then
    status=75
    append_log_line "$job_dir/job.log" "[codex_verify] DerivedData slot lease is unavailable"
    write_text_file "$job_dir/finished_at" "$(timestamp_now)"
    generate_verification_artifacts "$job_dir" "$status"
    write_text_file "$job_dir/exit_code" "$status"
    set_job_state "$job_dir" 'failed'
    rm -f "$ACTIVE_JOB_FILE"
    return 0
  fi
  if ! validate_job_publication "$job_dir" >/dev/null 2>&1 || ! job_running_is_owned "$job_dir"; then
    status=70
    append_log_line "$job_dir/job.log" "[codex_verify] immutable publication or daemon ownership changed before execution"
    write_text_file "$job_dir/finished_at" "$(timestamp_now)"
    generate_verification_artifacts "$job_dir" "$status"
    write_text_file "$job_dir/exit_code" "$status"
    set_job_state "$job_dir" 'failed'
    release_derived_data_slot_lease "$job_dir"
    rm -f "$ACTIVE_JOB_FILE"
    return 0
  fi
  if ! validate_queued_worktree_session "$job_dir" "$repo_root" locked >>"$job_dir/job.log" 2>&1; then
    status=64
    append_log_line "$job_dir/job.log" "[codex_verify] Apple Worktree Session changed while acquiring the Slot lease"
    write_text_file "$job_dir/finished_at" "$(timestamp_now)"
    generate_verification_artifacts "$job_dir" "$status"
    write_text_file "$job_dir/exit_code" "$status"
    set_job_state "$job_dir" 'failed'
    release_derived_data_slot_lease "$job_dir"
    rm -f "$ACTIVE_JOB_FILE"
    return 0
  fi
  if [[ -f "$job_dir/worktree-session-request.json" ]]; then
    session_artifact_directory="$SESSION_ARTIFACTS_DIR/$(cat "$job_dir/worktree_session_artifact_namespace")/$job_id"
    mkdir -p "$session_artifact_directory"
    write_text_file "$job_dir/worktree_session_artifact_directory" "$session_artifact_directory"
  fi

  set +e
  (
    cd "$repo_root"
    if [[ -f "$job_dir/env.sh" ]]; then
      # shellcheck source=/dev/null
      source "$job_dir/env.sh"
    fi
    export CODEX_VERIFY_BYPASS_WRAPPER=1
    export CODEX_VERIFY_QUEUE_ROOT="$QUEUE_ROOT"
    export CODEX_VERIFY_JOB_ID="$job_id"
    export CODEX_VERIFY_JOB_DIR="$job_dir"
    if [[ -f "$job_dir/worktree-session-request.json" ]]; then
      export CODEX_WORKTREE_SESSION_REQUEST="$job_dir/worktree-session-request.json"
      export CODEX_VERIFY_ARTIFACT_DIR="$session_artifact_directory"
    fi
    unset XCODE_DERIVED_DATA
    "${READ_ARGS_RESULT[@]}"
  ) 2>&1 | tee -a "$job_dir/job.log"
  status=${PIPESTATUS[0]}
  set -e

  if ! validate_queued_worktree_session "$job_dir" "$repo_root" post >>"$job_dir/job.log" 2>&1; then
    status=64
    append_log_line "$job_dir/job.log" "[codex_verify] Apple Worktree Session source changed during execution"
  fi
  if ! write_session_artifact_manifest "$job_dir" >>"$job_dir/job.log" 2>&1; then
    status=65
    append_log_line "$job_dir/job.log" "[codex_verify] Worktree Session artifact manifest generation failed"
  fi

  write_text_file "$job_dir/finished_at" "$(timestamp_now)"
  append_log_line "$job_dir/job.log" "[codex_verify] finished_at=$(cat "$job_dir/finished_at")"
  append_log_line "$job_dir/job.log" "[codex_verify] status=$status"
  generate_verification_artifacts "$job_dir" "$status"
  if ! job_artifacts_are_valid "$job_dir" "$status" "$(cat "$job_dir/request_fingerprint")"; then
    append_log_line "$job_dir/job.log" "[codex_verify] structured artifact validation failed"
    status=65
  fi
  write_text_file "$job_dir/exit_code" "$status"
  if [[ $status -eq 0 ]]; then
    set_job_state "$job_dir" 'succeeded'
  else
    set_job_state "$job_dir" 'failed'
  fi
  release_derived_data_slot_lease "$job_dir"
  rm -f "$ACTIVE_JOB_FILE"
}

daemon_cleanup() {
  local current_pid active_job current_token
  set +e
  if [[ -n "$DAEMON_HEARTBEAT_PID" ]]; then
    kill "$DAEMON_HEARTBEAT_PID" 2>/dev/null || true
    wait "$DAEMON_HEARTBEAT_PID" 2>/dev/null || true
  fi
  current_pid="$(daemon_pid)"
  current_token="$(python3 - "$DAEMON_OWNER_FILE" <<'PY' 2>/dev/null || true
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("token", ""))
PY
)"
  if [[ "$current_pid" == "$$" && "$current_token" == "$DAEMON_TOKEN" ]]; then
    active_job="$(cat "$ACTIVE_JOB_FILE" 2>/dev/null || true)"
    active_job="$(controlled_job_dir "$active_job" 2>/dev/null || true)"
    if [[ -n "$active_job" && "$(job_state "$active_job")" == 'running' \
      && "$(cat "$active_job/runner_token" 2>/dev/null || true)" == "$DAEMON_TOKEN" ]]; then
      append_log_line "$active_job/job.log" "[codex_verify] daemon exited before job finalization"
      write_text_file "$active_job/finished_at" "$(timestamp_now)"
      write_text_file "$active_job/exit_code" '70'
      set_job_state "$active_job" 'failed'
      release_derived_data_slot_lease "$active_job"
    fi
    rm -f "$ACTIVE_JOB_FILE"
    rm -f "$DAEMON_PID_FILE"
    rm -f "$DAEMON_OWNER_FILE" "$DAEMON_HEARTBEAT_FILE"
  fi
}

daemon_main() {
  mkdir -p "$QUEUE_ROOT" "$JOBS_DIR" "$DERIVED_DATA_SLOTS_DIR" "$SESSION_ARTIFACTS_DIR"

  if daemon_running; then
    local existing_pid
    existing_pid="$(daemon_pid)"
    if [[ "$existing_pid" != "$$" ]]; then
      exit 0
    fi
  fi

  [[ "$DAEMON_TOKEN" =~ ^[0-9a-f]{64}$ ]] || DAEMON_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  if ! adopt_daemon_start_lock "$DAEMON_TOKEN"; then
    daemon_running && exit 0
    die "another daemon startup owns $START_LOCK_DIR"
  fi
  queue_maintenance startup "${CODEX_VERIFY_QUEUE_INVALID_POLICY:-quarantine}"
  write_daemon_identity "$DAEMON_TOKEN"
  release_start_lock
  trap daemon_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  daemon_heartbeat_loop "$DAEMON_TOKEN" &
  DAEMON_HEARTBEAT_PID=$!

  mark_running_jobs_failed_on_recovery

  while true; do
    if next_queued_job; then
      run_job "$NEXT_QUEUED_JOB"
      continue
    fi
    sleep 1
  done
}

MODE=''
SCRIPT_PATH="$(resolve_path "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd -P)"
USER_REPO_ROOT=''
FORCE_REVERIFY="${CODEX_VERIFY_FORCE:-0}"
NO_CACHE="${CODEX_VERIFY_NO_CACHE:-0}"
WORKTREE_SESSION_REQUEST_PATH=''
WORKTREE_SESSION_REQUEST_CANONICAL=''
WORKTREE_SESSION_REQUEST_SHA256=''
WORKTREE_SESSION_ID=''
WORKTREE_SESSION_ATTEMPT_ID=''
WORKTREE_SESSION_SOURCE_IDENTITY=''
WORKTREE_SESSION_ENVIRONMENT_FINGERPRINT=''
WORKTREE_SESSION_DERIVED_DATA_SLOT=''
WORKTREE_SESSION_TEST_PLAN=''
WORKTREE_SESSION_ARTIFACT_NAMESPACE=''
QUEUE_STATUS_JSON='0'
QUEUE_DOCTOR_REPAIR='0'
QUEUE_DOCTOR_DELETE_INVALID='0'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      [[ $# -ge 2 ]] || die "--repo-root requires a path"
      USER_REPO_ROOT="$2"
      shift 2
      ;;
    --force)
      FORCE_REVERIFY='1'
      shift
      ;;
    --no-cache)
      NO_CACHE='1'
      shift
      ;;
    --worktree-session-request)
      [[ $# -ge 2 ]] || die "--worktree-session-request requires a JSON path"
      WORKTREE_SESSION_REQUEST_PATH="$(absolute_path_without_resolving_symlinks "$2")"
      shift 2
      ;;
    --|--build-check|--queue-status|--queue-doctor|--daemon|--help|-h)
      break
      ;;
    *)
      usage >&2
      exit 64
      ;;
  esac
done

if [[ -n "$USER_REPO_ROOT" ]]; then
  REPO_ROOT="$(resolve_path "$USER_REPO_ROOT")"
elif [[ "$(basename "$SCRIPT_PATH")" == 'codex_verify.sh' ]]; then
  REPO_ROOT="$SCRIPT_DIR"
else
  REPO_ROOT="$(pwd -P)"
fi
XCODE_ENV_FILE="$REPO_ROOT/.codex/xcodebuild.env"
SYSTEM_DERIVED_DATA_HOME="$HOME/Library/Developer/Xcode/DerivedData"
QUEUE_ROOT="$(queue_root)"
QUEUE_SCHEMA_VERSION='2.0'
QUEUE_GENERATION_ID='codex-verify-v2'
JOBS_DIR="$QUEUE_ROOT/jobs"
STAGING_DIR="$QUEUE_ROOT/staging"
REQUESTS_DIR="$QUEUE_ROOT/requests"
DERIVED_DATA_SLOTS_DIR="$QUEUE_ROOT/derived-data-slots"
SESSION_ARTIFACTS_DIR="$QUEUE_ROOT/artifacts"
DAEMON_PID_FILE="$QUEUE_ROOT/daemon.pid"
DAEMON_OWNER_FILE="$QUEUE_ROOT/daemon-owner.json"
DAEMON_HEARTBEAT_FILE="$QUEUE_ROOT/daemon-heartbeat.json"
DAEMON_STDOUT_LOG="$QUEUE_ROOT/daemon.log"
ACTIVE_JOB_FILE="$QUEUE_ROOT/active_job"
START_LOCK_DIR="$QUEUE_ROOT/start.lockdir"
DAEMON_TOKEN="${CODEX_VERIFY_DAEMON_TOKEN:-}"
DAEMON_HEARTBEAT_PID=''
STATUS_FILTER_REPO_ROOT=''

case "${1:-}" in
  --)
    shift
    [[ $# -gt 0 ]] || die "missing xcodebuild arguments after --"
    MODE='xcodebuild'
    RAW_ARGS=("$@")
    if [[ "${RAW_ARGS[0]}" == 'xcodebuild' ]]; then
      COMMAND=("xcodebuild" "${RAW_ARGS[@]:1}")
    else
      COMMAND=("xcodebuild" "${RAW_ARGS[@]}")
    fi
    ;;
  --build-check)
    shift
    [[ $# -ge 2 ]] || die "--build-check requires <build-check.sh> <repo-root>"
    MODE='build-check'
    BUILD_CHECK_SCRIPT="$1"
    BUILD_CHECK_ROOT="$2"
    shift 2
    [[ -f "$BUILD_CHECK_SCRIPT" ]] || die "build-check script not found: $BUILD_CHECK_SCRIPT"
    BUILD_CHECK_SCRIPT="$(resolve_path "$BUILD_CHECK_SCRIPT")"
    BUILD_CHECK_ROOT="$(resolve_path "$BUILD_CHECK_ROOT")"
    REPO_ROOT="$BUILD_CHECK_ROOT"
    XCODE_ENV_FILE="$REPO_ROOT/.codex/xcodebuild.env"
    COMMAND=(bash "$BUILD_CHECK_SCRIPT" "$BUILD_CHECK_ROOT" "$@")
    ;;
  --queue-status)
    shift
    MODE='queue-status'
    COMMAND=()
    if [[ "${1:-}" == '--json' ]]; then
      QUEUE_STATUS_JSON='1'
      shift
    fi
    [[ $# -eq 0 ]] || die "unsupported --queue-status arguments"
    if [[ -n "$USER_REPO_ROOT" || "$(basename "$SCRIPT_PATH")" == 'codex_verify.sh' ]]; then
      STATUS_FILTER_REPO_ROOT="$REPO_ROOT"
    fi
    ;;
  --queue-doctor)
    shift
    MODE='queue-doctor'
    COMMAND=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --repair)
          QUEUE_DOCTOR_REPAIR='1'
          ;;
        --delete-invalid)
          QUEUE_DOCTOR_DELETE_INVALID='1'
          ;;
        *)
          die "unsupported --queue-doctor argument: $1"
          ;;
      esac
      shift
    done
    if [[ "$QUEUE_DOCTOR_DELETE_INVALID" == '1' && "$QUEUE_DOCTOR_REPAIR" != '1' ]]; then
      die "--delete-invalid requires --repair"
    fi
    ;;
  --daemon)
    MODE='daemon'
    COMMAND=()
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac

META_WORKSPACE=''
META_PROJECT=''
META_SCHEME=''
META_SCHEME_FROM_ARGS='0'
META_DESTINATION=''
META_CONFIGURATION=''
META_ACTION=''

if [[ "$MODE" == 'xcodebuild' ]]; then
  load_metadata_from_xcode_args "${COMMAND[@]:1}"
fi

if [[ -n "$WORKTREE_SESSION_REQUEST_PATH" ]]; then
  [[ "$MODE" == 'xcodebuild' || "$MODE" == 'build-check' ]] \
    || die "--worktree-session-request is only valid for queued verification"
  [[ -f "$WORKTREE_SESSION_REQUEST_PATH" && ! -L "$WORKTREE_SESSION_REQUEST_PATH" ]] \
    || die "Apple Worktree Session request file is missing or unsafe"
  WORKTREE_SESSION_REQUEST_CANONICAL="$(
    validate_worktree_session_request "$WORKTREE_SESSION_REQUEST_PATH" '' ''
  )" || die "Apple Worktree Session request validation failed"
  WORKTREE_SESSION_ID="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" session_id)" \
    || die "Apple Worktree Session request session_id is invalid"
  WORKTREE_SESSION_ATTEMPT_ID="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" attempt_id)" \
    || die "Apple Worktree Session request attempt_id is invalid"
  WORKTREE_SESSION_SOURCE_IDENTITY="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" source_identity)" \
    || die "Apple Worktree Session request source_identity is invalid"
  WORKTREE_SESSION_ENVIRONMENT_FINGERPRINT="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" environment_fingerprint)" \
    || die "Apple Worktree Session request environment_fingerprint is invalid"
  WORKTREE_SESSION_DERIVED_DATA_SLOT="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" derived_data_slot)" \
    || die "Apple Worktree Session request derived_data_slot is invalid"
  WORKTREE_SESSION_TEST_PLAN="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" test_plan)" \
    || die "Apple Worktree Session request test_plan is invalid"
  WORKTREE_SESSION_ARTIFACT_NAMESPACE="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" artifact_namespace)" \
    || die "Apple Worktree Session request artifact_namespace is invalid"
  request_destination="$(request_json_field "$WORKTREE_SESSION_REQUEST_CANONICAL" destination)" \
    || die "Apple Worktree Session request destination is invalid"
  if [[ -n "$META_DESTINATION" && "$META_DESTINATION" != "$request_destination" ]]; then
    die "Apple Worktree Session destination conflicts with xcodebuild arguments"
  fi
  META_DESTINATION="$request_destination"
  export XCODE_DESTINATION="$request_destination"
  export XCODE_TEST_PLAN="$WORKTREE_SESSION_TEST_PLAN"
  export CODEX_WORKTREE_SESSION_ID="$WORKTREE_SESSION_ID"
  export CODEX_WORKTREE_SESSION_ATTEMPT_ID="$WORKTREE_SESSION_ATTEMPT_ID"
  export CODEX_WORKTREE_SESSION_SOURCE_IDENTITY="$WORKTREE_SESSION_SOURCE_IDENTITY"
  export CODEX_WORKTREE_SESSION_ENVIRONMENT_FINGERPRINT="$WORKTREE_SESSION_ENVIRONMENT_FINGERPRINT"
  export CODEX_WORKTREE_SESSION_DERIVED_DATA_SLOT="$WORKTREE_SESSION_DERIVED_DATA_SLOT"
  export CODEX_WORKTREE_SESSION_ARTIFACT_NAMESPACE="$WORKTREE_SESSION_ARTIFACT_NAMESPACE"
fi
load_metadata_defaults
normalize_xcodebuild_entry_args
assert_configured_scheme_exists

if [[ "$MODE" == 'daemon' ]]; then
  daemon_main
  exit 0
fi

assert_no_legacy_derived_data_settings

if [[ "$MODE" == 'queue-status' ]]; then
  if [[ "$QUEUE_STATUS_JSON" == '1' ]]; then
    set +e
    queue_maintenance doctor
    queue_status_code=$?
    set -e
    if [[ $queue_status_code -ne 0 && $queue_status_code -ne 2 ]]; then
      exit "$queue_status_code"
    fi
  else
    queue_status
  fi
  exit 0
fi

if [[ "$MODE" == 'queue-doctor' ]]; then
  if [[ "$QUEUE_DOCTOR_REPAIR" == '1' ]]; then
    if [[ "$QUEUE_DOCTOR_DELETE_INVALID" == '1' ]]; then
      queue_maintenance repair delete
    else
      queue_maintenance repair quarantine
    fi
  else
    queue_maintenance doctor
  fi
  exit 0
fi

if [[ -n "$WORKTREE_SESSION_REQUEST_PATH" ]]; then
  if [[ "$MODE" == 'xcodebuild' && "$META_ACTION" == 'test-without-building' ]]; then
    die "Apple Worktree Session test-without-building requires a daemon-validated immutable build artifact identity and is not yet enabled"
  fi
  if [[ "$MODE" == 'xcodebuild' && "$META_ACTION" =~ ^(test|build-for-testing|test-without-building)$ ]]; then
    provided_test_plan=''
    command_index=0
    while [[ $command_index -lt ${#COMMAND[@]} ]]; do
      command_part="${COMMAND[$command_index]}"
      if [[ "$command_part" == '-testPlan' ]]; then
        command_index=$(( command_index + 1 ))
        [[ $command_index -lt ${#COMMAND[@]} ]] || die "-testPlan requires a value"
        provided_test_plan="${COMMAND[$command_index]}"
        break
      elif [[ "$command_part" == -testPlan:* ]]; then
        provided_test_plan="${command_part#-testPlan:}"
        break
      fi
      command_index=$(( command_index + 1 ))
    done
    if [[ -n "$provided_test_plan" && "$provided_test_plan" != "$WORKTREE_SESSION_TEST_PLAN" ]]; then
      die "Apple Worktree Session test plan conflicts with xcodebuild arguments"
    fi
    if [[ -z "$provided_test_plan" ]]; then
      COMMAND+=("-testPlan" "$WORKTREE_SESSION_TEST_PLAN")
    fi
  fi
  WORKTREE_SESSION_REQUEST_SHA256="$(printf '%s\n' "$WORKTREE_SESSION_REQUEST_CANONICAL" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
  export CODEX_WORKTREE_SESSION_REQUEST_SHA256="$WORKTREE_SESSION_REQUEST_SHA256"
fi

COMMAND_PREVIEW="$(join_quoted_command "${COMMAND[@]}")"
mkdir -p "$JOBS_DIR" "$STAGING_DIR" "$REQUESTS_DIR"
ensure_queue_metadata || die "shared queue metadata is incompatible at $QUEUE_ROOT; run --queue-doctor for details"
REQUEST_FINGERPRINT="$(compute_request_fingerprint)"
REQUEST_CACHEABLE='1'
if [[ "$REQUEST_FINGERPRINT" == volatile-* ]]; then
  REQUEST_CACHEABLE='0'
fi
REQUEST_LOCK_DIR="$REQUESTS_DIR/$REQUEST_FINGERPRINT.lockdir"
JOB_REUSE_KIND='new'

queue_or_reuse_job
if [[ "$JOB_REUSE_KIND" == 'cached' ]]; then
  print_verification_report "$JOB_DIR" "$JOB_REUSE_KIND"
  exit "$(cat "$JOB_DIR/exit_code" 2>/dev/null || printf '%s' '1')"
fi
ensure_daemon_running
wait_for_job
exit $?
