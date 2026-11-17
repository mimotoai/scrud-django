"""Running without Django REST framework.

`block_drf` installs an import blocker that refuses ``rest_framework``,
``django_filters``, ``scrudful.rest_framework`` and scoped-rbac's DRF modules
(not ninja), as if DRF were not installed: ``tests/conftest.py`` calls it
when ``SCRUD_TEST_NO_DRF=1`` (the suite without DRF, CI's ``test-no-drf``),
before anything imports DRF. This module imports nothing from Django, so
that it can run first.

`run_without_drf` is a subprocess harness: Python with the blocker, the
settings ``tests.settings.without_drf(SETTINGS)`` (no ``rest_framework*``
apps, no ``REST_FRAMEWORK``) with the URLconf ``tests.no_drf_project_urls``
(``scrud_django.urls`` and the UI), and the in-memory database migrated,
then a script body.
"""

import importlib
import importlib.abc
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The modules (and their submodules) the blocker refuses.
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


def is_blocked(name: str) -> bool:
    """Whether the module `name` is one of `BLOCKED` or in one."""
    return any(name == blocked or name.startswith(blocked + ".") for blocked in BLOCKED)


class Blocker(importlib.abc.MetaPathFinder):
    """Refuses the `BLOCKED` modules with the ``ModuleNotFoundError`` (its
    ``name`` the module's) of a missing one."""

    def find_spec(self, name, path=None, target=None):
        if is_blocked(name):
            raise ModuleNotFoundError(f"blocked: {name}", name=name)
        return None


def block_drf():
    """Install a `Blocker` first on ``sys.meta_path`` (once), and check that
    no blocked module was imported before it and that DRF and django-filter
    no longer import."""
    imported = sorted(name for name in sys.modules if is_blocked(name))
    if imported:
        raise RuntimeError(f"imported before the DRF blocker: {imported}")
    if not any(isinstance(finder, Blocker) for finder in sys.meta_path):
        sys.meta_path.insert(0, Blocker())
    for blocked in ("rest_framework", "django_filters"):
        try:
            importlib.import_module(blocked)
        except ModuleNotFoundError:
            pass
        else:
            raise RuntimeError(f"the blocker let {blocked} import")


PRELUDE = """
import importlib, sys, tempfile

from tests.no_drf import block_drf

block_drf()

from django.conf import settings
from tests.settings import SETTINGS, without_drf

config = without_drf(SETTINGS)
config["ROOT_URLCONF"] = "tests.no_drf_project_urls"
config["SCRUD_UI_STUDIO_LAYER"] = True  # its packaged stylesheet is served
config["DATABASES"] = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
}
# Never the shared tests/whoosh_index.
config["HAYSTACK_CONNECTIONS"] = {
    "default": {
        "ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
        "PATH": tempfile.mkdtemp(),
    },
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
