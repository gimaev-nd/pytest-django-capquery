from django.db import models


class Order(models.Model):
    """A model that covers the value types capquery has to store."""

    name = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    created = models.DateTimeField(null=True, blank=True)
    payload = models.BinaryField(null=True, blank=True)
    token = models.UUIDField(null=True, blank=True)

    class Meta:
        app_label = "shop"
