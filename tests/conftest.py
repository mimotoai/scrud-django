import re

import pytest

from tests.no_drf import BLOCKED
from tests.settings import NO_DRF

# The suite without DRF (``SCRUD_TEST_NO_DRF=1``, CI's ``test-no-drf``): DRF's
# imports are blocked (`tests.no_drf.block_drf`) before anything imports it,
# and the settings are `tests.settings.without_drf`'s.
if NO_DRF:
    from tests.no_drf import block_drf

    block_drf()


def pytest_addoption(parser):
    parser.addoption(
        "--reverse",
        action="store_true",
        help="Run the tests in reverse order (the modules, and the tests in "
        "each, within each of pytest-django's groups, which keep their "
        "order), to show that no test depends on one that runs before it.",
    )


#: The test modules that need DRF, by module name: they get ``pytest.mark.drf``
#: and, when DRF cannot be imported, are skipped whole, not imported
#: (`NeedsDrfModule`). A module is here when it imports DRF, `scrud_django.drf`
#: or scoped-rbac's DRF modules (or `tests.urls`, which does), uses the
#: ``force_authenticate`` step, imports a marked module's helpers, or tests
#: DRF's routes; a module with a few such tests marks those itself
#: (``@pytest.mark.drf``, skipped by `mark_drf_modules`).
#: `tests.test_steps_client` checks the names exist. The reverse guard, an
#: unmarked test that needs DRF, is the suite without DRF
#: (``SCRUD_TEST_NO_DRF=1``, CI's ``test-no-drf``).
DRF_MODULES = frozenset(
    {
        "test_atomic_writes",
        "test_authz",
        "test_collection_schemas",
        "test_computed_types",
        "test_condition_contexts",
        "test_conditional_create",
        "test_context_limits",
        "test_core_collections",
        "test_core_errors",
        "test_core_json",
        "test_core_lists",
        "test_core_parity",
        "test_core_requirements",
        "test_core_resources",
        "test_core_ui_types",
        "test_core_workflow",
        "test_core_writes",
        "test_custom_operators",
        "test_default_workflow_policies",
        "test_delete_locking",
        "test_drf_package",
        "test_drf_parity",
        "test_empty_body",
        "test_error_media_types",
        "test_exact_contexts",
        "test_exceptions",
        "test_front_end_parity",
        "test_generic_views",
        "test_json_schema_refs",
        "test_list_filter_check",
        "test_list_object_checks",
        "test_list_rbac",
        "test_list_removals",
        "test_media_types",
        "test_ninja_drf_auth",
        "test_ninja_uniform_off",
        "test_nul_bodies",
        "test_object_check_readability",
        "test_object_checks",
        "test_options",
        "test_paginations",
        "test_policy_cache",
        "test_policy_duplicates",
        "test_post_commit",
        "test_query_budget",
        "test_resource_contexts",
        "test_schema_defaults",
        "test_serializers",
        "test_services",
        "test_studio_dispatch",
        "test_studio_writes",
        "test_type_hooks",
        "test_type_registration",
        "test_ui_app",
        "test_ui_drf_only_authorization",
        "test_ui_json_bodies",
        "test_ui_negotiation",
        "test_ui_routes",
        "test_ui_types",
        "test_upstream_drift",
        "test_workflow_contexts",
        "test_workflow_edge_cases",
        "test_workflow_fail_closed",
        "test_workflow_permission_class",
        "test_write_routing",
    }
)


def drf_importable():
    """Whether DRF can be imported (`tests.steps.drf_importable`; imported
    when called, as that module needs Django set up)."""
    from tests import steps

    return steps.drf_importable()


def mark_drf_modules(items):
    """Mark the tests of `DRF_MODULES` ``drf``, and skip every ``drf``-marked
    test (a module's or a test's own) when DRF cannot be imported."""
    available = drf_importable()
    skip = pytest.mark.skip(reason=NEEDS_DRF)
    for item in items:
        if item.path.stem in DRF_MODULES:
            item.add_marker(pytest.mark.drf)
        if not available and item.get_closest_marker("drf"):
            item.add_marker(skip)


