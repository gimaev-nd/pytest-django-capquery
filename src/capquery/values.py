"""Typed encoding of database values for capture files.

A capture file is read by humans during code review, so values are stored as
``{t: <type>, v: <value>}`` pairs instead of a lossy ``str()`` representation.
Decoding must give back objects of the very same type, so a test compares
exactly what it compared during the recording run (``Decimal`` stays
``Decimal``, ``datetime`` stays ``datetime`` and so on).
"""

from __future__ import annotations

import base64
import datetime
import decimal
import math
import uuid
from typing import Any

__all__ = [
    "UnsupportedValue",
    "decode_row",
    "decode_value",
    "encode_params",
    "encode_row",
    "encode_value",
]


class UnsupportedValue(TypeError):
    """Raised when a value has no typed representation in a capture file."""


def encode_value(value: Any) -> dict | None:
    """Encode a single value returned by psycopg into a typed pair."""
    if value is None:
        return None
    # bool must be checked before int: bool is a subclass of int.
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, float):
        if math.isnan(value):
            return {"t": "float", "v": "nan"}
        if math.isinf(value):
            return {"t": "float", "v": "inf" if value > 0 else "-inf"}
        return {"t": "float", "v": value}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, decimal.Decimal):
        return {"t": "decimal", "v": str(value)}
    # datetime before date: datetime is a subclass of date.
    if isinstance(value, datetime.datetime):
        return {"t": "datetime", "v": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"t": "time", "v": value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {"t": "timedelta", "v": value.total_seconds()}
    if isinstance(value, uuid.UUID):
        return {"t": "uuid", "v": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"t": "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}
    wrapped = _wrapped_bytes(value)
    if wrapped is not None:
        return {"t": "bytes", "v": base64.b64encode(wrapped).decode("ascii")}
    if isinstance(value, list):
        return {"t": "list", "v": [encode_value(item) for item in value]}
    if isinstance(value, tuple):
        return {"t": "tuple", "v": [encode_value(item) for item in value]}
    if isinstance(value, dict):
        return {"t": "dict", "v": {str(key): encode_value(item) for key, item in value.items()}}
    raise UnsupportedValue(
        f"capquery cannot store values of type {type(value).__module__}.{type(value).__name__}"
    )


def _wrapped_bytes(value: Any) -> bytes | None:
    """Bytes hidden inside a wrapper object, as DB-API drivers like to hand over.

    psycopg passes binary parameters as ``psycopg.dbapi20.Binary``, a plain wrapper
    with an ``obj`` attribute, and other drivers use ``adapted``.
    """
    for attribute in ("adapted", "obj"):
        inner = getattr(value, attribute, None)
        if isinstance(inner, (bytes, bytearray, memoryview)):
            return bytes(inner)
    try:
        return bytes(memoryview(value))
    except TypeError:
        return None


def decode_value(payload: Any) -> Any:
    """Decode a typed pair back into a Python object."""
    if payload is None:
        return None
    if not isinstance(payload, dict) or "t" not in payload:
        raise ValueError(f"capquery: malformed typed value: {payload!r}")
    kind = payload["t"]
    raw = payload.get("v")
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        return int(raw)
    if kind == "float":
        if raw == "nan":
            return float("nan")
        if raw == "inf":
            return float("inf")
        if raw == "-inf":
            return float("-inf")
        return float(raw)
    if kind == "str":
        return str(raw)
    if kind == "decimal":
        return decimal.Decimal(raw)
    if kind == "datetime":
        return datetime.datetime.fromisoformat(raw)
    if kind == "date":
        return datetime.date.fromisoformat(raw)
    if kind == "time":
        return datetime.time.fromisoformat(raw)
    if kind == "timedelta":
        return datetime.timedelta(seconds=raw)
    if kind == "uuid":
        return uuid.UUID(raw)
    if kind == "bytes":
        return base64.b64decode(raw.encode("ascii"))
    if kind == "list":
        return [decode_value(item) for item in raw]
    if kind == "tuple":
        return tuple(decode_value(item) for item in raw)
    if kind == "dict":
        return {key: decode_value(item) for key, item in raw.items()}
    raise ValueError(f"capquery: unknown value type {kind!r}")


def encode_row(row: Any) -> list:
    """Encode one database row (any sequence of values)."""
    return [encode_value(value) for value in row]


def decode_row(row: Any) -> tuple:
    """Decode one database row back into a tuple, like psycopg returns."""
    return tuple(decode_value(value) for value in row)


def encode_params(params: Any) -> list:
    """Encode query parameters into a hashable, storable form.

    ``None`` and ``()`` mean "no parameters" and are encoded identically, so a
    query with and without an empty parameter list hashes to the same value.
    """
    if params is None:
        return []
    if isinstance(params, dict):
        return [encode_value({"key": key, "value": value}) for key, value in sorted(params.items())]
    if isinstance(params, (str, bytes)):
        # psycopg requires a sequence, but be forgiving: treat as one parameter.
        return [encode_value(params)]
    try:
        values = list(params)
    except TypeError as exc:  # not iterable
        raise UnsupportedValue(f"capquery cannot store parameters of type {type(params)!r}") from exc
    # an unsupported value inside the parameters must not be reported as an
    # unsupported *container*: encode_value raises UnsupportedValue, a TypeError
    return [encode_value(value) for value in values]
