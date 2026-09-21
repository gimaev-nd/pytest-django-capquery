"""Query hashing.

Only the hash of a query is used as a lookup key (the SQL text is kept in the
capture file and in sqlite for diagnostics only).  The hash is computed from
the pair ``(sql, params)`` so that two executions of the same statement with
different parameters are two different capture entries.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .values import encode_params

__all__ = ["hash_for", "query_hash", "stable_json"]


def stable_json(payload: Any) -> str:
    """Deterministic JSON: key order and separators never depend on the caller."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def hash_for(sql: str, encoded_params: list) -> str:
    """Hash of a statement with already encoded parameters."""
    payload = stable_json(encoded_params).encode("utf-8")
    return hashlib.sha256(sql.encode("utf-8") + b"\x00" + payload).hexdigest()


def query_hash(sql: str, params: Any = None) -> str:
    """Hash of a statement and its parameters."""
    return hash_for(sql, encode_params(params))
