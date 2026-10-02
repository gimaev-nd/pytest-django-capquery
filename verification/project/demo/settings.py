"""Settings of the verification project.

Everything the project needs comes from the environment, so the same project can be
run against an embedded postgres (``pgserver``) or a real server:

    CAPQUERY_VERIFY_DB_HOST, CAPQUERY_VERIFY_DB_PORT, CAPQUERY_VERIFY_DB_USER,
    CAPQUERY_VERIFY_DB_PASSWORD, CAPQUERY_VERIFY_DB_NAME
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "capquery-verification-secret"
DEBUG = False
ALLOWED_HOSTS: list[str] = []

USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.postgres",
    "demo.shop",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("CAPQUERY_VERIFY_DB_NAME", "capquery_verify"),
        "USER": os.environ.get("CAPQUERY_VERIFY_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("CAPQUERY_VERIFY_DB_PASSWORD", ""),
        "HOST": os.environ.get("CAPQUERY_VERIFY_DB_HOST", "localhost"),
        "PORT": os.environ.get("CAPQUERY_VERIFY_DB_PORT", ""),
    }
}
