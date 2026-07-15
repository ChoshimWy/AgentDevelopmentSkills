"""Cross-platform Git Worktree Session primitives.

The package owns no platform build commands. Git workspace operations are
exposed through the shared Git Discipline; registry and final-gate semantics
are exposed through the shared Workflow Discipline.
"""

from .gate import attach_adapter_result, evaluate_session_gate
from .git_workspace import (
    create_session_worktree,
    freeze_checkpoint,
    inspect_repository,
    refresh_session_source_identity,
    repository_patch,
    session_source_identity,
)
from .registry import SessionRegistry, new_session_context

__all__ = [
    "SessionRegistry",
    "attach_adapter_result",
    "create_session_worktree",
    "evaluate_session_gate",
    "freeze_checkpoint",
    "inspect_repository",
    "new_session_context",
    "refresh_session_source_identity",
    "repository_patch",
    "session_source_identity",
]
