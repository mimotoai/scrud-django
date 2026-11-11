"""The studio's data as computed resources (spec E2): a branch's effective
items, a branch's status, the type catalog and the branches the user may
select. Each lists only what the user may GET as JSON (packaged layer items
are public), and renders through the cascade like any resource. These tests
serve the API at ``/api/`` and the studio at ``/studio/``
(``tests.mounted_urls``)."""

import re
from html import unescape

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, UI_CSS_TYPE_URI
from scrud_django.ui.branches.store import branches
from scrud_django.ui.layers import layer_item
from scrud_django.ui.studio.views import registered_types
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_studio_page import CONFLICT, SLUG, section, setup_branches, store_item
from .test_ui_branch_push import quoted
from .test_ui_branch_types import make_branch
from .test_ui_http import HTML, assert_html, grant, html, make_component

JSON = "application/json"
OT = "http://api.openteams.com/json-ld/"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def url(slug, key, **params):
    found = reverse(f"{slug}-detail", args=[key])
    if params:
        found += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return found


def get(slug, key, status=200, **params):
    When.http.get(url(slug, key, **params), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(status)
    return Http.response.json() if status == 200 else None


def items(branch="wip", status=200):
    return get("ui-branch-items", branch, status)


def detail(slug, resource):
    return reverse(f"{slug}-detail", args=[resource.pk])


def stored(resource):
    resource.refresh_from_db()
    return resource


def head_component():
    return Resource.objects.get(
        resource_type__type_uri=UI_COMPONENT_TYPE_URI, content__name="head"
    )


def css_site():
    return Resource.objects.get(
        resource_type__type_uri=UI_CSS_TYPE_URI, content__name="site"
    )


def layer_sheet_item(conflict=None):
    etag, content = layer_item("stylesheet", ("layertest:sheet", ""))
    return {
        "kind": "stylesheet",
        "name": "layertest:sheet",
        "applies_to": "",
        "origin": "packaged",
        "from": None,
        "href": None,
        "etag": quoted(etag),
        "content": content,
        "conflict": conflict,
    }


@pytest.mark.django_db
@test_context
def test_branch_items_marks_own_inherited_packaged(ui_alice_login):
    row = stored(setup_branches(ui_alice_login))
    head = head_component()
    site = css_site()
    Given.http.force_authenticate(ui_alice_login)
    body = items()
    assert body == {
        "branch": "wip",
        "items": [
            {
                "kind": "component",
                "name": "head",
                "origin": "inherited",
                "from": "main",
                "href": detail("ui-components", head),
                "etag": quoted(head.etag),
                "content": head.content,
                "conflict": None,
            },
            {
                "kind": "component",
                "name": "row",
                "origin": "own",
                "from": "wip",
                "href": detail("ui-components", row),
                "etag": quoted(row.etag),
                "content": row.content,
                "conflict": "parent",
            },
            layer_sheet_item(),
            # main's CSS ``base`` is in "", which alice may not read.
            {
                "kind": "css",
                "name": "site",
                "origin": "own",
                "from": "wip",
                "href": detail("ui-css", site),
                "etag": quoted(site.etag),
                "content": site.content,
                "conflict": None,
            },
        ],
    }
    # Each item's href is its resource, which alice may GET as JSON, with
    # that content and ETag.
    for item in body["items"]:
        if item["href"] is None:
            continue
        When.http.get(item["href"], HTTP_ACCEPT=JSON)
        Then.http.status_code_is(200)
        assert Http.response["ETag"] == item["etag"]
        assert Http.response.json() == item["content"]
    # Pinned at its branching point: main's items as that version recorded
    # them, which have no resource of their own.
    pinned = items("wip@pinned")
    assert pinned["branch"] == "wip@pinned"
    [pinned_head] = [i for i in pinned["items"] if i["name"] == "head"]
    assert pinned_head["origin"] == "inherited" and pinned_head["from"] == "main"
    assert pinned_head["href"] is None
    assert pinned_head["content"]["template"] == head.content["template"]
    # main: everything is its own, but the layer's; it has no branching
    # point to pin to.
    assert items("main@pinned")["branch"] == "main"
    body = items("main")
    assert [(i["name"], i["origin"], i["from"]) for i in body["items"]] == [
        ("row", "own", "main"),
        ("head", "own", "main"),
        ("layertest:sheet", "packaged", None),
    ]
    # An ETag, and If-None-Match.
    When.http.get(url("ui-branch-items", "wip"), HTTP_ACCEPT=JSON)
    etag = Http.response["ETag"]
    When.http.get(
        url("ui-branch-items", "wip"), HTTP_ACCEPT=JSON, HTTP_IF_NONE_MATCH=etag
    )
    Then.http.status_code_is(304)


@pytest.mark.django_db
@test_context
def test_branch_items_hides_unreadable_rows(ui_alice_login):
    row = setup_branches(ui_alice_login)
    # Reads wip (a branch in "alice") and components in "alice" only.
    reader = assign_role(
        {"http.GET": [UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI]}, "alice"
    )
    Given.http.force_authenticate(reader)
    listed = items()["items"]
    assert [(i["kind"], i["name"]) for i in listed] == [
        ("component", "row"),
        ("stylesheet", "layertest:sheet"),
    ]
    # Every row left out is one the reader may not GET as JSON.
    When.http.get(detail("ui-components", head_component()), HTTP_ACCEPT=JSON)
    assert Http.response.status_code in (403, 404)
    When.http.get(detail("ui-css", css_site()), HTTP_ACCEPT=JSON)
    assert Http.response.status_code in (403, 404)
    When.http.get(detail("ui-components", row), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    # A reader of the branch alone sees the packaged items only, and none
    # of its changes or conflicts.
    branch_only = assign_role({"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(branch_only)
    assert items()["items"] == [layer_sheet_item()]
    status = get("ui-branch-status", "wip")
    assert status["changed"] == [] and status["conflicts"] == []


@pytest.mark.django_db
@test_context
def test_branch_items_reports_parent_and_layer_conflicts(ui_alice_login):
    setup_branches(ui_alice_login)
    # wip overrides the suite layer's stylesheet, based on an old ETag.
    sheet = store_item(
        "ui-stylesheets",
        "wip",
        "alice",
        name="layertest:sheet",
        rules=[],
        base="stale",
    )
    # A conflict listed on wip for an item it has no row of.
    branch = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name="wip"
    )
    ghost = {"kind": "component", "key": ["ghost"], "base": "a", "current": "b"}
    branch.content = {**branch.content, "conflicts": [CONFLICT, ghost]}
    branch.save()
    Given.http.force_authenticate(ui_alice_login)
    listed = {(i["kind"], i["name"]): i for i in items()["items"]}
    assert listed["component", "row"]["conflict"] == "parent"
    assert listed["component", "head"]["conflict"] is None
    overriding = listed["stylesheet", "layertest:sheet"]
    assert overriding["origin"] == "own"
    assert overriding["conflict"] == "layer"
    assert overriding["href"] == detail("ui-stylesheets", sheet)
    assert listed["css", "site"]["conflict"] is None
    status = get("ui-branch-status", "wip")
    assert status["conflicts"] == [
        {"kind": "component", "name": "row", "applies_to": "", "with": "parent"},
        {
            "kind": "stylesheet",
            "name": "layertest:sheet",
            "applies_to": "",
            "with": "layer",
        },
    ]


@pytest.mark.django_db
@test_context
def test_unselectable_branch_404(ui_alice_login):
    setup_branches(ui_alice_login)
    make_branch("gap", "alice", branching_point={"main": 999}, base="pinned")
    Given.http.force_authenticate(ui_alice_login)
    bodies = []
    for slug in ("ui-branch-items", "ui-branch-status"):
        # bobs is in "bob"; nope is no branch; gap is pinned to a missing
        # snapshot; the last is no branch name.
        for key in ("bobs", "nope", "gap", "No%20Such"):
            When.http.get(url(slug, key), HTTP_ACCEPT=JSON)
            Then.http.status_code_is(404)
            bodies.append(Http.response.json())
    # One answer whatever the cause (but for the path it names).
    assert len({body["title"] for body in bodies}) == 1
    # Only "all" and "mine" are keys of the catalog and the picker.
    get("ui-type-catalog", "other", status=404)
    get("ui-selectable-branches", "other", status=404)
    # Anonymous: refused.
    Given.http.force_authenticate(None)
    When.http.get(url("ui-branch-items", "wip"), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(401)


@pytest.mark.django_db
@test_context
def test_branch_status_chain_and_browse_url(ui_alice_login):
    setup_branches(ui_alice_login)
    make_branch("feat", "alice", parent="shop")
    Given.http.force_authenticate(ui_alice_login)
    status = get("ui-branch-status", "wip", type=SLUG)
    assert status == {
        "branch": "wip",
        "published": False,
        "chain": [
            {"name": "wip", "published": False, "prefix": None},
            {"name": "main", "published": True, "prefix": ""},
        ],
        "changed": [
            {"kind": "component", "name": "row", "applies_to": "", "deleted": False},
            {"kind": "css", "name": "site", "applies_to": "", "deleted": False},
        ],
        "conflicts": [
            {"kind": "component", "name": "row", "applies_to": "", "with": "parent"}
        ],
        "browse_url": reverse("todo-lists-list") + "?ui-branch=wip",
    }
    # Under a published branch: browsed under its prefix.
    status = get("ui-branch-status", "feat", type=SLUG)
    assert status["chain"] == [
        {"name": "feat", "published": False, "prefix": None},
        {"name": "shop", "published": True, "prefix": "/shop/"},
        {"name": "main", "published": True, "prefix": ""},
    ]
    assert status["browse_url"] == "/shop/todo-lists/?ui-branch=feat"
    assert status["changed"] == [] and status["conflicts"] == []
    status = get("ui-branch-status", "shop", type=SLUG)
    assert status["published"] is True
    assert status["browse_url"] == "/shop/todo-lists/?ui-branch=shop"
    # No type, or an unknown one: nothing to browse.
    assert get("ui-branch-status", "wip")["browse_url"] is None
    assert get("ui-branch-status", "wip", type="nope")["browse_url"] is None


@pytest.mark.django_db
@test_context
def test_branch_status_chain_hides_published_ancestors_not_readable(ui_alice_login):
    """A published ancestor above the branch's published root is named in
    the chain only when the user may GET it: its published root (which the
    browse URL names anyway) and main always are."""
    setup_branches(ui_alice_login)
    make_branch("vault", "vault", prefix="/vault/")
    make_branch("outlet", "project-a", parent="vault", prefix="/outlet/")
    make_branch("feat", "alice", parent="outlet")
    Given.http.force_authenticate(ui_alice_login)
    status = get("ui-branch-status", "feat", type=SLUG)
    assert status["chain"] == [
        {"name": "feat", "published": False, "prefix": None},
        {"name": "outlet", "published": True, "prefix": "/outlet/"},
        {"name": "main", "published": True, "prefix": ""},
    ]
    assert status["browse_url"] == "/outlet/todo-lists/?ui-branch=feat"
    # With GET on it, it is named.
    grant(ui_alice_login, {"http.GET": [UI_BRANCH_TYPE_URI]}, "vault")
    status = get("ui-branch-status", "feat", type=SLUG)
    assert [b["name"] for b in status["chain"]] == ["feat", "outlet", "vault", "main"]
    # The published root itself is always named, even without GET on it.
    make_branch("hidden", "nobody", prefix="/hidden/")
    make_branch("side", "alice", parent="hidden")
    status = get("ui-branch-status", "side")
    assert [b["name"] for b in status["chain"]] == ["side", "hidden", "main"]


@pytest.mark.django_db
@test_context
def test_type_catalog_matches_bootstrap_list(ui_bob_login):
    reverse("todo-lists-list")  # the URLconf registers the types
    Given.http.force_authenticate(ui_bob_login)
    body = get("ui-type-catalog", "all")
    assert body == {"types": registered_types()}
    slugs = [t["slug"] for t in body["types"]]
    assert SLUG in slugs and "ui-branch-items" in slugs
    assert {"slug", "title", "type_uri"} == set(body["types"][0])
    # As the bootstrap studio's type browser lists them.
    When.http.get(reverse("scrud-studio:types"), HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    shown = unescape(html())
    for entry in body["types"]:
        assert entry["slug"] in shown


@pytest.mark.django_db
@test_context
def test_selectable_branches_per_user(ui_alice_login, ui_bob_login):
    setup_branches(ui_alice_login)
    stored_branches = branches()
    Given.http.force_authenticate(ui_alice_login)
    assert get("ui-selectable-branches", "mine") == {
        "branches": [
            # alice may not GET main itself (in ""): no href.
            {"name": "main", "published": True, "href": None},
            {
                "name": "shop",
                "published": True,
                "href": reverse(
                    "ui-branches-detail", args=[stored_branches["shop"].pk]
                ),
            },
            {
                "name": "wip",
                "published": False,
                "href": reverse("ui-branches-detail", args=[stored_branches["wip"].pk]),
            },
        ]
    }
    Given.http.force_authenticate(ui_bob_login)
    assert get("ui-selectable-branches", "mine")["branches"] == [
        {"name": "main", "published": True, "href": None},
        {
            "name": "bobs",
            "published": False,
            "href": reverse("ui-branches-detail", args=[stored_branches["bobs"].pk]),
        },
    ]
    # Each href is a branch the user may GET as JSON.
    When.http.get(
        reverse("ui-branches-detail", args=[stored_branches["bobs"].pk]),
        HTTP_ACCEPT=JSON,
    )
    Then.http.status_code_is(200)


@pytest.mark.django_db
@test_context
def test_studio_types_render_as_html(ui_alice_login):
    setup_branches(ui_alice_login)
    make_component("branch-items", "<section>items of {{ value.branch }}</section>")
    reverse("ui-stylesheets-list")
    ResourceRegistration.register(
        {
            "name": "studio-data",
            "prefixes": {"ot": OT},
            "rules": [{"select": "ot:UIBranchItems", "component": "branch-items"}],
            "rbac_context": "",
        },
        "ui-stylesheets",
    )
    Given.http.force_authenticate(ui_alice_login)
    for slug, key in (
        ("ui-branch-items", "wip"),
        ("ui-branch-status", "wip"),
        ("ui-type-catalog", "all"),
        ("ui-selectable-branches", "mine"),
    ):
        When.http.get(url(slug, key), HTTP_ACCEPT=HTML)
        Then.http.status_code_is(200)
        assert_html()
        assert 'class="scrud-resource"' in html(), slug
    When.http.get(url("ui-branch-items", "wip"), HTTP_ACCEPT=HTML)
    assert "<section>items of wip</section>" in html()
    # Each type has its schema and JSON-LD context, linked from the
    # response.
    When.http.get(url("ui-branch-status", "wip"), HTTP_ACCEPT=JSON)
    assert 'rel="describedby"' in Http.response["Link"]


@pytest.mark.django_db
@test_context
def test_bootstrap_strip_unchanged(ui_alice_login):
    """The bootstrap strip and the computed status share one
    implementation: the same changes, conflicts and browse link (the rest
    of ``tests/test_studio_page.py`` pins the strip itself)."""
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    When.http.get(
        reverse("scrud-studio:strip", args=[SLUG]),
        {"branch": "wip"},
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    strip = html()

    def listed(ident):
        return re.findall(
            r'data-kind="([^"]*)"[^>]*data-name="([^"]*)"', section(strip, ident)
        )

    browse = unescape(
        re.search(r'<a[^>]*id="studio-browse"[^>]*href="([^"]*)"', strip).group(1)
    )
    css = re.findall(r'data-name="([^"]*)"', section(strip, "studio-css"))
    status = get("ui-branch-status", "wip", type=SLUG)
    assert listed("studio-changes") == [
        (c["kind"], c["name"]) for c in status["changed"]
    ]
    assert listed("studio-conflicts") == [
        (c["kind"], c["name"]) for c in status["conflicts"]
    ]
    assert browse == status["browse_url"]
    assert css == [i["name"] for i in items()["items"] if i["kind"] == "css"]


def test_branch_status_chain_names_are_branch_names():
    """In the status's JSON-LD context, a chain entry's ``name`` is a branch
    name (``UIBranchName``, as the picker's), a changed item's an item name
    (``UIItemName``)."""
    import importlib.resources
    import json

    from rdflib import Graph, URIRef

    context = json.loads(
        importlib.resources.files("scrud_django.ui.studio")
        .joinpath("documents/UIBranchStatus-ld.json")
        .read_text()
    )
    document = {
        "@context": context,
        "@id": "http://example.org/status",
        "branch": "feat",
        "chain": [{"name": "feat", "published": False}],
        "changed": [{"kind": "component", "name": "row"}],
        "conflicts": [{"kind": "component", "name": "clash", "with": "parent"}],
    }
    graph = Graph().parse(data=json.dumps(document), format="json-ld")

    def names(term):
        found = set()
        for _s, _p, entry in graph.triples((None, URIRef(OT + term), None)):
            for _e, predicate, value in graph.triples((entry, None, None)):
                if predicate in (
                    URIRef(OT + "UIBranchName"),
                    URIRef(OT + "UIItemName"),
                ):
                    found.add((str(predicate)[len(OT) :], str(value)))
        return found

    assert names("UIBranchChain") == {("UIBranchName", "feat")}
    assert names("UIBranchChanged") == {("UIItemName", "row")}
    assert names("UIBranchConflicts") == {("UIItemName", "clash")}
