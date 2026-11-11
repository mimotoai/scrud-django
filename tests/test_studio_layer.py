"""The studio's packaged UI layer (``studio:``, `scrud_django.ui.studio.layer`),
the studio links on the pages of UI types, and the Jinja component globals
``in_mount`` and ``api_url``. The suite runs without the studio layer
(`tests.apps.StudioWithoutLayerConfig`); the tests that need it install the
real `scrud_django.ui.studio` config with the ``studio_layer`` fixture. These
tests serve the API at ``/api/`` and the bootstrap studio at ``/studio/``
(``tests.mounted_urls``)."""

import importlib.resources
import json
import re

import pytest
from django.apps import apps
from django.conf import settings
from django.core import checks
from django.templatetags.static import static
from django.test import override_settings
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.branches import UI_CSS_TYPE_URI, UI_JS_TYPE_URI
from scrud_django.ui.branches.effective import Selection, effective_rows
from scrud_django.ui.layers import (
    UILayer,
    check_layers,
    installed_layers,
    layer_item,
)
from scrud_django.ui.renderer import STATIC_HTMX, STATIC_SCRUD_UI
from scrud_django.ui.studio import studio_branch

from .fixtures import *  # noqa
from .settings import SETTINGS
from .steps import *  # noqa: F403, F401
from .test_ui_branch_prefixes import ADMIN, mounted, publish
from .test_ui_branch_render import may_select_branches
from .test_ui_branch_types import make_branch
from .test_ui_embeds import host, make_jinja, url_of
from .test_ui_http import FRAME, HTML, html, make_component, make_stylesheet

LAYER_PACKAGE = "scrud_django.ui.studio.layer"
STUDIO_LAYER = UILayer(
    label="scrud_django_ui_studio", prefix="studio:", package=LAYER_PACKAGE
)
# The suite's apps: the studio app without its layer.
SUITE_APPS = list(SETTINGS["INSTALLED_APPS"])
STUDIO_ROOT = "/workbench/"
NAV = '<nav class="scrud-studio-links">'
SAFE_PAGE_FALLBACK = (
    '<!-- scrud-ui: page component "{}" leaves out the branch banner'
    " or the safe mode notice -->"
)


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def get_page(url, status=200, **headers):
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(status)
    return html()


def without_csrf(page):
    return re.sub(r'name="csrf-token" content="[^"]*"', "", page)


def frame_of(page):
    """The page's body: what the page component wraps."""
    found = re.search(r"<main>(.*)</main>", page, re.S)
    assert found, page
    return found.group(1)


def layer_files(kind):
    """``{file name: content}`` of the studio layer's files of `kind`."""
    folder = importlib.resources.files(LAYER_PACKAGE).joinpath(kind)
    return {
        entry.name: json.loads(entry.read_text(encoding="utf-8"))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    }


