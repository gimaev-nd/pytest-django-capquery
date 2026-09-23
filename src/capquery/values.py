"""Typed encoding of database values.

A value gets an exact type and value pair — ``{t: <type>, v: <value>}`` — because
decoding has to give back objects of the very same type: a test compares exactly what
it compared during the recording run (``Decimal`` stays ``Decimal``, a ``Range`` stays
a ``Range`` and so on).  The pair is what the plugin keeps of a value: the query hash is
computed from the encoded parameters and the in-memory sqlite store keeps them.  A
capture file writes the data of a value and its type separately — the data in the
field, the type in the schema of that field (:mod:`capquery.schemas`).

Values are stored as the *exact* builtin type: a subclass of ``str``/``int``/``float``
— Django's ``models.TextChoices`` and ``models.IntegerChoices``, ``enum.StrEnum``, a
driver scalar (``psycopg``'s ``Int4``) — is normalized to its base type, because a
capture file holds no object of a type PyYAML has no representer for.  Whatever the
driver *prepares* for the server is normalized the same way, because Django hands the
cursor an adapter object rather than a plain value for the postgres-only fields:
``Jsonb`` (a ``jsonb`` parameter) is stored as the data it wraps, ``ipaddress`` objects
(an ``inet`` parameter) as their text form, and ``Range`` — as a parameter or in a row —
as a ``range`` value of its bounds and the bounds' own types.  psycopg2's forms of the
same values (``psycopg2.extras.Inet``, and range types that expose ``lower_inc`` /
``upper_inc`` instead of a public ``bounds``) are recognized the same way.

    encode_value(Jsonb({"team": "core"}))          -> {"t": "dict", "v": {...}}
    encode_value(ipaddress.ip_address("10.0.0.1")) -> {"t": "str", "v": "10.0.0.1"}
    encode_value(DateRange(date(2024, 1, 1), ...)) -> {"t": "range", "v": {...}}
"""

from __future__ import annotations

import base64
import datetime
import decimal
import math
import operator
import uuid
from typing import Any, Optional

__all__ = [
    "RANGE_BOUNDS",
    "RangeValue",
    "UnsupportedValue",
    "decode_row",
    "decode_value",
    "encode_params",
    "encode_row",
    "encode_value",
]

#: The bound flags ``[]``/``[)``/``(]``/``()`` of a range, and nothing else.
RANGE_BOUNDS = ("[]", "[)", "(]", "()")


class UnsupportedValue(TypeError):
    """Raised when a value has no typed representation in a capture file."""


#: Answers "this is not the wrapper/driver object I was looking for" without lying about
#: a payload of ``None``, which is a perfectly good value to encode.
_NO_PAYLOAD = object()


class RangeValue:
    """A range whose driver class is not importable (``psycopg`` missing at read time).

    ``psycopg``'s ``Range`` is rebuilt whenever it is available; this stand-in only
    keeps a capture readable (and comparable) for everything else.
    """

    __slots__ = ("lower", "upper", "bounds")

    def __init__(self, lower: Any = None, upper: Any = None, bounds: str = "[)", empty: bool = False) -> None:
        self.lower = lower
        self.upper = upper
        self.bounds = "" if empty else (bounds or "[)")

    @property
    def isempty(self) -> bool:
        return not self.bounds

    def __repr__(self) -> str:
        if not self.bounds:
            return "RangeValue(empty=True)"
        return f"RangeValue({self.lower!r}, {self.upper!r}, {self.bounds!r})"

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, RangeValue):
            return NotImplemented
        return (self.lower, self.upper, self.bounds) == (other.lower, other.upper, other.bounds)

    def __hash__(self) -> int:
        return hash((self.lower, self.upper, self.bounds))


def _bounds_of(value: Any) -> Optional[str]:
    """The two bound flags of a range as the string postgres writes them.

    ``psycopg`` exposes the bounds themselves (``"[)"``, ``""`` for an empty range),
    ``psycopg2`` only the flags of the two ends: its range classes have a private
    ``_bounds`` and public ``lower_inc`` / ``upper_inc``, so the string has to be built
    from the flags.  ``None`` means "this object carries no bounds at all", i.e. it is
    not a range.
    """
    if bool(getattr(value, "isempty", False)):
        return ""
    bounds = getattr(value, "bounds", None)
    if isinstance(bounds, str) and bounds in RANGE_BOUNDS:
        return bounds
    lower_inc = getattr(value, "lower_inc", None)
    upper_inc = getattr(value, "upper_inc", None)
    if isinstance(lower_inc, bool) and isinstance(upper_inc, bool):
        return ("[" if lower_inc else "(") + ("]" if upper_inc else ")")
    return None


def _range_of(value: Any) -> Optional[dict]:
    """The bounds inside a driver range object, or ``None`` when it is not one.

    Duck-typed on purpose: ``psycopg``'s ``Range``/``DateRange``/``NumericRange`` and
    ``psycopg2``'s range types all expose ``lower`` and ``upper``, and both drivers use
    ``""`` for an empty range.  Whether an object *is* one is decided by its bounds
    (:func:`_bounds_of`), not by its class.
    """
    lower = getattr(value, "lower", _NO_PAYLOAD)
    upper = getattr(value, "upper", _NO_PAYLOAD)
    if lower is _NO_PAYLOAD or upper is _NO_PAYLOAD:
        return None
    bounds = _bounds_of(value)
    if bounds is None:
        return None
    return {
        "lower": lower,
        "upper": upper,
        "bounds": bounds,
        "empty": bool(getattr(value, "isempty", not bounds)),
    }


