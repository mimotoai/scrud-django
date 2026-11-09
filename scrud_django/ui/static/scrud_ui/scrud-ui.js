/*
 * scrud-ui.js: the browser side of scrud-django's server-rendered UI.
 *
 * Requires htmx 2.0.4, vendored next to this file as htmx.min.js
 * (https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js):
 *   sha384-HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+
 *
 * The `scrud-json` extension builds the full JSON body of a PUT from the
 * content embedded in the rendered frame (script.scrud-content) and the
 * inputs named by JSON Pointer. The server's write path is unchanged: an
 * ordinary JSON PUT with If-Match.
 *
 * Plain ES2017, no modules, no dependencies besides htmx.
 */
(function () {
  'use strict';

  var FRAME = '.scrud-resource';
  var UNSAFE = ['__proto__', 'constructor', 'prototype'];

  // ---- pure helpers (exposed as window.ScrudUI) -------------------------

  /** Split a JSON Pointer ("" or "/a/b") into unescaped segments. */
  function parsePointer(pointer) {
    if (pointer === '' || pointer == null) return [];
    if (pointer.charAt(0) !== '/') {
      throw new Error('invalid JSON Pointer: ' + pointer);
    }
    return pointer
      .slice(1)
      .split('/')
      .map(function (s) {
        return s.replace(/~1/g, '/').replace(/~0/g, '~');
      });
  }

  function isIndex(segment) {
    return /^(0|[1-9][0-9]*)$/.test(segment);
  }

  function own(container, key) {
    return Object.prototype.hasOwnProperty.call(container, key);
  }

  /**
   * Set `value` at `pointer` in `doc` (mutating it, creating containers on
   * the way). A `-` segment on an array appends. `undefined` removes the
   * key. Returns `doc`.
   */
  function applyPointer(doc, pointer, value) {
    var segs = parsePointer(pointer);
    if (!segs.length) return value === undefined ? doc : value;
    var node = doc;
    for (var i = 0; i < segs.length; i++) {
      var seg = segs[i];
      if (UNSAFE.indexOf(seg) >= 0) return doc;
      var last = i === segs.length - 1;
      var key = Array.isArray(node) && seg === '-' ? node.length : seg;
      if (last) {
        if (value === undefined) {
          if (Array.isArray(node)) {
            if (isIndex(String(key)) && Number(key) < node.length) {
              node.splice(Number(key), 1);
            }
          } else if (own(node, key)) {
            delete node[key];
          }
        } else {
          node[key] = value;
        }
        return doc;
      }
      var child = own(node, key) ? node[key] : undefined;
      if (child === null || typeof child !== 'object') {
        if (value === undefined) return doc;
        child = isIndex(segs[i + 1]) || segs[i + 1] === '-' ? [] : {};
        node[key] = child;
      }
      node = child;
    }
    return doc;
  }

  function isEmpty(field) {
    if (field.type === 'checkbox') return !field.checked;
    return field.value === '' || field.value == null;
  }

  /** The value an input contributes, or undefined to omit/remove the key. */
  function fieldValue(field) {
    if (field.type === 'checkbox') return !!field.checked;
    if (field.type === 'number') {
      if (field.value === '' || field.value == null) return undefined;
      var n = Number(field.value);
      return isNaN(n) ? undefined : n;
    }
    return field.value;
  }

  /**
   * Build the JSON body: a deep copy of `content` with every field applied at
   * its pointer. `fields` are {name, type, value, checked} (DOM inputs fit).
   * Fields sharing a `<prefix>/-` become one new element appended to the
   * array at <prefix>; a group whose fields are all empty is dropped.
   */
  function buildBody(content, fields) {
    var body = JSON.parse(JSON.stringify(content));
    var groups = [];
    var byPrefix = {};
    fields.forEach(function (field) {
      var segs = parsePointer(field.name);
      var dash = segs.indexOf('-');
      if (dash < 0) {
        applyPointer(body, field.name, fieldValue(field));
        return;
      }
      var prefix = field.name.slice(0, field.name.indexOf('/-'));
      var group = byPrefix[prefix];
      if (!group) {
        group = byPrefix[prefix] = { prefix: prefix, fields: [] };
        groups.push(group);
      }
      var rest = field.name.slice(prefix.length + 2);
      group.fields.push({ rest: rest, field: field });
    });
    groups.forEach(function (group) {
      if (group.fields.every(function (g) { return isEmpty(g.field); })) return;
      var element;
      group.fields.forEach(function (g) {
        var value = fieldValue(g.field);
        if (g.rest === '') {
          element = value;
        } else {
          if (element === null || typeof element !== 'object') element = {};
          applyPointer(element, g.rest, value);
        }
      });
      if (element !== undefined) applyPointer(body, group.prefix + '/-', element);
    });
    return body;
  }

  window.ScrudUI = {
    applyPointer: applyPointer,
    buildBody: buildBody,
    parsePointer: parsePointer
  };

  // ---- htmx integration -------------------------------------------------

  var htmx = window.htmx;
  if (!htmx || typeof document === 'undefined') return;

  htmx.config.selfRequestsOnly = true;
  htmx.config.responseHandling = [
    { code: '204', swap: false },
    { code: '2..', swap: true },
    { code: '(400|403|409|412|428)', swap: true, error: true },
    { code: '.*', swap: false, error: true }
  ];

  function frameOf(el) {
    return el && el.closest ? el.closest(FRAME) : null;
  }

  function quoteEtag(etag) {
    return /^(W\/)?"/.test(etag) ? etag : '"' + etag + '"';
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name=csrf-token]');
    return meta ? meta.getAttribute('content') : '';
  }

  function hasHeader(headers, name) {
    var lower = name.toLowerCase();
    return Object.keys(headers).some(function (k) {
      return k.toLowerCase() === lower;
    });
  }

  function setDefault(headers, name, value) {
    if (value != null && !hasHeader(headers, name)) headers[name] = value;
  }

  function isPut(elt) {
    return elt.hasAttribute('hx-put') || elt.hasAttribute('data-hx-put');
  }

  /** The frame's own scrud-content (not a nested frame's). */
  function contentOf(frame) {
    var scripts = frame.querySelectorAll('script.scrud-content');
    for (var i = 0; i < scripts.length; i++) {
      if (frameOf(scripts[i]) === frame) return JSON.parse(scripts[i].textContent);
    }
    return null;
  }

  function fieldsOf(frame) {
    return Array.prototype.filter.call(
      frame.querySelectorAll('[name^="/"]'),
      function (el) {
        if (el.disabled) return false;
        return !(el.type === 'radio' && !el.checked);
      }
    );
  }

  // Innermost frame only: an event inside nested frames is acted on by the
  // frame that encloses its target; outer frames' requests are cancelled.
  document.addEventListener('htmx:confirm', function (evt) {
    var detail = evt.detail || {};
    var elt = detail.elt;
    var triggering = detail.triggeringEvent;
    if (!elt || !elt.matches || !elt.matches(FRAME) || !triggering) return;
    if (frameOf(triggering.target) !== elt) evt.preventDefault();
  });

  document.addEventListener('htmx:configRequest', function (evt) {
    var detail = evt.detail;
    var headers = detail.headers;
    var verb = String(detail.verb).toLowerCase();
    setDefault(headers, 'Accept', 'text/html');
    if (verb === 'get') return;
    setDefault(headers, 'X-CSRFToken', csrfToken());
    var frame = frameOf(detail.elt);
    if (!frame) return;
    var etag = frame.getAttribute('data-scrud-etag');
    if (etag) setDefault(headers, 'If-Match', quoteEtag(etag));
    setDefault(headers, 'Scrud-UI-Scope', frame.getAttribute('data-scrud-scope'));
    setDefault(headers, 'Scrud-UI-Pointer', frame.getAttribute('data-scrud-pointer'));
    if (verb === 'put' && isPut(detail.elt)) {
      headers['Content-Type'] = 'application/json';
    }
  });

  htmx.defineExtension('scrud-json', {
    encodeParameters: function (xhr, parameters, elt) {
      if (!isPut(elt)) return null;
      var frame = frameOf(elt);
      var content = frame && contentOf(frame);
      if (!content) return null;
      return JSON.stringify(buildBody(content, fieldsOf(frame)));
    }
  });
})();
