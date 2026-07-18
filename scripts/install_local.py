#!/usr/bin/env python3
"""Install the source-checkout Apple workflow into a local Codex root."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any
import zipfile


if sys.version_info < (3, 11):
    detected_version = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(
        f"AgentDevelopmentSkills requires Python 3.11+; current interpreter is Python {detected_version}. "
        "Launch scripts/install_local.py with Python 3.11 or newer."
    )

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - current Apple installer runs on POSIX.
    termios = None
    tty = None


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_workflow.canonical_json import dumps, load  # noqa: E402
from agent_workflow.contracts import validate_activation_lock, validate_manifest  # noqa: E402
from agent_workflow.installation import (  # noqa: E402
    MANAGED_DIRECTORY_MODE,
    MANAGED_FILE_MODE,
    MANAGED_ROOTS,
    build_install_bundle,
    install_bundle,
    target_lifecycle_lock,
)
from agent_workflow.models import ContractError  # noqa: E402


ACTIVATION_LOCK = ".agent-skills/activation-lock.json"
LEGACY_REPOSITORY_NAME = "iOSAgentSkills"
PLATFORM_ORDER = ("apple", "android", "web", "backend", "desktop")
PLATFORM_LABELS = {
    "apple": "Apple / iOS",
    "android": "Android",
    "web": "Web",
    "backend": "Backend",
    "desktop": "Desktop",
}
SOURCE_INSTALL_HANDLERS = {
    "apple": {
        "activation": "apple-codex-v1",
        "smoke": "ios-installed-workflow-v1",
    },
    "desktop": {
        "activation": "none",
        "smoke": "desktop-installed-workflow-v1",
    },
}

ACTIVATED_FILES: tuple[tuple[str, str, int], ...] = (
    ("disciplines/design/assets/codex/agents/design_researcher.toml", "agents/design_researcher.toml", 0o644),
    ("disciplines/review/assets/codex/agents/reviewer.toml", "agents/reviewer.toml", 0o644),
    ("disciplines/workflow/assets/codex/agents/explorer.toml", "agents/explorer.toml", 0o644),
    ("disciplines/workflow/assets/codex/agents/pm.toml", "agents/pm.toml", 0o644),
    ("disciplines/workflow/assets/codex/agents/reporter.toml", "agents/reporter.toml", 0o644),
    ("platforms/apple/config/codex/templates/agents/builder.toml", "agents/builder.toml", 0o644),
    ("platforms/apple/config/codex/templates/agents/docs_researcher.toml", "agents/docs_researcher.toml", 0o644),
    ("platforms/apple/config/codex/templates/agents/tester.toml", "agents/tester.toml", 0o644),
    ("platforms/apple/config/codex/templates/codex_verify.example.sh", "bin/codex_verify", 0o755),
    ("platforms/apple/tools/digest-xcodebuild-log.sh", "bin/digest-xcodebuild-log", 0o755),
    ("@generated/agent-session.pyz", "bin/agent-session", 0o755),
    (
        "platforms/apple/config/codex/templates/codex_verify.example.sh",
        "templates/codex_verify.example.sh",
        0o755,
    ),
    (
        "platforms/apple/config/codex/templates/ui-smoke.example.yml",
        "templates/ui-smoke.example.yml",
        0o644,
    ),
)

PROFILE_FILES: tuple[tuple[str, str, int], ...] = tuple(
    (
        f"runtime-configs/codex/assets/codex/profiles/{name}",
        name,
        0o644,
    )
    for name in (
        "budget.config.toml",
        "daily.config.toml",
        "deep.config.toml",
        "extreme.config.toml",
        "interactive-fast.config.toml",
        "readonly.config.toml",
    )
)

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_DIM = "\033[2m"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _activation_bytes(source: str) -> bytes:
    if source != "@generated/agent-session.pyz":
        return (ROOT / source).read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        main = zipfile.ZipInfo("__main__.py", date_time=(1980, 1, 1, 0, 0, 0))
        main.external_attr = 0o644 << 16
        archive.writestr(
            main,
            "from agent_workflow.worktree_sessions.cli import main\nraise SystemExit(main())\n",
        )
        package_root = ROOT / "src" / "agent_workflow"
        for path in sorted(package_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT / "src").as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return b"#!/usr/bin/env python3\n" + buffer.getvalue()


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _supports_color(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ


def _styled(value: str, *styles: str, enabled: bool) -> str:
    if not enabled:
        return value
    return "".join(styles) + value + ANSI_RESET


@contextmanager
def _terminal_key_mode(stream: Any):
    """Temporarily read single keys without echo while preserving terminal state."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        yield
        return
    if not os.isatty(descriptor):
        yield
        return
    if termios is None or tty is None:
        raise ContractError("interactive platform selection requires POSIX terminal support")
    original = termios.tcgetattr(descriptor)
    terminal_signals = tuple(
        candidate
        for candidate in (
            getattr(signal, "SIGHUP", None),
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGQUIT", None),
        )
        if candidate is not None
    )
    original_handlers = {candidate: signal.getsignal(candidate) for candidate in terminal_signals}

    def terminate_after_restore(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    try:
        for candidate in terminal_signals:
            signal.signal(candidate, terminate_after_restore)
        tty.setcbreak(descriptor)
        yield
    finally:
        try:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        finally:
            for candidate, handler in original_handlers.items():
                signal.signal(candidate, handler)


def _read_terminal_key(stream: Any) -> str | None:
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        descriptor = None
    if descriptor is not None and not os.isatty(descriptor):
        descriptor = None

    def read_character(timeout: float | None = None) -> str:
        if descriptor is None:
            return stream.read(1)
        if timeout is not None:
            ready, _, _ = select.select([descriptor], [], [], timeout)
            if not ready:
                return ""
        return os.read(descriptor, 1).decode("ascii", errors="ignore")

    value = read_character()
    if value == "":
        return "cancel"
    if value == "\x1b":
        suffix = ""
        if descriptor is None:
            suffix = stream.read(2)
        else:
            for _ in range(2):
                character = read_character(0.05)
                if not character:
                    break
                suffix += character
        if suffix in {"[A", "OA"}:
            return "up"
        if suffix in {"[B", "OB"}:
            return "down"
        return "cancel"
    if value in {"\r", "\n"}:
        return "confirm"
    if value == " ":
        return "toggle"
    if value in {"q", "Q", "\x03"}:
        return "cancel"
    if value in {"k", "K"}:
        return "up"
    if value in {"j", "J"}:
        return "down"
    return None


def _joined(values: list[str]) -> str:
    return "、".join(values) if values else "无"


def _display_ids(values: list[str]) -> str:
    labels = {**PLATFORM_LABELS, "codex": "Codex"}
    return _joined([labels.get(value, value) for value in values])


def _grouped_path_lines(values: list[str]) -> list[str]:
    groups: dict[str, list[str]] = {}
    for value in values:
        path = Path(value)
        parent = path.parent.as_posix()
        groups.setdefault(parent, []).append(path.name)
    return [f"      {parent}：{_joined(names)}" for parent, names in groups.items()]


def _platform_inventory() -> list[dict[str, Any]]:
    platform_root = ROOT / "platforms"
    options: list[dict[str, Any]] = []
    for candidate in platform_root.iterdir():
        manifest_path = candidate / "manifest.json"
        if candidate.is_symlink() or manifest_path.is_symlink():
            raise ContractError(f"platform package candidate is unsafe: {candidate.name}")
        if not candidate.is_dir() or not manifest_path.is_file():
            continue
        value = load(manifest_path)
        validate_manifest(value)
        if value.get("kind") != "platform":
            continue
        platform_id = value["id"]
        if candidate.name != platform_id:
            raise ContractError(f"platform directory and manifest id differ: {candidate.name}")
        status = value["implementation_status"]
        handler = SOURCE_INSTALL_HANDLERS.get(platform_id)
        has_installation = isinstance(value.get("installation"), dict)
        if status != "implemented":
            availability = status
        elif not has_installation:
            availability = "installation-contract-missing"
        elif handler is None:
            availability = "source-installer-handler-missing"
        else:
            availability = "ready"
        options.append(
            {
                "availability": availability,
                "handler": handler,
                "id": platform_id,
                "label": PLATFORM_LABELS.get(platform_id, platform_id.replace("-", " ").title()),
                "selectable": availability == "ready",
                "status": status,
                "version": value["version"],
            }
        )
    order = {platform_id: index for index, platform_id in enumerate(PLATFORM_ORDER)}
    return sorted(options, key=lambda item: (order.get(item["id"], len(order)), item["id"]))


def _validated_platform_selection(
    requested: list[str], options: list[dict[str, Any]]
) -> tuple[str, ...]:
    by_id = {item["id"]: item for item in options}
    if requested == ["all"]:
        selected = [item["id"] for item in options if item["selectable"]]
    else:
        if "all" in requested:
            raise ContractError("--platform all cannot be combined with another platform")
        if len(requested) != len(set(requested)):
            raise ContractError("selected platforms must be unique")
        unknown = [item for item in requested if item not in by_id]
        if unknown:
            raise ContractError(f"unknown platform selection: {', '.join(unknown)}")
        selected = requested
    if not selected:
        raise ContractError("no installable platform was selected")
    unavailable = [by_id[item] for item in selected if not by_id[item]["selectable"]]
    if unavailable:
        details = ", ".join(f"{item['label']} ({item['availability']})" for item in unavailable)
        raise ContractError(f"所选平台尚不可安装: {details}")
    return tuple(selected)


def _prompt_for_platforms(
    options: list[dict[str, Any]], *, input_stream: Any, output_stream: Any
) -> tuple[str, ...]:
    color = _supports_color(output_stream)
    default_index = next(
        (index for index, item in enumerate(options) if item["selectable"]),
        None,
    )
    if default_index is None:
        raise ContractError("no installable platform is currently available")
    selected = {options[default_index]["id"]}
    cursor = default_index
    rendered_line_count = 0
    message = ""

    def render() -> None:
        nonlocal rendered_line_count
        lines = []
        for index, item in enumerate(options):
            checked = "x" if item["id"] in selected else " "
            pointer = _styled("›", ANSI_CYAN, ANSI_BOLD, enabled=color) if index == cursor else " "
            if item["selectable"]:
                status = _styled("✓ 已支持", ANSI_GREEN, enabled=color)
            else:
                status = _styled(
                    f"○ 规划中 · {item['availability']} · 暂不可选",
                    ANSI_YELLOW,
                    enabled=color,
                )
            lines.append(f"{pointer} [{checked}] {item['label']:<18} {status}")
        lines.extend(
            [
                "",
                _styled(
                    "↑/↓ 移动   Space 选择/取消   Enter 确认   q 取消",
                    ANSI_DIM,
                    enabled=color,
                ),
                _styled(message, ANSI_YELLOW, enabled=color),
            ]
        )
        if rendered_line_count:
            output_stream.write(f"\033[{rendered_line_count}F\033[J")
        output_stream.write("\n".join(lines) + "\n")
        output_stream.flush()
        rendered_line_count = len(lines)

    with _terminal_key_mode(input_stream):
        while True:
            render()
            key = _read_terminal_key(input_stream)
            if key == "cancel":
                output_stream.write("\n")
                raise ContractError("platform selection was cancelled")
            if key == "up":
                cursor = (cursor - 1) % len(options)
                message = ""
            elif key == "down":
                cursor = (cursor + 1) % len(options)
                message = ""
            elif key == "toggle":
                item = options[cursor]
                if not item["selectable"]:
                    message = f"! {item['label']} ({item['availability']}) 尚不可安装"
                    continue
                if item["id"] in selected:
                    selected.remove(item["id"])
                else:
                    selected.add(item["id"])
                message = ""
            elif key == "confirm":
                if not selected:
                    message = "! 请至少选择一个可安装平台"
                    continue
                selection = _validated_platform_selection(
                    [item["id"] for item in options if item["id"] in selected],
                    options,
                )
                output_stream.write(f"\033[{rendered_line_count}F\033[J")
                output_stream.flush()
                return selection


def _select_platforms(
    args: argparse.Namespace,
    *,
    input_stream: Any | None = None,
    output_stream: Any | None = None,
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    options = _platform_inventory()
    requested = list(getattr(args, "platform", []) or [])
    if requested:
        return _validated_platform_selection(requested, options), options
    if getattr(args, "json", False):
        raise ContractError("--json requires an explicit --platform (for example: --platform apple)")
    if not input_stream.isatty() or not output_stream.isatty():
        raise ContractError(
            "platform selection requires an interactive terminal or explicit --platform apple"
        )
    return _prompt_for_platforms(options, input_stream=input_stream, output_stream=output_stream), options


def _human_report(report: dict[str, Any], *, color: bool) -> str:
    planned = report["status"] == "planned"
    marker = _styled("◇", ANSI_CYAN, ANSI_BOLD, enabled=color) if planned else _styled(
        "✓", ANSI_GREEN, ANSI_BOLD, enabled=color
    )
    selected_platforms = _display_ids(report["selected_platforms"])
    title = (
        f"{selected_platforms} 平台安装预览"
        if planned
        else f"{selected_platforms} 平台安装完成"
    )
    lines = [
        f"{marker} {_styled(title, ANSI_BOLD, enabled=color)}",
        "",
        _styled("变更摘要", ANSI_BOLD, enabled=color),
    ]

    activation = report["activation"]
    config_action = "将合并更新" if planned else "已合并更新"
    if not activation["config_changed"]:
        config_action = "无需变更"
    lines.append(f"  {'↻' if activation['config_changed'] else '✓'} config.toml：{config_action}")

    updates = activation["managed_file_updates"]
    update_action = "将更新" if planned else "已更新"
    if updates:
        lines.append(f"  ↻ 受管文件：{update_action} {len(updates)} 个")
        lines.extend(_grouped_path_lines(updates))
        lines.append(f"  ✓ 其余已一致：{len(activation['managed_files_unchanged'])} 个")
    else:
        lines.append(f"  ✓ 受管文件：{len(activation['managed_files_unchanged'])} 个均已一致")

    profile_creates = activation["profile_creates"]
    profile_preserves = activation["profile_preserves"]
    if profile_creates:
        profile_action = "将创建" if planned else "已创建"
        lines.append(f"  ↻ Profiles：{profile_action} {len(profile_creates)} 个，保留 {len(profile_preserves)} 个")
    else:
        lines.append(f"  ✓ Profiles：保留 {len(profile_preserves)} 个，无需创建")

    lines.append("  • 持久备份：不创建（按当前安装策略）")

    if planned:
        lines.extend(
            [
                "",
                _styled("未写入任何文件。", ANSI_YELLOW, enabled=color),
                "确认后移除 --dry-run，重新执行原命令。",
            ]
        )
    else:
        system_status = "已保留" if report["preserved_system_skills"] else "无需迁移"
        smoke = report["post_install_smoke"]
        lines.extend(
            [
                f"  ✓ Codex 系统 Skills：{system_status}",
                "",
                _styled("安装态验证", ANSI_BOLD, enabled=color),
                f"  ✓ Installed workflow smoke：{smoke['status']}",
                f"  ✓ Plan / Review / Final：{smoke['plan_status']} / {smoke['review_status']} / {smoke['final_status']}",
            ]
        )
    lines.extend(
        [
            "",
            _styled("自动化场景：添加 --json 获取 canonical JSON。", ANSI_DIM, enabled=color),
            "",
        ]
    )
    return "\n".join(lines)


def _human_error(error: Exception, *, dry_run: bool, color: bool) -> str:
    title = "安装预检查未通过" if dry_run else "平台工作流安装未完成"
    marker = _styled("✗", ANSI_RED, ANSI_BOLD, enabled=color)
    return "\n".join(
        [
            f"{marker} {_styled(title, ANSI_BOLD, enabled=color)}",
            "",
            f"  原因：{error}",
            "  状态：已停止；若已进入写入阶段，将执行单进程临时回滚。",
            "",
            _styled("自动化场景：添加 --json 获取结构化错误。", ANSI_DIM, enabled=color),
            "",
        ]
    )


def _legacy_target(path: Path, leaf: str) -> str | None:
    if not path.is_symlink():
        return None
    raw_target = os.readlink(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if resolved.name != leaf or resolved.parent.name != LEGACY_REPOSITORY_NAME:
        return None
    if leaf == "AGENTS.md" and not resolved.is_file():
        return None
    if leaf == "skills" and not resolved.is_dir():
        return None
    return raw_target


def _classify_legacy(target: Path) -> dict[str, str]:
    occupied = [name for name in MANAGED_ROOTS if _path_exists(target / name)]
    if not occupied:
        return {}
    if (target / ".agent-skills" / "install-lock.json").is_file():
        return {}
    if set(occupied) != {"AGENTS.md", "skills"}:
        raise ContractError(
            "refusing to replace incomplete or unknown unmanaged install roots: "
            + ", ".join(occupied)
        )
    agents_target = _legacy_target(target / "AGENTS.md", "AGENTS.md")
    skills_target = _legacy_target(target / "skills", "skills")
    if agents_target is None or skills_target is None:
        raise ContractError("refusing to replace non-iOSAgentSkills AGENTS.md or skills")
    return {"AGENTS.md": agents_target, "skills": skills_target}


def _activation_records(target: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": destination,
            "mode": mode,
            "sha256": _digest(_activation_bytes(source)),
        }
        for source, destination, mode in ACTIVATED_FILES
    ]


def _validate_activation_lock(target: Path) -> bool:
    lock_path = target / ACTIVATION_LOCK
    if not _path_exists(lock_path):
        return False
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ContractError("activation lock must be a regular file")
    lock = load(lock_path)
    validate_activation_lock(lock)
    files = lock.get("files")
    if not isinstance(files, list):
        raise ContractError("activation lock files must be a list")
    expected_paths = {destination for _, destination, _ in ACTIVATED_FILES}
    legacy_paths = expected_paths - {"bin/agent-session"}
    actual_paths = {
        item.get("path")
        for item in files
        if isinstance(item, dict)
    }
    if frozenset(actual_paths) not in {frozenset(expected_paths), frozenset(legacy_paths)} or len(files) != len(actual_paths):
        raise ContractError("activation lock does not cover the managed file set")
    for item in files:
        path = target / item["path"]
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"activated file is missing or unsafe: {item['path']}")
        if path.stat().st_mode & 0o777 != item["mode"] or _digest(path.read_bytes()) != item["sha256"]:
            raise ContractError(f"activated file was modified: {item['path']}")
    return True


def _preflight_activation(target: Path, *, adopting_legacy: bool) -> None:
    for name in ("agents", "bin", "templates"):
        directory = target / name
        if _path_exists(directory) and (directory.is_symlink() or not directory.is_dir()):
            raise ContractError(f"activation directory must be a regular directory: {name}")
    has_lock = _validate_activation_lock(target)
    if has_lock or adopting_legacy:
        return
    for source, destination, _ in ACTIVATED_FILES:
        path = target / destination
        if not _path_exists(path):
            continue
        if path.is_symlink() or not path.is_file() or path.read_bytes() != _activation_bytes(source):
            raise ContractError(f"refusing to overwrite unmanaged activation file: {destination}")


def _config_candidate(target: Path) -> bytes:
    config = target / "config.toml"
    if _path_exists(config) and (config.is_symlink() or not config.is_file()):
        raise ContractError("config.toml must be a regular file")
    command = [
        sys.executable,
        str(ROOT / "runtime-configs/codex/assets/scripts/sync_codex_shared_config.py"),
        "--shared-config",
        str(ROOT / "runtime-configs/codex/assets/codex/codex.shared.toml"),
        "--agents-path",
        str(target / "AGENTS.md"),
    ]
    if config.is_file():
        command.extend(["--existing-config", str(config)])
    completed = subprocess.run(command, check=True, capture_output=True)
    return completed.stdout


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        path.parent.chmod(MANAGED_DIRECTORY_MODE)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _activation_plan(target: Path, config_candidate: bytes) -> dict[str, Any]:
    updates: list[str] = []
    unchanged: list[str] = []
    for source, destination, mode in ACTIVATED_FILES:
        path = target / destination
        expected = _activation_bytes(source)
        if path.is_file() and not path.is_symlink() and path.read_bytes() == expected and path.stat().st_mode & 0o777 == mode:
            unchanged.append(destination)
        else:
            updates.append(destination)
    profile_creates = [destination for _, destination, _ in PROFILE_FILES if not _path_exists(target / destination)]
    profile_preserves = [destination for _, destination, _ in PROFILE_FILES if _path_exists(target / destination)]
    config_path = target / "config.toml"
    config_changed = not config_path.is_file() or config_path.read_bytes() != config_candidate
    return {
        "config_changed": config_changed,
        "managed_file_updates": updates,
        "managed_files_unchanged": unchanged,
        "profile_creates": profile_creates,
        "profile_preserves": profile_preserves,
    }


def _activate(target: Path, config_candidate: bytes) -> dict[str, Any]:
    plan = _activation_plan(target, config_candidate)
    writes: list[tuple[Path, bytes, int]] = []
    for source, destination, mode in ACTIVATED_FILES:
        path = target / destination
        expected = _activation_bytes(source)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.read_bytes() != expected
            or path.stat().st_mode & 0o777 != mode
        ):
            writes.append((path, expected, mode))
    for source, destination, mode in PROFILE_FILES:
        path = target / destination
        if not _path_exists(path):
            writes.append((path, (ROOT / source).read_bytes(), mode))
    if plan["config_changed"]:
        config_path = target / "config.toml"
        config_mode = (
            config_path.stat().st_mode & 0o777
            if config_path.is_file() and not config_path.is_symlink()
            else MANAGED_FILE_MODE
        )
        writes.append((config_path, config_candidate, config_mode))
    activation_lock = {
        "schema_version": "2.0",
        "manager": "agent-development-skills",
        "handler": "core.source-activation.apple-codex-v1",
        "files": _activation_records(target),
    }
    writes.append((target / ACTIVATION_LOCK, dumps(activation_lock).encode("utf-8"), MANAGED_FILE_MODE))

    with tempfile.TemporaryDirectory(prefix="agent-skills-activation-rollback-") as directory:
        rollback_root = Path(directory)
        snapshots: dict[Path, tuple[Path, int] | None] = {}
        for path, _, _ in writes:
            if path in snapshots:
                continue
            if _path_exists(path):
                if path.is_symlink() or not path.is_file():
                    raise ContractError(f"activation destination must be a regular file: {path.relative_to(target)}")
                snapshot = rollback_root / str(len(snapshots))
                snapshot.write_bytes(path.read_bytes())
                snapshots[path] = (snapshot, path.stat().st_mode & 0o777)
            else:
                snapshots[path] = None
        try:
            for path, data, mode in writes:
                _atomic_write(path, data, mode)
            _validate_activation_lock(target)
        except Exception as primary_error:
            rollback_errors: list[str] = []
            for path, snapshot in reversed(list(snapshots.items())):
                try:
                    if snapshot is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, snapshot[0].read_bytes(), snapshot[1])
                except OSError as error:
                    rollback_errors.append(f"{path.relative_to(target)}: {error}")
            if rollback_errors:
                raise ContractError(
                    f"activation failed ({primary_error}); temporary rollback incomplete: "
                    + "; ".join(rollback_errors)
                ) from primary_error
            raise
    return plan


