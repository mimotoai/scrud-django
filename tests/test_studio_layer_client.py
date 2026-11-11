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
from urllib.parse import urlsplit

import pytest
from django.http import QueryDict
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.registration import ResourceTypeRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI
from scrud_django.ui.types import UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_studio_client import BRIDGE_HARNESS, BRIDGE_JS, run_bridge, setup_wip, tree
from .test_studio_live_preview import grant_items, live
from .test_ui_branch_types import make_branch
from .test_ui_http import grant, make_stylesheet
from .test_ui_static import SCRUD_UI as SCRUD_UI_JS
from .test_ui_static import require_node

LAYER_PACKAGE = "scrud_django.ui.studio.layer"

# argv[1]: the script; argv[2]: "1" when the page has a workbench, "push"
# when it is a push's page (a ``[data-studio-push]`` element). Prints what the
# script did.
HARNESS = r"""
const vm = require('vm');
const source = process.argv[1];
const withWorkbench = process.argv[2] === '1';
const withPush = process.argv[2] === 'push';
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
    if (withWorkbench && selector === '[data-studio-workbench]') return workbench;
    return withPush && selector === '[data-studio-push]' ? workbench : null;
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


def layer_source(file="studio.json"):
    layer_js = importlib.resources.files(LAYER_PACKAGE).joinpath(f"js/{file}")
    return json.loads(layer_js.read_text(encoding="utf-8"))["js"]


def run(with_workbench, file="studio.json", push=False):
    source = layer_source(file)
    page = "push" if push else "1" if with_workbench else "0"
    done = subprocess.run(
        ["node", "-e", HARNESS, source, page],
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


def test_rules_js_inert_without_workbench():
    require_node()
    out = run(with_workbench=False, file="rules.json")
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
  const m = /^([a-zA-Z]*)((?:\.[\w-]+|\[[\w-]+(?:\^?="[^"]*")?\])*)$/.exec(sel);
  if (!m) throw new Error('stub: unsupported selector ' + sel);
  const parts = {tag: m[1].toUpperCase(), classes: [], attrs: []};
  const re = /\.([\w-]+)|\[([\w-]+)(?:(\^?)="([^"]*)")?\]/g;
  let p;
  while ((p = re.exec(m[2]))) {
    if (p[1]) parts.classes.push(p[1]);
    else parts.attrs.push([p[2], p[4], p[3] === '^']);
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
  // As a form control reads them (ScrudUI.fieldsOf and buildBody do).
  get name() { return this.attrs.name; }
  get type() {
    if (this.tagName === 'TEXTAREA') return 'textarea';
    return this.tagName === 'INPUT' ? this.attrs.type || 'text' : undefined;
  }
  matches(sel) {
    const s = parseSelector(sel);
    if (s.tag && s.tag !== this.tagName) return false;
    const classes = (this.attrs['class'] || '').split(/\s+/);
    if (!s.classes.every((c) => classes.includes(c))) return false;
    return s.attrs.every(([n, v, prefix]) => n in this.attrs && (v === undefined
      || (prefix ? this.attrs[n].startsWith(v) : this.attrs[n] === v)));
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
// opts.override: the attributes of a button the server rendered.
const override = el('button', opts.override || {type: 'button', 'class': 'studio-override',
  'hx-post': '/workbench/ui-components/', 'hx-ext': 'scrud-json',
  'hx-swap': 'none', 'data-scrud-json': 'fixed',
  'data-scrud-body': '{"name": "head", "branch": "alice-wip"}',
  'data-scrud-etag': ''}, li);
override.text = 'Override on this branch';
// alice-wip's own row, and a packaged item (no href: its content in a
// hidden <pre>, as tojson escapes it).
const ownLi = el('li', {'class': 'studio-item studio-own'}, itemsFrame);
const row = {kind: 'component', name: 'row', applies_to: null,
  href: '/workbench/ui-components/8/', override: null, reverts_to: 'inherited'};
const openRow = el('button', {type: 'button', 'class': 'studio-open',
  'data-studio-open': JSON.stringify(row)}, ownLi);
el('span', {'class': 'studio-origin'}, ownLi).text = 'own';
// An item alice-wip added: nothing beneath it to revert to.
const freshLi = el('li', {'class': 'studio-item studio-own'}, itemsFrame);
const fresh = {kind: 'component', name: 'fresh', applies_to: null,
  href: '/workbench/ui-components/9/', override: null, reverts_to: null};
const openFresh = el('button', {type: 'button', 'class': 'studio-open',
  'data-studio-open': JSON.stringify(fresh)}, freshLi);
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
// The top bar's branch picker (studio:branch-picker), with opts.picker: the
// branches' entries, linked when they have a workbench.
const branchesEmbed = el('div', {'class': 'studio-branches', 'data-studio-embed': 'branches',
  'data-studio-src': '/workbench/ui-selectable-branches/mine/',
  'data-studio-scope': 'studio studio-branches'}, workbench);
function renderPicker(names) {
  while (branchesEmbed.firstChild) branchesEmbed.removeChild(branchesEmbed.firstChild);
  const nav = el('nav', {'class': 'studio-branch-picker', 'data-studio-branch-picker': ''},
    el('div', {'class': 'scrud-resource'}, branchesEmbed));
  const entries = {};
  for (const name of names) {
    const li = el('li', {'class': 'studio-branch-choice', 'data-studio-branch-name': name}, nav);
    const link = name === 'main' ? null : el('a', {'class': 'studio-branch-link',
      href: '/workbench/ui-branches/' + name + '/?ui-scope=studio&ui-mode=view'}, li);
    entries[name] = {li, link};
  }
  return entries;
}
let pickerEntries = opts.picker ? renderPicker(opts.picker) : {};

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
// With argv[3] (rules.js) and argv[4] (scrud-ui.js): the page's ScrudUI (its
// pure helpers: scrud-ui.js stops before its htmx part without a document),
// then rules.js, after studio.js as the page orders them.
if (process.argv[3]) {
  const uiContext = vm.createContext({window: {}, console});
  vm.runInContext(process.argv[4], uiContext);
  window.ScrudUI = uiContext.window.ScrudUI;
  vm.runInContext(process.argv[3], context);
}
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


def scenario(script, rules=False, **opts):
    """Run studio.js on the stub workbench with `opts` (and, with `rules`,
    the page's ScrudUI and rules.js after it, with `RULES_STUB`), then
    `script`; return what it printed (JSON)."""
    require_node()
    extra = [layer_source("rules.json"), SCRUD_UI_JS.read_text()] if rules else []
    done = subprocess.run(
        [
            "node",
            "-e",
            WORKBENCH
            + (RULES_STUB if rules else "")
            + script
            + "\nconsole.log(JSON.stringify(out));",
            layer_source(),
            json.dumps(opts),
            *extra,
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
// A 409 is a stale state: the items and the strip are fetched again.
out.problemAjax = out.ajax.map((a) => a.target); reset();
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
    assert out["problemAjax"] == ["items", "status"]
    assert out["innerHTML"] == 0
    # A successful write clears the notice.
    assert out["cleared"] == ["", True, 2]


def test_open_packaged_groups_stay_open_after_a_refresh():
    """A "Packaged (N)" group the author opened closed again whenever the
    items pane was fetched again (after any write): the kinds whose group
    is open are noted before the refresh, and their groups in the new tree
    get ``open`` again (an attribute, never markup). A group that was
    closed stays closed."""
    out = scenario(
        r"""
// The tree as studio:items renders it: a section per kind, its packaged
// group collapsed.
function renderTree(open) {
  while (itemsFrame.firstChild) itemsFrame.removeChild(itemsFrame.firstChild);
  const groups = {};
  for (const kind of ['component', 'stylesheet', 'css', 'js']) {
    const section = el('section', {'class': 'studio-kind', 'data-studio-kind': kind},
      itemsFrame);
    const attrs = {'class': 'studio-packaged-group', 'data-studio-packaged': ''};
    if (open.includes(kind)) attrs.open = '';
    groups[kind] = el('details', attrs, section);
    el('summary', {}, groups[kind]).text = 'Packaged (1)';
  }
  return groups;
}
const opened = (groups) => Object.keys(groups).filter((k) => groups[k].hasAttribute('open'));
const ok = (elt) => fire(elt, 'htmx:afterRequest', {
  elt, successful: true, requestConfig: {verb: 'post'}, xhr: {status: 201}});
let groups = renderTree([]);
// The author opens two groups; a write (Pull) succeeds.
groups.component.setAttribute('open', '');
groups.js.setAttribute('open', '');
reset();
ok(pull);
out.refreshed = out.ajax.map((a) => a.target);
// The new tree swaps in, every group collapsed as the server renders it.
groups = renderTree([]);
fire(items, 'htmx:afterSwap', {target: items});
out.after = opened(groups);
// Closed again by the author: the next refresh leaves it closed.
groups.component.removeAttribute('open');
ok(pull);
groups = renderTree([]);
fire(items, 'htmx:afterSwap', {target: items});
out.again = opened(groups);
// A swap of the items pane with no refresh of ours keeps the server's.
groups = renderTree(['css']);
fire(items, 'htmx:afterSwap', {target: items});
out.unasked = opened(groups);
""",
        search=BASE,
    )
    assert out["refreshed"] == ["items", "status"]
    assert out["after"] == ["component", "js"]
    assert out["again"] == ["js"]
    assert out["unasked"] == ["css"]
    assert out["innerHTML"] == 0


def test_stale_write_refreshes_items_and_strip():
    """A write refused 412 (a stale ETag) or 409 shows the server's text and
    fetches the items and the strip again, so the next try sends their new
    ETags: a Pull refused 412 would otherwise keep the strip's stale tag and
    be refused again on every retry. Other refusals refresh nothing."""
    out = scenario(
        r"""
reset();
const refused = (status) => fire(pull, 'htmx:afterRequest', {elt: pull,
  successful: false, requestConfig: {verb: 'put'}, xhr: {status,
  responseText: '{"title": "Stale"}',
  getResponseHeader: () => 'application/problem+json'}});
for (const status of [412, 409, 403, 400, 0]) {
  refused(status);
  out['s' + status] = [notice.textContent, out.ajax.slice(), out.submits.slice()];
  reset();
}
"""
    )
    refreshed = [
        {
            "verb": "GET",
            "path": ITEMS_SRC,
            "target": "items",
            "source": True,
            "swap": "innerHTML",
            "headers": ITEMS_SCOPE,
        },
        {
            "verb": "GET",
            "path": STATUS_SRC,
            "target": "status",
            "source": True,
            "swap": "innerHTML",
            "headers": STATUS_SCOPE,
        },
    ]
    for status in (412, 409):
        text, ajax, submits = out[f"s{status}"]
        assert text == f"Refused (HTTP {status}): Stale", status
        assert ajax == refreshed and submits == [], status
    for status in (403, 400, 0):
        _text, ajax, submits = out[f"s{status}"]
        assert ajax == [] and submits == [], status


# A refusal of new-component's (a missing template), as scoped-rbac's
# uniform errors answer it.
UNIFORM_400 = {
    "type": "about:blank",
    "title": "Bad Request",
    "status": 400,
    "detail": "Invalid input.",
    "code": "invalid",
    "errors": {
        "template": [{"code": "invalid", "message": "A template is required."}],
        "": [{"code": "invalid", "message": "Pick a match."}],
    },
}


def test_refusal_shows_field_messages():
    """A validation refusal says why: each field's messages, after its path
    (none for the non-field errors under ""), whether the body is a uniform
    problem (``errors``), a bare ``{field: [messages]}`` (no title or
    detail) or a JSON validation error naming its field (``name``). Text
    only."""
    out = scenario(
        r"""
const xhr = (status, body, type = 'application/problem+json') => ({status,
  responseText: typeof body === 'string' ? body : JSON.stringify(body),
  getResponseHeader: () => type});
out.uniform = S.errorText(xhr(400, UNIFORM));
out.bare = S.errorText(xhr(400, {template: ['A template is required.'],
  where: {scope: ['Not a scope <b>here</b>.']}, applies_to: 'Must be "".'},
  'application/json'));
out.named = [S.errorText(xhr(400, {title: 'JSON Validation Error',
  detail: 'must be string', name: 'data.template'})),
  S.errorText(xhr(400, {title: 'JSON Validation Error',
  detail: 'data.template must be string', name: 'data.template'}))];
// Messages that are no text are left out, and so is an empty "errors".
out.odd = S.errorText(xhr(400, {title: 'Bad Request', detail: 'Invalid input.',
  errors: {template: [{code: 'invalid'}, 7, null], x: []}}));
out.plain = S.errorText(xhr(409, {title: 'Conflict', detail: 'Pull first.'}));
fire(override, 'htmx:afterRequest', {elt: override, successful: false,
  requestConfig: {verb: 'post'}, xhr: xhr(400, UNIFORM)});
out.notice = [notice.textContent, notice.hasAttribute('hidden'), notice.children.length];
""".replace("UNIFORM", json.dumps(UNIFORM_400)),
    )
    uniform = (
        "Refused (HTTP 400): Bad Request: Invalid input."
        " template: A template is required.; Pick a match."
    )
    assert out["uniform"] == uniform
    assert out["bare"] == (
        "Refused (HTTP 400): template: A template is required.;"
        " where.scope: Not a scope <b>here</b>.; applies_to: Must be \"\"."
    )
    # The field named once: not again when the detail names it.
    assert out["named"] == [
        "Refused (HTTP 400): JSON Validation Error: data.template: must be string",
        "Refused (HTTP 400): JSON Validation Error: data.template must be string",
    ]
    assert out["odd"] == "Refused (HTTP 400): Bad Request: Invalid input."
    assert out["plain"] == "Refused (HTTP 409): Conflict: Pull first."
    assert out["notice"] == [uniform, False, 0]
    assert out["innerHTML"] == 0


def test_branch_picker_marks_T():
    """The picker lists the user's branches; a component can't know which
    one the page shows, so studio.js marks T's entry (data-studio-current,
    and aria-current on its link), on load and when the picker is fetched
    again."""
    out = scenario(
        r"""
const marks = (entries) => Object.fromEntries(Object.entries(entries).map(
  ([name, e]) => [name, [e.li.hasAttribute('data-studio-current'),
                         e.link ? e.link.getAttribute('aria-current') : null]]));
out.load = marks(pickerEntries);
pickerEntries = renderPicker(['main', 'alice-wip', 'zed']);
fire(branchesEmbed, 'htmx:afterSwap', {target: branchesEmbed});
out.swapped = marks(pickerEntries);
""",
        picker=["main", "alice-wip", "studio"],
    )
    assert out["load"] == {
        "main": [False, None],
        "alice-wip": [True, "page"],
        "studio": [False, None],
    }
    assert out["swapped"] == {
        "main": [False, None],
        "alice-wip": [True, "page"],
        "zed": [False, None],
    }


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
// An item T added: its Delete deletes it, and says so.
click(openFresh);
const added = swapEditor({href: '/workbench/ui-components/9/'});
out.remove = [added.remove.text, added.remove.getAttribute('hx-confirm'),
  added.remove.getAttribute('class'), added.remove.getAttribute('hx-delete')];
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
    assert "studio-revert" in classes.split()
    assert confirm == (
        "Revert to inherited? This branch's own version of the item is deleted."
    )
    label_text, confirm, classes, href = out["remove"]
    assert label_text == "Delete" and href == "/workbench/ui-components/9/"
    assert "studio-revert" not in (classes or "").split()
    assert confirm == (
        "Delete this item from this branch? The branch has no inherited"
        " version of it you can read to show instead."
    )


def test_failed_editor_load_clears_the_old_item():
    """Opening an item empties the editor until its frame arrives: a load
    that fails with nothing swapped in (404, 401, 5xx, no network) leaves no
    frame of the item open before under the new item's header, and says
    why. A refusal htmx swaps (its notice) and a failed load of another
    item are left alone."""
    out = scenario(
        r"""
