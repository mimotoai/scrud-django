"""studio.js as the browser runs it, under node: the real studio page and a
real preview, rendered by Django and parsed into a small stub DOM (the
preview as the iframe's ``contentDocument``), with ``fetch`` answering from
a table of routes. Each test records what the script fetched and what the
page then shows. Elements the script makes are recorded too, and setting
``innerHTML`` (or ``outerHTML``, or calling ``insertAdjacentHTML``) throws:
the panel is filled with text and DOM nodes only."""

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

import scrud_django.ui.studio
from scrud_django import collection_type_uri_for
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import put_item
from .test_ui_branch_types import make_branch
from .test_ui_branch_versions import versions_of
from .test_ui_embeds import host, make_list, url_of
from .test_ui_http import grant, make_component
from .test_ui_static import require_node

STUDIO_JS = (
    Path(scrud_django.ui.studio.__file__).parent / "static/scrud_studio/studio.js"
)
SLUG = "todo-lists"
POINTER = "/items/0/text"
VOID = {"area", "br", "col", "hr", "img", "input", "link", "meta", "source", "wbr"}


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


class Tree(HTMLParser):
    """HTML as nested ``{tag, attrs, children}`` and ``{text}`` nodes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#document", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        self.stack[-1]["children"].append({"tag": tag, "attrs": attrs, "children": []})

    def handle_endtag(self, tag):
        for at in range(len(self.stack) - 1, 0, -1):
            if self.stack[at]["tag"] == tag:
                del self.stack[at:]
                return

    def handle_data(self, data):
        self.stack[-1]["children"].append({"text": data})


def tree(markup):
    parser = Tree()
    parser.feed(markup)
    parser.close()
    return parser.root


HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const created = [], htmlWrites = [], calls = [], errors = [], srcSets = [];
process.on('unhandledRejection', (e) => errors.push(String(e && e.stack || e)));

function parse(sel) {
  const m = /^([a-z][a-z0-9-]*)?((?:#[\w-]+|\.[\w-]+|\[[\w-]+(?:=(?:"[^"]*"|'[^']*'|[^\]]*))?\])*)$/.exec(sel);
  if (!m) throw new Error('stub selector not supported: ' + sel);
  const parts = [];
  const re = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g;
  let p;
  while ((p = re.exec(m[2]))) parts.push(p);
  return {tag: m[1], parts};
}

class Text_ {
  constructor(data) { this.data = data; this.parentNode = null; this.nodeType = 3; this.tagName = '#text'; }
  get textContent() { return this.data; }
  matches() { return false; }
}

class Node_ {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.nodeType = 1;
    this.attrs = {}; this.children = []; this.parentNode = null; this.listeners = {};
    this.hidden = false; this.disabled = false; this.readOnly = false; this.checked = false;
    this.selected = false; this._value = undefined;
  }
  get id() { return this.getAttribute('id') || ''; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  hasAttribute(n) { return n in this.attrs; }
  setAttribute(n, v) {
    this.attrs[n] = String(v);
    if (this.tagName === 'IFRAME' && n === 'src') srcSets.push(String(v));
  }
  removeAttribute(n) { delete this.attrs[n]; }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this; this.children.push(c); return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  set textContent(v) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    if (v !== null && v !== undefined && String(v) !== '') this.appendChild(new Text_(String(v)));
  }
  set innerHTML(v) { htmlWrites.push(String(v)); throw new Error('innerHTML set'); }
  set outerHTML(v) { htmlWrites.push(String(v)); throw new Error('outerHTML set'); }
  insertAdjacentHTML(_, v) { htmlWrites.push(String(v)); throw new Error('insertAdjacentHTML'); }
  get options() { return this.descendants().filter((n) => n.tagName === 'OPTION'); }
  get value() {
    if (this.tagName === 'SELECT') {
      const opts = this.options;
      const chosen = opts.find((o) => o.selected) || opts[0];
      return chosen ? chosen.value : '';
    }
    if (this.tagName === 'OPTION' && this._value === undefined) {
      return this.hasAttribute('value') ? this.getAttribute('value') : this.textContent;
    }
    return this._value === undefined ? '' : this._value;
  }
  set value(v) {
    if (this.tagName === 'SELECT') {
      this.options.forEach((o) => { o.selected = o.value === String(v); });
      return;
    }
    this._value = String(v);
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    if (!ev.target) ev.target = this;
    for (let n = this; n; n = n.parentNode) {
      ev.currentTarget = n;
      for (const fn of (n.listeners[ev.type] || []).slice()) fn.call(n, ev);
      if (ev._stopped || !ev.bubbles) break;
    }
    return !ev.defaultPrevented;
  }
  descendants() {
    const found = [];
    const walk = (n) => n.children.forEach((c) => { if (c.nodeType === 1) { found.push(c); walk(c); } });
    walk(this);
    return found;
  }
  matches(sel) {
    if (this.nodeType !== 1 || this.tagName === '#DOCUMENT') return false;
    return sel.split(',').some((one) => {
      const {tag, parts} = parse(one.trim());
      if (tag && this.tagName.toLowerCase() !== tag) return false;
      return parts.every((p) => {
        if (p[1] !== undefined) return this.id === p[1];
        if (p[2] !== undefined) return (this.getAttribute('class') || '').split(/\s+/).includes(p[2]);
        const value = p[4] !== undefined ? p[4] : p[5] !== undefined ? p[5] : p[6];
        if (!this.hasAttribute(p[3])) return false;
        return value === undefined || this.getAttribute(p[3]) === value;
      });
    });
  }
  closest(sel) {
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.matches(sel)) return n;
    return null;
  }
  querySelectorAll(sel) { return this.descendants().filter((n) => n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

class Doc extends Node_ {
  constructor() { super('#document'); this.readyState = 'complete'; }
  getElementById(id) { return this.descendants().find((n) => n.id === id) || null; }
  createElement(tag) { const n = new Node_(tag); created.push(n.tagName); return n; }
  createTextNode(data) { return new Text_(String(data)); }
  get body() { return this.querySelector('body'); }
  get documentElement() { return this.querySelector('html'); }
}

class Event_ {
  constructor(type, init) {
    this.type = type; this.bubbles = !!(init && init.bubbles);
    this.detail = init ? init.detail : undefined; this.defaultPrevented = false;
    this.target = null;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stopped = true; }
}

function build(spec, doc, parent) {
  if ('text' in spec) { parent.appendChild(new Text_(spec.text)); return; }
  const node = spec.tag === '#document' ? doc : new Node_(spec.tag);
  Object.assign(node.attrs, spec.attrs);
  if (node !== doc) parent.appendChild(node);
  spec.children.forEach((c) => build(c, doc, node));
  const a = spec.attrs;
  node.hidden = 'hidden' in a; node.disabled = 'disabled' in a; node.readOnly = 'readonly' in a;
  node.checked = 'checked' in a; node.selected = 'selected' in a;
  if (node.tagName === 'INPUT') node._value = a.value || '';
  if (node.tagName === 'TEXTAREA') node._value = node.textContent.replace(/^\n/, '');
  return node;
}

const document = new Doc();
build(input.page, document);
const iframe = document.getElementById('studio-preview');
const preview = new Doc();
build(input.preview, preview);
iframe.contentDocument = preview;
iframe.contentWindow = {document: preview};

const routes = {};
for (const r of input.routes) (routes[r.method + ' ' + r.url] = routes[r.method + ' ' + r.url] || []).push(r);
function fetch(url, init) {
  init = init || {};
  const method = (init.method || 'GET').toUpperCase();
  calls.push({url: String(url), method, headers: init.headers || {},
              body: init.body === undefined ? null : JSON.parse(init.body)});
  const queue = routes[method + ' ' + url];
  const r = queue ? (queue.length > 1 ? queue.shift() : queue[0]) : {status: 599, body: {title: 'no route'}};
  const headers = {};
  Object.entries(r.headers || {}).forEach(([k, v]) => { headers[k.toLowerCase()] = v; });
  const answer = {
    ok: r.status >= 200 && r.status < 300, status: r.status,
    headers: {get: (n) => (n.toLowerCase() in headers ? headers[n.toLowerCase()] : null)},
    json: () => (r.body === undefined ? Promise.reject(new Error('no body')) : Promise.resolve(JSON.parse(JSON.stringify(r.body)))),
  };
  if (r.delay) return new Promise((resolve) => setTimeout(() => resolve(answer), r.delay));
  return Promise.resolve(answer);
}

// window.confirm records its question and answers from `confirm` (true
// when none are left).
const confirms = [], answers = (input.confirm || []).slice();
const window = {confirm: (q) => { confirms.push(String(q)); return answers.length ? answers.shift() : true; }};
const ctx = vm.createContext({
  window, document, fetch, URLSearchParams, CustomEvent: Event_, Event: Event_,
  console, setTimeout, WeakSet,
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
iframe.dispatchEvent(new Event_('load'));

const settle = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setTimeout(r, 0)); };
const byId = (id) => document.getElementById(id);
const text = (id) => byId(id).textContent.replace(/\s+/g, ' ').trim();
const node = (pointer) => preview.querySelectorAll('scrud-node').find((n) => n.getAttribute('data-pointer') === pointer);
const innermost = (n) => { while (n.children.some((c) => c.nodeType === 1)) n = n.children.find((c) => c.nodeType === 1); return n; };
const click = (target) => { const ev = new Event_('click', {bubbles: true}); target.dispatchEvent(ev); return ev; };
const submit = (form) => { const ev = new Event_('submit', {bubbles: true}); form.dispatchEvent(ev); return ev; };
const change = (target) => { const ev = new Event_('change', {bubbles: true}); target.dispatchEvent(ev); return ev; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const saved = () => document.body.dispatchEvent(new Event_('scrud-studio-saved', {bubbles: true}));
const out = {};
const finish = () => {
  out.calls = calls; out.created = created; out.htmlWrites = htmlWrites;
  out.errors = errors; out.srcSets = srcSets; out.confirms = confirms;
  console.log(JSON.stringify(out));
};
(async () => {
  await settle();
SCENARIO
  await settle();
  finish();
})().catch((e) => { errors.push(String(e && e.stack || e)); finish(); });
"""


