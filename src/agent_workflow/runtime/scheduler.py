"""Deterministic in-process resource scheduler for Phase 1."""

from __future__ import annotations

from typing import Any, Iterable


class ResourceScheduler:
    def __init__(self) -> None:
        self._owners: dict[str, str] = {}
        self.events: list[dict[str, Any]] = []
        self._sequence = 0

    def acquire(self, attempt_id: str, resource_keys: Iterable[str]) -> bool:
        keys = sorted(set(resource_keys))
        for key in keys:
            self._event(attempt_id, key, "requested")
        if any(key in self._owners and self._owners[key] != attempt_id for key in keys):
            return False
        for key in keys:
            self._owners[key] = attempt_id
            self._event(attempt_id, key, "acquired")
        return True

    def release(self, attempt_id: str, *, action: str = "released") -> None:
        if action not in {"released", "timed-out", "cancelled"}:
            raise ValueError(f"invalid resource release action: {action}")
        keys = sorted(key for key, owner in self._owners.items() if owner == attempt_id)
        for key in keys:
            del self._owners[key]
            self._event(attempt_id, key, action)

    def owner(self, resource_key: str) -> str | None:
        return self._owners.get(resource_key)

    def seed_sequence(self, next_sequence: int) -> None:
        if self.events or next_sequence < self._sequence:
            raise ValueError("resource sequence can only be seeded before scheduling")
        self._sequence = next_sequence

    def _event(self, attempt_id: str, resource_key: str, action: str) -> None:
        self.events.append(
            {
                "action": action,
                "attempt_id": attempt_id,
                "resource_key": resource_key,
                "schema_version": "1.0",
                "sequence": self._sequence,
            }
        )
        self._sequence += 1
