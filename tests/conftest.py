import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--reverse",
        action="store_true",
        help="Run the tests in reverse order (the modules, and the tests in "
        "each, within each of pytest-django's groups, which keep their "
        "order), to show that no test depends on one that runs before it.",
    )


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config, items):
    """``--reverse``: reverse the collected tests before pytest-django's
    ordering runs (this wraps it). Its sort is stable, so each of its groups
    (database tests, then transactional ones, which flush the database, then
    the others) runs in reverse, and the groups keep their order: a
    transactional test never runs first."""
    if config.getoption("--reverse"):
        items.reverse()
    return (yield)


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
    from scrud_django.ui import types as ui_types
    from scrud_django.ui.studio.computed import studio_computed_type_urls
    from tests import resource_types

    with django_db_blocker.unblock():
        resource_types.json_resource_types()
        registration.workflow_policy_resource_type()
        ui_types.ui_component_resource_type()
        ui_types.ui_stylesheet_resource_type()
        ui_types.ui_branch_resource_type()
        ui_types.ui_branch_version_resource_type()
        ui_types.ui_push_resource_type()
        ui_types.ui_css_resource_type()
        ui_types.ui_js_resource_type()
        # The studio's computed types (registered with their routes).
        studio_computed_type_urls()


# The studio app as the suite installs it, and as a site does.
STUDIO_WITHOUT_LAYER = "tests.apps.StudioWithoutLayerConfig"
STUDIO_APP = "scrud_django.ui.studio"


@pytest.fixture
def studio_layer():
    """The real studio app config, with its UI layer (``studio:``), in place
    of the suite's `tests.apps.StudioWithoutLayerConfig`, for the test. The
    layers are read again on the settings change (and after it)."""
    from django.conf import settings
    from django.test import override_settings

    installed = [
        STUDIO_APP if app == STUDIO_WITHOUT_LAYER else app
        for app in settings.INSTALLED_APPS
    ]
    assert STUDIO_APP in installed
    with override_settings(INSTALLED_APPS=installed):
        yield


# The built-in resource types `scrud_django.registration` caches at module
# level.
REGISTRATION_CACHES = [
    "JSON_SCHEMA_REGISTRATION_",
    "JSON_SCHEMA_RESOURCE_TYPE_",
    "JSON_LD_RESOURCE_TYPE_",
    "JSON_LD_REGISTRATION_",
    "INDEXING_POLICY_RESOURCE_TYPE_",
    "INDEXING_POLICY_REGISTRATION_",
    "WORKFLOW_POLICY_RESOURCE_TYPE_",
    "WORKFLOW_POLICY_REGISTRATION_",
]


@pytest.fixture
def committed_types():
    """The test resource types, committed for other connections to see: use
    it in every ``@pytest.mark.django_db(transaction=True)`` test that needs
    them.

    A transactional test's database is flushed after it, which deletes the
    types `django_db_setup` registered for the session (and the built-in types
    `registration` caches): they are registered again here, and the caches
    forgotten before and afterwards, so that the next test registers its own.
    pytest-django runs transactional tests after the other database tests
    (with ``--reverse`` too), so those never see the flushed database."""
    from scrud_django import registration
    from scrud_django.ui import types as ui_types
    from scrud_django.ui.studio.computed import studio_computed_type_urls
    from tests import resource_types

    def forget():
        for name in REGISTRATION_CACHES:
            setattr(registration, name, None)
        for name in ui_types.REGISTRATION_CACHES:
            setattr(ui_types, name, None)

    forget()
    resource_types.json_resource_types()
    registration.workflow_policy_resource_type()
    ui_types.ui_component_resource_type()
    ui_types.ui_stylesheet_resource_type()
    ui_types.ui_branch_resource_type()
    ui_types.ui_branch_version_resource_type()
    ui_types.ui_push_resource_type()
    ui_types.ui_css_resource_type()
    ui_types.ui_js_resource_type()
    studio_computed_type_urls()
    yield
    forget()