def run_studio(page, preview, routes, scenario, confirm=()):
    """Run `scenario` (JS, inside an async function) on the parsed `page`
    and `preview` HTML with `routes` ``[{method, url, status, body,
    headers, delay}]`` answering ``fetch`` (several for one URL answer in
    turn, the last again and again; ``delay`` ms later when given), and
    `confirm` answering ``window.confirm`` in turn (then true). Returns what the scenario put in ``out``, with
    ``calls``, ``created``, ``htmlWrites``, ``errors``, ``srcSets`` and
    ``confirms`` (the questions asked)."""
    require_node()
    script = HARNESS.replace("SCENARIO", scenario)
    done = subprocess.run(
        ["node", "-e", script, str(STUDIO_JS)],
        capture_output=True,
        text=True,
        input=json.dumps(
            {
                "page": tree(page),
                "preview": tree(preview),
                "routes": routes,
                "confirm": list(confirm),
            }
        ),
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["errors"] == [], out["errors"]
    assert out["htmlWrites"] == []
    return out


def route(method, url, status=200, body=None, headers=None, delay=0):
    return {
        "method": method,
        "url": url,
        "status": status,
        "body": body,
        "headers": headers or {},
        "delay": delay,
    }


def get(url, params=None):
    When.http.get(url, params or {})
    Then.http.status_code_is(200)
    return Http.response


def setup_wip(user):
    """``wip`` (in "alice", over main), which `user` may select."""
    reverse("todo-lists-list")  # the URLconf registers the types
    make_branch("wip", "alice")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)


def studio_page(**params):
    return get(reverse("scrud-studio:type", args=[SLUG]), params).content.decode()


def preview_page(**params):
    url = reverse("scrud-studio:preview", args=[SLUG])
    return get(url, params).content.decode()


def explain_url(pointer, query="branch=wip&mode=view"):
    pointer = pointer.replace("/", "%2F")
    return f"{reverse('scrud-studio:explain', args=[SLUG])}?{query}&pointer={pointer}"


def options_url(pointer, query="branch=wip&mode=view"):
    pointer = pointer.replace("/", "%2F")
    url = reverse("scrud-studio:new-component-options", args=[SLUG])
    return f"{url}?{query}&pointer={pointer}"


