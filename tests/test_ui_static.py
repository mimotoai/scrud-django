"""Static assets of the UI: vendored htmx and the ``scrud-json`` extension.

The node tests here cover the pure helpers of scrud-ui.js (against a stub DOM
where one is needed; they skip without node, `require_node`). The htmx wiring
was verified manually in a browser; no automated browser test exists. The
other checks are Python-side."""

import json
import os
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


def require_node():
    """Skip the test when node is not on PATH: the scrud-ui.js helpers run
    under node. CI's test-mysql job installs it and sets
    SCRUD_TEST_REQUIRE_NODE, which turns the skip into a failure."""
    if shutil.which("node") is not None:
        return
    reason = (
        "node is not on PATH: the scrud-ui.js tests need Node.js"
        " (CI's test-mysql job installs it)"
    )
    if os.environ.get("SCRUD_TEST_REQUIRE_NODE"):
        pytest.fail(reason + "; SCRUD_TEST_REQUIRE_NODE is set")
    pytest.skip(reason)


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


def test_delete_does_not_send_form_values_as_url_params():
    """htmx 2.0.4 puts a form's values in the URL for DELETE by default, and
    the Delete button sits inside the root edit form."""
    source = SCRUD_UI.read_text()
    assert "htmx.config.methodsThatUseUrlParams = ['get'];" in source


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
    require_node()
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
    # a cleared number never deletes the stored one (buildBody reports it)
    assert out["number"] == {"n": 1}
    assert out["untouched"]["items"] == [{"text": "a", "done": False}]


def test_apply_pointer_ignores_prototype_keys():
    require_node()
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


