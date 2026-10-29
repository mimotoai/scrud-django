"""``scrud_django.__version__`` without the distribution's metadata (a source
tree on ``PYTHONPATH`` that is not installed)."""

import subprocess
import sys
from pathlib import Path

import scrud_django

ROOT = Path(__file__).resolve().parent.parent

# Import scrud_django with ``importlib.metadata`` knowing no "scrud-django"
# distribution, as for an uninstalled checkout.
WITHOUT_METADATA = """
import importlib.metadata as metadata

found = metadata.version

def version(name):
    if name == "scrud-django":
        raise metadata.PackageNotFoundError(name)
    return found(name)

metadata.version = version
import scrud_django
print(scrud_django.__version__)
"""


def test_the_version_is_the_installed_distributions():
    from importlib.metadata import version

    assert scrud_django.__version__ == version("scrud-django")


def test_the_version_without_metadata_is_unknown():
    result = subprocess.run(
        [sys.executable, "-c", WITHOUT_METADATA],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0+unknown"
