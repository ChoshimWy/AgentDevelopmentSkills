"""Compile resolved policy into a deterministic capability DAG."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..canonical_json import sha256
from ..contracts import validate_workflow_plan
from ..models import ContractError
from ..registry import ManifestRegistry


class PlanCompiler:
    def __init__(self, registry: ManifestRegistry) -> None:
        self.registry = registry

    def compile(self, profile: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        missing: list[str] = []
        task_type = policy["task"]["type"]
        routing_blocked = False

        if _requires_platform(task_type) and not policy["selected_platforms"]:
            missing.append("routing.platform-selection")
            routing_blocked = True
        if policy.get("constraints", {}).get("routing_ambiguities"):
            missing.append("routing.ambiguity-resolution")
            routing_blocked = True

        nodes.append(_node("intent", "core.intent-lock", mandatory=True))
        previous_ids: list[str] = ["intent"]

        for platform in policy["selected_platforms"]:
            capabilities = _required_capabilities(platform, task_type, policy["task"].get("disciplines", []))
            platform_previous = "intent"
            for index, capability in enumerate(capabilities):
                node_id = f"{platform}-{index + 1}"
                providers = self.registry.capability_providers(capability)
                contract = self.registry.capability_contract(capability)
                provider = providers[0].value["id"] if len(providers) == 1 else None
                if not providers:
                    missing.append(capability)
                nodes.append(_node(node_id, capability, mandatory=True, provider=provider, contract=contract))
                edges.append({"from": platform_previous, "to": node_id})
                platform_previous = node_id
            previous_ids.append(platform_previous)

        qa_needed = "qa" in policy["task"].get("disciplines", [])
        if qa_needed:
            capability = "qa.targeted"
            providers = self.registry.capability_providers(capability)
            if not providers:
                missing.append(capability)
            nodes.append(_node("qa", capability, mandatory=False, provider=providers[0].value["id"] if len(providers) == 1 else None, contract=self.registry.capability_contract(capability)))
            for source in sorted(set(previous_ids)):
                edges.append({"from": source, "to": "qa"})
            previous_ids = ["qa"]

        review_capability = "review.independent"
        providers = self.registry.capability_providers(review_capability)
        if not providers:
            missing.append(review_capability)
        nodes.append(_node("review", review_capability, mandatory=task_type.startswith("code"), provider=providers[0].value["id"] if len(providers) == 1 else None, contract=self.registry.capability_contract(review_capability)))
        for source in sorted(set(previous_ids)):
            if source != "review":
                edges.append({"from": source, "to": "review"})

        _topological_order(nodes, edges)
        provider_blocked = any(node["mandatory"] and node["capability"] in missing for node in nodes)
        status = "blocked" if routing_blocked or provider_blocked else "degraded" if missing else "ready"
        content = {
            "edges": sorted(edges, key=lambda edge: (edge["from"], edge["to"])),
            "missing_capabilities": sorted(set(missing)),
            "nodes": nodes,
            "profile_fingerprint": sha256(profile),
            "policy_fingerprint": policy.get("fingerprint", sha256(policy)),
            "schema_version": "1.0",
            "status": status,
        }
        fingerprint = sha256(content)
        plan = {"fingerprint": fingerprint, "plan_id": f"plan-{fingerprint[:12]}", **content}
        validate_workflow_plan(plan)
        return plan


def _node(
    node_id: str,
    capability: str,
    *,
    mandatory: bool,
    provider: str | None = "core",
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = contract or {}
    return {
        "approval": None,
        "capability": capability,
        "id": node_id,
        "mandatory": mandatory,
        "max_retries": 1 if contract.get("idempotent", True) else 0,
        "idempotent": contract.get("idempotent", True),
        "permission_profile": contract.get("permission_profile", "repository-read-only" if capability == "core.intent-lock" else "project-read-execute"),
        "provider": provider,
        "resource_keys": contract.get("concurrency_keys", []),
        "side_effects": contract.get("side_effects", []),
        "status": "pending",
        "timeout_seconds": 300,
    }


def _required_capabilities(platform: str, task_type: str, disciplines: list[str]) -> list[str]:
    if task_type == "review-only":
        return []
    if task_type in {"doc-only", "investigation"}:
        return [f"analysis.{platform}"]
    if task_type == "qa-only":
        return [f"verification.{platform}.affected-tests"]
    capabilities = [f"implementation.{platform}", f"verification.{platform}.affected-tests"]
    if "design" in disciplines:
        capabilities.insert(0, "design.context")
    return capabilities


def _requires_platform(task_type: str) -> bool:
    return task_type.startswith("code") or task_type in {"qa-only", "investigation"}


def _topological_order(nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[str]:
    node_ids = {node["id"] for node in nodes}
    incoming: dict[str, int] = {node_id: 0 for node_id in node_ids}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge["from"] not in node_ids or edge["to"] not in node_ids:
            raise ContractError("edge references unknown node")
        incoming[edge["to"]] += 1
        outgoing[edge["from"]].append(edge["to"])
    queue = deque(sorted(node_id for node_id, count in incoming.items() if count == 0))
    result: list[str] = []
    while queue:
        node_id = queue.popleft()
        result.append(node_id)
        for target in sorted(outgoing[node_id]):
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if len(result) != len(node_ids):
        raise ContractError("workflow plan contains dependency cycle")
    return result
