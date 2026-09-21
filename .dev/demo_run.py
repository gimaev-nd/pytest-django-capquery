"""Dev helper: start the embedded postgres and run pytest against the demo project."""

import os
import subprocess
import sys

import pgserver

REPO = "/opt/data/work/gimaev-nd/pytest-django-capquery"
DATA_DIR = "/tmp/capquery-pgdata"

pgserver.get_server(DATA_DIR, cleanup_mode=None)
env = dict(
    os.environ,
    CAPQUERY_DEMO_DB_HOST=DATA_DIR,
    CAPQUERY_DEMO_DB_USER="postgres",
    CAPQUERY_DEMO_DB_PASSWORD="",
    CAPQUERY_DEMO_DB_PORT="",
    CAPQUERY_DEMO_DB_NAME=os.environ.get("DEMO_DB", "demo_manual"),
)
args = sys.argv[1:] or ["tests/demo_project", "-q"]
sys.exit(subprocess.call([sys.executable, "-m", "pytest", *args], env=env, cwd=REPO))
