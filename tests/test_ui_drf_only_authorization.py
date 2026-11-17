"""The UI fails closed on DRF-only authorization (spec §1.4): its pages,
embeds, prefixes and studio previews read through the core, which never
asks a custom ``viewset_class``'s permissions (``IsAuthorized`` subclasses,
their compositions) or methods, so the UI refuses a type served by one, as
it refuses a slug it does not serve (404), unless the type declares its
rules to the core (``checks=`` and ``object_checks=``) or that the core's
checks are complete for it (``ui_authorization="core"``). It serves a type
only when the views its API routes resolve to were built by a builder the
core trusts (registration, scrud's ninja ``api()``): a type routed by hand
is refused too. The system check ``scrud_django_ui.W003`` names each type it
refuses. These tests serve the API at ``/api/`` and the UI at ``/``
(``tests.drf_only_urls``, which is ``tests.mounted_urls`` with the types)."""

import dataclasses
import functools
import json

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory
from django.urls import resolve, reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for, registration
from scrud_django.core.types import (
    UNRECORDED_ROUTE,
    TypeOptions,
    drf_only_authorization,
    mark_route,
    route_authorization,
    type_options_for_slug,
)
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    content_filtered_viewset,
    drf_only_authorization_of,
)
from scrud_django.ui import pages
from scrud_django.ui.branches import (
    UI_BRANCH_SLUG,
    UI_BRANCH_VERSION_SLUG,
    UI_CSS_SLUG,
    UI_JS_SLUG,
    UI_PUSH_SLUG,
)
from scrud_django.ui.checks import DRF_ONLY_ID, check_drf_only_authorization
from scrud_django.ui.embed import REFUSED_MESSAGE, embed_for
from scrud_django.ui.studio.computed import STUDIO_COMPUTED_TYPES
from scrud_django.ui.types import UI_COMPONENT_SLUG, UI_STYLESHEET_SLUG
from scrud_django.views import ResourceViewSet

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST, assign_role
from .resource_types import (
    CHECKED_DENIED_NOTE_TYPE_URI,
    CORE_DENIED_NOTE_TYPE_URI,
    CORE_HAND_ROUTED_NOTE_TYPE_URI,
    DENIED,
    DENIED_NOTE_TYPE_URI,
    HAND_ROUTED_NOTE_TYPE_URI,
    HOOKED_DENIED_NOTE_TYPE_URI,
    OBJECT_CHECKED_DENIED_NOTE_TYPE_URI,
    SUMMARIES_TYPE,
    drf_only_types,
    hand_routed_types,
)
from .steps import *  # noqa: F403, F401
from .test_ui_branch_prefixes import mounted, publish
from .test_ui_branch_render import may_select_branches
from .test_ui_embeds import REFUSED, frames, get_page, host, ui_of, url_of
from .test_ui_http import html, put_json
from .ui import ui_url

URLCONF = "tests.drf_only_urls"
JSON = "application/json"
OPEN = "open"

DENIED_SLUG = "denied-notes"
CORE_SLUG = "core-denied-notes"
HOOKED_SLUG = "hooked-denied-notes"
CHECKED_SLUG = "checked-denied-notes"
OBJECT_CHECKED_SLUG = "object-checked-denied-notes"
HAND_SLUG = "hand-routed-notes"
CORE_HAND_SLUG = "core-hand-routed-notes"
NOTE_TYPES = {
    DENIED_SLUG: DENIED_NOTE_TYPE_URI,
    CORE_SLUG: CORE_DENIED_NOTE_TYPE_URI,
    HOOKED_SLUG: HOOKED_DENIED_NOTE_TYPE_URI,
    CHECKED_SLUG: CHECKED_DENIED_NOTE_TYPE_URI,
    OBJECT_CHECKED_SLUG: OBJECT_CHECKED_DENIED_NOTE_TYPE_URI,
    HAND_SLUG: HAND_ROUTED_NOTE_TYPE_URI,
    CORE_HAND_SLUG: CORE_HAND_ROUTED_NOTE_TYPE_URI,
}
# The types the UI refuses, as W003 names them (sorted by slug).
REFUSED_SLUGS = [CHECKED_SLUG, DENIED_SLUG, HAND_SLUG, OBJECT_CHECKED_SLUG]

