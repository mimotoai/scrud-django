"""scrud-ui.js as the browser runs it: the request headers of its
``htmx:configRequest`` handler on a stub DOM, and a round trip from the
rendered edit form through ``buildBody`` to a JSON PUT."""

import json
import subprocess
from html.parser import HTMLParser

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_static import SCRUD_UI, STUB_DOM, require_node

HTML = "text/html"

HEADERS_SCRIPT = (
    STUB_DOM
    + r"""
document.querySelector = (sel) =>
  sel === 'meta[name=csrf-token]' ? el('META', {content: 'csrf-123'}) : null;
const root = frame('/l/1/');
root.attrs['data-scrud-etag'] = '"root-etag"';
root.attrs['data-scrud-scope'] = 'outer';
root.attrs['hx-put'] = '/l/1/';
el('SCRIPT', {class: 'scrud-content'}, root, {textContent: '{"items": [{"done": false}]}'});
const node = el('DIV', {class: 'scrud-resource', 'data-scrud-href': '/l/1/',
  'data-scrud-etag': 'inner-etag', 'data-scrud-scope': 'outer inner',
  'data-scrud-pointer': '/items/0', 'hx-put': '/l/1/'}, root);
el('SCRIPT', {class: 'scrud-content'}, node, {textContent: '{"items": [{"done": false}]}'});
const input = el('INPUT', {name: '/items/0/done'}, node, {type: 'checkbox', checked: true});
const fire = (elt, verb, headers) => {
  const detail = {elt, verb, headers: headers || {}};
  handlers['htmx:configRequest']({detail, preventDefault() { detail.prevented = true; }});
  return {headers: detail.headers, prevented: !!detail.prevented};
};
out.put = fire(node, 'put');
out.putFromInput = fire(input, 'put');
out.putRoot = fire(root, 'put');
out.get = fire(node, 'get');
out.getAccept = fire(node, 'GET', {Accept: 'application/json'});
out.putOwnAccept = fire(node, 'put', {accept: 'application/json', 'if-match': '"mine"'});
out.delete = fire(el('BUTTON', {'hx-delete': '/l/1/'}, node), 'delete');
console.log(JSON.stringify(out));
"""
)


