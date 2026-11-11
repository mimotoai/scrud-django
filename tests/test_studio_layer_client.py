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
const out = {ajax: [], replaced: [], submits: [], opened: [], innerHTML: 0,
  changes: 0};
// The elements htmx.process() was given (nodes: not printed).
let processed = [];

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
    this.capture = {};
    this.text = '';
    this.value = '';
    if (parent) parent.appendChild(this);
  }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) {
    this.children = this.children.filter((n) => n !== c);
    c.parentNode = null;
    return c;
  }
  get firstChild() { return this.children[0] || null; }
  cloneNode(deep) {
    const copy = new Node(this.tagName, this.attrs);
    copy.text = this.text;
    if (deep) this.children.forEach((c) => copy.appendChild(c.cloneNode(true)));
    return copy;
  }
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
  addEventListener(type, fn, capture) {
    const where = capture === true ? this.capture : this.listeners;
    (where[type] = where[type] || []).push(fn);
  }
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
el('span', {'class': 'studio-origin'}, li).text = 'from main';
const override = el('button', {type: 'button', 'class': 'studio-override',
  'hx-post': '/workbench/ui-components/', 'hx-ext': 'scrud-json',
  'hx-swap': 'none', 'data-scrud-json': 'fixed',
  'data-scrud-body': '{"name": "head", "branch": "alice-wip"}',
  'data-scrud-etag': ''}, li);
override.text = 'Override on this branch';
// alice-wip's own row, and a packaged item (no href: its content in a
// hidden <pre>, as tojson escapes it).
const ownLi = el('li', {'class': 'studio-item studio-own'}, itemsFrame);
const row = {kind: 'component', name: 'row', applies_to: null,
  href: '/workbench/ui-components/8/', override: null};
const openRow = el('button', {type: 'button', 'class': 'studio-open',
  'data-studio-open': JSON.stringify(row)}, ownLi);
el('span', {'class': 'studio-origin'}, ownLi).text = 'own';
const packagedLi = el('li', {'class': 'studio-item studio-packaged'}, itemsFrame);
const page = {kind: 'component', name: 'studio:page', applies_to: null, href: null,
  override: null};
const openPage = el('button', {type: 'button', 'class': 'studio-open',
  'data-studio-open': JSON.stringify(page)}, packagedLi);
const pre = el('pre', {'data-studio-content': '', hidden: ''}, packagedLi);
pre.text = '{\n  "name": "studio:page",\n  "template": "\\u003cmain\\u003e"\n}';
const editorHead = el('div', {'class': 'studio-editor-head',
  'data-studio-editor-head': '', hidden: ''}, panes.editor);
const editor = el('div', {'class': 'studio-pane-body', 'data-studio-editor': ''},
  panes.editor);
const draftErrors = el('div', {'class': 'studio-draft-errors',
  'data-studio-draft-errors': '', role: 'status', hidden: ''}, panes.editor);
const form = el('form', {'data-studio-preview-form': '', method: 'post',
  target: 'studio-preview', hidden: ''}, panes.preview);
const token = el('input', {type: 'hidden', name: 'csrfmiddlewaretoken', value: ''}, form);
const request = el('input', {type: 'hidden', name: 'request', value: ''}, form);
form.submit = function () {
  out.submits.push({action: form.getAttribute('action'), token: token.value,
    request: JSON.parse(request.value)});
};
const iframe = el('iframe', {name: 'studio-preview', sandbox: 'allow-scripts',
  'class': 'studio-preview'}, panes.preview);
iframe.contentWindow = {name: 'preview window'};
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
// Capture listeners run first, from the root down; stopPropagation() ends
// the dispatch.
function fire(target, type, detail, evt) {
  evt = evt || {type, detail};
  evt.target = target;
  evt.stopPropagation = () => { evt.stopped = true; };
  const path = [];
  for (let n = target; n; n = n.parentNode) path.push(n);
  const attached = path.includes(root);
  for (const n of path.slice().reverse()) {
    (n.capture[type] || []).forEach((fn) => fn(evt));
    if (evt.stopped) return evt;
  }
  for (const n of path) {
    (n.listeners[type] || []).forEach((fn) => fn(evt));
    if (evt.stopped) return evt;
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
    const target = context.target.getAttribute('data-studio-embed')
      || (context.target === editor ? 'editor' : null);
    out.ajax.push({verb, path, target, source: context.source === context.target,
      swap: context.swap, headers: context.headers});
  },
  process(elt) { processed.push(elt); },
};
// Timers run when `tick(ms)` passes their time.
let now = 0;
let timers = [];
let timerId = 0;
function tick(ms) {
  now += ms;
  const due = timers.filter((t) => t.at <= now);
  timers = timers.filter((t) => t.at > now);
  due.forEach((t) => t.fn());
}
const windowListeners = {};
const window = {
  location, history, htmx,
  setTimeout(fn, ms) { timerId += 1; timers.push({id: timerId, fn, at: now + ms}); return timerId; },
  clearTimeout(id) { timers = timers.filter((t) => t.id !== id); },
  addEventListener(type, fn) { (windowListeners[type] = windowListeners[type] || []).push(fn); },
};
// A message posted to the page from `source`.
const post = (source, data) => (windowListeners.message || []).forEach(
  (fn) => fn({type: 'message', source, data}));
