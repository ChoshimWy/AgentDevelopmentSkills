"""Durable, host-owned handoff transport for external Provider invocations.

The workflow Core never executes a binding.  It freezes the node execution
contract, publishes an Adapter Request, grants one time-bounded claim, and
accepts only an Adapter Result that validates against that exact request.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Iterator

from ..canonical_json import MAX_CONTRACT_JSON_BYTES, dumps, loads, sha256
from ..models import ContractError, require_fields, require_version
from .contracts import build_adapter_request, validate_adapter_request, validate_adapter_result

try:  # pragma: no cover - selected by the host platform
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
    import msvcrt as _msvcrt


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^adapter-request-[0-9a-f]{16}$")
_MAX_TIMESTAMP = (1 << 64) - 1
_RECORD_FIELDS = {
    "schema_version",
    "transport_id",
    "prepared_at",
    "request",
    "execution_contract",
    "claim",
    "result",
    "submitted_at",
}
_EXECUTION_FIELDS = {
    "approval",
    "idempotent",
    "max_retries",
    "permission_profile",
    "provider_manifest_digest",
    "resource_keys",
    "side_effects",
    "timeout_seconds",
}
_CLAIM_FIELDS = {
    "actor_id",
    "claim_token_sha256",
    "claimed_at",
    "deadline",
}
_SELECTION_FIELDS = {
    "schema_version",
    "plan_fingerprint",
    "requests",
}
_MAX_SELECTION_REQUESTS = 16_384


def prepare_provider_invocation(
    root: str | Path,
    plan: dict[str, Any],
    node_id: str,
    *,
    context: dict[str, Any],
    invocation_id: str,
    package_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically publish one immutable Provider handoff record."""

    validate_provider_invocation_plan(plan, package_lock)
    return _prepare_provider_invocation_at(
        root,
        plan,
        node_id,
        context=context,
        invocation_id=invocation_id,
        prepared_at=int(time.time()),
    )


def _prepare_provider_invocation_at(
    root: str | Path,
    plan: dict[str, Any],
    node_id: str,
    *,
    context: dict[str, Any],
    invocation_id: str,
    prepared_at: int,
) -> dict[str, Any]:
    request = build_adapter_request(
        plan,
        node_id,
        context=context,
        invocation_id=invocation_id,
    )
    node = next(
        node
        for node in plan["nodes"]
        if isinstance(node, dict) and node.get("id") == node_id
    )
    execution_contract = _freeze_execution_contract(node)
    timestamp = _timestamp(prepared_at, "prepared_at")
    identity = {
        "execution_contract": execution_contract,
        "prepared_at": timestamp,
        "request": request,
        "schema_version": "1.0",
    }
    record = {
        "schema_version": "1.0",
        "transport_id": f"provider-invocation-{sha256(identity)[:16]}",
        "prepared_at": timestamp,
        "request": request,
        "execution_contract": execution_contract,
        "claim": None,
        "result": None,
        "submitted_at": None,
    }
    validate_provider_invocation(record)
    store = ProviderInvocationStore(root)
    with store.locked(create_root=True):
        store.create(record)
    return deepcopy(record)


def claim_provider_invocation(
    root: str | Path,
    request_id: str,
    *,
    actor_id: str,
    claim_token: str,
) -> dict[str, Any]:
    """Grant the only claim for one request until its frozen timeout."""

    return _claim_provider_invocation_at(
        root,
        request_id,
        actor_id=actor_id,
        claim_token=claim_token,
        claimed_at=int(time.time()),
    )


