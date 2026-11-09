"""A database check that fails reading a migrated database warns
``scrud_django.W007`` (naming the check, the database and the error) rather
than passing. On a database that is not migrated the database checks report
nothing, so that ``migrate`` can run; on a migrated one, an error reading it
is a fault (a missing grant, a schema out of step with the migrations) that
the check would otherwise hide."""

import pytest
from django.core import checks
from django.db import ProgrammingError, connections
from django.test import override_settings
from django.urls import reverse

from scrud_django import checks as scrud_checks
from scrud_django.models import ResourceType

from .fixtures import unapply_a_scrud_migration


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

# Every database check of scrud_django, with the IDs its W007 names.
DATABASE_CHECKS = pytest.mark.parametrize(
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


@pytest.mark.django_db
@DATABASE_CHECKS
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


@pytest.mark.django_db
@DATABASE_CHECKS
def test_a_database_check_reports_nothing_on_a_database_not_migrated(check, checked):
    # Before migrate (of an upgrade) the tables may not all be there, or
    # not be readable: the checks report nothing, so that migrate can run,
    # even when reading the tables fails.
    reverse("todos-list")
    unapply_a_scrud_migration("default")
    with connections["default"].execute_wrapper(scrud_tables_denied):
        assert check(None, databases=["default"]) == []


class ScrudDjangoOnDefaultRouter:
    """scrud_django's tables are migrated on "default" only: an alias such as
    "other" (an analytics database) holds none of them, though Django
    records scrud_django's migrations as applied there too."""

    def allow_migrate(self, db, app_label, **hints):
        if app_label == "scrud_django":
            return db == "default"
        return None


@pytest.mark.django_db(databases=["default", "other"])
@DATABASE_CHECKS
def test_a_database_check_skips_a_database_without_scrud_djangos_tables(check, checked):
    reverse("todos-list")
    # The test's "other" database has the tables (it was migrated without
    # the router); refusing them stands for their absence. Without the
    # router, that is a fault on a migrated database.
    with connections["other"].execute_wrapper(scrud_tables_denied):
        found = check(None, databases=["other"])
    if checked != "scrud_django.W006":  # W006 checks ResourceType's database.
        assert [warning.id for warning in found] == ["scrud_django.W007"], found
        assert "database 'other'" in found[0].msg, found[0].msg
    # With it, "other" legitimately holds nothing of scrud_django's: nothing
    # is reported there, and "default" is still checked.
    with (
        override_settings(DATABASE_ROUTERS=[ScrudDjangoOnDefaultRouter()]),
        connections["other"].execute_wrapper(scrud_tables_denied),
    ):
        assert check(None, databases=["other"]) == []
        assert check(None, databases=["default", "other"]) == []
        with connections["default"].execute_wrapper(scrud_tables_denied):
            found = check(None, databases=["default", "other"])
    assert found == [could_not_run(checked, DENIED)]
