"""Reading and writing capture files.

The format is a mapping of two fields, one file per capture context::

    captures:
      - hash: 9f2c...
        n: 0
        sql: SELECT id, name FROM shop_order WHERE id = %s
        schemas: {params: 1, rows: 2}
        params: [1]
        rowcount: 1
        columns: [id, name]
        rows:
          - [1, first]
    schemas:
      1: [int]
      2: [int, str]

``captures`` holds the queries with their results and ``schemas`` the types of the
values: a capture names the schema of every field that holds typed values by id, and
identical schemas share one id, so the table never repeats a schema
(:mod:`capquery.schemas`).

Values are written as plain data, and every mapping that is not a capture, the file
itself or the schema table stays on one line: a code review sees a capture at a glance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .records import Record
from .schemas import SchemaTable, data_of_params, plain_of, schema_of_params, schema_of_rows, typed_field

__all__ = ["CaptureFileError", "delete_capture", "read_capture", "render_capture", "write_capture"]


class CaptureFileError(ValueError):
    """A capture file is malformed, or holds a value that cannot be stored."""


class _BlockMap(dict):
    """A mapping written line by line: the file itself, a capture, the schema table."""


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        # a block sequence starts at the column of its own key, which would put the
        # captures of the file next to ``captures:`` instead of under it
        return super().increase_indent(flow, False)


def _represent_block_map(dumper: _Dumper, data: _BlockMap):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=False)


def _represent_map(dumper: _Dumper, data: dict):
    # the schemas of a capture, a schema descriptor and a dict value are short enough
    # to stay on one line, which leaves the records readable
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)


def _represent_list(dumper: _Dumper, data: list):
    inline = len(data) <= 8 and all(
        item is None
        or isinstance(item, (str, int, float, bool))
        or (isinstance(item, dict) and not isinstance(item, _BlockMap))
        for item in data
    )
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=inline)


_Dumper.add_representer(_BlockMap, _represent_block_map)
_Dumper.add_representer(dict, _represent_map)
_Dumper.add_representer(list, _represent_list)


def render_capture(records: Iterable[Record]) -> str:
    """Serialize records into the yaml text of a capture file.

    A value capquery cannot store (anything but the exact builtin types, which
    :mod:`capquery.values` produces) is reported as a :class:`CaptureFileError`.
    PyYAML raises ``RepresenterError`` for a subclass of a builtin, and raised out of
    here it escapes the plugin and aborts the whole pytest session.
    """
    schemas = SchemaTable()
    payload = _BlockMap()
    payload["captures"] = [_capture(record, schemas) for record in records]
    payload["schemas"] = _BlockMap(schemas.payload())
    try:
        return yaml.dump(
            payload,
            Dumper=_Dumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        )
    except yaml.YAMLError as exc:
        raise CaptureFileError(f"a captured value cannot be stored in a capture file: {exc}") from exc


def _capture(record: Record, schemas: SchemaTable) -> _BlockMap:
    """One capture, naming the schema of every field that holds typed values."""
    ids = {}
    if record.params:
        ids["params"] = schemas.id_for(schema_of_params(record.params))
    if record.rows:
        ids["rows"] = schemas.id_for(schema_of_rows(record.rows))
    capture = _BlockMap()
    capture["hash"] = record.hash
    capture["n"] = record.n
    capture["sql"] = record.sql
    if ids:
        capture["schemas"] = ids
    capture["params"] = data_of_params(record.params)
    capture["rowcount"] = record.rowcount
    capture["columns"] = list(record.columns)
    capture["rows"] = [[plain_of(value) for value in row] for row in record.rows]
    return capture


def read_capture(path: Path) -> list[Record]:
    """Read a capture file. Raises CaptureFileError when it is malformed."""
    path = Path(path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaptureFileError(f"capquery: cannot parse {path}: {exc}") from exc
    if payload is None:
        return []
    if not isinstance(payload, dict) or "captures" not in payload:
        raise CaptureFileError(f"capquery: {path} must be a mapping with the fields captures and schemas")
    schemas = _read_schemas(path, payload.get("schemas"))
    captures = payload.get("captures") or []
    if not isinstance(captures, list):
        raise CaptureFileError(f"capquery: {path} must hold the captures as a list")
    records = []
    for capture in captures:
        if not isinstance(capture, dict) or "hash" not in capture or "sql" not in capture:
            raise CaptureFileError(f"capquery: {path} contains a malformed capture: {capture!r}")
        try:
            records.append(_read_capture(capture, schemas))
        except (TypeError, ValueError, KeyError) as exc:
            raise CaptureFileError(
                f"capquery: {path} contains a malformed capture: {capture!r} ({exc})"
            ) from exc
    return records


def _read_schemas(path: Path, payload: Any) -> dict:
    """The schema table of the file: id -> schema."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise CaptureFileError(f"capquery: {path} must hold the schemas as a mapping of id to schema")
    for number, schema in payload.items():
        if not isinstance(schema, list):
            raise CaptureFileError(f"capquery: {path} holds a malformed schema {number!r}: {schema!r}")
    return payload


def _read_capture(capture: dict, schemas: dict) -> Record:
    ids = capture.get("schemas") or {}
    if not isinstance(ids, dict):
        raise ValueError("the schemas field must map the name of a field to a schema id")
    return Record(
        hash=capture["hash"],
        n=int(capture["n"]),
        sql=capture["sql"],
        params=_read_values(ids, schemas, "params", capture.get("params") or []),
        rowcount=None if capture.get("rowcount") is None else int(capture["rowcount"]),
        columns=capture.get("columns") or [],
        rows=[_read_values(ids, schemas, "rows", row) for row in (capture.get("rows") or [])],
    )


def _read_values(ids: dict, schemas: dict, name: str, values: Any) -> list:
    """Decode the values of one field with the schema the capture names for it."""
    if not values:
        return []
    if not isinstance(values, list):
        raise ValueError(f"the {name} field must be a list, not {values!r}")
    schema = _schema_of(ids, schemas, name)
    try:
        return typed_field(schema, values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"the {name} field does not match its schema {schema!r}: {exc}") from exc


def _schema_of(ids: dict, schemas: dict, name: str) -> list:
    number = ids.get(name)
    if number is None:
        raise ValueError(f"the {name} field holds values but no schema id")
    if number not in schemas:
        raise ValueError(f"the {name} field names the unknown schema {number!r}")
    return schemas[number]


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
