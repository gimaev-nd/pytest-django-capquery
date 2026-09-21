"""Persistent bookkeeping of regeneration attempts.

A test that cannot produce stable captures must not be regenerated forever: the
number of consecutive regenerations is stored on disk (so it survives between
runs) and as soon as the configured limit is reached the test is marked
``unstable``, its captures are removed and it always runs against the real
database.

The same file carries one more flag, ``requires_real``.  It is raised when a test
had to run against the database in a session whose migration phase was answered
from the captures — such a run does not see what the migrations would have
written, so its captures cannot be trusted.  The next session reads the flag,
runs the migration phase for real and clears it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

__all__ = ["StateEntry", "StateFile"]


@dataclass
class StateEntry:
    attempts: int = 0
    unstable: bool = False
    requires_real: bool = False
    captures_nothing: bool = False

    def to_payload(self) -> dict:
        return {
            "attempts": self.attempts,
            "unstable": self.unstable,
            "requires_real": self.requires_real,
            "captures_nothing": self.captures_nothing,
        }


@dataclass
class StateFile:
    """``<rootdir>/captures/.capquery-state.yaml``."""

    path: Path
    _entries: dict[str, StateEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.load()

    def load(self) -> None:
        self._entries = {}
        if not self.path.exists():
            return
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return
        if not isinstance(payload, dict):
            return
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            self._entries[str(key)] = StateEntry(
                attempts=int(value.get("attempts", 0)),
                unstable=bool(value.get("unstable", False)),
                requires_real=bool(value.get("requires_real", False)),
                captures_nothing=bool(value.get("captures_nothing", False)),
            )

    def get(self, key: str) -> StateEntry:
        entry = self._entries.get(key)
        if entry is None:
            entry = StateEntry()
            self._entries[key] = entry
        return entry

    def is_unstable(self, key: str) -> bool:
        entry = self._entries.get(key)
        return bool(entry and entry.unstable)

    def attempts(self, key: str) -> int:
        entry = self._entries.get(key)
        return entry.attempts if entry else 0

    def note_regeneration(self, key: str, limit: int) -> StateEntry:
        """Count one more regeneration; mark unstable when the limit is reached."""
        entry = self.get(key)
        entry.attempts += 1
        if entry.attempts >= limit:
            entry.unstable = True
        self.save()
        return entry

    def requires_real(self, key: str) -> bool:
        entry = self._entries.get(key)
        return bool(entry and entry.requires_real)

    def set_requires_real(self, key: str, value: bool = True) -> None:
        entry = self.get(key)
        if entry.requires_real == value:
            return
        entry.requires_real = value
        self.save()

    def captures_nothing(self, key: str) -> bool:
        """True when a previous run saw this test issue no capturable statement."""
        entry = self._entries.get(key)
        return bool(entry and entry.captures_nothing)

    def set_captures_nothing(self, key: str, value: bool = True) -> None:
        entry = self.get(key)
        if entry.captures_nothing == value:
            return
        entry.captures_nothing = value
        self.save()

    def mark_unstable(self, key: str) -> StateEntry:
        entry = self.get(key)
        entry.unstable = True
        self.save()
        return entry

    def clear_attempts(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None and entry.attempts:
            entry.attempts = 0
            self.save()

    def forget(self, key: str) -> None:
        if key in self._entries:
            del self._entries[key]
            self.save()

    def unstable_keys(self) -> list[str]:
        return sorted(key for key, entry in self._entries.items() if entry.unstable)

    def save(self) -> None:
        if not self._entries:
            if self.path.exists():
                self.path.unlink()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: entry.to_payload() for key, entry in sorted(self._entries.items())}
        self.path.write_text(
            yaml.safe_dump(payload, sort_keys=True, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )


def load_state(path: Path) -> StateFile:
    return StateFile(Path(path))


def _unused(_: Optional[object]) -> None:  # pragma: no cover - keeps linters quiet
    return None
