"""Server-rendered UI components, stored as resources. Enabled by adding
``scrud_django.ui`` to ``INSTALLED_APPS``."""

from django.apps import apps as django_apps

UI_APP_NAME = "scrud_django.ui"


def is_enabled() -> bool:
    """Whether the project installed ``scrud_django.ui``."""
    return django_apps.is_installed(UI_APP_NAME)