def _claim_provider_invocation_at(
    root: str | Path,
    request_id: str,
    *,
    actor_id: str,
    claim_token: str,
    claimed_at: int,
) -> dict[str, Any]:
    if not _nonempty(actor_id) or len(actor_id) > 128:
        raise ContractError("provider invocation actor_id must be a non-empty string")
    _validate_claim_token(claim_token)
    timestamp = _timestamp(claimed_at, "claimed_at")
    store = ProviderInvocationStore(root)
    with store.locked():
        record = store.load(request_id)
        if record["result"] is not None:
            raise ContractError("provider invocation is already submitted")
        if record["claim"] is not None:
            state = provider_invocation_state(record, at=timestamp)
            raise ContractError(f"provider invocation cannot be claimed from {state} state")
        requested_resources = set(record["execution_contract"]["resource_keys"])
        for existing in store.list():
            if (
                existing["request"]["request_id"] != request_id
                and provider_invocation_state(existing, at=timestamp) == "claimed"
            ):
                conflicts = sorted(
                    requested_resources
                    & set(existing["execution_contract"]["resource_keys"])
                )
                if conflicts:
                    raise ContractError(
                        f"provider invocation resource is already claimed: {conflicts[0]}"
                    )
        timeout = record["execution_contract"]["timeout_seconds"]
        if timestamp > _MAX_TIMESTAMP - timeout:
            raise ContractError("provider invocation claim deadline overflows")
        deadline = timestamp + timeout
        record["claim"] = {
            "actor_id": actor_id,
            "claim_token_sha256": sha256(claim_token),
            "claimed_at": timestamp,
            "deadline": deadline,
        }
        validate_provider_invocation(record)
        store.replace(record)
        return deepcopy(record)


def submit_provider_invocation(
    root: str | Path,
    request_id: str,
    result: dict[str, Any],
    *,
    claim_token: str,
) -> dict[str, Any]:
    """Validate and atomically publish the terminal result for one live claim."""

    return _submit_provider_invocation_at(
        root,
        request_id,
        result,
        claim_token=claim_token,
        submitted_at=int(time.time()),
    )


def _submit_provider_invocation_at(
    root: str | Path,
    request_id: str,
    result: dict[str, Any],
    *,
    claim_token: str,
    submitted_at: int,
) -> dict[str, Any]:
    _validate_claim_token(claim_token)
    timestamp = _timestamp(submitted_at, "submitted_at")
    store = ProviderInvocationStore(root)
    with store.locked():
        record = store.load(request_id)
        if record["result"] is not None:
            raise ContractError("provider invocation is already submitted")
        claim = record["claim"]
        if claim is None:
            raise ContractError("provider invocation must be claimed before submission")
        if claim["claim_token_sha256"] != sha256(claim_token):
            raise ContractError("provider invocation claim token does not match")
        if timestamp < claim["claimed_at"]:
            raise ContractError("provider invocation submitted_at precedes claim")
        if timestamp >= claim["deadline"]:
            raise ContractError("provider invocation claim has expired")
        validate_adapter_result(record["request"], result)
        record["result"] = deepcopy(result)
        record["submitted_at"] = timestamp
        validate_provider_invocation(record)
        store.replace(record)
        return deepcopy(record)


def inspect_provider_invocation(
    root: str | Path,
    request_id: str,
) -> dict[str, Any]:
    """Load one record and derive its current state without mutating it."""

    return _inspect_provider_invocation_at(
        root,
        request_id,
        at=int(time.time()),
    )


def _inspect_provider_invocation_at(
    root: str | Path,
    request_id: str,
    *,
    at: int,
) -> dict[str, Any]:
    timestamp = _timestamp(at, "at")
    record = ProviderInvocationStore(root).load(request_id)
    return {
        "invocation": record,
        "schema_version": "1.0",
        "state": provider_invocation_state(record, at=timestamp),
    }


