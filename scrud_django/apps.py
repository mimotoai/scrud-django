from django.apps import AppConfig

from scrud_django.scrud_signals import ScrudSignalProcessor


class ScrudConfig(AppConfig):
    name = "scrud_django"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from django.core import checks
        from scoped_rbac.conf import register_operator

        from scrud_django.checks import (
            check_default_workflow_policies,
            check_stored_workflow_policies,
            check_write_routing,
        )
        from scrud_django.permissions import authorized_transitions

        register_operator("scrud_workflow", authorized_transitions)
        checks.register(check_default_workflow_policies)
        checks.register(check_stored_workflow_policies, checks.Tags.database)
        checks.register(check_write_routing)

        self.processor = ScrudSignalProcessor()
