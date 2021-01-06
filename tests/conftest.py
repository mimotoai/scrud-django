import importlib.resources
import os
import sys

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--no-pkgroot",
        action="store_true",
        default=False,
        help="Remove package root directory from sys.path, ensuring that "
        "rest_framework is imported from the installed site-packages. "
        "Used for testing the distribution.",
    )


def pytest_configure(config):
    from django.conf import settings

    settings.configure(
        DEBUG_PROPAGATE_EXCEPTIONS=True,
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }  # noqa: E231
        },
        SITE_ID=1,
        SECRET_KEY="not very secret in tests",
        USE_I18N=True,
        USE_L10N=True,
        STATIC_URL="/static/",
        ROOT_URLCONF="tests.urls",
        TEMPLATE_LOADERS=(
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ),
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
            "rest_framework",
            "rest_framework.authtoken",
            "scoped_rbac",
            "scrud_django",
            "tests",
        ),
        PASSWORD_HASHERS=(
            "django.contrib.auth.hashers.SHA1PasswordHasher",
            "django.contrib.auth.hashers.PBKDF2PasswordHasher",
            "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
            "django.contrib.auth.hashers.BCryptPasswordHasher",
            "django.contrib.auth.hashers.MD5PasswordHasher",
            "django.contrib.auth.hashers.CryptPasswordHasher",
        ),
        REST_FRAMEWORK={
            "EXCEPTION_HANDLER": "scrud_django.exceptions.scrud_exception_handler",
            "DEFAULT_PERMISSION_CLASSES": [
                "scoped_rbac.permissions.IsAuthorized",
                # "rest_framework.permissions.IsAdminUser",
            ],
        },
    )

    if config.getoption("--no-pkgroot"):
        sys.path.pop(0)

        # import scoped_rbac before pytest re-adds the package root directory.
        import scrud_django  # noqa

        package_dir = os.path.join(os.getcwd(), "scrud_django")
        assert not scrud_django.__file__.startswith(package_dir)

    try:
        import django  # noqa

        django.setup()
    except AttributeError:
        pass


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Ensure that the partner-profiles resource type is properly initialized for all
    tests.
    """
    from scrud_django.registration import json_resource_type

    with django_db_blocker.unblock():
        json_resource_type(
            resource_type_uri="tests://PartnerProfiles",
            revision="1",
            slug="partner-profiles",
            schema_func=lambda: importlib.resources.read_text(
                "tests.static.json_schema", "PartnerProfile"
            ),
            context_func=lambda: importlib.resources.read_text(
                "tests.static.json_ld", "PartnerProfile"
            ),
        )