def collect_submitted_results(
    root: str | Path,
    plan_fingerprint: str,
    selection: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Collect only explicitly selected results for RecordedAdapterExecutor."""

    return _collect_submitted_results_at(
        root,
        plan_fingerprint,
        selection,
        at=int(time.time()),
    )


def _collect_submitted_results_at(
    root: str | Path,
    plan_fingerprint: str,
    selection: dict[str, Any],
    *,
    at: int,
) -> dict[str, dict[str, Any]]:
    if not _nonempty(plan_fingerprint):
        raise ContractError("provider invocation plan_fingerprint is invalid")
    validate_provider_invocation_selection(selection)
    if selection["plan_fingerprint"] != plan_fingerprint:
        raise ContractError(
            "provider invocation selection plan_fingerprint does not match Plan"
        )
    timestamp = _timestamp(at, "at")
    store = ProviderInvocationStore(root)
    if not store.root.exists():
        if selection["requests"]:
            raise ContractError("provider invocation selection request is not submitted")
        return {}
    results: dict[str, dict[str, Any]] = {}
    with store.locked():
        records = {
            record["request"]["request_id"]: record
            for record in store.list()
            if record["request"]["plan_fingerprint"] == plan_fingerprint
        }
        for node_id, request_id in selection["requests"].items():
            record = records.get(request_id)
            if (
                record is None
                or record["request"]["node_id"] != node_id
                or provider_invocation_state(record, at=timestamp) != "submitted"
            ):
                raise ContractError(
                    f"provider invocation selection request is not submitted for node: {node_id}"
                )
            results[node_id] = deepcopy(record["result"])
    return results


def load_claim_token_file(path: str | Path) -> str:
    """Read a small no-follow bearer token file without emitting its contents."""

    token_path = Path(path)
    if token_path.is_symlink() or not token_path.is_file():
        raise ContractError("provider invocation claim token file is unsafe")
    descriptor = os.open(
        token_path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        before = os.stat(token_path, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077:
            raise ContractError("provider invocation claim token file permissions are too broad")
        if _metadata_identity(before) != _metadata_identity(opened):
            raise ContractError("provider invocation claim token file changed while opening")
        if opened.st_size > 4096:
            raise ContractError("provider invocation claim token file is too large")
        data = os.read(descriptor, 4097)
        if len(data) > 4096:
            raise ContractError("provider invocation claim token file is too large")
        after = os.stat(token_path, follow_symlinks=False)
        if _metadata_identity(after) != _metadata_identity(opened):
            raise ContractError("provider invocation claim token file changed while reading")
    finally:
        os.close(descriptor)
    try:
        token = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("provider invocation claim token file is not UTF-8") from error
    if token.endswith("\r\n"):
        token = token[:-2]
    elif token.endswith("\n"):
        token = token[:-1]
    if "\n" in token or "\r" in token:
        raise ContractError("provider invocation claim token file must contain one line")
    _validate_claim_token(token)
    return token


def provider_invocation_state(record: dict[str, Any], *, at: int) -> str:
    """Derive prepared/claimed/expired/submitted from validated persisted data."""

    validate_provider_invocation(record)
    _timestamp(at, "at")
    if record["result"] is not None:
        return "submitted"
    claim = record["claim"]
    if claim is None:
        return "prepared"
    if at >= claim["deadline"]:
        return "expired"
    return "claimed"


def validate_provider_invocation(value: dict[str, Any]) -> None:
    """Validate the persisted Provider Invocation v1 record."""

    if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
        raise ContractError("provider-invocation fields are invalid")
    require_version(value)
    if not _nonempty(value["transport_id"]) or not re.fullmatch(
        r"provider-invocation-[0-9a-f]{16}", value["transport_id"]
    ):
        raise ContractError("provider-invocation transport_id is invalid")
    prepared_at = _timestamp(value["prepared_at"], "prepared_at")
    validate_adapter_request(value["request"])
    contract = value["execution_contract"]
    if not isinstance(contract, dict) or set(contract) != _EXECUTION_FIELDS:
        raise ContractError("provider-invocation execution_contract fields are invalid")
    if not isinstance(contract["idempotent"], bool):
        raise ContractError("provider-invocation idempotent is invalid")
    if (
        not isinstance(contract["max_retries"], int)
        or isinstance(contract["max_retries"], bool)
        or contract["max_retries"] < 0
        or contract["max_retries"] > _MAX_TIMESTAMP
    ):
        raise ContractError("provider-invocation max_retries is invalid")
    if (
        not isinstance(contract["timeout_seconds"], int)
        or isinstance(contract["timeout_seconds"], bool)
        or contract["timeout_seconds"] <= 0
        or contract["timeout_seconds"] > _MAX_TIMESTAMP
    ):
        raise ContractError("provider-invocation timeout_seconds is invalid")
    for field in ("permission_profile", "provider_manifest_digest"):
        if not _nonempty(contract[field]):
            raise ContractError(f"provider-invocation {field} is invalid")
    for field in ("resource_keys", "side_effects"):
        items = contract[field]
        if (
            not isinstance(items, list)
            or any(not _nonempty(item) for item in items)
            or items != sorted(set(items))
        ):
            raise ContractError(f"provider-invocation {field} must be sorted unique strings")
    if contract["approval"] is not None:
        raise ContractError(
            "approval-bound provider invocation requires a runtime-granted attempt proof"
        )
    identity = {
        "execution_contract": contract,
        "prepared_at": prepared_at,
        "request": value["request"],
        "schema_version": "1.0",
    }
    if value["transport_id"] != f"provider-invocation-{sha256(identity)[:16]}":
        raise ContractError("provider-invocation transport_id does not match frozen identity")

    claim = value["claim"]
    result = value["result"]
    submitted_at = value["submitted_at"]
    if claim is None:
        if result is not None or submitted_at is not None:
            raise ContractError("provider-invocation result requires a claim")
        return
    if not isinstance(claim, dict) or set(claim) != _CLAIM_FIELDS:
        raise ContractError("provider-invocation claim fields are invalid")
    if (
        not _nonempty(claim["actor_id"])
        or len(claim["actor_id"]) > 128
        or not _HEX_64.fullmatch(
            claim["claim_token_sha256"]
            if isinstance(claim["claim_token_sha256"], str)
            else ""
        )
    ):
        raise ContractError("provider-invocation claim identity is invalid")
    claimed_at = _timestamp(claim["claimed_at"], "claimed_at")
    deadline = _timestamp(claim["deadline"], "deadline")
    if claimed_at < prepared_at or deadline != claimed_at + contract["timeout_seconds"]:
        raise ContractError("provider-invocation claim deadline is invalid")
    if result is None:
        if submitted_at is not None:
            raise ContractError("provider-invocation submitted_at requires a result")
        return
    validate_adapter_result(value["request"], result)
    submitted_at = _timestamp(submitted_at, "submitted_at")
    if submitted_at < claimed_at or submitted_at >= deadline:
        raise ContractError("provider-invocation submission timestamp is outside the claim")


def validate_provider_invocation_selection(value: dict[str, Any]) -> None:
    """Validate the explicit Provider result selection consumed by runtime."""

    if not isinstance(value, dict) or set(value) != _SELECTION_FIELDS:
        raise ContractError("provider-invocation-selection fields are invalid")
    require_version(value)
    if not _nonempty(value["plan_fingerprint"]):
        raise ContractError(
            "provider-invocation-selection plan_fingerprint is invalid"
        )
    requests = value["requests"]
    if (
        not isinstance(requests, dict)
        or len(requests) > _MAX_SELECTION_REQUESTS
        or any(not _nonempty(node_id) for node_id in requests)
        or any(
            not isinstance(request_id, str)
            or not _REQUEST_ID.fullmatch(request_id)
            for request_id in requests.values()
        )
    ):
        raise ContractError("provider-invocation-selection requests are invalid")
    if len(set(requests.values())) != len(requests):
        raise ContractError(
            "provider-invocation-selection request ids must be unique"
        )


def _freeze_execution_contract(node: dict[str, Any]) -> dict[str, Any]:
    require_fields(node, _EXECUTION_FIELDS, "workflow-plan.node")
    if node["approval"] is not None:
        raise ContractError(
            "approval-bound provider invocation requires a runtime-granted attempt proof"
        )
    for field in ("resource_keys", "side_effects"):
        items = node[field]
        if (
            not isinstance(items, list)
            or any(not _nonempty(item) for item in items)
            or len(items) != len(set(items))
        ):
            raise ContractError(f"provider-invocation {field} must be unique strings")
    value = {
        "approval": deepcopy(node["approval"]),
        "idempotent": node["idempotent"],
        "max_retries": node["max_retries"],
        "permission_profile": node["permission_profile"],
        "provider_manifest_digest": node["provider_manifest_digest"],
        "resource_keys": sorted(node["resource_keys"]),
        "side_effects": sorted(node["side_effects"]),
        "timeout_seconds": node["timeout_seconds"],
    }
    contract = value
    if not isinstance(contract["idempotent"], bool):
        raise ContractError("provider-invocation idempotent is invalid")
    if (
        not isinstance(contract["max_retries"], int)
        or isinstance(contract["max_retries"], bool)
        or not 0 <= contract["max_retries"] <= _MAX_TIMESTAMP
    ):
        raise ContractError("provider-invocation max_retries is invalid")
    if (
        not isinstance(contract["timeout_seconds"], int)
        or isinstance(contract["timeout_seconds"], bool)
        or not 0 < contract["timeout_seconds"] <= _MAX_TIMESTAMP
    ):
        raise ContractError("provider-invocation timeout_seconds is invalid")
    for field in ("permission_profile", "provider_manifest_digest"):
        if not _nonempty(contract[field]):
            raise ContractError(f"provider-invocation {field} is invalid")
    if contract["approval"] is not None:
        raise ContractError(
            "approval-bound provider invocation requires a runtime-granted attempt proof"
        )
    return value


def validate_provider_invocation_plan(
    plan: dict[str, Any],
    package_lock: dict[str, Any] | None,
) -> None:
    """Validate compiled Plan identity and active Lock provenance for handoff."""

    from ..contracts import validate
    from ..package_lock import validate_package_lock, validate_plan_package_lock

    validate("workflow-plan", plan)
    expected_fingerprint = sha256({
        key: item for key, item in plan.items() if key not in {"fingerprint", "plan_id"}
    })
    if plan["fingerprint"] != expected_fingerprint:
        raise ContractError("workflow-plan fingerprint mismatch")
    if plan["plan_id"] != f"plan-{expected_fingerprint[:12]}":
        raise ContractError("workflow-plan id mismatch")
    if plan.get("package_lock_hash") is None:
        if package_lock is not None:
            raise ContractError(
                "workflow plan is not frozen to the supplied package Lockfile"
            )
        return
    if package_lock is None:
        raise ContractError(
            "locked workflow operation requires the current package Lockfile"
        )
    validate_package_lock(package_lock)
    validate_plan_package_lock(plan, package_lock)


class ProviderInvocationStore:
    """Private canonical-JSON store guarded by one process-safe lock."""

    def __init__(self, root: str | Path):
        candidate = Path(root).expanduser()
        if candidate.is_symlink():
            raise ContractError("provider invocation root is unsafe")
        self.root = candidate.resolve()
        self.lock_path = self.root / ".invocations.lock"

    @contextmanager
    def locked(self, *, create_root: bool = False) -> Iterator[None]:
        self._ensure_private_root(create=create_root)
        try:
            before = os.lstat(self.lock_path)
        except FileNotFoundError:
            before = None
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        with os.fdopen(descriptor, "r+b", buffering=0) as handle:
            opened = os.fstat(handle.fileno())
            current = os.lstat(self.lock_path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or _metadata_identity(opened) != _metadata_identity(current)
                or before is not None
                and _metadata_identity(before) != _metadata_identity(opened)
            ):
                raise ContractError("provider invocation lock is unsafe")
            if os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077:
                raise ContractError("provider invocation lock permissions are too broad")
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)

    def create(self, record: dict[str, Any]) -> None:
        path = self._path(record["request"]["request_id"])
        if path.exists() or path.is_symlink():
            raise ContractError("provider invocation already exists")
        self._atomic_write(path, record, replace=False)

    def replace(self, record: dict[str, Any]) -> None:
        path = self._path(record["request"]["request_id"])
        if path.is_symlink() or not path.is_file():
            raise ContractError("provider invocation does not exist or is unsafe")
        self._atomic_write(path, record, replace=True)

    def load(self, request_id: str) -> dict[str, Any]:
        self._ensure_private_root(create=False)
        path = self._path(request_id)
        try:
            value = self._read_record(path)
        except FileNotFoundError:
            raise ContractError(f"provider invocation does not exist: {request_id}")
        validate_provider_invocation(value)
        if value["request"]["request_id"] != request_id:
            raise ContractError("provider invocation filename identity mismatch")
        return value

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        self._ensure_private_root(create=False)
        records = []
        for path in sorted(self.root.glob("adapter-request-*.json")):
            value = self._read_record(path)
            validate_provider_invocation(value)
            if path.stem != value["request"]["request_id"]:
                raise ContractError("provider invocation filename identity mismatch")
            records.append(value)
        return records

    def _path(self, request_id: str) -> Path:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ContractError("provider invocation request_id is invalid")
        return self.root / f"{request_id}.json"

    def _ensure_private_root(self, *, create: bool) -> None:
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            metadata = os.lstat(self.root)
        except FileNotFoundError:
            raise ContractError("provider invocation root does not exist")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ContractError("provider invocation root is unsafe")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ContractError("provider invocation root permissions are too broad")

    def _read_record(self, path: Path) -> dict[str, Any]:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise ContractError(f"provider invocation entry is unsafe: {path.name}")
        if before.st_size > MAX_CONTRACT_JSON_BYTES:
            raise ContractError(
                f"contract input has more than {MAX_CONTRACT_JSON_BYTES} bytes"
            )
        if os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
            raise ContractError("provider invocation entry permissions are too broad")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _metadata_identity(before) != _metadata_identity(opened)
            ):
                raise ContractError("provider invocation entry changed while opening")
            chunks = []
            remaining = MAX_CONTRACT_JSON_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            if len(encoded) > MAX_CONTRACT_JSON_BYTES:
                raise ContractError(
                    f"contract input has more than {MAX_CONTRACT_JSON_BYTES} bytes"
                )
            current = os.lstat(path)
            if (
                not stat.S_ISREG(current.st_mode)
                or _metadata_identity(opened) != _metadata_identity(current)
            ):
                raise ContractError("provider invocation entry changed while reading")
        finally:
            os.close(descriptor)
        value = loads(encoded)
        if not isinstance(value, dict):
            raise ContractError("provider-invocation must be an object")
        return value

    def _atomic_write(self, path: Path, value: dict[str, Any], *, replace: bool) -> None:
        encoded = dumps(value).encode("utf-8")
        _ensure_record_size(encoded, MAX_CONTRACT_JSON_BYTES)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=self.root,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                if path.is_symlink() or not path.is_file():
                    raise ContractError("provider invocation destination is unsafe")
            elif path.exists() or path.is_symlink():
                raise ContractError("provider invocation destination already exists")
            os.replace(temporary, path)
            if os.name != "nt":
                directory_descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _timestamp(value: Any, label: str) -> int:
    if value is None:
        return int(time.time())
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > _MAX_TIMESTAMP
    ):
        raise ContractError(f"provider invocation {label} is invalid")
    return value


def _validate_claim_token(value: Any) -> None:
    if not isinstance(value, str) or not 32 <= len(value) <= 4096:
        raise ContractError(
            "provider invocation claim token must contain between 32 and 4096 characters"
        )


def _ensure_record_size(encoded: bytes, maximum: int) -> None:
    if len(encoded) > maximum:
        raise ContractError(f"contract input has more than {maximum} bytes")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _metadata_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _lock_handle(handle: Any) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        return
    handle.seek(0)
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)


def _unlock_handle(handle: Any) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    handle.seek(0)
    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
