"""Compile resolved policy into a deterministic capability DAG."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..canonical_json import sha256
from ..contracts import validate_workflow_plan
from ..models import ContractError
from ..recipes import required_platform_capabilities
from ..registry import ManifestRegistry


class PlanCompiler:
    def __init__(self, registry: ManifestRegistry) -> None:
        self.registry = registry

    def compile(self, profile: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        missing: list[str] = []
        bootstrap_required: list[dict[str, Any]] = []
        task_type = policy["task"]["type"]
        routing_blocked = False

        if _requires_platform(task_type) and not policy["selected_platforms"]:
            missing.append("routing.platform-selection")
            routing_blocked = True
        if policy.get("constraints", {}).get("routing_ambiguities"):
            missing.append("routing.ambiguity-resolution")
            routing_blocked = True

        intent = self.registry.resolve_binding("core.intent-lock")
        nodes.append(_node("intent", "core.intent-lock", mandatory=True, resolution=intent))
        previous_ids: list[str] = ["intent"]

        workflow_analysis = self.registry.resolve_binding("workflow.analysis")
        if workflow_analysis is not None:
            nodes.append(_node("workflow-analysis", "workflow.analysis", mandatory=True, resolution=workflow_analysis))
            edges.append({"from": "intent", "to": "workflow-analysis"})
            previous_ids = ["workflow-analysis"]
        if task_type.startswith("code"):
            workflow_orchestration = self.registry.resolve_binding("workflow.orchestration")
            if workflow_orchestration is not None:
                nodes.append(
                    _node(
                        "workflow-orchestration",
                        "workflow.orchestration",
                        mandatory=True,
                        resolution=workflow_orchestration,
                    )
                )
                for source in previous_ids:
                    edges.append({"from": source, "to": "workflow-orchestration"})
                previous_ids = ["workflow-orchestration"]

        for platform in policy["selected_platforms"]:
            capabilities = required_platform_capabilities(
                platform, task_type, policy["task"].get("disciplines", [])
            )
            bootstrap = self.registry.bootstrap_requirement(platform)
            if capabilities and bootstrap is not None:
                bootstrap_required.append(bootstrap)
            platform_previous = previous_ids[0] if len(previous_ids) == 1 else "intent"
            for index, capability in enumerate(capabilities):
                node_id = f"{platform}-{index + 1}"
                resolution = self.registry.resolve_binding(capability, platform=platform)
                if resolution is None:
                    missing.append(capability)
                nodes.append(_node(node_id, capability, mandatory=True, resolution=resolution))
                edges.append({"from": platform_previous, "to": node_id})
                platform_previous = node_id
            previous_ids.append(platform_previous)

        if task_type.startswith("code") or task_type == "review-only":
            extension_ids: list[str] = []
            for platform in policy["selected_platforms"]:
                capability = f"review.{platform}.static"
                resolution = self.registry.resolve_binding(capability, platform=platform)
                if resolution is None:
                    continue
                node_id = f"review-{platform}"
                nodes.append(_node(node_id, capability, mandatory=True, resolution=resolution))
                for source in sorted(set(previous_ids)):
                    edges.append({"from": source, "to": node_id})
                extension_ids.append(node_id)
            if extension_ids:
                previous_ids = extension_ids

        qa_needed = "qa" in policy["task"].get("disciplines", [])
        if qa_needed:
            capability = "qa.targeted"
            resolution = self.registry.resolve_binding(capability)
            if resolution is None:
                missing.append(capability)
            nodes.append(_node("qa", capability, mandatory=False, resolution=resolution))
            for source in sorted(set(previous_ids)):
                edges.append({"from": source, "to": "qa"})
            previous_ids = ["qa"]

        review_capability = "review.independent"
        review_platform = policy["selected_platforms"][0] if len(policy["selected_platforms"]) == 1 else "*"
        resolution = self.registry.resolve_binding(review_capability, platform=review_platform)
        if resolution is None:
            missing.append(review_capability)
        nodes.append(
            _node(
                "review",
                review_capability,
                mandatory=task_type.startswith("code") or task_type == "review-only",
                resolution=resolution,
            )
        )
        for source in sorted(set(previous_ids)):
            if source != "review":
                edges.append({"from": source, "to": "review"})

        reporting = self.registry.resolve_binding("reporting.delivery")
        if reporting is None:
            reporting = self.registry.resolve_binding("report.apple.delivery", platform=review_platform)
        if reporting is not None:
            nodes.append(_node("report", reporting.capability_id, mandatory=True, resolution=reporting))
            edges.append({"from": "review", "to": "report"})

        _topological_order(nodes, edges)
        provider_blocked = any(node["mandatory"] and node["capability"] in missing for node in nodes)
        status = "blocked" if routing_blocked or provider_blocked or bootstrap_required else "degraded" if missing else "ready"
        content = {
            "edges": sorted(edges, key=lambda edge: (edge["from"], edge["to"])),
            "missing_capabilities": sorted(set(missing)),
            "nodes": nodes,
            "profile_fingerprint": sha256(profile),
            "policy_fingerprint": policy.get("fingerprint", sha256(policy)),
            "registry_fingerprint": self.registry.digest(),
            "schema_version": "1.0",
            "status": status,
            "workflow": _workflow_contract(task_type),
        }
        if bootstrap_required:
            content["bootstrap_required"] = sorted(bootstrap_required, key=lambda item: item["platform"])
        fingerprint = sha256(content)
        plan = {"fingerprint": fingerprint, "plan_id": f"plan-{fingerprint[:12]}", **content}
        validate_workflow_plan(plan)
        return plan


def _node(
    node_id: str,
    capability: str,
    *,
    mandatory: bool,
    resolution: Any | None = None,
) -> dict[str, Any]:
    contract = resolution.contract if resolution else {}
    return {
        "approval": None,
        "binding": resolution.binding if resolution else None,
        "capability": capability,
        "id": node_id,
        "mandatory": mandatory,
        "max_retries": 1 if contract.get("idempotent", True) else 0,
        "idempotent": contract.get("idempotent", True),
        "permission_profile": contract.get("permission_profile", "repository-read-only" if capability == "core.intent-lock" else "project-read-execute"),
        "provider": resolution.provider_id if resolution else None,
        "provider_manifest_digest": resolution.manifest_digest if resolution else None,
        "resource_keys": contract.get("concurrency_keys", []),
        "side_effects": contract.get("side_effects", []),
        "status": "pending",
        "timeout_seconds": 300,
    }


def _requires_platform(task_type: str) -> bool:
    return task_type.startswith("code") or task_type in {"qa-only", "investigation"}


def _workflow_contract(task_type: str) -> dict[str, Any]:
    if task_type.startswith("code"):
        roles = ["explorer", "builder", "reporter", "reviewer"]
        independent_review = True
    elif task_type == "review-only":
        roles = ["reviewer", "reporter"]
        independent_review = True
    elif task_type == "qa-only":
        roles = ["explorer", "test-executor", "reporter", "reviewer"]
        independent_review = False
    else:
        roles = ["explorer", "builder", "reporter"]
        independent_review = False
    return {
        "checkpoints": ["CP0", "CP1", "CP2", "CP3"],
        "independent_review": independent_review,
        "roles": roles,
    }


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