def _remove_managed_roots(target: Path) -> None:
    for name in MANAGED_ROOTS:
        path = target / name
        if not _path_exists(path):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _restore_legacy(target: Path, links: dict[str, str]) -> None:
    _remove_managed_roots(target)
    for name, raw_target in links.items():
        (target / name).symlink_to(raw_target)


def _copy_legacy_system_skills(target: Path, legacy_links: dict[str, str]) -> bool:
    if not legacy_links:
        return False
    legacy_skills = (target / legacy_links["skills"]).resolve(strict=False)
    source = legacy_skills / ".system"
    destination = target / "skills" / ".system"
    if not source.is_dir() or source.is_symlink():
        return False
    shutil.copytree(source, destination, symlinks=True)
    return True


def _run_target_smoke(target: Path, selected_platforms: tuple[str, ...]) -> dict[str, Any]:
    scripts = {
        "apple": "run_ios_installed_workflow_smoke.py",
        "desktop": "run_desktop_installed_workflow_smoke.py",
    }
    reports: list[dict[str, Any]] = []
    for platform_id in selected_platforms:
        script = scripts.get(platform_id)
        if script is None:
            raise ContractError(f"source installer smoke handler is missing: {platform_id}")
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--target-root", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        reports.append(json.loads(completed.stdout))
    return {
        "status": "passed" if reports and all(item["status"] == "passed" for item in reports) else "failed",
        "workflow": {
            "final_status": "completed" if all(item["workflow"]["final_status"] == "completed" for item in reports) else "blocked",
            "plan_status": "ready" if all(item["workflow"]["plan_status"] == "ready" for item in reports) else "blocked",
            "review_status": "passed" if all(item["workflow"]["review_status"] == "passed" for item in reports) else "blocked",
        },
    }


