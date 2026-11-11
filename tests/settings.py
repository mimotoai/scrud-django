"""Single source of test/dev settings.

Used as ``settings.configure(**SETTINGS)`` by both ``tests/conftest.py`` and
``manage.py``.

The databases are in-memory SQLite, unless ``SCRUD_TEST_PG_HOST`` names a
PostgreSQL server (the CI job ``test-postgres``) or ``SCRUD_TEST_MYSQL_HOST``
a MySQL or MariaDB server (``test-mysql``): both aliases then use it, with
``SCRUD_TEST_PG_PORT`` (5432), ``SCRUD_TEST_PG_USER`` (postgres),
``SCRUD_TEST_PG_PASSWORD`` (none) and ``SCRUD_TEST_PG_NAME`` (scrud; the
"other" alias appends ``_other``), or the ``SCRUD_TEST_MYSQL_`` ones
(port 3306, user root, the same defaults otherwise). MySQL connections use
utf8mb4 and READ COMMITTED, as scrud-django requires there; its driver,
mysqlclient, is not a dev dependency (it builds against the MySQL client
library): add it with ``uv run --with mysqlclient==2.3.0``. pytest-django
creates and drops the test databases (``test_`` and those names), in the
server's default character set and collation.
"""

import os
from typing import Any


def database(alias):
    """The settings of database `alias` ("default" or "other")."""
    pg_host = os.environ.get("SCRUD_TEST_PG_HOST")
    mysql_host = os.environ.get("SCRUD_TEST_MYSQL_HOST")
    if pg_host and mysql_host:
        raise RuntimeError("Set SCRUD_TEST_PG_HOST or SCRUD_TEST_MYSQL_HOST, not both.")
    if mysql_host:
        name = os.environ.get("SCRUD_TEST_MYSQL_NAME", "scrud")
        return {
            "ENGINE": "django.db.backends.mysql",
            "HOST": mysql_host,
            "PORT": os.environ.get("SCRUD_TEST_MYSQL_PORT", "3306"),
            "USER": os.environ.get("SCRUD_TEST_MYSQL_USER", "root"),
            "PASSWORD": os.environ.get("SCRUD_TEST_MYSQL_PASSWORD", ""),
            "NAME": name if alias == "default" else f"{name}_{alias}",
            "OPTIONS": {"charset": "utf8mb4", "isolation_level": "read committed"},
        }
    if not pg_host:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    name = os.environ.get("SCRUD_TEST_PG_NAME", "scrud")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": pg_host,
        "PORT": os.environ.get("SCRUD_TEST_PG_PORT", "5432"),
        "USER": os.environ.get("SCRUD_TEST_PG_USER", "postgres"),
        "PASSWORD": os.environ.get("SCRUD_TEST_PG_PASSWORD", ""),
        "NAME": name if alias == "default" else f"{name}_{alias}",
    }


SETTINGS: dict[str, Any] = dict(
    DEBUG_PROPAGATE_EXCEPTIONS=True,
    DATABASES={
        "default": database("default"),
        # Only for tests that route Resource writes to a second alias
        # (``@pytest.mark.django_db(databases=["default", "other"])``);
        # pytest-django creates it only for them.
        "other": database("other"),
    },
    SITE_ID=1,
    SECRET_KEY="not very secret in tests",
    USE_I18N=True,
    USE_TZ=True,
    DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    STATIC_URL="/static/",
    ROOT_URLCONF="tests.urls",
    TEMPLATES=[
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                ],
            },
        }
    ],
    MIDDLEWARE=(
        "django.middleware.common.CommonMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "scrud_django.ui.branches.middleware.PublishedBranchMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ),
    INSTALLED_APPS=(
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.sites",
        "django.contrib.messages",
        "django.contrib.staticfiles",
        "haystack",
        "rest_framework",
        "rest_framework.authtoken",
        "scoped_rbac",
        "scrud_django",
        "scrud_django.ui",
        "scrud_django.ui.studio",
        "tests",
        "tests.apps.UILayerTestConfig",
        "reversion",
    ),
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    REST_FRAMEWORK={
        "EXCEPTION_HANDLER": "scrud_django.exceptions.scrud_exception_handler",
        "DEFAULT_PERMISSION_CLASSES": [
            "scoped_rbac.permissions.IsAuthorized",
        ],
    },
    # The HTTP Schema Resource is built without a request: its relative links
    # resolve against BASE_URL (scrudful 0.10), the test client's host.
    SCRUDFUL={"BASE_URL": "http://testserver"},
    SCOPED_RBAC={
        "POLICY_FOR_UNAUTHENTICATED": "tests.rbac_defaults.policy_for_unauthenticated",
        "POLICY_FOR_STAFF": "tests.rbac_defaults.policy_for_staff",
    },
    SCRUD_WORKFLOW={
        "DEFAULT_WORKFLOW_POLICIES": "tests.workflow_defaults.workflow_policies",
    },
    HAYSTACK_CONNECTIONS={
        "default": {
            "ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
            "PATH": os.path.join(os.path.dirname(__file__), "whoosh_index"),
        },
    },
    HAYSTACK_SIGNAL_PROCESSOR="scrud_django.search.OnCommitSignalProcessor",
    SCRUD_WORKFLOW_ACTIONS=["tests.workflow_actions.actions"],
)
