"""Capture file serialization."""

from __future__ import annotations

import enum
from pathlib import Path

import pytest
import yaml

from capquery.hashing import query_hash
from capquery.records import Record
from capquery.values import encode_params, encode_row
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


def payload_of(records: list) -> dict:
    """The file as a mapping, to assert on the format without the yaml detail."""
    return yaml.safe_load(render_capture(records))


def test_the_file_is_a_capture_list_and_a_schema_table():
    payload = payload_of([record()])
    assert list(payload) == ["captures", "schemas"]
    capture = payload["captures"][0]
    assert list(capture) == ["hash", "n", "sql", "schemas", "params", "rowcount", "columns", "rows"]


def test_a_capture_is_written_under_the_captures_field():
    """A block sequence indented under its own key: a capture reads as a record."""
    text = render_capture([record()])
    assert text.startswith("captures:\n  - hash: 9f2c\n")
    assert "    schemas: {params: 1, rows: 2}\n" in text
    assert "    params: [1]\n" in text
    assert "    rows:\n      - [1, first]\n" in text
    assert text.endswith("schemas:\n  1: [int]\n  2: [int, str]\n")


def test_a_capture_names_the_schema_of_every_field_that_holds_values():
    payload = payload_of([record()])
    assert payload["captures"][0]["schemas"] == {"params": 1, "rows": 2}
    assert payload["schemas"] == {1: ["int"], 2: ["int", "str"]}


def test_values_are_written_as_data():
    capture = payload_of([record()])["captures"][0]
    assert capture["params"] == [1]
    assert capture["rows"] == [[1, "first"]]


def test_a_field_without_values_has_no_schema():
    payload = payload_of([record(params=[], rows=[])])
    assert "schemas" not in payload["captures"][0]
    assert payload["schemas"] == {}


def test_the_row_count_and_the_columns_are_part_of_the_file():
    text = render_capture([record(rowcount=3)])
    assert "rowcount: 3" in text
    assert "columns: [id, name]" in text


def test_a_capture_without_a_row_count_reads_back_without_one(tmp_path):
    path = tmp_path / "capture.yaml"
    write_capture(path, [record(rowcount=None)])
    assert read_capture(path)[0].rowcount is None


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
        rows=[encode_row([1])],
    )
    path = tmp_path / "capture.yaml"
    assert write_capture(path, [statement]) == "created"
    assert payload_of([statement])["captures"][0]["params"] == ["first"]
    assert payload_of([statement])["schemas"] == {1: ["str"], 2: ["int"]}
    assert read_capture(path) == [statement]


def test_a_value_that_cannot_be_serialized_is_reported_not_raised():
    """A value the encoder could not strip must not escape as a yaml error.

    PyYAML refuses a subclass of a builtin with ``RepresenterError``; raised out of
    the run it aborts the whole pytest session instead of one capture.
    """
    params = [{"t": "str", "v": _Status.FIRST}]
    with pytest.raises(CaptureFileError, match="cannot be stored"):
        render_capture([record(params=params)])


def test_an_unserializable_record_keeps_the_previous_file(tmp_path):
    path = tmp_path / "capture.yaml"
    write_capture(path, [record()])
    before = path.read_text(encoding="utf-8")

    with pytest.raises(CaptureFileError, match="cannot be stored"):
        write_capture(path, [record(params=[{"t": "str", "v": _Status.FIRST}])])

    assert path.read_text(encoding="utf-8") == before


# -- the schemas -------------------------------------------------------------


def test_an_identical_schema_is_never_written_twice():
    """The table is shared by the file: a repeated schema keeps the id it got."""
    first = record(params=[{"t": "int", "v": 1}], rows=[])
    second = record(hash="aa11", n=1, params=[{"t": "int", "v": 7}], rows=[])
    assert payload_of([first, second])["schemas"] == {1: ["int"]}
    assert payload_of([first, second])["captures"][1]["schemas"] == {"params": 1}


def test_the_schema_table_is_shared_by_fields_of_different_captures():
    """A rows schema of one capture can be the params schema of another."""
    rows_only = record(params=[], rows=[[{"t": "int", "v": 1}]])
    params_only = record(hash="aa11", n=1, params=[{"t": "int", "v": 2}], rows=[])
    payload = payload_of([rows_only, params_only])
    assert payload["schemas"] == {1: ["int"]}
    assert payload["captures"][0]["schemas"] == {"rows": 1}
    assert payload["captures"][1]["schemas"] == {"params": 1}


def test_a_different_schema_gets_another_id():
    records = [
        record(params=encode_params([1]), rows=[]),
        record(hash="aa11", params=encode_params(["a"]), rows=[]),
    ]
    assert payload_of(records)["schemas"] == {1: ["int"], 2: ["str"]}


def test_a_null_parameter_is_described_as_null():
    """A ``None`` has no type: the schema says so, the data stays null."""
    params = encode_params([None, 1])
    text = render_capture([record(params=params, rows=[])])
    assert yaml.safe_load(text)["schemas"] == {1: ["null", "int"]}
    assert payload_of([record(params=params, rows=[])])["captures"][0]["params"] == [None, 1]


def test_a_column_described_by_one_row_holds_null_in_another(tmp_path):
    """A column that is null here and there keeps the type of the row that has one."""
    rows = [encode_row([None, "first"]), encode_row([7, None])]
    text = render_capture([record(rows=rows)])
    assert yaml.safe_load(text)["schemas"] == {1: ["int"], 2: ["int", "str"]}
    assert payload_of([record(rows=rows)])["captures"][0]["rows"] == [[None, "first"], [7, None]]
    path = tmp_path / "capture.yaml"
    write_capture(path, [record(rows=rows)])
    assert read_capture(path)[0].rows == rows


