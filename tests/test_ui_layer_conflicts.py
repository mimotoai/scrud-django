"""Overrides of packaged UI layer items (R1, "Layer conflicts"): an
override's ``base`` is the layer item's ETag where no ancestor overrides it,
on a root branch too; when the installed layer item changes (a library
upgrade), the override is in conflict with the layer, computed live
(`layers.layer_conflicts`, the ``scrud_django_ui.W002`` check) and resolved by
re-saving it with ``rebase: true`` or reverting it; when the layer item is
gone, the override is orphaned: it keeps rendering, and is reported.

The tests that change a layer's files read ``layermore:`` from a copy of its
package (the `layer_files` fixture)."""

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from django.core import checks
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, UI_CSS_TYPE_URI
from scrud_django.ui.branches.store import branches
from scrud_django.ui.branches.sync import conflicts
from scrud_django.ui.layers import clear_caches, layer_conflicts, layer_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from . import apps as test_apps
from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_push import own_rows, push
from .test_ui_branch_render import may_select_branches
from .test_ui_branch_types import make_branch, problem
from .test_ui_layers import (
    LAYER_JSON,
    MORE_APP,
    MORE_LIST,
    OVERRIDE,
    installed,
    override_body,
    page,
    render,
)

CHECK_ID = "scrud_django_ui.W002"
CHANGED = '<p class="changed">{{ value.title }}</p>'
CHANGED_LIST = '<p class="changed">Groceries</p>'


class LayerFiles:
    """The files ``layermore:`` is read from while the fixture is in use: a
    copy of `tests.ui_layer_more`, which a test may change."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, kind, file_name):
        return self.root / kind / file_name

    def read(self, kind, file_name):
        return json.loads(self.path(kind, file_name).read_text(encoding="utf-8"))

    def write(self, kind, file_name, content):
        """Change a file (a library upgrade), and read the layers again."""
        self.path(kind, file_name).write_text(json.dumps(content), encoding="utf-8")
        clear_caches()

    def change(self, kind, file_name, **fields):
        self.write(kind, file_name, {**self.read(kind, file_name), **fields})

    def remove(self, kind, file_name):
        """Remove a file (an upgrade drops the item), and read the layers
        again."""
        self.path(kind, file_name).unlink()
        clear_caches()


@pytest.fixture
def layer_files(tmp_path, monkeypatch):
    """``layermore:`` (installed with `installed(MORE_APP)`) read from a
    copy of its package, under a package name of its own: the test changes
    the copy, never the suite's files."""
    package = f"layer_copy_{uuid.uuid4().hex}"
    source = Path(test_apps.__file__).parent / "ui_layer_more"
    shutil.copytree(
        source, tmp_path / package, ignore=shutil.ignore_patterns("__pycache__")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        test_apps.UILayerMoreConfig,
        "scrud_ui_layer",
        {"prefix": "layermore:", "package": package},
    )
    clear_caches()
    yield LayerFiles(tmp_path / package)
    sys.modules.pop(package, None)
    clear_caches()


def etag_of(kind, *key):
    return layer_item(kind, key)[0]


def branch(name):
    return branches()[name]


def wip_body(**fields):
    return override_body(branch="wip", rbac_context="project-a", **fields)


def css_body(**fields):
    return {"name": "layermore:theme", "css": ".mine {}", "rbac_context": "", **fields}


def create(slug, body):
    """POST `body` as an item of `slug`; the response's body."""
    When.http.post(reverse(f"{slug}-list"), data=body, format="json")
    return Http.response.json()


def stored(type_uri, name, on="main"):
    """The stored item of `type_uri` called `name` on branch `on`."""
    rows = Resource.objects.filter(
        resource_type__type_uri=type_uri, content__name=name
    ).order_by()
    [row] = [row for row in rows if row.content.get("branch", "main") == on]
    return row


def put(slug, resource, body):
    resource.refresh_from_db()
    When.http.put(
        reverse(f"{slug}-detail", args=[resource.pk]),
        data=body,
        format="json",
        HTTP_IF_MATCH=f'"{resource.etag}"',
    )


def stored_branch(name):
    return Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name=name
    )


def layer_warnings():
    return [m for m in checks.run_checks(databases=["default"]) if m.id == CHECK_ID]


@pytest.mark.django_db
@test_context
def test_root_override_records_layer_etag(admin_login):
    Given.http.force_authenticate(admin_login)
    make_branch("site", "project-a", parent=None)
    with installed(MORE_APP):
        on_main = create("ui-components", override_body())
        Then.http.status_code_is(201)
        assert on_main["base"] == etag_of("component", "layermore:list")
        css = create("ui-css", css_body())
        Then.http.status_code_is(201)
        assert css["base"] == etag_of("css", "layermore:theme")
        # The suite layer's stylesheet, overridden on main.
        sheet = create(
            "ui-stylesheets",
            {"name": "layertest:sheet", "rules": [], "rbac_context": ""},
        )
        Then.http.status_code_is(201)
        assert sheet["base"] == etag_of("stylesheet", "layertest:sheet", "")
        # A root that is not main.
        on_site = create(
            "ui-components", override_body(branch="site", rbac_context="project-a")
        )
        Then.http.status_code_is(201)
        assert on_site["base"] == etag_of("component", "layermore:list")
        # An item no layer has: no base, as before.
        plain = create("ui-components", override_body(name="plain"))
        Then.http.status_code_is(201)
        assert plain["base"] is None
        # A deletion marker over a layer item is based on it too.
        marker = create(
            "ui-js", {"name": "layermore:app", "deleted": True, "rbac_context": ""}
        )
        Then.http.status_code_is(201)
        assert marker["base"] == etag_of("js", "layermore:app")
        assert layer_conflicts(branch("main")) == []
        assert layer_conflicts(branch("site")) == []


@pytest.mark.django_db
@test_context
def test_child_override_records_layer_etag_when_no_ancestor_overrides(admin_login):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    make_branch("wip2", "project-a")
    with installed(MORE_APP):
        layer = etag_of("component", "layermore:list")
        create("ui-components", wip_body())
        Then.http.status_code_is(201)
        assert Http.response.json()["base"] == layer
        # Once main overrides it, a child's override is based on main's.
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        main_etag = Http.response["ETag"].strip('"')
        create("ui-components", override_body(branch="wip2", rbac_context="project-a"))
        Then.http.status_code_is(201)
        assert Http.response.json()["base"] == main_etag != layer
        # wip's override is not one of the layer's item now: main's is
        # beneath it, so it is no layer conflict (a pull reports it).
        assert layer_conflicts(branch("wip")) == []
        assert layer_conflicts(branch("wip2")) == []


@pytest.mark.django_db
@test_context
def test_child_override_of_layer_item_is_no_conflict_against_its_parent(
    admin_login,
):
    """Ruling 1: the layer is beneath the parent in `conflicts` too."""
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        create("ui-components", wip_body())
        Then.http.status_code_is(201)
        create("ui-css", css_body(branch="wip", rbac_context="project-a"))
        Then.http.status_code_is(201)
        assert conflicts("wip", "main") == []
        # A pull lists none.
        stored = stored_branch("wip")
        put("ui-branches", stored, {**stored.content, "pull": True})
        Then.http.status_code_is(200)
        assert Http.response.json()["conflicts"] == []
        # And it pushes cleanly into main, keeping the layer ETags.
        push(source="wip", target="main", context="")
        Then.http.status_code_is(201)
        [component] = own_rows(UI_COMPONENT_TYPE_URI, "main")
        assert component["base"] == etag_of("component", "layermore:list")
        [css] = own_rows(UI_CSS_TYPE_URI, "main")
        assert css["base"] == etag_of("css", "layermore:theme")
        assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []


@pytest.mark.django_db
@test_context
def test_changed_layer_item_is_a_layer_conflict_on_main_and_on_a_child(
    admin_login, todo_list, todo_reader_login, layer_files
):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        component_before = etag_of("component", "layermore:list")
        css_before = etag_of("css", "layermore:theme")
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        # wip overrides main's override (no layer conflict) and the layer's
        # CSS item, which main does not override.
        create("ui-components", wip_body())
        Then.http.status_code_is(201)
        create("ui-css", css_body(branch="wip", rbac_context="project-a"))
        Then.http.status_code_is(201)
        assert layer_conflicts(branch("main")) == []
        assert layer_conflicts(branch("wip")) == []
        assert layer_warnings() == []
        # An upgrade changes both layer items.
        layer_files.change("component", "list.json", template=CHANGED)
        layer_files.change("css", "theme.json", css=".layermore { color: red; }")
        component_after = etag_of("component", "layermore:list")
        css_after = etag_of("css", "layermore:theme")
        assert component_after != component_before and css_after != css_before
        assert layer_conflicts(branch("main")) == [
            {
                "kind": "component",
                "key": ["layermore:list"],
                "base": component_before,
                "current": component_after,
            }
        ]
        assert layer_conflicts(branch("wip")) == [
            {
                "kind": "css",
                "key": ["layermore:theme"],
                "base": css_before,
                "current": css_after,
            }
        ]
        # The overrides still render.
        Given.http.force_authenticate(todo_reader_login)
        assert OVERRIDE in render(todo_list)


