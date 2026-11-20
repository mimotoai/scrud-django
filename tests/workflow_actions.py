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
