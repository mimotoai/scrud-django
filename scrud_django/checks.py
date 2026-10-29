"""Django system checks for scrud-django (registered in ``ScrudConfig.ready``)."""

from django.core import checks

from scrud_django.conf import DefaultWorkflowPolicies

HINT = 'Fix SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"].'


def check_default_workflow_policies(app_configs, **kwargs):
    """An Error (``scrud_django.E001``) for each settings default workflow
    policy that does not pass ``WorkflowPolicy``'s checks, naming its type, or
    for a ``DEFAULT_WORKFLOW_POLICIES`` setting that cannot be loaded. At request
    time such a policy fails the writes of its type with
    ``ImproperlyConfigured``; ``manage.py check`` reports it before that.
    Stored ``WorkflowPolicy`` resources are checked when they are written."""
    defaults = DefaultWorkflowPolicies()
    if defaults.load_error is not None:
        return [
            checks.Error(
                str(defaults.load_error),
                hint=HINT,
                id="scrud_django.E001",
            )
        ]
    return [
        checks.Error(
            f"Invalid default workflow policy for {type_uri!r}: {error}",
            hint=HINT,
            obj=type_uri,
            id="scrud_django.E001",
        )
        for type_uri, error in defaults.errors.items()
    ]
