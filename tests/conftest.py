import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--reverse",
        action="store_true",
        help="Run the tests in reverse order (the modules, and the tests in "
        "each), to show that no test depends on one that runs before it.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--reverse"):
        items.reverse()


def pytest_configure(config):
    import django
    from django.conf import settings

    from tests.settings import SETTINGS

    settings.configure(**SETTINGS)
    django.setup()

    # Importing the steps module registers them; it imports Django models, so it
    # must happen after django.setup().
    import tests.steps  # noqa: F401


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Ensure that the partner-profiles resource type is properly initialized for all
    tests.

    The built-in WorkflowPolicy type is registered here too: otherwise it is only
    created when ``scrud_django.urls`` is first imported, inside whichever test
    first resolves a URL, and that test's rollback removes it (and its JSON-LD
    context document) for every later test.
    """
    from scrud_django import registration
    from tests import resource_types

    with django_db_blocker.unblock():
        resource_types.json_resource_types()
        registration.workflow_policy_resource_type()
