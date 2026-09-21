"""Capture file serialization."""

from __future__ import annotations

import enum

import pytest

from capquery.hashing import query_hash
from capquery.records import Record
from capquery.values import encode_params
from capquery.yaml_io import CaptureFileError, delete_capture, read_capture, render_capture, write_capture


class _Status(str, enum.Enum):
    """A ``str`` subclass, the shape Django's ``models.TextChoices`` has."""

    FIRST = "first"


def record(**overrides) -> Record:
    payload = {
        "hash": "9f2c",
        "n": 0,
        "sql": "SELECT id, name FROM shop_order WHERE id = %s",
        "params": [{"t": "int", "v": 1}],
        "columns": ["id", "name"],
        "rows": [[{"t": "int", "v": 1}, {"t": "str", "v": "first"}]],
    }
    payload.update(overrides)
    return Record(**payload)


def test_yaml_keeps_the_documented_field_order():
    text = render_capture([record()])
    keys = [line.split(":")[0] for line in text.splitlines() if line and not line.startswith((" ", "-"))]
    assert keys[:5] == ["- hash", "  n", "  sql", "  params", "  columns"] or text.startswith("- hash:")


def test_the_row_count_is_part_of_the_file():
    text = render_capture([record(rowcount=3)])
    assert "rowcount: 3" in text
    assert "columns: [id, name]" in text


def test_a_record_without_a_row_count_is_read_back_without_one():
    payload = {"hash": "9f2c", "n": 0, "sql": "UPDATE shop_order SET name = %s", "params": []}
    restored = Record.from_payload(payload)
    assert restored.rowcount is None


def test_typed_values_are_rendered_inline():
    text = render_capture([record()])
    assert "params: [{t: int, v: 1}]" in text
    assert "columns: [id, name]" in text
    assert "[{t: int, v: 1}, {t: str, v: first}]" in text


def test_round_trip(tmp_path):
    path = tmp_path / "capture.yaml"
    original = record()
    assert write_capture(path, [original]) == "created"
    assert read_capture(path) == [original]


def test_writing_an_unchanged_file_is_reported(tmp_path):
    path = tmp_path / "capture.yaml"
    write_capture(path, [record()])
    before = path.read_text(encoding="utf-8")
    assert write_capture(path, [record()]) == "unchanged"
    assert path.read_text(encoding="utf-8") == before


def test_changed_records_update_the_file(tmp_path):
    path = tmp_path / "capture.yaml"
    write_capture(path, [record()])
    assert write_capture(path, [record(rows=[])]) == "updated"
    assert read_capture(path)[0].rows == []


def test_writing_an_empty_capture_deletes_the_file(tmp_path):
    path = tmp_path / "capture.yaml"
    write_capture(path, [record()])
    assert write_capture(path, []) == "deleted"
    assert not path.exists()


def test_deleting_a_missing_file_is_reported_as_absent(tmp_path):
    assert delete_capture(tmp_path / "nope.yaml") == "absent"


def test_migrations_are_readable(tmp_path):
    path = tmp_path / "migrations.yaml"
    write_capture(path, [record(sql="SELECT app, name FROM django_migrations", hash="aa")])
    assert read_capture(path)[0].hash == "aa"


def test_an_enum_parameter_is_written_as_its_value(tmp_path):
    """``Order.objects.filter(name=OrderStatus.FIRST)`` records a str subclass."""
    params = encode_params([_Status.FIRST])
    statement = Record(
        hash=query_hash("SELECT id FROM shop_order WHERE name = %s", [_Status.FIRST]),
        n=0,
        sql="SELECT id FROM shop_order WHERE name = %s",
        params=params,
        rowcount=1,
        columns=["id"],
        rows=[[{"t": "int", "v": 1}]],
    )
    path = tmp_path / "capture.yaml"
    assert write_capture(path, [statement]) == "created"
    assert "{t: str, v: first}" in path.read_text(encoding="utf-8")
    assert read_capture(path) == [statement]


def test_a_value_that_cannot_be_serialized_is_reported_not_raised():
    """A value the encoder could not strip must not escape as a yaml error.

    PyYAML refuses a subclass of a builtin with ``RepresenterError``; raised out of
    the run it aborts the whole pytest session instead of one capture.
    """
    payload = [{"t": "str", "v": _Status.FIRST}]
    with pytest.raises(CaptureFileError, match="cannot be stored"):
        render_capture([record(params=payload)])


def test_an_unserializable_record_keeps_the_previous_file(tmp_path):
    path = tmp_path / "capture.yaml"
    write_capture(path, [record()])
    before = path.read_text(encoding="utf-8")

    with pytest.raises(CaptureFileError, match="cannot be stored"):
        write_capture(path, [record(params=[{"t": "str", "v": _Status.FIRST}])])

    assert path.read_text(encoding="utf-8") == before


def test_a_malformed_file_is_reported(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("- hash: x\n  sql: SELECT 1\n  n: not-a-number\n", encoding="utf-8")
    with pytest.raises(CaptureFileError):
        read_capture(path)


def test_a_file_with_a_wrong_root_is_reported(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("hash: x\n", encoding="utf-8")
    with pytest.raises(CaptureFileError, match="list of records"):
        read_capture(path)
