"""``SCRUD_WORKFLOW_ACTIONS`` may be any sequence of dotted paths (a list or a
tuple, as Django settings often are); any other shape is refused with
``ImproperlyConfigured`` when the actions are loaded (at startup), not with a
500 on every write. The entries are checked by system checks
(``scrud_django.E007``-``E009``, ``W006``), and a bad one never fails a write."""

import logging

import pytest
from django.apps import apps
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse

from scrud_django.checks import check_workflow_action_types, check_workflow_actions
from scrud_django.conf import workflow_actions
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import (
    ScrudSignalProcessor,
)

from .workflow_actions import actions, todo_on_delete, todo_on_save

PATH = "tests.workflow_actions.actions"
TODO = "tests://ToDo"


@pytest.fixture
def processor():
    """A `ScrudSignalProcessor` built under the test's settings; the app's
    processor, which it replaced, is connected again afterwards."""
    yield ScrudSignalProcessor
    apps.get_app_config("scrud_django").processor.connect()


@pytest.mark.parametrize("setting", [[PATH], (PATH,)])
def test_a_list_or_a_tuple_of_paths_loads_the_actions(setting, processor):
    with override_settings(SCRUD_WORKFLOW_ACTIONS=setting):
        assert workflow_actions() == actions
        built = processor()
        assert built.determine_action("on_save", "tests://ToDo") is todo_on_save
        assert built.determine_action("on_delete", "tests://ToDo") is todo_on_delete
        assert built.determine_action("on_save", "tests://Other") is None


@pytest.mark.parametrize("setting", [[], ()])
def test_an_empty_sequence_has_no_actions(setting, processor):
    with override_settings(SCRUD_WORKFLOW_ACTIONS=setting):
        assert workflow_actions() == {}
        built = processor()
        assert built.determine_action("on_save", "tests://ToDo") is None
    # A processor reads the setting as it is now, not as it was built.
    assert built.determine_action("on_save", "tests://ToDo") is todo_on_save


@pytest.mark.parametrize(
    "setting",
    [PATH, {PATH: True}, None, {PATH}, 3],
    ids=["a string", "a mapping", "None", "a set", "a number"],
)
def test_another_shape_is_improperly_configured(setting):
    with override_settings(SCRUD_WORKFLOW_ACTIONS=setting):
        with pytest.raises(ImproperlyConfigured, match="SCRUD_WORKFLOW_ACTIONS"):
            workflow_actions()


def test_a_path_to_something_other_than_a_mapping_is_improperly_configured():
    with override_settings(
        SCRUD_WORKFLOW_ACTIONS=["tests.workflow_actions.todo_on_save"]
    ):
        with pytest.raises(ImproperlyConfigured, match="mapping"):
            workflow_actions()


# The system checks: each per-type entry is a mapping of "on_save" and
# "on_delete" to callables (or None, no action), and its type is registered.


def setting_of(name):
    return override_settings(SCRUD_WORKFLOW_ACTIONS=[f"tests.workflow_actions.{name}"])


def found_by(check, **kwargs):
    return [(m.id, m.level, m.obj) for m in check(None, **kwargs)]


def test_the_test_settings_pass_the_check():
    assert check_workflow_actions(None) == []
    with setting_of("none_is_no_action"):
        assert check_workflow_actions(None) == []


def test_an_entry_that_is_not_a_mapping_is_an_error():
    with setting_of("not_a_mapping"):
        (found,) = check_workflow_actions(None)
    assert (found.id, found.level, found.obj) == (
        "scrud_django.E007",
        checks.ERROR,
        TODO,
    )
    assert "todo_on_save" in found.msg
    assert "on_save" in found.hint and "on_delete" in found.hint


def test_a_setting_that_cannot_be_loaded_is_an_error():
    # ready() refuses it at startup; a setting changed since is reported.
    with override_settings(SCRUD_WORKFLOW_ACTIONS="tests.workflow_actions.actions"):
        assert found_by(check_workflow_actions) == [
            ("scrud_django.E007", checks.ERROR, None)
        ]
    with override_settings(SCRUD_WORKFLOW_ACTIONS=["tests.workflow_actions.nothing"]):
        (found,) = check_workflow_actions(None)
    assert found.id == "scrud_django.E007"
    assert "tests.workflow_actions" in found.msg and "nothing" in found.msg


