from django.conf import settings
from rest_framework.settings import import_from_string


DEFAULTS = {
    "DEFAULT_WORKFLOW_POLICIES": {}
}


def workflow_policy_settings():
    return getattr(settings, "SCRUD_WORKFLOW", dict())


def import_policy_or_func(setting):
    default = DEFAULTS[setting]
    policy_or_func = workflow_policy_settings().get(setting, default)
    if isinstance(policy_or_func, str):
        policy_or_func = import_from_string(policy_or_func, setting)
        if callable(policy_or_func):
            policy_or_func = policy_or_func()
    return policy_or_func


def default_workflow_policies():
    return import_policy_or_func("DEFAULT_WORKFLOW_POLICIES")