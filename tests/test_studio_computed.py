"""The studio's data as computed resources (spec E2): a branch's effective
items, a branch's status, the type catalog and the branches the user may
select. Each lists only what the user may GET as JSON (packaged layer items
are public), and renders through the cascade like any resource. These tests
serve the API at ``/api/`` and the studio at ``/studio/``
(``tests.mounted_urls``)."""

import importlib.resources
import json
import logging
import re
from html import unescape
from unittest import mock

import fastjsonschema
import pytest
from django.urls import resolve, reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, write_db
from scrud_django.ui.branches import (
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_CSS_TYPE_URI,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.branches.store import branches
from scrud_django.ui.branches.versions import record_version
from scrud_django.ui.layers import layer_item
from scrud_django.ui.studio.views import registered_types
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_studio_page import CONFLICT, SLUG, section, setup_branches, store_item
from .test_ui_branch_push import make_wip, quoted
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


def layer_sheet_item(conflict=None, override=None):
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
        "override": override,
        "reverts_to": None,
    }


def action(method, href, body, etag=None):
    return {"method": method, "href": href, "body": body, "etag": etag}


def override_of(slug, content, branch="wip", context="alice", **fields):
    """The action that creates `branch`'s override of the item stored with
    `content` (of type `slug`): a POST of its content without ``base`` and
    ``deleted``, on `branch`, in `context`."""
    body = {k: v for k, v in content.items() if k not in ("base", "deleted")}
    body.update(branch=branch, rbac_context=context, **fields)
    return action("POST", reverse(f"{slug}-list"), body)


def send(sent):
    """Send the action `sent` as the studio layer does (an R3 fixed-body
    element): its method to its href with its body as JSON, and its etag as
    ``If-Match`` (none when it is null)."""
    headers = {} if sent["etag"] is None else {"HTTP_IF_MATCH": sent["etag"]}
    request = getattr(When.http, sent["method"].lower())
    request(sent["href"], data=sent["body"], format="json", HTTP_ACCEPT=JSON, **headers)
    return Http.response


def status(branch="wip", **params):
    return get("ui-branch-status", branch, **params)


def store_push(source, target, context, state="proposed", **fields):
    """A push stored as is (not through the API: nothing is applied)."""
    reverse("ui-pushes-list")
    return ResourceRegistration.register(
        {
            "source": source,
            "target": target,
            "restore": None,
            "state": state,
            "rbac_context": context,
            **fields,
        },
        "ui-pushes",
    )


def pushes_into(user, *contexts):
    """Let `user` create and read pushes, and create, read and update
    components, in each of `contexts` (what a push into a branch there
    needs)."""
    for context in contexts:
        grant(
            user,
            {
                "http.GET": [UI_PUSH_TYPE_URI, UI_COMPONENT_TYPE_URI],
                "http.POST": [UI_PUSH_TYPE_URI, UI_COMPONENT_TYPE_URI],
                "http.PUT": [UI_COMPONENT_TYPE_URI],
            },
            context,
        )


def conforms(name, body):
    """Validate `body` against the packaged schema of the studio's computed
    type `name`, and parse it as JSON-LD with the type's context."""
    from rdflib import Graph

    documents = importlib.resources.files("scrud_django.ui.studio.documents")
    schema = json.loads(documents.joinpath(f"{name}-schema.json").read_text())
    fastjsonschema.compile(schema)(body)
    context = json.loads(documents.joinpath(f"{name}-ld.json").read_text())
    document = {"@context": context, "@id": "http://example.org/data", **body}
    assert len(Graph().parse(data=json.dumps(document), format="json-ld")) > 0


# The studio's endpoints at /studio/ (tests.mounted_urls).
ENDPOINTS = {
    "live_preview": "/studio/live-preview",
    "explain": "/studio/types/{slug}/explain",
    "options": "/studio/types/{slug}/new-component/options",
    "new_component": "/studio/new-component",
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
                # An inherited item: the POST that overrides it on wip.
                "override": override_of("ui-components", head.content),
                "reverts_to": None,
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
                # wip's own: nothing to override; deleting it shows main's.
                "override": None,
                "reverts_to": "inherited",
            },
            layer_sheet_item(
                override=override_of(
                    "ui-stylesheets",
                    layer_item("stylesheet", ("layertest:sheet", ""))[1],
                    applies_to="",
                )
            ),
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
                "override": None,
                # wip added it: deleting it shows nothing in its place.
                "reverts_to": None,
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
    wip = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name="wip"
    )
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
        # alice may PUT wip (in "alice"), not branches in "".
        "may_write": True,
        "may_publish": False,
        # No GET on pushes or on versions.
        "pushes": [],
        "versions": [],
        "actions": {
            "pull": action(
                "PUT",
                detail("ui-branches", wip),
                {**wip.content, "pull": True},
                quoted(wip.etag),
            ),
            # No POST on pushes in "".
            "promote": [{"target": "main", "apply": None, "propose": None}],
        },
        "endpoints": ENDPOINTS,
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