def test_an_unknown_key_is_an_error_naming_the_allowed_ones():
    with setting_of("misspelled"):
        (found,) = check_workflow_actions(None)
    assert (found.id, found.level, found.obj) == (
        "scrud_django.E008",
        checks.ERROR,
        TODO,
    )
    assert "'on_saved'" in found.msg
    assert "'on_save'" in found.hint and "'on_delete'" in found.hint


def test_a_value_that_is_not_callable_is_an_error():
    with setting_of("not_callable"):
        (found,) = check_workflow_actions(None)
    assert (found.id, found.level, found.obj) == (
        "scrud_django.E009",
        checks.ERROR,
        TODO,
    )
    assert "on_save" in found.msg
    assert "'tests.workflow_actions.todo_on_save'" in found.msg


@pytest.mark.django_db
def test_the_checks_are_registered():
    with setting_of("not_a_mapping"):
        assert "scrud_django.E007" in [m.id for m in checks.run_checks()]


@pytest.mark.django_db
def test_an_unregistered_type_is_a_warning_of_the_database_check():
    reverse("todos-list")  # Registers the test types.
    assert check_workflow_action_types(None, databases=["default"]) == []
    with setting_of("unregistered"):
        found = check_workflow_action_types(None, databases=["default"])
        assert [(m.id, m.level, m.obj) for m in found] == [
            ("scrud_django.W006", checks.WARNING, "tests://NoSuchType")
        ]
        # A database check: `check --database` and `migrate` run it.
        assert found[0] in checks.run_checks(databases=["default"])
        assert found[0] not in checks.run_checks()
        # Only the database resource types are written to is asked.
        assert check_workflow_action_types(None, databases=["other"]) == []
        assert check_workflow_action_types(None) == []


# A bad entry never fails a write: the write stands, no action runs, and the
# problem is logged on the scrud_django.workflow_actions logger.


def logged_on_actions_logger(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "scrud_django.workflow_actions" and r.levelno == logging.ERROR
    ]


@pytest.mark.parametrize(
    "entry, problem",
    [
        (todo_on_save, "not a mapping"),
        ({"on_save": "tests.workflow_actions.todo_on_save"}, "not callable"),
    ],
    ids=["not a mapping", "not callable"],
)
def test_determine_action_skips_a_bad_entry_and_logs_it(
    entry, problem, processor, caplog
):
    built = processor()
    built.workflow_actions = {TODO: entry}
    assert built.determine_action("on_save", TODO) is None
    (record,) = logged_on_actions_logger(caplog)
    assert TODO in record.getMessage()
    assert problem in record.getMessage()
    # A missing key or None is no action, and nothing to log.
    built.workflow_actions = {TODO: {"on_save": todo_on_save, "on_delete": None}}
    assert built.determine_action("on_delete", TODO) is None
    assert built.determine_action("on_save", "tests://Other") is None
    assert len(logged_on_actions_logger(caplog)) == 1


@pytest.mark.django_db
def test_a_write_of_a_type_whose_entry_is_not_a_mapping_stands(
    monkeypatch, caplog, django_capture_on_commit_callbacks
):
    reverse("todos-list")
    app_processor = apps.get_app_config("scrud_django").processor
    monkeypatch.setitem(app_processor.workflow_actions, TODO, todo_on_save)
    registered = []
    monkeypatch.setattr(
        app_processor, "run_on_commit", lambda *args, **kwargs: registered.append(args)
    )
    with django_capture_on_commit_callbacks(execute=True):
        todo = ResourceRegistration.register({"title": "t"}, "todos")
        ResourceRegistration.delete("todos", str(todo.pk))
    assert not Resource.objects.filter(pk=todo.pk).exists()
    assert registered == []
    # Once per write, naming the type.
    records = logged_on_actions_logger(caplog)
    assert len(records) == 2
    assert all(TODO in r.getMessage() for r in records)


