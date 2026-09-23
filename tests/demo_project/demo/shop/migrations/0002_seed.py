"""A data migration: it inserts rows, which moves the id sequence forward.

It is the reason the plugin resets sequences with ``sqlsequencereset`` before the
migrations run: without the reset the ids of newly created rows would depend on
how often the test database was reused.
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from django.db import migrations

ROWS = [
    ("first", decimal.Decimal("10.50"), "11111111-1111-1111-1111-111111111111", b"\x00\x01capquery"),
    ("second", decimal.Decimal("20.00"), "22222222-2222-2222-2222-222222222222", None),
    ("third", decimal.Decimal("30.25"), "33333333-3333-3333-3333-333333333333", None),
]


def seed(apps, schema_editor):
    order = apps.get_model("shop", "Order")
    created = datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    for name, amount, token, payload in ROWS:
        order.objects.create(
            name=name, amount=amount, token=uuid.UUID(token), payload=payload, created=created
        )


def unseed(apps, schema_editor):
    order = apps.get_model("shop", "Order")
    order.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