@pytest.mark.django_db
@test_context
def test_rebase_on_root_resolves_layer_conflict(admin_login, layer_files):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        on_main = stored(UI_COMPONENT_TYPE_URI, "layermore:list")
        create("ui-css", css_body(branch="wip", rbac_context="project-a"))
        Then.http.status_code_is(201)
        on_wip = stored(UI_CSS_TYPE_URI, "layermore:theme", on="wip")
        layer_files.change("component", "list.json", template=CHANGED)
        layer_files.change("css", "theme.json", css=".layermore { color: red; }")
        assert len(layer_conflicts(branch("main"))) == 1
        assert len(layer_conflicts(branch("wip"))) == 1
        # A plain edit keeps the conflict.
        put("ui-components", on_main, override_body(template="<i>edited</i>"))
        Then.http.status_code_is(200)
        assert len(layer_conflicts(branch("main"))) == 1
        # rebase: true on main (a root) moves base to the layer's ETag.
        put(
            "ui-components",
            on_main,
            override_body(template="<i>kept</i>", base="forged", rebase=True),
        )
        Then.http.status_code_is(200)
        assert Http.response.json()["base"] == etag_of("component", "layermore:list")
        assert "rebase" not in Http.response.json()
        assert layer_conflicts(branch("main")) == []
        # On a child, as before: the parent's state, the layer's here.
        put(
            "ui-css",
            on_wip,
            css_body(branch="wip", rbac_context="project-a", rebase=True),
        )
        Then.http.status_code_is(200)
        assert Http.response.json()["base"] == etag_of("css", "layermore:theme")
        assert layer_conflicts(branch("wip")) == []
        assert layer_warnings() == []
        # rebase on a root item that overrides no layer item stays refused.
        create("ui-components", override_body(name="plain"))
        Then.http.status_code_is(201)
        plain = stored(UI_COMPONENT_TYPE_URI, "plain")
        put("ui-components", plain, override_body(name="plain", rebase=True))
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-branch-invalid")


@pytest.mark.django_db
@test_context
def test_revert_resolves_layer_conflict(
    admin_login, todo_list, todo_reader_login, layer_files
):
    Given.http.force_authenticate(admin_login)
    with installed(MORE_APP):
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        location, etag = Http.response["Location"], Http.response["ETag"]
        layer_files.change("component", "list.json", template=CHANGED)
        assert len(layer_conflicts(branch("main"))) == 1
        When.http.delete(location, HTTP_IF_MATCH=etag)
        Then.http.status_code_is(204)
        assert layer_conflicts(branch("main")) == []
        assert layer_warnings() == []
        # The upgraded layer item shows.
        Given.http.force_authenticate(todo_reader_login)
        assert CHANGED_LIST in render(todo_list)


@pytest.mark.django_db
@test_context
def test_removed_layer_item_leaves_orphan_rendering_and_reported(
    admin_login, todo_list, todo_reader_login, layer_files
):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        layer = etag_of("component", "layermore:list")
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        orphan = stored(UI_COMPONENT_TYPE_URI, "layermore:list")
        # The upgrade drops the component (the layer's rule still names it).
        layer_files.remove("component", "list.json")
        assert layer_item("component", ("layermore:list",)) is None
        expected = {
            "kind": "component",
            "key": ["layermore:list"],
            "base": layer,
            "current": None,
        }
        assert layer_conflicts(branch("main")) == [expected]
        [warning] = layer_warnings()
        assert warning.level == checks.WARNING
        assert "'main'" in warning.msg and "layermore:list" in warning.msg
        assert "orphan" in warning.msg
        # It keeps rendering as an ordinary item: no 500, no silent drop.
        Given.http.force_authenticate(todo_reader_login)
        assert OVERRIDE in render(todo_list)
        # It stays editable under its name (ruling 2), and stays reported.
        Given.http.force_authenticate(admin_login)
        put("ui-components", orphan, override_body(template="<i>{{ value.title }}</i>"))
        Then.http.status_code_is(200)
        Given.http.force_authenticate(todo_reader_login)
        assert "<i>Groceries</i>" in render(todo_list)
        assert layer_conflicts(branch("main")) == [expected]
        Given.http.force_authenticate(admin_login)
        # There is no layer item to rebase onto.
        put("ui-components", orphan, override_body(rebase=True))
        Then.http.status_code_is(400)
        # A rename into a layer prefix, and a new item with its name on
        # another branch, are refused.
        put("ui-components", orphan, override_body(name="layermore:other"))
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-layer-name-reserved")
        When.http.post(reverse("ui-components-list"), data=wip_body(), format="json")
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-layer-name-reserved")
        # Deleting it resolves it.
        orphan.refresh_from_db()
        When.http.delete(
            reverse("ui-components-detail", args=[orphan.pk]),
            HTTP_IF_MATCH=f'"{orphan.etag}"',
        )
        Then.http.status_code_is(204)
        assert layer_conflicts(branch("main")) == []
        assert layer_warnings() == []