POINTER_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const warnings = [];
const ctx = vm.createContext({
  window, htmx: {config: {}, defineExtension() {}},
  console: {warn: (m) => warnings.push(m)},
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const attempt = (doc, pointer, value) => {
  try { U.applyPointer(doc, pointer, value); return doc; }
  catch (e) { return 'error'; }
};
const out = {};
out.length = attempt({l: [1, 2]}, '/l/length', '0');
out.range = attempt({l: [1, 2]}, '/l/5', 'x');
out.append = attempt({l: [1, 2]}, '/l/2', 'x');
out.zero = attempt({l: [1, 2]}, '/l/01', 'x');
out.hyphenKey = U.buildBody({'-x': []}, [
  {name: '/-x/-/n', type: 'text', value: 'v'},
]);
out.keepsOthers = U.buildBody({l: [1, 2], t: 'a'}, [
  {name: '/l/length', type: 'text', value: '0'},
  {name: '/t', type: 'text', value: 'b'},
]);
out.noShift = U.buildBody({l: [{n: 1}, {n: 2}]}, [
  {name: '/l/0/n', type: 'number', value: ''},
]);
out.warned = warnings.length;
console.log(JSON.stringify(out));
"""


def test_apply_pointer_arrays_and_groups_under_node():
    require_node()
    done = subprocess.run(
        ["node", "-e", POINTER_SCRIPT, str(SCRUD_UI)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["length"] == "error"
    assert out["range"] == "error"
    assert out["append"] == {"l": [1, 2, "x"]}
    assert out["zero"] == "error"
    assert out["hyphenKey"] == {"-x": [{"n": "v"}]}
    assert out["keepsOthers"] == {"l": [1, 2], "t": "b"}
    assert out["noShift"] == {"l": [{"n": 1}, {"n": 2}]}
    assert out["warned"] == 1


def test_confirm_handler_returns_early_without_triggering_target():
    source = SCRUD_UI.read_text()
    assert "if (!triggering || !triggering.target) return;" in source


UNTOUCHED_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const ctx = vm.createContext({window, htmx: {config: {}, defineExtension() {}}});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const content = {
  title: 'line 1\nline 2', crlf: 'a\r\nb', n: 1.5,
  items: [{text: 'x\ny', done: false}],
};
const out = {};
// A text input's value has lost the newlines: untouched, it changes nothing.
out.checkbox = U.buildBody(content, [
  {name: '/title', type: 'text', value: 'line 1line 2'},
  {name: '/items/0/text', type: 'text', value: 'xy'},
  {name: '/n', type: 'number', value: '1.5'},
  {name: '/items/0/done', type: 'checkbox', checked: true},
]);
// A textarea's value has LF line breaks (CRLF normalized): untouched.
out.textarea = U.buildBody(content, [
  {name: '/crlf', type: 'textarea', value: 'a\nb'},
  {name: '/title', type: 'textarea', value: 'line 1\nline 2'},
]);
// Edits still apply, including one that only removes a line break.
out.edited = U.buildBody(content, [
  {name: '/title', type: 'text', value: 'new'},
  {name: '/crlf', type: 'textarea', value: 'ab'},
  {name: '/n', type: 'number', value: '2'},
]);
console.log(JSON.stringify(out));
"""


def test_build_body_keeps_untouched_multiline_strings():
    require_node()
    done = subprocess.run(
        ["node", "-e", UNTOUCHED_SCRIPT, str(SCRUD_UI)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["checkbox"] == {
        "title": "line 1\nline 2",
        "crlf": "a\r\nb",
        "n": 1.5,
        "items": [{"text": "x\ny", "done": True}],
    }
    assert out["textarea"]["crlf"] == "a\r\nb"
    assert out["textarea"]["title"] == "line 1\nline 2"
    assert out["edited"]["title"] == "new"
    assert out["edited"]["crlf"] == "ab"
    assert out["edited"]["n"] == 2


# A minimal DOM: elements with attributes, parents, `closest`, `matches` and
# `querySelectorAll('[name^="/"]')`, and a document that records its event
# listeners and the events dispatched on it.
STUB_DOM = r"""
const vm = require('vm'), fs = require('fs');
const handlers = {}, dispatched = [];
const document = {
  addEventListener(name, fn) { handlers[name] = fn; },
  querySelector() { return null; },
  dispatchEvent(e) { dispatched.push({type: e.type, detail: e.detail}); return true; },
  createElement(tag) { return el(tag.toUpperCase()); },
};
function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; }
function el(tag, attrs, parent, props) {
  const node = Object.assign({
    tagName: tag, attrs: attrs || {}, parentNode: null, children: [],
    textContent: '',
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
    hasAttribute(n) { return n in this.attrs; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parentNode = this;
      const at = ref ? this.children.indexOf(ref) : -1;
      if (at < 0) this.children.push(c); else this.children.splice(at, 0, c);
      return c;
    },
    setAttribute(n, v) { this.attrs[n] = v; },
    get firstChild() { return this.children[0] || null; },
    removeChild(c) {
      this.children = this.children.filter((x) => x !== c);
      c.parentNode = null;
      return c;
    },
    matches(sel) {
      const attr = /^\[([a-z-]+)\]$/.exec(sel);
      if (attr) return attr[1] in this.attrs;
      return sel === '.scrud-resource' && this.attrs.class === 'scrud-resource';
    },
    closest(sel) {
      for (let n = this; n; n = n.parentNode) if (n.matches(sel)) return n;
      return null;
    },
    querySelectorAll(sel) {
      const found = [];
      const scripts = sel === 'script.scrud-content';
      const walk = (n) => n.children.forEach((c) => {
        if (scripts) {
          if (c.tagName === 'SCRIPT' && c.attrs.class === 'scrud-content') found.push(c);
        } else if (typeof c.name === 'string' && c.name.charAt(0) === '/') {
          found.push(c);
        }
        walk(c);
      });
      walk(this);
      return found;
    },
  }, props || {});
  if (attrs && 'name' in attrs) node.name = attrs.name;
  if (parent) parent.appendChild(node);
  return node;
}
const frame = (href, parent, pointer) =>
  el('FORM', {class: 'scrud-resource', 'data-scrud-href': href,
              'data-scrud-pointer': pointer || ''}, parent);
const htmx = {config: {}, defineExtension() {}};
const window = {htmx};
const ctx = vm.createContext({window, document, CustomEvent, console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const out = {};
"""

DELETE_SCRIPT = (
    STUB_DOM
    + r"""
const ul = el('UL');
const li1 = el('LI', {}, ul), li2 = el('LI', {}, ul);
const one = frame('/l/1/', li1), two = frame('/l/2/', li2);
const button = el('BUTTON', {'hx-delete': '/l/1/'}, one);
const after = handlers['htmx:afterRequest'];
const fire = (detail) => after({detail: Object.assign({elt: button}, detail)});
fire({successful: false, requestConfig: {verb: 'delete'},
      pathInfo: {requestPath: '/l/1/'}});
out.afterFailure = ul.children.length;
fire({successful: true, requestConfig: {verb: 'put'},
      pathInfo: {requestPath: '/l/1/'}});
out.afterPut = ul.children.length;
fire({successful: true, requestConfig: {verb: 'delete'},
      pathInfo: {requestPath: '/l/1/'}});
out.afterDelete = ul.children.length;
out.removed = one.parentNode === null && li1.parentNode === null;
out.kept = ul.children[0] === li2 && li2.children[0] === two;
out.dispatched = dispatched;
console.log(JSON.stringify(out));
"""
)


def test_delete_removes_the_frame():
    source = SCRUD_UI.read_text()
    assert "htmx:afterRequest" in source and "scrud:deleted" in source
    require_node()
    done = subprocess.run(
        ["node", "-e", DELETE_SCRIPT, str(SCRUD_UI)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["afterFailure"] == 2
    assert out["afterPut"] == 2
    assert out["afterDelete"] == 1
    assert out["removed"] is True and out["kept"] is True
    assert out["dispatched"] == [{"type": "scrud:deleted", "detail": {"href": "/l/1/"}}]


FIELDS_SCRIPT = (
    STUB_DOM
    + r"""
const outer = frame('/a/');
el('INPUT', {name: '/title'}, outer, {type: 'text', value: 't'});
const embedded = frame('/b/', el('DIV', {}, outer));
el('INPUT', {name: '/title'}, embedded, {type: 'text', value: 'other'});
el('INPUT', {name: '/secret'}, embedded, {type: 'checkbox', checked: true});
const node = frame('/a/', outer, '/items/0');
el('INPUT', {name: '/items/0/done'}, node, {type: 'checkbox', checked: true});
el('INPUT', {name: '/off'}, outer, {type: 'text', value: 'x', disabled: true});
out.outer = U.fieldsOf(outer).map((f) => f.name);
out.embedded = U.fieldsOf(embedded).map((f) => f.name);
out.node = U.fieldsOf(node).map((f) => f.name);
console.log(JSON.stringify(out));
"""
)


def test_fields_of_keeps_only_the_frames_resource():
    require_node()
    done = subprocess.run(
        ["node", "-e", FIELDS_SCRIPT, str(SCRUD_UI)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    # the embedded resource's inputs are its own, never the outer frame's
    assert out["outer"] == ["/title", "/items/0/done"]
    assert out["embedded"] == ["/title", "/secret"]
    assert out["node"] == ["/items/0/done"]


NULL_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const ctx = vm.createContext({window, htmx: {config: {}, defineExtension() {}}});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const content = {a: null, t: null, done: false, items: [null]};
const out = {};
// Empty text-like inputs over null or a missing key stay as they were.
out.untouched = U.buildBody(content, [
  {name: '/a', type: 'text', value: ''},
  {name: '/missing', type: 'text', value: ''},
  {name: '/t', type: 'textarea', value: ''},
  {name: '/u', type: 'url', value: ''},
  {name: '/d', type: 'date', value: ''},
  {name: '/n', type: 'number', value: ''},
  {name: '/done', type: 'checkbox', checked: true},
]);
// Typing a value over null applies.
out.typed = U.buildBody(content, [{name: '/a', type: 'text', value: 'x'}]);
// An empty string over a present string is a real edit (clearing).
out.cleared = U.buildBody({s: 'x'}, [{name: '/s', type: 'text', value: ''}]);
// Append-row inputs are unchanged: a group with a value still appends.
out.append = U.buildBody({l: []}, [{name: '/l/-', type: 'text', value: 'v'}]);
console.log(JSON.stringify(out));
"""


def test_build_body_keeps_null_and_missing_under_empty_inputs():
    require_node()
    done = subprocess.run(
        ["node", "-e", NULL_SCRIPT, str(SCRUD_UI)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["untouched"] == {
        "a": None,
        "t": None,
        "done": True,
        "items": [None],
    }
    assert out["typed"]["a"] == "x"
    assert out["cleared"] == {"s": ""}
    assert out["append"] == {"l": ["v"]}


PRIMITIVE_APPEND_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const ctx = vm.createContext({window, htmx: {config: {}, defineExtension() {}}});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const out = {};
out.number = U.buildBody({nums: [1]}, [{name: '/nums/-', type: 'number', value: '5'}]);
out.fraction = U.buildBody({nums: []}, [{name: '/nums/-', type: 'number', value: '2.5'}]);
out.empty = U.buildBody({nums: [1]}, [{name: '/nums/-', type: 'number', value: ''}]);
out.flag = U.buildBody({f: []}, [{name: '/f/-', type: 'checkbox', checked: true}]);
console.log(JSON.stringify(out));
"""


def test_build_body_appends_typed_primitive_items():
    require_node()
    done = subprocess.run(
        ["node", "-e", PRIMITIVE_APPEND_SCRIPT, str(SCRUD_UI)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["number"] == {"nums": [1, 5]}
    assert out["fraction"] == {"nums": [2.5]}
    assert out["empty"] == {"nums": [1]}
    assert out["flag"] == {"f": [True]}


INVALID_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const window = {};
const ctx = vm.createContext({window, htmx: {config: {}, defineExtension() {}}});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = window.ScrudUI;
const out = {};
const run = (content, fields) => {
  const errors = [];
  const body = U.buildBody(content, fields, errors);
  return {body, errors};
};
out.unsafe = run({t: 'a'}, [
  {name: '/__proto__/x', type: 'text', value: '1'},
  {name: '/a/constructor/b', type: 'text', value: '1'},
  {name: '/items/-/prototype', type: 'text', value: '1'},
  {name: '/t', type: 'text', value: 'b'},
]);
out.bad = run({l: [1], t: 'a'}, [
  {name: 'no-slash', type: 'text', value: 'x'},
  {name: '/l/5', type: 'text', value: 'x'},
  {name: '/t', type: 'text', value: 'b'},
]);
out.clean = run({t: 'a'}, [{name: '/t', type: 'text', value: 'b'}]);
// numbers: empty or NaN over a stored number is an error, the key is kept
out.cleared = run({n: 3, m: 4}, [
  {name: '/n', type: 'number', value: ''},
  {name: '/m', type: 'number', value: 'abc'},
]);
// untouched: empty over null or missing is left as it is, with no error
out.untouchedNumber = run({n: null}, [
  {name: '/n', type: 'number', value: ''},
  {name: '/k', type: 'number', value: ''},
]);
out.edited = run({n: 3}, [{name: '/n', type: 'number', value: '4'}]);
// no errors argument: still returns the body
out.noArg = U.buildBody({n: 3}, [{name: '/n', type: 'number', value: ''}]);
console.log(JSON.stringify(out));
"""


def test_build_body_reports_invalid_fields_and_number_clears():
    require_node()
    done = subprocess.run(
        ["node", "-e", INVALID_SCRIPT, str(SCRUD_UI)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["unsafe"]["errors"] == [
        "/__proto__/x",
        "/a/constructor/b",
        "/items/-/prototype",
    ]
    assert out["unsafe"]["body"] == {"t": "b"}
    assert out["bad"]["errors"] == ["no-slash", "/l/5"]
    assert out["bad"]["body"] == {"l": [1], "t": "b"}
    assert out["clean"] == {"body": {"t": "b"}, "errors": []}
    assert out["cleared"]["errors"] == ["/n", "/m"]
    assert out["cleared"]["body"] == {"n": 3, "m": 4}
    assert out["untouchedNumber"] == {"body": {"n": None}, "errors": []}
    assert out["edited"] == {"body": {"n": 4}, "errors": []}
    assert out["noArg"] == {"n": 3}


def test_client_listens_for_failed_requests():
    source = SCRUD_UI.read_text()
    assert "'htmx:responseError'" in source and "'htmx:sendError'" in source
    assert "'htmx:beforeSwap'" in source
    assert "textContent" in source and "innerHTML" not in source


NOTICE_SCRIPT = (
    STUB_DOM
    + r"""
out.texts = [
  U.noticeText(404, 'put'), U.noticeText(0, 'put'), U.noticeText(undefined, 'put'),
  U.noticeText(500, 'delete'), U.noticeText(0, 'delete'),
  U.noticeText(500, 'put'), U.noticeText(503, 'delete'), U.noticeText(404, 'get'),
  U.noticeText(0, 'get'),
];
out.invalid = [U.invalidFieldsText(['/a']), U.invalidFieldsText(['/a', '/b'])];
out.shows = [
  ['put', 404], ['put', 400], ['put', 412], ['delete', 409], ['delete', 412],
  ['delete', 403], ['delete', 404], ['put', 500], ['delete', 428],
].map(([v, s]) => U.showsClientNotice(v, s));

const root = frame('/l/1/');
const content = el('SCRIPT', {class: 'scrud-content'}, root);
const input = el('INPUT', {name: '/n'}, root, {type: 'number', value: ''});
const button = el('BUTTON', {'hx-delete': '/l/1/'}, root);
const notices = () => root.children.filter((c) => /scrud-client-notice/.test(c.className || ''));

// a failed PUT: a notice at the top of the frame, set with textContent
handlers['htmx:responseError']({detail: {
  elt: root, xhr: {status: 404}, requestConfig: {verb: 'put'}}});
out.afterError = notices().map((n) => ({
  text: n.textContent, role: n.attrs.role, cls: n.className, first: root.children[0] === n}));
// a second one replaces the first
handlers['htmx:sendError']({detail: {elt: root, requestConfig: {verb: 'put'}}});
out.afterSend = notices().map((n) => n.textContent);
out.kept = root.children.includes(input) && root.parentNode === null;
// the server re-renders a refused PUT: no client notice for a swapped status
const other = frame('/l/2/');
handlers['htmx:responseError']({detail: {
  elt: other, xhr: {status: 400}, requestConfig: {verb: 'put'}}});
out.swapped = other.children.length;
// a failed DELETE (403) is not swapped, and its notice goes in the frame
const evt = {detail: {elt: button, xhr: {status: 403}, requestConfig: {verb: 'delete'},
  shouldSwap: true, isError: true}};
handlers['htmx:beforeSwap'](evt);
out.deleteSwap = evt.detail.shouldSwap;
const evt409 = {detail: {elt: button, xhr: {status: 409}, requestConfig: {verb: 'delete'},
  shouldSwap: true, isError: true}};
handlers['htmx:beforeSwap'](evt409);
out.delete409Swap = evt409.detail.shouldSwap;
handlers['htmx:responseError']({detail: {
  elt: button, xhr: {status: 403}, requestConfig: {verb: 'delete'}}});
out.afterDelete = notices().map((n) => n.textContent);
// a failed GET (a reload frame's) is a load failure, not an unsaved change
const gframe = frame('/l/3/');
handlers['htmx:responseError']({detail: {
  elt: gframe, xhr: {status: 404}, requestConfig: {verb: 'get'}}});
handlers['htmx:responseError']({detail: {
  elt: gframe, xhr: {status: 400}, requestConfig: {verb: 'get'}}});
out.afterGet = gframe.children.filter((c) => /scrud-client-notice/.test(c.className || ''))
  .map((n) => n.textContent);
handlers['htmx:sendError']({detail: {elt: gframe, requestConfig: {verb: 'get'}}});
out.afterGetSend = gframe.children.filter((c) => /scrud-client-notice/.test(c.className || ''))
  .map((n) => n.textContent);
console.log(JSON.stringify(out));
"""
)


def test_failed_requests_show_a_client_notice():
    require_node()
    done = subprocess.run(
        ["node", "-e", NOTICE_SCRIPT, str(SCRUD_UI)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["texts"] == [
        "Your change was not saved (HTTP 404).",
        "Your change was not saved (network error).",
        "Your change was not saved (network error).",
        "Something went wrong (HTTP 500). Reload to see the current state.",
        "The item was not deleted (network error).",
        "Something went wrong (HTTP 500). Reload to see the current state.",
        "Something went wrong (HTTP 503). Reload to see the current state.",
        "This item could not be loaded (HTTP 404).",
        "This item could not be loaded (network error).",
    ]
    assert out["invalid"] == [
        "This form could not be saved: invalid field /a.",
        "This form could not be saved: invalid fields /a, /b.",
    ]
    # swapped by the server: PUT 400, 412; DELETE 409, 412 (a notice and a reload)
    assert out["shows"] == [True, False, False, False, False, True, True, True, True]
    assert out["afterError"] == [
        {
            "text": "Your change was not saved (HTTP 404).",
            "role": "alert",
            "cls": "scrud-notice scrud-notice-error scrud-client-notice",
            "first": True,
        }
    ]
    assert out["afterSend"] == ["Your change was not saved (network error)."]
    assert out["kept"] is True
    assert out["swapped"] == 0
    assert out["deleteSwap"] is False and out["delete409Swap"] is True
    assert out["afterDelete"] == ["The item was not deleted (HTTP 403)."]
    assert out["afterGet"] == ["This item could not be loaded (HTTP 400)."]
    assert out["afterGetSend"] == ["This item could not be loaded (network error)."]


CANCEL_SCRIPT = (
    STUB_DOM
    + r"""
const root = frame('/l/1/');
root.attrs['hx-put'] = '/l/1/';
const content = el('SCRIPT', {class: 'scrud-content'}, root, {textContent: '{"n": 3, "t": "a"}'});
const n = el('INPUT', {name: '/n'}, root, {type: 'number', value: ''});
const t = el('INPUT', {name: '/t'}, root, {type: 'text', value: 'b'});
const bad = el('INPUT', {name: '/__proto__/x'}, root, {type: 'text', value: '1'});
const notices = () => root.children.filter((c) => /scrud-client-notice/.test(c.className || ''));
const configure = () => {
  let prevented = false;
  handlers['htmx:configRequest']({
    detail: {elt: root, verb: 'put', headers: {}},
    preventDefault() { prevented = true; },
  });
  return prevented;
};
out.first = configure();
out.text = notices().map((c) => c.textContent);
// fix the inputs: the request goes ahead and the notice is gone
n.value = '4';
bad.disabled = true;
out.second = configure();
out.after = notices().length;
console.log(JSON.stringify(out));
"""
)


def test_invalid_fields_cancel_the_put_and_show_a_notice():
    source = SCRUD_UI.read_text()
    assert "preventDefault" in source
    require_node()
    done = subprocess.run(
        ["node", "-e", CANCEL_SCRIPT, str(SCRUD_UI)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["first"] is True
    assert out["text"] == [
        "This form could not be saved: invalid fields /n, /__proto__/x."
    ]
    assert out["second"] is False and out["after"] == 0


def test_scrud_ui_logs_missing_htmx_and_cancelled_outer_frames():
    source = SCRUD_UI.read_text()
    assert (
        "console.error('scrud-ui: htmx is not loaded; scrud-ui.js is inactive')"
        in source
    )
    assert "console.debug(" in source
    # The debug call names the frame href and sits before the cancellation.
    cancel = source[source.index("console.debug(") :]
    assert cancel.index("data-scrud-href") < cancel.index("evt.preventDefault()")