@pytest.mark.django_db
@test_context
def test_status_of_a_branch_with_no_published_root_is_logged(ui_alice_login, caplog):
    """`selectable` refuses a branch whose chain is broken, so the status
    always finds its published root; should it not, the status answers
    without the root (a browse link to the API's own URL) and logs a
    warning, never silently."""
    from scrud_django.ui.branches.store import BranchChainError

    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    with (
        mock.patch(
            "scrud_django.ui.studio.computed.published_root",
            side_effect=BranchChainError("Branch 'wip' has no published ancestor."),
        ),
        caplog.at_level(logging.WARNING, logger="scrud_django.ui.studio.computed"),
    ):
        status = get("ui-branch-status", "wip", type=SLUG)
    assert status["branch"] == "wip"
    assert status["browse_url"] == "/api/todo-lists/?ui-branch=wip"
    logged = [
        r.getMessage()
        for r in caplog.records
        if r.name == "scrud_django.ui.studio.computed" and r.levelno == logging.WARNING
    ]
    assert len(logged) == 1
    assert "'wip'" in logged[0] and "no published ancestor" in logged[0]


@pytest.mark.django_db
@test_context
def test_override_body_creates_the_override(ui_alice_login):
    """An item wip shows but does not own carries the POST that creates
    wip's override of it: sent as it is, it creates the override, and the
    server sets its ``base`` (an inherited item's ETag; a packaged item's,
    its layer item's)."""
    setup_branches(ui_alice_login)
    head = head_component()
    sheet_etag, _sheet = layer_item("stylesheet", ("layertest:sheet", ""))
    Given.http.force_authenticate(ui_alice_login)
    listed = {(i["kind"], i["name"]): i for i in items()["items"]}
    inherited = listed["component", "head"]["override"]
    assert inherited["method"] == "POST" and inherited["etag"] is None
    assert inherited["href"] == reverse("ui-components-list")
    assert "base" not in inherited["body"]
    response = send(inherited)
    Then.http.status_code_is(201)
    created = response.json()
    assert created["base"] == head.etag
    assert created["branch"] == "wip" and created["rbac_context"] == "alice"
    assert created["template"] == head.content["template"]
    packaged = listed["stylesheet", "layertest:sheet"]["override"]
    assert packaged["href"] == reverse("ui-stylesheets-list")
    response = send(packaged)
    Then.http.status_code_is(201)
    created = response.json()
    assert created["base"] == sheet_etag
    assert (created["name"], created["applies_to"]) == ("layertest:sheet", "")
    assert created["branch"] == "wip" and created["rbac_context"] == "alice"
    # Both are now wip's own: no override is offered for them.
    listed = {(i["kind"], i["name"]): i for i in items()["items"]}
    for key in (("component", "head"), ("stylesheet", "layertest:sheet")):
        assert listed[key]["origin"] == "own"
        assert listed[key]["override"] is None
    # main's head is untouched.
    assert stored(head).etag == head.etag


