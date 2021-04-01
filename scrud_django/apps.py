from django.apps import AppConfig


class ScrudConfig(AppConfig):
    name = "scrud_django"

    def ready(self):
        from scoped_rbac.conf import register_operator
        from scrud_django.permissions import authorized_transitions

        register_operator("scrud_workflow", authorized_transitions)
