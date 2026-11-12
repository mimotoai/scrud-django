import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "scrud_django" / "core"

# Nothing under scrud_django/core/ imports these (the DRF adapters' own).
DENIED = (
    "rest_framework",
    "django_filters",
    "scrudful.rest_framework",
    "scoped_rbac.rest",
    "scoped_rbac.filters",
    "scoped_rbac.permissions",
)

SCRIPT = """
import importlib.abc, pkgutil, sys

BLOCKED = ("rest_framework", "django_filters", "scrudful.rest_framework", "scoped_rbac.rest")

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        for blocked in BLOCKED:
            if name == blocked or name.startswith(blocked + "."):
                raise ModuleNotFoundError(f"blocked: {name}", name=name)
        return None

sys.meta_path.insert(0, Blocker())

from django.conf import settings

settings.configure(
    INSTALLED_APPS=[
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "reversion",
        "scoped_rbac",
        "scrud_django",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
import django

django.setup()

import importlib
import scrud_django.core

for name in ("scrud_django.models", "scrud_django.workflow", "scrud_django.exceptions"):
    importlib.import_module(name)
for info in pkgutil.walk_packages(scrud_django.core.__path__, "scrud_django.core."):
    importlib.import_module(info.name)
"""


def test_setup_and_core_import_without_drf():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_core_modules_import_no_drf():
    offenders = []
    for path in CORE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base] + [f"{base}.{alias.name}" for alias in node.names]
            else:
                continue
            for name in names:
                if any(name == d or name.startswith(d + ".") for d in DENIED):
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders
