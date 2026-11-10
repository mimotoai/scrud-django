from django.apps import AppConfig
from django.core import checks


class ScrudUIConfig(AppConfig):
    name = "scrud_django.ui"
    label = "scrud_django_ui"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        """Every write of a UI item records its branch's next version
        (`scrud_django.ui.branches.versions.item_written`). Registers the
        check of published branches' prefixes
        (`scrud_django.ui.branches.checks.check_prefixes`) and of the
        packaged UI layers' files (`scrud_django.ui.layers.check_layers`)."""
        from scrud_django.ui.branches.checks import check_prefixes
        from scrud_django.ui.branches.versions import connect_receivers
        from scrud_django.ui.layers import check_layers

        connect_receivers()
        checks.register(check_prefixes, checks.Tags.database)
        checks.register(check_layers)
