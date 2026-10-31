"""READ COMMITTED is the supported transaction isolation level: the database
check ``scrud_django.W002`` warns when the database scrud-django writes
resources to runs its transactions at a higher one."""

from contextlib import contextmanager
from unittest import mock

import pytest
from django.core import checks
from django.db import DatabaseError, connections

from scrud_django import checks as scrud_checks


@contextmanager
def isolation(level):
    with mock.patch.object(scrud_checks, "transaction_isolation", return_value=level):
        yield


def warnings_for(databases):
    return scrud_checks.check_isolation_level(None, databases=databases)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "level", ["repeatable read", "serializable", "read uncommitted"]
)
def test_an_isolation_level_other_than_read_committed_is_a_warning(level):
    with isolation(level):
        found = warnings_for(["default"])
        assert [(m.id, m.level) for m in found] == [
            ("scrud_django.W002", checks.WARNING)
        ]
        if level == "read uncommitted":
            consequence = (
                "On MySQL or MariaDB the workflow check and the WorkflowPolicy "
                "uniqueness check can read a policy whose write is later rolled "
                "back."
            )
        else:
            consequence = (
                "Concurrent writes of one resource, or of WorkflowPolicy "
                "resources, can fail with a serialization error (a 500) instead "
                "of a 412 or 400; on PostgreSQL at REPEATABLE READ two POSTs to "
                "a list with the same If-Match can both create, with no error; "
                "and on MySQL or MariaDB two WorkflowPolicy resources can be "
                "stored for one type and context."
            )
        assert found[0].msg == (
            f"Transactions on database 'default', where scrud-django writes "
            f"resources, run at {level.upper()}; scrud-django supports READ "
            f"COMMITTED. {consequence}"
        )
        # A database check: `check --database` and `migrate` run it.
        assert found[0] in checks.run_checks(databases=["default"])
        assert found[0] not in checks.run_checks()


@pytest.mark.django_db
@pytest.mark.parametrize("level", ["read committed", None])
def test_read_committed_and_unknown_levels_pass(level):
    with isolation(level):
        assert warnings_for(["default"]) == []


@pytest.mark.django_db
def test_only_the_resource_write_database_is_checked():
    with isolation("serializable"):
        assert warnings_for(["other"]) == []
        assert warnings_for(None) == []


@pytest.mark.django_db
def test_sqlite_is_not_asked():
    if connections["default"].vendor != "sqlite":
        pytest.skip("SQLite only")
    assert scrud_checks.transaction_isolation("default") is None


class FakeCursor:
    def __init__(self, results):
        self.results = results
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql not in self.results:
            return  # The atomic block's savepoint statements.
        result = self.results[sql]
        if isinstance(result, Exception):
            raise result
        self.row = result

    def fetchone(self):
        return self.row


@pytest.mark.django_db
@pytest.mark.parametrize(
    "vendor, results, expected",
    [
        (
            "postgresql",
            {"SHOW transaction_isolation": ("repeatable read",)},
            "repeatable read",
        ),
        (
            "mysql",
            {"SELECT @@SESSION.transaction_isolation": ("REPEATABLE-READ",)},
            "repeatable read",
        ),
        (
            "mysql",
            {
                "SELECT @@SESSION.transaction_isolation": DatabaseError("unknown"),
                "SELECT @@SESSION.tx_isolation": ("READ-COMMITTED",),
            },
            "read committed",
        ),
        (
            "mysql",
            {
                "SELECT @@SESSION.transaction_isolation": DatabaseError("unknown"),
                "SELECT @@SESSION.tx_isolation": DatabaseError("unknown"),
            },
            None,
        ),
    ],
)
def test_the_level_is_read_from_the_connection(vendor, results, expected):
    connection = connections["default"]
    with (
        mock.patch.object(connection, "vendor", vendor),
        mock.patch.object(connection, "cursor", lambda: FakeCursor(results)),
    ):
        assert scrud_checks.transaction_isolation("default") == expected


@pytest.mark.django_db
def test_a_server_at_read_committed_passes():
    # PostgreSQL's default; on MySQL and MariaDB, Django's (the test
    # settings also name it in OPTIONS["isolation_level"]).
    if connections["default"].vendor == "sqlite":
        pytest.skip("PostgreSQL, MySQL and MariaDB")
    assert scrud_checks.transaction_isolation("default") == "read committed"
    assert warnings_for(["default"]) == []


@pytest.mark.django_db
def test_mysql_at_repeatable_read_is_a_warning():
    # The server's own default, which a connection gets with
    # OPTIONS["isolation_level"] set to None.
    connection = connections["default"]
    if connection.vendor != "mysql":
        pytest.skip("MySQL and MariaDB")
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    try:
        assert scrud_checks.transaction_isolation("default") == "repeatable read"
        assert [m.id for m in warnings_for(["default"])] == ["scrud_django.W002"]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
