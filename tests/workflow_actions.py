def todo_on_save(sender, instance, **kwargs):
    return True


def todo_on_delete(sender, instance, **kwargs):
    return True


actions = {"tests://ToDo": {"on_save": todo_on_save, "on_delete": todo_on_delete}}

# Misconfigured settings, for the system checks
# (tests/test_workflow_actions_setting.py).
not_a_mapping = {"tests://ToDo": todo_on_save}
misspelled = {"tests://ToDo": {"on_saved": todo_on_save, "on_delete": todo_on_delete}}
not_callable = {"tests://ToDo": {"on_save": "tests.workflow_actions.todo_on_save"}}
none_is_no_action = {"tests://ToDo": {"on_save": todo_on_save, "on_delete": None}}
unregistered = {"tests://NoSuchType": {"on_save": todo_on_save}}
