"""The DRF adapter's package, `scrud_django.drf`, and the deprecated aliases
its old import paths keep for one release: each alias is the moved object
itself and warns once (``DeprecationWarning``), naming the new path."""

import importlib
import subprocess
import sys
import warnings

import pytest
from django.test import override_settings
from rest_framework.settings import api_settings

from .no_drf import ROOT, run_without_drf

# Importing a URLconf registers resource types in the database.
pytestmark = pytest.mark.django_db

NEW_MODULES = [
    "scrud_django.drf",
    "scrud_django.drf.auth",
    "scrud_django.drf.computed",
    "scrud_django.drf.exceptions",
    "scrud_django.drf.filters",
    "scrud_django.drf.mixins",
    "scrud_django.drf.paginations",
    "scrud_django.drf.permissions",
    "scrud_django.drf.renderers",
    "scrud_django.drf.requirements",
    "scrud_django.drf.serializers",
    "scrud_django.drf.ui",
    "scrud_django.drf.urls",
    "scrud_django.drf.validators",
    "scrud_django.drf.views",
]

#: Each old module path, an alias of the module that replaced it.
MODULE_ALIASES = {
    "scrud_django.views": "scrud_django.drf.views",
    "scrud_django.computed": "scrud_django.drf.computed",
    "scrud_django.filters": "scrud_django.drf.filters",
    "scrud_django.paginations": "scrud_django.drf.paginations",
    "scrud_django.permissions": "scrud_django.drf.permissions",
    "scrud_django.renderers": "scrud_django.drf.renderers",
    "scrud_django.serializers": "scrud_django.drf.serializers",
    "scrud_django.drf_requirements": "scrud_django.drf.requirements",
    "scrud_django.mixins.view_mixins": "scrud_django.drf.mixins",
    "scrud_django.mixins.serializer_mixins": "scrud_django.drf.serializers",
    "scrud_django.ui.views": "scrud_django.drf.ui",
    "scrud_django.ui.branches.views": "scrud_django.drf.ui",
}

#: The DRF names of the modules that stay (their neutral names do not warn):
#: (module, name) -> the module the name moved to.
NAME_ALIASES = {
    ("scrud_django.exceptions", "scrud_exception_handler"): (
        "scrud_django.drf.exceptions"
    ),
    ("scrud_django.exceptions", "as_drf"): "scrud_django.drf.exceptions",
    ("scrud_django.exceptions", "drf_errors"): "scrud_django.drf.exceptions",
    ("scrud_django.exceptions", "DOCUMENT_RENDERERS"): "scrud_django.drf.renderers",
    ("scrud_django.validators", "WorkflowValidator"): "scrud_django.drf.validators",
    ("scrud_django.validators", "narrow_of"): "scrud_django.drf.validators",
    ("scrud_django.validators", "view_of"): "scrud_django.drf.validators",
    ("scrud_django.validators", "rbac_permissions_of"): ("scrud_django.drf.validators"),
    ("scrud_django.validators", "drf_error"): "scrud_django.drf.validators",
    ("scrud_django.validators", "type_hook_leaf"): "scrud_django.drf.validators",
    ("scrud_django.validators", "checks_read_content"): ("scrud_django.drf.validators"),
    ("scrud_django.validators", "reading_content"): "scrud_django.drf.validators",
}

#: Names of the modules that stay which are neutral: read without a warning.
NEUTRAL_NAMES = {
    "scrud_django.exceptions": [
        "ScrudException",
        "ScrudAPIException",
        "ErrorText",
        "JsonValidationError",
        "WorkflowTransitionError",
    ],
    "scrud_django.validators": [
        "JsonValidator",
        "compile_json_schema",
        "check_policy_is_unique",
        "prior_of",
        "WorkflowPolicy",
        "WORKFLOW_POLICY_TYPE_URI",
        "HiddenWorkflowTransitionError",
        "content_of",
        "may_read_in_context",
        "_checking_transition",
        "rbac_contexts_of",
        "logger",
    ],
}


def deprecations(caught):
    return [w for w in caught if issubclass(w.category, DeprecationWarning)]


@pytest.mark.parametrize("module", NEW_MODULES)
def test_each_new_module_imports(module):
    assert importlib.import_module(module).__name__ == module


