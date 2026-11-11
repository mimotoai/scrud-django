"""Packaged UI layers (R1): items an installed app ships as package files,
beneath every branch, overridable by name. The suite runs with the test layer
``layertest:`` (`tests.ui_layer`, one stylesheet: its rule takes to-do lists
in the scope ``layertest`` to ``scrud:json``) installed; ``layermore:`` (a
component, a stylesheet whose rule takes the same lists to it, CSS and JS) and
``layerbad:`` are installed only here."""

import hashlib
import importlib.resources
import json
import logging
import re

import pytest
from django.conf import settings
from django.core import checks
from django.test import override_settings
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches import UI_CSS_TYPE_URI, UI_JS_TYPE_URI
from scrud_django.ui.branches.effective import Selection, effective_rows
from scrud_django.ui.cascade import effective_rules, rank_rules, rules_memo
from scrud_django.ui.layers import (
    LAYER_BRANCH_NAME,
    NAME_PATTERN,
    UILayer,
    check_layers,
    installed_layers,
    layer_item,
    layer_rows,
)
from scrud_django.ui.selectors import TypeTerm
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import may_select_branches, put_item
from .test_ui_branch_types import create, make_branch, problem
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, detail_url, html, make_component, make_stylesheet

LAYER_APP = "tests.apps.UILayerTestConfig"
MORE_APP = "tests.apps.UILayerMoreConfig"
BAD_APP = "tests.apps.UILayerBadConfig"
INVALID_APP = "tests.apps.UILayerInvalidConfig"
EX = "http://example.org/todo#"
TERMS = [TypeTerm(iri=EX + "TodoList"), TypeTerm(schema_type="object")]
# The suite layer's rule's output.
LAYER_JSON = '<pre class="scrud-json">'
MORE_LIST = '<p class="layermore">Groceries</p>'
OVERRIDE = '<p class="override">Groceries</p>'


def installed(*extra, without=()):
    apps = [app for app in settings.INSTALLED_APPS if app not in without]
    return override_settings(INSTALLED_APPS=[*apps, *extra])