def css_selectors(css):
    """Every selector of the style rules in `css` (inside ``@media`` and
    ``@supports`` blocks too), each selector of a list on its own. Any other
    at-rule is an error: the layer has none."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    selectors = []

    def block(text, at):
        """Parse the rules of `text` from `at` to its closing brace (or the
        end); return the index after it."""
        while True:
            opening = text.find("{", at)
            closing = text.find("}", at)
            if closing != -1 and (opening == -1 or closing < opening):
                assert not text[at:closing].strip(), text[at:closing]
                return closing + 1
            if opening == -1:
                assert not text[at:].strip(), text[at:]
                return len(text)
            prelude = text[at:opening].strip()
            if prelude.startswith("@"):
                assert re.match(r"@(media|supports)\b", prelude), prelude
                at = block(text, opening + 1)
                continue
            selectors.extend(part.strip() for part in prelude.split(","))
            end = text.find("}", opening)
            assert end != -1, text[opening:]
            assert "{" not in text[opening + 1 : end], text[opening:end]
            at = end + 1

    block(css, 0)
    return selectors


@pytest.mark.django_db
def test_layer_installed_with_studio_app(studio_layer):
    assert STUDIO_LAYER in installed_layers()
    assert check_layers(None) == []
    # As manage.py check runs them.
    assert [e for e in checks.run_checks() if e.id == "scrud_django_ui.E002"] == []
    page = layer_item("component", ("studio:page",))
    assert page is not None
    assert page[1]["engine"] == "jinja2" and page[1]["mode"] == "view"
    sheet = layer_item("stylesheet", ("studio:studio", ""))
    assert sheet is not None
    assert {"select": "studio scrud:page", "component": "studio:page"} in sheet[1][
        "rules"
    ]
    assert layer_item("css", ("studio:studio",)) is not None
    assert layer_item("js", ("studio:studio",)) is not None


def test_suite_runs_without_studio_layer():
    assert apps.is_installed("scrud_django.ui.studio")
    assert "tests.apps.StudioWithoutLayerConfig" in settings.INSTALLED_APPS
    assert "scrud_django.ui.studio" not in settings.INSTALLED_APPS
    config = apps.get_app_config("scrud_django_ui_studio")
    assert config.scrud_ui_layer is None
    assert all(layer.prefix != "studio:" for layer in installed_layers())
    assert layer_item("component", ("studio:page",)) is None
    assert studio_branch() is None


@pytest.mark.django_db
@test_context
def test_studio_page_only_in_studio_scope(studio_layer, admin_login):
    row = make_component("row", "<b>{{ value }}</b>")
    url = url_of(row)
    Given.http.force_authenticate(admin_login)
    with override_settings(INSTALLED_APPS=SUITE_APPS):
        assert STUDIO_LAYER not in installed_layers()
        without = without_csrf(get_page(url))
    with_layer = without_csrf(get_page(url))
    [(_pk, css_etag, _css)] = effective_rows(UI_CSS_TYPE_URI, Selection())
    # The layer's five JS items: studio:studio (order 0), then studio:rules
    # (order 1), studio:inspector (order 2), studio:promote (order 3) and
    # studio:branch (order 4, Task 8), which use the API studio:studio sets
    # up.
    js_items = {
        content["name"]: (content["order"], etag)
        for _pk, etag, content in effective_rows(UI_JS_TYPE_URI, Selection())
    }
    assert sorted(js_items) == [
        "studio:branch",
        "studio:inspector",
        "studio:promote",
        "studio:rules",
        "studio:studio",
    ]
    assert (
        js_items["studio:studio"][0]
        < js_items["studio:rules"][0]
        < js_items["studio:inspector"][0]
        < js_items["studio:promote"][0]
        < js_items["studio:branch"][0]
    )
    css_tag = '<link rel="stylesheet" href="{}">\n'.format(
        reverse("ui-css-file", args=["studio:studio", css_etag])
    )
    js_tag = "".join(
        '<script src="{}" defer></script>\n'.format(
            reverse("ui-js-file", args=[name, js_items[name][1]])
        )
        for name in (
            "studio:studio",
            "studio:rules",
            "studio:inspector",
            "studio:promote",
            "studio:branch",
        )
    )
    # The frame is byte-identical; the page differs by the layer's CSS link
    # and JS script only.
    assert frame_of(with_layer) == frame_of(without)
    assert css_tag in with_layer and js_tag in with_layer
    assert with_layer.replace(css_tag, "").replace(js_tag, "") == without
    assert "data-studio" not in with_layer
    # In scope studio: the layer's page, which links the CSS, the JS and the
    # static scripts, and wraps the body (the chrome with it).
    studio = get_page(url + "?ui-scope=studio")
    assert "scrud-ui: page component" not in studio
    assert '<meta name="csrf-token" content="' in studio
    assert css_tag in studio and js_tag in studio
    for script in (STATIC_HTMX, STATIC_SCRUD_UI):
        assert f'<script src="{static(script)}" defer></script>' in studio
    head, body = studio.split('<div class="studio" data-studio>', 1)
    assert "</head>" in head and "<body" in head
    assert NAV in body and FRAME in body


@pytest.mark.django_db
@test_context
def test_component_globals_in_mount_and_api_url(todo_list, todo_reader_login):
    pk = todo_list.pk
    host(
        "<p>"
        f'{{{{ in_mount("/api/todo-lists/{pk}/") }}}}'
        f'|{{{{ api_url("todo-lists", {pk}) }}}}'
        '|{{ api_url("todo-lists") }}'
        '|{{ api_url("ui-branch-items", "main") }}'
        "|{{ in_mount(3) }}"
        "</p>"
    )
    publish("admin", ADMIN)
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    # At the API's URLs, in_mount changes nothing.
    page = get_page(url_of(todo_list), HTTP_SCRUD_UI_SCOPE="host")
    assert (
        f"<p>/api/todo-lists/{pk}/|/api/todo-lists/{pk}/|/api/todo-lists/"
        "|/api/ui-branch-items/main/|3</p>"
    ) in page
    # Under a published prefix, in_mount maps an API path into it; api_url
    # is always the API's path (which embed maps).
    page = get_page(mounted(ADMIN, todo_list), HTTP_SCRUD_UI_SCOPE="host")
    assert (
        f"<p>{ADMIN}todo-lists/{pk}/|/api/todo-lists/{pk}/|/api/todo-lists/"
        "|/api/ui-branch-items/main/|3</p>"
    ) in page


@pytest.mark.django_db
@test_context
def test_api_url_of_unknown_slug_fails_its_component(todo_list, todo_reader_login):
    host('<p class="host">{{ api_url("no-such-type") }}</p>')
    Given.http.force_authenticate(todo_reader_login)
    page = get_page(url_of(todo_list), HTTP_SCRUD_UI_SCOPE="host")
    assert '<!-- scrud-ui: component "host" failed -->' in page
    assert '<p class="host">' not in page
    assert "Groceries" in page
    # Nor is a route of another view of a type (its slug is not the type's).
    other = '<p class="host">{{ api_url("denying-partner-profiles") }}</p>'
    make_jinja("host2", other)
    make_stylesheet("hosts2", [("host2 ex:TodoList", "host2")])
    page = get_page(url_of(todo_list), HTTP_SCRUD_UI_SCOPE="host2")
    assert '<!-- scrud-ui: component "host2" failed -->' in page


@pytest.mark.django_db
@test_context
def test_globals_in_list_and_page_components_not_django(todo_list, todo_reader_login):
    make_jinja(
        "jinja-list",
        '<ul data-list="{{ api_url("todo-lists") }}">{{ in_mount("/api/x/") }}</ul>',
    )
    make_jinja(
        "jinja-page",
        '<html><body data-page="{{ api_url("todo-lists") }}">{{ body }}</body></html>',
    )
    make_component("django-row", '<i data-django="{{ in_mount }}|{{ api_url }}">x</i>')
    make_stylesheet(
        "s",
        [
            ("listing scrud:collection", "jinja-list"),
            ("scrud:page", "jinja-page"),
            ("plain ex:TodoList", "django-row"),
        ],
    )
    Given.http.force_authenticate(todo_reader_login)
    page = get_page(reverse("todo-lists-list") + "?ui-scope=listing")
    assert '<ul data-list="/api/todo-lists/">/api/x/</ul>' in page
    assert '<body data-page="/api/todo-lists/">' in page
    page = get_page(url_of(todo_list), HTTP_SCRUD_UI_SCOPE="plain")
    assert '<i data-django="|">x</i>' in page


@pytest.mark.django_db
@test_context
def test_studio_links_on_ui_type_pages_only(todo_list, admin_login):
    root = make_branch("studio-root", "project-a", parent=None, prefix=STUDIO_ROOT)
    make_branch("no-prefix", "project-a", parent=None)
    row = make_component("row", "<b>{{ value }}</b>")
    Given.http.force_authenticate(admin_login)
    # The workbench in the scope studio, in view mode (the workbench is a view
    # of its branch, also for a user who may PUT it), the & escaped.
    studio_href = (
        f"{STUDIO_ROOT}ui-branches/{root.pk}/?ui-scope=studio&amp;ui-mode=view"
    )
    bootstrap_href = reverse("scrud-studio:types")
    with override_settings(SCRUD_UI_STUDIO_BRANCH="studio-root"):
        assert studio_branch() == "studio-root"
        page = get_page(url_of(row))
        assert (
            f"{NAV}"
            f'<a href="{studio_href}">Studio</a>'
            f'<a href="{url_of(row)}?ui-safe=1">Safe mode</a>'
            f'<a href="{bootstrap_href}">Bootstrap studio</a>'
            "</nav>"
        ) in page
        # The safe link keeps the query.
        page = get_page(url_of(row) + "?ui-scope=a")
        assert f'<a href="{url_of(row)}?ui-scope=a&amp;ui-safe=1">Safe mode</a>' in (
            page
        )
        # The UI types' lists, and the studio's computed types.
        assert NAV in get_page(reverse("ui-components-list"))
        assert NAV in get_page(url_of(root))
        assert NAV in get_page(reverse("ui-branch-items-detail", args=["main"]))
        # Not on another type's pages, nor in a fragment.
        page = get_page(url_of(todo_list))
        assert "scrud-studio-links" not in page and "Buy milk" in page
        assert "scrud-studio-links" not in get_page(reverse("todo-lists-list"))
        page = get_page(url_of(row), HTTP_HX_REQUEST="true")
        assert "scrud-studio-links" not in page
    # Without the setting, a branch it names, or that branch's prefix: no
    # studio link; the others stay.
    for value in (None, "nope", "no-prefix"):
        with override_settings(SCRUD_UI_STUDIO_BRANCH=value):
            page = get_page(url_of(row))
            assert NAV in page and ">Studio</a>" not in page, value
            assert f'<a href="{bootstrap_href}">Bootstrap studio</a>' in page
    # Without the studio app: none.
    installed = [
        app
        for app in settings.INSTALLED_APPS
        if app != "tests.apps.StudioWithoutLayerConfig"
    ]
    with override_settings(INSTALLED_APPS=installed):
        assert "scrud-studio-links" not in get_page(url_of(row))


@pytest.mark.django_db
@test_context
def test_studio_links_required_in_page(todo_list, admin_login):
    make_component("my-page", "<html><body>no body here</body></html>")
    make_stylesheet("pages", [("scrud:page", "my-page")])
    row = make_component("row", "<b>{{ value }}</b>")
    Given.http.force_authenticate(admin_login)
    # On a page with no chrome, the page component is used.
    assert get_page(url_of(todo_list)) == "<html><body>no body here</body></html>"
    # The studio links are chrome: a page that leaves them out is replaced
    # by the built-in.
    page = get_page(url_of(row))
    assert page.startswith(SAFE_PAGE_FALLBACK.format("my-page"))
    assert "no body here" not in page and NAV in page


def test_layer_css_is_namespaced(studio_layer):
    [(_name, css)] = [
        (name, content["css"]) for name, content in layer_files("css").items()
    ]
    selectors = css_selectors(css)
    assert selectors
    for selector in selectors:
        assert ".studio" in selector or "[data-studio" in selector, selector
    # The parser itself.
    assert css_selectors(".a, .b { x: y } @media (x) { .c { z: w } }") == [
        ".a",
        ".b",
        ".c",
    ]


def test_layer_components_have_no_inline_scripts(studio_layer):
    components = layer_files("component")
    assert "page.json" in components
    for name, content in components.items():
        template = content["template"]
        assert content["engine"] == "jinja2", name
        for tag in re.findall(r"<script\b[^>]*>", template, re.I):
            assert re.search(r"\ssrc=", tag), (name, tag)
        assert not re.search(r"<script\b[^>]*>\s*[^<\s]", template, re.I), name
        assert not re.search(r"\son[a-z]+\s*=", template, re.I), name
        assert "javascript:" not in template.lower(), name
    for kind in ("component", "stylesheet", "css", "js"):
        for name, content in layer_files(kind).items():
            assert content["name"].startswith("studio:"), (kind, name)
            assert ":" not in name, (kind, name)
