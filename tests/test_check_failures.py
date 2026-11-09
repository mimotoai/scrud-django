"""A database check that fails reading a migrated database warns
``scrud_django.W007`` (naming the check, the database and the error) rather
than passing. On a database that is not migrated the database checks report
nothing, so that ``migrate`` can run; on a migrated one, an error reading it
is a fault (a missing grant, a schema out of step with the migrations) that
the check would otherwise hide."""

import pytest
from django.core import checks
from django.db import ProgrammingError, connections
from django.urls import reverse

from scrud_django import checks as scrud_checks
from scrud_django.models import ResourceType


def scrud_tables_denied(execute, sql, params, many, context):
    """A connection's ``execute_wrapper``: reading scrud_django's tables is
    refused, the rest of the database (its migration table) works."""
    if ResourceType._meta.app_label + "_" in sql:
        raise ProgrammingError("permission denied for table")
    return execute(sql, params, many, context)


def could_not_run(checked, problem, hint=None):
    return checks.Warning(
        f"The {checked} database check could not run on database 'default', "
        f"which is migrated ({problem}): it reports nothing there.",
        hint=hint
        or (
            "Fix the error (a missing grant on scrud_django's tables? a schema "
            "out of step with its migrations?) and run manage.py check "
            "--database default again."
        ),
        id="scrud_django.W007",
    )


DENIED = "ProgrammingError: permission denied for table"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "check, checked",
    [
        (scrud_checks.check_default_policy_conditions, "scrud_django.W005"),
        (scrud_checks.check_workflow_action_types, "scrud_django.W006"),
        (
            scrud_checks.check_stored_workflow_policies,
            "scrud_django.E002, E003, E005 and W001",
        ),
        (scrud_checks.check_overlong_contexts, "scrud_django.W003"),
        (scrud_checks.check_context_drift, "scrud_django.W004"),
    ],
    ids=["W005", "W006", "stored policies", "W003", "W004"],
)
def test_a_database_check_that_cannot_read_a_migrated_database_warns(check, checked):
    reverse("todos-list")  # Registers the test types first.
    assert check(None, databases=["default"]) == []
    with connections["default"].execute_wrapper(scrud_tables_denied):
        found = check(None, databases=["default"])
    assert found == [could_not_run(checked, DENIED)]
    # Not run on a database it is not asked about.
    assert check(None) == []


@pytest.mark.django_db
def test_the_workflow_action_types_check_warns_when_the_urlconf_fails(
    monkeypatch,
):
    import django.urls

    def failing_resolver():
        raise ImportError("No module named 'myapp.urls'")

    monkeypatch.setattr(django.urls, "get_resolver", failing_resolver)
    found = scrud_checks.check_workflow_action_types(None, databases=["default"])
    assert found == [
        could_not_run(
            "scrud_django.W006",
            "the URLconf could not be imported: ImportError: No module named "
            "'myapp.urls'",
            hint=(
                "Fix the URLconf (ROOT_URLCONF), which registers the resource "
                "types, and run manage.py check --database default again."
            ),
        )
    ]
