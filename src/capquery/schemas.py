"""The data schemas of a capture file.

A capture file is read by humans during code review, so a value is written as plain
data and its type is described once, per field, in a table of schemas shared by the
whole file::

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

A schema is the list of the types of the values of one field — one type per parameter,
one type per column of a row — so the type of a value is the schema entry at its own
place.  Identical schemas share one id (:class:`SchemaTable`), and a capture names the
schemas of its fields by id, so the file never repeats a schema.

The descriptor of a scalar is the name of its type, the same names :mod:`capquery.values`
uses.  A container is described by a mapping of its kind to the descriptors of what it
holds::

    {list: [int, str]}      a list of an int and a str
    {tuple: [int]}          a tuple of one int
    {dict: {key: int}}      a dict whose ``key`` holds an int

A schema is only as exact as the data allows.  A type no piece of data can be told
apart from a string — ``decimal``, ``date``, ``time``, ``datetime``, ``timedelta``,
``uuid``, ``bytes`` — comes from the schema alone, so every value of a field that is
described keeps the very type the recording run compared.

An ``executemany`` is one capture whose parameters are the parameter sets it was called
with, so the schema of such a field describes the sets one by one::

    params:                          schemas:
      - [first, 1]                     1: [[str, int], [str, int]]
      - [second, 2]
"""

from __future__ import annotations

import datetime
from typing import Any, Iterable

from .hashing import stable_json
from .values import UnsupportedValue, decode_value, encode_value

__all__ = [
    "NULL_TYPE",
    "SchemaTable",
    "data_of_params",
    "descriptor_of",
    "plain_of",
    "schema_of_params",
    "schema_of_rows",
    "typed_field",
]

#: Descriptor of a value that is ``None`` wherever the field holds anything at all.
NULL_TYPE = "null"

_CONTAINER_KINDS = ("list", "tuple", "dict")