def _range_class():
    """The driver's range class, or ``None`` when no driver is importable.

    The *generic* class of each driver is what a capture can be decoded with: it takes
    the bounds of any range, and psycopg2's ``Range`` compares equal to its
    ``DateRange``/``NumericRange`` (psycopg's ``Range`` does too, and it is what
    psycopg itself returns for a range whose bounds it cannot type further).
    """
    try:
        from psycopg.types.range import Range

        return Range
    except Exception:  # noqa: BLE001 - a missing driver is not an error here
        pass
    try:  # psycopg2 exposes the same interface through its range types
        from psycopg2.extras import Range as Psycopg2Range

        return Psycopg2Range
    except Exception:  # noqa: BLE001
        return None


def _decode_range(payload: Any) -> Any:
    """Rebuild a range value, with the driver's class when it is available."""
    if not isinstance(payload, dict):
        raise ValueError(f"capquery: malformed range value: {payload!r}")
    lower = decode_value(payload.get("lower"))
    upper = decode_value(payload.get("upper"))
    bounds = payload.get("bounds") or ""
    empty = bool(payload.get("empty")) or not bounds
    factory = _range_class()
    if factory is not None:
        try:
            return factory(lower, upper, bounds or "[)", empty)
        except Exception:  # noqa: BLE001 - an unusual driver signature, keep the data
            pass
    return RangeValue(lower, upper, bounds or "[)", empty)


def _json_payload(value: Any) -> Any:
    """The plain value inside a driver's JSON wrapper, or :data:`_NO_PAYLOAD`.

    Django prepares a ``jsonb`` parameter as ``psycopg.types.json.Jsonb(value)`` (and
    as ``psycopg2.extras.Json`` with an ``adapted`` attribute on psycopg2), so the
    value it meant to send is hidden inside a wrapper object that has no place in a
    capture file.  The wrapper is unwrapped, its payload is what gets stored.
    """
    for attribute in ("obj", "adapted"):
        if not hasattr(value, attribute):
            continue
        inner = getattr(value, attribute)
        if inner is None or isinstance(inner, (bool, int, float, str, list, tuple, dict, decimal.Decimal)):
            return inner
    return _NO_PAYLOAD


def _ipaddress_text(value: Any) -> Optional[str]:
    """The text form of an ``ipaddress`` object, or ``None`` for anything else.

    Django prepares an ``inet`` parameter with ``ipaddress.ip_address``; storing its
    text form is what the server receives (``inet`` columns are read back as text as
    well, because Django registers a text loader for them).  With psycopg2 Django hands
    over the driver's own adapter instead, whose ``addr`` is the very text the server
    receives — a netmask survives it (``"10.0.0.0/24"``), so it is stored as it is.
    """
    module = type(value).__module__
    if module == "ipaddress":
        return str(value)
    if module.split(".")[0] == "psycopg2":
        addr = getattr(value, "addr", None)
        if isinstance(addr, str):
            return addr
    return None


def encode_value(value: Any) -> dict | None:
    """Encode a single value returned by psycopg into a typed pair.

    A subclass of a builtin is stored as the builtin itself: only the exact type has a
    representer in PyYAML, and ``v`` holds what postgres received (the text of a
    ``TextChoices`` member, not its display form).  A value the driver wrapped
    (``Jsonb``, ``Binary``) or adapted (``ipaddress``, ``Range``) is encoded as the
    value it stands for.
    """
    if value is None:
        return None
    # bool must be checked before int: bool is a subclass of int.
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        # operator.index() gives an exact int, never the subclass instance
        return {"t": "int", "v": operator.index(value)}
    if isinstance(value, float):
        if math.isnan(value):
            return {"t": "float", "v": "nan"}
        if math.isinf(value):
            return {"t": "float", "v": "inf" if value > 0 else "-inf"}
        # float.__float__ copies the value out of a subclass as an exact float
        return {"t": "float", "v": float.__float__(value)}
    if isinstance(value, str):
        # str.__str__ is the text postgres received, not the __str__ a class may
        # override for display (TextChoices.DRAFT prints "ResultStatus.DRAFT")
        return {"t": "str", "v": str.__str__(value)}
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
    payload = _json_payload(value)
    if payload is not _NO_PAYLOAD:
        return encode_value(payload)
    text = _ipaddress_text(value)
    if text is not None:
        return {"t": "str", "v": text}
    bounds = _range_of(value)
    if bounds is not None:
        return {
            "t": "range",
            "v": {
                "lower": encode_value(bounds["lower"]),
                "upper": encode_value(bounds["upper"]),
                "bounds": bounds["bounds"],
                "empty": bounds["empty"],
            },
        }
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
    if kind == "range":
        return _decode_range(raw)
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
