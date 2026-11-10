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


APPLY_OP_SCRIPT = (
    STUB_DOM
    + r"""
const rules = () => [{select: 'a'}, {select: 'b'}, {select: 'c'}];
const rows = (body) => body.rules.map((r) => r.select).join('');
const doc = () => ({rules: rules(), keep: 1});
out.remove = rows(U.applyArrayOp(doc(), {op: 'remove', pointer: '/rules/1'}));
out.removeKeeps = U.applyArrayOp(doc(), {op: 'remove', pointer: '/rules/1'}).keep;
out.down = rows(U.applyArrayOp(doc(), {op: 'move', pointer: '/rules/0', to: 1}));
out.downEnd = rows(U.applyArrayOp(doc(), {op: 'move', pointer: '/rules/0', to: 2}));
out.up = rows(U.applyArrayOp(doc(), {op: 'move', pointer: '/rules/2', to: '1'}));
out.same = rows(U.applyArrayOp(doc(), {op: 'move', pointer: '/rules/1', to: 1}));
out.nested = U.applyArrayOp({a: {b: [1, 2, 3]}}, {op: 'remove', pointer: '/a/b/0'}).a.b;
out.root = U.applyArrayOp([1, 2], {op: 'remove', pointer: '/1'});
// never a throw or a change for what is not an array element
out.badIndex = rows(U.applyArrayOp(doc(), {op: 'remove', pointer: '/rules/9'}));
out.badTo = rows(U.applyArrayOp(doc(), {op: 'move', pointer: '/rules/0', to: 7}));
out.notArray = JSON.stringify(U.applyArrayOp({a: {b: 1}}, {op: 'remove', pointer: '/a/b'}));
out.badOp = rows(U.applyArrayOp(doc(), {op: 'explode', pointer: '/rules/0'}));
out.badPointer = rows(U.applyArrayOp(doc(), {op: 'remove', pointer: 'rules/0'}));
console.log(JSON.stringify(out));
"""
)


def test_apply_array_op_remove_and_move():
    require_node()
    out = run_node(APPLY_OP_SCRIPT)
    assert out["remove"] == "ac" and out["removeKeeps"] == 1
    assert out["down"] == "bac" and out["downEnd"] == "bca"
    assert out["up"] == "acb" and out["same"] == "abc"
    assert out["nested"] == [2, 3] and out["root"] == [1]
    for unchanged in ("badIndex", "badTo", "badOp", "badPointer"):
        assert out[unchanged] == "abc", unchanged
    assert out["notArray"] == '{"a":{"b":1}}'


ARRAY_OP_SCRIPT = (
    STUB_DOM.replace(
        "const htmx = {config: {}, defineExtension() {}};",
        "const exts = {}, triggered = [];"
        "const htmx = {config: {}, defineExtension(n, e) { exts[n] = e; },"
        " trigger(elt, name) { triggered.push({elt, name}); }};",
    )
    + r"""
const root = frame('/l/1/');
root.attrs['data-scrud-etag'] = '"e1"';
root.attrs['hx-put'] = '/l/1/';
el('SCRIPT', {class: 'scrud-content'}, root,
   {textContent: '{"rules": [{"select": "a"}, {"select": "b"}, {"select": "c"}]}'});
const li = el('LI', {}, root);
el('INPUT', {name: '/rules/1/select'}, li, {type: 'text', value: 'B2'});
const op = (name, pointer, to) => {
  const attrs = {'data-scrud-array-op': name, 'data-scrud-pointer': pointer};
  if (to !== undefined) attrs['data-scrud-to'] = to;
  return el('BUTTON', attrs, li);
};
let prevented = 0;
const click = (target) =>
  handlers.click({target, preventDefault() { prevented++; }});
const put = () => {
  const detail = {elt: root, verb: 'put', headers: {}};
  handlers['htmx:configRequest']({detail, preventDefault() { detail.prevented = true; }});
  return detail;
};
const encode = () => JSON.parse(exts['scrud-json'].encodeParameters({}, {}, root));
// something that is no array button does nothing
click(el('SPAN', {}, li));
out.idle = triggered.length;
click(op('remove', '/rules/0'));
out.triggers = triggered.map((t) => [t.elt === root, t.name]);
out.prevented = prevented;
const sent = put();
out.headers = sent.headers;
out.sentPrevented = !!sent.prevented;
out.body = encode().rules.map((r) => r.select);
// the operation is spent: the next PUT is an ordinary one
out.next = encode().rules.map((r) => r.select);
click(op('move', '/rules/0', '2'));
put();
out.moved = encode().rules.map((r) => r.select);
// a PUT that is refused (an invalid input) leaves no operation behind
click(op('remove', '/rules/2'));
el('INPUT', {name: '/rules/__proto__/x'}, li, {type: 'text', value: 'z'});
out.refused = put().prevented === true;
li.children = li.children.filter((c) => c.name !== '/rules/__proto__/x');
out.afterRefused = encode().rules.map((r) => r.select);
out.responseHandling = htmx.config.responseHandling.find((h) => new RegExp('^' + h.code + '$').test('412'));
console.log(JSON.stringify(out));
"""
)