def test_a_column_that_is_null_in_every_row_is_described_as_null():
    rows = [encode_row([None, 1]), encode_row([None, 2])]
    assert yaml.safe_load(render_capture([record(rows=rows)]))["schemas"] == {1: ["int"], 2: ["null", "int"]}


def test_a_container_is_described_by_its_kind(tmp_path):
    params = encode_params([[1, "a"], (2,), {"key": "value"}, []])
    text = render_capture([record(params=params, rows=[])])
    assert text.count("{dict: {key: str}}") == 1
    assert payload_of([record(params=params, rows=[])])["schemas"] == {
        1: [{"list": ["int", "str"]}, {"tuple": ["int"]}, {"dict": {"key": "str"}}, {"list": []}]
    }
    path = tmp_path / "capture.yaml"
    write_capture(path, [record(params=params, rows=[])])
    assert read_capture(path)[0].params == params


def test_a_container_the_schema_does_not_cover_is_read_from_the_data(tmp_path):
    """An array column: one schema describes rows of every length."""
    rows = [encode_row([[], 1]), encode_row([[1, 2], 2]), encode_row([[3, 4, 5], 3])]
    text = render_capture([record(rows=rows)])
    assert yaml.safe_load(text)["schemas"] == {1: ["int"], 2: [{"list": []}, "int"]}
    path = tmp_path / "capture.yaml"
    write_capture(path, [record(rows=rows)])
    assert read_capture(path)[0].rows == rows


def test_the_parameter_sets_of_an_executemany_are_described_one_by_one(tmp_path):
    """``executemany`` is one capture whose parameters are the sets it was given."""
    sets = [encode_params(["first", 1]), encode_params(["second", 2])]
    text = render_capture([record(params=sets, rows=[])])
    assert yaml.safe_load(text)["schemas"] == {1: [["str", "int"], ["str", "int"]]}
    payload = payload_of([record(params=sets, rows=[])])
    assert payload["captures"][0]["params"] == [["first", 1], ["second", 2]]
    path = tmp_path / "capture.yaml"
    write_capture(path, [record(params=sets, rows=[])])
    assert read_capture(path)[0].params == sets


def test_a_precise_type_comes_from_the_schema_not_from_the_data(tmp_path):
    """``'1.00'`` is data, ``decimal`` is the type of it: both survive a round trip."""
    params = encode_params([True, 1.5, "1.00", float("nan")])
    params[2] = {"t": "decimal", "v": "1.00"}
    path = tmp_path / "capture.yaml"
    write_capture(path, [record(params=params, rows=[])])
    assert "params: [true, 1.5, '1.00', nan]" in path.read_text(encoding="utf-8")
    assert read_capture(path)[0].params == params


# -- malformed files ----------------------------------------------------------


def capture(**overrides) -> dict:
    """A capture as a file holds it — built here, not by the writer."""
    payload = {"hash": "x", "sql": "SELECT 1", "n": 0}
    payload.update(overrides)
    return payload


def broken(path, payload):
    """A file of the shape the assertions below need."""
    Path(path).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_a_malformed_capture_is_reported(tmp_path):
    path = broken(tmp_path / "broken.yaml", {"captures": [capture(n="not-a-number")], "schemas": {}})
    with pytest.raises(CaptureFileError):
        read_capture(path)


def test_a_file_with_a_wrong_root_is_reported(tmp_path):
    path = broken(tmp_path / "broken.yaml", {"hash": "x"})
    with pytest.raises(CaptureFileError, match="mapping with the fields captures and schemas"):
        read_capture(path)


def test_a_file_that_is_a_plain_list_is_reported(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump([capture()], sort_keys=False), encoding="utf-8")
    with pytest.raises(CaptureFileError, match="mapping with the fields captures and schemas"):
        read_capture(path)


def test_a_malformed_schema_table_is_reported(tmp_path):
    path = broken(tmp_path / "broken.yaml", {"captures": [], "schemas": {1: "int"}})
    with pytest.raises(CaptureFileError, match="malformed schema"):
        read_capture(path)


def test_a_field_with_values_but_no_schema_is_reported(tmp_path):
    path = broken(tmp_path / "broken.yaml", {"captures": [capture(params=[1])], "schemas": {}})
    with pytest.raises(CaptureFileError, match="no schema id"):
        read_capture(path)


def test_a_capture_naming_an_unknown_schema_is_reported(tmp_path):
    payload = {"captures": [capture(schemas={"params": 4}, params=[1])], "schemas": {1: ["int"]}}
    with pytest.raises(CaptureFileError, match="unknown schema"):
        read_capture(broken(tmp_path / "broken.yaml", payload))


def test_a_value_that_does_not_match_its_schema_is_reported(tmp_path):
    payload = {"captures": [capture(schemas={"params": 1}, params=["abc"])], "schemas": {1: ["int"]}}
    with pytest.raises(CaptureFileError, match="does not match its schema"):
        read_capture(broken(tmp_path / "broken.yaml", payload))


def test_a_capture_without_a_schema_table_at_all_is_readable(tmp_path):
    """Nothing to describe: a capture that holds no typed value needs no schema."""
    path = broken(tmp_path / "capture.yaml", {"captures": [capture(sql="DELETE FROM shop_order")]})
    restored = read_capture(path)[0]
    assert restored.params == []
    assert restored.rows == []
    assert restored.rowcount is None
