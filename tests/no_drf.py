"""A subprocess harness running Python in a project without Django REST
framework: an import blocker refuses ``rest_framework``, ``django_filters``,
``scrudful.rest_framework`` and scoped-rbac's DRF modules (not ninja), the
settings are ``tests.settings.SETTINGS`` without ``rest_framework*`` and
``REST_FRAMEWORK``, the URLconf is ``tests.no_drf_urls``, and the in-memory
database is migrated. `run_without_drf` runs a script body after that setup.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRELUDE = """
import importlib.abc, sys

BLOCKED = (
    "rest_framework",
    "django_filters",
    "scrudful.rest_framework",
    "scoped_rbac.rest",
    "scoped_rbac.permissions",
    "scoped_rbac.filters",
    "scoped_rbac.serializers",
    "scoped_rbac.authentication",
    "scoped_rbac.urls",
)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        for blocked in BLOCKED:
            if name == blocked or name.startswith(blocked + "."):
                raise ModuleNotFoundError(f"blocked: {name}", name=name)
        return None

sys.meta_path.insert(0, Blocker())

from django.conf import settings
from tests.settings import SETTINGS

config = dict(SETTINGS)
config["INSTALLED_APPS"] = tuple(
    app for app in config["INSTALLED_APPS"] if not app.startswith("rest_framework")
)
config.pop("REST_FRAMEWORK", None)
config["SCRUD_UI_STUDIO_LAYER"] = True  # its packaged stylesheet is served
config["ROOT_URLCONF"] = "tests.no_drf_urls"
config["DATABASES"] = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
}
settings.configure(**config)

import django

django.setup()

from django.core.management import call_command

call_command("migrate", verbosity=0)

from django.test.utils import setup_test_environment

setup_test_environment()
"""


def run_without_drf(body: str) -> subprocess.CompletedProcess:
    """Run `body` (Python source) after the prelude; the caller asserts on
    ``returncode`` and ``stderr``."""
    return subprocess.run(
        [sys.executable, "-c", PRELUDE + body],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