@pytest.mark.django_db
@test_context
def test_override_null_on_published_or_without_grant(ui_alice_login):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    # A published branch is written by a push, never by an override.
    for published in ("main", "shop"):
        listed = items(published)["items"]
        assert listed, published
        assert [i["override"] for i in listed] == [None] * len(listed)
    # A reader of wip and of components who may POST none.
    reader = assign_role(
        {"http.GET": [UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI]}, "alice"
    )
    grant(reader, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(reader)
    listed = {(i["kind"], i["name"]): i for i in items()["items"]}
    assert listed["component", "head"]["origin"] == "inherited"
    assert listed["stylesheet", "layertest:sheet"]["origin"] == "packaged"
    assert [i["override"] for i in listed.values()] == [None] * len(listed)
    # POST on stylesheets in "alice" alone: the packaged stylesheet's is
    # offered, the inherited component's is not.
    grant(reader, {"http.POST": [UI_STYLESHEET_TYPE_URI]}, "alice")
    listed = {(i["kind"], i["name"]): i for i in items()["items"]}
    assert listed["component", "head"]["override"] is None
    assert listed["stylesheet", "layertest:sheet"]["override"] is not None


@pytest.mark.django_db
@test_context
def test_status_may_flags(ui_alice_login):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)

    def flags(branch):
        found = status(branch)
        return found["may_write"], found["may_publish"]

    # PUT on wip's context; none in "".
    assert flags("wip") == (True, False)
    # A published branch is not written in the studio.
    assert flags("shop") == (False, False)
    assert flags("main") == (False, False)
    # A reader of wip may not write it.
    reader = assign_role({"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(reader)
    assert flags("wip") == (False, False)
    # PUT on branches in "" publishes, on any branch.
    grant(reader, {"http.PUT": [UI_BRANCH_TYPE_URI]}, "")
    assert flags("wip") == (False, True)
    Given.http.force_authenticate(ui_alice_login)
    grant(ui_alice_login, {"http.PUT": [UI_BRANCH_TYPE_URI]}, "")
    assert flags("wip") == (True, True)
    assert flags("shop") == (False, True)


@pytest.mark.django_db
@test_context
def test_status_pushes_and_versions_readable_only(ui_alice_login):
    setup_branches(ui_alice_login)
    hidden = store_push("wip", "main", "")
    store_push("bobs", "shop", "project-a")  # not wip's
    many = [store_push("wip", "shop", "project-a", label=f"p{i}") for i in range(21)]
    restore = store_push(None, "wip", "alice", restore=1)
    for _ in range(21):
        record_version("wip", write_db())
    Given.http.force_authenticate(ui_alice_login)
    # No GET on pushes nor on versions: none listed.
    found = status()
    assert found["pushes"] == [] and found["versions"] == []
    grant(ui_alice_login, {"http.GET": [UI_PUSH_TYPE_URI]}, "project-a")
    grant(ui_alice_login, {"http.GET": [UI_PUSH_TYPE_URI]}, "alice")
    grant(ui_alice_login, {"http.GET": [UI_BRANCH_VERSION_TYPE_URI]}, "alice")
    found = status()

    def listed(push):
        content = push.content
        return {
            "href": detail("ui-pushes", push),
            "source": content["source"],
            "target": content["target"],
            "state": content["state"],
            "label": content.get("label"),
        }

    # The latest 20 with wip as source or target that alice may GET, newest
    # first: the one in "" is not among them.
    assert found["pushes"] == [listed(p) for p in [restore, *reversed(many)][:20]]
    for push in found["pushes"]:
        When.http.get(push["href"], HTTP_ACCEPT=JSON)
        Then.http.status_code_is(200)
    When.http.get(detail("ui-pushes", hidden), HTTP_ACCEPT=JSON)
    assert Http.response.status_code in (403, 404)
    # wip's latest 20 versions, newest first, each one alice may GET.
    versions = Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI, content__branch="wip"
    ).order_by("-pk")[:20]
    assert len(versions) == 20
    assert found["versions"] == [
        {"number": v.content["number"], "href": detail("ui-branch-versions", v)}
        for v in versions
    ]
    numbers = [v["number"] for v in found["versions"]]
    assert numbers == sorted(numbers, reverse=True)
    for version in found["versions"]:
        When.http.get(version["href"], HTTP_ACCEPT=JSON)
        Then.http.status_code_is(200)
        assert Http.response.json()["number"] == version["number"]
    # main's versions are in "", which alice may not read.
    assert status("main")["versions"] == []


@pytest.mark.django_db
@test_context
def test_status_actions_round_trip(admin_login, ui_alice_login):
    """Each action, sent as the layer sends it, succeeds: the pull, a
    proposal and a push to the parent, and a push to a further ancestor."""
    main_row = make_component("row", "<b>main</b>")
    make_branch("team", "team", prefix="/team/")
    make_wip(admin_login, parent="team")
    Then.http.status_code_is(201)
    pushes_into(ui_alice_login, "team", "")
    Given.http.force_authenticate(ui_alice_login)
    # wip overrides main's row (which team shows), then changes it.
    listed = {(i["kind"], i["name"]): i for i in items()["items"]}
    assert listed["component", "row"]["from"] == "main"
    response = send(listed["component", "row"]["override"])
    Then.http.status_code_is(201)
    override = response.json()
    When.http.put(
        response["Location"],
        data={**override, "template": "<i>wip</i>"},
        format="json",
        HTTP_IF_MATCH=response["ETag"],
    )
    Then.http.status_code_is(200)
    # The pull: wip's PUT with pull, and its ETag.
    found = status()
    assert found["may_write"] is True
    pull = found["actions"]["pull"]
    assert pull["method"] == "PUT" and pull["body"]["pull"] is True
    response = send(pull)
    Then.http.status_code_is(200)
    assert "pull" not in response.json()
    # Promote: to each ancestor, nearest first.
    found = status()
    conforms("UIBranchStatus", found)
    promote = found["actions"]["promote"]
    assert [entry["target"] for entry in promote] == ["team", "main"]
    pushes = reverse("ui-pushes-list")
    for entry, context in zip(promote, ("team", ""), strict=True):
        for name, state in (("apply", "applied"), ("propose", "proposed")):
            assert entry[name] == action(
                "POST",
                pushes,
                {
                    "source": "wip",
                    "target": entry["target"],
                    "state": state,
                    "rbac_context": context,
                },
            )
    # Apply to main, a further ancestor.
    response = send(promote[1]["apply"])
    Then.http.status_code_is(201)
    assert response.json()["state"] == "applied"
    assert stored(main_row).content["template"] == "<i>wip</i>"
    # Propose to team, the parent.
    response = send(status()["actions"]["promote"][0]["propose"])
    Then.http.status_code_is(201)
    assert response.json()["state"] == "proposed"
    # Override main's row again, and apply to team.
    body = items()
    conforms("UIBranchItems", body)
    listed = {(i["kind"], i["name"]): i for i in body["items"]}
    send(listed["component", "row"]["override"])
    Then.http.status_code_is(201)
    response = send(status()["actions"]["promote"][0]["apply"])
    Then.http.status_code_is(201)
    assert response.json()["state"] == "applied"
    team_rows = Resource.objects.filter(
        resource_type__type_uri=UI_COMPONENT_TYPE_URI, content__branch="team"
    )
    assert [r.content["template"] for r in team_rows] == ["<i>wip</i>"]
    # The pushes alice made (she may GET pushes in "team" and ""), newest
    # first.
    found = status()
    conforms("UIBranchStatus", found)
    assert [(p["target"], p["state"]) for p in found["pushes"]] == [
        ("team", "applied"),
        ("team", "proposed"),
        ("main", "applied"),
    ]


@pytest.mark.django_db
@test_context
def test_status_actions_null_on_published_T(ui_alice_login):
    setup_branches(ui_alice_login)
    make_branch("team", "alice", parent="shop", prefix="/team/")
    pushes_into(ui_alice_login, "", "project-a", "alice")
    grant(ui_alice_login, {"http.PUT": [UI_BRANCH_TYPE_URI]}, "project-a")
    Given.http.force_authenticate(ui_alice_login)
    # Published: no action, whatever the grants.
    for published in ("main", "shop", "team"):
        found = status(published)
        assert found["published"] is True
        assert found["may_write"] is False
        assert found["actions"] == {"pull": None, "promote": []}, published
    # A working branch with the same grants has them.
    found = status()
    assert found["actions"]["pull"] is not None
    assert [e["target"] for e in found["actions"]["promote"]] == ["main"]
    assert found["actions"]["promote"][0]["apply"] is not None
    # Without PUT on wip, and without POST on pushes: none.
    reader = assign_role({"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(reader)
    assert status()["actions"] == {
        "pull": None,
        "promote": [{"target": "main", "apply": None, "propose": None}],
    }


@pytest.mark.django_db
@test_context
def test_endpoints_in_status_reverse_correctly(ui_alice_login, settings):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    endpoints = status()["endpoints"]
    assert endpoints == ENDPOINTS
    assert endpoints["live_preview"] == reverse("scrud-studio:live-preview")
    assert endpoints["new_component"] == reverse("scrud-studio:new-component")
    # The slug-taking routes are templates: {slug} is the type's slug.
    assert endpoints["explain"].replace("{slug}", SLUG) == reverse(
        "scrud-studio:explain", args=[SLUG]
    )
    assert endpoints["options"].replace("{slug}", SLUG) == reverse(
        "scrud-studio:new-component-options", args=[SLUG]
    )
    for name, path in endpoints.items():
        match = resolve(path.replace("{slug}", SLUG))
        assert match.namespace == "scrud-studio", name
    # Without the studio's URLs (tests.urls), there are none.
    settings.ROOT_URLCONF = "tests.urls"
    found = status()
    assert found["endpoints"] is None
    conforms("UIBranchStatus", found)


@pytest.mark.django_db
@test_context
def test_override_null_on_a_pinned_view(ui_alice_login):
    """A ``@pinned`` view shows the parent's items as of the branching
    point, but an override is based on the live parent item: sent, it would
    silently revert the parent's changes since. None is offered there."""
    setup_branches(ui_alice_login)
    head = head_component()
    ResourceRegistration.update(
        {**head.content, "template": "<h1>changed</h1>"}, "ui-components", head.pk
    )
    Given.http.force_authenticate(ui_alice_login)
    pinned = {(i["kind"], i["name"]): i for i in items("wip@pinned")["items"]}
    assert pinned["component", "head"]["origin"] == "inherited"
    assert pinned["component", "head"]["content"]["template"] == "<h1>main</h1>"
    assert [i["override"] for i in pinned.values()] == [None] * len(pinned)
    # Live, the same item's override is offered, with the live content.
    live = {(i["kind"], i["name"]): i for i in items()["items"]}
    offered = live["component", "head"]["override"]
    assert offered["body"]["template"] == "<h1>changed</h1>"
    # The status of a pinned view keeps its actions: they act on the
    # branch's stored rows, not on what the snapshot shows.
    assert status("wip@pinned")["actions"] == status()["actions"]
