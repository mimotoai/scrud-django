from collections.abc import Mapping

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string
from rest_framework.settings import import_from_string

DEFAULTS = {"DEFAULT_WORKFLOW_POLICIES": {}}


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


class DefaultWorkflowPolicies:
    """``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``, loaded and checked once.

    The setting is a mapping of type URI to policy, or the dotted path of one
    (or of a function returning one). Give each policy as its content (a
    mapping, for example ``json.loads`` of a file): it is compiled and checked
    here, per type, so an invalid one fails only the writes of its type and is
    reported by the ``scrud_django.E001`` system check. A ``WorkflowPolicy`` is
    accepted too, but a function that builds them eagerly and raises on an
    invalid one cannot be isolated: the whole setting fails to load
    (`load_error`) and every type's default with it.

    A function is called once per process: the result is cached until
    ``SCRUD_WORKFLOW`` changes (``setting_changed``, as ``override_settings``
    sends it). `policies` holds the valid policies; `errors` the
    ``ImproperlyConfigured`` of each type whose policy is invalid, and
    `load_error` the one raised loading the setting itself (then there are no
    policies at all)."""

    def __init__(self):
        from scrud_django.workflow import WorkflowPolicy

        self.policies = {}
        self.errors = {}
        self.load_error = None
        try:
            configured = import_policy_or_func("DEFAULT_WORKFLOW_POLICIES")
        except Exception as e:
            self.load_error = ImproperlyConfigured(
                f'SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"] could not be loaded: {e}'
            )
            self.load_error.__cause__ = e
            return
        if not isinstance(configured, Mapping):
            self.load_error = ImproperlyConfigured(
                'SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"] must be a mapping of '
                f"type URI to workflow policy, not {configured!r}."
            )
            return
        for type_uri, policy in configured.items():
            if isinstance(policy, WorkflowPolicy):
                self.policies[type_uri] = policy
                continue
            try:
                if not isinstance(policy, Mapping):
                    raise ImproperlyConfigured(
                        "a workflow policy must be a mapping or a WorkflowPolicy, "
                        f"not {policy!r}."
                    )
                self.policies[type_uri] = WorkflowPolicy(policy)
            except ImproperlyConfigured as e:
                self.errors[type_uri] = e

    def policy_for(self, type_uri):
        """The default policy for `type_uri`, or ``None``. Raises
        ``ImproperlyConfigured`` when that policy (or the setting) is invalid:
        only the writes it governs fail, not those of other types."""
        if self.load_error is not None:
            raise ImproperlyConfigured(str(self.load_error)) from self.load_error
        error = self.errors.get(type_uri)
        if error is not None:
            raise ImproperlyConfigured(
                f"Invalid default workflow policy for {type_uri!r}: {error}"
            ) from error
        return self.policies.get(type_uri)


_DEFAULT_WORKFLOW_POLICIES = None


def default_workflow_policy_set():
    """The `DefaultWorkflowPolicies`, built on first use and kept until
    ``SCRUD_WORKFLOW`` changes (``setting_changed``, as ``override_settings``
    sends it)."""
    global _DEFAULT_WORKFLOW_POLICIES
    if _DEFAULT_WORKFLOW_POLICIES is None:
        _DEFAULT_WORKFLOW_POLICIES = DefaultWorkflowPolicies()
    return _DEFAULT_WORKFLOW_POLICIES


def default_workflow_policies():
    """Type URI -> ``WorkflowPolicy``: the valid settings default policies,
    built once. Raises ``ImproperlyConfigured`` if the setting cannot be
    loaded."""
    defaults = default_workflow_policy_set()
    if defaults.load_error is not None:
        raise ImproperlyConfigured(str(defaults.load_error)) from defaults.load_error
    return defaults.policies


@receiver(setting_changed)
def reset_default_workflow_policies(setting, **kwargs):
    global _DEFAULT_WORKFLOW_POLICIES
    if setting == "SCRUD_WORKFLOW":
        _DEFAULT_WORKFLOW_POLICIES = None


def workflow_action_settings():
    return getattr(settings, "SCRUD_WORKFLOW_ACTIONS", [])


def workflow_actions():
    all_actions = {}
    actions_strings: list = workflow_action_settings()
    if isinstance(actions_strings, list):
        for a_string in actions_strings:
            actions_by_type_uri: dict = import_string(a_string)
            for type_uri in list(actions_by_type_uri.keys()):
                all_actions[type_uri] = actions_by_type_uri.get(type_uri, None)
        return all_actions
