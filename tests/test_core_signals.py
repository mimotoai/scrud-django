from scrud_django import scrud_signals
from scrud_django.core import signals


def test_the_old_module_names_the_core_signals():
    for name in (
        "scrud_post_save",
        "scrud_post_delete",
        "write_alias",
        "action_name",
        "WorkflowActionCall",
        "ScrudSignalProcessor",
    ):
        assert getattr(scrud_signals, name) is getattr(signals, name), name


def test_the_actions_logger_name_is_unchanged():
    # WorkflowActionCall logs on this name; the README documents it.
    assert signals.logger.name == "scrud_django.workflow_actions"