def item_url(name, kind="component", branch="wip"):
    return f"{reverse('scrud-studio:item')}?branch={branch}&kind={kind}&name={name}"


def explanation(component, readable=True, **rule):
    """An explanation of `POINTER` whose winner is a rule naming
    `component`."""
    winner = {"readable": readable, "rank": [0, 0, 0, 1], "outcome": "wins"}
    if readable:
        winner.update(
            {
                "selector": "string",
                "component": component,
                "stylesheet": "studio",
                "branch": "wip",
                "applies_to": "",
                **rule,
            }
        )
    return {
        "pointer": POINTER,
        "terms": ["string"],
        "scope": [],
        "winner": 0,
        "fallback": None,
        "rules": [winner],
    }


NO_OPTIONS = {"matches": [], "inside_type": None, "where": [], "applies_to": [""]}


def item(template, engine="jinja2", own=True, etag='"e1"', name="row"):
    """The item endpoint's JSON for a component: wip's own (over main's,
    which a revert shows again), or main's, inherited."""
    return {
        "kind": "component",
        "name": name,
        "applies_to": "",
        "own": own,
        "from": "wip" if own else "main",
        "reverts_to": "inherited" if own else None,
        "etag": etag,
        "content": {
            "name": name,
            "engine": engine,
            "mode": "view",
            "template": template,
            "branch": "wip" if own else "main",
            "base": "server-managed",
            "rbac_context": "alice",
        },
    }


def csrf(page):
    """The page's CSRF token, from its meta tag."""
    return re.search(r'<meta name="csrf-token" content="([^"]*)"', page).group(1)


@pytest.mark.django_db
@test_context
def test_click_in_preview_explains_nearest_node(ui_alice_login):
    setup_wip(ui_alice_login)
    page = studio_page(branch="wip")
    preview = preview_page(branch="wip")
    explained = get(explain_url(POINTER)).json()
    offered = get(options_url(POINTER)).json()
    assert explained["pointer"] == POINTER and offered["matches"]
    out = run_studio(
        page,
        preview,
        [
            route("GET", explain_url(POINTER), body=explained),
            route("GET", options_url(POINTER), body=offered),
        ],
        r"""
        // Outside every scrud-node: nothing is asked.
        out.outside = click(preview.body).defaultPrevented;
        out.before = calls.length;
        // The innermost element of the node: the nearest scrud-node is it.
        const ev = click(innermost(node('/items/0/text')));
        out.prevented = ev.defaultPrevented;
        await settle();
        out.selected = preview.querySelectorAll('scrud-node')
          .filter((n) => n.hasAttribute('data-studio-selected'))
          .map((n) => n.getAttribute('data-pointer'));
        out.panel = text('studio-explain');
        const match = byId('studio-new-component').querySelector('select[name="match"]');
        out.matches = match.options.map((o) => [o.value, o.textContent]);
        out.formHidden = byId('studio-new-component').hidden;
        out.editor = text('studio-editor');
        """,
    )
    assert out["outside"] is False and out["before"] == 0
    assert out["prevented"] is True
    assert [(c["method"], c["url"]) for c in out["calls"]] == [
        ("GET", explain_url(POINTER)),
        ("GET", options_url(POINTER)),
    ]
    assert out["selected"] == [POINTER]
    assert POINTER in out["panel"]
    for term in explained["terms"]:
        assert term in out["panel"]
    # The default stylesheet's rule gives it a built-in, which the panel
    # names; there is nothing stored to edit.
    winner = explained["rules"][explained["winner"]]
    assert winner["component"].startswith("scrud:")
    assert winner["component"] in out["panel"] and "wins" in out["panel"]
    # It comes from no branch: "built-in default", not "on " and nothing.
    assert winner["branch"] == ""
    assert "built-in default" in out["panel"] and " on )" not in out["panel"]
    assert "built-in" in out["editor"]
    assert out["matches"] == [[m["select"], m["label"]] for m in offered["matches"]]
    assert out["formHidden"] is False


