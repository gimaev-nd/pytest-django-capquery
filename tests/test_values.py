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
