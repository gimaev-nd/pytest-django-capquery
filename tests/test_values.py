"""Typed value encoding: what goes into a capture file must come back unchanged."""

from __future__ import annotations

import datetime
import decimal
import enum
import uuid

import pytest

from capquery.hashing import query_hash
from capquery.values import (
    UnsupportedValue,
    decode_row,
    decode_value,
    encode_params,
    encode_row,
    encode_value,
)

class _DriverWrapper:
    """psycopg hands binary parameters over in a wrapper like this one."""

    def __init__(self, obj):
        self.obj = obj


class _Status(str, enum.Enum):
    """What Django's ``models.TextChoices`` gives you: a ``str``, but not a ``str``."""

    FIRST = "first"
    SECOND = "second"

    def __str__(self):  # a display form that must never reach a capture file
        return f"OrderStatus.{self.name}"


class _Level(int, enum.Enum):
    """What Django's ``models.IntegerChoices`` gives you."""

    LOW = 1


class _Ratio(float, enum.Enum):
    """What a ``float`` subclass (``enum.StrEnum``'s siblings, numpy scalars) is."""

    HALF = 0.5


VALUES = [
    None,
    True,
    False,
    0,
    -17,
    3.5,
    "",
    "юникод ✓",
    decimal.Decimal("10.50"),
    decimal.Decimal("-0.000001"),
    datetime.date(2024, 2, 29),
    datetime.time(23, 59, 59, 123456),
    datetime.datetime(2024, 1, 1, 12, 0, 0, 500),
    datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc),
    datetime.timedelta(days=1, seconds=5, microseconds=3),
    uuid.UUID("11111111-2222-3333-4444-555555555555"),
    b"\x00\x01capquery",
    bytearray(b"\xff\xfe"),
    [1, "two", None],
    (1, "two"),
    {"a": 1, "b": [decimal.Decimal("1.5")]},
]


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_round_trip(value):
    assert decode_value(encode_value(value)) == value


def test_round_trip_keeps_exact_types():
    assert isinstance(decode_value(encode_value(decimal.Decimal("1.10"))), decimal.Decimal)
    assert isinstance(decode_value(encode_value(datetime.date(2024, 1, 1))), datetime.date)
    decoded = decode_value(encode_value(datetime.date(2024, 1, 1)))
    assert not isinstance(decoded, datetime.datetime)
    assert type(decode_value(encode_value(b"x"))) is bytes
    assert type(decode_value(encode_value((1, 2)))) is tuple


