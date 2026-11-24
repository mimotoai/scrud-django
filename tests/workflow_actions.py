from scrud_django.core.signals import acting_user

from .workflow_defaults import ARTICLE


def todo_on_save(sender, instance, **kwargs):
    return True


def todo_on_delete(sender, instance, **kwargs):
    return True


# The calls of ``tests://Article``'s actions in turn, ``(kind, kwargs)`` (the
# signal's arguments but ``sender``); a test that reads it clears it first.
article_calls = []


def article_on_save(sender, **kwargs):
    article_calls.append(("on_save", kwargs))
    return True


def article_on_delete(sender, **kwargs):
    article_calls.append(("on_delete", kwargs))
    return True


actions = {
    "tests://ToDo": {"on_save": todo_on_save, "on_delete": todo_on_delete},
    ARTICLE: {"on_save": article_on_save, "on_delete": article_on_delete},
}

# Misconfigured settings, for the system checks
# (tests/test_workflow_actions_setting.py).
not_a_mapping = {"tests://ToDo": todo_on_save}
misspelled = {"tests://ToDo": {"on_saved": todo_on_save, "on_delete": todo_on_delete}}
not_callable = {"tests://ToDo": {"on_save": "tests.workflow_actions.todo_on_save"}}
none_is_no_action = {"tests://ToDo": {"on_save": todo_on_save, "on_delete": None}}
unregistered = {"tests://NoSuchType": {"on_save": todo_on_save}}
transition_none = {"tests://ToDo": {"on_transition": None}}


# Recorders for tests/test_transition_actions.py: every call in order, as
# ``(kind, kwargs)`` (``on_save``, or the transition's name); a test clears
# it first.
transition_calls = []


def record_on_save(sender, **kwargs):
    transition_calls.append(("on_save", kwargs))


def record_start(sender, **kwargs):
    transition_calls.append(("start", kwargs))


def record_override(sender, **kwargs):
    transition_calls.append(("__OVERRIDE__", kwargs))


def failing_start(sender, **kwargs):
    transition_calls.append(("start", kwargs))
    raise RuntimeError("start failed")


recording = {
    "tests://ToDo": {
        "on_save": record_on_save,
        "on_transition": {"start": record_start, "__OVERRIDE__": record_override},
    }
}
failing = {
    "tests://ToDo": {
        "on_save": record_on_save,
        "on_transition": {"start": failing_start},
    }
}

# The actor (``acting_user()``) each action of ``acting`` sees as it runs, as
# ``(kind, user)``; a test clears it first.
acting_calls = []


def acting_on_save(sender, **kwargs):
    acting_calls.append(("on_save", acting_user()))


def acting_on_delete(sender, **kwargs):
    acting_calls.append(("on_delete", acting_user()))


def acting_start(sender, **kwargs):
    acting_calls.append(("start", acting_user()))


acting = {
    "tests://ToDo": {
        "on_save": acting_on_save,
        "on_delete": acting_on_delete,
        "on_transition": {"start": acting_start},
    }
}

# Misconfigured ``on_transition`` entries, for the system checks.
transition_not_a_mapping = {"tests://ToDo": {"on_transition": record_start}}
transition_key_not_a_string = {"tests://ToDo": {"on_transition": {3: record_start}}}
transition_value_not_callable = {
    "tests://ToDo": {"on_transition": {"start": "tests.workflow_actions.record_start"}}
}
transition_reserved_key = {
    "tests://ToDo": {"on_transition": {"__LEAVE__": record_start}}
}
transition_valid = {
    "tests://ToDo": {
        "on_transition": {
            "start": record_start,
            "__INITIALIZE__": record_start,
            "__SAME_STATE__": None,
            "__OVERRIDE__": record_override,
        }
    }
}
transition_string = {
    "tests://ToDo": {"on_transition": "tests.workflow_actions.record_start"}
}
