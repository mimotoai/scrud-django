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


#: `tests.front_ends.FRONT_ENDS`, which this module cannot import (it imports
#: Django models, before ``pytest_configure`` sets Django up): the
#: ``front_end`` fixture checks the two are alike.
FRONT_END_PARAMS = ("drf", "ninja")


@pytest.fixture(params=FRONT_END_PARAMS)
def front_end(request):
    """The front end a generic test runs on, once for each of
    `tests.front_ends.FRONT_ENDS`: ``"drf"`` on ``tests.urls`` (the
    settings' URLconf), ``"ninja"`` on ``tests.ninja_urls``, whose types are
    registered first (`tests.front_ends.serve_ninja`). Paths come from
    `tests.front_ends.api_url`. ``override_settings`` sends
    ``setting_changed``, on which Django forgets its URL caches and scrudful
    the HTTP Schema Resources it built, as the test starts and ends; the
    caches are cleared here too, so that no resolver of one URLconf serves
    the other."""
    from django.test import override_settings
    from django.urls import clear_url_caches, set_urlconf

    from tests.front_ends import FRONT_ENDS, serve_ninja

    assert FRONT_END_PARAMS == FRONT_ENDS, (FRONT_END_PARAMS, FRONT_ENDS)
    if request.param == "drf":
        yield request.param
        return
    with override_settings(ROOT_URLCONF="tests.ninja_urls"):
        clear_url_caches()
        set_urlconf(None)
        serve_ninja()
        try:
            yield request.param
        finally:
            clear_url_caches()
            set_urlconf(None)
    clear_url_caches()
    set_urlconf(None)


@pytest.fixture
def studio_layer():
    """The studio's UI layer (``studio:``), which the suite turns off
    (``SCRUD_UI_STUDIO_LAYER = False``), on for the test. The layers are
    read again on the settings change (and after it)."""
    from django.test import override_settings

    with override_settings(SCRUD_UI_STUDIO_LAYER=True):
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