def test_bytes_are_base64_encoded_in_the_payload():
    payload = encode_value(b"\x00capquery")
    assert payload == {"t": "bytes", "v": "AGNhcHF1ZXJ5"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_special_floats(value):
    decoded = decode_value(encode_value(value))
    assert str(decoded) == str(value)


def test_bool_is_not_encoded_as_int():
    assert encode_value(True) == {"t": "bool", "v": True}
    assert encode_value(1) == {"t": "int", "v": 1}


def test_datetime_is_not_encoded_as_date():
    moment = datetime.datetime(2024, 1, 1, 1, 2, 3)
    assert encode_value(moment)["t"] == "datetime"
    assert encode_value(moment.date())["t"] == "date"


def test_unsupported_value_raises():
    class Opaque:
        pass

    with pytest.raises(UnsupportedValue):
        encode_value(Opaque())


def test_a_subclass_of_a_builtin_is_stored_as_its_builtin():
    """Django's ``TextChoices``/``IntegerChoices`` members are subclasses of a builtin.

    PyYAML resolves its representers by *exact* type, so a subclass has no
    representer at all (``RepresenterError: cannot represent an object``): a capture
    file may only ever hold the base type.
    """
    payloads = {
        _Status.FIRST: {"t": "str", "v": "first"},
        _Level.LOW: {"t": "int", "v": 1},
        _Ratio.HALF: {"t": "float", "v": 0.5},
    }
    for value, expected in payloads.items():
        payload = encode_value(value)
        assert payload is not None, "a supported value must always be encodable"
        assert payload == expected
        assert type(payload["v"]) is type(expected["v"])


def test_a_subclass_keeps_the_value_postgres_got_not_its_display_form():
    # ``str(TextChoices.DRAFT)`` is a human readable form ("OrderStatus.FIRST" here),
    # while the text sent to postgres is the underlying string: store that one
    assert encode_value(_Status.FIRST) == {"t": "str", "v": "first"}
    assert decode_value(encode_value(_Status.FIRST)) == _Status.FIRST


def test_subclasses_are_stripped_inside_parameters_rows_and_containers():
    assert encode_params([_Status.FIRST, _Level.LOW]) == [
        {"t": "str", "v": "first"},
        {"t": "int", "v": 1},
    ]
    assert encode_row([_Ratio.HALF]) == [{"t": "float", "v": 0.5}]
    assert encode_value([_Status.FIRST]) == {"t": "list", "v": [{"t": "str", "v": "first"}]}
    assert encode_value({"state": _Status.FIRST}) == {
        "t": "dict",
        "v": {"state": {"t": "str", "v": "first"}},
    }


def test_a_subclass_hashes_like_its_base_value():
    """A statement recorded before the subclasses were stripped keeps its hash."""
    assert query_hash("SELECT %s", [_Status.FIRST]) == query_hash("SELECT %s", ["first"])


def test_row_round_trip():
    row = (1, "name", decimal.Decimal("2.50"), None, b"\x01")
    assert decode_row(encode_row(row)) == row


def test_row_decodes_to_a_tuple():
    assert type(decode_row(encode_row([1, 2]))) is tuple


def test_bytes_wrapped_by_the_driver_are_encoded_as_bytes():
    """psycopg passes binary parameters as ``dbapi20.Binary``, not as plain bytes."""
    wrapped = _DriverWrapper(b"\x00\x01capquery")
    assert encode_value(wrapped) == encode_value(b"\x00\x01capquery")
    assert encode_value(memoryview(b"abc")) == {"t": "bytes", "v": "YWJj"}
    assert encode_params([wrapped])[0]["t"] == "bytes"


def test_an_unsupported_value_is_reported_as_a_value_not_as_a_container():
    with pytest.raises(UnsupportedValue, match="values of type .*set"):
        encode_params([{1, 2}])


def test_params_none_and_empty_are_the_same():
    assert encode_params(None) == encode_params(())
    assert encode_params(None) == encode_params([])


def test_params_are_encoded_positionally():
    assert encode_params(("x", 1)) == [{"t": "str", "v": "x"}, {"t": "int", "v": 1}]


def test_named_params_are_encoded_in_key_order():
    assert encode_params({"b": 2, "a": 1}) == encode_params({"a": 1, "b": 2})


def test_unknown_type_in_decoding_is_reported():
    with pytest.raises(ValueError, match="unknown value type"):
        decode_value({"t": "matrix", "v": []})


def test_malformed_payload_is_reported():
    with pytest.raises(ValueError, match="malformed typed value"):
        decode_value([1, 2])


# -- what the driver prepares for postgres-only columns ---------------------- #


def _psycopg_json(value):
    pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _psycopg_range(lower=None, upper=None, bounds="[)", empty=False):
    pytest.importorskip("psycopg")
    from psycopg.types.range import Range

    return Range(lower, upper, bounds, empty)


def test_a_json_wrapper_is_stored_as_the_data_it_wraps():
    """Django prepares a ``jsonb`` parameter as ``Jsonb(value)``, not as the value."""
    assert encode_value(_psycopg_json({"team": "core", "level": 1})) == encode_value(
        {"team": "core", "level": 1}
    )
    assert encode_value(_psycopg_json(["a", None])) == encode_value(["a", None])
    assert encode_value(_psycopg_json(None)) is None
    assert encode_params([_psycopg_json({"a": 1})])[0]["t"] == "dict"


def test_json_wrappers_of_other_drivers_are_unwrapped_too():
    """psycopg2 hides the value in ``adapted``; the shape of the pair is the same."""
    assert encode_value(_DriverWrapper({"a": 1})) == encode_value({"a": 1})
    wrapper = _DriverWrapper(b"\x00\x01")
    assert encode_value(wrapper) == {"t": "bytes", "v": "AAE="}  # bytes, not json


def test_an_ipaddress_object_is_stored_as_its_text():
    """Django prepares an ``inet`` parameter with ``ipaddress.ip_address``."""
    ipaddress = pytest.importorskip("ipaddress")

    assert encode_value(ipaddress.ip_address("10.0.0.1")) == {"t": "str", "v": "10.0.0.1"}
    assert encode_value(ipaddress.ip_address("2001:db8::1")) == {"t": "str", "v": "2001:db8::1"}
    assert encode_value(ipaddress.ip_network("10.0.0.0/24")) == {"t": "str", "v": "10.0.0.0/24"}


def test_a_range_round_trips_with_its_bounds_and_their_types():
    value = _psycopg_range(datetime.date(2024, 1, 1), datetime.date(2024, 1, 31))
    payload = encode_value(value)
    assert payload is not None and payload["t"] == "range"
    assert payload["v"]["bounds"] == "[)"
    decoded = decode_value(payload)
    assert (decoded.lower, decoded.upper, decoded.bounds) == (
        datetime.date(2024, 1, 1),
        datetime.date(2024, 1, 31),
        "[)",
    )
    assert decoded == value


def test_a_range_with_unset_bounds_round_trips():
    value = _psycopg_range(None, decimal.Decimal("20.5"), bounds="(]")
    payload = encode_value(value)
    assert payload["v"]["lower"] is None
    decoded = decode_value(payload)
    assert (decoded.lower, decoded.upper, decoded.bounds) == (None, decimal.Decimal("20.50"), "(]")
    assert decoded == value


def test_an_empty_range_round_trips():
    value = _psycopg_range(empty=True)
    payload = encode_value(value)
    assert payload["v"]["empty"] is True
    decoded = decode_value(payload)
    assert decoded.isempty
    assert decoded == value


def test_a_range_inside_a_container_is_encoded_as_a_range():
    value = [_psycopg_range(1, 5), (2, "b")]
    payload = encode_value(value)
    assert payload["t"] == "list"
    assert payload["v"][0]["t"] == "range"
    items = decode_value(payload)
    assert items[0] == _psycopg_range(1, 5)
    assert items[1] == (2, "b")


def test_a_range_keeps_its_type_in_a_capture_file(tmp_path):
    """The bounds' types come from the schema, the payload from the data."""
    from capquery.records import Record
    from capquery.yaml_io import read_capture, write_capture

    range_value = _psycopg_range(datetime.date(2024, 3, 1), datetime.date(2024, 3, 31))
    record = Record(
        hash="9f2c",
        n=0,
        sql="SELECT active FROM shop_ticket WHERE id = %s",
        params=[{"t": "int", "v": 1}],
        rowcount=1,
        columns=["active"],
        rows=[[encode_value(range_value)]],
    )
    path = tmp_path / "capture.yaml"
    write_capture(path, [record])
    text = path.read_text(encoding="utf-8")
    assert "{range: {lower: date, upper: date}}" in text

    [read_back] = read_capture(path)
    decoded = read_back.decoded_rows()[0][0]
    assert decoded == range_value
    assert decoded.lower == datetime.date(2024, 3, 1)


def test_an_object_of_an_unknown_type_is_still_unsupported():
    class Opaque:
        pass

    with pytest.raises(UnsupportedValue, match="Opaque"):
        encode_value(Opaque())
    with pytest.raises(UnsupportedValue, match="Bounds"):
        # duck-typing a range must not swallow anything that happens to be shaped
        # like one: only the four bound flags postgres has are a range
        encode_value(type("Bounds", (), {"lower": 1, "upper": 2, "bounds": "both"})())
