from django.apps import AppConfig, apps
from django.core import checks

CHECK_ID_UI_MISSING = "scrud_django_ui_studio.E001"


def check_ui_installed(app_configs=None, **kwargs):
    """An Error (``scrud_django_ui_studio.E001``) when `scrud_django.ui`,
    which the studio builds on, is not installed."""
    if apps.is_installed("scrud_django.ui"):
        return []
    return [
        checks.Error(
            "scrud_django.ui.studio requires scrud_django.ui.",
            hint="Add 'scrud_django.ui' to INSTALLED_APPS.",
            id=CHECK_ID_UI_MISSING,
        )
    ]


class ScrudUIStudioConfig(AppConfig):
    name = "scrud_django.ui.studio"
    label = "scrud_django_ui_studio"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        checks.register(check_ui_installed)
