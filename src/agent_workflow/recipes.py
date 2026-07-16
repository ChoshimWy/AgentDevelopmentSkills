"""Canonical automatic workflow recipe capability registry."""

from __future__ import annotations

from itertools import combinations


RECIPE_DISCIPLINES = ("automation", "build", "debug", "design", "documentation", "performance")
RECIPE_TASK_TYPES = ("code-small", "doc-only", "investigation", "qa-only", "review-only")


def required_platform_capabilities(platform: str, task_type: str, disciplines: list[str]) -> list[str]:
    """Return the capabilities emitted by the automatic platform recipe."""

    if task_type == "review-only":
        return []
    if task_type == "doc-only":
        capabilities = [f"analysis.{platform}"]
        if "documentation" in disciplines:
            capabilities.append("documentation.html")
        return capabilities
    if task_type == "investigation":
        if "debug" in disciplines and platform == "apple":
            return ["debugging.apple.analysis"]
        if "performance" in disciplines and platform == "apple":
            return ["performance.apple"]
        if "automation" in disciplines and platform == "apple":
            return ["automation.apple"]
        return [f"analysis.{platform}"]
    if task_type == "qa-only":
        capabilities = [f"verification.{platform}.affected-tests"]
        if platform == "apple":
            capabilities.append("verification.apple.auto")
        return capabilities
    if "build" in disciplines and platform == "apple":
        capabilities = ["build.apple.configuration", "verification.apple.affected-tests"]
    elif "debug" in disciplines and platform == "apple":
        capabilities = ["debugging.apple.execute", "verification.apple.affected-tests"]
    elif "performance" in disciplines and platform == "apple":
        capabilities = ["performance.apple"]
    elif "automation" in disciplines and platform == "apple":
        capabilities = ["automation.apple"]
    else:
        capabilities = [f"implementation.{platform}", f"verification.{platform}.affected-tests"]
    if "design" in disciplines:
        design_capabilities = ["design.apple.source"] if platform == "apple" else []
        design_capabilities.extend(
            [
                "design.evidence.normalize",
                "design.system",
                "design.ir.compile",
                "design.registry.resolve",
                "design.packet.slice",
            ]
        )
        if platform == "apple":
            design_capabilities.append("design.apple.binding")
        capabilities = [*design_capabilities, *capabilities]
    if platform == "apple" and "verification.apple.affected-tests" in capabilities:
        affected_index = capabilities.index("verification.apple.affected-tests")
        capabilities.insert(affected_index + 1, "verification.apple.auto")
    return capabilities


def automatic_recipe_capabilities(targets: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Enumerate every capability reachable from the compiler's automatic recipes."""

    result = {
        "core.intent-lock",
        "qa.contract.validate",
        "qa.coverage.compile",
        "qa.plan.compile",
        "qa.report.aggregate",
        "report.apple.delivery",
        "reporting.delivery",
        "review.independent",
        "workflow.analysis",
        "workflow.orchestration",
    }
    discipline_sets = [
        list(combo)
        for size in range(len(RECIPE_DISCIPLINES) + 1)
        for combo in combinations(RECIPE_DISCIPLINES, size)
    ]
    for target in targets:
        result.add(f"review.{target}.static")
        for task_type in RECIPE_TASK_TYPES:
            for disciplines in discipline_sets:
                result.update(required_platform_capabilities(target, task_type, disciplines))
    return frozenset(result)