const all = (node, sel) => node.querySelectorAll(sel);
click(openRow);
swapEditor();
reset();
click(openFresh);
out.loading = [texts(editor), texts(editorHead)[0], out.ajax.length];
const failed = (status, text, path = '/workbench/ui-components/9/') => fire(editor,
  'htmx:afterRequest', {elt: editor, successful: false,
  requestConfig: {verb: 'get', path}, xhr: {status, responseText: text || '',
    getResponseHeader: () => 'application/problem+json'}});
// The item opened before failing late: not this load's.
failed(404, '', '/workbench/ui-components/8/');
out.other = texts(editor);
failed(404, '{"title": "Not Found", "detail": "No <b>such</b> item."}');
out.failed = [texts(editor), all(editor, '.scrud-resource').length,
  all(editor, '[hx-put]').length, texts(editorHead)[0]];
click(openRow);
failed(0, '', '/workbench/ui-components/8/');
out.network = texts(editor);
// The server's notice, swapped in for a 403: kept.
click(openFresh);
while (editor.firstChild) editor.removeChild(editor.firstChild);
el('div', {'class': 'scrud-notice'}, editor).text = 'You may not read it.';
failed(403, '{"title": "Forbidden"}');
out.swapped = texts(editor);
// The item loaded: a failure of a request inside its frame is its own.
click(openRow);
const {frame} = swapEditor();
fire(frame, 'htmx:afterRequest', {elt: frame, successful: false,
  requestConfig: {verb: 'get'}, xhr: {status: 500, responseText: ''}});
out.inside = [all(editor, '.scrud-resource').length, all(editor, '[hx-put]').length];
""",
        search=BASE,
    )
    body, head, fetches = out["loading"]
    assert body == [["p", "Loading component fresh...", []]]
    assert head == ["h3", "component fresh", []] and fetches == 1
    assert out["other"] == body
    body, frames, puts, head = out["failed"]
    assert frames == 0 and puts == 0
    assert body == [["p", "Refused (HTTP 404): Not Found: No <b>such</b> item.", []]]
    assert head == ["h3", "component fresh", []]
    assert out["network"] == [["p", "The request failed (network error).", []]]
    assert out["swapped"] == [["div", "You may not read it.", []]]
    assert out["inside"] == [1, 1]
    assert out["innerHTML"] == 0


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


# The rule editor as the layer renders it into the editor pane (the
# stylesheet's frame: a form in edit mode, its scrud-content, the fields, the
# rows with their array operations, the counts and the append row), for
# rules.js. `swapSheet` swaps it in as htmx does.
RULES_STUB = r"""
const THEME = {name: 'theme', applies_to: '', order: 2,
  prefixes: {ex: 'http://example.org/todo#'},
  rules: [{select: 'ex:TodoItem', component: 'row'},
          {select: 'compact ex:TodoItem', component: 'card'},
          {select: 'string', component: 'scrud:string'}],
  branch: 'alice-wip', rbac_context: 'alice'};
const input = (attrs, parent, value) => {
  const node = el('input', Object.assign({type: 'text'}, attrs), parent);
  node.value = value === undefined || value === null ? '' : String(value);
  return node;
};
function swapSheet({edit = true, content = THEME} = {}) {
  while (editor.firstChild) editor.removeChild(editor.firstChild);
  const href = '/workbench/ui-stylesheets/5/';
  const frame = el(edit ? 'form' : 'div', {'class': 'scrud-resource',
    'data-scrud-href': href, 'data-scrud-etag': '"e1"',
    'data-scrud-scope': 'studio studio-editor',
    ...(edit ? {'hx-put': href, 'hx-trigger': 'change, submit'} : {})}, editor);
  frame.addEventListener('change', (evt) => {
    out.changes += 1;
    if (evt.detail && evt.detail.scrudArrayOp) out.arrayOps = (out.arrayOps || 0) + 1;
  });
  el('script', {type: 'application/json', 'class': 'scrud-content'}, frame).text =
    JSON.stringify(content);
  const sheet = el('div', {'class': 'studio-editor studio-stylesheet',
    'data-studio-editor-item': '', 'data-studio-kind': 'stylesheet',
    'data-studio-mode': edit ? 'edit' : 'view', 'data-studio-stylesheet': content.name,
    'data-studio-applies-to': content.applies_to || ''}, frame);
  const fields = {};
  if (edit) {
    fields.appliesTo = input({name: '/applies_to', 'data-studio-field': ''}, sheet,
      content.applies_to);
    fields.order = input({type: 'number', name: '/order', 'data-studio-field': ''}, sheet,
      content.order);
    const fresh = el('div', {'class': 'studio-prefix-new', 'data-studio-prefix-new': ''},
      sheet);
    fields.key = input({'data-studio-prefix-key': '', 'data-studio-field': ''}, fresh);
    fields.iri = input({'data-studio-prefix-iri': '', 'data-studio-field': ''}, fresh);
  }
  const list = el('ol', {'class': 'studio-rule-list'}, sheet);
  const rows = content.rules.map((rule, i) => {
    const li = el('li', Object.assign({'class': 'studio-rule',
      'data-studio-rule-index': String(i)}, edit ? {} : {tabindex: '0'}), list);
    if (!edit) {
      el('code', {'class': 'studio-rule-select'}, li).text = rule.select;
      return {li};
    }
    const select = input({name: '/rules/' + i + '/select', 'data-studio-field': ''}, li,
      rule.select);
    const component = input({name: '/rules/' + i + '/component', 'data-studio-field': ''},
      li, rule.component);
    const remove = el('button', {type: 'button', 'data-scrud-array-op': 'remove',
      'data-scrud-pointer': '/rules/' + i}, li);
    return {li, select, component, remove};
  });
  const counts = el('p', {'class': 'studio-rule-counts', 'data-studio-rule-counts': '',
    role: 'status', hidden: ''}, sheet);
  if (edit) {
    fields.newSelect = input({name: '/rules/-/select', 'data-studio-field': ''}, sheet);
    fields.newComponent = input({name: '/rules/-/component', 'data-studio-field': ''},
      sheet);
  }
  fire(frame, 'htmx:afterSwap', {target: editor});
  return {frame, rows, counts, fields};
}
const focus = (node) => fire(node, 'focusin');
const blur = (node) => fire(node, 'focusout');
const shown = (counts) => [counts.hasAttribute('hidden'), counts.textContent];
const requests = () => out.submits.map((s) => s.request);
"""

THEME = {
    "name": "theme",
    "applies_to": "",
    "order": 2,
    "prefixes": {"ex": "http://example.org/todo#"},
    "rules": [
        {"select": "ex:TodoItem", "component": "row"},
        {"select": "compact ex:TodoItem", "component": "card"},
        {"select": "string", "component": "scrud:string"},
    ],
    "branch": "alice-wip",
    "rbac_context": "alice",
}
TYPED = BASE + "&type=todo-lists"


def theme(**changes):
    return json.loads(json.dumps({**THEME, **changes}))


def with_rule(index, **changes):
    content = theme()
    content["rules"][index].update(changes)
    return content


def mark(index, stylesheet="theme", applies_to=""):
    return {"stylesheet": stylesheet, "applies_to": applies_to, "index": index}


def sheet_draft(content):
    return [{"kind": "stylesheet", "content": content}]


def test_focus_sends_mark_with_draft():
    out = scenario(
        r"""
const {frame, rows, fields} = swapSheet();
reset();
// An unsaved edit of rule 1, then focus on its selector: the mark, and the
// form (the content with its inputs applied) as a draft.
rows[1].select.value = 'card ex:TodoItem';
focus(rows[1].select);
out.focused = requests();
reset();
// Within the row: the mark stays, nothing is sent.
blur(rows[1].select);
focus(rows[1].component);
focus(rows[1].remove);
tick(0);
out.within = requests();
// To another row: its mark, sent once.
blur(rows[1].remove);
focus(rows[0].component);
tick(0);
out.moved = requests();
reset();
// Typing in a field: once it pauses, the form again, with the mark.
rows[0].component.value = 'row-b';
fire(rows[0].component, 'keyup');
tick(299);
out.early = out.submits.length;
tick(1);
out.typed = requests();
reset();
fire(rows[0].component, 'keyup');
tick(300);
out.same = out.submits.length;
// A change of a field never reaches the frame (no save on blur); an array
// operation's change, triggered on the frame itself, does.
fire(rows[0].select, 'change');
fire(fields.order, 'change');
fire(fields.newSelect, 'change');
out.fieldChanges = out.changes;
fire(frame, 'change', {scrudArrayOp: {op: 'remove', pointer: '/rules/0'}});
out.arrayChanges = [out.changes, out.arrayOps];
// A new prefix: the IRI input is named by its key, escaped.
fields.iri.value = 'urn:y:';
fields.key.value = 'a/b~c';
fire(fields.key, 'input');
out.named = fields.iri.getAttribute('name');
reset();
blur(rows[0].component);
focus(rows[2].select);
tick(0);
out.prefixed = requests();
fields.key.value = '';
fire(fields.key, 'input');
out.unnamed = fields.iri.hasAttribute('name');
out.innerHTML = out.innerHTML;
""",
        rules=True,
        search=TYPED,
    )
    edited = with_rule(1, select="card ex:TodoItem")
    assert out["focused"] == [preview_request(drafts=sheet_draft(edited), mark=mark(1))]
    assert out["within"] == []
    assert out["moved"] == [preview_request(drafts=sheet_draft(edited), mark=mark(0))]
    assert out["early"] == 0
    typed = with_rule(1, select="card ex:TodoItem")
    typed["rules"][0]["component"] = "row-b"
    assert out["typed"] == [preview_request(drafts=sheet_draft(typed), mark=mark(0))]
    assert out["same"] == 0
    assert out["fieldChanges"] == 0
    assert out["arrayChanges"] == [1, 1]
    assert out["named"] == "/prefixes/a~1b~0c"
    prefixed = json.loads(json.dumps(typed))
    prefixed["prefixes"]["a/b~c"] = "urn:y:"
    assert out["prefixed"] == [
        preview_request(drafts=sheet_draft(prefixed), mark=mark(2))
    ]
    assert out["unnamed"] is False
    assert out["innerHTML"] == 0


def test_focus_marks_without_drafts_when_published_or_view():
    script = r"""
const {rows} = swapSheet(OPTS);
reset();
rows[0].select ? (rows[0].select.value = 'changed') : null;
focus(rows[1].select || rows[1].li);
out.focused = requests();
reset();
blur(rows[1].select || rows[1].li);
tick(0);
out.blurred = requests();
reset();
if (rows[0].select) { fire(rows[0].select, 'keyup'); tick(300); }
out.typed = requests();
"""
    # A published T (even a crafted edit-mode editor): the mark only.
    out = scenario(
        script.replace("OPTS", "{}"), rules=True, search=TYPED, published=True
    )
    assert out["focused"] == [preview_request(mark=mark(1))]
    assert out["blurred"] == [preview_request()]
    assert out["typed"] == []
    # View mode: a row takes focus itself; the mark of the stored stylesheet.
    out = scenario(script.replace("OPTS", "{edit: false}"), rules=True, search=TYPED)
    assert out["focused"] == [preview_request(mark=mark(1))]
    assert out["blurred"] == [preview_request()]
    # Another stylesheet's identity: its name and applies_to.
    out = scenario(
        r"""
const {rows} = swapSheet({edit: false, content: Object.assign({}, THEME,
  {name: 'acme-sheet', applies_to: 'acme'})});
reset();
focus(rows[2].li);
out.focused = requests();
""",
        rules=True,
        search=TYPED,
    )
    assert out["focused"] == [preview_request(mark=mark(2, "acme-sheet", "acme"))]
    # No type to preview: nothing is sent.
    out = scenario(
        r"""
const {rows} = swapSheet();
reset();
focus(rows[0].select);
blur(rows[0].select);
tick(0);
""",
        rules=True,
        search=BASE,
    )
    assert out["submits"] == []


def test_blur_clears_mark():
    out = scenario(
        r"""
const {rows, counts} = swapSheet();
focus(rows[1].select);
post(iframe.contentWindow, {type: 'scrud-preview:marks', wins: 1, loses: 2});
reset();
blur(rows[1].select);
out.pending = requests();
tick(0);
// The form is as stored: no draft.
out.cleared = [requests(), shown(counts)];
reset();
// With an unsaved edit, the form is still the draft.
focus(rows[0].select);
rows[0].select.value = 'ex:TodoList';
reset();
blur(rows[0].select);
tick(0);
out.edited = requests();
reset();
// The editor swapped out (a save, another item) before focus settled:
// nothing.
focus(rows[2].select);
reset();
blur(rows[2].select);
swapSheet();
tick(0);
out.swapped = requests();
""",
        rules=True,
        search=TYPED,
    )
    assert out["pending"] == []
    requests, counts = out["cleared"]
    assert requests == [preview_request()]
    assert counts == [True, ""]
    assert out["edited"] == [
        preview_request(drafts=sheet_draft(with_rule(0, select="ex:TodoList")))
    ]
    assert out["swapped"] == []


def test_marks_message_updates_counts():
    out = scenario(
        r"""
