"""Resolve task intent and routing policy with decision provenance."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from ..canonical_json import sha256
from ..contracts import validate_resolved_policy
from ..models import ContractError


PLATFORM_TERMS = {
    "apple": ("ios", "ipad", "macos", "swift", "xcode", "apple"),
    "android": ("android", "kotlin", "gradle", "compose"),
    "backend": ("backend", "server", "api", "database", "后端", "服务端"),
    "desktop": ("desktop", "windows", "linux", "electron", "tauri", "桌面"),
    "web": ("web", "frontend", "react", "vue", "网页", "前端"),
}


def classify_task(task: str) -> dict[str, Any]:
    lowered = task.lower()
    if any(term in lowered for term in ("review", "审查", "评审")):
        task_type = "review-only"
    elif any(term in lowered for term in ("测试", "qa", "regression", "回归")) and not any(
        term in lowered for term in ("实现", "修复", "implement", "fix")
    ):
        task_type = "qa-only"
    elif any(term in lowered for term in ("文档", "docs", "document")):
        task_type = "doc-only"
    elif any(term in lowered for term in ("调查", "分析", "investigate", "why")):
        task_type = "investigation"
    elif any(term in lowered for term in ("跨平台", "contract", "schema", "migration", "并发", "权限")):
        task_type = "code-risky"
    else:
        task_type = "code-medium"
    risk = "high" if task_type == "code-risky" else "medium" if task_type.startswith("code") else "low"
    disciplines = ["development"]
    if any(term in lowered for term in ("ui", "design", "figma", "sketch", "设计")):
        disciplines.append("design")
    if any(term in lowered for term in ("test", "qa", "测试", "回归")):
        disciplines.append("qa")
    return {"disciplines": sorted(set(disciplines)), "risk": risk, "type": task_type}


class PolicyResolver:
    def resolve(
        self,
        profile: dict[str, Any],
        task_text: str,
        *,
        explicit_platforms: Iterable[str] = (),
        constraints: dict[str, Any] | None = None,
        policy_layers: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        task = classify_task(task_text)
        explicit = sorted(set(explicit_platforms))
        inferred = _platforms_from_task(task_text)
        targeted = sorted({item["platform"] for item in profile.get("target_modules", [])})
        discovered = profile.get("platforms", [])
        decisions: list[dict[str, Any]] = []

        if explicit:
            selected = explicit
            source = "user-explicit"
            reason = "EXPLICIT_PLATFORM_LOCK"
            confidence = 1.0
        elif inferred:
            selected = sorted(inferred)
            source = "task-text"
            reason = "TASK_PLATFORM_MATCH"
            confidence = 0.95
        elif targeted:
            selected = targeted
            source = "target-files-or-cwd"
            reason = "TARGET_MODULE_MATCH"
            confidence = 0.9
        else:
            selected = sorted(discovered)
            source = "project-profile"
            reason = "DISCOVERY_EVIDENCE"
            confidence = 0.8 if selected else 0.0

        unresolved_ambiguities = []
        unique_target = len({(item.get("path"), item.get("platform")) for item in profile.get("target_modules", [])}) == 1
        if not explicit and not inferred and profile.get("ambiguities") and not unique_target:
            unresolved_ambiguities = deepcopy(profile["ambiguities"])

        decisions.append(
            {
                "confidence": confidence,
                "decision": f"select platforms: {', '.join(selected) if selected else 'unknown'}",
                "merge_strategy": "locked" if explicit else "replace",
                "overridden_candidates": sorted(set(discovered) - set(selected)),
                "reason_code": reason if selected else "NO_PLATFORM_EVIDENCE",
                "source": source,
            }
        )
        merged_constraints, merge_decisions = merge_policy_layers(policy_layers)
        if constraints:
            merged_constraints.update(deepcopy(constraints))
        if unresolved_ambiguities:
            merged_constraints["routing_ambiguities"] = unresolved_ambiguities
            decisions.append(
                {
                    "confidence": 0.0,
                    "decision": "block automatic platform selection until ambiguity is resolved",
                    "merge_strategy": "locked",
                    "overridden_candidates": [],
                    "reason_code": "UNRESOLVED_PLATFORM_AMBIGUITY",
                    "source": "project-profile",
                }
            )
        decisions.extend(merge_decisions)
        value: dict[str, Any] = {
            "constraints": merged_constraints,
            "decisions": decisions,
            "schema_version": "1.0",
            "selected_platforms": selected,
            "task": {"text": task_text, **task},
        }
        value["fingerprint"] = sha256(value)
        validate_resolved_policy(value)
        return value


def _platforms_from_task(task: str) -> set[str]:
    lowered = task.lower()
    return {
        platform
        for platform, terms in PLATFORM_TERMS.items()
        if any(term in lowered for term in terms)
    }


def merge_policy_layers(layers: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Merge explicit policy layers using field-level strategies.

    A layer has ``source``, ``values`` and optional ``strategies``. Locked
    values cannot be changed by later layers. The caller controls layer order.
    """

    result: dict[str, Any] = {}
    locked: set[str] = set()
    decisions: list[dict[str, Any]] = []
    for layer in layers:
        source = layer.get("source", "unknown")
        values = layer.get("values", {})
        strategies = layer.get("strategies", {})
        for field in sorted(values):
            incoming = deepcopy(values[field])
            strategy = strategies.get(field, "replace")
            if field in locked and field in result and incoming != result[field]:
                raise ContractError(f"locked policy field cannot be overridden: {field}")
            current = result.get(field)
            result[field] = _merge_value(current, incoming, strategy)
            if strategy == "locked":
                locked.add(field)
            decisions.append(
                {
                    "confidence": 1.0,
                    "decision": f"merge constraint: {field}",
                    "merge_strategy": strategy,
                    "overridden_candidates": [],
                    "reason_code": "POLICY_LAYER_MERGE",
                    "source": source,
                }
            )
    return result, decisions


def _merge_value(current: Any, incoming: Any, strategy: str) -> Any:
    if current is None or strategy in {"replace", "locked"}:
        return incoming
    if strategy == "append":
        return list(current) + list(incoming)
    if strategy == "union":
        return sorted(set(current) | set(incoming))
    if strategy == "intersect":
        return sorted(set(current) & set(incoming))
    if strategy == "deny-wins":
        if not isinstance(current, bool) or not isinstance(incoming, bool):
            raise ContractError("deny-wins requires boolean values")
        return current and incoming
    raise ContractError(f"unknown merge strategy: {strategy}")