# Every type the UI app and the studio register.
UI_SLUGS = (
    UI_COMPONENT_SLUG,
    UI_STYLESHEET_SLUG,
    UI_CSS_SLUG,
    UI_JS_SLUG,
    UI_BRANCH_SLUG,
    UI_BRANCH_VERSION_SLUG,
    UI_PUSH_SLUG,
)
STUDIO_SLUGS = tuple(slug for _, slug, _ in STUDIO_COMPUTED_TYPES)


@pytest.fixture(autouse=True)
def drf_only_urlconf(settings, db):
    """The URLconf with the types, whose rows are registered again (a test's
    rollback removed those its import stored)."""
    settings.ROOT_URLCONF = URLCONF
    reverse("denied-notes-list")
    drf_only_types()
    hand_routed_types()


def note_writer():
    """A user granted every method on the types and on to-do lists in
    "denied", "open" and "project-a": only the viewset's
    ``ContextDenyingIsAuthorized`` (or the hooks) refuse it."""
    iris = [*NOTE_TYPES.values(), TYPE_URI_TODO_LIST]
    every = [*iris, *map(collection_type_uri_for, iris)]
    return assign_role(
        {
            "http.GET": every,
            "http.PUT": every,
            "http.DELETE": every,
            "http.POST": every,
        },
        DENIED,
        OPEN,
        "project-a",
    )


def note(slug, context, text="A note"):
    return ResourceRegistration.register({"text": text, "rbac_context": context}, slug)


def api_list(slug):
    return reverse(f"{slug}-list")