// The server's editor for an item, as htmx swaps it into the editor pane:
// the frame (a form in edit mode), its scrud-content, the editor element,
// the code textarea (named in edit mode) and the frame's Delete button.
function swapEditor({href = '/workbench/ui-components/8/', edit = true, kind = 'component',
                     field = 'template', content = {name: 'row', engine: 'jinja2',
                     mode: 'view', template: '<b>row</b>', branch: 'alice-wip',
                     rbac_context: 'alice'}, deletable = true} = {}) {
  while (editor.firstChild) editor.removeChild(editor.firstChild);
  const frame = el(edit ? 'form' : 'div', {'class': 'scrud-resource',
    'data-scrud-href': href, 'data-scrud-etag': '"e1"',
    'data-scrud-scope': 'studio studio-editor',
    ...(edit ? {'hx-put': href, 'hx-trigger': 'change, submit'} : {})}, editor);
  frame.addEventListener('change', () => { out.changes += 1; });
  el('script', {type: 'application/json', 'class': 'scrud-content'}, frame).text =
    JSON.stringify(content);
  const item = el('div', {'class': 'studio-editor', 'data-studio-editor-item': '',
    'data-studio-kind': kind, 'data-studio-mode': edit ? 'edit' : 'view'}, frame);
  const code = el('textarea', edit
    ? {'class': 'studio-code', name: '/' + field, 'data-studio-code': ''}
    : {'class': 'studio-code', readonly: ''}, item);
  code.value = content[field];
  const remove = deletable ? el('button', {type: 'button', 'hx-delete': href,
    'hx-confirm': 'Delete?', 'hx-target': 'closest .scrud-resource'}, frame) : null;
  if (remove) remove.text = 'Delete';
  fire(frame, 'htmx:afterSwap', {target: editor});
  return {frame, code, remove};
}
const typing = (code, text) => { code.value = text; fire(code, 'keyup', undefined); };
Object.defineProperty(window, 'localStorage', {
  get() {
    if (opts.storage === 'missing') throw new Error('SecurityError');
    return storage;
  },
});
workbench.addEventListener('studio:open', (evt) => out.opened.push(evt.detail.open));
const context = vm.createContext({window, document, URLSearchParams, CustomEvent,
  DOMParser, console, htmx, setTimeout: window.setTimeout,
  clearTimeout: window.clearTimeout});
vm.runInContext(source, context);
const S = window.ScrudStudio;
const state = () => S.state();
const collapsed = () => Object.fromEntries(Object.entries(panes).map(
  ([name, pane]) => [name, pane.hasAttribute('data-studio-collapsed')]));
const pressed = () => Object.fromEntries(Object.entries(toggles).map(
  ([name, t]) => [name, t.getAttribute('aria-pressed')]));
const reset = () => {
  out.ajax = []; out.submits = []; out.replaced = []; processed = [];
};
const texts = (node) => node.children.map((c) => [c.tagName.toLowerCase(), c.text,
  texts(c)]);
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


EDITOR_SCOPE = {"Scrud-UI-Scope": "studio studio-editor"}
VIEW_SCOPE = {**EDITOR_SCOPE, "Scrud-UI-Mode": "view"}
ROW_HREF = "/workbench/ui-components/8/"
ROW = {
    "name": "row",
    "engine": "jinja2",
    "mode": "view",
    "template": "<b>row</b>",
    "branch": "alice-wip",
    "rbac_context": "alice",
}


def editor_fetch(path, headers):
    return {
        "verb": "GET",
        "path": path,
        "target": "editor",
        "source": True,
        "swap": "innerHTML",
        "headers": headers,
    }