def _empty_activation_plan() -> dict[str, Any]:
    return {
        "config_changed": False,
        "managed_file_updates": [],
        "managed_files_unchanged": [],
        "profile_creates": [],
        "profile_preserves": [],
    }


def _install_engine() -> str:
    value = os.environ.get("AGENT_SKILLS_INSTALL_ENGINE_SELECTED", "python-source")
    if value not in {"python-fallback", "python-source"}:
        raise ContractError("Python installer engine provenance is invalid")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select and install platform Agent workflows from this source checkout; "
            "only implemented platforms are installable."
        )
    )
    parser.add_argument(
        "--target-root",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        help="installation root (default: $CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help="platform id; repeat for multiple platforms or use all",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without writing files")
    parser.add_argument("--json", action="store_true", help="output canonical JSON for automation")
    return parser.parse_args()


def run(args: argparse.Namespace | None = None) -> dict[str, Any]:
    if args is None:
        args = parse_args()
    selected_platforms, platform_options = _select_platforms(args)
    apple_selected = "apple" in selected_platforms
    raw_target = Path(args.target_root).expanduser()
    if raw_target.is_symlink():
        raise ContractError(f"install target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    if target.exists() and not target.is_dir():
        raise ContractError(f"install target must be a directory: {target}")
    if not args.dry_run:
        target.mkdir(parents=True, exist_ok=True)

    bundle = build_install_bundle(
        ROOT / "platforms",
        platforms=selected_platforms,
        runtime_configs=["codex"] if apple_selected else [],
        schema_root=ROOT / "schemas",
    )

    if args.dry_run:
        legacy_links = _classify_legacy(target) if apple_selected else {}
        if apple_selected:
            _preflight_activation(target, adopting_legacy=bool(legacy_links))
            config_candidate = _config_candidate(target)
            activation = _activation_plan(target, config_candidate)
        else:
            activation = _empty_activation_plan()
        if not legacy_links:
            # Managed/fresh targets use the same fail-closed preflight as a real install.
            install_bundle(bundle, target, dry_run=True)
        return {
            "engine": _install_engine(),
            "schema_version": "1.0",
            "status": "planned",
            "target_root": str(target),
            "selected_platforms": bundle.plan["selected_platforms"],
            "selected_runtime_configs": bundle.plan["selected_runtime_configs"],
            "selected_packages": [item["id"] for item in bundle.plan["selected_packages"]],
            "skill_count": len(bundle.plan["skills"]),
            "would_remove_legacy_symlinks": sorted(legacy_links),
            "persistent_backup": False,
            "platform_options": platform_options,
            "activation": activation,
        }

    post_install: dict[str, Any] = {}
    with target_lifecycle_lock(target) as lifecycle_token:
        legacy_links = _classify_legacy(target) if apple_selected else {}
        if apple_selected:
            _preflight_activation(target, adopting_legacy=bool(legacy_links))
            config_candidate = _config_candidate(target)
        else:
            config_candidate = b""

        def complete_install(installed_target: Path, _: dict[str, Any]) -> None:
            post_install["preserved_system_skills"] = _copy_legacy_system_skills(installed_target, legacy_links) if apple_selected else False
            post_install["smoke"] = _run_target_smoke(installed_target, selected_platforms)
            post_install["activation"] = _activate(installed_target, config_candidate) if apple_selected else _empty_activation_plan()

        try:
            for name in legacy_links:
                (target / name).unlink()
            install_result = install_bundle(
                bundle,
                target,
                post_install=complete_install,
                lifecycle_token=lifecycle_token,
            )
        except Exception:
            if legacy_links:
                _restore_legacy(target, legacy_links)
            raise

    return {
        "engine": _install_engine(),
        "schema_version": "1.0",
        "status": "installed",
        "target_root": str(target),
        "selected_platforms": install_result["selected_platforms"],
        "selected_runtime_configs": install_result["selected_runtime_configs"],
        "selected_packages": [item["id"] for item in install_result["selected_packages"]],
        "skill_count": len(install_result["skills"]),
        "removed_legacy_symlinks": sorted(legacy_links),
        "persistent_backup": False,
        "platform_options": platform_options,
        "preserved_system_skills": post_install["preserved_system_skills"],
        "activation": post_install["activation"],
        "post_install_smoke": {
            "status": post_install["smoke"]["status"],
            "plan_status": post_install["smoke"]["workflow"]["plan_status"],
            "final_status": post_install["smoke"]["workflow"]["final_status"],
            "review_status": post_install["smoke"]["workflow"]["review_status"],
        },
    }


def main() -> int:
    args = parse_args()
    try:
        report = run(args)
        if args.json:
            print(dumps(report), end="")
        else:
            print(_human_report(report, color=_supports_color(sys.stdout)))
        return 0
    except (ContractError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        if args.json:
            print(dumps({"status": "blocked", "error": str(error)}), end="", file=sys.stderr)
        else:
            print(
                _human_error(error, dry_run=args.dry_run, color=_supports_color(sys.stderr)),
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
