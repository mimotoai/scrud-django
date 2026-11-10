// The studio's behaviour. On every page it sends the CSRF token with htmx
// requests. On a type's page (#studio) it inspects the node clicked in the
// preview, fills the inspector, the editor and the notices, and makes the
// studio's writes.
//
// Rules it keeps:
// - Everything the server sends is shown as text, through textContent, form
//   values and DOM nodes it makes itself; never as HTML.
// - The preview iframe is sandboxed without allow-scripts, so a branch's
//   scripts never run in it. Its document is same-origin, and this script
//   listens for clicks there.
// - Every write is a JSON POST with the page's CSRF token. A save of the
//   branch's own item sends the ETag the editor read as If-Match. So does a
//   revert (ruling S10).
// - A successful write carries HX-Trigger: scrud-studio-saved, and this script
//   dispatches that event on <body>. The preview, the panel and (through htmx)
//   the branch strip then reload.
(function () {
  "use strict";

  var SAVED = "scrud-studio-saved";
  var BUILTIN = "scrud:";
  // The fields of an item that the server manages, which a save never sends.
  var SERVER_MANAGED = ["base"];

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  document.addEventListener("htmx:configRequest", function (event) {
    var token = csrfToken();
    if (token) event.detail.headers["X-CSRFToken"] = token;
  });

  // DOM helpers: every node is made here, and text is only ever text.
  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.setAttribute("class", className);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function show(node, text) {
    if (!node) return;
    node.textContent = text || "";
    node.hidden = !text;
  }

  function readJson(response) {
    return response.json().then(
      function (data) {
        return data;
      },
      function () {
        return null;
      }
    );
  }

  function getJson(url) {
    return fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then(function (response) {
      return readJson(response).then(function (data) {
        return { response: response, data: data };
      });
    });
  }

  // POST `body` as JSON to `url`, with the CSRF token and `headers`. When the
  // answer triggers SAVED, dispatch it on <body>, after the caller has
  // reported the answer (`then`).
  function send(url, body, headers, then) {
    var all = {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-CSRFToken": csrfToken(),
    };
    Object.keys(headers || {}).forEach(function (name) {
      all[name] = headers[name];
    });
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: all,
      body: JSON.stringify(body),
    }).then(function (response) {
      return readJson(response).then(function (data) {
        then(response, data);
        var trigger = response.headers.get("HX-Trigger") || "";
        if (response.ok && trigger.indexOf(SAVED) >= 0) {
          document.body.dispatchEvent(new CustomEvent(SAVED, { bubbles: true }));
        }
      });
    });
  }

  // What a refused answer says: a sentence for its status, then the
  // problem's title and detail.
  function refusal(status, data) {
    var said =
      {
        400: "The server refused this.",
        403: "You may not do this on this branch.",
        404: "Not found, or not yours to read.",
        409: "There is a conflict.",
        412: "This item changed since you opened it. The editor now shows it as it is.",
        428: "The server needs the item's ETag; reopen the item and try again.",
      }[status] || "Refused (" + status + ").";
    if (data && typeof data === "object") {
      if (typeof data.title === "string" && data.title) said += " " + data.title;
      if (typeof data.detail === "string" && data.detail !== data.title) {
        said += ": " + data.detail;
      }
    }
    return said;
  }

  // "field: message" for each field error of a validation answer.
  function fieldErrors(data) {
    var found = [];
    if (!data || typeof data !== "object" || Array.isArray(data)) return found;
    Object.keys(data).forEach(function (key) {
      var value = data[key];
      if (["type", "title", "detail", "status"].indexOf(key) >= 0) return;
      (Array.isArray(value) ? value : [value]).forEach(function (message) {
        if (typeof message === "string") found.push(key + ": " + message);
        else if (message !== null && message !== undefined) {
          found.push(key + ": " + JSON.stringify(message));
        }
      });
    });
    return found;
  }

  function describeRule(rule) {
    if (!rule.readable) return "a rule you can't read";
    var source = "stylesheet " + rule.stylesheet + " on " + rule.branch;
    if (rule.applies_to) source += ", applies to " + rule.applies_to;
    return rule.selector + " → " + rule.component + " (" + source + ")";
  }

  function Studio(root) {
    var data = function (name) {
      return root.getAttribute("data-" + name) || "";
    };
    this.url = {
      preview: data("preview-url"),
      explain: data("explain-url"),
      options: data("options-url"),
      create: data("new-component-url"),
      save: data("save-url"),
      revert: data("revert-url"),
      pull: data("pull-url"),
      push: data("push-url"),
      item: data("item-url"),
    };
    this.slug = data("slug");
    this.branch = data("branch");
    this.query = data("query");
    this.params = new URLSearchParams(this.query);
    this.editable = data("editable") === "true";
    this.frame = byId("studio-preview");
    this.pointer = null;
    this.item = null;
    this.seq = 0;
    this.bound = new WeakSet();
  }

  Studio.prototype.start = function () {
    var self = this;
    if (this.frame) {
      this.frame.addEventListener("load", function () {
        self.bindFrame();
      });
      this.bindFrame();
    }
    document.addEventListener(SAVED, function () {
      self.refresh();
    });
    document.addEventListener("click", function (event) {
      self.onClick(event);
    });
    document.addEventListener("submit", function (event) {
      self.onSubmit(event);
    });
  };

  // Listen for clicks in the preview's document (once per document): the
  // nearest scrud-node of the target is the node selected.
  Studio.prototype.bindFrame = function () {
    var doc = this.frame.contentDocument;
    if (!doc || this.bound.has(doc)) return;
    this.bound.add(doc);
    var self = this;
    doc.addEventListener("click", function (event) {
      var target = event.target;
      var node = target && target.closest ? target.closest("scrud-node") : null;
      if (!node) return;
      event.preventDefault();
      self.select(node.getAttribute("data-pointer") || "");
    });
    this.highlight();
  };

  Studio.prototype.highlight = function () {
    var doc = this.frame && this.frame.contentDocument;
    if (!doc) return;
    var pointer = this.pointer;
    Array.prototype.forEach.call(doc.querySelectorAll("scrud-node"), function (node) {
      if (pointer !== null && node.getAttribute("data-pointer") === pointer) {
        node.setAttribute("data-studio-selected", "");
      } else if (node.hasAttribute("data-studio-selected")) {
        node.removeAttribute("data-studio-selected");
      }
    });
  };

  Studio.prototype.select = function (pointer) {
    this.pointer = pointer;
    this.highlight();
    show(byId("studio-new-result"), "");
    return this.explain(true);
  };

  // After a save: reload the preview, explain the node again, and reopen
  // the item the editor shows (or the node's winner).
  Studio.prototype.refresh = function () {
    if (this.frame) this.frame.setAttribute("src", this.url.preview);
    var keep = this.item && this.item.kind !== "component";
    if (this.pointer !== null) return this.explain(!keep).then(this.reopenIf(keep));
    if (this.item) return this.reopen();
    return Promise.resolve();
  };

  Studio.prototype.reopenIf = function (keep) {
    var self = this;
    return function () {
      if (keep) return self.reopen();
    };
  };

  Studio.prototype.nodeQuery = function (base) {
    return base + "?" + this.query + "&pointer=" + encodeURIComponent(this.pointer);
  };

  Studio.prototype.explain = function (loadWinner) {
    var self = this;
    var seq = ++this.seq;
    show(byId("studio-hint"), "");
    return getJson(this.nodeQuery(this.url.explain)).then(function (got) {
      if (seq !== self.seq) return;
      if (!got.response.ok) {
        self.renderRefusal(refusal(got.response.status, got.data));
        return;
      }
      self.renderExplanation(got.data);
      var next = [];
      if (loadWinner) next.push(self.openWinner(got.data));
      if (self.editable) next.push(self.loadOptions(seq));
      return Promise.all(next);
    });
  };

  Studio.prototype.renderRefusal = function (text) {
    var panel = byId("studio-explain");
    clear(panel);
    panel.appendChild(make("p", text, "studio-notice"));
    byId("studio-new-component").hidden = true;
  };

  Studio.prototype.renderExplanation = function (data) {
    var panel = byId("studio-explain");
    clear(panel);
    var heading = make("h3", "Node ");
    heading.appendChild(make("code", data.pointer || "(the resource)"));
    panel.appendChild(heading);

    var facts = make("dl", null, "studio-facts");
    var fact = function (name, values) {
      facts.appendChild(make("dt", name));
      var dd = make("dd");
      if (!values.length) dd.appendChild(make("span", "none", "studio-empty"));
      values.forEach(function (value, index) {
        if (index) dd.appendChild(document.createTextNode(" "));
        dd.appendChild(make("code", value));
      });
      facts.appendChild(dd);
    };
    fact("Terms", data.terms || []);
    fact("Scope chain", data.scope || []);
    panel.appendChild(facts);

    var rules = data.rules || [];
    var winner = data.winner === null || data.winner === undefined ? null : rules[data.winner];
    var verdict;
    if (winner && winner.readable) {
      verdict = "Component " + winner.component + ", chosen by " + describeRule(winner) + ".";
    } else if (winner) {
      verdict = "Its component is chosen by a rule you can't read.";
    } else {
      verdict = "No rule chose a component: it gets the built-in " + data.fallback + ".";
    }
    panel.appendChild(make("p", verdict, "studio-verdict"));

    if (rules.length) {
      panel.appendChild(make("h4", "Rules that matched, best first"));
      var list = make("ol", null, "studio-rules");
      rules.forEach(function (rule) {
        var li = make("li", null, "studio-rule studio-" + String(rule.outcome).replace(/\W+/g, "-"));
        li.appendChild(make("strong", rule.outcome));
        li.appendChild(document.createTextNode(" " + describeRule(rule) + " "));
        li.appendChild(make("code", "rank " + JSON.stringify(rule.rank)));
        list.appendChild(li);
      });
      panel.appendChild(list);
    }
  };

  // Open the node's winning component in the editor, when it is a stored
  // one the user may read; else say why there is nothing to edit.
  Studio.prototype.openWinner = function (data) {
    var rules = data.rules || [];
    var winner = data.winner === null || data.winner === undefined ? null : rules[data.winner];
    if (winner && winner.readable && winner.component.indexOf(BUILTIN) !== 0) {
      return this.open("component", winner.component, "");
    }
    var why = "The node gets a built-in component; make one for it below.";
    if (winner && !winner.readable) why = "The winning rule is one you can't read.";
    this.showMessage(why);
    return Promise.resolve();
  };

  Studio.prototype.showMessage = function (text) {
    this.item = null;
    byId("studio-editor").hidden = false;
    byId("studio-editor-title").textContent = "Editor";
    show(byId("studio-editor-source"), "");
    show(byId("studio-editor-reason"), text);
    var area = byId("studio-editor-text");
    area.value = "";
    area.hidden = true;
    byId("studio-save").hidden = true;
    byId("studio-revert").hidden = true;
    clear(byId("studio-editor-errors"));
  };

  Studio.prototype.itemUrl = function (kind, name, appliesTo) {
    var params = new URLSearchParams();
    params.set("branch", this.branch);
    params.set("kind", kind);
    params.set("name", name);
    if (appliesTo) params.set("applies_to", appliesTo);
    return this.url.item + "?" + params.toString();
  };

  // Open item {kind, name, applies_to} as the branch shows it. With
  // `orNew`, an item the branch has none of opens as a new one.
  Studio.prototype.open = function (kind, name, appliesTo, orNew) {
    var self = this;
    return getJson(this.itemUrl(kind, name, appliesTo)).then(function (got) {
      if (got.response.ok) {
        self.showItem(got.data);
      } else if (orNew && got.response.status === 404) {
        self.showItem({
          kind: kind,
          name: name,
          applies_to: appliesTo || "",
          own: false,
          isNew: true,
          etag: null,
          content: kind === "css" ? { css: "" } : {},
        });
      } else {
        self.showMessage(refusal(got.response.status, got.data));
      }
    });
  };

  Studio.prototype.reopen = function () {
    var item = this.item;
    if (!item) return Promise.resolve();
    return this.open(item.kind, item.name, item.applies_to, item.isNew);
  };

  function editedField(kind) {
    return { component: "template", css: "css" }[kind] || null;
  }

  Studio.prototype.showItem = function (item) {
    this.item = item;
    var content = item.content || {};
    var field = editedField(item.kind);
    var reason = "";
    if (!this.editable) {
      reason = "This branch is published: read-only. Changes reach it by a push from a working branch.";
    } else if (content.deleted === true) {
      reason = "Deleted on this branch.";
    } else if (item.kind === "component" && content.engine !== "jinja2") {
      reason = "A " + content.engine + "-engine component is shown read-only: the studio edits Jinja.";
    } else if (!field) {
      reason = "Shown read-only: the studio changes stylesheets through New component.";
    }
    var editable = !reason;
    var labels = { component: "Component ", css: "CSS item ", stylesheet: "Stylesheet " };
    var title = (labels[item.kind] || "") + item.name;
    if (item.applies_to) title += " (applies to " + item.applies_to + ")";
    byId("studio-editor").hidden = false;
    byId("studio-editor-title").textContent = title;
    var source;
    if (item.isNew) source = "New on branch " + this.branch + ".";
    else if (item.own) source = "Changed on branch " + this.branch + ".";
    else source = "Inherited from " + item.from + ". Saving makes an override on " + this.branch + ".";
    show(byId("studio-editor-source"), source);
    show(byId("studio-editor-reason"), reason);
    var area = byId("studio-editor-text");
    var text = field ? content[field] : JSON.stringify(content.rules || [], null, 2);
    area.value = typeof text === "string" ? text : "";
    area.hidden = false;
    area.readOnly = !editable;
    byId("studio-save").hidden = !editable;
    byId("studio-revert").hidden = !(this.editable && item.own);
    clear(byId("studio-editor-errors"));
  };

  Studio.prototype.notify = function (text) {
    show(byId("studio-message"), text);
  };

  // Report a write's answer: `done` on success; else the refusal, its field
  // errors next to the editor and, on a 412, the item reopened as it is now.
  Studio.prototype.reported = function (done) {
    var self = this;
    return function (response, data) {
      var errors = byId("studio-editor-errors");
      clear(errors);
      if (response.ok) {
        self.notify(typeof done === "function" ? done(data) : done);
        return;
      }
      self.notify(refusal(response.status, data));
      fieldErrors(data).forEach(function (error) {
        errors.appendChild(make("li", error));
      });
      if (response.status === 412) self.reopen();
    };
  };

  Studio.prototype.save = function () {
    var item = this.item;
    var field = item && editedField(item.kind);
    if (!field) return Promise.resolve();
    var content = {};
    Object.keys(item.content || {}).forEach(function (key) {
      if (SERVER_MANAGED.indexOf(key) < 0) content[key] = item.content[key];
    });
    content[field] = byId("studio-editor-text").value;
    var body = { kind: item.kind, branch: this.branch, name: item.name, content: content };
    if (item.kind === "stylesheet") body.applies_to = item.applies_to;
    var headers = item.own && item.etag ? { "If-Match": item.etag } : {};
    if (item.isNew) item.isNew = false;
    return send(this.url.save, body, headers, this.reported("Saved " + item.name + "."));
  };

  Studio.prototype.revert = function () {
    var item = this.item;
    if (!item || !item.own) return Promise.resolve();
    var body = { kind: item.kind, branch: this.branch, name: item.name };
    if (item.kind === "stylesheet") body.applies_to = item.applies_to;
    var headers = item.etag ? { "If-Match": item.etag } : {};
    return send(
      this.url.revert,
      body,
      headers,
      this.reported("Reverted " + item.name + " to the inherited item.")
    );
  };

  Studio.prototype.pull = function () {
    return send(
      this.url.pull,
      { branch: this.branch },
      {},
      this.reported(function (data) {
        var count = data && Array.isArray(data.conflicts) ? data.conflicts.length : 0;
        return "Pulled. " + count + (count === 1 ? " conflict." : " conflicts.");
      })
    );
  };

  Studio.prototype.push = function () {
    return send(this.url.push, { branch: this.branch }, {}, this.reported("Pushed."));
  };

  function fill(select, choices) {
    clear(select);
    choices.forEach(function (choice) {
      var option = make("option", choice[1]);
      option.value = choice[0];
      select.appendChild(option);
    });
  }

  Studio.prototype.loadOptions = function (seq) {
    var self = this;
    var form = byId("studio-new-component");
    return getJson(this.nodeQuery(this.url.options)).then(function (got) {
      if (seq !== self.seq) return;
      if (!got.response.ok || !got.data || !(got.data.matches || []).length) {
        form.hidden = true;
        return;
      }
      var data = got.data;
      fill(
        form.querySelector('select[name="match"]'),
        data.matches.map(function (m) {
          return [m.select, m.label];
        })
      );
      fill(
        form.querySelector('select[name="where"]'),
        [["", "anywhere"]].concat(
          (data.where || []).map(function (s) {
            return [s, "inside " + s];
          })
        )
      );
      fill(
        form.querySelector('select[name="applies_to"]'),
        (data.applies_to || [""]).map(function (c) {
          return [c, c ? "data in " + c : "every context"];
        })
      );
      var inside = data.inside_type;
      var label = byId("studio-inside-label");
      var box = form.querySelector('input[name="inside_type"]');
      box.checked = false;
      label.hidden = !inside;
      byId("studio-inside").textContent = inside
        ? "Inside this type: give property " + inside.property + " the scope " + inside.scope +
          (inside.creates_root
            ? " in a new Jinja copy of " + inside.root_component + " on this branch"
            : " in " + inside.root_component) +
          ", and select inside it"
        : "";
      show(byId("studio-new-name"), data.name ? "It will be called " + data.name + "." : "");
      self.offered = data;
      form.hidden = false;
    });
  };

  Studio.prototype.createComponent = function (form) {
    var value = function (name) {
      return form.querySelector('[name="' + name + '"]').value;
    };
    var body = {
      branch: this.branch,
      slug: this.slug,
      mode: this.params.get("mode") || "view",
      scope: this.params.get("scope") || "",
      context: this.params.get("context") || "",
      pointer: this.pointer,
      match: value("match"),
      inside_type: !!(this.offered && this.offered.inside_type) &&
        form.querySelector('input[name="inside_type"]').checked,
      where: value("where"),
      applies_to: value("applies_to"),
      template: value("template"),
    };
    var instance = this.params.get("instance");
    if (instance) body.instance = instance;
    var result = byId("studio-new-result");
    return send(this.url.create, body, {}, function (response, data) {
      if (!response.ok) {
        var said = refusal(response.status, data);
        var errors = fieldErrors(data);
        show(result, errors.length ? said + " " + errors.join("; ") : said);
        return;
      }
      show(result, verdict(data));
    });
  };

  // Whether the new rule wins for the node, and if not, what beats it.
  function verdict(data) {
    var made = (data.written || []).filter(function (w) {
      return w.kind === "component";
    });
    var name = made.length ? made[made.length - 1].name : "the component";
    if (data.wins) return "Created " + name + ". Its rule now wins for this node.";
    var explanation = data.explanation || {};
    var rules = explanation.rules || [];
    var mine = rules.filter(function (r) {
      return r.readable && r.component === name;
    })[0];
    var winner = explanation.winner === null || explanation.winner === undefined
      ? null
      : rules[explanation.winner];
    var said = "Created " + name + ". Its rule does not win for this node";
    if (mine) said += " (" + mine.outcome + ")";
    if (winner) said += ": it loses to " + describeRule(winner);
    return said + ".";
  }

  Studio.prototype.onClick = function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var opener = target.closest("[data-studio-open]");
    if (opener) {
      event.preventDefault();
      this.open(
        opener.getAttribute("data-kind"),
        opener.getAttribute("data-name"),
        opener.getAttribute("data-applies-to") || ""
      );
      return;
    }
    var actions = {
      "studio-save": this.save,
      "studio-revert": this.revert,
      "studio-pull": this.pull,
      "studio-push": this.push,
    };
    var button = target.closest("button");
    var action = button && actions[button.getAttribute("id")];
    if (action) {
      event.preventDefault();
      action.call(this);
    }
  };

  Studio.prototype.onSubmit = function (event) {
    var form = event.target;
    var id = form && form.getAttribute ? form.getAttribute("id") : null;
    if (id === "studio-new-component") {
      event.preventDefault();
      if (this.pointer !== null) this.createComponent(form);
    } else if (id === "studio-new-css") {
      event.preventDefault();
      var name = form.querySelector('input[name="name"]').value.trim();
      if (name) this.open("css", name, "", true);
    }
  };

  function start() {
    var root = byId("studio");
    if (!root) return null;
    var studio = new Studio(root);
    studio.start();
    return studio;
  }

  window.ScrudStudio = { start: start, Studio: Studio };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
