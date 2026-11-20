"""pyproject.toml's dependency sets: ninja in the base install, DRF an extra."""

import tomllib
from pathlib import Path

PYPROJECT = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
PROJECT = PYPROJECT["project"]

DRF_EXTRA = [
    "django-scoped-rbac[drf]>=0.12.1,<0.13",
    "scrudful[drf]>=0.10.1,<0.11",
    "djangorestframework>=3.15",
    "django-filter>=25.1",
]


def test_base_install_is_ninja_without_drf():
    assert sorted(PROJECT["dependencies"]) == sorted(
        [
            "django>=5.2,<6.0",
            "django-ninja>=1.7.0,<1.7.4",
            "django-scoped-rbac[ninja]>=0.12.1,<0.13",
            "scrudful[ninja]>=0.10.1,<0.11",
            "django-reversion>=5.1",
            "jsonpath-ng>=1.6",
            "fastjsonschema>=2.19",
        ]
    )


def test_drf_extra():
    assert sorted(PROJECT["optional-dependencies"]["drf"]) == sorted(DRF_EXTRA)


def test_ninja_extra_is_an_empty_alias():
    assert PROJECT["optional-dependencies"]["ninja"] == []


def test_dev_group_installs_the_drf_extra():
    assert "scrud-django[search,ui,drf]" in PYPROJECT["dependency-groups"]["dev"]
