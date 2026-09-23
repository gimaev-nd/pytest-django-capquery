from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=50)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=8)),
                ("created", models.DateTimeField(blank=True, null=True)),
                ("payload", models.BinaryField(blank=True, null=True)),
                ("token", models.UUIDField(blank=True, null=True)),
            ],
        ),
    ]