const {rows, counts} = swapSheet();
// No row marked: the counts stay hidden.
post(iframe.contentWindow, {type: 'scrud-preview:marks', wins: 0, loses: 0});
out.unmarked = shown(counts);
focus(rows[1].select);
out.focused = shown(counts);
post(iframe.contentWindow, {type: 'scrud-preview:marks', wins: 2, loses: 3});
out.counted = shown(counts);
// From another window, another type, or counts that are not non-negative
// integers: ignored.
post({}, {type: 'scrud-preview:marks', wins: 9, loses: 9});
post(null, {type: 'scrud-preview:marks', wins: 9, loses: 9});
post(iframe.contentWindow, {type: 'scrud-preview:select', wins: 9, loses: 9});
post(iframe.contentWindow, 'scrud-preview:marks');
for (const [wins, loses] of [[1.5, 0], [-1, 0], ['2', 0], [0, NaN], [0, Infinity],
                             [0, null], [undefined, 0]]) {
  post(iframe.contentWindow, {type: 'scrud-preview:marks', wins, loses});
}
out.kept = shown(counts);
// A mark of another row clears the counts until its preview answers.
blur(rows[1].select);
focus(rows[0].select);
tick(0);
out.moved = shown(counts);
post(iframe.contentWindow, {type: 'scrud-preview:marks', wins: 0, loses: 4});
out.recounted = shown(counts);
// The draft errors are studio.js's: untouched.
out.draftErrors = draftErrors.hasAttribute('hidden');
blur(rows[0].select);
tick(0);
post(iframe.contentWindow, {type: 'scrud-preview:marks', wins: 1, loses: 1});
out.blurred = shown(counts);
""",
        rules=True,
        search=TYPED,
    )
    assert out["unmarked"] == [True, ""]
    assert out["focused"] == [True, ""]
    assert out["counted"] == [False, "matches 5 nodes, wins on 2"]
    assert out["kept"] == out["counted"]
    assert out["moved"] == [True, ""]
    assert out["recounted"] == [False, "matches 4 nodes, wins on 0"]
    assert out["draftErrors"] is True
    assert out["blurred"] == [True, ""]
    assert out["innerHTML"] == 0


# The preview's bridge is a static file, not a layer item: run as
# test_studio_client runs it, on a live preview the server rendered. Here a
# branch script (run after the bridge, before the page is loaded) removes
# every marker: the bridge counted them when it ran.
BRIDGE_MARKS_HARNESS = BRIDGE_HARNESS.replace(
    "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);",
    "document.readyState = 'loading';"
    "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
    "document.querySelectorAll('[data-scrud-match]').forEach("
    "(n) => n.removeAttribute('data-scrud-match'));"
    "document.readyState = 'complete';"
    "(document.listeners.DOMContentLoaded || []).forEach("
    "(fn) => fn({type: 'DOMContentLoaded'}));",
)


def marks_message(wins, loses):
    return {
        "message": {"type": "scrud-preview:marks", "wins": wins, "loses": loses},
        "origin": "*",
    }


@pytest.mark.django_db
@test_context
def test_bridge_posts_marks(ui_alice_login, settings):
    """On load the bridge posts, after the errors, the counts of the nodes
    the preview marks as winning and losing (0 and 0 without a mark), read
    when it runs, before the branch's scripts."""
    settings.ROOT_URLCONF = "tests.mounted_urls"
    setup_wip(ui_alice_login)
    # The object rule wins on the list and loses on each item (a less
    # specific term than the item's class).
    make_stylesheet("s", [("ex:TodoItem", "scrud:object"), ("object", "scrud:object")])
    grant_items(ui_alice_login, "", UI_STYLESHEET_TYPE_URI, methods=("GET",))
    marked = live(mark={"stylesheet": "s", "applies_to": "", "index": 1})
    wins = marked.count('data-scrud-match="wins"')
    loses = marked.count('data-scrud-match="loses"')
    assert wins == 1 and loses >= 1
    out = run_bridge(marked)
    assert out["loaded"] == [
        {"message": {"type": "scrud-preview:errors", "errors": []}, "origin": "*"},
        marks_message(wins, loses),
    ]
    # Without a mark: none.
    out = run_bridge(live())
    assert out["loaded"][1:] == [marks_message(0, 0)]
    # Read when the bridge runs: a branch script's change comes too late.
    require_node()
    done = subprocess.run(
        ["node", "-e", BRIDGE_MARKS_HARNESS, str(BRIDGE_JS)],
        capture_output=True,
        text=True,
        input=json.dumps({"preview": tree(marked)}),
    )
    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout)["loaded"]
    assert loaded[1:] == [marks_message(wins, loses)]


# ---- the preview pane's controls and the inspector (inspector.js) --------


def test_inspector_js_inert_without_workbench():
    require_node()
    out = run(with_workbench=False, file="inspector.json")
    assert out["queries"] == ["[data-studio-workbench]"]
    assert out["touched"] == ["document.querySelector"]
    assert out["listeners"] == [] and out["fetches"] == 0
    assert out["attributes"] == {}


# The preview pane's controls (the type picker as studio:type-picker renders
# it, in its embedded frame) and the inspector under the preview, as
# studio:workbench renders them (no new rule form on a published T); a
# `fetch` that records each request and answers when the scenario says
# (`respond`); then inspector.js (argv[5]), after studio.js and rules.js.
INSPECTOR_STUB = r"""
const controlsBox = el('div', {'class': 'studio-preview-controls',
  'data-studio-preview-controls': ''}, panes.preview);
const typesFrame = el('div', {'class': 'scrud-resource',
  'data-scrud-href': '/workbench/ui-type-catalog/all/'},
  el('div', {'class': 'studio-types', 'data-studio-types': ''}, controlsBox));
const control = {type: el('select', {'data-studio-control': 'type'}, typesFrame)};
for (const slug of ['', 'todo-lists', 'notes']) {
  el('option', {value: slug}, control.type);
}
control.mode = el('select', {'data-studio-control': 'mode'}, controlsBox);
for (const name of ['context', 'scope', 'instance']) {
  control[name] = el('input', {type: 'text', 'data-studio-control': name}, controlsBox);
}
control.pinned = el('input', {type: 'checkbox', 'data-studio-control': 'pinned'},
  controlsBox);
const inspector = el('section', {'class': 'studio-inspector',
  'data-studio-inspector': ''}, panes.preview);
const explainBox = el('div', {'class': 'studio-explain', 'data-studio-explain': '',
  'aria-live': 'polite'}, inspector);
const newRule = opts.published ? null : el('div', {'class': 'studio-new-rule',
  'data-studio-new-rule': '', hidden: ''}, inspector);
out.fetches = [];
const pendingFetches = [];
function fetch(url, init) {
  init = init || {};
  out.fetches.push({url: String(url), method: init.method || 'GET',
    headers: init.headers || {}, credentials: init.credentials || null,
    body: init.body === undefined ? null : JSON.parse(init.body)});
  return new Promise((resolve) => pendingFetches.push(resolve));
}
context.fetch = fetch;
window.fetch = fetch;
const settle = () => new Promise((resolve) => setImmediate(resolve));
// Answer the oldest pending fetch, and let the script take the answer.
async function respond(status, data, type = 'application/json') {
  const next = pendingFetches.shift();
  if (!next) throw new Error('stub: no fetch pending');
  const text = typeof data === 'string' ? data : JSON.stringify(data);
  next({ok: status >= 200 && status < 300, status,
    headers: {get(name) { return name.toLowerCase() === 'content-type' ? type : null; }},
    json() { return Promise.resolve(JSON.parse(text)); },
    text() { return Promise.resolve(text); }});
  for (let i = 0; i < 5; i += 1) await settle();
}
const select = (pointer, href) => post(iframe.contentWindow,
  {type: 'scrud-preview:select', pointer, href});
const change = (node, value) => {
  if (node.attrs.type === 'checkbox') node.checked = value;
  else node.value = value;
  fire(node, 'change', undefined);
};
// The elements of `node` (deep) matching `sel`.
const all = (node, sel) => node.querySelectorAll(sel);
const button = (label) => all(inspector, 'button').filter((b) => b.text === label)[0];
const fetched = () => out.fetches.map((f) => f.url);
const requests = () => out.submits.map((s) => s.request);
vm.runInContext(process.argv[5], context);
"""

ENDPOINTS = {
    "live_preview": "/studio/live-preview",
    "sample": "/studio/types/{slug}/sample",
    "explain": "/studio/types/{slug}/explain",
    "options": "/studio/types/{slug}/new-component/options",
    "new_component": "/studio/new-component",
}
SAMPLE_HREF = "/studio/types/todo-lists/sample"
EXPLAINED = {
    "pointer": "/items/0",
    "terms": ["http://example.org/todo#TodoItem", "object"],
    "scope": [],
    "winner": 1,
    "fallback": None,
    "rules": [
        {"readable": False, "rank": [3, 1, 0, 2], "outcome": "missing component"},
        {
            "readable": True,
            "rank": [2, 0, 1, 0],
            "outcome": "wins",
            "selector": "ex:TodoItem",
            "component": "row",
            "stylesheet": "s",
            "branch": "alice-wip",
            "applies_to": "",
        },
        {
            "readable": True,
            "rank": [1, 1, 0, 4],
            "outcome": "less specific term",
            "selector": "object",
            "component": "scrud:object",
            "stylesheet": "studio",
            "branch": "",
            "applies_to": "",
        },
    ],
}
OPTIONS = {
    "matches": [
        {
            "select": "http://example.org/todo#TodoItem",
            "label": "ex:TodoItem",
            "broad": False,
        },
        {"select": "object", "label": "every object on this branch", "broad": True},
    ],
    "inside_type": None,
    "where": ["row"],
    "applies_to": ["", "project-a"],
    "name": "todo-lists-items-view",
}


