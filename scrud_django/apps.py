from django.apps import AppConfig

from scrud_django.scrud_signals import ScrudSignalProcessor


class ScrudConfig(AppConfig):
    name = "scrud_django"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from django.core import checks
        from scoped_rbac.conf import register_operator

        from scrud_django.checks import (
            check_context_drift,
            check_default_workflow_policies,
            check_isolation_level,
            check_list_filters,
            check_overlong_contexts,
            check_stored_workflow_policies,
            check_write_routing,
        )
        from scrud_django.models.resource import mark_reversion_reverts
        from scrud_django.permissions import authorized_transitions

        register_operator("scrud_workflow", authorized_transitions)
        mark_reversion_reverts()
        checks.register(check_default_workflow_policies)
        checks.register(check_stored_workflow_policies, checks.Tags.database)
        checks.register(check_overlong_contexts, checks.Tags.database)
        checks.register(check_context_drift, checks.Tags.database)
        checks.register(check_write_routing)
        checks.register(check_isolation_level, checks.Tags.database)
        checks.register(check_list_filters, checks.Tags.urls)

        self.processor = ScrudSignalProcessor()