def render(todo_list, scope="layertest", branch=None):
    """The fragment of `todo_list` in `scope`, on `branch`."""
    headers = {"HTTP_HX_REQUEST": "true"}
    if branch is not None:
        headers["HTTP_SCRUD_UI_BRANCH"] = branch
    query = f"?ui-scope={scope}" if scope else ""
    When.http.get(detail_url(todo_list) + query, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    return html()


def page(todo_list):
    """The page of `todo_list`, without its (per request) CSRF token."""
    When.http.get(detail_url(todo_list), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    return re.sub(r'name="csrf-token" content="[^"]*"', "", html())


def override_body(**fields):
    return {
        "name": "layermore:list",
        "engine": "django",
        "mode": "view",
        "template": '<p class="override">{{ value.title }}</p>',
        "rbac_context": "",
        **fields,
    }


def canonical_etag(content):
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def test_installed_layers_and_rows():
    suite_layer = UILayer(
        label="tests_ui_layer", prefix="layertest:", package="tests.ui_layer"
    )
    assert installed_layers() == (suite_layer,)
    [(pk, _etag, sheet)] = layer_rows(UI_STYLESHEET_TYPE_URI)
    assert pk is None and sheet["name"] == "layertest:sheet"
    assert sheet["applies_to"] == ""
    assert layer_rows(UI_COMPONENT_TYPE_URI) == [] == layer_rows("http://other")
    with installed(MORE_APP):
        assert installed_layers() == (
            suite_layer,
            UILayer(
                label="tests_ui_layer_more",
                prefix="layermore:",
                package="tests.ui_layer_more",
            ),
        )
        [(pk, etag, content)] = layer_rows(UI_COMPONENT_TYPE_URI)
        assert pk is None
        assert content["name"] == "layermore:list"
        assert content["branch"] == LAYER_BRANCH_NAME == "@layers"
        assert content["rbac_context"] == ""
        packaged = {
            "name": "layermore:list",
            "engine": "django",
            "mode": "view",
            "template": '<p class="layermore">{{ value.title }}</p>',
        }
        assert etag == canonical_etag(packaged)
        assert layer_item("component", ("layermore:list",)) == (etag, content)
        assert layer_item("stylesheet", ("layermore:sheet", ""))[1]["rules"]
        assert layer_item("stylesheet", ("layermore:sheet", "acme")) is None
        assert layer_item("component", ("layermore:sheet",)) is None
        names = [row[2]["name"] for row in layer_rows(UI_STYLESHEET_TYPE_URI)]
        assert names == ["layertest:sheet", "layermore:sheet"]
        # Each call hands out its own copy.
        content["template"] = "changed"
        assert layer_rows(UI_COMPONENT_TYPE_URI)[0][2]["template"] != "changed"
    assert layer_item("component", ("layermore:list",)) is None


@pytest.mark.django_db
@test_context
def test_layer_items_render_beneath_main(todo_list, todo_reader_login):
    Given.http.force_authenticate(todo_reader_login)
    # The suite layer's rule, with a built-in.
    assert LAYER_JSON in render(todo_list)
    # Inert outside its scope.
    assert LAYER_JSON not in render(todo_list, scope=None)
    with installed(MORE_APP):
        # A layer's rule and component.
        output = render(todo_list)
        assert MORE_LIST in output and LAYER_JSON not in output
        assert MORE_LIST not in render(todo_list, scope=None)
        # A stored item on main of another name changes nothing.
        make_component("row", "<b>{{ value.text }}</b>")
        assert MORE_LIST in render(todo_list)


@pytest.mark.django_db
@test_context
def test_branch_overrides_layer_item_by_name(todo_list, admin_login, todo_reader_login):
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        Given.http.force_authenticate(admin_login)
        create("ui-components", override_body(branch="wip", rbac_context="project-a"))
        Then.http.status_code_is(201)
        # Its base is the layer item's ETag: the layer is beneath main.
        etag, _content = layer_item("component", ("layermore:list",))
        assert Http.response.json()["base"] == etag
        Given.http.force_authenticate(may_select_branches(todo_reader_login))
        output = render(todo_list, branch="wip")
        assert OVERRIDE in output and MORE_LIST not in output
        assert MORE_LIST in render(todo_list)  # main still shows the layer's


@pytest.mark.django_db
@test_context
def test_deleting_override_reverts_to_layer(todo_list, admin_login, todo_reader_login):
    with installed(MORE_APP):
        Given.http.force_authenticate(admin_login)
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        location = Http.response["Location"]
        etag = Http.response["ETag"]
        Given.http.force_authenticate(todo_reader_login)
        output = render(todo_list)
        assert OVERRIDE in output and MORE_LIST not in output
        Given.http.force_authenticate(admin_login)
        When.http.delete(location, HTTP_IF_MATCH=etag)
        Then.http.status_code_is(204)
        Given.http.force_authenticate(todo_reader_login)
        assert MORE_LIST in render(todo_list)


@pytest.mark.django_db
def test_layer_stylesheet_ranks_between_default_and_stored():
    make_component("stored-list", "<p>{{ value.title }}</p>")
    sheet = make_stylesheet("s", [("layertest ex:TodoList", "stored-list")])
    with rules_memo():
        rules = effective_rules([])
        ranking = rank_rules(rules, ("layertest",), TERMS, "view")
    by_sheet = {}
    for rule in rules:
        by_sheet.setdefault(rule.source.name, []).append(rule)
    default = by_sheet["scrud:default"][0]
    [layer] = by_sheet["layertest:sheet"]
    [stored] = by_sheet["s"]
    assert default.rank == (0, 0, 0)
    assert layer.rank == (0, 1, 0, 0, 0)
    assert stored.rank == (1, 0, sheet.pk, 0)
    assert default.rank < layer.rank < stored.rank
    assert layer.source.pk is None
    assert layer.source.branch == LAYER_BRANCH_NAME
    assert layer.source.applies_to == ""
    assert layer.source.content["name"] == "layertest:sheet"
    # Same selector: the stored stylesheet beats the layer's.
    ranked = [rule for _key, rule in ranking.ranked]
    assert ranked[:2] == [stored, layer]
    assert ranked[ranking.winner].component == "stored-list"


@pytest.mark.django_db
@test_context
def test_layers_stack_in_installed_apps_order_and_ship_css_and_js(
    todo_list, todo_reader_login
):
    with installed(MORE_APP):
        assert [layer.prefix for layer in installed_layers()] == [
            "layertest:",
            "layermore:",
        ]
        with rules_memo():
            ranks = {
                rule.source.name: rule.rank
                for rule in effective_rules([])
                if rule.source.branch == LAYER_BRANCH_NAME
            }
        assert ranks == {
            "layertest:sheet": (0, 1, 0, 0, 0),
            "layermore:sheet": (0, 1, 1, 0, 0),
        }
        Given.http.force_authenticate(todo_reader_login)
        # The later layer's rule wins the same selector.
        output = render(todo_list)
        assert MORE_LIST in output and LAYER_JSON not in output
        # Its CSS and JS items are linked and served.
        output = page(todo_list)
        [(_pk, css_etag, _css)] = effective_rows(UI_CSS_TYPE_URI, Selection())
        [(_pk, js_etag, _js)] = effective_rows(UI_JS_TYPE_URI, Selection())
        css_url = reverse("ui-css-file", args=["layermore:theme", css_etag])
        js_url = reverse("ui-js-file", args=["layermore:app", js_etag])
        assert css_url in output and js_url in output
        When.http.get(css_url)
        Then.http.status_code_is(200)
        assert Http.response.content == b".layermore { color: teal; }"
        When.http.get(js_url)
        Then.http.status_code_is(200)
        assert Http.response.content == b"window.layermore = true;"
    assert [layer.prefix for layer in installed_layers()] == ["layertest:"]


@pytest.mark.django_db
@test_context
def test_new_item_cannot_use_layer_prefix(admin_login):
    Given.http.force_authenticate(admin_login)
    refused = [
        ("ui-components", override_body(name="layermore:new")),
        # A layer stylesheet's name, but a component.
        ("ui-components", override_body(name="layermore:sheet")),
        ("ui-stylesheets", {"name": "layermore:list", "rules": [], "rbac_context": ""}),
        # A layer stylesheet's name, but applying to a context.
        (
            "ui-stylesheets",
            {
                "name": "layertest:sheet",
                "rules": [],
                "rbac_context": "acme",
                "applies_to": "acme",
            },
        ),
        ("ui-css", {"name": "layermore:app", "css": "", "rbac_context": ""}),
        ("ui-js", {"name": "layermore:theme", "js": "", "rbac_context": ""}),
    ]
    with installed(MORE_APP):
        for slug, body in refused:
            create(slug, body)
            Then.http.status_code_is(400)
            assert problem()["type"] == (
                "https://api.openteams.com/http-problem/ui-layer-name-reserved"
            ), (slug, body)
            assert body["name"] in problem()["detail"]
        stored = Resource.objects.filter(
            resource_type__type_uri__in=[
                UI_COMPONENT_TYPE_URI,
                UI_STYLESHEET_TYPE_URI,
                UI_CSS_TYPE_URI,
                UI_JS_TYPE_URI,
            ]
        ).values_list("content", flat=True)
        assert not [content for content in stored if ":" in content["name"]]
        # Overrides of existing layer items, of their kind, are accepted.
        for slug, body in (
            ("ui-components", override_body()),
            ("ui-css", {"name": "layermore:theme", "css": "", "rbac_context": ""}),
            ("ui-js", {"name": "layermore:app", "js": "", "rbac_context": ""}),
            (
                "ui-stylesheets",
                {"name": "layertest:sheet", "rules": [], "rbac_context": ""},
            ),
        ):
            create(slug, body)
            Then.http.status_code_is(201)
        # Renaming an item onto an unknown layer name is refused too.
        location, etag = Http.response["Location"], Http.response["ETag"]
        When.http.put(
            location,
            data={"name": "layertest:other", "rules": [], "rbac_context": ""},
            format="json",
            HTTP_IF_MATCH=etag,
        )
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-layer-name-reserved")


@pytest.mark.django_db
@test_context
def test_prefixed_name_with_a_malformed_body_is_a_400(admin_login):
    Given.http.force_authenticate(admin_login)
    for applies_to in (["acme"], {"a": 1}, 3):
        create(
            "ui-stylesheets",
            {
                "name": "layertest:sheet",
                "rules": [],
                "rbac_context": "",
                "applies_to": applies_to,
            },
        )
        Then.http.status_code_is(400)


@pytest.mark.django_db
@test_context
def test_scrud_prefix_still_reserved(admin_login):
    Given.http.force_authenticate(admin_login)
    for slug, body in (
        ("ui-components", override_body(name="scrud:object")),
        ("ui-components", override_body(name="scrud:new")),
        ("ui-stylesheets", {"name": "scrud:default", "rules": [], "rbac_context": ""}),
        ("ui-css", {"name": "scrud:x", "css": "", "rbac_context": ""}),
        ("ui-js", {"name": "scrud:x", "js": "", "rbac_context": ""}),
    ):
        create(slug, body)
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-layer-name-reserved"), body
    # The item schemas share the name pattern, which refuses it too.
    for document in ("UIComponent", "UIStylesheet", "UICss", "UIJs"):
        schema = json.loads(
            importlib.resources.files("scrud_django.ui.documents")
            .joinpath(f"{document}-schema.json")
            .read_text(encoding="utf-8")
        )
        assert schema["properties"]["name"]["pattern"] == NAME_PATTERN.pattern
    assert not NAME_PATTERN.match("scrud:object")
    assert NAME_PATTERN.match("layertest:list")
    assert NAME_PATTERN.match("plain.name-1")
    for bad in ("Layer:x", "a:b:c", ":x", "a:", "x" * 129):
        assert not NAME_PATTERN.match(bad), bad


@pytest.mark.django_db
@test_context
def test_unknown_prefix_refused(admin_login):
    make_branch("wip", "project-a")
    Given.http.force_authenticate(admin_login)
    for body in (
        override_body(name="nolayer:list"),
        override_body(name="nolayer:list", branch="wip", rbac_context="project-a"),
        # The layer's items exist only while it is installed.
        override_body(name="layermore:list"),
    ):
        create("ui-components", body)
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-layer-name-reserved"), body
    with installed(MORE_APP):
        create("ui-components", override_body(name="layermore:list"))
        Then.http.status_code_is(201)


@pytest.mark.django_db
@test_context
def test_layer_items_not_in_lists(admin_login):
    make_branch("wip", "project-a")
    make_component("row", "<b>{{ value.text }}</b>")
    Given.http.force_authenticate(admin_login)
    with installed(MORE_APP):
        for slug in ("ui-components", "ui-stylesheets", "ui-css", "ui-js"):
            for query in ("", "?ui-branch=wip"):
                When.http.get(reverse(f"{slug}-list") + query)
                Then.http.status_code_is(200)
                names = [
                    item["content"]["name"] for item in Http.response.json()["content"]
                ]
                assert not [n for n in names if ":" in n], (slug, query, names)
        When.http.get(reverse("ui-components-list"))
        listed = Http.response.json()["content"]
        assert [item["content"]["name"] for item in listed] == ["row"]


@pytest.mark.django_db
def test_layer_check_reports_bad_files():
    assert check_layers(None) == []
    with installed(BAD_APP):
        errors = check_layers(None)
        assert {error.id for error in errors} == {"scrud_django_ui.E002"}
        text = "\n".join(error.msg for error in errors)
        assert len(errors) == 3, text
        assert "component/unprefixed.json" in text and "layerbad:" in text
        assert "stylesheet/applies.json" in text and "applies_to" in text
        assert "css/broken.json" in text
        # The check is registered, and not as a database check.
        found = [e for e in checks.run_checks() if e.id == "scrud_django_ui.E002"]
        assert len(found) == 3
        # At runtime the bad items are left out, the good one kept.
        rows = layer_rows(UI_COMPONENT_TYPE_URI)
        assert [content["name"] for _pk, _etag, content in rows] == ["layerbad:good"]
        assert layer_rows(UI_STYLESHEET_TYPE_URI)[0][2]["name"] == "layertest:sheet"
        assert len(layer_rows(UI_STYLESHEET_TYPE_URI)) == 1
        assert layer_rows(UI_CSS_TYPE_URI) == []
    assert check_layers(None) == []


@pytest.mark.django_db
def test_layer_check_validates_content_as_the_viewsets_do():
    """A layer file the viewsets would refuse is an E002 and left out: one
    its kind's JSON Schema refuses, one without the fields its kind needs,
    a component whose template does not compile, and a stylesheet whose
    rule's selector does not parse. So the runtime's fallbacks (a skipped
    rule, a component that fails to compile) never apply to a packaged
    item."""
    with installed(INVALID_APP):
        errors = check_layers(None)
        assert {error.id for error in errors} == {"scrud_django_ui.E002"}
        text = "\n".join(error.msg for error in errors)
        by_file = {
            name: [error.msg for error in errors if f"/{name}" in error.msg]
            for name in (
                "component/engine.json",
                "component/missing.json",
                "component/django.json",
                "component/jinja.json",
                "stylesheet/selector.json",
                "stylesheet/rule.json",
                "css/number.json",
            )
        }
        assert all(len(found) == 1 for found in by_file.values()), text
        assert len(errors) == len(by_file), text
        assert "JSON Schema" in by_file["component/engine.json"][0]
        assert "'template'" in by_file["component/missing.json"][0]
        assert "does not compile" in by_file["component/django.json"][0]
        assert "does not compile" in by_file["component/jinja.json"][0]
        assert "'1bad'" in by_file["stylesheet/selector.json"][0]
        assert "JSON Schema" in by_file["stylesheet/rule.json"][0]
        assert "JSON Schema" in by_file["css/number.json"][0]
        rows = layer_rows(UI_COMPONENT_TYPE_URI)
        assert [content["name"] for _pk, _etag, content in rows] == [
            "layerinvalid:good"
        ]
        assert [c["name"] for _p, _e, c in layer_rows(UI_STYLESHEET_TYPE_URI)] == [
            "layertest:sheet"
        ]
        assert layer_rows(UI_CSS_TYPE_URI) == []


def test_layer_errors_are_logged_once_when_loaded(caplog):
    """Each E002 is also logged at ERROR when the layers are read (once:
    they are read once), so a deployment that runs no checks still says
    why an item is missing."""
    with installed(BAD_APP, INVALID_APP):
        with caplog.at_level(logging.ERROR, logger="scrud_django.ui.layers"):
            layer_rows(UI_COMPONENT_TYPE_URI)
            layer_rows(UI_STYLESHEET_TYPE_URI)
            installed_layers()
        logged = [
            r.getMessage()
            for r in caplog.records
            if r.name == "scrud_django.ui.layers" and r.levelno == logging.ERROR
        ]
        assert sorted(logged) == sorted(
            f"scrud-ui: {error.msg}" for error in check_layers(None)
        )
        assert len(logged) == 10


def test_unreadable_layer_folder_is_an_error(monkeypatch):
    """A layer folder that cannot be listed (an ``OSError``) is an E002 for
    that folder, not an exception at first use: the layer's other folders
    still load."""
    real = importlib.resources.files

    class Unlistable:
        def __init__(self, path):
            self.path = path

        def joinpath(self, name):
            inner = self.path.joinpath(name)
            if name == "component":
                return Unlistable(inner)
            return inner

        def is_dir(self):
            return True

        def iterdir(self):
            raise PermissionError(13, "Permission denied", str(self.path))

    def files(package):
        root = real(package)
        return Unlistable(root) if package == "tests.ui_layer_more" else root

    monkeypatch.setattr(importlib.resources, "files", files)
    with installed(MORE_APP):
        errors = check_layers(None)
        assert [error.id for error in errors] == ["scrud_django_ui.E002"]
        assert "tests.ui_layer_more/component" in errors[0].msg
        assert "Permission denied" in errors[0].msg
        assert layer_rows(UI_COMPONENT_TYPE_URI) == []
        assert [c["name"] for _p, _e, c in layer_rows(UI_STYLESHEET_TYPE_URI)] == [
            "layertest:sheet",
            "layermore:sheet",
        ]


@pytest.mark.parametrize(
    "declaration,reason",
    [
        ({"prefix": "scrud:", "package": "tests.ui_layer"}, "prefix"),
        ({"prefix": "Bad:", "package": "tests.ui_layer"}, "prefix"),
        ({"prefix": "layertest:", "package": "tests.ui_layer"}, "already"),
        ({"prefix": "nopkg:", "package": "tests.no_such_package"}, "package"),
        ("layer", "mapping"),
    ],
)
def test_layer_check_reports_bad_declarations(declaration, reason, monkeypatch):
    from tests.apps import UILayerMoreConfig

    monkeypatch.setattr(UILayerMoreConfig, "scrud_ui_layer", declaration)
    with installed(MORE_APP):
        errors = check_layers(None)
        assert [error.id for error in errors] == ["scrud_django_ui.E002"]
        assert reason in errors[0].msg
        assert "tests_ui_layer_more" in errors[0].msg
        if reason != "package":  # left out
            assert [layer.prefix for layer in installed_layers()] == ["layertest:"]


@pytest.mark.django_db
@test_context
def test_pinned_branch_sees_layer(todo_list, todo_reader_login):
    make_component("row", "<b>{{ value.text }}</b>")
    make_branch(
        "wip",
        "project-a",
        base="pinned",
        branching_point={"main": versions_of("main")[-1]},
    )
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")
    make_branch("live", "project-a", branching_point={"main": versions_of("main")[-1]})
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert LAYER_JSON in render(todo_list, branch="wip")
    with installed(MORE_APP):
        output = render(todo_list, branch="wip")
        assert MORE_LIST in output and "<i>Buy milk</i>" not in output
        # And a live branch viewed as pinned.
        assert MORE_LIST in render(todo_list, branch="live@pinned")


@pytest.mark.django_db
@test_context
def test_no_layer_installed_changes_nothing(todo_list, todo_reader_login):
    make_component("row", "<b>{{ value.text }}</b>")
    make_stylesheet("s", [("ex:TodoItem", "row")])
    Given.http.force_authenticate(todo_reader_login)
    with_layer = page(todo_list), render(todo_list, scope=None)
    with rules_memo():
        layered_rules = effective_rules([])
    with installed(without=[LAYER_APP]):
        assert installed_layers() == ()
        assert layer_rows(UI_COMPONENT_TYPE_URI) == []
        assert (page(todo_list), render(todo_list, scope=None)) == with_layer
        output = render(todo_list)
        assert LAYER_JSON not in output and "scrud-object" in output
        with rules_memo():
            rules = effective_rules([])
        assert rules == tuple(
            rule for rule in layered_rules if rule.source.branch != LAYER_BRANCH_NAME
        )
        rows = effective_rows(UI_COMPONENT_TYPE_URI, Selection())
        assert [content["name"] for _pk, _etag, content in rows] == ["row"]
        assert check_layers(None) == []


@pytest.mark.django_db
def test_only_a_layer_item_with_no_row_is_public(ui_alice_login):
    """A packaged layer's item is no stored row (no pk): only such an item
    on `LAYER_BRANCH_NAME` is public, and ranks as a layer's. A stored row
    that names that branch (a raw save; the viewsets refuse it) is read
    checked as any stored row, and ranks as one."""
    from django.test import RequestFactory

    from scrud_django.ui.branches.reads import may_read_item
    from scrud_django.ui.cascade import _layer_rank
    from scrud_django.ui.layers import is_layer_row

    content = {
        "name": "layertest:sheet",
        "rules": [],
        "branch": LAYER_BRANCH_NAME,
        "rbac_context": "",
        "applies_to": "",
    }
    request = RequestFactory().get("/")
    request.user = ui_alice_login
    assert is_layer_row(None, content) is True
    assert is_layer_row(7, content) is False
    assert is_layer_row(None, {**content, "branch": "main"}) is False
    assert may_read_item(request, "stylesheet", None, content) is True
    assert may_read_item(request, "stylesheet", 7, content) is False
    assert _layer_rank(None, content) == 0
    assert _layer_rank(7, content) is None
    # alice reads her own context's items, whatever their pk.
    own = {**content, "branch": "wip", "rbac_context": "alice"}
    assert may_read_item(request, "stylesheet", 7, own) is True
