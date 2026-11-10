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
   * the way). A `-` segment on an array appends. `undefined` removes the key
   * of an object (never an array element) and leaves `doc` as it is for the
   * root pointer. A pointer with an unsafe segment (`__proto__`,
   * `constructor`, `prototype`) is ignored: `doc` is returned unchanged.
   * Throws an Error for an array index that is not a number or is past the
   * end of the array. Returns `doc` (or `value` for the root pointer).
   */
  function applyPointer(doc, pointer, value) {
    var segs = parsePointer(pointer);
    if (!segs.length) return value === undefined ? doc : value;
    var node = doc;
    for (var i = 0; i < segs.length; i++) {
      var seg = segs[i];
      if (UNSAFE.indexOf(seg) >= 0) return doc;
      var last = i === segs.length - 1;
      var key = seg;
      if (Array.isArray(node)) {
        if (seg === '-') {
          key = node.length;
        } else if (!isIndex(seg)) {
          throw new Error('invalid array index "' + seg + '" in ' + pointer);
        } else if (Number(seg) > node.length) {
          throw new Error('array index out of range in ' + pointer);
        } else {
          key = Number(seg);
        }
      }
      if (last) {
        if (value === undefined) {
          // never remove array elements by index; objects lose the key
          if (!Array.isArray(node) && own(node, key)) delete node[key];
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

  function escapeSegment(seg) {
    return seg.replace(/~/g, '~0').replace(/\//g, '~1');
  }

  function joinPointer(segs) {
    return segs.map(function (s) { return '/' + escapeSegment(s); }).join('');
  }

  function warn(field, err) {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('scrud-ui: skipped input ' + field.name + ': ' + err.message);
    }
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

  /** The value at `pointer` in `doc`, or undefined (never throws). */
  function valueAt(doc, pointer) {
    var segs;
    try {
      segs = parsePointer(pointer);
    } catch (err) {
      return undefined;
    }
    var node = doc;
    for (var i = 0; i < segs.length; i++) {
      if (node === null || typeof node !== 'object') return undefined;
      var seg = segs[i];
      if (Array.isArray(node)) {
        if (!isIndex(seg) || Number(seg) >= node.length) return undefined;
        node = node[Number(seg)];
      } else {
        if (!own(node, seg)) return undefined;
        node = node[seg];
      }
    }
    return node;
  }

  var TEXT_LIKE = ['text', 'textarea', 'url', 'date', 'number'];

  /**
   * Whether `field` (a text-like input) still shows the value embedded at its
   * pointer. A text input's value has lost CR and LF (the browser strips
   * them), and a textarea's line breaks are LF: compared accordingly, so that
   * an untouched field leaves the embedded value exactly as it is. An embedded
   * number is compared numerically (`Number(value) === embedded`, so "3.0"
   * matches 3), and an empty input never matches a number. An empty input
   * over `null` or a missing value is untouched (it is how the browser shows
   * them), while an empty input over a string is a change to "". Any other
   * embedded type (boolean, object) is never untouched. Non-text-like fields
   * are never untouched.
   */
  function untouched(field, embedded) {
    if (TEXT_LIKE.indexOf(field.type) < 0) return false;
    var value = field.value == null ? '' : String(field.value);
    // An empty input over null or a missing value is how the browser shows
    // them: leave them as they are rather than writing "".
    if (value === '' && (embedded === null || embedded === undefined)) return true;
    if (typeof embedded === 'number') {
      return value !== '' && Number(value) === embedded;
    }
    if (typeof embedded !== 'string') return false;
    if (field.type === 'textarea') {
      return embedded.replace(/\r\n?/g, '\n') === value.replace(/\r\n?/g, '\n');
    }
    return embedded.replace(/[\r\n]/g, '') === value.replace(/[\r\n]/g, '');
  }

  /**
   * Build the JSON body: a deep copy of `content` with every field applied at
   * its pointer. `fields` are {name, type, value, checked} (DOM inputs fit).
   * A text-like field that still shows the embedded value is skipped
   * (`untouched`). Fields sharing a `<prefix>/-` become one new element
   * appended to the array at <prefix>; a group whose fields are all empty is
   * dropped.
   *
   * Pure: when `errors` (an array) is given, the name of every field that
   * could not be applied is pushed on it: an invalid pointer, an unsafe
   * segment (`__proto__`, `constructor`, `prototype`), or an empty or
   * non-numeric number input over a stored number. The body is still computed
   * from the other fields, and such a field leaves the content as it is; the
   * caller decides what to do with the errors.
   */
  function buildBody(content, fields, errors) {
    var body = JSON.parse(JSON.stringify(content));
    var groups = [];
    var byPrefix = {};
    function fail(field, err) {
      if (err) warn(field, err);
      if (errors) errors.push(field.name);
    }
    fields.forEach(function (field) {
      var segs;
      try {
        segs = parsePointer(field.name);
      } catch (err) {
        fail(field, err);
        return;
      }
      if (segs.some(function (s) { return UNSAFE.indexOf(s) >= 0; })) {
        fail(field, new Error('unsafe pointer segment'));
        return;
      }
      var dash = segs.indexOf('-');
      if (dash < 0) {
        var embedded = valueAt(content, field.name);
        if (untouched(field, embedded)) return;
        var value = fieldValue(field);
        if (value === undefined && field.type === 'number' && typeof embedded === 'number') {
          // cleared or half-typed (the browser reports "1e" as ""): never
          // delete the stored number
          fail(field);
          return;
        }
        try {
          applyPointer(body, field.name, value);
        } catch (err) {
          fail(field, err);
        }
        return;
      }
      var prefix = joinPointer(segs.slice(0, dash));
      var group = byPrefix[prefix];
      if (!group) {
        group = byPrefix[prefix] = { prefix: prefix, fields: [] };
        groups.push(group);
      }
      var rest = joinPointer(segs.slice(dash + 1));
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
          try {
            applyPointer(element, g.rest, value);
          } catch (err) {
            fail(g.field, err);
          }
        }
      });
      if (element !== undefined) {
        try {
          applyPointer(body, group.prefix + '/-', element);
        } catch (err) {
          fail(group.fields[0].field, err);
        }
      }
    });
    return body;
  }

  function frameOf(el) {
    return el && el.closest ? el.closest(FRAME) : null;
  }

  /**
   * The inputs a PUT from `frame` applies: those named by a JSON Pointer whose
   * own frame is of the same resource (the same data-scrud-href: the frame,
   * or a node frame of it), never those of an embedded resource's frame.
   * Disabled inputs and unchecked radios are left out.
   */
  function fieldsOf(frame) {
    var href = frame.getAttribute('data-scrud-href');
    return Array.prototype.filter.call(
      frame.querySelectorAll('[name^="/"]'),
      function (el) {
        if (el.disabled) return false;
        if (el.type === 'radio' && !el.checked) return false;
        var own = frameOf(el);
        return !!own && own.getAttribute('data-scrud-href') === href;
      }
    );
  }

  var NOTICE_CLASS = 'scrud-client-notice';

  /**
   * The notice for a request that failed: `status` is the HTTP status, or
   * falsy for a network failure; `verb` is the request's. A GET is a failed
   * load; a 5xx for a write may come after the change was committed (the
   * response failed to render), so it does not claim the change was lost.
   */
  function noticeText(status, verb) {
    verb = String(verb).toLowerCase();
    var where = status ? ' (HTTP ' + status + ').' : ' (network error).';
    if (verb === 'get') return 'This item could not be loaded' + where;
    if (status >= 500) {
      return (
        'Something went wrong (HTTP ' + status + '). Reload to see the current state.'
      );
    }
    var what =
      verb === 'delete'
        ? 'The item was not deleted'
        : 'Your change was not saved';
    return what + where;
  }

  function invalidFieldsText(names) {
    return (
      'This form could not be saved: invalid field' +
      (names.length === 1 ? ' ' : 's ') +
      names.join(', ') +
      '.'
    );
  }

  /**
   * Whether a failed request needs a client-side notice: not when the server
   * answers with a body that is swapped in. A refused PUT (400, 403) is
   * re-rendered by the server, a 412 or a 409 DELETE is a notice with a frame
   * that reloads the current state; any other failed DELETE is not swapped
   * (see `htmx:beforeSwap`).
   */
  function showsClientNotice(verb, status) {
    if (String(verb).toLowerCase() === 'delete') {
      return !(status === 409 || status === 412);
    }
    return [400, 403, 409, 412, 428].indexOf(status) < 0;
  }

  function clearNotice(frame) {
    Array.prototype.slice.call(frame.children || []).forEach(function (child) {
      if (String(child.className || '').split(' ').indexOf(NOTICE_CLASS) >= 0) {
        frame.removeChild(child);
      }
    });
  }

  /**
   * Insert a notice (the markup of the server's `scrud:notice`) at the top of
   * `frame`, replacing a previous client notice. The text is set as text.
   */
  function showNotice(frame, text) {
    clearNotice(frame);
    var notice = document.createElement('div');
    notice.className = 'scrud-notice scrud-notice-error ' + NOTICE_CLASS;
    notice.setAttribute('role', 'alert');
    notice.textContent = text;
    frame.insertBefore(notice, frame.firstChild);
  }

  window.ScrudUI = {
    applyPointer: applyPointer,
    buildBody: buildBody,
    fieldsOf: fieldsOf,
    invalidFieldsText: invalidFieldsText,
    noticeText: noticeText,
    parsePointer: parsePointer,
    showNotice: showNotice,
    showsClientNotice: showsClientNotice
  };

  // ---- htmx integration -------------------------------------------------

  var htmx = window.htmx;
  if (typeof document === 'undefined') return;
  if (!htmx) {
    console.error('scrud-ui: htmx is not loaded; scrud-ui.js is inactive');
    return;
  }

  htmx.config.selfRequestsOnly = true;
  // htmx 2.0.4 also sends DELETE's form values as URL params, and the Delete
  // button sits inside the root edit form.
  htmx.config.methodsThatUseUrlParams = ['get'];
  htmx.config.responseHandling = [
    { code: '204', swap: false },
    { code: '2..', swap: true },
    { code: '(400|403|409|412|428)', swap: true, error: true },
    { code: '.*', swap: false, error: true }
  ];

  function quoteEtag(etag) {
    return /^(W\/)?"/.test(etag) ? etag : '"' + etag + '"';
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name=csrf-token]');
    return (meta && meta.getAttribute('content')) || null;
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

  // Innermost frame only: an event inside nested frames is acted on by the
  // frame that encloses its target; outer frames' requests are cancelled.
  document.addEventListener('htmx:confirm', function (evt) {
    var detail = evt.detail || {};
    var elt = detail.elt;
    var triggering = detail.triggeringEvent;
    if (!elt || !elt.matches || !elt.matches(FRAME)) return;
    if (!triggering || !triggering.target) return;
    if (frameOf(triggering.target) !== elt) {
      // Expected: the inner frame handles it, so this is debug, not a warning.
      console.debug(
        'scrud-ui: cancelled request of outer frame ' +
          elt.getAttribute('data-scrud-href') +
          '; the event target belongs to an inner frame'
      );
      evt.preventDefault();
    }
  });

  /**
   * The branch the page was rendered on, so that a page never changes branch
   * halfway: the data-scrud-branch of the closest element carrying one (the
   * frame), else the page's (.scrud-branch-page, for elements outside any
   * frame), else null. "" names the published branch.
   */
  function branchOf(elt) {
    var own = elt && elt.closest ? elt.closest('[data-scrud-branch]') : null;
    if (!own) own = document.querySelector('.scrud-branch-page[data-scrud-branch]');
    return own ? own.getAttribute('data-scrud-branch') : null;
  }

  document.addEventListener('htmx:configRequest', function (evt) {
    var detail = evt.detail;
    var headers = detail.headers;
    var verb = String(detail.verb).toLowerCase();
    setDefault(headers, 'Accept', 'text/html');
    setDefault(headers, 'Scrud-UI-Branch', branchOf(detail.elt));
    if (verb === 'get') return;
    setDefault(headers, 'X-CSRFToken', csrfToken());
    var frame = frameOf(detail.elt);
    if (!frame) return;
    if (verb === 'put' && isPut(detail.elt)) {
      // An input that cannot be applied would be silently lost by a PUT that
      // otherwise succeeds: send nothing and say so.
      clearNotice(frame);
      var content = contentOf(frame);
      var invalid = [];
      if (content) buildBody(content, fieldsOf(frame), invalid);
      if (invalid.length) {
        evt.preventDefault();
        showNotice(frame, invalidFieldsText(invalid));
        return;
      }
    }
    var etag = frame.getAttribute('data-scrud-etag');
    if (etag) setDefault(headers, 'If-Match', quoteEtag(etag));
    setDefault(headers, 'Scrud-UI-Scope', frame.getAttribute('data-scrud-scope'));
    setDefault(headers, 'Scrud-UI-Pointer', frame.getAttribute('data-scrud-pointer'));
    if (verb === 'put' && isPut(detail.elt)) {
      headers['Content-Type'] = 'application/json';
    }
  });

  /**
   * After a successful DELETE of a frame's resource (its Delete button), remove
   * the frame from the page (and the list item that held only it), and
   * dispatch `scrud:deleted` on the document with the resource's href. The
   * 204 itself swaps nothing.
   */
  document.addEventListener('htmx:afterRequest', function (evt) {
    var detail = evt.detail || {};
    var config = detail.requestConfig || {};
    if (!detail.successful || String(config.verb).toLowerCase() !== 'delete') {
      return;
    }
    var frame = frameOf(detail.elt);
    if (!frame || !frame.parentNode) return;
    var href = frame.getAttribute('data-scrud-href');
    var path = detail.pathInfo && detail.pathInfo.requestPath;
    if (path && path !== href) return; // something else in the frame was deleted
    var parent = frame.parentNode;
    parent.removeChild(frame);
    if (
      parent.tagName === 'LI' &&
      parent.parentNode &&
      !parent.children.length &&
      !String(parent.textContent || '').trim()
    ) {
      parent.parentNode.removeChild(parent);
    }
    if (typeof CustomEvent === 'function') {
      document.dispatchEvent(
        new CustomEvent('scrud:deleted', { detail: { href: href } })
      );
    }
  });

  // A failed DELETE is never swapped in, except the 409 and 412 that the server
  // answers with a notice and a frame that reloads the current state: swapping
  // a bare notice would make the item look deleted.
  document.addEventListener('htmx:beforeSwap', function (evt) {
    var detail = evt.detail || {};
    var config = detail.requestConfig || {};
    var status = detail.xhr && detail.xhr.status;
    if (String(config.verb).toLowerCase() !== 'delete' || !(status >= 400)) return;
    if (!showsClientNotice('delete', status)) return;
    detail.shouldSwap = false;
    detail.isError = true;
  });

  // A failure nothing is swapped in for (401, 404, 415, 5xx, no network) is
  // shown in the frame that made the request.
  function failed(evt) {
    var detail = evt.detail || {};
    var config = detail.requestConfig || {};
    var status = detail.xhr && detail.xhr.status;
    var verb = String(config.verb).toLowerCase();
    if (verb !== 'get' && status && !showsClientNotice(verb, status)) return;
    var frame = frameOf(detail.elt);
    if (frame) showNotice(frame, noticeText(status, verb));
  }
  document.addEventListener('htmx:responseError', failed);
  document.addEventListener('htmx:sendError', failed);

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