@pytest.mark.django_db
@test_context
def test_w002_lists_layer_conflicts(admin_login, layer_files):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        create("ui-components", override_body())
        Then.http.status_code_is(201)
        create("ui-css", css_body(branch="wip", rbac_context="project-a"))
        Then.http.status_code_is(201)
        create("ui-js", {"name": "layermore:app", "js": "", "rbac_context": ""})
        Then.http.status_code_is(201)
        assert layer_warnings() == []
        layer_files.change("component", "list.json", template=CHANGED)
        layer_files.change("css", "theme.json", css=".layermore { color: red; }")
        layer_files.remove("js", "app.json")
        warnings = layer_warnings()
        # One per branch, in name order.
        assert [w.level for w in warnings] == [checks.WARNING, checks.WARNING]
        main_warning, wip_warning = warnings
        assert "'main'" in main_warning.msg
        assert "component 'layermore:list'" in main_warning.msg
        assert "js 'layermore:app'" in main_warning.msg
        assert "'wip'" in wip_warning.msg
        assert "css 'layermore:theme'" in wip_warning.msg
        assert "layermore:list" not in wip_warning.msg
        assert "rebase" in main_warning.hint
        # A database check: run_checks without databases does not run it.
        assert not [m for m in checks.run_checks() if m.id == CHECK_ID]


@pytest.mark.django_db
@test_context
def test_push_override_of_layer_item_into_root(
    admin_login, todo_list, todo_reader_login
):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        layer = etag_of("component", "layermore:list")
        css_layer = etag_of("css", "layermore:theme")
        # main overrides the CSS item; wip overrides the component and
        # deletes the CSS item.
        create("ui-css", css_body())
        Then.http.status_code_is(201)
        create("ui-components", wip_body())
        Then.http.status_code_is(201)
        create(
            "ui-css",
            {
                "name": "layermore:theme",
                "deleted": True,
                "branch": "wip",
                "rbac_context": "project-a",
            },
        )
        Then.http.status_code_is(201)
        push(source="wip", target="main", context="")
        Then.http.status_code_is(201)
        [component] = own_rows(UI_COMPONENT_TYPE_URI, "main")
        assert component["name"] == "layermore:list"
        assert component["base"] == layer
        # main's override is deleted, and a marker hides the layer's item,
        # based on it.
        [marker] = own_rows(UI_CSS_TYPE_URI, "main")
        assert marker["deleted"] is True and marker["base"] == css_layer
        assert layer_conflicts(branch("main")) == []
        Given.http.force_authenticate(todo_reader_login)
        assert OVERRIDE in render(todo_list)
        css_url = reverse("ui-css-file", args=["layermore:theme", css_layer])
        assert css_url not in page(todo_list)


@pytest.mark.django_db
@test_context
def test_branch_override_of_layer_stylesheet_hides_its_rules_there(
    admin_login, todo_list, todo_reader_login
):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    create(
        "ui-stylesheets",
        {
            "name": "layertest:sheet",
            "rules": [],
            "branch": "wip",
            "rbac_context": "project-a",
        },
    )
    Then.http.status_code_is(201)
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert LAYER_JSON not in render(todo_list, branch="wip")
    assert LAYER_JSON in render(todo_list)


@pytest.mark.django_db
@test_context
def test_deletion_marker_hides_layer_item_on_its_branch_only(
    admin_login, todo_list, todo_reader_login
):
    Given.http.force_authenticate(admin_login)
    make_branch("wip", "project-a")
    with installed(MORE_APP):
        create(
            "ui-stylesheets",
            {
                "name": "layermore:sheet",
                "deleted": True,
                "branch": "wip",
                "rbac_context": "project-a",
            },
        )
        Then.http.status_code_is(201)
        Given.http.force_authenticate(may_select_branches(todo_reader_login))
        # On wip, the suite layer's rule takes the list again.
        on_wip = render(todo_list, branch="wip")
        assert MORE_LIST not in on_wip and LAYER_JSON in on_wip
        assert MORE_LIST in render(todo_list)
