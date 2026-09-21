"""Reading and writing capture files.

The format is a plain yaml list of records, one file per capture context::

    - hash: 9f2c...
      n: 0
      sql: SELECT id, name FROM shop_order WHERE id = %s
      params: [{t: int, v: 1}]
      columns: [id, name]
      rows:
        - [{t: int, v: 1}, {t: str, v: первый}]

Typed values are rendered in flow style so that a code review shows them inline
while the files stay readable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import yaml

from .records import Record

__all__ = ["CaptureFileError", "delete_capture", "read_capture", "render_capture", "write_capture"]


class CaptureFileError(ValueError):
    """A capture file exists but cannot be parsed."""


class _Dumper(yaml.SafeDumper):
    pass


def _represent_dict(dumper: _Dumper, data: dict):
    flow = set(data) == {"t", "v"}
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=flow)


def _represent_list(dumper: _Dumper, data: list):
    inline = len(data) <= 8 and all(
        item is None or isinstance(item, (str, int, float, bool)) or (isinstance(item, dict) and set(item) == {"t", "v"})
        for item in data
    )
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=inline)


_Dumper.add_representer(dict, _represent_dict)
_Dumper.add_representer(list, _represent_list)


def render_capture(records: Iterable[Record]) -> str:
    """Serialize records into the yaml text of a capture file."""
    payload = [record.to_payload() for record in records]
    return yaml.dump(
        payload,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def read_capture(path: Path) -> list[Record]:
    """Read a capture file. Raises CaptureFileError when it is malformed."""
    path = Path(path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaptureFileError(f"capquery: cannot parse {path}: {exc}") from exc
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise CaptureFileError(f"capquery: {path} must contain a list of records")
    records = []
    for item in payload:
        if not isinstance(item, dict) or "hash" not in item or "sql" not in item:
            raise CaptureFileError(f"capquery: {path} contains a malformed record: {item!r}")
        try:
            records.append(Record.from_payload(item))
        except (TypeError, ValueError, KeyError) as exc:
            raise CaptureFileError(f"capquery: {path} contains a malformed record: {item!r} ({exc})") from exc
    return records


def write_capture(path: Path, records: list[Record]) -> str:
    """Write a capture file, keeping it byte stable when nothing changed.

    Returns one of ``created``, ``updated``, ``unchanged``, ``deleted`` or
    ``absent`` (nothing to store and no file to remove).
    """
    path = Path(path)
    if not records:
        return delete_capture(path)
    text = render_capture(records)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return "unchanged"
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "updated" if existed else "created"


def delete_capture(path: Path) -> str:
    """Delete a capture file (a test without queries must not keep stale captures)."""
    path = Path(path)
    if path.exists():
        path.unlink()
        return "deleted"
    return "absent"


def read_capture_or_none(path: Path) -> Optional[list[Record]]:
    if not Path(path).exists():
        return None
    return read_capture(path)
