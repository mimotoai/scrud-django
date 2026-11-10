from django.apps import AppConfig


class ScrudUIConfig(AppConfig):
    name = "scrud_django.ui"
    label = "scrud_django_ui"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        """Every write of a UI item records its branch's next version
        (`scrud_django.ui.branches.versions.item_written`)."""
        from scrud_django.ui.branches.versions import connect_receivers

        connect_receivers()