#: The reason a test that needs DRF is skipped without it.
NEEDS_DRF = "needs DRF, which cannot be imported"

#: A skip reason naming DRF, a module the no-DRF run blocks
#: (`tests.no_drf.BLOCKED`), or the blocker's own message (``blocked: ...``).
NAMES_DRF = re.compile(
    "|".join([r"drf", r"django[_-]filter", r"blocked:", *map(re.escape, BLOCKED)]),
    re.IGNORECASE,
)

#: The no-DRF run's skips, counted by kind (`count_skip`).
NO_DRF_SKIPS = pytest.StashKey[dict]()

#: The test modules the no-DRF run collects as modules (not `DRF_MODULES`),
#: and those of its tests deselected (``-k``), for `pytest_collection_finish`.
NO_DRF_MODULES = pytest.StashKey[set]()
NO_DRF_DESELECTED_MODULES = pytest.StashKey[set]()


def skip_kind(item):
    """Why the no-DRF run may skip `item` for want of DRF: ``"drf-marked"``,
    ``"front_end drf parameter"`` (the ``front_end`` fixture's), or None."""
    if item.get_closest_marker("drf"):
        return "drf-marked"
    callspec = getattr(item, "callspec", None)
    if callspec is not None and callspec.params.get("front_end") == "drf":
        return "front_end drf parameter"
    return None


def skip_reason(report):
    """The reason of a skipped test's or collector's `report`."""
    return report.longrepr[2] if isinstance(report.longrepr, tuple) else ""


def refused_skip(kind, reason):
    """The failure message for a no-DRF skip that may not be one: a skip whose
    `reason` names DRF (`NAMES_DRF`) of something neither ``drf``-marked nor
    the ``front_end`` fixture's ``drf`` parameter (`kind` None); else None."""
    if kind is None and NAMES_DRF.search(reason):
        return (
            "skipped without DRF, but neither drf-marked nor the front_end "
            f"fixture's drf parameter: {reason}"
        )
    return None


def count_skip(config, kind):
    counts = config.stash.setdefault(NO_DRF_SKIPS, {})
    counts[kind] = counts.get(kind, 0) + 1


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """The no-DRF run skips only what needs DRF: a test's skip that
    `refused_skip` refuses (an ``importorskip`` of DRF, say) fails, so that no
    test silently disappears without DRF. Every skip is counted by kind for
    the terminal summary."""
    report = yield
    if not NO_DRF or not report.skipped or hasattr(report, "wasxfail"):
        return report
    reason = skip_reason(report)
    kind = skip_kind(item)
    refused = refused_skip(kind, reason)
    if refused:
        report.outcome = "failed"
        report.longrepr = refused
        return report
    count_skip(item.config, kind or f"other: {reason}")
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    """`pytest_runtest_makereport`'s rule for a module skipped as it is
    collected (a module-level ``importorskip`` of DRF): the skip of a module
    not in `DRF_MODULES` whose reason names DRF fails. Without DRF, each test
    module collected (not `DRF_MODULES`') is noted for
    `pytest_collection_finish`."""
    report = yield
    if not NO_DRF or isinstance(collector, NeedsDrfModule):
        return report
    if isinstance(collector, pytest.Module) and collector.path.stem.startswith("test_"):
        collector.config.stash.setdefault(NO_DRF_MODULES, set()).add(collector.path)
    if not report.skipped:
        return report
    reason = skip_reason(report)
    refused = refused_skip(None, reason)
    if refused:
        report.outcome = "failed"
        report.longrepr = refused
        return report
    count_skip(collector.config, f"other (collection): {reason}")
    return report


def modules_without_tests(modules, item_modules):
    """The paths of `modules` none of whose tests are among `item_modules`
    (the paths the collected tests came from), sorted."""
    return sorted(str(path) for path in set(modules) - set(item_modules))


def pytest_deselected(items):
    """Without DRF, the modules of deselected tests, which yielded tests."""
    if NO_DRF and items:
        modules = items[0].config.stash.setdefault(NO_DRF_DESELECTED_MODULES, set())
        modules.update(item.path for item in items)


