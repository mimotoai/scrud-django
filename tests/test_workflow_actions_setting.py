"""``SCRUD_WORKFLOW_ACTIONS`` may be any sequence of dotted paths (a list or a
tuple, as Django settings often are); any other shape is refused with
``ImproperlyConfigured`` when the actions are loaded (at startup), not with a
500 on every write."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from scrud_django.conf import workflow_actions
from scrud_django.scrud_signals import (
    ScrudSignalProcessor,
    scrud_post_delete,
    scrud_post_save,
)

from .workflow_actions import actions, todo_on_delete, todo_on_save

PATH = "tests.workflow_actions.actions"


@pytest.fixture
def processor():
    """A `ScrudSignalProcessor` built under the test's settings, its
    receivers disconnected afterwards."""
    built = []

    def build():
        built.append(ScrudSignalProcessor())
        return built[-1]

    yield build
    for p in built:
        scrud_post_save.disconnect(p.post_save_trigger)
        scrud_post_delete.disconnect(p.post_delete_trigger)


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
