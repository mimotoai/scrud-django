"""The studio layer's JS item (``studio:studio``) under node. Layer JS is
effective on every branch, so every page loads it: it must do nothing unless
the page has a studio workbench (``[data-studio-workbench]``). The script runs
against a stub ``document`` and ``window`` that record what it touches, with
every other global missing."""

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
});
const context = vm.createContext({
  document,
  window,
  fetch() { out.fetches += 1; return new Promise(() => {}); },
});
vm.runInContext(source, context);
console.log(JSON.stringify(out));
"""


def run(with_workbench):
    layer_js = importlib.resources.files(LAYER_PACKAGE).joinpath("js/studio.json")
    source = json.loads(layer_js.read_text(encoding="utf-8"))["js"]
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
