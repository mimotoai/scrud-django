"""The studio layer's JS item (``studio:studio``) under node. Layer JS is
effective on every branch, so every page loads it: it must do nothing unless
the page has a studio workbench (``[data-studio-workbench]``). The script runs
against a stub ``document`` and ``window`` that record what it touches, with
every other global missing.

On a workbench (`WORKBENCH`), it runs against a stub DOM shaped like the
markup ``studio:workbench``, ``studio:items`` and ``studio:status`` render,
with stub ``localStorage``, ``location``, ``history`` and ``htmx.ajax`` that
record what it does; each test's scenario drives it."""

import importlib.resources
import json
import subprocess

from .test_ui_static import require_node

LAYER_PACKAGE = "scrud_django.ui.studio.layer"

# argv[1]: the script; argv[2]: "1" when the page has a workbench. Prints
# what the script did.
HARNESS = r"""
const vm = require('vm');
const source = process.argv[1];
const withWorkbench = process.argv[2] === '1';
const out = {queries: [], touched: [], listeners: [], fetches: 0, attributes: {}};
const workbench = {
  setAttribute(name, value) { out.attributes[name] = String(value); },
  getAttribute(name) { return out.attributes[name] ?? null; },
  hasAttribute(name) { return name in out.attributes; },
  addEventListener(type) { out.listeners.push('workbench:' + type); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
const recorded = (name, target) => new Proxy(target, {
  get(object, key) {
    if (typeof key === 'string') out.touched.push(name + '.' + key);
    return object[key];
  },
});
const document = recorded('document', {
  readyState: 'interactive',
  querySelector(selector) {
    out.queries.push(selector);
    return withWorkbench && selector === '[data-studio-workbench]' ? workbench : null;
  },
  addEventListener(type) { out.listeners.push('document:' + type); },
});
const window = recorded('window', {
  addEventListener(type) { out.listeners.push('window:' + type); },
  location: {pathname: '/', search: '', hash: ''},
  history: {state: null, replaceState() {}},
});
const context = vm.createContext({
  document,
  window,
  URLSearchParams,
  fetch() { out.fetches += 1; return new Promise(() => {}); },
});
vm.runInContext(source, context);
console.log(JSON.stringify(out));
"""


def layer_source():
    layer_js = importlib.resources.files(LAYER_PACKAGE).joinpath("js/studio.json")
    return json.loads(layer_js.read_text(encoding="utf-8"))["js"]