def run_node(script, *args, stdin=None):
    done = subprocess.run(
        ["node", "-e", script, str(SCRUD_UI), *args],
        capture_output=True,
        text=True,
        input=stdin,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_config_request_headers_come_from_the_innermost_frame():
    require_node()
    out = run_node(HEADERS_SCRIPT)
    # a PUT from an input in a node frame in a root frame (the node frame
    # carries the hx-put htmx fires from)
    for key in ("put", "putFromInput"):
        headers = out[key]["headers"]
        assert headers["If-Match"] == '"inner-etag"', key
        assert headers["X-CSRFToken"] == "csrf-123"
        assert headers["Scrud-UI-Scope"] == "outer inner"
        assert headers["Scrud-UI-Pointer"] == "/items/0"
        assert headers["Accept"] == "text/html"
        assert not out[key].get("prevented")
    assert out["put"]["headers"]["Content-Type"] == "application/json"
    # an input is no hx-put element: nothing is sent as JSON for it
    assert "Content-Type" not in out["putFromInput"]["headers"]
    # the root frame's own request uses its own etag
    assert out["putRoot"]["headers"]["If-Match"] == '"root-etag"'
    assert out["putRoot"]["headers"]["Scrud-UI-Scope"] == "outer"
    # DELETE from the button inside the node frame: same frame, no body type
    assert out["delete"]["headers"]["If-Match"] == '"inner-etag"'
    assert out["delete"]["headers"]["X-CSRFToken"] == "csrf-123"
    assert "Content-Type" not in out["delete"]["headers"]


def test_get_requests_get_no_write_headers():
    require_node()
    out = run_node(HEADERS_SCRIPT)
    assert out["get"]["headers"] == {"Accept": "text/html"}
    # an Accept the page set itself is kept, in any case
    assert out["getAccept"]["headers"] == {"Accept": "application/json"}
    assert out["putOwnAccept"]["headers"]["accept"] == "application/json"
    assert "Accept" not in out["putOwnAccept"]["headers"]
    assert out["putOwnAccept"]["headers"]["if-match"] == '"mine"'
    assert "If-Match" not in out["putOwnAccept"]["headers"]


BRANCH_SCRIPT = (
    STUB_DOM
    + r"""
const page = el('MAIN', {});
const wip = frame('/l/1/', page);
wip.attrs['data-scrud-branch'] = 'wip';
const node = el('DIV', {class: 'scrud-resource', 'data-scrud-href': '/l/1/',
  'data-scrud-pointer': '/items/0'}, wip);
const button = el('BUTTON', {'hx-delete': '/l/1/'}, node);
const published = frame('/l/2/', page);
published.attrs['data-scrud-branch'] = '';
const plain = frame('/l/3/', page);
const fire = (elt, verb, headers) => {
  const detail = {elt, verb, headers: headers || {}};
  handlers['htmx:configRequest']({detail, preventDefault() {}});
  return detail.headers;
};
out.get = fire(node, 'get');
out.delete = fire(button, 'delete');
out.published = fire(published, 'get');
out.plain = fire(plain, 'get');
out.own = fire(node, 'get', {'scrud-ui-branch': 'mine'});
// An element outside any frame (a pager link, a stored page component's nav)
// uses the page's branch, which a frame's own attribute overrides.
const nav = el('A', {'hx-get': '/l/?page=2'}, el('NAV', {}));
out.outsideBare = fire(nav, 'get');
const marker = el('DIV', {class: 'scrud-branch-page', 'data-scrud-branch': 'wip@pinned'});
document.querySelector = (sel) =>
  sel === '.scrud-branch-page[data-scrud-branch]' ? marker : null;
out.outside = fire(nav, 'get');
out.insideFrame = fire(node, 'get');
console.log(JSON.stringify(out));
"""
)


def test_config_request_sends_the_frames_branch():
    require_node()
    out = run_node(BRANCH_SCRIPT)
    # from the closest element carrying data-scrud-branch, GET and writes alike
    assert out["get"]["Scrud-UI-Branch"] == "wip"
    assert out["delete"]["Scrud-UI-Branch"] == "wip"
    # "" names the published branch explicitly
    assert out["published"]["Scrud-UI-Branch"] == ""
    # none without the attribute (a page on the published branch)
    assert out["plain"] == {"Accept": "text/html"}
    # a header the page set itself is kept, in any case
    assert out["own"] == {"Accept": "text/html", "scrud-ui-branch": "mine"}
    # outside any frame: the page's branch (.scrud-branch-page), else none
    assert out["outsideBare"] == {"Accept": "text/html"}
    assert out["outside"]["Scrud-UI-Branch"] == "wip@pinned"
    assert out["insideFrame"]["Scrud-UI-Branch"] == "wip"


BUILD_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const ctx = vm.createContext({window, htmx: {config: {}, defineExtension() {}}});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const errors = [];
const body = window.ScrudUI.buildBody(input.content, input.fields, errors);
console.log(JSON.stringify({body, errors}));
"""


class EditForm(HTMLParser):
    """The root frame of rendered HTML: its attributes, embedded content and
    named inputs, read as a browser's DOM would give them (a textarea's first
    newline dropped)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frame = None
        self.content = None
        self.fields = []
        self._in = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self.frame is None and "scrud-resource" in classes:
            self.frame = attrs
        elif tag == "script" and "scrud-content" in classes:
            self._in, self._text = "script", ""
        elif tag == "textarea" and (attrs.get("name") or "").startswith("/"):
            self._in, self._text = ("textarea", attrs), ""
        elif tag == "input" and (attrs.get("name") or "").startswith("/"):
            kind = attrs.get("type", "text")
            self.fields.append(
                {
                    "name": attrs["name"],
                    "type": kind,
                    "value": attrs.get("value", ""),
                    "checked": "checked" in attrs,
                }
            )

    def handle_data(self, data):
        if self._in is not None:
            self._text += data

    def handle_endtag(self, tag):
        if self._in == "script" and tag == "script":
            if self.content is None:
                self.content = json.loads(self._text)
            self._in = None
        elif isinstance(self._in, tuple) and tag == "textarea":
            _, attrs = self._in
            text = self._text[1:] if self._text.startswith("\n") else self._text
            self.fields.append(
                {
                    "name": attrs["name"],
                    "type": "textarea",
                    "value": text,
                    "checked": False,
                }
            )
            self._in = None


def parse_form(output):
    form = EditForm()
    form.feed(output)
    return form


@pytest.mark.django_db
@test_context
def test_edit_form_round_trip_through_build_body(todo_list, todo_owner_login):
    """Render the owner's edit form, apply what the client applies (tick the
    first item, add one, rename the second), PUT the body it builds with the
    frame's ETag: 200, and everything untouched is kept exactly, a multi-line
    string among it."""
    require_node()
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    todo_list.content = {**todo_list.content, "notes": "line 1\nline 2\n\nline 4"}
    todo_list.save()
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    form = parse_form(Http.response.content.decode())
    assert form.frame["hx-put"] == url and form.content["notes"].count("\n") == 3

    fields = form.fields
    by_name = {f["name"]: f for f in fields}
    assert "/title" in by_name and "/items/-/text" in by_name
    by_name["/items/0/done"]["checked"] = True
    by_name["/items/1/text"]["value"] = "Buy eggs and flour"
    by_name["/items/-/text"]["value"] = "Buy bread"
    by_name["/items/-/done"]["checked"] = True
    built = run_node(
        BUILD_SCRIPT, stdin=json.dumps({"content": form.content, "fields": fields})
    )
    assert built["errors"] == []
    body = built["body"]
    assert body["items"] == [
        {"text": "Buy milk", "done": True},
        {"text": "Buy eggs and flour", "done": True},
        {"text": "Buy bread", "done": True},
    ]
    assert body["title"] == "Groceries"
    assert body["rbac_context"] == "project-a"

    etag = form.frame["data-scrud-etag"]
    When.http.put(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IF_MATCH=etag,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    todo_list.refresh_from_db()
    assert todo_list.content == body
    assert todo_list.content["notes"] == "line 1\nline 2\n\nline 4"
