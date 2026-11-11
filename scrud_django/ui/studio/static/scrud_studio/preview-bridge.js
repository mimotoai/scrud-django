// The live preview's bridge to the studio page that frames it. The preview
// has an opaque origin (sandbox allow-scripts, no allow-same-origin), so the
// page cannot read its DOM: the preview tells it what it needs with
// postMessage, to the parent window, to any origin ("*": the frame's own is
// opaque, and only a same-origin page may frame it, X-Frame-Options). The
// receiving page checks event.source is its preview frame and accepts these
// shapes only:
//
//   {"type": "scrud-preview:errors", "errors": [{"kind", "name", "messages"}]}
//     once, when loaded: the drafts the preview left out, and why, as the
//     server listed them (#scrud-preview-errors, read when this script runs,
//     before the branch's own scripts); when that list cannot be read,
//     {"type": "scrud-preview:errors", "errors": null, "failed": true};
//   {"type": "scrud-preview:marks", "wins": <M>, "loses": <L>}
//     once, when loaded, after the errors message: how many nodes the
//     preview marks as the marked rule winning (data-scrud-match="wins")
//     and losing ("loses"), counted when this script runs, before the
//     branch's scripts; 0 and 0 when the request had no mark (it is posted
//     either way);
//   {"type": "scrud-preview:select", "pointer": "<JSON Pointer>",
//    "href": "<data-scrud-href>" | null}
//     on a click inside a node: the nearest scrud-node's pointer, and the
//     data-scrud-href of the resource frame it is in (the nearest
//     .scrud-resource; null outside one), so that a node of an embedded
//     resource, whose pointer is relative to that resource, is told from the
//     previewed resource's (the click's default is prevented, so a link in
//     the preview does not navigate).
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

  // The errors message, read now: the element is before this script, and
  // the branch's scripts, which could change it, run after it.
  function errorsMessage() {
    var element = document.getElementById("scrud-preview-errors");
    var found;
    try {
      found = element ? JSON.parse(element.textContent) : undefined;
    } catch (error) {
      found = undefined;
    }
    if (!Array.isArray(found)) {
      // Not "no errors": the list could not be read.
      return { type: "scrud-preview:errors", errors: null, failed: true };
    }
    return { type: "scrud-preview:errors", errors: found };
  }
  var errors = errorsMessage();

  // The marks message, counted now, for the same reason.
  function marksMessage() {
    return {
      type: "scrud-preview:marks",
      wins: document.querySelectorAll('[data-scrud-match="wins"]').length,
      loses: document.querySelectorAll('[data-scrud-match="loses"]').length,
    };
  }
  var marks = marksMessage();

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
      var frame = node.closest(".scrud-resource");
      post({
        type: "scrud-preview:select",
        pointer: node.getAttribute("data-pointer") || "",
        href: frame ? frame.getAttribute("data-scrud-href") : null,
      });
    },
    true
  );

  function loaded() {
    post(errors);
    post(marks);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loaded);
  } else {
    loaded();
  }
})();