def test_open_loads_editor():
    out = scenario(
        r"""
reset();
click(openRow);
out.own = [out.ajax.slice(), texts(editorHead), editorHead.hasAttribute('hidden')];
reset();
click(label);
out.inherited = [out.ajax.slice(), texts(editorHead),
  editorHead.children.filter((c) => c.tagName === 'BUTTON').map((c) => c.attrs),
  processed.map((p) => editorHead.contains(p) && p !== override)];
reset();
click(openPage);
out.packaged = [out.ajax.slice(), texts(editorHead), texts(editor)];
// The frame's Delete is the one revert control.
click(openRow);
const swapped = swapEditor();
out.revert = [swapped.remove.text, swapped.remove.getAttribute('hx-confirm'),
  swapped.remove.getAttribute('class'), swapped.remove.getAttribute('hx-delete')];
""",
        search=BASE,
    )
    own_fetch, own_head, own_hidden = out["own"]
    assert own_fetch == [editor_fetch(ROW_HREF, EDITOR_SCOPE)]
    assert own_head == [["h3", "component row", []], ["span", "own", []]]
    assert own_hidden is False
    # An item T does not own: view mode, and the tree's override, copied
    # (its body the server's) and processed by htmx.
    fetch, head, buttons, processed = out["inherited"]
    assert fetch == [editor_fetch("/workbench/ui-components/7/", VIEW_SCOPE)]
    assert head[:2] == [["h3", "component head", []], ["span", "from main", []]]
    assert buttons == [
        {
            "type": "button",
            "class": "studio-override",
            "hx-post": "/workbench/ui-components/",
            "hx-ext": "scrud-json",
            "hx-swap": "none",
            "data-scrud-json": "fixed",
            "data-scrud-body": '{"name": "head", "branch": "alice-wip"}',
            "data-scrud-etag": "",
        }
    ]
    assert processed == [True]
    # No href: the content as text, parsed and indented; nothing fetched.
    fetch, head, body = out["packaged"]
    assert fetch == []
    assert head == [["h3", "component studio:page", []]]
    content = json.dumps({"name": "studio:page", "template": "<main>"}, indent=2)
    assert body == [["pre", content, []]]
    assert out["innerHTML"] == 0
    label_text, confirm, classes, href = out["revert"]
    assert label_text == "Revert to inherited" and href == ROW_HREF
    assert "studio-revert" in classes.split() and confirm != "Delete?"


def test_open_published_reopen_and_refresh():
    # A published T: every item in view mode, no override offered.
    out = scenario(
        r"""
reset();
click(openRow);
click(label);
out.fetches = out.ajax.slice();
out.buttons = editorHead.children.filter((c) => c.tagName === 'BUTTON').length;
out.processed = processed.length;
""",
        search=BASE,
        published=True,
    )
    assert out["fetches"] == [
        editor_fetch(ROW_HREF, VIEW_SCOPE),
        editor_fetch("/workbench/ui-components/7/", VIEW_SCOPE),
    ]
    assert out["buttons"] == 0 and out["processed"] == 0
    # The URL's item opens on load.
    out = scenario(
        "out.loaded = out.ajax.slice();", search=BASE + "&item=component:row"
    )
    assert out["loaded"] == [editor_fetch(ROW_HREF, EDITOR_SCOPE)]
    # After an override or a revert, the refreshed tree's entry opens again;
    # after the editor's own save (its response is the editor) it does not.
    out = scenario(
        r"""
click(openRow);
const {frame, remove} = swapEditor();
reset();
const done = (elt, verb) => fire(elt, 'htmx:afterRequest', {elt, successful: true,
  requestConfig: {verb}, xhr: {status: 200}});
done(frame, 'put');
fire(items, 'htmx:afterSwap', {target: items});
out.saved = out.ajax.map((a) => a.target);
reset();
done(remove, 'delete');
out.reverted = out.ajax.map((a) => a.target);
reset();
fire(items, 'htmx:afterSwap', {target: items});
fire(items, 'htmx:afterSwap', {target: items});
out.reopened = out.ajax.slice();
reset();
// An item the refreshed tree no longer has.
S.setState({item: 'component:gone'});
done(override, 'post');
reset();
fire(items, 'htmx:afterSwap', {target: items});
out.gone = [out.ajax.length, texts(editor), editorHead.hasAttribute('hidden')];
""",
        search=BASE,
    )
    assert out["saved"] == ["items", "status"]
    assert out["reverted"] == ["items", "status"]
    assert out["reopened"] == [editor_fetch(ROW_HREF, EDITOR_SCOPE)]
    count, body, hidden = out["gone"]
    assert count == 0 and hidden is True
    assert body == [["p", "component:gone is not on this branch.", []]]
    for fetch in out["reopened"]:
        assert "Scrud-UI-Branch" not in fetch["headers"]