def test_array_op_sends_put_with_if_match():
    require_node()
    out = run_node(ARRAY_OP_SCRIPT)
    assert out["idle"] == 0
    # the click triggers the frame's own PUT, through htmx
    assert out["triggers"] == [[True, "change"]] and out["prevented"] == 1
    assert out["headers"]["If-Match"] == '"e1"'
    assert out["headers"]["Content-Type"] == "application/json"
    assert not out["sentPrevented"]
    # the input is applied at its pre-operation pointer, then rule 0 removed
    assert out["body"] == ["B2", "c"]
    assert out["next"] == ["a", "B2", "c"]
    assert out["moved"] == ["B2", "c", "a"]
    assert out["refused"] is True
    assert out["afterRefused"] == ["a", "B2", "c"]


@pytest.mark.django_db
@test_context
def test_array_op_after_stale_etag_gets_reload(todo_list, todo_owner_login):
    """Review Focus 4: a removal built from a stale page sends If-Match, gets
    the 412 and the reload frame, and is never saved."""
    require_node()
    out = run_node(ARRAY_OP_SCRIPT)
    # the client swaps the 412 (the reload notice) in
    assert out["responseHandling"]["swap"] is True
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    form = parse_form(Http.response.content.decode())
    stale = form.frame["data-scrud-etag"]
    body = run_node(
        APPLY_ITEMS_SCRIPT,
        stdin=json.dumps({"content": form.content, "pointer": "/items/0"}),
    )
    # another tab saves the same list first
    When.http.put(
        url,
        data=json.dumps({**form.content, "title": "Other tab"}),
        content_type="application/json",
        HTTP_IF_MATCH=stale,
    )
    Then.http.status_code_is(200)
    When.http.put(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IF_MATCH=stale,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(412)
    output = Http.response.content.decode()
    assert 'class="scrud-reload"' in output and 'hx-trigger="load"' in output
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Other tab"
    assert len(todo_list.content["items"]) == len(form.content["items"])


APPLY_ITEMS_SCRIPT = (
    STUB_DOM
    + r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(U.applyArrayOp(input.content, {op: 'remove', pointer: input.pointer})));
"""
)


JSON_SCRIPT = (
    STUB_DOM
    + r"""
const page = el('MAIN', {});
const marker = el('DIV', {class: 'scrud-branch-page', 'data-scrud-branch': 'wip'}, page);
document.querySelector = (sel) =>
  sel === 'meta[name=csrf-token]' ? el('META', {content: 'csrf-123'})
  : sel === '.scrud-branch-page[data-scrud-branch]' ? marker : null;
const exts = {};
htmx.defineExtension = (name, ext) => { exts[name] = ext; };
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const fire = (elt, verb) => {
  const detail = {elt, verb, headers: {}};
  handlers['htmx:configRequest']({detail, preventDefault() { detail.prevented = true; }});
  return {headers: detail.headers, prevented: !!detail.prevented};
};
const encode = (elt) => exts['scrud-json'].encodeParameters({}, {}, elt);
const logged = [];
console.error = (m) => logged.push(m);

// a create form outside any frame
const form = el('FORM', {'hx-ext': 'scrud-json', 'hx-post': '/l/', 'data-scrud-json': 'form'}, page);
el('INPUT', {name: '/title'}, form, {type: 'text', value: 'Groceries'});
el('INPUT', {name: '/items/-/text'}, form, {type: 'text', value: 'milk'});
el('INPUT', {name: '/items/-/done'}, form, {type: 'checkbox', checked: true});
el('INPUT', {name: '/count', 'data-scrud-type': 'number'}, form, {type: 'text', value: '3'});
el('INPUT', {name: '/off'}, form, {type: 'text', value: 'x', disabled: true});
out.form = {sent: fire(form, 'post'), body: JSON.parse(encode(form))};

// a fixed body, unchanged byte for byte
const fixedText = '{"source":"a",  "target":"b","state":"x"}';
const push = el('BUTTON', {'hx-ext': 'scrud-json', 'hx-post': '/push/',
  'data-scrud-json': 'fixed', 'data-scrud-body': fixedText}, page);
out.fixed = {sent: fire(push, 'post'), body: encode(push), same: encode(push) === fixedText};

// DELETE with If-Match from the element, and from its frame
const revert = el('BUTTON', {'hx-ext': 'scrud-json', 'hx-delete': '/o/1/',
  'data-scrud-json': 'fixed', 'data-scrud-body': '{}', 'data-scrud-etag': 'W/"abc"'}, page);
out.deleteOwn = fire(revert, 'delete');
const fr = frame('/o/2/', page);
fr.attrs['data-scrud-etag'] = '"frame-tag"';
const inFrame = el('BUTTON', {'hx-ext': 'scrud-json', 'hx-delete': '/o/2/',
  'data-scrud-json': 'fixed', 'data-scrud-body': '{}'}, fr);
out.deleteFrame = fire(inFrame, 'delete');
out.deleteNoTag = fire(push, 'delete');

// bad JSON: cancelled, notice in the frame, or logged outside one
const badIn = el('BUTTON', {'hx-ext': 'scrud-json', 'hx-post': '/x/',
  'data-scrud-json': 'fixed', 'data-scrud-body': '{nope'}, fr);
out.badFrame = fire(badIn, 'post');
out.notice = fr.children[0].textContent;
const badOut = el('BUTTON', {'hx-ext': 'scrud-json', 'hx-post': '/x/',
  'data-scrud-json': 'fixed', 'data-scrud-body': '{nope'}, page);
out.badOutside = fire(badOut, 'post');
out.logged = logged.slice();
const noBody = el('BUTTON', {'hx-post': '/x/', 'data-scrud-json': 'fixed'}, page);
out.noBody = fire(noBody, 'post').prevented;
console.log(JSON.stringify(out));
"""
)


def test_form_json_post_builds_body_from_pointers():
    require_node()
    out = run_node(JSON_SCRIPT)["form"]
    assert out["body"] == {
        "title": "Groceries",
        "items": [{"text": "milk", "done": True}],
        "count": 3,
    }
    headers = out["sent"]["headers"]
    assert not out["sent"]["prevented"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-CSRFToken"] == "csrf-123"
    assert headers["Scrud-UI-Branch"] == "wip"
    assert headers["Accept"] == "text/html"
    assert "If-Match" not in headers


def test_fixed_json_post_sends_body_unchanged():
    require_node()
    out = run_node(JSON_SCRIPT)["fixed"]
    assert out["same"] is True
    assert out["sent"]["headers"]["Content-Type"] == "application/json"
    assert out["sent"]["headers"]["X-CSRFToken"] == "csrf-123"


def test_json_delete_with_if_match():
    require_node()
    out = run_node(JSON_SCRIPT)
    assert out["deleteOwn"]["headers"]["If-Match"] == 'W/"abc"'
    assert out["deleteOwn"]["headers"]["Content-Type"] == "application/json"
    assert out["deleteFrame"]["headers"]["If-Match"] == '"frame-tag"'
    assert "If-Match" not in out["deleteNoTag"]["headers"]


def test_bad_fixed_body_cancels_with_notice():
    require_node()
    out = run_node(JSON_SCRIPT)
    assert out["badFrame"]["prevented"] is True
    assert out["notice"] == "This action is misconfigured."
    # outside any frame: logged, still cancelled
    assert out["badOutside"]["prevented"] is True
    assert out["logged"] == ["scrud-ui: This action is misconfigured."]
    # a fixed element with no body at all is misconfigured too
    assert out["noBody"] is True


def test_existing_put_unchanged():
    """A PUT from a frame without data-scrud-json takes the old path."""
    require_node()
    out = run_node(HEADERS_SCRIPT)
    assert out["put"]["headers"]["Content-Type"] == "application/json"
    assert out["put"]["headers"]["If-Match"] == '"inner-etag"'
    assert out["put"]["headers"]["Scrud-UI-Pointer"] == "/items/0"
    script = (
        STUB_DOM
        + r"""
const exts = {};
htmx.defineExtension = (n, e) => { exts[n] = e; };
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const root = frame('/l/1/');
root.attrs['hx-put'] = '/l/1/';
el('SCRIPT', {class: 'scrud-content'}, root, {textContent: '{"title": "a", "keep": 1}'});
el('INPUT', {name: '/title'}, root, {type: 'text', value: 'b'});
out.body = JSON.parse(exts['scrud-json'].encodeParameters({}, {}, root));
out.notPut = exts['scrud-json'].encodeParameters({}, {}, el('DIV', {}, root));
console.log(JSON.stringify(out));
"""
    )
    got = run_node(script)
    assert got["body"] == {"title": "b", "keep": 1} and got["notPut"] is None
