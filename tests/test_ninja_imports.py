import importlib
import subprocess
import sys
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = """
import importlib.abc, sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "ninja" or name.startswith("ninja."):
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
"""


def run(body, script=SCRIPT):
    return subprocess.run(
        [sys.executable, "-c", script + body],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_ninja_package_imports():
    importlib.import_module("scrud_django.ninja")
    import ninja

    assert Version("1.7.0") <= Version(ninja.__version__) < Version("1.7.4")


def test_ninja_package_names_the_extra_without_ninja():
    result = run(
        """
try:
    import scrud_django.ninja
except ImportError as error:
    assert "scrud-django[ninja]" in str(error), str(error)
else:
    raise SystemExit("import succeeded")
"""
    )
    assert result.returncode == 0, result.stderr


def test_models_import_without_ninja():
    result = run("import scrud_django.models\n")
    assert result.returncode == 0, result.stderr


def test_a_ninja_that_fails_to_import_raises_its_own_error():
    # Installed, but broken: not "needs django-ninja".
    script = SCRIPT.replace(
        'raise ModuleNotFoundError(f"blocked: {name}", name=name)',
        'raise ImportError(f"ninja is broken: {name}", name=name)',
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script
            + """
try:
    import scrud_django.ninja
except ImportError as error:
    assert "ninja is broken" in str(error), str(error)
    assert "scrud-django[ninja]" not in str(error), str(error)
else:
    raise SystemExit("import succeeded")
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_a_missing_dependency_of_ninja_raises_its_own_error():
    # django-ninja is installed, but one of its own dependencies is missing:
    # a ModuleNotFoundError naming pydantic, not ninja, re-raised unchanged.
    script = SCRIPT.replace(
        'if name == "ninja" or name.startswith("ninja."):',
        'if name == "pydantic" or name.startswith("pydantic."):',
    ).replace(
        'raise ModuleNotFoundError(f"blocked: {name}", name=name)',
        'raise ModuleNotFoundError(f"No module named {name!r}", name=name)',
    )
    assert script != SCRIPT
    result = run(
        """
assert "pydantic" not in sys.modules and "ninja" not in sys.modules
try:
    import scrud_django.ninja
except ImportError as error:
    assert type(error) is ModuleNotFoundError, repr(error)
    assert error.name == "pydantic", repr(error)
    assert str(error) == "No module named 'pydantic'", str(error)
    assert error.__cause__ is None, repr(error.__cause__)
else:
    raise SystemExit("import succeeded")
""",
        script=script,
    )
    assert result.returncode == 0, result.stderr