def pytest_collection_finish(session):
    """The no-DRF run: each test module it collected that is not in
    `DRF_MODULES` yielded a test (one skipped as it was collected, say, has
    none), or the run stops, failed."""
    if not NO_DRF:
        return
    item_modules = {item.path for item in session.items}
    item_modules |= session.config.stash.get(NO_DRF_DESELECTED_MODULES, set())
    missing = modules_without_tests(
        session.config.stash.get(NO_DRF_MODULES, set()), item_modules
    )
    if missing:
        pytest.exit(
            "test modules with no test without DRF (not in DRF_MODULES): "
            + ", ".join(missing),
            returncode=pytest.ExitCode.TESTS_FAILED,
        )


def pytest_terminal_summary(terminalreporter, config):
    """The no-DRF run's skips by kind."""
    if not NO_DRF:
        return
    terminalreporter.section("skipped without DRF (SCRUD_TEST_NO_DRF=1)")
    counts = config.stash.get(NO_DRF_SKIPS, {})
    for kind, count in sorted(counts.items()):
        terminalreporter.write_line(f"{count:5d}  {kind}")


class NeedsDrfModule(pytest.Module):
    """A module of `DRF_MODULES` when DRF cannot be imported: skipped whole
    (`NEEDS_DRF`), never imported (most import DRF)."""

    def collect(self):
        count_skip(self.config, "drf-marked module, not collected")
        pytest.skip(NEEDS_DRF, allow_module_level=True)


def pytest_pycollect_makemodule(module_path, parent):
    """A `NeedsDrfModule` for a module of `DRF_MODULES` when DRF cannot be
    imported; pytest's own module otherwise."""
    if module_path.stem in DRF_MODULES and not drf_importable():
        return NeedsDrfModule.from_parent(parent, path=module_path)
    return None


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config, items):
    """``--reverse``: reverse the collected tests before pytest-django's
    ordering runs (this wraps it). Its sort is stable, so each of its groups
    (database tests, then transactional ones, which flush the database, then
    the others) runs in reverse, and the groups keep their order: a
    transactional test never runs first."""
    if config.getoption("--reverse"):
        items.reverse()
    mark_drf_modules(items)
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
    created when ``scrud_django.drf.urls`` is first imported, inside whichever test
    first resolves a URL, and that test's rollback removes it (and its JSON-LD
    context document) for every later test.
    """
    with django_db_blocker.unblock():
        _register_types()


def _register_types():
    """Register the test types (registered when the URLconf is imported,
    inside whichever test first resolves a URL, whose rollback would remove
    their rows), the built-in WorkflowPolicy type, and the UI's and studio's
    types. With DRF, the test types' DRF registrations (`tests.urls`
    serves them); without, the specs of those ninja serves
    (`tests.resource_types.ninja_type_specs`: all but the DRF viewset's
    ``todo-summaries``)."""
    from scrud_django import registration
    from tests import resource_types

    if NO_DRF:
        for spec in resource_types.ninja_type_specs():
            spec.register()
    else:
        resource_types.json_resource_types()
        resource_types.hooked_resource_types()
    registration.workflow_policy_resource_type()
    # The UI types and the studio's computed types, with their hooks.
    _register_ui_types()


def _register_ui_types():
    """Register the UI resource types and the studio's computed types
    (`scrud_django.ninja.api.ui_specs`, as both front ends serve them): their
    hooks recorded and their rows stored."""
    from scrud_django.ninja.api import ui_specs

    for spec in ui_specs():
        spec.register()


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
        if not drf_importable():
            pytest.skip("needs DRF, which cannot be imported")
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
def plain_client(monkeypatch):
    """Opt a test (or a module, with ``pytestmark = pytest.mark.usefixtures(
    "plain_client")``) in to ``django.test.Client`` for the ``Http`` steps
    (`tests.steps.PlainClient`), where DRF is installed too."""
    from tests import steps

    steps.use_plain_client(monkeypatch)


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

    def forget():
        for name in REGISTRATION_CACHES:
            setattr(registration, name, None)

    forget()
    _register_types()
    yield
    forget()
