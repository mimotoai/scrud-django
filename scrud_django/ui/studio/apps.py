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

    @property
    def scrud_ui_layer(self):
        """The studio's packaged UI layer (`scrud_django.ui.studio.layer`),
        or ``None`` when ``SCRUD_UI_STUDIO_LAYER`` turns it off
        (`studio_layer_enabled`). Read when the layers are
        (`scrud_django.ui.layers`), so a change of the setting takes effect
        without a restart."""
        from scrud_django.ui.studio import studio_layer_enabled

        if not studio_layer_enabled():
            return None
        return {"prefix": "studio:", "package": "scrud_django.ui.studio.layer"}

    def ready(self):
        checks.register(check_ui_installed)
