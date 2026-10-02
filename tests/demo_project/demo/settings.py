"""Settings of the demo project used by the integration tests.

Everything the tests need comes from the environment, so the project can be run
against an embedded postgres (``pgserver``) or a real server::

    CAPQUERY_DEMO_DB_HOST, CAPQUERY_DEMO_DB_PORT, CAPQUERY_DEMO_DB_USER,
    CAPQUERY_DEMO_DB_PASSWORD, CAPQUERY_DEMO_DB_NAME
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "capquery-demo-secret-key"
DEBUG = False
ALLOWED_HOSTS: list[str] = []

USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "demo.shop",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("CAPQUERY_DEMO_DB_NAME", "capquery_demo"),
        "USER": os.environ.get("CAPQUERY_DEMO_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("CAPQUERY_DEMO_DB_PASSWORD", ""),
        "HOST": os.environ.get("CAPQUERY_DEMO_DB_HOST", "localhost"),
        "PORT": os.environ.get("CAPQUERY_DEMO_DB_PORT", ""),
    }
}
