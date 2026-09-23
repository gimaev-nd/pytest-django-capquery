"""Where capture files live.

Captures are stored next to the tests they belong to::

    <rootdir>/<first part of the test path>/captures/<mirrored test path>/<test name>.yaml

so for ``tests/shop/test_orders.py::test_create`` the capture file is
``tests/captures/shop/test_orders.py/test_create.yaml``.  For a test module that
sits in the rootdir itself the captures directory is ``<rootdir>/captures``.

Migration captures and the regeneration state file are project level::

    <rootdir>/captures/migrations.yaml
    <rootdir>/captures/.capquery-state.yaml
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "MIGRATIONS_CONTEXT",
    "capture_file",
    "captures_root",
    "migrations_file",
    "sanitize",
    "split_nodeid",
    "state_file",
]

MIGRATIONS_CONTEXT = "@migrations"

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    """Make an arbitrary test name safe to use as a file name."""
    cleaned = _INVALID_CHARS.sub("_", name).strip()
    cleaned = cleaned.replace(" ", "_")
    return cleaned or "_"


def split_nodeid(nodeid: str) -> tuple[str, str]:
    """Split a node id into the module path and the test name."""
    path, _, rest = nodeid.partition("::")
    return path, rest


def captures_root(rootdir: Path, test_path: str) -> Path:
    """Directory holding the captures of the test file."""
    parts = Path(test_path).parts
    if len(parts) > 1:
        return Path(rootdir) / parts[0] / "captures"
    return Path(rootdir) / "captures"


def capture_file(rootdir: Path, nodeid: str) -> Path:
    """Path of the capture file of a test."""
    test_path, rest = split_nodeid(nodeid)
    parts = Path(test_path).parts
    root = captures_root(rootdir, test_path)
    mirrored = parts[1:] if len(parts) > 1 else parts
    name = sanitize(rest.replace("::", ".")) + ".yaml"
    return root.joinpath(*mirrored, name)


def migrations_file(rootdir: Path) -> Path:
    """Path of the capture file of the migration phase."""
    return Path(rootdir) / "captures" / "migrations.yaml"


def state_file(rootdir: Path) -> Path:
    """Path of the regeneration state file."""
    return Path(rootdir) / "captures" / ".capquery-state.yaml"