def test_keyup_debounced_sends_drafts():
    out = scenario(
        r"""
click(openRow);
const {code} = swapEditor();
reset();
typing(code, '<i>1</i>');
tick(100);
typing(code, '<i>12</i>');
tick(299);
out.early = out.submits.length;
tick(1);
out.sent = out.submits.slice();
reset();
typing(code, '<i>12</i>');
tick(300);
out.same = out.submits.length;
// A change of the code never reaches the frame: Save is the only save.
fire(code, 'change');
out.changes = out.changes;
// View mode: no drafts.
const view = swapEditor({edit: false});
reset();
typing(view.code, 'x');
tick(300);
out.view = out.submits.length;
// The CSS and JS editor.
const css = swapEditor({href: '/workbench/ui-css/3/', kind: 'css', field: 'css',
  content: {name: 'site', css: 'p {}', branch: 'alice-wip', rbac_context: 'alice'}});
typing(css.code, 'p { color: red }');
tick(300);
out.css = out.submits.map((s) => s.request.drafts);
reset();
// Replaced before the delay passed (a save, another item): nothing.
const before = swapEditor();
typing(before.code, 'late');
swapEditor();
tick(300);
out.replaced = out.submits.length;
""",
        search=BASE + "&type=todo-lists",
    )
    assert out["early"] == 0
    [sent] = out["sent"]
    assert sent["action"] == "/studio/live-preview"
    assert sent["request"] == preview_request(
        drafts=[{"kind": "component", "content": {**ROW, "template": "<i>12</i>"}}]
    )
    assert out["same"] == 0
    assert out["changes"] == 0
    assert out["view"] == 0
    assert out["css"] == [
        [
            {
                "kind": "css",
                "content": {
                    "name": "site",
                    "css": "p { color: red }",
                    "branch": "alice-wip",
                    "rbac_context": "alice",
                },
            }
        ]
    ]
    assert out["replaced"] == 0
    # No type chosen: nothing is sent.
    out = scenario(
        r"""
click(openRow);
const {code} = swapEditor();
typing(code, 'x');
tick(300);
""",
        search=BASE,
    )
    assert out["submits"] == []


def test_no_drafts_when_published():
    out = scenario(
        r"""
click(openRow);
// Even an edit-mode editor (crafted): a published T sends no drafts.
const {code} = swapEditor();
reset();
typing(code, '<i>draft</i>');
tick(1000);
out.after = out.submits.slice();
out.direct = S.submitPreview({drafts: [{kind: 'component', content: {name: 'row'}}]});
""",
        search=BASE + "&type=todo-lists",
        published=True,
    )
    assert out["after"] == []
    assert out["direct"] is True
    [direct] = out["submits"]
    assert direct["request"]["drafts"] == []


def test_draft_errors_shown_as_text():
    out = scenario(
        r"""
const errors = [{kind: 'component', name: 'row',
                 messages: ['Template error: <b>x</b>', 'Second.']},
                {kind: 'css', name: 'site', messages: ['Bad.']}];
post(iframe.contentWindow, {type: 'scrud-preview:errors', errors});
out.shown = [draftErrors.hasAttribute('hidden'), texts(draftErrors)];
// From another window, of another type, or not an object: ignored.
post({}, {type: 'scrud-preview:errors', errors: null, failed: true});
post(null, {type: 'scrud-preview:errors', errors: []});
post(iframe.contentWindow, {type: 'scrud-preview:select', pointer: '/a', href: null});
post(iframe.contentWindow, 'scrud-preview:errors');
out.kept = texts(draftErrors);
post(iframe.contentWindow, {type: 'scrud-preview:errors', errors: null, failed: true});
out.failed = [draftErrors.hasAttribute('hidden'), texts(draftErrors)];
post(iframe.contentWindow, {type: 'scrud-preview:errors', errors: []});
out.none = [draftErrors.hasAttribute('hidden'), texts(draftErrors)];
""",
        search=BASE + "&type=todo-lists",
    )
    hidden, shown = out["shown"]
    assert hidden is False
    assert shown == [
        ["p", "Not shown in the preview:", []],
        [
            "ul",
            "",
            [
                ["li", "component row: Template error: <b>x</b> Second.", []],
                ["li", "css site: Bad.", []],
            ],
        ],
    ]
    assert out["kept"] == shown
    hidden, failed = out["failed"]
    assert hidden is False
    assert failed == [
        ["p", "The preview could not report its errors: a draft may not be shown.", []]
    ]
    assert out["none"] == [True, []]
    assert out["innerHTML"] == 0