def api_detail(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def listed_texts():
    return sorted(r["content"]["text"] for r in Http.response.json()["content"])


def assert_refused_by_the_ui(slug, resource):
    """Every page of `slug` (its list and `resource`'s, each method) is the
    UI's 404 for a slug it does not serve: no page, nothing of the type."""

    def refused():
        Then.http.status_code_is(404)
        page = html()
        assert "Hidden" not in page and "Shown" not in page and slug not in page

    for url in (ui_url(api_list(slug)), ui_url(api_detail(resource))):
        for method in ("get", "head"):
            getattr(When.http, method)(url)
            refused()
    body = {"text": "Written", "rbac_context": OPEN}
    When.http.post(ui_url(api_list(slug)), data=json.dumps(body), content_type=JSON)
    refused()
    put_json(ui_url(api_detail(resource)), body, HTTP_IF_MATCH=f'"{resource.etag}"')
    refused()
    When.http.delete(ui_url(api_detail(resource)), HTTP_IF_MATCH=f'"{resource.etag}"')
    refused()


@pytest.mark.django_db
@test_context
def test_the_api_refuses_what_the_custom_viewset_refuses():
    hidden = note(DENIED_SLUG, DENIED, "Hidden")
    note(DENIED_SLUG, OPEN, "Shown")
    Given.http.force_login(note_writer())
    When.http.get(api_detail(hidden), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(404)
    When.http.get(api_list(DENIED_SLUG), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert listed_texts() == ["Shown"]
    When.http.post(
        api_list(DENIED_SLUG),
        data=json.dumps({"text": "New", "rbac_context": DENIED}),
        content_type=JSON,
    )
    Then.http.status_code_is(403)


@pytest.mark.django_db
@test_context
def test_the_ui_refuses_a_type_with_drf_only_authorization():
    hidden = note(DENIED_SLUG, DENIED, "Hidden")
    shown = note(DENIED_SLUG, OPEN, "Shown")
    Given.http.force_login(note_writer())
    for resource in (hidden, shown):
        assert_refused_by_the_ui(DENIED_SLUG, resource)
    assert pages.served(DENIED_SLUG) is None
    # Nothing was written or deleted.
    stored = Resource.objects.filter(resource_type__slug=DENIED_SLUG)
    assert sorted(r.content["text"] for r in stored) == ["Hidden", "Shown"]


@pytest.mark.django_db
@test_context
def test_the_ui_refuses_the_anonymous_alike():
    """The anonymous user gets the 404 of an unserved slug too, not the 401
    or 403 a served type would answer: nothing tells the type exists."""
    hidden = note(DENIED_SLUG, DENIED, "Hidden")
    shown = note(DENIED_SLUG, OPEN, "Shown")
    for resource in (hidden, shown):
        assert_refused_by_the_ui(DENIED_SLUG, resource)


@pytest.mark.django_db
@test_context
def test_a_type_declaring_only_one_of_the_hooks_stays_refused():
    """Only ``checks=`` (writes) or only ``object_checks=`` (reads): the
    viewset may still authorize the other side, so the UI refuses it."""
    Given.http.force_login(note_writer())
    for slug in (CHECKED_SLUG, OBJECT_CHECKED_SLUG):
        resource = note(slug, OPEN, "Shown")
        assert pages.refusal(slug) is not None, slug
        assert_refused_by_the_ui(slug, resource)


@pytest.mark.django_db
@test_context
def test_a_type_routed_by_hand_is_refused_unless_it_opts_in():
    """Routes not built by registration (``ResourceTypeRegistration(
    build_urls=False)`` and ``as_view`` named as registration names them):
    nothing tells what authorizes them, so the UI refuses the type; with
    ``ui_authorization="core"`` among its options it serves it."""
    hidden = note(HAND_SLUG, DENIED, "Hidden")
    shown = note(HAND_SLUG, OPEN, "Shown")
    Given.http.force_login(note_writer())
    # The API serves it, with its view's authorization.
    When.http.get(api_detail(hidden), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(404)
    When.http.get(api_detail(shown), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert pages.refusal(HAND_SLUG) == UNRECORDED_ROUTE
    for resource in (hidden, shown):
        assert_refused_by_the_ui(HAND_SLUG, resource)
    opted_in = note(CORE_HAND_SLUG, OPEN, "Opted in")
    assert pages.refusal(CORE_HAND_SLUG) is None
    When.http.get(ui_url(api_list(CORE_HAND_SLUG)))
    Then.http.status_code_is(200)
    When.http.get(ui_url(api_detail(opted_in)))
    Then.http.status_code_is(200)
    assert "Opted in" in html()


@pytest.mark.django_db
@test_context
def test_two_urlconfs_building_a_slug_differently_each_decide_alike(settings):
    """The views the request's URLconf routes the slug to decide, whichever
    URLconf built its routes last: ``tests.drf_only_default_urls`` builds
    ``denied-notes`` with the default viewset, ``tests.drf_only_urls`` with
    a custom one."""
    shown = note(DENIED_SLUG, OPEN, "Shown")
    Given.http.force_login(note_writer())
    for urlconf, status in (
        ("tests.drf_only_default_urls", 200),
        (URLCONF, 404),
        ("tests.drf_only_default_urls", 200),
        (URLCONF, 404),
    ):
        settings.ROOT_URLCONF = urlconf
        # Each import (or not) of the URLconf builds its routes again.
        reverse("denied-notes-list")
        When.http.get(ui_url(api_detail(shown)))
        Then.http.status_code_is(status)


@pytest.mark.django_db
@test_context
def test_an_embed_of_a_drf_only_type_is_refused(todo_list):
    hidden = note(DENIED_SLUG, DENIED, "Hidden")
    shown = note(HOOKED_SLUG, OPEN, "Shown")
    refused = [
        api_detail(hidden),
        ui_url(api_detail(hidden)),
        api_list(DENIED_SLUG),
        ui_url(api_list(DENIED_SLUG)),
    ]
    template = "".join(
        '<p class="e">{{ embed(' + repr(path) + ") }}</p>" for path in refused
    )
    host(template + '<p class="ok">{{ embed("' + url_of(shown) + '") }}</p>')
    Given.http.force_login(note_writer())
    page = get_page(ui_of(todo_list), scope="host")
    assert page.count(f'<p class="e">{REFUSED}</p>') == len(refused)
    assert "Hidden" not in page
    # A type that declares its rules to the core embeds.
    assert len(frames(page, ui_of(shown))) == 1 and "Shown" in page
    assert str(embed_for(RequestFactory().get("/x/"))(api_detail(hidden))) == REFUSED
    assert REFUSED_MESSAGE in page


@pytest.mark.django_db
@test_context
def test_a_prefix_does_not_claim_a_drf_only_type():
    hidden = note(DENIED_SLUG, OPEN, "Hidden")
    shown = note(HOOKED_SLUG, OPEN, "Shown")
    publish("front", "/front/")
    Given.http.force_login(may_select_branches(note_writer()))
    for url in (
        mounted("/front/", hidden, DENIED_SLUG),
        mounted("/front/", None, DENIED_SLUG),
    ):
        When.http.get(url)
        Then.http.status_code_is(404)
        assert "Hidden" not in html()
    When.http.get(mounted("/front/", shown, HOOKED_SLUG))
    Then.http.status_code_is(200)
    assert "Shown" in html()


@pytest.mark.django_db
@test_context
def test_a_studio_preview_of_a_drf_only_instance_is_refused():
    hidden = note(DENIED_SLUG, OPEN, "Hidden")
    shown = note(HOOKED_SLUG, OPEN, "Shown")
    Given.http.force_login(note_writer())
    When.http.get(
        reverse("scrud-studio:preview", args=[DENIED_SLUG]), {"instance": hidden.pk}
    )
    Then.http.status_code_is(404)
    assert "Hidden" not in html()
    When.http.get(
        reverse("scrud-studio:preview", args=[HOOKED_SLUG]), {"instance": shown.pk}
    )
    Then.http.status_code_is(200)
    assert "Shown" in html()


@pytest.mark.django_db
@test_context
def test_the_studio_refuses_as_for_an_unknown_slug():
    """The studio's read of a refused type's resource answers exactly as one
    of a slug that names no type: the same status and problem body."""
    from scrud_django.ui.studio import answers

    hidden = note(DENIED_SLUG, OPEN, "Hidden")
    request = RequestFactory().get("/studio/")
    request.user = note_writer()
    refused = answers.write(request, DENIED_SLUG, "GET", pk=hidden.pk)
    unknown = answers.write(request, "no-such-type", "GET", pk=hidden.pk)
    assert refused.status == unknown.status == 404
    assert refused.problem == unknown.problem
    assert refused.problem_type == unknown.problem_type
    assert answers.validate_draft(request, DENIED_SLUG, {"text": "x"}) == (
        answers.validate_draft(request, "no-such-type", {"text": "x"})
    )


@pytest.mark.django_db
@test_context
def test_ui_authorization_core_serves_the_type():
    """The type declares the core's checks complete for it: the UI serves it
    by its grants (and hooks) alone, as declared."""
    in_denied = note(CORE_SLUG, DENIED, "In denied")
    Given.http.force_login(note_writer())
    When.http.get(ui_url(api_list(CORE_SLUG)))
    Then.http.status_code_is(200)
    assert "In denied" in html()
    When.http.get(ui_url(api_detail(in_denied)))
    Then.http.status_code_is(200)
    assert "In denied" in html()
    # The API keeps its viewset's authorization.
    When.http.get(api_detail(in_denied), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(404)


@pytest.mark.django_db
@test_context
def test_checks_and_object_checks_serve_the_type_and_apply():
    in_denied = note(HOOKED_SLUG, DENIED, "In denied")
    in_open = note(HOOKED_SLUG, OPEN, "In open")
    Given.http.force_login(note_writer())
    When.http.get(ui_url(api_list(HOOKED_SLUG)))
    Then.http.status_code_is(200)
    assert "In open" in html() and "In denied" not in html()
    When.http.get(ui_url(api_detail(in_open)))
    Then.http.status_code_is(200)
    assert "In open" in html()
    # The object check hides it, as the API's viewset does.
    When.http.get(ui_url(api_detail(in_denied)))
    Then.http.status_code_is(404)
    When.http.get(api_detail(in_denied), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(404)
    # The check refuses a write in "denied", as the API's viewset does.
    body = {"text": "New", "rbac_context": DENIED}
    When.http.post(
        ui_url(api_list(HOOKED_SLUG)), data=json.dumps(body), content_type=JSON
    )
    Then.http.status_code_is(403)
    When.http.post(api_list(HOOKED_SLUG), data=json.dumps(body), content_type=JSON)
    Then.http.status_code_is(403)


@pytest.mark.django_db
def test_the_system_check_names_each_refused_type():
    found = check_drf_only_authorization(None)
    assert [w.id for w in found] == [DRF_ONLY_ID] * len(REFUSED_SLUGS)
    assert [w.obj for w in found] == REFUSED_SLUGS
    by_slug = {w.obj: w for w in found}
    for slug in (CHECKED_SLUG, DENIED_SLUG, OBJECT_CHECKED_SLUG):
        assert f"{slug!r}" in by_slug[slug].msg
        assert "tests.urls.ContextDenyingResourceViewSet" in by_slug[slug].msg
    assert f"{HAND_SLUG!r}" in by_slug[HAND_SLUG].msg
    assert "not built by registration" in by_slug[HAND_SLUG].msg
    for warning in found:
        assert "checks=" in warning.hint and "object_checks=" in warning.hint
        assert 'ui_authorization="core"' in warning.hint
        for slug in (CORE_SLUG, HOOKED_SLUG, CORE_HAND_SLUG):
            assert f"{slug!r}" not in warning.msg
    # ``manage.py check`` runs it.
    assert [w for w in checks.run_checks() if w.id == DRF_ONLY_ID] == found


@pytest.mark.django_db
def test_the_system_check_names_no_type_another_urlconf_serves(settings):
    """``tests.urls`` serves none of the types, and none of its own is
    refused (``todo-summaries``, served by a viewset class alone, has no UI
    page in the first place)."""
    settings.ROOT_URLCONF = "tests.mounted_urls"
    assert check_drf_only_authorization(None) == []


@pytest.mark.django_db
def test_every_built_in_ui_and_studio_type_is_served():
    reverse("ui-components-list")
    for slug in (*UI_SLUGS, *STUDIO_SLUGS, "json-schema", "json-ld"):
        assert pages.refusal(slug) is None, slug
        assert pages.served(slug) is not None, slug
    # The UI types' viewsets are their own, which hold no authorization
    # beyond their hooks: the types declare the core's checks complete.
    for slug in UI_SLUGS:
        viewset = resolve(reverse(f"{slug}-list")).func.cls
        assert drf_only_authorization_of(viewset) is not None, slug
        assert type_options_for_slug(slug).ui_authorization == "core", slug


@pytest.mark.django_db
def test_scrud_viewsets_hold_no_drf_only_authorization():
    from scrud_django import views
    from scrud_django.computed import ComputedResourceViewSet

    from .urls import ContextDenyingResourceViewSet

    for viewset in (
        views.ResourceViewSet,
        views.ResourceViewSetGenericFilter,
        views.JsonSchemaViewSet,
        views.JsonLdViewSet,
        ComputedResourceViewSet,
        content_filtered_viewset(ResourceViewSet),
    ):
        assert drf_only_authorization_of(viewset) is None, viewset
    # A custom one, content filtered or not, and a subclass of the filtered
    # default.
    filtered = content_filtered_viewset(ResourceViewSet)
    for viewset in (
        ContextDenyingResourceViewSet,
        content_filtered_viewset(ContextDenyingResourceViewSet),
        type("Sub", (filtered,), {}),
    ):
        assert drf_only_authorization_of(viewset) is not None, viewset


def test_ui_authorization_is_core_or_none():
    assert TypeOptions(ui_authorization="core").ui_authorization == "core"
    with pytest.raises(ValueError):
        TypeOptions(ui_authorization="drf")


def test_only_the_marked_view_itself_is_trusted():
    """A view no trusted builder marked, or one wrapping a marked view
    (``functools.wraps`` copies its mark), is unrecorded: it may authorize
    on its own, so it fails closed."""

    def view(request):
        return None

    assert route_authorization(view) == UNRECORDED_ROUTE
    assert drf_only_authorization("any-slug", view) == UNRECORDED_ROUTE
    assert drf_only_authorization("any-slug", None) == UNRECORDED_ROUTE
    mark_route(view)
    assert route_authorization(view) is None
    assert drf_only_authorization("any-slug", view) is None

    @functools.wraps(view)
    def wrapping(request):
        return view(request)

    assert route_authorization(wrapping) == UNRECORDED_ROUTE
    mark_route(view, "its custom viewset")
    assert route_authorization(view) == "its custom viewset"


@pytest.mark.django_db
def test_ninja_marks_its_type_routes_as_the_core_s():
    """Scrud's ninja ``api()`` handlers call only the core: their views are
    marked as the core's."""
    from . import ninja_api

    names = {"partner-profiles-list", "partner-profiles-detail", "summaries-detail"}
    found = {}

    def walk(patterns):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns)
            elif pattern.name in names:
                found[pattern.name] = pattern.callback

    walk(ninja_api.URLS[0])
    assert set(found) == names
    for name, view in found.items():
        assert route_authorization(view) is None, name


@pytest.mark.django_db
def test_a_computed_type_may_declare_ui_authorization():
    spec = registration.computed_resource_type_spec(
        "tests://DeclaredSummary",
        "declared-summaries",
        SUMMARIES_TYPE["schema_func"],
        SUMMARIES_TYPE["context_func"],
        compute=SUMMARIES_TYPE["compute"],
    )
    declared = dataclasses.replace(
        spec, options=dataclasses.replace(spec.options, ui_authorization="core")
    )
    assert declared.options.ui_authorization == "core"
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(
            spec,
            options=dataclasses.replace(spec.options, checks=(lambda *a: True,)),
        )
