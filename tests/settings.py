"""Single source of test/dev settings.

Used as ``settings.configure(**SETTINGS)`` by both ``tests/conftest.py`` and
``manage.py``.

The databases are in-memory SQLite, unless ``SCRUD_TEST_PG_HOST`` names a
PostgreSQL server (the CI job ``test-postgres``): both aliases then use it,
with ``SCRUD_TEST_PG_PORT`` (5432), ``SCRUD_TEST_PG_USER`` (postgres),
``SCRUD_TEST_PG_PASSWORD`` (none) and ``SCRUD_TEST_PG_NAME`` (scrud; the
"other" alias appends ``_other``). pytest-django creates and drops the test
databases (``test_`` and those names).
"""

import os
from typing import Any


def database(alias):
    """The settings of database `alias` ("default" or "other")."""
    host = os.environ.get("SCRUD_TEST_PG_HOST")
    if not host:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    name = os.environ.get("SCRUD_TEST_PG_NAME", "scrud")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": host,
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
        "tests",
        "reversion",
    ),
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    REST_FRAMEWORK={
        "EXCEPTION_HANDLER": "scrud_django.exceptions.scrud_exception_handler",
        "DEFAULT_PERMISSION_CLASSES": [
            "scoped_rbac.permissions.IsAuthorized",
        ],
    },
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