@pytest.mark.django_db
@test_context
def test_panel_text_is_not_html(ui_alice_login):
    setup_wip(ui_alice_login)
    page = studio_page(branch="wip")
    preview = preview_page(branch="wip")
    shown = explanation("<img src=x>", selector="<b>bold</b> string")
    shown["terms"] = ["<b>term</b>"]
    shown["scope"] = ["<i>scope</i>"]
    shown["rules"].append(
        {"readable": False, "rank": [0, 0, 0, 0], "outcome": "lower cascade rank"}
    )
    out = run_studio(
        page,
        preview,
        [
            route("GET", explain_url(POINTER), body=shown),
            route("GET", options_url(POINTER), body=NO_OPTIONS),
            route(
                "GET",
                item_url("%3Cimg+src%3Dx%3E"),
                body=item("</textarea><b>t</b>", name="<img src=x>"),
            ),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        out.panel = text('studio-explain');
        out.title = text('studio-editor-title');
        out.template = byId('studio-editor-text').value;
        """,
    )
    for literal in ("<b>bold</b> string", "<img src=x>", "<b>term</b>", "<i>scope</i>"):
        assert literal in out["panel"], literal
    assert "can't read" in out["panel"]
    assert "<img src=x>" in out["title"]
    assert out["template"] == "</textarea><b>t</b>"
    # Only the elements the panel is built of; none parsed from server text.
    assert not {"B", "I", "IMG", "SCRIPT"} & set(out["created"])


@pytest.mark.django_db
@test_context
def test_saved_event_reloads_preview(ui_alice_login):
    setup_wip(ui_alice_login)
    page = studio_page(branch="wip")
    preview = preview_page(branch="wip")
    preview_url = reverse("scrud-studio:preview", args=[SLUG]) + "?branch=wip&mode=view"
    out = run_studio(
        page,
        preview,
        [
            route("GET", explain_url(POINTER), body=explanation("scrud:string")),
            route("GET", options_url(POINTER), body=NO_OPTIONS),
            route(
                "POST",
                reverse("scrud-studio:pull"),
                body={"name": "wip", "conflicts": [{"kind": "css"}]},
                headers={"HX-Trigger": "scrud-studio-saved"},
            ),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        out.afterClick = calls.length;
        saved();
        await settle();
        out.afterSaved = calls.slice(out.afterClick).map((c) => c.url);
        out.srcAfterSaved = srcSets.slice();
        // A write whose answer triggers the event reloads too.
        click(byId('studio-pull'));
        await settle();
        out.message = text('studio-message');
        """,
    )
    assert out["srcAfterSaved"] == [preview_url]
    assert out["afterSaved"] == [explain_url(POINTER), options_url(POINTER)]
    assert out["srcSets"] == [preview_url, preview_url]
    pull = [c for c in out["calls"] if c["method"] == "POST"]
    assert len(pull) == 1
    assert pull[0]["url"] == reverse("scrud-studio:pull")
    assert pull[0]["body"] == {"branch": "wip"}
    assert pull[0]["headers"]["Content-Type"] == "application/json"
    assert pull[0]["headers"]["X-CSRFToken"] == csrf(page) != ""
    assert "1 conflict" in out["message"]
    # After the pull, the panel is explained again.
    assert [c["url"] for c in out["calls"]].count(explain_url(POINTER)) == 3


@pytest.mark.django_db
@test_context
def test_save_sends_if_match_and_a_412_reloads_the_editor(ui_alice_login):
    setup_wip(ui_alice_login)
    page = studio_page(branch="wip")
    preview = preview_page(branch="wip")
    save_url = reverse("scrud-studio:save-item")
    out = run_studio(
        page,
        preview,
        [
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", options_url(POINTER), body=NO_OPTIONS),
            route("GET", item_url("row"), body=item("<i>{{ value }}</i>")),
            route("GET", item_url("row"), body=item("<u>theirs</u>", etag='"e2"')),
            route("POST", save_url, 412, {"title": "Precondition Failed"}),
            route("POST", save_url, 403, {"title": "Forbidden"}),
            route("POST", save_url, 400, {"template": ["Unexpected end of block"]}),
            route(
                "POST",
                reverse("scrud-studio:revert-item"),
                body=None,
                headers={"HX-Trigger": "scrud-studio-saved"},
            ),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        out.loaded = byId('studio-editor-text').value;
        out.readOnly = byId('studio-editor-text').readOnly;
        byId('studio-editor-text').value = '<i>mine</i>';
        click(byId('studio-save'));
        await settle();
        out.after412 = {
          message: text('studio-message'), template: byId('studio-editor-text').value,
          draft: byId('studio-editor-draft') && byId('studio-editor-draft').value,
          draftReadOnly: byId('studio-editor-draft') && byId('studio-editor-draft').readOnly,
          draftHidden: byId('studio-editor-draft-box') ? byId('studio-editor-draft-box').hidden : true,
        };
        click(byId('studio-save'));
        await settle();
        out.after403 = text('studio-message');
        click(byId('studio-save'));
        await settle();
        out.after400 = {message: text('studio-message'), errors: text('studio-editor-errors')};
        out.revertHidden = byId('studio-revert').hidden;
        click(byId('studio-revert'));
        await settle();
        """,
    )
    assert out["loaded"] == "<i>{{ value }}</i>" and out["readOnly"] is False
    saves = [c for c in out["calls"] if c["url"] == save_url]
    assert len(saves) == 3
    first = saves[0]
    assert first["headers"]["If-Match"] == '"e1"'
    assert first["headers"]["X-CSRFToken"] == csrf(page)
    assert first["headers"]["Content-Type"] == "application/json"
    assert first["body"] == {
        "kind": "component",
        "branch": "wip",
        "name": "row",
        "content": {
            "name": "row",
            "engine": "jinja2",
            "mode": "view",
            "template": "<i>mine</i>",
            "branch": "wip",
            "rbac_context": "alice",
        },
    }
    # 412: a notice, and the editor shows the item as it is now.
    assert "changed" in out["after412"]["message"]
    assert out["after412"]["template"] == "<u>theirs</u>"
    # The author's draft stays visible beside it, read-only (ruling S17).
    assert out["after412"]["draftHidden"] is False
    assert out["after412"]["draft"] == "<i>mine</i>"
    assert out["after412"]["draftReadOnly"] is True
    assert saves[1]["headers"]["If-Match"] == '"e2"'
    assert "may not" in out["after403"]
    assert "template: Unexpected end of block" in out["after400"]["errors"]
    # Revert sends the ETag the editor read (ruling S10).
    assert out["revertHidden"] is False
    (revert,) = [c for c in out["calls"] if c["url"].endswith("revert-item")]
    assert revert["headers"]["If-Match"] == '"e2"'
    assert revert["body"] == {"kind": "component", "branch": "wip", "name": "row"}


@pytest.mark.django_db
@test_context
def test_editor_edits_a_real_jinja2_component(ui_alice_login):
    """The item endpoint's own JSON for a component stored with the engine
    name the server uses (``jinja2``) opens editable."""
    setup_wip(ui_alice_login)
    put_item(
        "ui-components", "row", "<i>{{ value }}</i>", "wip", "alice", engine="jinja2"
    )
    real = get(item_url("row")).json()
    assert real["content"]["engine"] == "jinja2" and real["own"] is True
    out = run_studio(
        studio_page(branch="wip"),
        preview_page(branch="wip"),
        [
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", options_url(POINTER), body=NO_OPTIONS),
            route("GET", item_url("row"), body=real),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        out.template = byId('studio-editor-text').value;
        out.readOnly = byId('studio-editor-text').readOnly;
        out.saveHidden = byId('studio-save').hidden;
        out.reason = text('studio-editor-reason');
        """,
    )
    assert out["template"] == "<i>{{ value }}</i>"
    assert out["readOnly"] is False and out["saveHidden"] is False
    assert out["reason"] == ""


@pytest.mark.django_db
@test_context
def test_editor_is_read_only_for_django_components_and_published_branches(
    ui_alice_login,
):
    setup_wip(ui_alice_login)
    preview = preview_page(branch="wip")
    script = r"""
        click(innermost(node('/items/0/text')));
        await settle();
        out.readOnly = byId('studio-editor-text').readOnly;
        out.saveHidden = byId('studio-save').hidden;
        out.revertHidden = byId('studio-revert').hidden;
        out.editor = text('studio-editor');
        """
    # A Django-engine component on a working branch.
    out = run_studio(
        studio_page(branch="wip"),
        preview,
        [
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", options_url(POINTER), body=NO_OPTIONS),
            route("GET", item_url("row"), body=item("<b>x</b>", engine="django")),
        ],
        script,
    )
    assert out["readOnly"] is True and out["saveHidden"] is True
    assert "django" in out["editor"].lower()
    # A Jinja component on a published branch: no options are asked for.
    query = "branch=main&mode=view"
    out = run_studio(
        studio_page(branch="main"),
        preview,
        [
            route("GET", explain_url(POINTER, query), body=explanation("row")),
            route(
                "GET",
                item_url("row", branch="main"),
                body=item("<b>x</b>", own=False),
            ),
        ],
        script,
    )
    assert out["readOnly"] is True
    assert out["saveHidden"] is True and out["revertHidden"] is True
    assert "read-only" in out["editor"]
    assert all("options" not in c["url"] for c in out["calls"])


@pytest.mark.django_db
@test_context
def test_published_branch_offers_revert_for_its_own_items_only(ui_alice_login):
    """On a published branch the editor stays read-only (no Save), but the
    branch's own item, opened from the strip, offers Revert: it sends the
    item's ETag as If-Match and reopens the item, now inherited, with no
    Revert."""
    setup_wip(ui_alice_login)
    make_branch("shop", "alice", prefix="/shop/")
    put_item("ui-components", "row", "<u>{{ value }}</u>", "shop", "alice")
    query = "branch=shop&mode=view"
    page = studio_page(branch="shop")
    own = {**item("<u>{{ value }}</u>", etag='"s1"'), "from": "shop"}
    own["content"]["branch"] = "shop"
    inherited = item("<b>{{ value }}</b>", own=False, etag='"m1"')
    out = run_studio(
        page,
        preview_page(branch="shop"),
        [
            route("GET", explain_url(POINTER, query), body=explanation("row")),
            route("GET", item_url("row", branch="shop"), body=own),
            route("GET", item_url("row", branch="shop"), body=inherited),
            route(
                "POST",
                reverse("scrud-studio:revert-item"),
                204,
                headers={"HX-Trigger": "scrud-studio-saved"},
            ),
        ],
        r"""
        click(byId('studio-changes').querySelector('[data-studio-open]'));
        await settle();
        const state = () => ({
          readOnly: byId('studio-editor-text').readOnly,
          saveHidden: byId('studio-save').hidden,
          revertHidden: byId('studio-revert').hidden,
          label: text('studio-revert'),
          source: text('studio-editor-source'),
          reason: text('studio-editor-reason'),
        });
        out.own = state();
        click(byId('studio-revert'));
        await settle();
        out.after = state();
        out.message = text('studio-message');
        """,
    )
    assert out["own"]["readOnly"] is True and out["own"]["saveHidden"] is True
    assert out["own"]["revertHidden"] is False
    # Main has the item beneath (reverts_to "inherited"): a revert, which
    # asks nothing.
    assert out["own"]["label"] == "Revert to inherited"
    assert out["confirms"] == []
    assert "Changed on branch shop" in out["own"]["source"]
    assert "read-only" in out["own"]["reason"]
    assert "Revert" in out["own"]["reason"]
    assert "Saving" not in out["own"]["source"] + out["own"]["reason"]
    (revert,) = [c for c in out["calls"] if c["url"].endswith("revert-item")]
    assert revert["method"] == "POST"
    assert revert["headers"]["If-Match"] == '"s1"'
    assert revert["headers"]["X-CSRFToken"] == csrf(page)
    assert revert["body"] == {"kind": "component", "branch": "shop", "name": "row"}
    # Reopened, inherited from main: read-only, and nothing to revert.
    items = [c["url"] for c in out["calls"] if "/item?" in c["url"]]
    assert items == [item_url("row", branch="shop")] * 2
    assert out["after"]["readOnly"] is True and out["after"]["saveHidden"] is True
    assert out["after"]["revertHidden"] is True
    assert "Inherited from main" in out["after"]["source"]
    # Saving is refused on a published branch: the editor does not offer it.
    assert "Saving" not in out["after"]["source"] + out["after"]["reason"]
    assert "read-only" in out["after"]["reason"]
    assert "Reverted row" in out["message"]


@pytest.mark.django_db
@test_context
def test_an_own_item_with_nothing_beneath_is_deleted_after_a_confirm(
    ui_alice_login,
):
    """An own item whose deletion shows nothing in its place (``reverts_to``
    null: nothing beneath, or nothing the user may read) is labelled "Delete
    from <branch>", and asks first: from a published branch, that it is
    removed from the live site; from a working branch, from that branch.
    Declined, nothing is sent; confirmed, the revert is sent as a revert
    is."""
    setup_wip(ui_alice_login)
    make_branch("shop", "alice", prefix="/shop/")
    script = r"""
        click(byId('studio-changes').querySelector('[data-studio-open]'));
        await settle();
        out.label = text('studio-revert');
        out.reason = text('studio-editor-reason');
        out.hidden = byId('studio-revert').hidden;
        click(byId('studio-revert'));
        await settle();
        out.declined = calls.filter((c) => c.url.endsWith('revert-item')).length;
        click(byId('studio-revert'));
        await settle();
        out.message = text('studio-message');
        """
    saved = {"HX-Trigger": "scrud-studio-saved"}
    for branch, where in (("shop", "live site"), ("wip", "branch wip")):
        put_item("ui-components", "row", "<u>{{ value }}</u>", branch, "alice")
        added = {**item("<u>x</u>", etag='"a1"'), "from": branch}
        added["reverts_to"] = None
        out = run_studio(
            studio_page(branch=branch),
            preview_page(branch=branch),
            [
                route("GET", item_url("row", branch=branch), body=added),
                route("POST", reverse("scrud-studio:revert-item"), 204, None, saved),
            ],
            script,
            confirm=[False, True],
        )
        assert out["hidden"] is False
        assert out["label"] == f"Delete from {branch}"
        assert len(out["confirms"]) == 2
        assert "row" in out["confirms"][0] and where in out["confirms"][0]
        assert out["declined"] == 0
        (revert,) = [c for c in out["calls"] if c["url"].endswith("revert-item")]
        assert revert["headers"]["If-Match"] == '"a1"'
        assert revert["body"] == {"kind": "component", "branch": branch, "name": "row"}
        assert "Deleted row" in out["message"]


@pytest.mark.django_db
@test_context
def test_new_component_form_posts_the_choice_and_says_whether_it_wins(
    ui_alice_login,
):
    setup_wip(ui_alice_login)
    page = studio_page(branch="wip", mode="edit", context="", scope="card")
    preview = preview_page(branch="wip", mode="edit")
    query = "branch=wip&mode=edit&scope=card"
    offered = {
        "matches": [
            {"select": "ex:Text", "label": "ex:Text", "broad": False},
            {"select": "string", "label": "every string on this branch", "broad": True},
        ],
        "inside_type": {
            "scope": "todo-lists-items",
            "root_component": "scrud:object-edit",
            "property": "items",
            "creates_root": True,
        },
        "where": ["card"],
        "applies_to": [""],
        "name": "todo-lists-items-text-edit",
    }
    created = {
        "written": [
            {"kind": "component", "name": "todo-lists-edit"},
            {"kind": "component", "name": "todo-lists-items-text-edit"},
            {"kind": "stylesheet", "name": "studio", "applies_to": ""},
        ],
        "wins": False,
        "explanation": {
            **explanation("other"),
            "rules": [
                {"readable": False, "rank": [2, 0, 0, 1], "outcome": "wins"},
                {
                    "readable": True,
                    "rank": [1, -1, 0, 1],
                    "outcome": "fewer scopes",
                    "selector": "todo-lists-items string",
                    "component": "todo-lists-items-text-edit",
                    "stylesheet": "studio",
                    "branch": "wip",
                    "applies_to": "",
                },
            ],
        },
    }
    out = run_studio(
        page,
        preview,
        [
            route("GET", explain_url(POINTER, query), body=explanation("scrud:x")),
            route("GET", options_url(POINTER, query), body=offered),
            route(
                "POST",
                reverse("scrud-studio:new-component"),
                201,
                created,
                {"HX-Trigger": "scrud-studio-saved"},
            ),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        const form = byId('studio-new-component');
        out.inside = text('studio-inside');
        form.querySelector('select[name="match"]').value = 'string';
        form.querySelector('input[name="inside_type"]').checked = true;
        form.querySelector('select[name="where"]').value = 'card';
        form.querySelector('textarea[name="template"]').value = '<em>{{ value }}</em>';
        out.prevented = submit(form).defaultPrevented;
        await settle();
        out.result = text('studio-new-result');
        """,
    )
    assert "items" in out["inside"] and "todo-lists-items" in out["inside"]
    assert out["prevented"] is True
    (post,) = [c for c in out["calls"] if c["method"] == "POST"]
    assert post["headers"]["X-CSRFToken"] == csrf(page)
    assert post["body"] == {
        "branch": "wip",
        "slug": SLUG,
        "mode": "edit",
        "scope": "card",
        "context": "",
        "pointer": POINTER,
        "match": "string",
        "inside_type": True,
        "where": "card",
        "applies_to": "",
        "template": "<em>{{ value }}</em>",
    }
    assert "todo-lists-items-text-edit" in out["result"]
    assert "does not win" in out["result"] and "can't read" in out["result"]
    # It says why it loses, as the explanation does (ruling S16).
    assert "(fewer scopes)" in out["result"]
    # The saved event reloaded the preview.
    assert len(out["srcSets"]) == 1


def setup_other(user):
    """``wip`` as `setup_wip` makes it, with its own Jinja component
    ``other``, which the branch strip lists."""
    setup_wip(user)
    put_item(
        "ui-components", "other", "<p>{{ value }}</p>", "wip", "alice", engine="jinja2"
    )


@pytest.mark.django_db
@test_context
def test_editor_keeps_its_item_after_save_and_revert(ui_alice_login):
    """After a save or a revert the editor reopens the item it edits, not
    the node's winner; after a new component it opens the winner (ruling
    S17)."""
    setup_other(ui_alice_login)
    page = studio_page(branch="wip")
    offered = {
        "matches": [
            {"select": "string", "label": "every string on this branch", "broad": True}
        ],
        "inside_type": None,
        "where": [],
        "applies_to": [""],
        "name": "made",
    }
    saved = {"HX-Trigger": "scrud-studio-saved"}
    out = run_studio(
        page,
        preview_page(branch="wip"),
        [
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", explain_url(POINTER), body=explanation("made")),
            route("GET", options_url(POINTER), body=offered),
            route("GET", item_url("row"), body=item("<b>row</b>")),
            route("GET", item_url("other"), body=item("<p>other</p>", name="other")),
            route("GET", item_url("other"), body=item("<p>2</p>", name="other")),
            route(
                "GET",
                item_url("other"),
                body=item("<p>main</p>", name="other", own=False, etag='"m"'),
            ),
            route("GET", item_url("made"), body=item("<em>made</em>", name="made")),
            route("POST", reverse("scrud-studio:save-item"), body={}, headers=saved),
            route("POST", reverse("scrud-studio:revert-item"), headers=saved),
            route(
                "POST",
                reverse("scrud-studio:new-component"),
                201,
                {"written": [{"kind": "component", "name": "made"}], "wins": True},
                saved,
            ),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        out.first = text('studio-editor-title');
        click(byId('studio-changes').querySelector('[data-studio-open]'));
        await settle();
        out.opened = text('studio-editor-title');
        byId('studio-editor-text').value = '<p>2</p>';
        let from = calls.length;
        click(byId('studio-save'));
        await settle();
        out.afterSave = {
          title: text('studio-editor-title'), template: byId('studio-editor-text').value,
          items: calls.slice(from).filter((c) => c.url.includes('/item?')).map((c) => c.url),
        };
        from = calls.length;
        click(byId('studio-revert'));
        await settle();
        out.afterRevert = {
          title: text('studio-editor-title'), source: text('studio-editor-source'),
          items: calls.slice(from).filter((c) => c.url.includes('/item?')).map((c) => c.url),
        };
        from = calls.length;
        submit(byId('studio-new-component'));
        await settle();
        out.afterCreate = {
          title: text('studio-editor-title'),
          items: calls.slice(from).filter((c) => c.url.includes('/item?')).map((c) => c.url),
        };
        """,
    )
    assert out["first"] == "Component row"
    assert out["opened"] == "Component other"
    assert out["afterSave"] == {
        "title": "Component other",
        "template": "<p>2</p>",
        "items": [item_url("other")],
    }
    assert out["afterRevert"]["title"] == "Component other"
    assert out["afterRevert"]["items"] == [item_url("other")]
    assert "Inherited from main" in out["afterRevert"]["source"]
    # A new component: the node's winner, the component just made.
    assert out["afterCreate"] == {
        "title": "Component made",
        "items": [item_url("made")],
    }


@pytest.mark.django_db
@test_context
def test_a_slow_item_never_replaces_a_newer_one(ui_alice_login):
    setup_other(ui_alice_login)
    out = run_studio(
        studio_page(branch="wip"),
        preview_page(branch="wip"),
        [
            route("GET", explain_url(POINTER), body=explanation("row")),
            route("GET", options_url(POINTER), body=NO_OPTIONS),
            route("GET", item_url("row"), body=item("<b>row</b>"), delay=60),
            route("GET", item_url("other"), body=item("<p>other</p>", name="other")),
        ],
        r"""
        // The node's winner is asked for, and answers slowly...
        click(innermost(node('/items/0/text')));
        await settle();
        out.pending = text('studio-editor-title');
        // ...after the author opened another item.
        click(byId('studio-changes').querySelector('[data-studio-open]'));
        await settle();
        out.opened = text('studio-editor-title');
        await sleep(150);
        await settle();
        out.title = text('studio-editor-title');
        out.template = byId('studio-editor-text').value;
        """,
    )
    assert out["pending"] != "Component row"
    assert out["opened"] == "Component other"
    assert out["title"] == "Component other" and out["template"] == "<p>other</p>"


@pytest.mark.django_db
@test_context
def test_new_component_is_off_while_the_preview_is_pinned(ui_alice_login):
    reverse("todo-lists-list")
    make_component("base-row", "<b>{{ value }}</b>")
    make_branch("wip", "alice", branching_point={"main": versions_of("main")[-1]})
    grant(ui_alice_login, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(ui_alice_login)
    page = studio_page(branch="wip", pinned="1")
    query = "branch=wip%40pinned&mode=view"
    assert f'data-query="{query.replace("&", "&amp;")}"' in page
    offered = get(options_url(POINTER, query)).json()
    assert offered["matches"]
    out = run_studio(
        page,
        preview_page(branch="wip@pinned"),
        [
            route("GET", explain_url(POINTER, query), body=explanation("scrud:x")),
            route("GET", options_url(POINTER, query), body=offered),
            route("POST", reverse("scrud-studio:new-component"), 201, {"wins": True}),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        const form = byId('studio-new-component');
        out.disabled = form.querySelector('fieldset').disabled;
        out.form = text('studio-new-component');
        out.prevented = submit(form).defaultPrevented;
        await settle();
        """,
    )
    assert out["disabled"] is True
    assert "pinned" in out["form"]
    assert out["prevented"] is True
    assert not [c for c in out["calls"] if c["method"] == "POST"]


@pytest.mark.django_db
@test_context
def test_broad_labels_name_the_data_context(ui_alice_login):
    setup_wip(ui_alice_login)
    offered = {
        "matches": [
            {"select": "ex:Text", "label": "ex:Text", "broad": False},
            {"select": "string", "label": "every string on this branch", "broad": True},
        ],
        "inside_type": None,
        "where": [],
        "applies_to": ["", "acme"],
        "name": "made",
    }
    out = run_studio(
        studio_page(branch="wip"),
        preview_page(branch="wip"),
        [
            route("GET", explain_url(POINTER), body=explanation("scrud:x")),
            route("GET", options_url(POINTER), body=offered),
        ],
        r"""
        click(innermost(node('/items/0/text')));
        await settle();
        const form = byId('studio-new-component');
        const labels = () => form.querySelector('select[name="match"]').options.map((o) => o.textContent);
        out.everywhere = labels();
        form.querySelector('select[name="match"]').value = 'string';
        form.querySelector('select[name="applies_to"]').value = 'acme';
        change(form.querySelector('select[name="applies_to"]'));
        out.acme = labels();
        out.match = form.querySelector('select[name="match"]').value;
        form.querySelector('select[name="applies_to"]').value = '';
        change(form.querySelector('select[name="applies_to"]'));
        out.back = labels();
        """,
    )
    assert out["everywhere"] == ["ex:Text", "every string on this branch"]
    assert out["acme"] == ["ex:Text", "every string in acme on this branch"]
    assert out["match"] == "string"
    assert out["back"] == out["everywhere"]


# The marks message a preview without a mark posts after the errors.
NO_MARKS = {"type": "scrud-preview:marks", "wins": 0, "loses": 0}


BRIDGE_JS = (
    Path(scrud_django.ui.studio.__file__).parent
    / "static/scrud_studio/preview-bridge.js"
)

BRIDGE_HARNESS = (
    HARNESS[: HARNESS.index("const document = new Doc();")]
    + r"""
const document = new Doc();
build(input.preview, document);
const posted = [];
const window = {};
window.parent = {
  postMessage: (message, origin) =>
    posted.push({message: JSON.parse(JSON.stringify(message)), origin}),
};
const ctx = vm.createContext({window, document, console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const node = (pointer) => document.querySelectorAll('scrud-node').find((n) => n.getAttribute('data-pointer') === pointer);
const innermost = (n) => { while (n.children.some((c) => c.nodeType === 1)) n = n.children.find((c) => c.nodeType === 1); return n; };
const click = (target) => { const ev = new Event_('click', {bubbles: true}); target.dispatchEvent(ev); return ev; };
const out = {};
try {
  out.loaded = posted.splice(0);
  out.outside = click(document.body).defaultPrevented;
  out.afterOutside = posted.splice(0);
  out.inside = click(innermost(node('/items/0/text'))).defaultPrevented;
  // The item's own element, outside its children's nodes: the item.
  const item = node('/items/0');
  click(item);
  out.clicks = posted.splice(0);
  // An element of the previewed resource's root node, outside its children.
  const section = document.querySelector('section');
  if (section) { click(section); out.sectionClicks = posted.splice(0); }
} catch (e) { errors.push(String(e && e.stack || e)); }
out.errors = errors; out.htmlWrites = htmlWrites;
console.log(JSON.stringify(out));
"""
)


@pytest.mark.django_db
@test_context
def test_bridge_posts_select_and_errors(ui_alice_login):
    setup_wip(ui_alice_login)
    body = {
        "type": SLUG,
        "branch": "wip",
        "drafts": [
            {
                "kind": "component",
                "content": {
                    "name": "bad",
                    "engine": "django",
                    "mode": "view",
                    "template": "{% if %}",
                },
            }
        ],
    }
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({"request": json.dumps(body)}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(200)
    out = run_bridge(Http.response.content.decode())
    # On load: the draft errors the page lists, to the parent, to any
    # origin (the frame's own is opaque).
    (loaded, marks) = out["loaded"]
    assert loaded["origin"] == "*"
    assert loaded["message"]["type"] == "scrud-preview:errors"
    # Then the marks counts (none: no mark was asked for).
    assert marks == {"message": NO_MARKS, "origin": "*"}
    (error,) = loaded["message"]["errors"]
    assert error["kind"] == "component" and error["name"] == "bad"
    assert "template is invalid" in error["messages"][0]
    # A click outside every node posts nothing and is not prevented.
    assert out["outside"] is False and out["afterOutside"] == []
    # The click selects the nearest node, with its resource frame's href.
    sample = reverse("scrud-studio:sample", args=[SLUG])
    assert out["inside"] is True
    assert out["clicks"] == [
        {"message": select(POINTER, sample), "origin": "*"},
        {"message": select("/items/0", sample), "origin": "*"},
    ]
    assert "sectionClicks" not in out


def select(pointer, href):
    return {"type": "scrud-preview:select", "pointer": pointer, "href": href}


def run_bridge(page):
    require_node()
    done = subprocess.run(
        ["node", "-e", BRIDGE_HARNESS, str(BRIDGE_JS)],
        capture_output=True,
        text=True,
        input=json.dumps({"preview": tree(page)}),
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["errors"] == [] and out["htmlWrites"] == []
    return out


@pytest.mark.django_db
@test_context
def test_bridge_tells_an_embeds_nodes_by_href(ui_alice_login):
    """A node of an embedded resource has a pointer relative to it: the
    select message's href is the embedded resource's, not the preview's."""
    setup_wip(ui_alice_login)
    grant(
        ui_alice_login,
        {
            "http.GET": [
                TYPE_URI_TODO_LIST,
                collection_type_uri_for(TYPE_URI_TODO_LIST),
            ]
        },
        "project-a",
    )
    other = make_list("Chores", items=["Sweep"])
    host(
        '<section class="host">{{ embed("' + url_of(other) + '", mode="view") }}'
        "</section>"
    )
    body = {"type": SLUG, "branch": "wip", "scope": ["host"]}
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({"request": json.dumps(body)}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(200)
    out = run_bridge(Http.response.content.decode())
    assert out["loaded"] == [
        {"message": {"type": "scrud-preview:errors", "errors": []}, "origin": "*"},
        {"message": NO_MARKS, "origin": "*"},
    ]
    # The host prints only the embed: these nodes are the embedded list's.
    assert out["clicks"] == [
        {"message": select(POINTER, url_of(other)), "origin": "*"},
        {"message": select("/items/0", url_of(other)), "origin": "*"},
    ]
    sample = reverse("scrud-studio:sample", args=[SLUG])
    assert out["sectionClicks"] == [{"message": select("", sample), "origin": "*"}]


# The bridge on a page still loading: it is run, then a branch script
# replaces the errors element's text (as one may: it runs in the same
# document), then the document is loaded.
BRIDGE_LOADING_HARNESS = BRIDGE_HARNESS.replace(
    "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);",
    "document.readyState = 'loading';"
    "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
    "document.getElementById('scrud-preview-errors').textContent ="
    " JSON.stringify([{kind: 'component', name: 'forged', messages: []}]);"
    "document.readyState = 'complete';"
    "(document.listeners.DOMContentLoaded || []).forEach("
    "(fn) => fn({type: 'DOMContentLoaded'}));",
)


def bad_draft_preview():
    body = {
        "type": SLUG,
        "branch": "wip",
        "drafts": [
            {
                "kind": "component",
                "content": {
                    "name": "bad",
                    "engine": "django",
                    "mode": "view",
                    "template": "{% if %}",
                },
            }
        ],
    }
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({"request": json.dumps(body)}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(200)
    return Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_bridge_reads_the_errors_before_branch_scripts_run(ui_alice_login):
    """The bridge reads the errors the server listed when it runs, before
    the branch's scripts (which come after it): what it posts on load is
    the server's list, whatever a script did to the element since."""
    setup_wip(ui_alice_login)
    require_node()
    done = subprocess.run(
        ["node", "-e", BRIDGE_LOADING_HARNESS, str(BRIDGE_JS)],
        capture_output=True,
        text=True,
        input=json.dumps({"preview": tree(bad_draft_preview())}),
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["errors"] == []
    (loaded, marks) = out["loaded"]
    assert [e["name"] for e in loaded["message"]["errors"]] == ["bad"]
    assert marks["message"] == NO_MARKS


@pytest.mark.django_db
@test_context
def test_bridge_says_when_the_errors_cannot_be_read(ui_alice_login):
    """Errors it cannot parse are posted as ``errors: null, failed: true``,
    never as an empty list, which would say every draft was shown."""
    setup_wip(ui_alice_login)
    page = re.sub(
        r'(<script type="application/json" id="scrud-preview-errors"'
        r' nonce="[^"]+">).*?(</script>)',
        r"\1{not json\2",
        bad_draft_preview(),
        flags=re.S,
    )
    assert "{not json</script>" in page
    (loaded, marks) = run_bridge(page)["loaded"]
    assert marks["message"] == NO_MARKS
    assert loaded["message"] == {
        "type": "scrud-preview:errors",
        "errors": None,
        "failed": True,
    }
