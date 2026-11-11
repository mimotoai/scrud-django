"""The self-hosted studio's preview pane: the bare live preview (a hidden form
submitted into a sandboxed frame), its controls (a type picker embedded from
``ui-type-catalog``, rendered by ``studio:type-picker``, and the mode, data
context, scope, instance and pinned controls) and the inspector under the
preview. The client side is tested under node in
``tests/test_studio_layer_client.py``. These tests serve the API at
``/api/`` and the bootstrap studio at ``/studio/`` (``tests.mounted_urls``),
with the studio layer installed (the ``studio_layer`` fixture)."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.layers import layer_item
from scrud_django.ui.studio.views import registered_types

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_studio_workbench import (
    STUDIO_ROOT,
    Page,
    get_page,
    setup_studio,
    workbench_url,
)
from .test_ui_branch_types import make_branch

UITYPECATALOG = "http://api.openteams.com/json-ld/UITypeCatalog"
CONTROLS = ["type", "mode", "context", "scope", "instance", "pinned"]


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def no_inline_code(page):
    """No event handler attribute, and every script a file (``src``) or
    data (a JSON ``scrud-content`` block)."""
    for e in page.elements:
        assert not [a for a in e.attrs if a.startswith("on")], e.attrs
        if e.tag == "script" and "src" not in e.attrs:
            assert e.attrs.get("type") == "application/json", e.attrs
            assert "scrud-content" in e.classes(), e.attrs


def preview_pane(page):
    [pane] = [
        e
        for e in page.having("data-studio-pane")
        if e.attrs["data-studio-pane"] == "preview"
    ]
    return pane


@pytest.mark.django_db
def test_type_picker_rule_and_component(studio_layer):
    _etag, sheet = layer_item("stylesheet", ("studio:studio", ""))
    rules = {(r["select"], r["component"]) for r in sheet["rules"]}
    assert (f"studio studio-types {UITYPECATALOG}", "studio:type-picker") in rules
    _etag, component = layer_item("component", ("studio:type-picker",))
    assert component["engine"] == "jinja2" and component["mode"] == "view"


@pytest.mark.django_db
@test_context
def test_preview_pane_markup(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    page = Page(get_page(workbench_url(alice_wip, STUDIO_ROOT)))
    no_inline_code(page)
    pane = preview_pane(page)
    # The bare live preview: a hidden form into the sandboxed frame, whose
    # token and request studio.js fills.
    [iframe] = [e for e in page.elements if e.tag == "iframe"]
    assert iframe.within(pane)
    assert iframe.attrs["sandbox"] == "allow-scripts"
    assert "allow-same-origin" not in iframe.attrs["sandbox"]
    form = page.one("data-studio-preview-form")
    assert form.within(pane) and "hidden" in form.attrs
    assert form.attrs["method"] == "post"
    assert form.attrs["target"] == iframe.attrs["name"] == "studio-preview"
    inputs = [e.attrs for e in page.elements if e.tag == "input" and e.within(form)]
    assert inputs == [
        {"type": "hidden", "name": "csrfmiddlewaretoken", "value": ""},
        {"type": "hidden", "name": "request", "value": ""},
    ]
    # The controls, in the preview pane, none a field of a frame (no name).
    controls = page.having("data-studio-control", within=pane)
    assert [c.attrs["data-studio-control"] for c in controls] == CONTROLS
    assert all("name" not in c.attrs for c in controls)
    by_name = {c.attrs["data-studio-control"]: c for c in controls}
    assert by_name["type"].tag == "select"
    assert by_name["mode"].tag == "select"
    modes = [
        (e.attrs["value"], e.text)
        for e in page.elements
        if e.tag == "option" and e.within(by_name["mode"])
    ]
    assert modes == [("view", "view"), ("edit", "edit")]
    for name in ("context", "scope", "instance"):
        assert (by_name[name].tag, by_name[name].attrs["type"]) == ("input", "text")
    assert (by_name["pinned"].tag, by_name["pinned"].attrs["type"]) == (
        "input",
        "checkbox",
    )
    # The inspector, under the preview: the explanation, and the new rule
    # form's place (T is a working branch).
    inspector = page.one("data-studio-inspector")
    assert inspector.within(pane)
    assert page.elements.index(inspector) > page.elements.index(iframe)
    explain = page.one("data-studio-explain")
    assert explain.within(inspector) and explain.attrs["aria-live"] == "polite"
    new_rule = page.one("data-studio-new-rule")
    assert new_rule.within(inspector) and "hidden" in new_rule.attrs
    # No write control in the preview pane: the new rule is the script's.
    assert not [e for e in page.writes() if e.within(pane)]


@pytest.mark.django_db
@test_context
def test_type_picker_lists_catalog(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    catalog = reverse("ui-type-catalog-detail", args=["all"])
    When.http.get(catalog, HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    types = Http.response.json()["types"]
    assert types == registered_types() and len(types) > 3
    page = Page(get_page(workbench_url(alice_wip, STUDIO_ROOT)))
    pane = preview_pane(page)
    # The catalog embedded in the preview pane, in its scope.
    [frame] = [
        e
        for e in page.having("data-scrud-href")
        if e.attrs["data-scrud-href"] == STUDIO_ROOT + catalog[len("/api/") :]
    ]
    assert frame.within(pane)
    assert frame.attrs["data-scrud-scope"] == "studio studio-types"
    [picker] = [
        e
        for e in page.having("data-studio-control")
        if e.attrs["data-studio-control"] == "type"
    ]
    assert picker.within(frame)
    options = [
        (e.attrs["value"], " ".join(e.text.split()))
        for e in page.elements
        if e.tag == "option" and e.within(picker)
    ]
    assert options == [("", "Choose a type")] + [
        (t["slug"], f"{t['title']} ({t['slug']})") for t in types
    ]


@pytest.mark.django_db
@test_context
def test_published_T_inspector_has_no_new_rule(studio_layer, admin_login):
    studio, _alice_wip, _bobs = setup_studio(admin_login)
    main = make_branch("main", "", parent=None)
    Given.http.force_authenticate(admin_login)
    for branch in (studio, main):
        page = Page(get_page(workbench_url(branch, STUDIO_ROOT)))
        assert page.one("data-studio-workbench").attrs["data-studio-published"] == (
            "true"
        )
        # The preview and its controls are there; the new rule form is not.
        assert page.one("data-studio-inspector")
        assert page.one("data-studio-explain")
        assert page.having("data-studio-new-rule") == []
        assert len(page.having("data-studio-control")) == len(CONTROLS)