def test_the_new_modules_import_without_a_deprecation_warning():
    # A fresh process, alias warnings errors: no module of the package
    # imports an old path.
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error:scrud_django:DeprecationWarning",
            "-c",
            "from django.conf import settings\n"
            "from tests.settings import SETTINGS\n"
            "settings.configure(**SETTINGS)\n"
            "import django\n"
            "django.setup()\n"
            "import importlib\n"
            f"for module in {NEW_MODULES!r}:\n"
            "    importlib.import_module(module)\n",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def fresh(monkeypatch):
    """Import an old module path anew (its alias module runs again), the
    modules restored afterwards."""

    def fresh_import(name):
        parent_name, _, child = name.rpartition(".")
        parent = importlib.import_module(parent_name)
        if hasattr(parent, child):
            monkeypatch.setattr(parent, child, getattr(parent, child))
        monkeypatch.delitem(sys.modules, name, raising=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            __import__(name)
        return sys.modules[name], deprecations(caught)

    return fresh_import


@pytest.mark.parametrize("old,new", sorted(MODULE_ALIASES.items()))
def test_an_old_module_is_its_new_module_and_warns_once(fresh, old, new):
    module, caught = fresh(old)
    new_module = importlib.import_module(new)
    assert module is new_module
    assert len(caught) == 1, [str(w.message) for w in caught]
    message = str(caught[0].message)
    assert message.startswith(old), message
    assert new in message, message
    # The warning points at the import.
    assert caught[0].filename == __file__
    public = [name for name in vars(new_module) if not name.startswith("_")]
    assert public
    for name in public:
        assert getattr(module, name) is getattr(new_module, name), name


@pytest.mark.parametrize("old,name", sorted(NAME_ALIASES))
def test_a_moved_name_is_its_new_object_and_warns_once(monkeypatch, old, name):
    module = importlib.import_module(old)
    new = importlib.import_module(NAME_ALIASES[old, name])
    monkeypatch.delitem(vars(module), name, raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        exec(f"from {old} import {name} as first", {})
        assert getattr(module, name) is getattr(new, name)
        assert getattr(module, name) is getattr(new, name)
    caught = deprecations(caught)
    assert len(caught) == 1, [str(w.message) for w in caught]
    message = str(caught[0].message)
    assert message.startswith(f"{old}.{name}"), message
    assert f"{new.__name__}.{name}" in message, message


@pytest.mark.parametrize(
    "old,name", [(old, name) for old, names in NEUTRAL_NAMES.items() for name in names]
)
def test_a_neutral_name_that_stays_does_not_warn(old, name):
    module = importlib.import_module(old)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        getattr(module, name)
    assert not deprecations(caught)


def test_an_unknown_name_of_a_module_that_stays_is_an_attribute_error():
    for old in NEUTRAL_NAMES:
        with pytest.raises(AttributeError):
            importlib.import_module(old).no_such_name  # noqa: B018


def test_the_old_exception_handler_setting_resolves_and_warns_at_most_once(
    monkeypatch,
):
    from scrud_django import exceptions
    from scrud_django.drf.exceptions import scrud_exception_handler

    monkeypatch.delitem(vars(exceptions), "scrud_exception_handler", raising=False)
    old = "scrud_django.exceptions.scrud_exception_handler"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(2):
            # DRF reads the setting anew after each change of it.
            with override_settings(REST_FRAMEWORK={"EXCEPTION_HANDLER": old}):
                assert api_settings.EXCEPTION_HANDLER is scrud_exception_handler
    caught = deprecations(caught)
    assert len(caught) == 1, [str(w.message) for w in caught]
    assert "scrud_django.drf.exceptions.scrud_exception_handler" in str(
        caught[0].message
    )


def test_the_drf_package_names_the_extra_without_drf():
    result = run_without_drf(
        """
try:
    import scrud_django.drf
except ImportError as error:
    assert "scrud_django.drf needs scrud-django[drf]" in str(error), str(error)
else:
    raise SystemExit("import succeeded")

# The modules that stay import without DRF; their DRF names do not.
import scrud_django.exceptions, scrud_django.validators
from scrud_django.validators import compile_json_schema, JsonValidator

try:
    from scrud_django.validators import WorkflowValidator
except ImportError as error:
    assert "scrud-django[drf]" in str(error), str(error)
else:
    raise SystemExit("WorkflowValidator imported")
"""
    )
    assert result.returncode == 0, result.stderr