def run(with_workbench):
    source = layer_source()
    done = subprocess.run(
        ["node", "-e", HARNESS, source, "1" if with_workbench else "0"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_layer_js_inert_without_workbench():
    require_node()
    out = run(with_workbench=False)
    # It looks for the workbench, and does nothing else.
    assert out["queries"] == ["[data-studio-workbench]"]
    assert out["touched"] == ["document.querySelector"]
    assert out["listeners"] == [] and out["fetches"] == 0
    assert out["attributes"] == {}


def test_layer_js_starts_on_workbench():
    require_node()
    out = run(with_workbench=True)
    assert out["queries"][0] == "[data-studio-workbench]"
    assert out["attributes"] == {"data-studio-ready": "1"}


# A stub DOM of the workbench (the markup the layer's components render) and
# the globals studio.js uses. argv[1]: the script; argv[2]: JSON options
# ``{search, storage, published, endpoints}``; ``storage`` is an object (the
# stored items), ``"throws"`` (every call throws) or ``"missing"`` (reading
# ``window.localStorage`` throws). The scenario follows.
WORKBENCH = r"""
const vm = require('vm');
const source = process.argv[1];
const opts = JSON.parse(process.argv[2]);
const out = {ajax: [], replaced: [], submits: [], opened: [], innerHTML: 0};

function parseSelector(sel) {
  const m = /^([a-zA-Z]*)((?:\.[\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/.exec(sel);
  if (!m) throw new Error('stub: unsupported selector ' + sel);
  const parts = {tag: m[1].toUpperCase(), classes: [], attrs: []};
  const re = /\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let p;
  while ((p = re.exec(m[2]))) {
    if (p[1]) parts.classes.push(p[1]);
    else parts.attrs.push([p[2], p[3]]);
  }
  return parts;
}
class Node {
  constructor(tag, attrs, parent) {
    this.tagName = tag.toUpperCase();
    this.attrs = Object.assign({}, attrs || {});
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.text = '';
    this.value = '';
    if (parent) parent.appendChild(this);
  }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  hasAttribute(n) { return n in this.attrs; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  get textContent() { return this.text; }
  set textContent(v) { this.text = String(v); }
  set innerHTML(v) { out.innerHTML += 1; }
  matches(sel) {
    const s = parseSelector(sel);
    if (s.tag && s.tag !== this.tagName) return false;
    const classes = (this.attrs['class'] || '').split(/\s+/);
    if (!s.classes.every((c) => classes.includes(c))) return false;
    return s.attrs.every(([n, v]) => n in this.attrs && (v === undefined || this.attrs[n] === v));
  }
  closest(sel) {
    for (let n = this; n; n = n.parentNode) if (n.matches && n.matches(sel)) return n;
    return null;
  }
  contains(other) {
    for (let n = other; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  querySelectorAll(sel) {
    const found = [];
    const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) found.push(c); walk(c); });
    walk(this);
    return found;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent(evt) { fire(this, evt.type, evt.detail, evt); return true; }
}
const el = (tag, attrs, parent) => new Node(tag, attrs, parent);

const root = el('html');
const meta = el('meta', {name: 'csrf-token', content: 'tok'}, root);
const body = el('body', {}, root);
const elsewhere = el('div', {}, body);
const workbench = el('div', {
  'class': 'studio-workbench', 'data-studio-workbench': '',
  'data-studio-branch': 'alice-wip',
  'data-studio-published': opts.published ? 'true' : 'false'}, body);
const toggles = {};
for (const name of ['items', 'editor', 'preview']) {
  toggles[name] = el('button', {type: 'button', 'class': 'studio-toggle',
    'data-studio-toggle': name, 'aria-pressed': 'true'}, workbench);
}
const notice = el('div', {'class': 'studio-notice', 'data-studio-notice': '',
  role: 'alert', hidden: ''}, workbench);
const panes = {};
for (const name of ['items', 'editor', 'preview']) {
  panes[name] = el('section', {'class': 'studio-pane', 'data-studio-pane': name}, workbench);
}
const items = el('div', {'class': 'studio-pane-body', 'data-studio-embed': 'items',
  'data-studio-src': '/workbench/ui-branch-items/alice-wip/',
  'data-studio-scope': 'studio studio-items'}, panes.items);
const itemsFrame = el('div', {'class': 'scrud-resource'}, items);
const li = el('li', {'class': 'studio-item studio-inherited'}, itemsFrame);
const head = {kind: 'component', name: 'head', applies_to: null,
  href: '/workbench/ui-components/7/', override: null};
const openHead = el('button', {type: 'button', 'class': 'studio-open',
  'data-studio-open': JSON.stringify(head)}, li);
const label = el('span', {}, openHead);
const sheet = {kind: 'stylesheet', name: 'theme', applies_to: 'alice', href: null,
  override: null};
const openSheet = el('button', {'data-studio-open': JSON.stringify(sheet)}, li);
const override = el('button', {type: 'button', 'class': 'studio-override',
  'hx-post': '/workbench/ui-components/', 'data-scrud-json': 'fixed'}, li);
const editor = el('div', {'data-studio-editor': ''}, panes.editor);
const form = el('form', {'data-studio-preview-form': '', method: 'post',
  target: 'studio-preview', hidden: ''}, panes.preview);
const token = el('input', {type: 'hidden', name: 'csrfmiddlewaretoken', value: ''}, form);
const request = el('input', {type: 'hidden', name: 'request', value: ''}, form);
form.submit = function () {
  out.submits.push({action: form.getAttribute('action'), token: token.value,
    request: JSON.parse(request.value)});
};
el('iframe', {name: 'studio-preview', sandbox: 'allow-scripts',
  'class': 'studio-preview'}, panes.preview);
const strip = el('footer', {'class': 'studio-strip', 'data-studio-embed': 'status',
  'data-studio-src': '/workbench/ui-branch-status/alice-wip/',
  'data-studio-scope': 'studio studio-status'}, workbench);
const stripFrame = el('div', {'class': 'scrud-resource'}, strip);
const endpoints = opts.endpoints === undefined
  ? {live_preview: '/studio/live-preview'} : opts.endpoints;
const status = el('div', {'class': 'studio-status', 'data-studio-status': '',
  'data-studio-endpoints': JSON.stringify(endpoints)}, stripFrame);
const pull = el('button', {type: 'button', 'hx-put': '/workbench/ui-branches/9/'}, status);

const docListeners = {};
const document = {
  readyState: 'interactive',
  querySelector(sel) { return root.querySelector(sel); },
  querySelectorAll(sel) { return root.querySelectorAll(sel); },
  addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  createElement(tag) { return el(tag); },
};
// An event dispatched on `target`, bubbling to the document (a detached
// node's never reaches it).
function fire(target, type, detail, evt) {
  evt = evt || {type, detail};
  evt.target = target;
  let attached = false;
  for (let n = target; n; n = n.parentNode) {
    (n.listeners[type] || []).forEach((fn) => fn(evt));
    if (n === root) attached = true;
  }
  if (attached) (docListeners[type] || []).forEach((fn) => fn(evt));
  return evt;
}
const click = (target) => fire(target, 'click', undefined);
function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; }
// Text of the parsed response as the browser's DOMParser would give it: the
// notices, else the body, tags stripped.
class DOMParser {
  parseFromString(text) {
    const strip = (s) => s.replace(/<[^>]*>/g, '');
    const notices = [...text.matchAll(/<div class="scrud-notice[^"]*"[^>]*>(.*?)<\/div>/gs)]
      .map((m) => ({textContent: strip(m[1])}));
    return {
      querySelectorAll(sel) { return sel === '.scrud-notice' ? notices : []; },
      body: {textContent: strip(text)},
    };
  }
}

const stored = typeof opts.storage === 'object' && opts.storage ? opts.storage : {};
const storage = {
  getItem(k) {
    if (opts.storage === 'throws') throw new Error('denied');
    return k in stored ? stored[k] : null;
  },
  setItem(k, v) {
    if (opts.storage === 'throws') throw new Error('denied');
    stored[k] = String(v);
  },
};
const location = {pathname: '/workbench/ui-branches/9/', search: opts.search || '', hash: ''};
const history = {
  state: null,
  replaceState(state, title, url) {
    out.replaced.push(url);
    const at = url.indexOf('?');
    location.search = at < 0 ? '' : url.slice(at);
    location.pathname = at < 0 ? url : url.slice(0, at);
  },
};
const htmx = {
  ajax(verb, path, context) {
    out.ajax.push({verb, path, target: context.target.getAttribute('data-studio-embed'),
      source: context.source === context.target, swap: context.swap,
      headers: context.headers});
  },
};
const window = {location, history, htmx};
Object.defineProperty(window, 'localStorage', {
  get() {
    if (opts.storage === 'missing') throw new Error('SecurityError');
    return storage;
  },
});
workbench.addEventListener('studio:open', (evt) => out.opened.push(evt.detail.open));
const context = vm.createContext({window, document, URLSearchParams, CustomEvent,
  DOMParser, console, htmx});
vm.runInContext(source, context);
const S = window.ScrudStudio;
const state = () => S.state();
const collapsed = () => Object.fromEntries(Object.entries(panes).map(
  ([name, pane]) => [name, pane.hasAttribute('data-studio-collapsed')]));
const pressed = () => Object.fromEntries(Object.entries(toggles).map(
  ([name, t]) => [name, t.getAttribute('aria-pressed')]));
const reset = () => { out.ajax = []; out.submits = []; out.replaced = []; };
"""


def scenario(script, **opts):
    """Run studio.js on the stub workbench with `opts`, then `script`;
    return what it printed (JSON)."""
    require_node()
    done = subprocess.run(
        [
            "node",
            "-e",
            WORKBENCH + script + "\nconsole.log(JSON.stringify(out));",
            layer_source(),
            json.dumps(opts),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


BASE = "?ui-scope=studio&ui-mode=view"
STATUS_SRC = "/workbench/ui-branch-status/alice-wip/"
ITEMS_SRC = "/workbench/ui-branch-items/alice-wip/"
STATUS_SCOPE = {"Scrud-UI-Scope": "studio studio-status"}
ITEMS_SCOPE = {"Scrud-UI-Scope": "studio studio-items"}


def status_fetch(type_slug):
    return {
        "verb": "GET",
        "path": f"{STATUS_SRC}?type={type_slug}",
        "target": "status",
        "source": True,
        "swap": "innerHTML",
        "headers": STATUS_SCOPE,
    }


def preview_request(**fields):
    return {
        "type": "todo-lists",
        "branch": "alice-wip",
        "mode": "view",
        "scope": [],
        "context": "",
        "instance": None,
        "drafts": [],
        "mark": None,
        "after_push": None,
        **fields,
    }


def test_toggles_persist_and_preview_cannot_hide():
    out = scenario(
        r"""
out.loaded = [collapsed(), pressed()];
click(toggles.items);
out.items = [collapsed(), pressed(), stored['studio:panes']];
click(toggles.preview);
out.preview = [collapsed(), stored['studio:panes']];
click(toggles.items);
out.reopened = [collapsed(), JSON.parse(stored['studio:panes'])];
""",
        search=BASE,
        storage={"studio:panes": json.dumps({"editor": True, "preview": True})},
    )
    open_all = {"items": False, "editor": False, "preview": False}
    # The stored state applies on load; the preview is never collapsed.
    assert out["loaded"] == [
        {**open_all, "editor": True},
        {"items": "true", "editor": "false", "preview": "true"},
    ]
    collapsed, pressed, stored = out["items"]
    assert collapsed == {"items": True, "editor": True, "preview": False}
    assert pressed == {"items": "false", "editor": "false", "preview": "true"}
    assert json.loads(stored) == {"items": True, "editor": True}
    # The preview's toggle changes nothing.
    assert out["preview"] == [collapsed, stored]
    assert out["reopened"] == [{**open_all, "editor": True}, {"editor": True}]
    # Without storage (calls that throw, or no localStorage at all), or with a
    # value that is not JSON: every pane open, and the toggles still work.
    for storage in ("throws", "missing", {"studio:panes": "{not json"}):
        out = scenario(
            r"""
out.loaded = collapsed();
click(toggles.editor);
out.toggled = collapsed();
""",
            search=BASE,
            storage=storage,
        )
        assert out["loaded"] == open_all, storage
        assert out["toggled"] == {**open_all, "editor": True}, storage


def test_strip_refetched_with_type():
    # No type: the strip as embedded, nothing fetched, no preview.
    out = scenario("", search=BASE)
    assert out["ajax"] == [] and out["submits"] == []
    # A type on load: the strip again with ?type=, with its scope only.
    out = scenario(
        r"""
out.load = out.ajax; reset();
S.setState({item: 'component:head'});
out.sameType = out.ajax; reset();
S.setState({type: 'notes'});
out.newType = out.ajax; reset();
S.setState({type: null});
out.noType = out.ajax;
""",
        search=BASE + "&type=todo-lists",
    )
    assert out["load"] == [status_fetch("todo-lists")]
    assert out["sameType"] == []
    assert out["newType"] == [status_fetch("notes")]
    # Without a type, the strip is fetched as embedded (no browse link).
    assert out["noType"] == [{**status_fetch("x"), "path": STATUS_SRC}]
    for fetch in out["load"] + out["newType"]:
        assert "Scrud-UI-Branch" not in fetch["headers"]


def test_state_round_trips_through_url():
    out = scenario(
        r"""
out.loaded = state();
S.setState({item: 'component:studio:page', mode: 'edit', scope: 'row card',
            instance: '12', context: 'project-a', pinned: '1', other: 'x'});
out.url = out.replaced[out.replaced.length - 1];
out.set = state();
S.setState({pointer: null, scope: ''});
out.cleared = state();
out.query = Object.fromEntries(new URLSearchParams(location.search));
out.path = location.pathname;
reset();
click(label);
out.openHead = [state().item, out.opened.slice()];
click(openSheet);
out.openSheet = state().item;
out.keys = [S.itemKey(head), S.itemKey(sheet)];
""",
        search=BASE + "&ui-safe=1&type=todo-lists&pointer=%2Fa~1b",
    )
    assert out["loaded"] == {"type": "todo-lists", "pointer": "/a~1b"}
    assert out["set"] == {
        "type": "todo-lists",
        "pointer": "/a~1b",
        "item": "component:studio:page",
        "mode": "edit",
        "scope": "row card",
        "instance": "12",
        "context": "project-a",
        "pinned": "1",
    }
    # ui-scope=studio, ui-mode=view and ui-safe stay; unknown keys are not
    # written.
    assert out["url"].startswith("/workbench/ui-branches/9/?ui-scope=studio")
    assert "ui-mode=view" in out["url"] and "ui-safe=1" in out["url"]
    assert "other" not in out["url"]
    assert out["cleared"] == {
        k: v for k, v in out["set"].items() if k not in ("pointer", "scope")
    }
    assert out["query"] == {
        "ui-scope": "studio",
        "ui-mode": "view",
        "ui-safe": "1",
        **out["cleared"],
    }
    assert out["path"] == "/workbench/ui-branches/9/"
    # Clicking an entry (or inside it) opens it: `item`, and an event for
    # the editor (Task 4).
    assert out["openHead"] == [
        "component:head",
        [
            {
                "kind": "component",
                "name": "head",
                "applies_to": None,
                "href": "/workbench/ui-components/7/",
                "override": None,
            }
        ],
    ]
    assert out["openSheet"] == "stylesheet:theme@alice"
    assert out["keys"] == ["component:head", "stylesheet:theme@alice"]


def test_write_refreshes_items_strip_preview():
    out = scenario(
        r"""
out.load = [out.ajax, out.submits]; reset();
const ok = (elt, verb, status) => fire(elt, 'htmx:afterRequest', {
  elt, successful: true, requestConfig: {verb}, xhr: {status}});
ok(override, 'post', 201);
out.afterPost = [out.ajax, out.submits, notice.hasAttribute('hidden')]; reset();
ok(pull, 'get', 200);
ok(elsewhere, 'post', 201);
out.ignored = [out.ajax, out.submits]; reset();
const html = '<div class="scrud-notice scrud-notice-error" role="alert">'
  + 'You may not <b>override</b> this.</div>';
fire(override, 'htmx:afterRequest', {elt: override, successful: false,
  requestConfig: {verb: 'post'},
  xhr: {status: 403, responseText: html, getResponseHeader: () => 'text/html'}});
out.refused = [notice.textContent, notice.hasAttribute('hidden'), out.ajax.slice(),
  out.submits.slice()];
fire(pull, 'htmx:afterRequest', {elt: pull, successful: false,
  requestConfig: {verb: 'put'}, xhr: {status: 0, responseText: ''}});
out.network = notice.textContent;
fire(pull, 'htmx:afterRequest', {elt: pull, successful: false,
  requestConfig: {verb: 'put'}, xhr: {status: 409,
  responseText: '{"title": "Conflict", "detail": "Pull first."}',
  getResponseHeader: () => 'application/problem+json'}});
out.problem = notice.textContent;
// A frame's own request whose element was swapped out: htmx fires it again
// on an ancestor still in the page.
ok(itemsFrame, 'put', 200);
out.cleared = [notice.textContent, notice.hasAttribute('hidden'), out.ajax.length];
""",
        search=BASE + "&type=todo-lists",
    )
    load_ajax, load_submits = out["load"]
    assert load_ajax == [status_fetch("todo-lists")]
    assert load_submits == [
        {
            "action": "/studio/live-preview",
            "token": "tok",
            "request": preview_request(),
        }
    ]
    ajax, submits, hidden = out["afterPost"]
    assert ajax == [
        {
            "verb": "GET",
            "path": ITEMS_SRC,
            "target": "items",
            "source": True,
            "swap": "innerHTML",
            "headers": ITEMS_SCOPE,
        },
        status_fetch("todo-lists"),
    ]
    assert submits == load_submits and hidden is True
    # A GET, and a write outside the workbench, refresh nothing.
    assert out["ignored"] == [[], []]
    # A refused write shows the server's text, as text, and refreshes nothing.
    text, hidden, ajax, submits = out["refused"]
    assert text == "Refused (HTTP 403): You may not override this."
    assert hidden is False and ajax == [] and submits == []
    assert out["network"] == "The request failed (network error)."
    assert out["problem"] == "Refused (HTTP 409): Conflict: Pull first."
    assert out["innerHTML"] == 0
    # A successful write clears the notice.
    assert out["cleared"] == ["", True, 2]


def test_preview_needs_type_and_endpoints():
    script = r"""
reset();
out.sent = S.submitPreview({drafts: [{kind: 'css', content: {name: 'x'}}],
  mark: {stylesheet: 's', applies_to: '', index: 0}, after_push: null});
"""
    # No type, or no endpoints (the studio's URLs are not included): nothing.
    out = scenario(script, search=BASE)
    assert out["sent"] is False and out["submits"] == []
    out = scenario(script, search=BASE + "&type=todo-lists", endpoints=None)
    assert out["sent"] is False and out["submits"] == []
    # The URL state, T as the branch (pinned: its snapshot), and the extras.
    query = (
        "&type=todo-lists&mode=edit&scope=row,card&instance=12"
        "&context=project-a&pinned=1"
    )
    out = scenario(script, search=BASE + query)
    assert out["sent"] is True
    [sent] = out["submits"]
    state = {
        "branch": "alice-wip@pinned",
        "mode": "edit",
        "scope": ["row", "card"],
        "context": "project-a",
        "instance": 12,
        "mark": {"stylesheet": "s", "applies_to": "", "index": 0},
    }
    assert sent["request"] == preview_request(
        **state, drafts=[{"kind": "css", "content": {"name": "x"}}]
    )
    # On a published T the preview sends no drafts.
    out = scenario(script, search=BASE + query, published=True)
    [sent] = out["submits"]
    assert sent["request"] == preview_request(**state)
