# Self-hosted studio, MR 2: the studio layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The self-hosted studio. The UI types' own pages are styled by a packaged `studio:` layer into the workbench, the editors, the rule editor and the publishing screens. It is mounted as its own published tree, and an author can edit it in itself without being able to lock themselves out.

**Architecture:**
- **Data, not views.** Everything is data in the packaged layer of `scrud_django.ui.studio` (R1, from MR 1):
  - one stylesheet (`studio:studio`);
  - Jinja components;
  - one CSS item and one JS item.
- **Screens.** The screens are renders of existing resources (`UIBranch`, `UIComponent`, `UIStylesheet`, `UICss`, `UIJs`, `UIPush`, `UIBranchVersion`) and of the studio's computed types (E2, E3). They are composed on the server with `embed(...)` (R9).
- **Writes.** They use the runtime's JSON bodies (R3) against the existing resource routes and the bootstrap studio's JSON endpoints.
- **The only new Python:**
  - small additions to the computed studio data;
  - the server-rendered studio link;
  - two component globals, `in_mount` and `api_url` (runtime, Reconciliation 4), and the `embed-preview-mode` fix (Task 6);
  - test settings that keep the suite inert (Reconciliation 2);
  - the demo's seed.

**Tech Stack:** as MR 1. The layer's JS is plain ES2017 with no modules, like `scrud-ui.js`.

**Spec:** `docs/superpowers/specs/2026-11-10-ui-self-hosted-studio-design.md`. This plan covers its "Delivery" item 2: "Where the studio lives", goals 1–7, "Self-editing" and "Publishing". It builds on MR 1's plan, `docs/superpowers/plans/2026-11-10-ui-self-hosted-studio-runtime.md`; its Interfaces blocks name every runtime function used here.

## Reconciliation with MR 1 (2026-11-10, before execution)

This plan was written before MR 1 (GitLab !12) was built. These are the
interfaces MR 1 actually shipped (`docs/ui.md`) and the rulings that adapt the
tasks to them. The task texts below are already updated; this list says why.

1. **Layer declaration.** The app config is `ScrudUIStudioConfig` (not
   `StudioConfig`). Layer files are `<package>/<kind>/<file>.json`; the item's
   name, not the file name, carries the prefix, so files are named without the
   colon (`layer/component/page.json` holds `studio:page`), which keeps the
   wheel portable.
2. **The suite stays inert (MR 1 ruling 2).** MR 1 kept the suite-wide test
   layer free of components, CSS and JS, because every page would link them
   and every item listing would list them. The studio layer has all three, so
   `tests/settings.py` installs the studio app through
   `tests.apps.StudioWithoutLayerConfig` (a `ScrudUIStudioConfig` subclass with
   `scrud_ui_layer = None`), and a `studio_layer` fixture in
   `tests/conftest.py` swaps in the real `scrud_django.ui.studio` config with
   `override_settings(INSTALLED_APPS=...)` (the pattern
   `tests/test_ui_layers.py` uses for `tests.ui_layer_more`). Every studio-layer
   test uses the fixture. Task 1 tests the real config end to end.
3. **"Byte-identical" means the frame.** Layer CSS and JS are effective on
   every branch, so with the studio app installed every page links
   `studio:studio`'s CSS and JS. Without `?ui-scope=studio` a UI-type page's
   rendered body (its frame) is byte-identical to the render without the
   layer; the page differs only by those two links.
4. **Links and embed paths in components (small runtime growth).** Stored
   Jinja components get two more globals beside `embed` (node, list and page
   contexts): `in_mount(href)`, which maps an API path into the current
   request's prefix as `selection.in_mount` does (item and push `href`s in
   studio data are API paths), and `api_url(slug, key=None)`, a registered
   type's list path, or its detail path for `key` (computed types included).
   Documented in `docs/ui.md` as runtime features (Task 1).
5. **Read check.** `branches.reads.may_read_item(request, kind, pk, content)`
   (argument order as in the code) and `layers.is_layer_row(pk, content)` are
   the only checks used for items; packaged items are public.
6. **POST decisions.** `authz.decisions_for` answers GET, PUT and DELETE only.
   "May POST" (override bodies, push actions) is
   `permissions.may_act_in_context(request, Permission("http.POST", type_uri), body, rbac_contexts_of(body))`,
   counting a decision allowed only inside a write's checks
   (`_checking_write()`, a workflow condition) as allowed: hiding a control is
   a convenience, the write's own check decides.
7. **Computed types.** `registration.computed_resource_type(...)` has no
   `register_service`; computed types are not in the service catalog. No new
   computed type is added: Tasks 2 and 6 add fields to `ui-branch-items` and
   `ui-branch-status` (`scrud_django/ui/studio/computed.py`, schemas in
   `scrud_django/ui/studio/documents/`), which `tests/conftest.py` already
   registers.
8. **Items without a URL.** A packaged item, and an ancestor's item a pinned
   branch reads from a version, has `href: null`. The editor shows such an
   item's content read-only, as text, from a hidden
   `<pre data-studio-content>` the items tree renders next to its entry.
9. **Status needs `?type=`.** `browse_url` is `null` without `?type=<slug>`. A
   component cannot read the page's query, so the workbench embeds the strip
   without it, and `studio.js` re-fetches the strip with `?type=<type>` when the
   URL state has a type (on load and when it changes).
