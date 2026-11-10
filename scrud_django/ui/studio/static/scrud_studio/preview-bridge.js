// The live preview's bridge to the studio page that frames it. The preview
// has an opaque origin (sandbox allow-scripts, no allow-same-origin), so the
// page cannot read its DOM: the preview tells it what it needs with
// postMessage, to the parent window, to any origin ("*": the frame's own is
// opaque, and only a same-origin page may frame it, X-Frame-Options). The
// receiving page checks event.source is its preview frame and accepts these
// shapes only:
//
//   {"type": "scrud-preview:errors", "errors": [{"kind", "name", "messages"}]}
//     once, when loaded: the drafts the preview left out, and why;
//   {"type": "scrud-preview:select", "pointer": "<JSON Pointer>"}
//     on a click inside a node: the nearest scrud-node's pointer (the click's
//     default is prevented, so a link in the preview does not navigate).
//
// The branch's own scripts run in the same document and can post messages
// too: the page treats both as data, never as instructions.
(function () {
  "use strict";
  var parent = window.parent;
  if (!parent || parent === window) {
    return;
  }

  function post(message) {
    parent.postMessage(message, "*");
  }

  function errors() {
    var element = document.getElementById("scrud-preview-errors");
    if (!element) {
      return [];
    }
    try {
      var found = JSON.parse(element.textContent);
      return Array.isArray(found) ? found : [];
    } catch (error) {
      return [];
    }
  }

  document.addEventListener(
    "click",
    function (event) {
      var target = event.target;
      if (target && target.nodeType !== 1) {
        target = target.parentNode;
      }
      var node = target && target.closest ? target.closest("scrud-node") : null;
      if (!node) {
        return;
      }
      event.preventDefault();
      post({
        type: "scrud-preview:select",
        pointer: node.getAttribute("data-pointer") || "",
      });
    },
    true
  );

  function loaded() {
    post({ type: "scrud-preview:errors", errors: errors() });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loaded);
  } else {
    loaded();
  }
})();
