"""Helpers for the integration tests: running the demo project and parsing its output."""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["Summary", "read_capture_text", "run_demo", "summarize"]


def run_demo(pytester, *args: str):
    """Run pytest on the demo project inside pytester's temporary directory."""
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)


class Summary:
    """Parsed ``capquery:`` lines of a pytest run."""

    _STATEMENTS = re.compile(
        r"statements: (\d+) replayed, (\d+) missed, (\d+) executed against postgres, "
        r"(\d+) always sent"
    )
    _CAPTURES = re.compile(r"captures (\d+) created, (\d+) updated, (\d+) unchanged, (\d+) deleted")
    _MIGRATIONS = re.compile(r"migrations: (?:not captured.*|(\d+) statement\(s\) replayed, (\d+) missed, (\d+) recorded)")

    def __init__(self, outlines: list[str]) -> None:
        self.lines = [line for line in outlines if line.startswith("capquery:")]
        self.replayed = self.missed = self.executed_postgres = self.always_sent = 0
        self.created = self.updated = self.unchanged = self.deleted = 0
        self.migrations_replayed = self.migrations_missed = self.migrations_recorded = 0
        self.migrations_line = next((line for line in self.lines if ": migrations:" in line), "")
        self.disabled: str | None = None
        self.unstable: list[str] = []
        self.retried: list[str] = []
        self.deferred: list[str] = []
        self.enabled_line = next((line for line in self.lines if not line.startswith("capquery: disabled")), "")
        joined = "\n".join(self.lines)
        if match := self._STATEMENTS.search(joined):
            (
                self.replayed,
                self.missed,
                self.executed_postgres,
                self.always_sent,
            ) = map(int, match.groups())
        if match := self._CAPTURES.search(joined):
            self.created, self.updated, self.unchanged, self.deleted = map(int, match.groups())
        if match := self._MIGRATIONS.search(joined):
            groups = [group for group in match.groups() if group is not None]
            if len(groups) == 3:  # a "not captured (..." line has no numbers
                (
                    self.migrations_replayed,
                    self.migrations_missed,
                    self.migrations_recorded,
                ) = map(int, groups)
        for line in self.lines:
            if line.startswith("capquery: disabled"):
                self.disabled = line
            elif line.startswith("capquery: unstable tests"):
                self.unstable = [item.strip() for item in line.split(":", 2)[2].split(",")]
            elif line.startswith("capquery: retried tests"):
                self.retried = [item.strip() for item in line.split(":", 2)[2].split(",")]
            elif line.startswith("capquery: tests that need the real migration phase"):
                self.deferred = [
                    item.strip() for item in line.split(":", 2)[2].split(",") if item.strip()
                ]

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return "\n".join(self.lines)


def summarize(result) -> Summary:
    return Summary(list(result.outlines))


def read_capture_text(root: Path, relative: str) -> str:
    return (Path(root) / relative).read_text(encoding="utf-8")
