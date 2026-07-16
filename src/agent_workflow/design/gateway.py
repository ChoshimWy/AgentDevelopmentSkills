"""Credential-free Design Source Gateway and exact-scope write approval gate."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..canonical_json import sha256
from ..models import ContractError
from .contracts import validate_design_evidence


@dataclass(frozen=True)
class WriteApproval:
    approval_id: str
    attempt_id: str
    document_id: str
    page_id: str | None
    node_ids: tuple[str, ...]

    def allows(self, *, attempt_id: str, document_id: str, page_id: str | None, node_ids: tuple[str, ...]) -> bool:
        return (
            bool(self.approval_id)
            and self.attempt_id == attempt_id
            and self.document_id == document_id
            and self.page_id == page_id
            and set(node_ids) <= set(self.node_ids)
        )


class DesignSourceGateway:
    """Normalize caller-supplied source slices without invoking any connector."""

    def normalize(
        self,
        *,
        source_kind: str,
        document_id: str,
        document_version: str,
        page_id: str | None,
        node_slices: list[dict[str, Any]],
        parser_name: str,
        parser_version: str,
        mode: str = "read",
        approval: WriteApproval | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if source_kind not in {"figma", "sketch", "manual", "screenshot"}:
            raise ContractError("design gateway source kind is unsupported")
        if not node_slices:
            raise ContractError("design gateway requires a minimal non-empty source slice")
        if len(node_slices) > 128:
            raise ContractError("design gateway source slice exceeds the 128-node safety limit")
        if _contains_sensitive_key(node_slices):
            raise ContractError("design gateway source slice contains credential-like fields")
        node_ids = tuple(sorted(item.get("id", "") for item in node_slices))
        if not all(node_ids) or len(node_ids) != len(set(node_ids)):
            raise ContractError("design gateway node ids must be non-empty and unique")
        approval_id = None
        if mode == "write":
            if approval is None or attempt_id is None or not approval.allows(
                attempt_id=attempt_id,
                document_id=document_id,
                page_id=page_id,
                node_ids=node_ids,
            ):
                raise ContractError("design gateway write scope is not approved for this attempt")
            approval_id = approval.approval_id
        elif mode not in {"read", "export"}:
            raise ContractError("design gateway permission mode is invalid")

        confidence = 0.5 if source_kind == "screenshot" else 0.75 if source_kind == "manual" else 1.0
        provenance_kind = "inference" if source_kind == "screenshot" else "manual-contract" if source_kind == "manual" else "source"
        source_identity = {
            "document_id": document_id,
            "document_version": document_version,
            "kind": source_kind,
            "node_slices": node_slices,
            "page_id": page_id,
        }
        evidence_id = f"evidence-{sha256(source_identity)[:16]}"
        slices = [
            {
                "id": item["id"],
                "kind": item.get("kind", "component"),
                "payload": item.get("payload", {}),
                "provenance": {
                    "confidence": confidence,
                    "evidence_ref": evidence_id,
                    "kind": provenance_kind,
                },
            }
            for item in sorted(node_slices, key=lambda entry: entry["id"])
        ]
        unknowns = []
        if source_kind == "screenshot":
            unknowns.append({
                "blocking": True,
                "id": "structured-states-unavailable",
                "reason": "Screenshot fallback cannot prove hidden states or interaction semantics.",
            })
        value = {
            "evidence_id": evidence_id,
            "permission": {"approval_id": approval_id, "mode": mode},
            "schema_version": "1.0",
            "slices": slices,
            "source": {
                "content_sha256": sha256(source_identity),
                "document_id": document_id,
                "document_version": document_version,
                "kind": source_kind,
                "parser": {"name": parser_name, "version": parser_version},
                "scope": {"node_ids": list(node_ids), "page_id": page_id},
            },
            "status": "partial" if unknowns else "complete",
            "unknowns": unknowns,
        }
        validate_design_evidence(value)
        return value

    def ledger_projection(self, evidence: dict[str, Any], *, artifact_uri: str) -> dict[str, Any]:
        """Return the minimal credential-free task ledger record for one Evidence artifact."""

        validate_design_evidence(evidence)
        if not _is_controlled_artifact_uri(artifact_uri):
            raise ContractError("design gateway ledger artifact URI is uncontrolled")
        approval_id = evidence["permission"]["approval_id"]
        return {
            "approval_sha256": sha256(approval_id) if approval_id is not None else None,
            "artifact_uri": artifact_uri,
            "cleanup": "not-required",
            "evidence_id": evidence["evidence_id"],
            "retention": "task",
            "schema_version": "1.0",
            "scope_sha256": sha256(evidence["source"]["scope"]),
            "source_content_sha256": evidence["source"]["content_sha256"],
        }

    def cleanup(self) -> dict[str, str]:
        """Declare cleanup outcome; this gateway never creates a credential or source cache."""

        return {"reason": "gateway-does-not-cache", "status": "not-required"}


def _contains_sensitive_key(value: Any) -> bool:
    sensitive = {
        "accesstoken", "apikey", "authorization", "authtoken", "bearertoken",
        "clientsecret", "cookie", "credential", "credentials", "password",
        "privatekey", "refreshtoken", "secret", "sessiontoken", "token",
    }
    if isinstance(value, dict):
        return any(
            re.sub(r"[^a-z0-9]", "", str(key).lower()) in sensitive or _contains_sensitive_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def _is_controlled_artifact_uri(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("artifact://")
        and len(value) > len("artifact://")
        and ".." not in value
        and not any(character.isspace() for character in value)
    )
