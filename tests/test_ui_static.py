"""Static assets of the UI: vendored htmx and the ``scrud-json`` extension.

Browser behaviour is covered in the browser tests; these checks are
Python-side only, plus one optional run of the pure helpers under node."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui import renderer

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

STATIC_DIR = Path(renderer.__file__).parent / "static" / "scrud_ui"
HTMX = STATIC_DIR / "htmx.min.js"
SCRUD_UI = STATIC_DIR / "scrud-ui.js"
HTML = "text/html"


def detail_url(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def test_static_assets_present():
    assert HTMX.is_file() and SCRUD_UI.is_file()
    assert 'version:"2.0.4"' in HTMX.read_text()


def test_scrud_ui_source_is_safe_and_declares_extension():
    source = SCRUD_UI.read_text()
    assert "eval(" not in source
    assert "new Function" not in source
    assert re.search(r"defineExtension\(\s*['\"]scrud-json['\"]", source)
    assert "sha384-" in source
    assert "window.ScrudUI" in source
    assert "selfRequestsOnly" in source and "responseHandling" in source


@pytest.mark.django_db
@test_context
def test_page_references_static(todo_list, todo_owner_login):
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(detail_url(todo_list), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    page = Http.response.content.decode()
    assert re.search(r'<script src="/static/scrud_ui/htmx\.min\.js" defer>', page)
    assert re.search(r'<script src="/static/scrud_ui/scrud-ui\.js" defer>', page)


@pytest.mark.django_db
@test_context
def test_fragment_frame_embeds_whole_content(todo_list, todo_owner_login):
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(detail_url(todo_list), HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    full = Http.response.json()
    When.http.get(
        detail_url(todo_list),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_POINTER="/items/1",
    )
    Then.http.status_code_is(200)
    output = Http.response.content.decode()
    assert 'data-scrud-pointer="/items/1"' in output
    blocks = re.findall(
        r'<script type="application/json" class="scrud-content">(.*?)</script>',
        output,
        re.S,
    )
    assert blocks and all(json.loads(b) == full for b in blocks)


NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const htmx = {config: {}, defineExtension() {}};
const ctx = vm.createContext({window, htmx, console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const content = {title: 't', items: [{text: 'a', done: false}]};
const out = {};
out.done = U.buildBody(content, [
  {name: '/items/0/done', type: 'checkbox', checked: true},
]);
out.append = U.buildBody(content, [
  {name: '/items/-/text', type: 'text', value: 'b'},
  {name: '/items/-/done', type: 'checkbox', checked: true},
]);
out.dropped = U.buildBody(content, [
  {name: '/items/-/text', type: 'text', value: ''},
  {name: '/items/-/done', type: 'checkbox', checked: false},
]);
out.number = U.buildBody({n: 1}, [{name: '/n', type: 'number', value: ''}]);
out.untouched = content;
console.log(JSON.stringify(out));
"""


def test_build_body_under_node():
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    done = subprocess.run(
        ["node", "-e", NODE_SCRIPT, str(SCRUD_UI)],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(done.stdout)
    assert out["done"]["items"][0]["done"] is True
    assert out["append"]["items"] == [
        {"text": "a", "done": False},
        {"text": "b", "done": True},
    ]
    assert out["dropped"]["items"] == [{"text": "a", "done": False}]
    assert out["number"] == {}
    assert out["untouched"]["items"] == [{"text": "a", "done": False}]


def test_apply_pointer_ignores_prototype_keys():
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    script = NODE_SCRIPT.split("const content")[0] + (
        "U.applyPointer({}, '/__proto__/x', 1);"
        "U.applyPointer({}, '/a/constructor/prototype/y', 1);"
        "console.log(JSON.stringify([({}).x, ({}).y]));"
    )
    done = subprocess.run(
        ["node", "-e", script, str(SCRUD_UI)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [None, None]
