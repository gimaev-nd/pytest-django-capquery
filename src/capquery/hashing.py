"""Query hashing.

A capture is looked up by the *position* of a statement in its context — the first
data statement of a test, the second one, and so on — and the hash of the statement
is what tells whether the query that arrived at that position is still the one that
was captured.  The hash is therefore computed from the statement text alone: the
parameters are not part of any key, they are stored next to the rows as data.  Two
executions of one statement with different parameters are two positions of the
context, and a parameter that is new in every run (``timezone.now``, a ``uuid4``
default, an ``IN (...)`` list built from a ``set``) no longer makes its statement
unfindable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["query_hash", "stable_json"]


def stable_json(payload: Any) -> str:
    """Deterministic JSON: key order and separators never depend on the caller."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def query_hash(sql: str) -> str:
    """Hash of a statement, the same for every execution of it.

    The parameters are deliberately left out: an execution is found by its position in
    the context, and this hash only has to answer "is the query at this position still
    the recorded one?".
    """
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