# The app's processor follows the setting (override_settings included), and
# one processor receives the signals at a time: building another replaces it.


@pytest.fixture
def todo_actions_run(monkeypatch):
    """The ToDo actions of ``tests.workflow_actions.actions`` that ran:
    ``("save", pk)`` and ``("delete", pk)``. The app's processor is connected
    again afterwards, whichever processor a test built."""
    calls = []

    def on_save(sender, instance, **kwargs):
        calls.append(("save", instance.pk))

    def on_delete(sender, instance, pk=None, **kwargs):
        calls.append(("delete", pk))

    monkeypatch.setitem(actions[TODO], "on_save", on_save)
    monkeypatch.setitem(actions[TODO], "on_delete", on_delete)
    yield calls
    apps.get_app_config("scrud_django").processor.connect()


def save_and_delete_a_todo(django_capture_on_commit_callbacks):
    reverse("todos-list")
    with django_capture_on_commit_callbacks(execute=True):
        todo = ResourceRegistration.register({"title": "t"}, "todos")
        ResourceRegistration.delete("todos", str(todo.pk))
    return todo.pk


@pytest.mark.django_db
def test_the_app_processor_follows_override_settings(
    todo_actions_run, django_capture_on_commit_callbacks
):
    app_processor = apps.get_app_config("scrud_django").processor
    # on_delete is None there: no action.
    with setting_of("none_is_no_action"):
        assert app_processor.determine_action("on_delete", TODO) is None
        pk = save_and_delete_a_todo(django_capture_on_commit_callbacks)
    assert todo_actions_run == []
    with setting_of("unregistered"):
        assert app_processor.determine_action("on_save", TODO) is None
    # The test settings again.
    pk = save_and_delete_a_todo(django_capture_on_commit_callbacks)
    assert todo_actions_run == [("save", pk), ("delete", pk)]


@pytest.mark.django_db
def test_a_setting_changed_to_one_that_cannot_be_loaded_fails_no_write(
    todo_actions_run, caplog, django_capture_on_commit_callbacks
):
    with override_settings(SCRUD_WORKFLOW_ACTIONS=["tests.workflow_actions.nothing"]):
        pk = save_and_delete_a_todo(django_capture_on_commit_callbacks)
    assert not Resource.objects.filter(pk=pk).exists()
    assert todo_actions_run == []
    records = logged_on_actions_logger(caplog)
    assert len(records) == 2
    assert all("SCRUD_WORKFLOW_ACTIONS" in r.getMessage() for r in records)


@pytest.mark.django_db
@pytest.mark.parametrize("keep", [True, False], ids=["kept", "unreferenced"])
def test_a_second_processor_replaces_the_first(
    keep, todo_actions_run, django_capture_on_commit_callbacks
):
    import gc

    second = ScrudSignalProcessor()
    if not keep:
        # Held by the signals: not collected, and still delivering.
        del second
        gc.collect()
    pk = save_and_delete_a_todo(django_capture_on_commit_callbacks)
    # Once each, not once per processor.
    assert todo_actions_run == [("save", pk), ("delete", pk)]


@pytest.mark.django_db
def test_a_subclass_instance_replaces_the_default_processor(
    todo_actions_run, django_capture_on_commit_callbacks
):
    received = []

    class Recording(ScrudSignalProcessor):
        def post_save_trigger(self, sender, **kwargs):
            received.append("save")
            super().post_save_trigger(sender, **kwargs)

    Recording()
    pk = save_and_delete_a_todo(django_capture_on_commit_callbacks)
    assert received == ["save"]
    assert todo_actions_run == [("save", pk), ("delete", pk)]
    # The app's processor connected again: the subclass receives no more.
    apps.get_app_config("scrud_django").processor.connect()
    received.clear()
    todo_actions_run.clear()
    pk = save_and_delete_a_todo(django_capture_on_commit_callbacks)
    assert received == []
    assert todo_actions_run == [("save", pk), ("delete", pk)]