class SchemaTable:
    """The schemas of one file and the ids they are referred to by."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._schemas: dict[int, Any] = {}

    def id_for(self, schema: list) -> int:
        """The id of a schema, handed out in the order the schemas appear.

        An identical schema never gets a second id and is never written twice.
        """
        key = stable_json(schema)
        known = self._ids.get(key)
        if known is None:
            known = len(self._ids) + 1
            self._ids[key] = known
            self._schemas[known] = schema
        return known

    def payload(self) -> dict:
        """The ``schemas:`` table of the file: id -> schema, in id order."""
        return {number: self._schemas[number] for number in sorted(self._schemas)}


def descriptor_of(encoded: Any) -> Any:
    """The descriptor of one encoded value (a ``{t, v}`` pair, or ``None``)."""
    if encoded is None:
        return NULL_TYPE
    kind = encoded["t"]
    data = encoded.get("v")
    if kind == "dict":
        return {"dict": {str(key): descriptor_of(item) for key, item in (data or {}).items()}}
    if kind in ("list", "tuple"):
        return {kind: [descriptor_of(item) for item in (data or [])]}
    return kind


def plain_of(encoded: Any) -> Any:
    """The data of one encoded value, without its type."""
    if encoded is None:
        return None
    kind = encoded["t"]
    data = encoded.get("v")
    if kind == "dict":
        return {str(key): plain_of(item) for key, item in (data or {}).items()}
    if kind in ("list", "tuple"):
        return [plain_of(item) for item in (data or [])]
    return data


def schema_of_params(encoded_params: Iterable[Any]) -> list:
    """The schema of a params field: one descriptor per parameter.

    ``executemany`` is captured as one record whose parameters are the list of the
    parameter sets it was called with, so the schema of such a field is a list of the
    schemas of those sets.
    """
    return [_descriptor_of_entry(value) for value in encoded_params]


def _descriptor_of_entry(entry: Any) -> Any:
    """One entry of a params field: an encoded value, or a whole parameter set."""
    if isinstance(entry, list):
        return [_descriptor_of_entry(item) for item in entry]
    return descriptor_of(entry)


def data_of_params(encoded_params: Iterable[Any]) -> list:
    """The data of a params field, an ``executemany`` kept as the sets it was given."""
    return [_plain_of_entry(value) for value in encoded_params]


def _plain_of_entry(entry: Any) -> Any:
    """One entry of a params field: the data of an encoded value, or of a whole set."""
    if isinstance(entry, list):
        return [_plain_of_entry(item) for item in entry]
    return plain_of(entry)


def schema_of_rows(encoded_rows: Iterable[list]) -> list:
    """The schema of a field that holds rows: one descriptor per column.

    A column is described by the first row that holds something other than ``None``
    there — a null value is written as null data, which needs no type of its own — and
    a column that is null in every row keeps the ``null`` descriptor.
    """
    schema: list = []
    for row in encoded_rows:
        for index, value in enumerate(row):
            if index == len(schema):
                schema.append(NULL_TYPE)
            if schema[index] == NULL_TYPE:
                descriptor = descriptor_of(value)
                if descriptor != NULL_TYPE:
                    schema[index] = descriptor
    return schema


def typed_field(schema: Any, values: Any) -> list:
    """Decode the values of one field: each value with the schema entry at its place."""
    return [_typed_at(schema, index, value) for index, value in enumerate(values)]


def typed_of(descriptor: Any, plain: Any) -> Any:
    """Encode one plain value with the type its descriptor names."""
    if plain is None:
        return None
    if descriptor is None:
        return _guessed_of(plain)
    if isinstance(descriptor, str):
        if descriptor == NULL_TYPE:
            raise ValueError(f"the schema says null where the data holds {plain!r}")
        if isinstance(plain, (list, dict)):
            raise ValueError(f"the schema says {descriptor} where the data holds {plain!r}")
        return {"t": descriptor, "v": _scalar_of(descriptor, plain)}
    if isinstance(descriptor, list):
        # the schema of one parameter set of an ``executemany``; its data is the set
        if not isinstance(plain, list):
            raise ValueError(f"the schema describes a parameter set where the data holds {plain!r}")
        return typed_field(descriptor, plain)
    if isinstance(descriptor, dict):
        kind = next((name for name in _CONTAINER_KINDS if name in descriptor), None)
        if kind is None:
            raise ValueError(f"unknown schema descriptor {descriptor!r}")
        contents = descriptor[kind]
        if kind == "dict":
            if not isinstance(plain, dict):
                raise ValueError(f"the schema says a dict where the data holds {plain!r}")
            return {
                "t": "dict",
                "v": {str(key): _entry_of(contents, key, item) for key, item in plain.items()},
            }
        if not isinstance(plain, list):
            raise ValueError(f"the schema says a {kind} where the data holds {plain!r}")
        return {"t": kind, "v": [_typed_at(contents, index, item) for index, item in enumerate(plain)]}
    raise ValueError(f"unknown schema descriptor {descriptor!r}")


def _scalar_of(kind: str, plain: Any) -> Any:
    """The data of one scalar in the form :mod:`capquery.values` writes it.

    A capture file is read at the start of a session and what it holds is what a replay
    answers with, so the data goes through the decoder and the encoder of
    :mod:`capquery.values`: the pair is the very one a recording run produced, with
    ``'1.00'`` for a decimal and an ISO string for a date, and an unquoted yaml
    timestamp (read back as a date instead of a string) ends up in the same form.
    """
    if kind in ("date", "datetime", "time") and isinstance(plain, (datetime.date, datetime.datetime)):
        plain = plain.isoformat()
    encoded = encode_value(decode_value({"t": kind, "v": plain}))
    # encode_value answers None only for None, and a scalar that holds data is not None
    return encoded["v"] if encoded is not None else plain


def _typed_at(schema: Any, index: int, plain: Any) -> Any:
    """Decode the value at one place of a field.

    A place the schema does not describe is read from the data itself: either the file
    was written by hand, or the schema came from a container of another row (an array
    column whose rows hold a different number of elements).
    """
    if isinstance(schema, list) and index < len(schema):
        return typed_of(schema[index], plain)
    return _guessed_of(plain)


def _entry_of(contents: Any, key: str, plain: Any) -> Any:
    """One entry of a described dict."""
    if isinstance(contents, dict) and key in contents:
        return typed_of(contents[key], plain)
    return _guessed_of(plain)


def _guessed_of(plain: Any) -> Any:
    """An encoded value guessed from the data alone, for what no schema describes.

    Only the types the data itself tells apart can be guessed: a string standing for a
    ``decimal``, ``date``, ``time``, ``uuid`` or ``bytes`` is indistinguishable from a
    string, and is read back as one.
    """
    if plain is None:
        return None
    # bool must be checked before int: bool is a subclass of int.
    if isinstance(plain, bool):
        return {"t": "bool", "v": plain}
    if isinstance(plain, int):
        return {"t": "int", "v": plain}
    if isinstance(plain, float):
        return {"t": "float", "v": plain}
    if isinstance(plain, str):
        return {"t": "str", "v": plain}
    # datetime before date: datetime is a subclass of date.
    if isinstance(plain, datetime.datetime):
        return {"t": "datetime", "v": plain.isoformat()}
    if isinstance(plain, datetime.date):
        return {"t": "date", "v": plain.isoformat()}
    if isinstance(plain, list):
        return {"t": "list", "v": [_guessed_of(item) for item in plain]}
    if isinstance(plain, dict):
        return {"t": "dict", "v": {str(key): _guessed_of(item) for key, item in plain.items()}}
    raise UnsupportedValue(f"capquery cannot read a value of type {type(plain).__name__}")