10. **Push preview.** `ui-push-previews` answers 404 for unknown and unreadable
    branches alike; `changes` leaves out items the user can't read and counts
    them in `unreadable_changes` / `unreadable_conflicts`. The promote panel
    shows both counts, and Apply is disabled when `conflicts` is non-empty
    **or** `unreadable_conflicts > 0` (the push is refused for any conflict).
11. **Chain.** `chain` lists published ancestors above T's published root only
    when the user may GET them; promote targets are taken from `chain`.
12. **Live preview.** `POST` to `scrud-studio:live-preview`, form-encoded, one
    `request` field holding JSON, at most 50 drafts. A node component has no
    `csrf_token`, so the hidden form's `csrfmiddlewaretoken` input is filled by
    `studio.js` from the page's `csrf-token` meta tag. Bridge messages are
    `scrud-preview:select` (`pointer`, `href`) and `scrud-preview:errors`
    (`failed: true` when unreadable); Task 5 adds `scrud-preview:marks`. A select
    whose `href` is not the previewed resource's own frame comes from an embedded
    resource: the inspector names it and does not ask `explain`.
13. **Embeds.** `embed(path, scope=(), mode=None)`, Jinja only. API paths on a
    prefixed page are mapped into the prefix; same-host absolute hrefs are
    accepted; `scope=None` is no scope; an undefined variable is refused.
    Limits are 20 embeds per response and depth 3, so promote panels and push
    previews are loaded on demand (`hx-get`), not embedded at render. The
    workbench embeds at most items, strip, type picker and branch settings.
14. **Safe mode.** `?ui-safe=1` or `Scrud-UI-Safe: 1`; safe pages carry
    `data-scrud-safe`, and `scrud-ui.js` sends the header on their htmx
    requests. Requests `studio.js` makes with `fetch` send it too when the page
    is safe. Render-only state (safe mode, drafts) is cut at `dispatch_path`,
    so embedded panes read stored data.
15. **R3 writes.** `If-Match` comes from the frame only when the request
    targets the frame's own URL; an element's empty `data-scrud-etag` sends
    none. JSON-mode requests send `Scrud-UI-Branch` and `Scrud-UI-Safe`, never a
    scope or pointer, so a layer action element either swaps nothing
    (`hx-swap="none"`) and lets `studio.js` refresh the panes, or sets its scope
    with `hx-headers`. A refused action must show the server's error text.