def inspector_scenario(script, **opts):
    """Run studio.js, rules.js and inspector.js on the stub workbench with
    the preview pane's controls and the inspector (`INSPECTOR_STUB`), with
    `opts` (the endpoints are `ENDPOINTS` unless given), then `script`, an
    async function's body (it may `await respond(...)`); return what it
    printed (JSON)."""
    require_node()
    opts.setdefault("endpoints", ENDPOINTS)
    script = (
        "(async () => {\n"
        + script
        + "\nconsole.log(JSON.stringify(out));\n})().catch((e) => {"
        " console.error(e && e.stack || e); process.exit(1); });"
    )
    done = subprocess.run(
        [
            "node",
            "-e",
            WORKBENCH + INSPECTOR_STUB + script,
            layer_source(),
            json.dumps(opts),
            layer_source("rules.json"),
            SCRUD_UI_JS.read_text(),
            layer_source("inspector.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def query_of(url, path):
    """The parameters of `url` as the server reads them (Django's
    ``QueryDict``), after checking its path is `path`."""
    parts = urlsplit(url)
    assert parts.path == path, url
    return {key: values for key, values in QueryDict(parts.query).lists()}


def explain_query(pointer, **params):
    """What the inspector asks `explain` for `pointer` on alice-wip."""
    found = {"branch": ["alice-wip"], "mode": ["view"], "scope": [""], "context": [""]}
    found.update({key: [value] for key, value in params.items()})
    return {**found, "pointer": [pointer]}


EXPLAIN_PATH = "/studio/types/todo-lists/explain"
OPTIONS_PATH = "/studio/types/todo-lists/new-component/options"


def test_csrf_token_filled_from_meta():
    out = scenario(
        r"""
out.first = out.submits.map((s) => [s.action, s.token]);
meta.setAttribute('content', 'rotated');
reset();
S.submitPreview({});
out.second = out.submits.map((s) => [s.action, s.token]);
out.inForm = token.value;
""",
        search=BASE + "&type=todo-lists",
    )
    # The node component has no csrf_token: the token is the meta tag's,
    # read on every submit.
    assert out["first"] == [["/studio/live-preview", "tok"]]
    assert out["second"] == [["/studio/live-preview", "rotated"]]
    assert out["inForm"] == "rotated"


def test_controls_update_state_and_resubmit():
    out = inspector_scenario(
        r"""
out.initial = [control.type.value, control.mode.value, control.context.value,
  control.scope.value, control.instance.value, !!control.pinned.checked];
reset();
change(control.mode, 'edit');
change(control.context, ' project-a ');
change(control.scope, 'row card');
change(control.instance, '12');
change(control.pinned, true);
out.set = [state(), requests(), out.ajax.length];
reset();
change(control.instance, '');
change(control.pinned, false);
out.cleared = [state(), requests()];
reset();
change(control.type, 'notes');
out.typed = [state(), requests(), out.ajax.map((a) => a.path)];
""",
        search=BASE + "&type=todo-lists&mode=view&scope=row&instance=7&pointer=%2Fa",
    )
    assert out["initial"] == ["todo-lists", "view", "", "row", "7", False]
    state, requests, ajax = out["set"]
    assert state == {
        "type": "todo-lists",
        "mode": "edit",
        "context": "project-a",
        "scope": "row card",
        "instance": "12",
        "pinned": "1",
    }
    # Each change resubmits the preview (the pointer is of the old preview:
    # it is cleared), and only a new type fetches the strip.
    assert len(requests) == 5 and ajax == 0
    assert requests[-1] == preview_request(
        branch="alice-wip@pinned",
        mode="edit",
        scope=["row", "card"],
        context="project-a",
        instance=12,
    )
    state, requests = out["cleared"]
    assert "instance" not in state and "pinned" not in state
    assert requests[-1]["instance"] is None
    assert requests[-1]["branch"] == "alice-wip"
    state, requests, ajax = out["typed"]
    assert state["type"] == "notes"
    assert [r["type"] for r in requests] == ["notes"]
    assert ajax == [f"{STATUS_SRC}?type=notes"]


def test_select_message_from_preview_only():
    out = inspector_scenario(
        r"""
reset();
select('/items/0', SAMPLE);
out.fromPreview = [fetched(), state().pointer || null];
// From another window, of another shape, or not an object: ignored.
post({}, {type: 'scrud-preview:select', pointer: '/title', href: SAMPLE});
post(null, {type: 'scrud-preview:select', pointer: '/title', href: SAMPLE});
post(iframe.contentWindow, {type: 'scrud-preview:select', pointer: 5, href: SAMPLE});
post(iframe.contentWindow, {type: 'scrud-preview:errors', errors: []});
post(iframe.contentWindow, 'scrud-preview:select');
out.ignored = [fetched(), state().pointer];
""".replace("SAMPLE", repr(SAMPLE_HREF)),
        search=BASE + "&type=todo-lists",
    )
    urls, pointer = out["fromPreview"]
    assert pointer == "/items/0"
    [url] = urls
    assert query_of(url, EXPLAIN_PATH) == explain_query("/items/0")
    assert out["ignored"] == [urls, "/items/0"]


def test_select_in_embedded_resource_not_explained():
    out = inspector_scenario(
        r"""
select('/items/0', '/api/todo-lists/31/');
out.embedded = [fetched(), state().pointer || null, texts(explainBox)];
// An instance: its own frame is its detail; another is embedded.
S.setState({instance: '7'});
select('/title', '/api/todo-lists/7/');
out.own = fetched();
await respond(200, {pointer: '/title', terms: ['string'], scope: [], winner: null,
  fallback: 'scrud:string', rules: []});
select('/title', SAMPLE);
out.sampleHref = [fetched().length, texts(explainBox), state().pointer || null];
select('/title', null);
out.noHref = [fetched().length, texts(explainBox)];
// A list's item, of the same type and key, has an absolute URL: embedded.
S.setState({instance: '7'});
select('/title', 'http://testserver/api/todo-lists/7/');
select('/title', '//testserver/api/todo-lists/7/');
out.absolute = fetched().length;
""".replace("SAMPLE", repr(SAMPLE_HREF)),
        search=BASE + "&type=todo-lists",
    )
    urls, pointer, shown = out["embedded"]
    # Its pointer is relative to the embedded resource: not explained, and
    # the URL keeps no pointer.
    assert urls == [] and pointer is None
    assert shown == [["p", "Inside embedded resource /api/todo-lists/31/", []]]
    [own] = out["own"]
    assert query_of(own, EXPLAIN_PATH) == explain_query("/title", instance="7")
    # With an instance, the sample's href is not the previewed frame.
    count, shown, pointer = out["sampleHref"]
    assert count == 1 and pointer is None
    assert shown == [["p", f"Inside embedded resource {SAMPLE_HREF}", []]]
    count, shown = out["noHref"]
    assert count == 1
    assert shown == [["p", "Not inside the previewed resource.", []]]
    assert out["absolute"] == 1


def test_pointer_with_tilde_explained_exactly_in_client():
    """Review Focus 5: a click on a node whose pointer has ``~1`` and ``~0``
    asks ``explain`` for that exact pointer (as the server decodes the
    query), pinned, in the preview's mode, scope and context, with
    ``Scrud-UI-Safe`` on a safe page."""
    pointer = "/a~1b~0c/0/x y+z%"
    out = inspector_scenario(
        r"""
select(POINTER, SAMPLE);
out.state = state().pointer;
out.fetch = out.fetches[0];
""".replace("POINTER", json.dumps(pointer)).replace("SAMPLE", repr(SAMPLE_HREF)),
        search=BASE
        + "&ui-safe=1&type=todo-lists&mode=edit&scope=row,card&context=acme&pinned=1",
    )
    assert out["state"] == pointer
    fetch = out["fetch"]
    assert query_of(fetch["url"], EXPLAIN_PATH) == explain_query(
        pointer,
        branch="alice-wip@pinned",
        mode="edit",
        scope="row card",
        context="acme",
    )
    assert fetch["method"] == "GET" and fetch["credentials"] == "same-origin"
    # The script never sets the branch header (scrud-ui.js's is the page's).
    assert "Scrud-UI-Branch" not in fetch["headers"]


def test_fetches_send_safe_header_on_safe_page():
    out = inspector_scenario(
        r"""
body.setAttribute('class', 'scrud-branch-page');
select('/items/0', SAMPLE);
body.setAttribute('data-scrud-safe', '1');
select('/items/1', SAMPLE);
out.headers = out.fetches.map((f) => f.headers);
""".replace("SAMPLE", repr(SAMPLE_HREF)),
        search=BASE + "&type=todo-lists",
    )
    plain, safe = out["headers"]
    assert plain == {"Accept": "application/json"}
    assert safe == {"Accept": "application/json", "Scrud-UI-Safe": "1"}


def test_inspector_text_only():
    explained = json.loads(json.dumps(EXPLAINED))
    explained["pointer"] = "/<b>x</b>"
    explained["terms"] = ["<img src=x onerror=alert(1)>", "object"]
    explained["scope"] = ["<i>row</i>"]
    explained["rules"][1]["selector"] = "<script>alert(1)</script>"
    explained["rules"][1]["component"] = "<b>row</b>"
    out = inspector_scenario(
        r"""
select('/<b>x</b>', SAMPLE);
await respond(200, EXPLAINED);
out.shown = texts(explainBox);
""".replace("SAMPLE", repr(SAMPLE_HREF)).replace("EXPLAINED", json.dumps(explained)),
        search=BASE + "&type=todo-lists",
    )
    assert out["innerHTML"] == 0
    shown = out["shown"]
    assert shown[0] == ["p", "Node /<b>x</b>", []]
    assert shown[1] == [
        "dl",
        "",
        [
            ["dt", "Terms", []],
            ["dd", "<img src=x onerror=alert(1)> object", []],
            ["dt", "Scope", []],
            ["dd", "<i>row</i>", []],
        ],
    ]
    assert shown[2] == [
        "p",
        "Component <b>row</b>, chosen by <script>alert(1)</script>"
        " (stylesheet s on alice-wip).",
        [],
    ]
    assert shown[3] == [
        "ol",
        "",
        [
            ["li", "missing component: a rule you can't read (rank [3,1,0,2])", []],
            [
                "li",
                "wins: <script>alert(1)</script> -> <b>row</b>"
                " (stylesheet s on alice-wip)",
                [],
            ],
            # A rule below the winner says it was never tried (its outcome
            # is why it ranks lower, not that its mode was checked).
            [
                "li",
                "less specific term (below the winner, not tried):"
                " object -> scrud:object (stylesheet studio)",
                [],
            ],
        ],
    ]


def test_rules_below_the_winner_say_they_were_not_tried():
    """After Move up put ``row ex:TodoItem -> todo-row-edit`` before
    ``-> todo-row``, the edit-mode rule ranks below the view winner and
    `explain` reports why it ranks lower ("lower cascade rank"), as
    documented: the cascade never tries a rule below the winner, so its mode
    is not checked. The inspector says so, rather than leaving it to read as
    the reason the rule did not apply; a rule above the winner, which was
    tried, keeps its outcome as it is."""
    edit_row = {
        "readable": True,
        "rank": [1, 0, 2, 0, 0, 3],
        "outcome": "lower cascade rank",
        "selector": "row http://example.org/todo#TodoItem",
        "component": "todo-row-edit",
        "stylesheet": "todo",
        "branch": "main",
        "applies_to": "",
    }
    explained = {
        **EXPLAINED,
        "rules": [*EXPLAINED["rules"][:2], edit_row, EXPLAINED["rules"][2]],
    }
    out = inspector_scenario(
        r"""
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
out.rules = texts(explainBox).filter((row) => row[0] === 'ol')[0][2].map((li) => li[1]);
""".replace("SAMPLE", repr(SAMPLE_HREF)).replace("EXPLAINED", json.dumps(explained)),
        search=BASE + "&type=todo-lists",
    )
    assert out["rules"] == [
        "missing component: a rule you can't read (rank [3,1,0,2])",
        "wins: ex:TodoItem -> row (stylesheet s on alice-wip)",
        "lower cascade rank (below the winner, not tried):"
        " row http://example.org/todo#TodoItem -> todo-row-edit"
        " (stylesheet todo on main)",
        "less specific term (below the winner, not tried):"
        " object -> scrud:object (stylesheet studio)",
    ]


def test_explanation_refused_and_fallback_shown_as_text():
    out = inspector_scenario(
        r"""
select('/nope', SAMPLE);
await respond(400, {title: 'No such node', detail: 'Pointer <b>/nope</b>.'},
  'application/problem+json');
out.refused = texts(explainBox);
select('/done', SAMPLE);
await respond(200, {pointer: '/done', terms: ['boolean'], scope: [], winner: null,
  fallback: 'scrud:boolean', rules: []});
out.fallback = texts(explainBox);
// A slow answer for an older select never replaces a newer one.
select('/a', SAMPLE);
select('/b', SAMPLE);
await respond(200, {pointer: '/a', terms: [], scope: [], winner: null,
  fallback: 'scrud:json', rules: []});
out.stale = texts(explainBox);
""".replace("SAMPLE", repr(SAMPLE_HREF)),
        search=BASE + "&type=todo-lists",
    )
    assert out["refused"] == [
        ["p", "Refused (HTTP 400): No such node: Pointer <b>/nope</b>.", []]
    ]
    fallback = out["fallback"]
    assert fallback[0] == ["p", "Node /done", []]
    assert fallback[2] == [
        "p",
        "No rule chose a component: it gets the built-in scrud:boolean.",
        [],
    ]
    # No "Edit this component" for a built-in; "New rule for this node" is.
    labels = [c[1] for c in fallback[-1][2]]
    assert fallback[-1][0] == "div" and labels == ["New rule for this node"]
    assert all(row[1] != "Node /a" for row in out["stale"])


AFTER_WIN = {**EXPLAINED["rules"][1], "branch": "testing-admin"}


def test_explanation_fetched_again_after_a_write():
    """After an override and after a promote, the inspector still named the
    old winner's branch until a reload: a successful write from the
    workbench (the one that fetches the items, the strip and the preview
    again) asks `explain` again for the selected node. Nothing is asked
    with no node selected, after a refused write or a GET, and an answer to
    the explain the write superseded is dropped."""
    out = inspector_scenario(
        r"""
const ok = (elt, verb) => fire(elt, 'htmx:afterRequest', {
  elt, successful: true, requestConfig: {verb}, xhr: {status: 200}});
// Nothing selected: a write asks nothing.
ok(override, 'post');
out.unselected = fetched();
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
reset();
out.fetches = [];
// The override succeeds: the node is explained again.
ok(override, 'post');
out.afterWrite = [fetched(), texts(explainBox)[0]];
await respond(200, AFTER);
out.rendered = texts(explainBox);
out.fetches = [];
// A GET, and a refused write, ask nothing.
ok(pull, 'get');
fire(pull, 'htmx:afterRequest', {elt: pull, successful: false,
  requestConfig: {verb: 'put'}, xhr: {status: 403, responseText: '',
  getResponseHeader: () => null}});
out.ignored = fetched();
// A write while an explain is in flight: the older answer is dropped.
select('/items/1', SAMPLE);
ok(pull, 'put');
out.both = fetched();
await respond(200, Object.assign({}, EXPLAINED, {pointer: '/stale'}));
out.stale = texts(explainBox)[0];
await respond(200, Object.assign({}, AFTER, {pointer: '/items/1'}));
out.fresh = texts(explainBox)[0];
""".replace("SAMPLE", repr(SAMPLE_HREF))
        .replace("EXPLAINED", json.dumps(EXPLAINED))
        .replace(
            "AFTER",
            json.dumps(
                {
                    **EXPLAINED,
                    "rules": [EXPLAINED["rules"][0], AFTER_WIN, EXPLAINED["rules"][2]],
                }
            ),
        ),
        search=BASE + "&type=todo-lists",
    )
    assert out["unselected"] == []
    urls, first = out["afterWrite"]
    [url] = urls
    assert query_of(url, EXPLAIN_PATH) == explain_query("/items/0")
    assert first == ["p", "Explaining /items/0...", []]
    verdict = [row for row in out["rendered"] if "chosen by" in row[1]]
    assert verdict == [
        [
            "p",
            "Component row, chosen by ex:TodoItem (stylesheet s on testing-admin).",
            [],
        ]
    ]
    assert out["ignored"] == []
    selected, again = out["both"]
    assert query_of(selected, EXPLAIN_PATH) == explain_query("/items/1")
    assert query_of(again, EXPLAIN_PATH) == explain_query("/items/1")
    assert out["stale"] == ["p", "Explaining /items/1...", []]
    assert out["fresh"] == ["p", "Node /items/1", []]


def test_edit_this_component_opens_the_tree_entry():
    out = inspector_scenario(
        r"""
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
out.labels = all(inspector, 'button').map((b) => b.text);
click(toggles.editor);
out.collapsedBefore = panes.editor.hasAttribute('data-studio-collapsed');
reset();
click(button('Edit this component'));
out.opened = [out.opened.slice(), state().item, out.ajax.map((a) => [a.path, a.target]),
  panes.editor.hasAttribute('data-studio-collapsed')];
// A winner the tree does not list, or the user may not read: no action.
const unlisted = JSON.parse(JSON.stringify(EXPLAINED));
unlisted.rules[1].component = 'gone';
select('/items/0', SAMPLE);
await respond(200, unlisted);
out.unlisted = all(inspector, 'button').map((b) => b.text);
const unreadable = JSON.parse(JSON.stringify(EXPLAINED));
unreadable.winner = 0;
select('/items/0', SAMPLE);
await respond(200, unreadable);
out.unreadable = [all(inspector, 'button').map((b) => b.text), texts(explainBox)[2]];
""".replace("SAMPLE", repr(SAMPLE_HREF)).replace("EXPLAINED", json.dumps(EXPLAINED)),
        search=BASE + "&type=todo-lists",
    )
    assert out["labels"] == ["Edit this component", "New rule for this node"]
    opened, item, ajax, collapsed = out["opened"]
    assert opened == [
        {
            "kind": "component",
            "name": "row",
            "applies_to": None,
            "href": ROW_HREF,
            "override": None,
            "reverts_to": "inherited",
        }
    ]
    assert item == "component:row"
    assert ajax == [[ROW_HREF, "editor"]]
    # The editor pane opens if it was collapsed.
    assert out["collapsedBefore"] is True and collapsed is False
    assert out["unlisted"] == ["New rule for this node"]
    labels, verdict = out["unreadable"]
    assert labels == ["New rule for this node"]
    assert verdict == ["p", "Its component is chosen by a rule you can't read.", []]


def test_new_rule_posts_and_refreshes():
    out = inspector_scenario(
        r"""
// The instance's own frame.
select('/items/0', '/api/todo-lists/7/');
await respond(200, EXPLAINED);
reset();
click(button('New rule for this node'));
out.optionsUrl = fetched()[1];
await respond(200, OPTIONS);
out.form = [newRule.hasAttribute('hidden'), texts(newRule)];
const field = (name) => all(newRule, '[data-studio-new-field="' + name + '"]')[0];
out.defaults = ['match', 'where', 'applies_to', 'template'].map((n) => field(n).value);
field('where').value = 'row';
field('applies_to').value = 'project-a';
fire(field('applies_to'), 'change', undefined);
out.relabelled = texts(field('match')).map((o) => o[1]);
field('template').value = '<em>{{ value.text }}</em>';
reset();
click(button('Create'));
out.post = out.fetches[out.fetches.length - 1];
await respond(201, {written: [{kind: 'component', name: 'todo-lists-items-view'}],
  wins: true, explanation: EXPLAINED});
out.created = [out.ajax.map((a) => a.target), out.submits.length, fetched().slice(-1),
  texts(newRule).slice(-1)[0]];
await respond(200, EXPLAINED);
// A refusal shows the problem's text, and refreshes nothing.
reset();
click(button('Create'));
await respond(400, {title: 'Invalid', detail: 'Bad <b>template</b>.'},
  'application/problem+json');
out.refused = [out.ajax.length, out.submits.length, texts(newRule).slice(-1)[0]];
""".replace("EXPLAINED", json.dumps(EXPLAINED)).replace("OPTIONS", json.dumps(OPTIONS)),
        search=BASE + "&type=todo-lists&scope=row&instance=7&context=project-a",
    )
    assert query_of(out["optionsUrl"], OPTIONS_PATH) == explain_query(
        "/items/0", scope="row", instance="7", context="project-a"
    )
    hidden, form = out["form"]
    assert hidden is False
    labels = [row[1] for row in form]
    assert labels[0] == "New rule for this node"
    assert "It will be called todo-lists-items-view." in labels
    assert out["defaults"] == [
        "http://example.org/todo#TodoItem",
        "",
        "",
        "<span>{{ value }}</span>",
    ]
    assert out["relabelled"] == [
        "ex:TodoItem",
        "every object in project-a on this branch",
    ]
    post = out["post"]
    assert post["url"] == "/studio/new-component"
    assert post["method"] == "POST" and post["credentials"] == "same-origin"
    assert post["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRFToken": "tok",
    }
    assert post["body"] == {
        "branch": "alice-wip",
        "slug": "todo-lists",
        "mode": "view",
        "scope": "row",
        "context": "project-a",
        "instance": "7",
        "pointer": "/items/0",
        "match": "http://example.org/todo#TodoItem",
        "inside_type": False,
        "where": "row",
        "applies_to": "project-a",
        "template": "<em>{{ value.text }}</em>",
    }
    targets, submits, last_fetch, result = out["created"]
    # Everything again: the items, the strip, the preview and the
    # explanation of the node.
    assert targets == ["items", "status"] and submits == 1
    assert query_of(last_fetch[0], EXPLAIN_PATH) == explain_query(
        "/items/0", scope="row", instance="7", context="project-a"
    )
    assert result == [
        "p",
        "Created todo-lists-items-view. Its rule now wins for this node.",
        [],
    ]
    ajax, submits, result = out["refused"]
    assert ajax == 0 and submits == 0
    assert result == ["p", "Refused (HTTP 400): Invalid: Bad <b>template</b>.", []]
    assert out["innerHTML"] == 0


def test_new_rule_answer_after_its_form_closed():
    """Another node selected, or a control changed, while Create is in
    flight closes the form: the answer is not dropped, it goes to the
    workbench's notice (a refusal with its field messages, a success with
    what was created). A template typed in a closed form is kept for its
    node, and comes back when the form opens there again, until a Create
    from it succeeds; meanwhile it counts as unsaved."""
    out = inspector_scenario(
        r"""
const field = (name) => all(newRule, '[data-studio-new-field="' + name + '"]')[0];
const openForm = async (pointer) => {
  select(pointer, SAMPLE);
  await respond(200, EXPLAINED);
  click(button('New rule for this node'));
  await respond(200, OPTIONS);
};
await openForm('/items/0');
out.fresh = S.unsaved();
field('template').value = '<em>typed</em>';
out.typed = S.unsaved();
click(button('Create'));
// Another node while the Create is in flight: the form closes.
select('/title', SAMPLE);
out.closed = [newRule.hasAttribute('hidden'), S.unsaved()];
reset();
await respond(400, UNIFORM, 'application/problem+json');
out.refused = [notice.hasAttribute('hidden'), notice.textContent, out.ajax.length,
  out.submits.length];
await respond(200, EXPLAINED);
// Another node's form: the default template.
click(button('New rule for this node'));
await respond(200, OPTIONS);
out.other = field('template').value;
// Back to the node: what was typed there.
await openForm('/items/0');
out.restored = [field('template').value, texts(newRule).map((row) => row[1])];
// A control changed while the Create is in flight: closed; the success is
// in the notice, and everything is fetched again.
click(button('Create'));
change(control.mode, 'edit');
reset();
await respond(201, {written: [{kind: 'component', name: 'todo-lists-items-view'}],
  wins: true, explanation: EXPLAINED});
out.created = [notice.hasAttribute('hidden'), notice.textContent,
  out.ajax.map((a) => a.target), out.submits.length, S.unsaved()];
// Created: nothing kept for the node any more.
change(control.mode, 'view');
await openForm('/items/0');
out.after = field('template').value;
""".replace("SAMPLE", repr(SAMPLE_HREF))
        .replace("EXPLAINED", json.dumps(EXPLAINED))
        .replace("OPTIONS", json.dumps(OPTIONS))
        .replace("UNIFORM", json.dumps(UNIFORM_400)),
        search=BASE + "&type=todo-lists",
    )
    assert out["fresh"] is False and out["typed"] is True
    assert out["closed"] == [True, True]
    assert out["refused"] == [
        False,
        "New rule for /items/0: Refused (HTTP 400): Bad Request: Invalid input."
        " template: A template is required.; Pick a match.",
        0,
        0,
    ]
    assert out["other"] == "<span>{{ value }}</span>"
    template, labels = out["restored"]
    assert template == "<em>typed</em>"
    assert "The template you typed for this node before is back." in labels
    hidden, text, targets, submits, unsaved = out["created"]
    assert hidden is False
    assert text == (
        "New rule for /items/0: Created todo-lists-items-view."
        " Its rule now wins for this node."
    )
    assert targets == ["items", "status"] and submits == 1
    assert unsaved is False
    assert out["after"] == "<span>{{ value }}</span>"
    assert out["innerHTML"] == 0


def test_edit_new_component_whatever_answers_first():
    """After a new rule, the explanation and the items tree are fetched
    again at once. When the explanation (whose winner is the new
    component) arrives before the tree's swap, "Edit this component"
    appears once the tree lists it, and opens it."""
    created = json.loads(json.dumps(EXPLAINED))
    created["rules"][1]["component"] = "todo-lists-items-view"
    out = inspector_scenario(
        r"""
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
click(button('New rule for this node'));
await respond(200, OPTIONS);
click(button('Create'));
await respond(201, {written: [{kind: 'component', name: 'todo-lists-items-view'}],
  wins: true, explanation: CREATED});
// The explanation first: the tree does not list the new component yet.
await respond(200, CREATED);
out.before = all(inspector, 'button').map((b) => b.text);
// Then the items pane's swap, with the new component's entry.
const made = {kind: 'component', name: 'todo-lists-items-view', applies_to: null,
  href: '/workbench/ui-components/12/', override: null, reverts_to: null};
const madeLi = el('li', {'class': 'studio-item studio-own'}, itemsFrame);
el('button', {type: 'button', 'class': 'studio-open',
  'data-studio-open': JSON.stringify(made)}, madeLi);
fire(items, 'htmx:afterSwap', {target: items});
out.after = all(inspector, 'button').map((b) => b.text);
out.form = texts(newRule).slice(-1)[0];
reset();
click(button('Edit this component'));
out.opened = [out.opened.slice(-1), state().item, out.ajax.map((a) => [a.path, a.target])];
// A swap after another node was selected leaves its explanation alone.
select('/title', SAMPLE);
fire(items, 'htmx:afterSwap', {target: items});
out.pending = texts(explainBox);
""".replace("SAMPLE", repr(SAMPLE_HREF))
        .replace("EXPLAINED", json.dumps(EXPLAINED))
        .replace("OPTIONS", json.dumps(OPTIONS))
        .replace("CREATED", json.dumps(created)),
        search=BASE + "&type=todo-lists",
    )
    assert out["before"] == ["New rule for this node", "Create"]
    assert out["after"] == [
        "Edit this component",
        "New rule for this node",
        "Create",
    ]
    # The form and its result stay.
    assert out["form"] == [
        "p",
        "Created todo-lists-items-view. Its rule now wins for this node.",
        [],
    ]
    opened, item, ajax = out["opened"]
    assert opened == [
        {
            "kind": "component",
            "name": "todo-lists-items-view",
            "applies_to": None,
            "href": "/workbench/ui-components/12/",
            "override": None,
            "reverts_to": None,
        }
    ]
    assert item == "component:todo-lists-items-view"
    assert ajax == [["/workbench/ui-components/12/", "editor"]]
    assert out["pending"] == [["p", "Explaining /title...", []]]


def test_new_rule_off_while_pinned():
    out = inspector_scenario(
        r"""
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
const create = button('New rule for this node');
out.pinned = [create.hasAttribute('disabled'), texts(explainBox).slice(-1)];
click(create);
out.fetched = fetched().length;
""".replace("SAMPLE", repr(SAMPLE_HREF)).replace("EXPLAINED", json.dumps(EXPLAINED)),
        search=BASE + "&type=todo-lists&pinned=1",
    )
    disabled, actions = out["pinned"]
    assert disabled is True
    assert actions[0][2][-1] == [
        "p",
        "The preview is pinned at the branching point, but a new rule is made on"
        " the live branch. Unpin the preview to make one.",
        [],
    ]
    assert out["fetched"] == 1


def test_no_inspector_actions_when_published():
    out = inspector_scenario(
        r"""
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
out.shown = texts(explainBox);
out.buttons = all(inspector, 'button').length;
out.fetched = fetched().length;
""".replace("SAMPLE", repr(SAMPLE_HREF)).replace("EXPLAINED", json.dumps(EXPLAINED)),
        search=BASE + "&type=todo-lists",
        published=True,
    )
    # The explanation, and no action: no "Edit this component", no "New rule
    # for this node", and no options asked for.
    assert [row[0] for row in out["shown"]] == ["p", "dl", "p", "ol"]
    assert out["buttons"] == 0 and out["fetched"] == 1


def test_pointer_restored_on_load_is_explained():
    out = inspector_scenario(
        r"""
out.fetched = fetched();
""",
        search=BASE + "&type=todo-lists&pointer=%2Fa~1b~0c",
    )
    [url] = out["fetched"]
    assert query_of(url, EXPLAIN_PATH) == explain_query("/a~1b~0c")


@pytest.mark.django_db
@test_context
def test_pointer_with_tilde_explained_exactly(ui_alice_login, settings):
    """Review Focus 5, end to end: the live preview renders a node whose
    pointer has ``~1`` and ``~0``; the inspector, told of a click on it by
    the bridge's select message, asks ``explain`` a URL that explains that
    exact node."""
    settings.ROOT_URLCONF = "tests.mounted_urls"
    ResourceTypeRegistration(
        "tests://StudioTilde",
        slug="studio-tilde",
        schema_func=lambda: json.dumps(
            {"type": "object", "properties": {"a/b~c": {"type": "string"}}}
        ),
        context_func=lambda: json.dumps({"@type": "urn:x:Tilde"}),
        register_service=False,
    )
    reverse("todo-lists-list")
    make_branch("alice-wip", "alice")
    grant(ui_alice_login, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(ui_alice_login)
    page = live(type="studio-tilde", branch="alice-wip")
    pointer = "/a~1b~0c"
    assert f'<scrud-node data-pointer="{pointer}">' in page
    sample = reverse("scrud-studio:sample", args=["studio-tilde"])
    assert f'data-scrud-href="{sample}"' in page
    out = inspector_scenario(
        r"""
select(POINTER, SAMPLE);
out.url = fetched()[0];
""".replace("POINTER", json.dumps(pointer)).replace("SAMPLE", json.dumps(sample)),
        search=BASE + "&type=studio-tilde",
    )
    When.http.get(out["url"], HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    explained = Http.response.json()
    assert explained["pointer"] == pointer
    assert explained["terms"] == ["string"]


# ---- promote (studio:promote) -----------------------------------------------


def test_promote_js_inert_without_workbench():
    require_node()
    out = run(with_workbench=False, file="promote.json")
    # It also looks for a push's page, and finds none: nothing else.
    assert out["queries"] == ["[data-studio-workbench]", "[data-studio-push]"]
    assert out["touched"] == ["document.querySelector", "document.querySelector"]
    assert out["listeners"] == [] and out["fetches"] == 0
    assert out["attributes"] == {}


def test_push_page_installs_only_the_change_stop_and_the_preview():
    require_node()
    # On a push's page (no workbench) the other layer scripts stay inert.
    for file in ("studio.json", "rules.json", "inspector.json", "branch.json"):
        out = run(with_workbench=False, file=file, push=True)
        assert out["queries"] == ["[data-studio-workbench]"], file
        assert out["touched"] == ["document.querySelector"], file
        assert out["listeners"] == [] and out["fetches"] == 0, file
        assert out["attributes"] == {}, file
    # studio:promote installs two listeners on the document: the change
    # stop (only Save writes the push; it also takes the type picker's
    # change, which previews the target after the push) and the click of
    # "Preview after push". Nothing else: no query, fetch, window listener
    # or attribute until an event comes.
    out = run(with_workbench=False, file="promote.json", push=True)
    assert out["queries"] == ["[data-studio-workbench]", "[data-studio-push]"]
    assert out["touched"] == [
        "document.querySelector",
        "document.querySelector",
        "document.addEventListener",
        "document.addEventListener",
    ]
    assert out["listeners"] == ["document:change", "document:click"]
    assert out["fetches"] == 0 and out["attributes"] == {}


# A push's page as studio:push renders it in edit mode: the frame's form
# (htmx saves it on change and on submit: counted), the push's root, the
# state input and another input; a document whose events capture from the
# document down, then bubble. argv[1]: promote.js.
PUSH_PAGE = r"""
const vm = require('vm');
const out = {frame: [], input: []};
class Node {
  constructor(tag, attrs, parent) {
    this.tagName = tag.toUpperCase();
    this.attrs = attrs || {};
    this.parentNode = parent || null;
    this.listeners = {};
    this.capture = {};
  }
  hasAttribute(n) { return n in this.attrs; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  // Only "[attr]" selectors.
  closest(sel) {
    const name = /^\[([\w-]+)\]$/.exec(sel)[1];
    for (let n = this; n && n.attrs; n = n.parentNode) if (name in n.attrs) return n;
    return null;
  }
  addEventListener(type, fn, capture) {
    const where = capture === true ? this.capture : this.listeners;
    (where[type] = where[type] || []).push(fn);
  }
}
const document = new Node('#document', null, null);
document.attrs = null;
document.querySelector = (sel) => (sel === '[data-studio-push]' ? push : null);
const body = new Node('body', {}, document);
let frame;
let push;
let state;
let label;
function render() {
  frame = new Node('form', {'class': 'scrud-resource', 'hx-put': '/ui-pushes/7/',
    'hx-trigger': 'change, submit'}, body);
  frame.addEventListener('change', (e) => out.frame.push('change:' + e.target.attrs.name));
  frame.addEventListener('submit', () => out.frame.push('submit'));
  push = new Node('div', {'class': 'studio-push', 'data-studio-push': '',
    'data-studio-mode': 'edit'}, frame);
  state = new Node('input', {type: 'text', name: '/state', list: 'states',
    'data-studio-push-state-input': ''}, push);
  state.addEventListener('change', () => out.input.push('change'));
  label = new Node('input', {type: 'text', name: '/label'}, push);
}
function fire(target, type) {
  const evt = {type, target, stopped: false};
  evt.stopPropagation = () => { evt.stopped = true; };
  const path = [];
  for (let n = target; n; n = n.parentNode) path.push(n);
  for (const n of path.slice().reverse()) {
    (n.capture[type] || []).forEach((fn) => fn(evt));
    if (evt.stopped) return evt;
  }
  for (const n of path) {
    (n.listeners[type] || []).forEach((fn) => fn(evt));
    if (evt.stopped) return evt;
  }
  return evt;
}
render();
vm.runInContext(process.argv[1], vm.createContext({document, window: {}}));
fire(state, 'change');
fire(label, 'change');
fire(frame, 'submit');
// A refused PUT swaps a new frame in: the stop still holds.
render();
fire(state, 'change');
fire(frame, 'submit');
console.log(JSON.stringify(out));
"""


def test_push_state_saves_only_on_save():
    require_node()
    done = subprocess.run(
        ["node", "-e", PUSH_PAGE, layer_source("promote.json")],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    # A change of the state input never reaches the frame (no PUT); the
    # form's submit (Save, or Enter) does, as do other inputs' changes.
    assert out["frame"] == ["change:/label", "submit", "submit"]


# A push's page with "Preview after push" as studio:push and
# studio:push-after render it in edit mode: the frame's form (htmx saves it
# on change and on submit: counted), the push's root and state input, the
# embedded section with the type picker (in its own embedded frame), the
# button, the note, the live-preview form in a <template> (its content a
# separate fragment, as a browser parses it) and the iframe; the CSRF meta
# tag. A form's submit() is recorded with where the form is and what it
# sends. A click on a button that is not type="button" submits its form
# unless prevented, as a browser does. argv[1]: promote.js.
PUSH_AFTER = r"""
const vm = require('vm');
const out = {frame: [], submits: [], clicks: []};
class Node {
  constructor(tag, attrs, parent) {
    this.tagName = tag.toUpperCase();
    this.attrs = attrs || {};
    this.parentNode = null;
    this.children = [];
    this.listeners = {};
    this.capture = {};
    this.textContent = '';
    this.value = '';
    if (parent) parent.appendChild(this);
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1);
    child.parentNode = null;
    return child;
  }
  hasAttribute(n) { return n in this.attrs; }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  // Only "tag", "[attr]", "tag[attr]" and "[attr=\"value\"]" selectors.
  matches(sel) {
    const m = /^([a-z]*)(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(sel);
    if (!m) throw new Error('unsupported selector ' + sel);
    if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
    if (!m[2]) return !!m[1];
    if (!(m[2] in this.attrs)) return false;
    return m[3] === undefined || this.attrs[m[2]] === m[3];
  }
  closest(sel) {
    for (let n = this; n && n.attrs; n = n.parentNode) if (n.matches(sel)) return n;
    return null;
  }
  querySelector(sel) {
    for (const c of this.children) {
      if (c.matches(sel)) return c;
      const found = c.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  cloneNode(deep) {
    const copy = new Node(this.tagName.toLowerCase(), Object.assign({}, this.attrs));
    copy.value = this.value;
    copy.textContent = this.textContent;
    copy.clone = true;
    if (deep) this.children.forEach((c) => copy.appendChild(c.cloneNode(true)));
    return copy;
  }
  addEventListener(type, fn, capture) {
    const where = capture === true ? this.capture : this.listeners;
    (where[type] = where[type] || []).push(fn);
  }
  submit() {
    const field = (name) => this.querySelector('[name="' + name + '"]');
    out.submits.push({
      inBody: this.parentNode === body,
      clone: !!this.clone,
      forms: body.children.filter((c) => c.tagName === 'FORM' && c !== frame).length,
      method: this.getAttribute('method'),
      action: this.getAttribute('action'),
      target: this.getAttribute('target'),
      token: field('csrfmiddlewaretoken').value,
      request: JSON.parse(field('request').value),
    });
  }
}
const document = new Node('#document', null, null);
document.attrs = null;
document.querySelector = (sel) => {
  if (sel === '[data-studio-push]') return push;
  if (sel === 'meta[name="csrf-token"]') return meta;
  return null;
};
const head = new Node('head', {}, document);
const meta = new Node('meta', {name: 'csrf-token', content: 'tok-123'}, head);
const body = new Node('body', {}, document);
document.body = body;
let frame, push, state, label, section, select, button, note, iframe;
function render() {
  if (frame) body.removeChild(frame);
  frame = new Node('form', {'class': 'scrud-resource', 'hx-put': '/ui-pushes/7/',
    'hx-trigger': 'change, submit'}, body);
  frame.addEventListener('change', (e) => out.frame.push('change:' + e.target.attrs.name));
  frame.addEventListener('submit', () => out.frame.push('submit'));
  push = new Node('div', {'class': 'studio-push', 'data-studio-push': '',
    'data-studio-mode': 'edit'}, frame);
  state = new Node('input', {type: 'text', name: '/state',
    'data-studio-push-state-input': ''}, push);
  label = new Node('input', {type: 'text', name: '/label'}, push);
  const embedded = new Node('div', {'class': 'scrud-resource',
    'data-scrud-scope': 'studio studio-push-after'}, push);
  section = new Node('section', {'class': 'studio-push-page-after',
    'data-studio-push-after': '', 'data-studio-source': 'alice-wip',
    'data-studio-target': 'shop & co'}, embedded);
  const types = new Node('div', {'class': 'scrud-resource',
    'data-scrud-scope': 'studio studio-types'}, section);
  select = new Node('select', {'data-studio-control': 'type'}, types);
  button = new Node('button', {type: 'button', 'data-studio-push-after-show': ''}, section);
  note = new Node('p', {'data-studio-push-after-note': ''}, section);
  note.textContent = 'Choose a type to preview shop & co after this push.';
  const template = new Node('template', {'data-studio-push-after-form': ''}, section);
  template.content = new Node('#document-fragment', {});
  const form = new Node('form', {'class': 'studio-preview-form',
    'data-studio-preview-form': '', method: 'post', action: '/studio/live-preview',
    target: 'studio-push-after-preview', hidden: ''}, template.content);
  form.submit = () => { throw new Error('the template form itself is inert'); };
  new Node('input', {type: 'hidden', name: 'csrfmiddlewaretoken', value: ''}, form);
  new Node('input', {type: 'hidden', name: 'request', value: ''}, form);
  iframe = new Node('iframe', {name: 'studio-push-after-preview',
    'class': 'studio-preview', sandbox: 'allow-scripts'}, section);
}
function fire(target, type) {
  const evt = {type, target, stopped: false, defaultPrevented: false};
  evt.stopPropagation = () => { evt.stopped = true; };
  evt.preventDefault = () => { evt.defaultPrevented = true; };
  const path = [];
  for (let n = target; n; n = n.parentNode) path.push(n);
  let stopped = false;
  for (const n of path.slice().reverse()) {
    (n.capture[type] || []).forEach((fn) => fn(evt));
    if (evt.stopped) { stopped = true; break; }
  }
  if (!stopped) {
    for (const n of path) {
      (n.listeners[type] || []).forEach((fn) => fn(evt));
      if (evt.stopped) break;
    }
  }
  // A browser's default action of a click on a submit button.
  if (type === 'click' && target.tagName === 'BUTTON' &&
      target.getAttribute('type') !== 'button' && !evt.defaultPrevented) {
    const form = target.closest('form');
    if (form) fire(form, 'submit');
  }
  return evt;
}
function choose(value) { select.value = value; return fire(select, 'change'); }
render();
vm.runInContext(process.argv[1], vm.createContext({document, window: {}}));
"""


def push_after_scenario(script):
    """Run promote.js on `PUSH_AFTER`'s push page, then `script`; return
    ``out``."""
    require_node()
    done = subprocess.run(
        [
            "node",
            "-e",
            PUSH_AFTER + script + "\nconsole.log(JSON.stringify(out));",
            layer_source("promote.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


AFTER_REQUEST = {
    "type": "todo-lists",
    "branch": "shop & co",
    "mode": "view",
    "after_push": {"source": "alice-wip"},
}


def after_submit(request=AFTER_REQUEST, forms=1):
    return {
        "inBody": True,
        "clone": True,
        "forms": forms,
        "method": "post",
        "action": "/studio/live-preview",
        "target": "studio-push-after-preview",
        "token": "tok-123",
        "request": request,
    }


def test_push_page_type_choice_previews_target_after_push():
    out = push_after_scenario(
        r"""
// No type: nothing is sent, and the note says so.
out.none = [choose('').stopped, out.submits.length, note.textContent];
// A type: the live preview of the target after the push, in the clone of
// the template's form, put in the body (outside the frame's form).
out.chosen = [choose('todo-lists').stopped, note.textContent];
out.templateForm = section.querySelector('template').content.children.length;
"""
    )
    # The type picker's change never reaches the frame: no PUT.
    assert out["frame"] == []
    assert out["none"] == [
        True,
        0,
        "Choose a type to preview shop & co after this push.",
    ]
    assert out["chosen"] == [True, "Previewing shop & co after this push."]
    # Exactly the request: the target as the branch, the source pushed, view
    # mode, no drafts; the CSRF token from the meta tag.
    assert out["submits"] == [after_submit()]
    assert "drafts" not in out["submits"][0]["request"]
    # The template keeps its form (cloned, not moved).
    assert out["templateForm"] == 1


def test_push_page_preview_button_sends_and_never_saves():
    out = push_after_scenario(
        r"""
// Without a type, the button sends nothing.
let evt = fire(button, 'click');
out.clicks.push([evt.defaultPrevented, out.submits.length, note.textContent]);
select.value = 'todo-lists';
evt = fire(button, 'click');
out.clicks.push([evt.defaultPrevented, out.submits.length, note.textContent]);
// Again: the same clone is reused (one form in the body).
fire(button, 'click');
// The push's own inputs: the state's change is stopped, another's is not;
// the form's submit (Save) is the frame's.
fire(state, 'change');
fire(label, 'change');
fire(frame, 'submit');
// A refused PUT swaps a new frame in: both still hold.
render();
choose('notes');
fire(button, 'click');
fire(state, 'change');
"""
    )
    assert out["clicks"] == [
        [True, 0, "Choose a type to preview shop & co after this push."],
        [True, 1, "Previewing shop & co after this push."],
    ]
    notes = {**AFTER_REQUEST, "type": "notes"}
    assert out["submits"] == [
        after_submit(),
        after_submit(),
        after_submit(notes),
        after_submit(notes),
    ]
    # Neither a click nor a choice ever saves the push: only its own
    # inputs' changes and Save reach the frame.
    assert out["frame"] == ["change:/label", "submit"]


# The strip's promote panels as studio:status renders them, one per
# ``actions.promote`` entry (main: Apply and Propose; "shop & co": Propose
# only); an htmx.ajax that records the requests into a panel's review
# (`out.reviews`); `answer(panel, ...)` swaps the push preview (or a
# refusal) in as htmx does and fires the request's end; then promote.js
# (argv[5]), after studio.js and rules.js.
PROMOTE_STUB = r"""
out.reviews = [];
const promotes = el('div', {'class': 'studio-promotes'}, status);
// With `rendered` ({src, apply, propose}: what the server rendered), the
// box's source and its buttons' attributes are those.
function promoteBox(target, index, {apply = true, rendered = null} = {}) {
  const box = el('div', {'class': 'studio-promote', 'data-studio-promote': target,
    'data-studio-promote-src': rendered ? rendered.src
      : '/workbench/ui-push-previews/alice-wip/'}, promotes);
  const opener = el('button', {type: 'button', 'class': 'studio-promote-open',
    'data-studio-promote-open': '', 'aria-expanded': 'false',
    'aria-controls': 'studio-promote-' + index}, box);
  opener.text = 'Promote to ' + target + '…';
  const panel = el('div', {'class': 'studio-promote-panel',
    'data-studio-promote-panel': '', id: 'studio-promote-' + index, hidden: ''}, box);
  const review = el('div', {'class': 'studio-promote-review',
    'data-studio-promote-review': ''}, panel);
  const reason = el('p', {'class': 'studio-promote-reason',
    'data-studio-promote-reason': '', role: 'status', hidden: ''}, panel);
  const note = el('p', {'class': 'studio-promote-note', 'data-studio-promote-note': '',
    role: 'status', hidden: ''}, panel);
  const actions = el('div', {'class': 'studio-promote-actions'}, panel);
  const after = el('button', {type: 'button', 'class': 'studio-after-push',
    'data-studio-after-push': ''}, actions);
  const body = (state) => JSON.stringify({source: 'alice-wip', target, state,
    rbac_context: ''});
  const applyButton = apply ? el('button', rendered ? rendered.apply : {type: 'button',
    'class': 'studio-apply',
    'hx-post': '/workbench/ui-pushes/', 'hx-ext': 'scrud-json', 'hx-swap': 'none',
    'data-scrud-json': 'fixed', 'data-scrud-body': body('applied'),
    'data-scrud-etag': '', disabled: ''}, actions) : null;
  const propose = el('button', rendered ? rendered.propose : {type: 'button',
    'class': 'studio-propose',
    'hx-post': '/workbench/ui-pushes/', 'hx-ext': 'scrud-json', 'hx-swap': 'none',
    'data-scrud-json': 'fixed', 'data-scrud-body': body('proposed'),
    'data-scrud-etag': ''}, actions);
  return {box, opener, panel, review, reason, note, after, apply: applyButton, propose};
}
const toMain = promoteBox('main', 1, {rendered: opts.promote || null});
const toShop = promoteBox('shop & co', 2, {apply: false});
const ajaxBefore = htmx.ajax;
htmx.ajax = function (verb, path, context) {
  const review = context.target;
  if (!review.hasAttribute('data-studio-promote-review')) {
    return ajaxBefore(verb, path, context);
  }
  out.reviews.push({verb, path,
    panel: review.closest('[data-studio-promote]').getAttribute('data-studio-promote'),
    source: context.source === review, swap: context.swap, headers: context.headers});
};
// The review's answer, as htmx gives it: a 2xx (or a 400, which scrud-ui.js
// swaps) is swapped in; then the request ends.
function answer(p, {status = 200, conflicts = '0', unreadable = '0', preview = true,
                    text = ''} = {}) {
  const swapped = status < 300 || status === 400;
  if (swapped) {
    while (p.review.firstChild) p.review.removeChild(p.review.firstChild);
    const frame = el('div', {'class': 'scrud-resource'}, p.review);
    if (preview) {
      el('div', {'class': 'studio-push-preview', 'data-studio-push-preview': '',
        'data-studio-conflicts': conflicts,
        'data-studio-unreadable-conflicts': unreadable}, frame);
    }
    fire(p.review, 'htmx:afterSwap', {target: p.review});
  }
  fire(p.review, 'htmx:afterRequest', {elt: p.review, successful: status < 300,
    requestConfig: {verb: 'get'}, xhr: {status, responseText: text,
    getResponseHeader: () => 'application/problem+json'}});
}
const shown = (node) => [node.hasAttribute('hidden'), node.textContent];
vm.runInContext(process.argv[5], context);
"""


def promote_scenario(script, **opts):
    """Run studio.js, rules.js and promote.js on the stub workbench with the
    strip's promote panels (`PROMOTE_STUB`), then `script`; return what it
    printed (JSON)."""
    require_node()
    done = subprocess.run(
        [
            "node",
            "-e",
            WORKBENCH + PROMOTE_STUB + script + "\nconsole.log(JSON.stringify(out));",
            layer_source(),
            json.dumps(opts),
            layer_source("rules.json"),
            SCRUD_UI_JS.read_text(),
            layer_source("promote.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


REVIEW_SRC = "/workbench/ui-push-previews/alice-wip/"
APPLY_LOADING = "Apply waits for the review to load."


def review_fetch(target, query):
    return {
        "verb": "GET",
        "path": f"{REVIEW_SRC}?target={query}",
        "panel": target,
        "source": True,
        "swap": "innerHTML",
        "headers": {"Scrud-UI-Scope": "studio studio-push"},
    }


def test_promote_panel_loads_on_demand():
    out = promote_scenario(
        r"""
out.load = [out.reviews.slice(), shown(toMain.panel)[0], shown(toShop.panel)[0]];
click(toMain.opener);
out.opened = [shown(toMain.panel)[0], toMain.opener.getAttribute('aria-expanded'),
  out.reviews.slice(), toMain.apply.hasAttribute('disabled'), shown(toMain.reason),
  shown(toShop.panel)[0]];
answer(toMain);
out.answered = [toMain.apply.hasAttribute('disabled'), shown(toMain.reason)];
click(toMain.opener);
out.closed = [shown(toMain.panel)[0], toMain.opener.getAttribute('aria-expanded'),
  out.reviews.length];
click(toMain.opener);
out.again = [out.reviews.length, toMain.apply.hasAttribute('disabled'),
  shown(toMain.reason)];
click(toShop.opener);
out.shop = [out.reviews[out.reviews.length - 1], shown(toShop.panel)[0]];
answer(toShop);
out.shopAnswered = shown(toShop.reason);
""",
        search=BASE,
    )
    # Nothing is loaded until a panel opens.
    assert out["load"] == [[], True, True]
    shown, expanded, reviews, disabled, reason, shop_hidden = out["opened"]
    assert shown is False and expanded == "true" and shop_hidden is True
    assert reviews == [review_fetch("main", "main")]
    # Apply waits for the review.
    assert disabled is True and reason == [False, APPLY_LOADING]
    assert out["answered"] == [False, [True, ""]]
    # Closing hides it and loads nothing; opening it again loads it again.
    assert out["closed"] == [True, "false", 1]
    assert out["again"] == [2, True, [False, APPLY_LOADING]]
    # The target is URL-encoded.
    assert out["shop"] == [review_fetch("shop & co", "shop%20%26%20co"), False]
    # A panel without Apply has nothing to wait for.
    assert out["shopAnswered"] == [True, ""]


def test_apply_disabled_with_conflicts_in_client():
    out = promote_scenario(
        r"""
const cases = [
  {conflicts: '1'},
  {unreadable: '2'},
  {conflicts: '1', unreadable: '1'},
  {conflicts: 'x'},
  {preview: false},
  {status: 404, text: '{"title": "Not Found", "detail": "No such branch."}'},
  {status: 400, preview: false,
   text: '{"title": "Invalid branch", "detail": "main has no parent."}'},
  {},
];
click(toMain.opener);
out.cases = cases.map((c) => {
  click(toMain.opener);
  click(toMain.opener);
  answer(toMain, c);
  return [toMain.apply.hasAttribute('disabled'), shown(toMain.reason),
    toMain.propose.hasAttribute('disabled')];
});
// A refused apply: the server's text in the workbench's notice.
fire(toMain.apply, 'htmx:afterRequest', {elt: toMain.apply, successful: false,
  requestConfig: {verb: 'post'}, xhr: {status: 403,
  responseText: '{"title": "Forbidden", "detail": "You may not write row in main."}',
  getResponseHeader: () => 'application/problem+json'}});
out.refused = [notice.textContent, notice.hasAttribute('hidden')];
""",
        search=BASE,
    )
    refused_for = (
        "Apply is off: this push has {}, and a push with any conflict is"
        " refused. Pull, resolve them, and push again."
    )
    unloaded = "Apply is off: the review could not be loaded."
    assert out["cases"] == [
        [True, [False, refused_for.format("1 conflict")], False],
        [
            True,
            [False, refused_for.format("2 conflicts on items you can't read")],
            False,
        ],
        [
            True,
            [False, refused_for.format("2 conflicts (1 on items you can't read)")],
            False,
        ],
        [True, [False, unloaded], False],
        [True, [False, unloaded], False],
        [
            True,
            [False, unloaded + " Refused (HTTP 404): Not Found: No such branch."],
            False,
        ],
        [
            True,
            [
                False,
                unloaded + " Refused (HTTP 400): Invalid branch: main has no parent.",
            ],
            False,
        ],
        [False, [True, ""], False],
    ]
    assert out["refused"] == [
        "Refused (HTTP 403): Forbidden: You may not write row in main.",
        False,
    ]


def test_after_push_preview_request_shape():
    out = promote_scenario(
        r"""
reset();
click(toMain.opener);
click(toMain.after);
out.after = [out.submits.map((s) => s.request), shown(toMain.note)];
reset();
out.direct = S.submitPreview({after_push: {source: 'alice-wip'}, branch: 'shop',
  drafts: [{kind: 'css', content: {name: 'x'}}]});
out.directSent = out.submits.map((s) => s.request);
reset();
S.submitPreview({drafts: [{kind: 'css', content: {name: 'x'}}]});
out.plain = out.submits.map((s) => s.request);
""",
        search=BASE + "&type=todo-lists&mode=edit&pinned=1",
    )
    after, note = out["after"]
    # The target as the branch, T as the source; never drafts (the server
    # refuses after_push with drafts), whatever `pinned` says.
    assert after == [
        preview_request(
            branch="main", mode="edit", after_push={"source": "alice-wip"}, drafts=[]
        )
    ]
    assert note == [False, "The preview shows main after this push."]
    assert out["direct"] is True
    assert out["directSent"] == [
        preview_request(branch="shop", mode="edit", after_push={"source": "alice-wip"})
    ]
    # Without `after_push`, the preview is T's as before.
    assert out["plain"] == [
        preview_request(
            branch="alice-wip@pinned",
            mode="edit",
            drafts=[{"kind": "css", "content": {"name": "x"}}],
        )
    ]
    # Without a type there is nothing to preview: the panel says so.
    out = promote_scenario(
        r"""
click(toShop.opener);
click(toShop.after);
out.none = [out.submits.length, shown(toShop.note)];
""",
        search=BASE,
    )
    assert out["none"] == [
        0,
        [False, "Choose a type to preview shop & co after this push."],
    ]


# ---- branch settings (studio:branch) -------------------------------------


def test_branch_js_inert_without_workbench():
    require_node()
    for push in (False, True):
        out = run(with_workbench=False, file="branch.json", push=push)
        assert out["queries"] == ["[data-studio-workbench]"]
        assert out["touched"] == ["document.querySelector"]
        assert out["listeners"] == [] and out["fetches"] == 0
        assert out["attributes"] == {}


# The workbench's branch settings pane as studio:workbench embeds it (T's
# own frame in the scope studio studio-branch, a form in edit mode, whose
# htmx saves on change and on submit: recorded in `out.branchFrame`), the
# strip's data-studio-published and data-studio-may-publish, a recorded
# location.reload(); then branch.js (argv[3]) after studio.js.
BRANCH_STUB = r"""
status.setAttribute('data-studio-published', opts.published ? 'true' : 'false');
status.setAttribute('data-studio-may-publish', opts.may_publish ? 'true' : 'false');
const branchPane = el('div', {'class': 'studio-branch-body', 'data-studio-embed': 'branch',
  'data-studio-src': '/workbench/ui-branches/9/',
  'data-studio-scope': 'studio studio-branch',
  ...(opts.published ? {'data-studio-mode': 'view'} : {})}, workbench);
out.branchFrame = [];
let branchFrame;
let labelInput;
let baseSelect;
let prefixInput;
let deleteButton;
function renderSettings() {
  while (branchPane.firstChild) branchPane.removeChild(branchPane.firstChild);
  branchFrame = el('form', {'class': 'scrud-resource',
    'data-scrud-href': '/workbench/ui-branches/9/',
    'data-scrud-scope': 'studio studio-branch', 'hx-put': '/workbench/ui-branches/9/',
    'hx-trigger': 'change, submit'}, branchPane);
  branchFrame.addEventListener('change', (e) => out.branchFrame.push(
    'change:' + e.target.getAttribute('name')));
  branchFrame.addEventListener('submit', () => out.branchFrame.push('submit'));
  const settings = el('div', {'class': 'studio-branch-settings',
    'data-studio-branch-settings': '', 'data-studio-mode': 'edit'}, branchFrame);
  labelInput = el('input', {type: 'text', name: '/label'}, settings);
  baseSelect = el('select', {name: '/base'}, settings);
  prefixInput = el('input', {type: 'text', name: '/prefix'}, settings);
  deleteButton = el('button', {type: 'button', 'hx-delete': '/workbench/ui-branches/9/'},
    branchFrame);
}
renderSettings();
const newBranch = el('form', {'class': 'studio-new-branch', 'data-studio-new-branch': '',
  'hx-post': '/workbench/ui-branches/', 'data-scrud-json': 'form'}, workbench);
out.newBranch = [];
newBranch.addEventListener('change', (e) => out.newBranch.push(e.target.getAttribute('name')));
const newName = el('input', {type: 'text', name: '/name'}, newBranch);
out.reloads = 0;
location.reload = () => { out.reloads += 1; };
vm.runInContext(process.argv[3], context);
const branchLoads = () => out.ajax.filter((a) => a.target === 'branch');
const ok = (elt, verb) => fire(elt, 'htmx:afterRequest', {
  elt, successful: true, requestConfig: {verb}, xhr: {status: 200}});
"""


def branch_scenario(script, **opts):
    """Run studio.js and branch.js on the stub workbench with the branch
    settings pane (`BRANCH_STUB`) and the page's ScrudUI (its pure helpers),
    then `script`; return what it printed."""
    require_node()
    done = subprocess.run(
        [
            "node",
            "-e",
            WORKBENCH + BRANCH_STUB + script + "\nconsole.log(JSON.stringify(out));",
            layer_source(),
            json.dumps(opts),
            layer_source("branch.json"),
            SCRUD_UI_JS.read_text(),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


BRANCH_SRC = "/workbench/ui-branches/9/"


def branch_load(scope, view=False):
    headers = {"Scrud-UI-Scope": scope}
    if view:
        headers["Scrud-UI-Mode"] = "view"
    return {
        "verb": "GET",
        "path": BRANCH_SRC,
        "target": "branch",
        "source": True,
        "swap": "innerHTML",
        "headers": headers,
    }


def test_branch_settings_save_only_on_save():
    """A change of a settings field is no save (a prefix publishes URLs):
    Save, the frame's submit, is. It holds for the frame a refused PUT
    re-renders, and leaves New branch's form alone."""
    out = branch_scenario(
        r"""
fire(labelInput, 'change');
fire(baseSelect, 'change');
fire(prefixInput, 'change');
fire(branchFrame, 'submit');
renderSettings();
fire(prefixInput, 'change');
fire(branchFrame, 'submit');
fire(newName, 'change');
"""
    )
    assert out["branchFrame"] == ["submit", "submit"]
    assert out["newBranch"] == ["/name"]


def test_branch_pane_scope_follows_may_publish():
    # Not a publisher, working T: the embedded settings stay as they are.
    out = branch_scenario("out.start = branchLoads();")
    assert out["start"] == []
    # A publisher: the settings are fetched again in the publisher's scope,
    # where they have the prefix input; the pane keeps that scope.
    out = branch_scenario(
        "out.start = branchLoads();"
        "out.scope = branchPane.getAttribute('data-studio-scope');",
        may_publish=True,
    )
    assert out["start"] == [branch_load("studio studio-branch studio-publish")]
    assert out["scope"] == "studio studio-branch studio-publish"
    # A published T is embedded in view mode: a non-publisher keeps it, a
    # publisher gets it in their scope in their own mode.
    out = branch_scenario("out.start = branchLoads();", published=True)
    assert out["start"] == []
    out = branch_scenario(
        "out.start = branchLoads();"
        "out.mode = branchPane.getAttribute('data-studio-mode');",
        published=True,
        may_publish=True,
    )
    assert out["start"] == [branch_load("studio studio-branch studio-publish")]
    assert out["mode"] is None
    # The strip, fetched again, says the grant is gone: back to the plain
    # scope (and, on a published T, view mode).
    out = branch_scenario(
        r"""
reset();
status.setAttribute('data-studio-may-publish', 'false');
fire(strip, 'htmx:afterSwap', {target: strip});
out.after = branchLoads();
out.mode = branchPane.getAttribute('data-studio-mode');
""",
        published=True,
        may_publish=True,
    )
    assert out["after"] == [branch_load("studio studio-branch", view=True)]
    assert out["mode"] == "view"


def test_branch_pane_refreshed_after_strip_write():
    """A write from the strip (a pull, a push) changes T's row: its settings
    are fetched again, with the new ETag. Other writes leave them (and what
    the user typed in them) alone."""
    out = branch_scenario(
        r"""
reset();
ok(pull, 'put');
out.pull = branchLoads(); reset();
ok(override, 'post');
ok(branchFrame, 'put');
fire(pull, 'htmx:afterRequest', {elt: pull, successful: false,
  requestConfig: {verb: 'put'}, xhr: {status: 409, responseText: ''}});
ok(pull, 'get');
out.others = branchLoads();
"""
    )
    assert out["pull"] == [branch_load("studio studio-branch")]
    assert out["others"] == []


def test_workbench_reloads_when_T_published_or_deleted():
    """T published, or no longer, by its settings (the strip says so once
    fetched again), or deleted from them: the workbench reloads, since
    what it offers depends on it."""
    out = branch_scenario(
        r"""
out.start = out.reloads;
status.setAttribute('data-studio-published', 'true');
fire(strip, 'htmx:afterSwap', {target: strip});
out.published = out.reloads;
"""
    )
    assert out["start"] == 0 and out["published"] == 1
    # scrud-ui.js says a frame's resource was deleted with scrud:deleted
    # (after it removed the frame): only T's own reloads.
    out = branch_scenario(
        r"""
const deleted = (href) => (docListeners['scrud:deleted'] || []).forEach(
  (fn) => fn({type: 'scrud:deleted', detail: {href}}));
deleted('/workbench/ui-components/8/');
deleted(null);
out.other = out.reloads;
deleted('/workbench/ui-branches/9/');
out.deleted = out.reloads;
"""
    )
    assert out["other"] == 0 and out["deleted"] == 1


# T's settings as stored (the frame's scrud-content), and its inputs showing
# them: nothing typed.
SETTINGS = r"""
const showSettings = () => {
  el('script', {type: 'application/json', 'class': 'scrud-content'}, branchFrame)
    .text = JSON.stringify({name: 'alice-wip', label: 'Mine', base: 'live',
      prefix: null, parent: 'main', rbac_context: 'alice'});
  labelInput.value = 'Mine';
  baseSelect.value = 'live';
  prefixInput.value = '';
};
showSettings();
const noticeShown = () => [notice.hasAttribute('hidden'), notice.textContent,
  notice.children.map((c) => [c.tagName.toLowerCase(), c.text, c.attrs.type || null])];
"""
RELOAD_NOTICE = (
    "This branch was {}: reload the workbench for what it offers now."
    " Save your unsaved changes first, or they are lost."
)


def test_published_change_waits_for_unsaved_work():
    """T published (or no longer) while the editor, the settings or the new
    rule form hold unsaved input: no reload under it, but the workbench's
    notice says so, with a Reload button. Nothing typed: it reloads."""
    published = r"""
status.setAttribute('data-studio-published', PUBLISHED);
fire(strip, 'htmx:afterSwap', {target: strip});
"""
    # The editor's code, typed and not saved.
    out = branch_scenario(
        SETTINGS
        + r"""
click(openRow);
const {code} = swapEditor();
code.value = '<b>typed</b>';
CHANGE
out.waiting = [out.reloads, noticeShown()];
click(notice.children[0]);
out.reloaded = out.reloads;
""".replace("CHANGE", published.replace("PUBLISHED", "'true'"))
    )
    assert out["waiting"] == [
        0,
        [False, RELOAD_NOTICE.format("published"), [["button", "Reload", "button"]]],
    ]
    assert out["reloaded"] == 1
    # The settings, typed and not saved; on a published T, unpublished.
    out = branch_scenario(
        SETTINGS
        + r"""
labelInput.value = 'Typed';
CHANGE
out.waiting = [out.reloads, noticeShown()];
""".replace("CHANGE", published.replace("PUBLISHED", "'false'")),
        published=True,
    )
    assert out["waiting"] == [
        0,
        [False, RELOAD_NOTICE.format("unpublished"), [["button", "Reload", "button"]]],
    ]
    # Another layer script's unsaved input (the inspector's new rule form).
    out = branch_scenario(
        SETTINGS
        + r"""
S.watchUnsaved(() => true);
CHANGE
out.waiting = out.reloads;
""".replace("CHANGE", published.replace("PUBLISHED", "'true'"))
    )
    assert out["waiting"] == 0
    # The editor and the settings show what is stored: it reloads.
    out = branch_scenario(
        SETTINGS
        + r"""
click(openRow);
swapEditor();
CHANGE
out.reloaded = [out.reloads, notice.hasAttribute('hidden')];
""".replace("CHANGE", published.replace("PUBLISHED", "'true'"))
    )
    assert out["reloaded"] == [1, True]


def test_settings_not_refetched_over_typed_values():
    """A write from the strip changes T's row, but the settings hold input
    not saved: they are not fetched again under it. The notice says their
    Save is now stale, with a button that fetches them again."""
    out = branch_scenario(
        SETTINGS
        + r"""
reset();
prefixInput.value = '/typed/';
ok(pull, 'put');
out.kept = [branchLoads(), prefixInput.value, noticeShown()];
click(notice.children[0]);
out.reloaded = [branchLoads(), notice.hasAttribute('hidden')];
""",
        may_publish=False,
    )
    loads, prefix, shown = out["kept"]
    assert loads == [] and prefix == "/typed/"
    assert shown == [
        False,
        "This branch changed since its settings were loaded: they keep what"
        " you typed, but their Save is refused until they are loaded again.",
        [["button", "Reload settings", "button"]],
    ]
    assert out["reloaded"] == [[branch_load("studio studio-branch")], True]
    # Nothing typed: fetched again at once, as before.
    out = branch_scenario(
        SETTINGS
        + r"""
reset();
ok(pull, 'put');
out.loads = [branchLoads(), notice.hasAttribute('hidden')];
"""
    )
    assert out["loads"] == [[branch_load("studio studio-branch")], True]


def test_settings_not_refetched_over_typed_values_on_grant_change():
    """The publish grant changing in the strip changes the scope the settings
    are wanted in, but they hold input not saved: they are not fetched again
    under it. The same notice offers the button that fetches them. Nothing
    typed: fetched again at once."""
    flip = r"""
reset();
status.setAttribute('data-studio-may-publish', 'true');
fire(strip, 'htmx:afterSwap', {target: strip});
"""
    out = branch_scenario(
        SETTINGS
        + r"""
labelInput.value = 'Typed';
"""
        + flip
        + r"""
out.kept = [branchLoads(), labelInput.value, noticeShown()];
click(notice.children[0]);
out.reloaded = [branchLoads(), notice.hasAttribute('hidden')];
"""
    )
    loads, label, shown = out["kept"]
    assert loads == [] and label == "Typed"
    assert shown == [
        False,
        "This branch changed since its settings were loaded: they keep what"
        " you typed, but their Save is refused until they are loaded again.",
        [["button", "Reload settings", "button"]],
    ]
    assert out["reloaded"] == [
        [branch_load("studio studio-branch studio-publish")],
        True,
    ]
    # Nothing typed: fetched again at once, in the new scope.
    out = branch_scenario(
        SETTINGS
        + flip
        + r"""
out.loads = [branchLoads(), notice.hasAttribute('hidden')];
"""
    )
    assert out["loads"] == [
        [branch_load("studio studio-branch studio-publish")],
        True,
    ]


PUBLISHED_DELETE = (
    "Delete the published branch alice-wip? It is published at /shop/:"
    " deleting it takes away the site's URLs under that prefix."
)


def test_published_branch_delete_says_it_takes_its_urls():
    """Deleting a published branch from its settings takes its prefix's URLs
    away: the frame's Delete asks with that, not the generic "Delete?". A
    working branch keeps the runtime's confirmation."""
    script = r"""
const fill = (content) => {
  el('script', {type: 'application/json', 'class': 'scrud-content'}, branchFrame)
    .text = JSON.stringify(content);
};
out.start = deleteButton.getAttribute('hx-confirm');
renderSettings();
fill({name: 'alice-wip', prefix: '/shop/'});
fire(branchPane, 'htmx:afterSwap', {target: branchPane});
out.swapped = [deleteButton.getAttribute('hx-confirm'), deleteButton.text];
// A delete of something else in the pane is left alone.
const other = el('button', {type: 'button', 'hx-delete': '/workbench/ui-components/8/',
  'hx-confirm': 'Delete?'}, branchFrame);
fire(branchPane, 'htmx:afterSwap', {target: branchPane});
out.other = other.getAttribute('hx-confirm');
"""
    out = branch_scenario(script, published=True, may_publish=True)
    # Without its content the prefix is not named.
    assert out["start"] == (
        "Delete the published branch alice-wip? Deleting it takes away the"
        " site's URLs under its prefix."
    )
    assert out["swapped"] == [PUBLISHED_DELETE, ""]
    assert out["other"] == "Delete?"
    # A working branch: the runtime's confirmation stays.
    out = branch_scenario(
        script.replace("prefix: '/shop/'", "prefix: null"), may_publish=True
    )
    assert out["start"] is None
    assert out["swapped"] == [None, ""]


# argv[1]: scrud-ui.js; stdin: the fields jsonFormFields reads ({name, type,
# value, checked}). Prints the body buildBody builds onto {}, as a
# data-scrud-json="form" element sends it.
FORM_BODY_HARNESS = r"""
const vm = require('vm');
const ui = vm.createContext({window: {}, console});
vm.runInContext(process.argv[1], ui);
let input = '';
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  const bad = [];
  const body = ui.window.ScrudUI.buildBody({}, JSON.parse(input), bad);
  console.log(JSON.stringify({body, bad}));
});
"""


@pytest.mark.django_db
@test_context
def test_new_branch_form_body_from_main(studio_layer, admin_login, settings):
    """The New branch form on main's workbench (context ""), through the real
    buildBody: the empty context is sent (the schema requires it), and the
    branch is created."""
    from .test_studio_rollback_presentations import form_inputs
    from .test_studio_workbench import Page, workbench_url

    settings.ROOT_URLCONF = "tests.mounted_urls"
    main = make_branch("main", "", parent=None)
    Given.http.force_authenticate(admin_login)
    When.http.get(workbench_url(main), HTTP_ACCEPT="text/html")
    Then.http.status_code_is(200)
    page = Page(Http.response.content.decode())
    [form] = page.having("data-studio-new-branch")
    fields = [
        {
            "name": name,
            "type": kind,
            "value": "feature" if name == "/name" else value,
            "checked": False,
        }
        for name, (kind, value) in form_inputs(page, form).items()
    ]
    require_node()
    done = subprocess.run(
        ["node", "-e", FORM_BODY_HARNESS, SCRUD_UI_JS.read_text()],
        capture_output=True,
        text=True,
        input=json.dumps(fields),
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["bad"] == []
    assert out["body"] == {"name": "feature", "parent": "main", "rbac_context": ""}
    When.http.post(
        form.attrs["hx-post"],
        data=json.dumps(out["body"]),
        content_type="application/json",
        HTTP_ACCEPT="application/json",
    )
    Then.http.status_code_is(201)
    assert Http.response.json()["rbac_context"] == ""


# ---- bodies from the server's markup, through the API ---------------------


@pytest.mark.django_db
@test_context
def test_override_copy_sends_the_rendered_body(studio_layer, settings):
    """The editor header's "Override on this branch" is a copy of the
    tree's button as the server renders it: the copy keeps its URL and body
    byte for byte, and sent as scrud-json sends it, the API makes T's own
    row of the item."""
    from django.contrib.auth import get_user_model

    from .test_studio_promote import press
    from .test_studio_workbench import (
        STUDIO_ROOT,
        Element,
        Page,
        get_page,
        load_seed,
        run_seed,
        seeded_branch,
        workbench_url,
    )

    settings.ROOT_URLCONF = "tests.mounted_urls"
    run_seed(load_seed())
    alice = seeded_branch("alice-wip")
    Given.http.force_authenticate(get_user_model().objects.get(username="author"))
    page = Page(get_page(workbench_url(alice, STUDIO_ROOT)))
    classes, _row, li = page.entries()[("component", "todo-row")]
    assert "studio-own" not in classes
    [button] = [e for e in page.writes() if e.within(li)]
    out = scenario(
        r"""
click(label);
out.copies = editorHead.children.filter((c) => c.tagName === 'BUTTON')
  .map((c) => c.attrs);
""",
        search=BASE,
        override=button.attrs,
    )
    [copy] = out["copies"]
    assert copy == button.attrs
    press(Element("button", copy, None), 201)
    page = Page(get_page(workbench_url(alice, STUDIO_ROOT)))
    classes, own, _li = page.entries()[("component", "todo-row")]
    assert "studio-own" in classes and own["override"] is None


@pytest.mark.django_db
@test_context
def test_promote_sends_the_rendered_bodies(studio_layer, admin_login, settings):
    """The strip's promote panel as the server renders it: promote.js loads
    the review from the box's own source, turns Apply on for the review the
    server answers (no conflict), and Propose and Apply, sent as scrud-json
    sends them, are pushes the API makes."""
    from .test_studio_promote import (
        box_button,
        get_page,
        press,
        promote_boxes,
        wip_with_changes,
    )
    from .test_studio_workbench import Element, Page, workbench_url
    from .test_ui_branch_push import created

    settings.ROOT_URLCONF = "tests.mounted_urls"
    main_row, _override, wip = wip_with_changes(admin_login)
    page = get_page(workbench_url(wip))
    box = promote_boxes(page)["main"]
    rendered = {
        "src": box.attrs["data-studio-promote-src"],
        "apply": box_button(page, box, "studio-apply").attrs,
        "propose": box_button(page, box, "studio-propose").attrs,
    }
    # The review, fetched as promote.js asks for it.
    out = promote_scenario(
        "click(toMain.opener); out.review = out.reviews[0];",
        search=BASE,
        promote=rendered,
    )
    review = out["review"]
    assert review["verb"] == "GET" and review["source"] is True
    When.http.get(
        review["path"],
        HTTP_ACCEPT="text/html",
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE=review["headers"]["Scrud-UI-Scope"],
    )
    Then.http.status_code_is(200)
    [found] = Page(Http.response.content.decode()).having("data-studio-push-preview")
    answered = {
        "conflicts": found.attrs["data-studio-conflicts"],
        "unreadable": found.attrs["data-studio-unreadable-conflicts"],
    }
    assert answered == {"conflicts": "0", "unreadable": "0"}
    out = promote_scenario(
        r"""
click(toMain.opener);
answer(toMain, ANSWERED);
out.buttons = [toMain.propose.attrs, toMain.apply.attrs, shown(toMain.reason)];
""".replace("ANSWERED", json.dumps(answered)),
        search=BASE,
        promote=rendered,
    )
    propose, apply, reason = out["buttons"]
    assert propose == rendered["propose"]
    assert "disabled" not in apply and reason == [True, ""]
    assert apply == {k: v for k, v in rendered["apply"].items() if k != "disabled"}
    press(Element("button", propose, None), 201)
    assert created().content["state"] == "proposed"
    press(Element("button", apply, None), 201)
    assert created().content["state"] == "applied"
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>wip</i>"


@pytest.mark.django_db
@test_context
def test_new_rule_sends_what_the_server_offers(admin_login, ui_alice_login, settings):
    """New rule for a node, end to end: the inspector asks the options at the
    strip's own endpoints, builds its form from the server's answer, and the
    body its Create posts makes the rule (201), which then wins for the
    node."""
    from .test_studio_new_component import explain
    from .test_ui_branch_push import make_wip

    settings.ROOT_URLCONF = "tests.mounted_urls"
    reverse("todo-lists-list")
    make_wip(admin_login, name="alice-wip")
    grant(ui_alice_login, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(ui_alice_login)
    When.http.get(
        reverse("ui-branch-status-detail", args=["alice-wip"]),
        HTTP_ACCEPT="application/json",
    )
    Then.http.status_code_is(200)
    endpoints = Http.response.json()["endpoints"]
    sample = endpoints["sample"].replace("{slug}", "todo-lists")
    select = r"""
select('/items/0', SAMPLE);
await respond(200, EXPLAINED);
click(button('New rule for this node'));
""".replace("SAMPLE", json.dumps(sample)).replace("EXPLAINED", json.dumps(EXPLAINED))
    out = inspector_scenario(
        select + "out.asked = fetched()[1];",
        search=BASE + "&type=todo-lists",
        endpoints=endpoints,
    )
    When.http.get(out["asked"], HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    offered = Http.response.json()
    out = inspector_scenario(
        select
        + r"""
await respond(200, OFFERED);
const field = (name) => all(newRule, '[data-studio-new-field="' + name + '"]')[0];
field('template').value = '<em>{{ value.text }}</em>';
click(button('Create'));
out.post = out.fetches[out.fetches.length - 1];
""".replace("OFFERED", json.dumps(offered)),
        search=BASE + "&type=todo-lists",
        endpoints=endpoints,
    )
    post = out["post"]
    assert post["url"] == endpoints["new_component"]
    assert post["body"]["match"] == offered["matches"][0]["select"]
    When.http.post(
        post["url"],
        data=json.dumps(post["body"]),
        content_type="application/json",
        HTTP_ACCEPT="application/json",
    )
    Then.http.status_code_is(201)
    made = Http.response.json()
    assert made["wins"] is True
    assert {"kind": "component", "name": offered["name"]} in made["written"]
    explained = explain(branch="alice-wip", pointer="/items/0")
    assert explained["rules"][explained["winner"]]["component"] == offered["name"]


# argv[1]: scrud-ui.js; argv[2]: the frame's content (JSON); stdin: its
# inputs ({name, type, value}). Prints the body Save sends (buildBody onto
# the content) and whether the frame counts as changed.
SAVE_BODY_HARNESS = r"""
const vm = require('vm');
const ui = vm.createContext({window: {}, console});
vm.runInContext(process.argv[1], ui);
const content = JSON.parse(process.argv[2]);
let input = '';
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  const bad = [];
  const body = ui.window.ScrudUI.buildBody(content, JSON.parse(input), bad);
  console.log(JSON.stringify({body, bad,
    changed: JSON.stringify(body) !== JSON.stringify(content)}));
});
"""


def save_body(content, inputs):
    """The body the frame's Save builds from `content` and `inputs` (the
    rendered ``name``d inputs as ``{name, type, value}``), through the real
    ``ScrudUI.buildBody``."""
    require_node()
    done = subprocess.run(
        ["node", "-e", SAVE_BODY_HARNESS, SCRUD_UI_JS.read_text(), json.dumps(content)],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.django_db
@test_context
def test_order_shows_its_default_and_saves_nothing_untouched(
    studio_layer, ui_alice_login, settings
):
    """A stylesheet stored without ``order`` (the demo's) showed 0 in view
    mode but an empty Order input in edit mode. The input shows the default,
    0, as its placeholder (the schema's ``default``): its value stays empty,
    which the runtime's untouched rule reads as the missing value, so
    opening the editor is no change and Save writes no ``order: 0``; a value
    typed in it is saved."""
    from .test_studio_editors import (
        content_of,
        open_editor,
        root_frame,
        workbench_entries,
    )
    from .test_studio_page import store_item
    from .test_studio_workbench import Page, setup_studio

    settings.ROOT_URLCONF = "tests.mounted_urls"
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    sheet = store_item(
        "ui-stylesheets",
        "alice-wip",
        "alice",
        name="plain",
        applies_to="",
        prefixes={},
        rules=[{"select": "string", "component": "scrud:string"}],
    )
    assert "order" not in sheet.content
    Given.http.force_authenticate(ui_alice_login)
    classes, opened, _li, _page = workbench_entries(alice_wip)[("stylesheet", "plain")]
    assert "studio-own" in classes
    href = opened["href"]
    page = Page(open_editor(href))
    frame = root_frame(page, href)
    stored = content_of(page, frame)
    assert "order" not in stored
    [order] = [e for e in page.elements if e.attrs.get("name") == "/order"]
    assert order.tag == "input" and order.attrs["type"] == "number"
    assert order.attrs.get("value", "") == ""
    assert order.attrs["placeholder"] == "0"
    # View mode shows the same default.
    view = Page(open_editor(href, view=True))
    shown = [e for e in view.elements if e.tag in ("dt", "dd")]
    [at] = [n for n, e in enumerate(shown) if e.tag == "dt" and e.text == "Order"]
    assert shown[at + 1].tag == "dd" and shown[at + 1].text == "0"
    inputs = [
        {
            "name": e.attrs["name"],
            "type": e.attrs.get("type", "text"),
            "value": e.attrs.get("value", ""),
        }
        for e in page.elements
        if e.tag == "input"
        and "name" in e.attrs
        and e.within(frame)
        and e.attrs.get("type") not in ("hidden", "submit")
    ]
    assert "/order" in [i["name"] for i in inputs]
    # Untouched: Save sends the content as stored, and nothing is unsaved.
    untouched = save_body(stored, inputs)
    assert untouched["bad"] == [] and untouched["changed"] is False
    assert "order" not in untouched["body"]
    # A typed order is saved.
    typed = [{**i, "value": "5"} if i["name"] == "/order" else i for i in inputs]
    saved = save_body(stored, typed)
    assert saved["changed"] is True and saved["body"]["order"] == 5
