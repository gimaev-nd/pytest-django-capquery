"""Models built around the field types only postgres has.

Every field the plugin has to survive is here: ``text[]``/``int[]`` arrays, ``jsonb``,
``daterange``/``tstzrange``/``int4range``, ``inet``, ``bytea`` and a ``tsvector`` with a
GIN index.  The tests then query them with the postgres-only lookups (array containment
and overlap, ``DISTINCT ON``, ranges, full text search) and write to them through the
ORM, so both the parameters and the rows of a capture carry postgres types.
"""

from __future__ import annotations

from django.contrib.postgres.fields import (
    ArrayField,
    DateRangeField,
    DateTimeRangeField,
    IntegerRangeField,
)
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models


class Team(models.Model):
    name = models.CharField(max_length=40, unique=True)


class Comment(models.Model):
    """A plain model: only column types every backend has.

    The row of such a model holds nothing the plugin cannot store, so its ordinary ORM
    usage — ``create``, ``update``, ``delete``, ``get_or_create`` — is cacheable.  It is
    here to tell apart "the plugin is broken" from "this statement holds a value the
    plugin cannot store".
    """

    ticket = models.ForeignKey("shop.Ticket", on_delete=models.CASCADE, related_name="comments")
    body = models.CharField(max_length=200)
    score = models.IntegerField()
    created = models.DateTimeField()


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "open"
        CLOSED = "closed", "closed"
        HOLD = "hold", "hold"

    code = models.UUIDField(unique=True)
    title = models.CharField(max_length=120)
    status = models.CharField(max_length=8, choices=Status.choices)
    priority = models.IntegerField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    effort = models.DurationField(null=True)
    created = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)

    # postgres-only column types
    labels = ArrayField(models.CharField(max_length=24), default=list)  # text[]
    scores = ArrayField(models.IntegerField(), default=list)  # int[]
    meta = models.JSONField(default=dict)  # jsonb
    active = DateRangeField(null=True)  # daterange
    review = DateTimeRangeField(null=True)  # tstzrange
    weight = IntegerRangeField(null=True)  # int4range
    peer = models.GenericIPAddressField(null=True)  # inet
    payload = models.BinaryField(null=True)  # bytea
    document = SearchVectorField(null=True)  # tsvector

    owner = models.ForeignKey(Team, null=True, on_delete=models.CASCADE, related_name="tickets")
    watchers = models.ManyToManyField(Team, related_name="watching", blank=True)

    class Meta:
        indexes = [
            GinIndex(fields=["labels"]),
            GinIndex(fields=["meta"]),
            GinIndex(fields=["document"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.title} ({self.status})"