16. **Follow-ups.** `embed-preview-mode` (widened to the bootstrap preview) is
    fixed in Task 6: the studio's previews embed editable resources.
    `push-preview-write-grants` stays open: Apply is offered on the POST grant
    (ruling 6) and a push the per-item grants refuse shows its 403
    `ui-push-forbidden`. `rules-memo-render-state-key`, `push-write-invariants`
    and `rule-index-not-rank-tail` are not touched unless a task's tests expose
    them (Task 5's marker tests run against `explain`).
17. **MR 1 carry-overs (Task 10).** A queued array operation dropped when htmx
    replaces a queued request behind an in-flight save (htmx's `last` queue
    strategy) is covered in the acceptance run, with a notice added if it
    reproduces. Acceptance tries real mouse clicks first and records whether
    real clicks or in-page `element.click()` were used.

## Global Constraints

- **Branch.** Start `feature/ui-studio-layer` from `develop` after MR 1 has merged. Don't bump the version or publish; releases happen only through a develop → main MR.
- **Repo conventions** are as MR 1's plan lists them (CLAUDE.md, commits ending with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`, verify every task with `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`, and run client tests with `SCRUD_TEST_REQUIRE_NODE=1`).
- **The layer's files.**
  - The layer is `scrud_django.ui.studio.layer`, with prefix `studio:`. It is declared on `ScrudUIStudioConfig` as `scrud_ui_layer = {"prefix": "studio:", "package": "scrud_django.ui.studio.layer"}`.
  - Its files are `layer/component/<file>.json`, `layer/stylesheet/studio.json`, `layer/css/studio.json` and `layer/js/studio.json`, shipped as package data. File names have no colon; every item's `name` starts with `studio:` (so `layer/component/page.json` is `studio:page`).
  - Components are `engine: "jinja2"`.
  - The test suite runs without the layer; studio-layer tests use the `studio_layer` fixture (Reconciliation 2).
- **Studio pages are in scope `studio` (call made while planning).**
  - The layer's rules for the page and for every UI-type root are scoped: `studio scrud:page`, `studio <UIBranch IRI>`, and so on. Without the scope, UI-type pages render as before.
  - Workbench URLs always carry `?ui-scope=studio&ui-mode=view` (R4, R10). Ruling (Task 3): a user who may PUT T gets edit mode, in which the root frame is a `<form hx-put>` and a view-mode component is skipped; the workbench is a view of T, so it is requested in view mode. A requested view mode (`?ui-mode=view`, `Scrud-UI-Mode: view`, an embed's `mode="view"`) also omits the frame's Delete button (runtime, Task 3). Branch fields are edited in Task 8's embedded settings panel.
  - Panes use narrower scopes inside it: `studio studio-items`, `studio studio-status`, `studio studio-editor`, `studio studio-push`, `studio studio-types`, `studio studio-branch`.
- **Links.** Item and push `href`s in studio data are API paths: a component links them with `in_mount(href)`; embeds map them already. Embed paths are built with `api_url(slug, key)` (Reconciliation 4).
- **Layer CSS and JS load on every page (call made while planning; there is no runtime "scope" for CSS or JS items).** They are layer items, so they are effective on every branch, so:
  - every CSS selector is under `.studio-` classes or `[data-studio]` attributes;
  - the JS does nothing unless `document.querySelector('[data-studio-workbench]')` exists.

  Test both in Task 1.
- **S and T.**
  - T is always the branch named in a URL, either the `UIBranch` resource being rendered or the `<branch>` of a computed type.
  - S is never read or changed by layer code. The layer never sets `Scrud-UI-Branch` itself. `scrud-ui.js` keeps sending the page's own branch.
- **Published T is read-only.** When T is published, editors are embedded with `mode="view"`. Save, revert, new-component, override, pull and push controls are absent, and the live preview sends no drafts.
- **Text, not HTML.** Data that JS puts into the page (explain JSON, preview errors) is inserted with `textContent` only.
- **No inline scripts or handlers** in any layer component. They are refused by tests.

## Review Focus

1. **An author breaks `studio:page` or `studio:workbench` on the published studio branch S.** `?ui-safe=1` must still render a working workbench from the layer, and the bootstrap studio must still work. Test in Task 10.
2. **T is a published branch.** Every editor is view-only, there are no write controls, and a crafted save still goes through the API's own checks. Test in Task 4.
3. **Two tabs edit the same template or rules.** The second save gets the 412 reload; live drafts never overwrite. Test in Task 4.
4. **A user who may GET T but holds no grants on UI item types.** The workbench renders: the items they can't read are absent, and no write control appears. Test in Task 3.
5. **The preview reports a click on a pointer containing `~1` or `~0`.** The inspector explains that exact node. Test in Task 6.

---

### Task 1: The studio layer skeleton and the server-rendered studio link

**Files:**
- Create:
  - `scrud_django/ui/studio/layer/__init__.py`
  - `layer/stylesheet/studio.json` (starting with the page rule)
  - `layer/component/page.json` (`studio:page`)
  - `layer/css/studio.json`
  - `layer/js/studio.json`
- Modify:
  - `scrud_django/ui/studio/apps.py`: `scrud_ui_layer` on `ScrudUIStudioConfig`;
  - `scrud_django/ui/renderer.py` (`_page` chrome): the studio link;
  - `scrud_django/ui/render.py` and `renderer.py`, where `embed` is put in a stored Jinja component's context: `in_mount` and `api_url` beside it;
  - `scrud_django/ui/studio/__init__.py`: a `studio_branch()` setting reader;
  - `tests/settings.py` (the studio app through `tests.apps.StudioWithoutLayerConfig`), `tests/apps.py`, `tests/conftest.py` (the `studio_layer` fixture);
  - `pyproject.toml` if the layer's JSON files need declaring as package data (check the built wheel, as CI's package job does).
- Test: `tests/test_studio_layer.py`

**Interfaces:**
- Produces:
  - **Setting.** `SCRUD_UI_STUDIO_BRANCH` (default `None`) names the site's published studio root. `studio_branch() -> str | None`.
  - **`studio:page`.** It keeps the branch banner and safe-mode notice (they're required). It links `css` and `scripts`, and wraps `body` in `<div class="studio" data-studio>`.
  - **The studio link.** When `scrud_django.ui.studio` is installed and the rendered resource's type is one of the seven UI types or a studio computed type, `_page` chrome adds:

    ```html
    <nav class="scrud-studio-links">
      <a href="{studio}">Studio</a>
      <a href="{safe}">Safe mode</a>
      <a href="{bootstrap}">Bootstrap studio</a>
    </nav>
    ```

    - `{studio}` is the studio root's prefix plus `ui-branches/<pk>/?ui-scope=studio&ui-mode=view`, linking to the studio root's own workbench. It is omitted when the setting is unset or the branch has no prefix.
    - `{safe}` is the current URL with `ui-safe=1`.
    - `{bootstrap}` is `reverse("scrud-studio:types")`.

    It is server chrome, like the banner: it joins `_page`'s `chrome`, so a stored page component that leaves it out is replaced by the built-in page.
  - **Component globals (runtime).** Every stored Jinja component that gets `embed` also gets `in_mount(href)` (`selection.in_mount(request, href)`; a non-string is returned unchanged) and `api_url(slug, key=None)` (the API path of the registered type `slug`'s list, or of its detail for `key`; an unknown slug is a render error of that component, so it falls back as any failing component does). Documented under "The node context" and "Embeds" in `docs/ui.md`.
  - **Test isolation.** `tests.apps.StudioWithoutLayerConfig` and the `studio_layer` fixture (Reconciliation 2).

- [ ] **Step 1: Write the failing tests:**
  - `test_layer_installed_with_studio_app`: with the real config, `installed_layers()` has `studio:`, and `manage.py check` reports no `scrud_django_ui.E002`.
  - `test_suite_runs_without_studio_layer`: with the suite's settings, no `studio:` layer is installed.
  - `test_studio_page_only_in_studio_scope`: with the layer, a UI-type page without `?ui-scope=studio` renders a body (frame) byte-identical to the render without the layer; the page differs only by the layer's CSS link and JS script.
  - `test_component_globals_in_mount_and_api_url`: under a published prefix, `in_mount("/api/todo-lists/1/")` is the prefixed path and `api_url("todo-lists", 1)` the API path; a Django component has neither.
  - `test_studio_links_on_ui_type_pages_only`: not on `todo-lists`.
  - `test_studio_links_required_in_page`
  - `test_layer_css_is_namespaced`: parse the CSS; every selector contains `.studio` or `[data-studio`.
  - `test_layer_js_inert_without_workbench` (node)
  - `test_layer_components_have_no_inline_scripts`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; add a "Self-hosted studio: setup" section to `docs/ui.md` covering the setting, the studio root and scope `studio`.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): the studio layer and the studio links`).**

---

### Task 2: Studio data the screens need

**Files:**
- Modify: `scrud_django/ui/studio/computed.py` and its documents (MR 1 Task 12)
- Test: `tests/test_studio_computed.py` (extend)

**Interfaces:**
- Produces additions to MR 1's computed types (`scrud_django/ui/studio/computed.py`; schemas in `scrud_django/ui/studio/documents/`; reads inside its `_stored_state()`; items checked with `may_read_item(request, kind, pk, content)` only):
  - **An action** is `{"method": "POST" | "PUT", "href": <API path>, "body": {...}, "etag": <quoted ETag> | null}`: what the layer sends unchanged as an R3 fixed-body element (`hx-<method>="{{ in_mount(href) }}"`, `data-scrud-body`, and `data-scrud-etag` set to `etag`, or `""` for none).
  - **`ui-branch-items`.** Each item that isn't T's own gains `override: action | null`: a POST to the item type's collection whose body creates the override on T (the item's content without `base` and `deleted`, plus `branch: T` and `rbac_context = authorizing_context(kind, content, T)`; the server sets `base`). It's `null` when T is published (`main` or one with a prefix), the entry is a deletion marker, or the user may not POST that body (Reconciliation 6).
  - **`ui-branch-status` gains:**
    - `may_write: bool`: T is working and the user may PUT T;
    - `may_publish: bool`: PUT on `ui-branches` in context `""`;
    - `pushes: [{"href", "source", "target", "state", "label"}]`, the latest 20 involving T as source or target that the user may GET, newest first;
    - `versions: [{"number", "href"}]`, T's latest 20, newest first (only when the user may GET T's versions; else `[]`);
    - `actions`, each `null` when not allowed, and all `null` (and `promote` empty) when T is published:
      - `pull`: a PUT of T's content plus `pull: true` to T's href, with T's ETag; only when T has a parent and `may_write`;
      - `promote`: one entry per ancestor in `chain` (nearest first), `{"target": name, "apply": action | null, "propose": action | null}`, where `apply` and `propose` POST `{"source": T, "target": name, "state": "applied" | "proposed", "rbac_context": <target's context>}` to `ui-pushes`, each present when the user may POST that body (Reconciliation 6).
    - `endpoints: {"live_preview", "explain", "options", "new_component"} | null`, `reverse()`d from the `scrud-studio` namespace (`live-preview`, and the explain, new-component options and new-component routes; `explain` and `options` take the type slug, so they are given as a template with `{slug}` or as the route prefix, builder's choice, documented). `null` when the studio's URLs are not included (`NoReverseMatch`).

- [ ] **Step 1: Write the failing tests:**
  - `test_override_body_creates_the_override`: POST it and get 201, with the server setting `base`; also for a packaged item (its `base` is the layer item's ETag).
  - `test_override_null_on_published_or_without_grant`
  - `test_status_may_flags`
  - `test_status_pushes_and_versions_readable_only`
  - `test_status_actions_round_trip`: sending each action (method, href, body, `If-Match` from `etag`) succeeds: pull, apply and propose to the parent, and apply to a further ancestor.
  - `test_status_actions_null_on_published_T`
  - `test_endpoints_in_status_reverse_correctly` (moved from Task 6)
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; update the types' schemas and `docs/ui.md`.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): override bodies, permissions and actions in studio data`).**

---

### Task 3: The workbench shell, items tree and branch strip

**Files:**
- Create, in `layer/component/` (file names without the prefix):
  - `workbench.json` (`studio:workbench`): the root of `UIBranch` in scope `studio`;
  - `items.json` (`studio:items`): the root of `UIBranchItems` in `studio studio-items`;
  - `status.json` (`studio:status`): the root of `UIBranchStatus` in `studio studio-status`.
- Modify:
  - `layer/stylesheet/studio.json` (rules)
  - `layer/css/studio.json`
  - `layer/js/studio.json`
- Test: `tests/test_studio_workbench.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes: `embed` (R9); `ui-branch-items`, `ui-branch-status` (Task 2).
- Produces:
  - **`studio:workbench`** renders:
    - `<div class="studio-workbench" data-studio-workbench data-studio-branch="{{ value.name }}" data-studio-published="true|false">`, where T is published when it is `main` or has a `prefix` (a working branch's content has no `prefix` key);
    - a top bar with the Items / Editor / Preview toggles;
    - three panes, `<section class="studio-pane" data-studio-pane="items|editor|preview">`;
    - a bottom strip.

    It embeds `api_url("ui-branch-items", value.name)` with scope `["studio", "studio-items"]` into the Items pane, and `api_url("ui-branch-status", value.name)` with scope `["studio", "studio-status"]` into the strip.
  - **`studio:items`** renders a tree grouped by kind:
    - each entry has a `data-studio-open` JSON attribute, `{"kind", "name", "applies_to", "href", "override"}`, with `href` mapped by `in_mount` (or `null`);
    - an entry whose `href` is `null` (packaged, or an ancestor's item read from a version) has a hidden `<pre data-studio-content>` with its content as indented JSON (Reconciliation 8);
    - an entry with a non-null `override` has an "Override on this branch" R3 fixed-body button from it (Task 4 uses it);
    - each is marked with a class: `studio-own`, `studio-inherited` or `studio-packaged`;
    - each conflict gets a `studio-conflict-parent` or `studio-conflict-layer` class.
  - **`studio:status`** renders:
    - the chain;
    - the changed and conflict counts;
    - Pull, and Push / Propose to the parent (`promote[0]`'s `apply` and `propose`), as R3 fixed-body elements from `actions`, present only when non-null (Task 7 turns Push into Promote panels);
    - "Browse app on this branch" (`browse_url`), only when non-null.
  - **`studio.js` workbench behaviour:**
    - pane toggles collapse a pane to a rail. The state is stored in `localStorage` under `studio:panes`, wrapped in try/catch, and the preview can't be hidden;
    - workbench state lives in the URL query (`type`, `item`, `pointer`, `mode`, `context`, `scope`, `instance`, `pinned`), via `history.replaceState`; `ui-scope=studio`, `ui-mode=view` and any `ui-safe` stay;
    - when the state has a `type` (on load, and when it changes), it re-fetches the strip with `?type=<type>` (Reconciliation 9);
    - clicking a `data-studio-open` entry sets `item` and loads the editor (Task 4);
    - after any successful write (`htmx:afterRequest` with a 2xx on a non-GET), it re-fetches the embedded items and strip with `hx-get` (same URL, with the pane's `Scrud-UI-Scope` header set explicitly) and refreshes the preview;
    - a failed write shows the server's error text in the workbench (as text), whatever element sent it.
  - **The bare preview (moved here from Task 6, which Tasks 4 and 5 need).** The Preview pane holds a hidden `<form method="post" target="studio-preview">` with a `csrfmiddlewaretoken` input and a `request` input, and `<iframe name="studio-preview" sandbox="allow-scripts" class="studio-preview">`. `studio.js` sets the form's `action` from the strip's `endpoints.live_preview` (Task 2), fills the token from the `csrf-token` meta tag (Reconciliation 12), and exposes `submitPreview(extra)`, which builds the `request` JSON from the URL state (`type`, T as `branch`, `mode`, `scope`, `context`, `instance`) merged with `extra` (`drafts`, `mark`, `after_push`) and submits. With no `type`, or no `endpoints`, nothing is submitted. Task 6 adds the controls and the inspector.

- [ ] **Step 1: Write the failing tests:**
  - `test_workbench_renders_for_branch_user_may_get`, and 404/403 otherwise, as `ui-branches` answers.
  - `test_workbench_embeds_items_and_strip_for_T_not_S`: render T=`alice-wip` with S=`studio`; the items listed are `alice-wip`'s.
  - `test_reader_without_ui_grants_sees_no_write_controls` (Review Focus 4)
  - `test_published_T_strip_has_no_write_actions`
  - `test_workbench_hrefs_stay_in_mount`: under a published prefix, `data-studio-open` hrefs and the strip's action URLs are under the prefix.
  - Node:
    - `test_toggles_persist_and_preview_cannot_hide`
    - `test_strip_refetched_with_type`
    - `test_state_round_trips_through_url`
    - `test_write_refreshes_items_strip_preview`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): workbench shell, items tree and branch strip`).**

---

### Task 4: Template, CSS and JS editors with live typing

**Files:**
- Create, in `layer/component/` (file names without the prefix):
  - `component-editor.json` (`studio:component-editor`): the root of `UIComponent` in `studio studio-editor`, edit mode;
  - `component-view.json` (`studio:component-view`): the same, view mode;
  - `text-editor.json` and `text-view.json` (`studio:text-editor`, `studio:text-view`): `UICss` and `UIJs`.
- Modify: `layer/stylesheet/studio.json`, `layer/js/studio.json`, `layer/css/studio.json`
- Test: `tests/test_studio_editors.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes: R3, R10, live preview (E1), `override` (Task 2).
- Produces:
  - **Loading the editor.** `studio.js` loads the open item's editor with `hx-get` of its (mounted) `href`, with `Scrud-UI-Scope: studio studio-editor`, and `Scrud-UI-Mode: view` when T is published or the item isn't T's own. The resource view then decides edit or view by the user's PUT grant as usual. An item with no `href` (Reconciliation 8) is shown read-only from its `<pre data-studio-content>`, as text.
  - **Inherited or packaged items** show "Override on this branch": the items tree's R3 fixed-body POST of the `override` action (Task 2), shown in the editor pane while the item is open. On 201 the panes refresh and the editor loads the new own row (from the refreshed tree's entry).
  - **The edit components.**
    - Template, CSS or JS: a monospace `<textarea name="/template">` (or `/css`, `/js`).
    - **Save:** the frame's PUT with `If-Match`, through the existing `scrud-json`.
    - **Revert to inherited:** an R3 `hx-delete` of the item with `If-Match`.
    - A Django-engine component always renders read-only, with a note.
  - **Live typing.** On `keyup` with a 300 ms debounce (call), `studio.js` submits the preview form (Task 3's `submitPreview`) with `drafts: [{kind, content}]`, the editor's current content (the frame's `scrud-content` with the textarea applied). It sends none when T is published, or when no type is chosen.
  - **Errors.** Draft errors arrive from the bridge's `scrud-preview:errors` and show under the editor as text. Save errors use the runtime's refused-PUT re-render.

- [ ] **Step 1: Write the failing tests:**
  - `test_own_component_editor_has_save_and_revert`
  - `test_inherited_item_view_mode_with_override_button`
  - `test_override_then_save_round_trip`: the override gets the server's `base`, and the save gets `If-Match`.
  - `test_published_T_editor_view_only` (Review Focus 2): plus a crafted PUT to an item on the published branch gets exactly the API's answer for that user.
  - `test_django_engine_component_read_only`
  - `test_second_tab_save_gets_412_reload` (Review Focus 3)
  - Node:
    - `test_keyup_debounced_sends_drafts`
    - `test_no_drafts_when_published`
    - `test_draft_errors_shown_as_text`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): template, CSS and JS editors with live preview`).**

---

### Task 5: The rule editor

**Files:**
- Create, in `layer/component/` (file names without the prefix):
  - `stylesheet-editor.json` (`studio:stylesheet-editor`): the root of `UIStylesheet` in `studio studio-editor`;
  - `rules-editor.json` (`studio:rules-editor`): the `/rules` array in edit mode;
  - `rule-row.json` (`studio:rule-row`): a rule item;
  - view-mode twins of each.
- Modify: `layer/stylesheet/studio.json`, `layer/js/studio.json`, `layer/css/studio.json`, `scrud_django/ui/studio/static/scrud_studio/preview-bridge.js` (the `marks` message), `docs/ui.md` (the bridge's messages)
- Test: `tests/test_studio_rule_editor.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes: R6 array operations and selector-error placement; E1 `mark`.
- Produces:
  - **The stylesheet editor** shows `name` read-only, plus `applies_to`, `order`, `prefixes` (key/value rows) and the rules. All fields are inputs named by JSON Pointer, so the frame's PUT saves them.
  - **Each rule row** has:
    - inputs `{{ pointer }}/select` and `{{ pointer }}/component`;
    - R6 Remove, Move up and Move down;
    - `data-studio-rule-index="{{ index }}"`.

    The append row adds a rule (R6).
  - **Focus.** Focusing a row's input makes `studio.js` resubmit the preview with `mark: {"stylesheet": name, "applies_to": applies_to, "index": i}` and the stylesheet's current form content as a draft, so unsaved edits are marked too. Blur clears `mark`.
  - **Counts.** Under the rows, the bridge's reply fills "matches N nodes, wins on M". The preview bridge posts `{"type": "scrud-preview:marks", "wins": M, "loses": L}` on load (counting `data-scrud-match` nodes, read when the bridge runs, before the branch's scripts, as the errors message is); add this message to `preview-bridge.js` and its header comment, and to the bridge list in `docs/ui.md`. `studio.js` accepts it only from the preview iframe (`event.source`), with integer counts.
  - **Marks agree with the render.** Mark indexes are rule indexes in the stylesheet (follow-up `rule-index-not-rank-tail` stays open); `test_marks_agree_with_explain_for_layer_and_stored_sheets` checks a draft with a reordered rule against `explain`.

- [ ] **Step 1: Write the failing tests:**
  - `test_stylesheet_editor_fields_and_rows`
  - `test_rule_rows_have_array_controls_and_index`
  - `test_bad_selector_error_on_its_row`
  - `test_view_mode_twin_has_no_inputs`
  - `test_marks_agree_with_explain_for_layer_and_stored_sheets`
  - Node:
    - `test_focus_sends_mark_with_draft`
    - `test_blur_clears_mark`
    - `test_marks_message_updates_counts`
    - `test_bridge_posts_marks`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): the rule editor with live match markers`).**

---

### Task 6: Preview pane, controls, inspector and new component

**Files:**
- Create: `layer/component/type-picker.json` (`studio:type-picker`)
- Modify: `layer/component/workbench.json`, `layer/stylesheet/studio.json`, `layer/js/studio.json`, `layer/css/studio.json`; the runtime's embed or preview code for `embed-preview-mode` (`scrud_django/ui/embed.py` and/or the studio preview), `docs/follow-ups.md` (remove the entry), `docs/ui.md`
- Test: `tests/test_studio_preview_pane.py`, `tests/test_studio_live_preview.py`, `tests/test_studio_preview.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes:
  - E1 `scrud-studio:live-preview`;
  - the bootstrap endpoints `scrud-studio:explain`, `scrud-studio:new-component-options` and `scrud-studio:new-component` (unchanged);
  - `ui-type-catalog`.
- Produces:
  - **The preview pane** (its hidden form, iframe and `submitPreview` are Task 3's; the endpoint URLs are the strip's `endpoints`, Task 2) gains the controls: a type picker (embed of `api_url("ui-type-catalog", "all")` in `studio studio-types`, rendering a `<select>`; a new `studio:type-picker` component, `layer/component/type-picker.json`), mode, data context, scope, instance pk and pinned. Each control updates the URL state and resubmits the preview.
  - **`embed-preview-mode` (runtime fix, follow-up removed).** An embed inside a preview rendered in view mode (the live preview, and the bootstrap studio's `preview` with `mode=view`) renders in view mode: no `<input name="/...">`, `<textarea name="/...">` or `data-scrud-array-op`. Test as the follow-up describes, in `tests/test_studio_live_preview.py` and `tests/test_studio_preview.py`.
  - **The inspector** sits under the preview:
    1. On `message` events where `event.source === iframe.contentWindow` and `data.type === "scrud-preview:select"`, it sets `pointer` in the URL state and fetches `explain` for T. A select whose `href` is not the previewed resource's own frame is a node of an embedded resource: the inspector names that resource (text) and does not ask `explain` (Reconciliation 12). `fetch` requests send `Scrud-UI-Safe: 1` on a safe page.
    2. It renders the explanation as text: pointer, terms, winner, and the losers with their outcomes.
    3. "Edit this component" opens the winning component in the editor.
    4. "New rule for this node" shows the bootstrap studio's new-component form fields (from `options`), and POSTs `new-component` as JSON with `fetch` and the CSRF header. On 201 it refreshes everything.

    These actions are absent when T is published.

- [ ] **Step 1: Write the failing tests:**
  - `test_preview_pane_markup`: the iframe has exactly `sandbox="allow-scripts"`, the form targets it, it has the `csrfmiddlewaretoken` and `request` inputs, and there's no inline script.
  - `test_type_picker_lists_catalog`
  - `test_embed_in_view_mode_preview_is_view_mode` (live preview and bootstrap `preview`)
  - Node:
    - `test_csrf_token_filled_from_meta`
    - `test_select_in_embedded_resource_not_explained`
    - `test_select_message_from_preview_only`: a message from another window is ignored.
    - `test_pointer_with_tilde_explained_exactly` (Review Focus 5)
    - `test_inspector_text_only`
    - `test_new_rule_posts_and_refreshes`
    - `test_no_inspector_actions_when_published`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): preview pane, inspector and new rule for a node`).**

---

### Task 7: Promote: push review and push records (publishing 1–2)

**Files:**
- Create, in `layer/component/` (file names without the prefix):
  - `push-preview.json` (`studio:push-preview`): the root of `UIPushPreview` in `studio studio-push`;
  - `push.json` (`studio:push`): the root of `UIPush` in scope `studio`, a page.
- Modify: `layer/component/status.json`, `layer/stylesheet/studio.json`, `layer/js/studio.json`, `layer/css/studio.json`
- Test: `tests/test_studio_promote.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes: `ui-push-previews` (E3: 404 for an unknown or unreadable branch; `unreadable_changes`, `unreadable_conflicts`); E1 `after_push`; R3; Task 2's `actions.promote`.
- Produces:
  - **Promote in the strip.** For each `actions.promote` entry (the parent first, then the ancestors in `chain`), "Promote to <target>…" opens a review panel. The panel:
    - is loaded on demand with `hx-get` of `in_mount(api_url("ui-push-previews", T))` plus `?target=<target>`, with `Scrud-UI-Scope: studio studio-push` (not embedded at render: Reconciliation 13). It shows the changes with before and after, the conflicts, and "N changes / conflicts on items you can't read" from the two counts;
    - has "Preview after push", which calls `submitPreview` with `after_push: {"source": T}` and branch = target;
    - has "Apply" and "Propose", the entry's `apply` and `propose` actions (R3 fixed-body POSTs to `ui-pushes`), each present only when non-null. Their bodies come from the strip's data; the panel gets them from the strip's entry (`studio.js` carries them), not from the push preview, which has no actions.

    When `conflicts` is non-empty or `unreadable_conflicts > 0`, Apply is disabled and the panel says why. A push the per-item grants refuse shows its 403 `ui-push-forbidden` text (follow-up `push-preview-write-grants` stays open).
  - **`studio:push` page** for a push:
    - source, target, state, label and result;
    - for an unapplied push with a source, an embed of `api_url("ui-push-previews", source)` plus `?target=<target>` in `studio studio-push` (a restore has no source: no embed);
    - a state form: a text input with `datalist` `proposed`, `applied`, plus a PUT through the frame. A refused transition shows the server's error, through the runtime's refused-PUT re-render. A user who may not PUT the push gets view mode: no form.
  - **The strip's `pushes`** link to these pages with `in_mount(href)` plus `?ui-scope=studio`.

- [ ] **Step 1: Write the failing tests:**
  - `test_promote_panel_shows_changes_and_conflicts`
  - `test_apply_push_from_panel_round_trip`: the target changes, and T is emptied.
  - `test_propose_then_apply_from_push_page`
  - `test_refused_transition_shows_server_error`: use a `WorkflowPolicy` that forbids `proposed` → `applied` for the user.
  - `test_apply_disabled_with_conflicts`, including conflicts only on unreadable items
  - `test_unreadable_counts_shown`
  - `test_after_push_preview_request_shape` (node)
  - `test_promote_to_ancestor_beyond_parent`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): promote with review, propose and apply`).**

---

### Task 8: Roll back and presentations (publishing 3–4)

**Files:**
- Create, in `layer/component/` (file names without the prefix):
  - `version.json` (`studio:version`): the root of `UIBranchVersion` in scope `studio`;
  - `branch-form.json` (`studio:branch-form`): the edit component of the `UIBranch` fields, in `studio studio-branch`.
- Modify: `layer/component/status.json`, `layer/component/workbench.json`, `layer/stylesheet/studio.json`, `layer/css/studio.json`
- Test: `tests/test_studio_rollback_presentations.py`

**Interfaces:**
- Consumes: `versions`, `may_publish`, `may_write` (Task 2); R3; R10.
- Produces:
  - **History.** The strip lists `versions`, linked with `in_mount(href)` plus `?ui-scope=studio`. A version page shows its items. "Restore to this version" is an R3 fixed-body POST of `{"source": null, "target": T, "restore": N, "state": "applied", "rbac_context": <T's context>}` to `ui-pushes` (`api_url("ui-pushes")`, mapped with `in_mount`). The version page takes T's name and context from the version's own content (check `UIBranchVersion`'s schema; if it lacks either, offer the restore from the strip's `versions` entries instead, where T is known, and record the choice in the report). Absent when T is published, and on the version page when the user may not POST it (view only).
  - **Branch settings.** The workbench has a "Branch settings" panel, an embed of the branch itself (`api_url("ui-branches", <T's pk>)`, the workbench's own resource) in `studio studio-branch`. It is a different scope chain from the workbench's, so it is not a cycle. It shows `label`, `description` and `base` (live or pinned), which are editable when `may_write`.
    - `prefix` is an input only when `may_publish`; otherwise it renders as text.
    - Server prefix errors show beside the field through the refused-PUT re-render.
  - **New branch.** A "New branch" form, `data-scrud-json="form"` POST to `in_mount(api_url("ui-branches"))`, with `name`, `parent` (default T) and `rbac_context` (default T's context). It sits outside the branch's frame, or carries an empty `data-scrud-etag=""`, so no `If-Match` is sent.

- [ ] **Step 1: Write the failing tests:**
  - `test_restore_from_version_page_round_trip`
  - `test_prefix_input_only_with_site_grant`: without the grant, a crafted PUT is refused with the API's answer.
  - `test_overlapping_prefix_error_beside_field`
  - `test_new_branch_form_creates_working_branch`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): version history, restore, branch settings and new branches`).**

---

### Task 9: The demo

**Files:**
- Modify: `demo/demo/settings.py` (`SCRUD_UI_STUDIO_BRANCH = "studio"`), `demo/demo/management/commands/seed_todo_demo.py`
- Test: `tests/test_demo_seed.py` if one exists, else a seed smoke test in `tests/test_studio_workbench.py` that runs the command's seeding functions against the test database.

**Interfaces:**
- Produces, in the seed (idempotent, like the rest; the demo installs the real `scrud_django.ui.studio`, so its layer is live there):
  - a published root branch `studio`, at prefix `/workbench/`, context `ui-studio`;
  - a working branch `studio-wip` under it, context `ui-studio-wip`;
  - grants for `author`: GET on `studio`; write on UI item types and `ui-pushes` in `ui-studio-wip`; push into `studio` (`ui-pushes` in `ui-studio`); GET on `alice-wip`'s tree, as today;
  - `owner` holds the site-level `ui-branches` grant in `""`.
  - It prints the workbench URLs: `/workbench/ui-branches/<alice-wip pk>/?ui-scope=studio&ui-mode=view` and the `studio-wip` one.
- The demo sidebar's move from `hx-get` to `embed` is the builder's choice; keep it if it's free.

- [ ] **Step 1: Write the failing test: `test_seed_creates_studio_tree_and_grants`.**
- [ ] **Step 2: Run it; expect failure.**
- [ ] **Step 3: Implement; CLAUDE.md's demo commands already run `seed_todo_demo`: add the workbench URL after them.**
- [ ] **Step 4: Run it; expect PASS.**
- [ ] **Step 5: Lint, then commit (`demo: the studio tree at /workbench/`).**

---

### Task 10: Self-editing, recovery, docs and acceptance

**Files:**
- Modify: `docs/ui.md` (a "Self-hosted studio" chapter: the workbench, the editors, the rule editor, publishing, editing the studio, safe mode and the bootstrap studio as ways out); `docs/release-notes.md`; `docs/follow-ups.md`
- Test: `tests/test_studio_self_editing.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing tests:**
  - `test_override_studio_component_on_studio_wip_changes_only_its_preview`: S=`studio` is unchanged. The live preview of a `UIStylesheet` with T=`studio-wip` shows the override.
  - `test_push_studio_override_promotes_it`: after pushing `studio-wip` → `studio`, the workbench under `/workbench/` uses the override.
  - `test_broken_studio_page_on_S_recoverable` (Review Focus 1):
    - a `studio:page` override on `studio` that raises falls back to the built-in page, with the links still present;
    - with `?ui-safe=1` (and with `Scrud-UI-Safe: 1` on the panes' refreshes), the full layer workbench renders, its panes included;
    - `/studio/` (bootstrap) still works, and its revert removes the override.
  - `test_app_css_and_js_not_under_studio_prefix`: `main`'s CSS and JS items are not linked on `/workbench/` pages (spec: "the app's CSS and JS don't load under the studio's prefix").
- [ ] **Step 2: Run them; expect failures until the pieces are wired; fix what they expose.**
- [ ] **Step 3: Write the docs and release notes; build with `uv run --group docs sphinx-build -W -b html docs docs/_build`.**
- [ ] **Step 4: Run `uv run pytest -q`, then `--reverse`, with node required; run on PostgreSQL and MySQL in throwaway containers; `manage.py check`; the migrations check.**
- [ ] **Step 5: Run browser acceptance on the demo with Playwright (the MCP tools in this session), and record the results and screenshots in the report:**
  1. `author` opens `alice-wip`'s workbench and selects `todo-lists`.
  2. They click an item; the inspector explains it.
  3. They override `todo-row`, type in the template, and see the preview change before saving. They save.
  4. They open stylesheet `todo`, focus a rule, and see wins and losses marked. They reorder a rule and save.
  5. They promote to `testing-admin`, reviewing the changes and the after-push preview, then apply.
  6. On `studio-wip` they override `studio:rule-row`, check it in the preview of a stylesheet, and push to `studio`. The workbench shows it.
  7. They break `studio:page` on `studio`, recover with Safe mode, and revert the override.
  8. They collapse Items and then Editor, reload, and the layout persists.
  9. (MR 1 carry-over) In the rule editor, start a save and, while it is in flight, click Move up on a rule twice; check whether htmx's `last` queue strategy drops a queued array operation silently. If it reproduces, add a client notice for a dropped request in `scrud-ui.js` (with a node test) and record it; if not, record that.

  Try real mouse clicks (Playwright `browser_click`) first; record in the report whether real clicks or in-page `element.click()` were used, and why.
- [ ] **Step 6: Commit (`docs(studio): the self-hosted studio; acceptance`).**
