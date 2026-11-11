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
  - the demo's seed.

**Tech Stack:** as MR 1. The layer's JS is plain ES2017 with no modules, like `scrud-ui.js`.

**Spec:** `docs/superpowers/specs/2026-11-10-ui-self-hosted-studio-design.md`. This plan covers its "Delivery" item 2: "Where the studio lives", goals 1–7, "Self-editing" and "Publishing". It builds on MR 1's plan, `docs/superpowers/plans/2026-11-10-ui-self-hosted-studio-runtime.md`; its Interfaces blocks name every runtime function used here.

## Global Constraints

- **Branch.** Start `feature/ui-studio-layer` from `develop` after MR 1 has merged. Don't bump the version.
- **Repo conventions** are as MR 1's plan lists them (CLAUDE.md, commits ending with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`, verify every task with `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`, and run client tests with `SCRUD_TEST_REQUIRE_NODE=1`).
- **The layer's files.**
  - The layer is `scrud_django.ui.studio.layer`, with prefix `studio:`. It is declared on `StudioConfig` as `scrud_ui_layer = {"prefix": "studio:", "package": "scrud_django.ui.studio.layer"}`.
  - Its files are `layer/component/<name>.json`, `layer/stylesheet/studio.json`, `layer/css/studio.json` and `layer/js/studio.json`. Every name starts with `studio:`.
  - Components are `engine: "jinja2"`.
- **Studio pages are in scope `studio` (call made while planning).**
  - The layer's rules for the page and for every UI-type root are scoped: `studio scrud:page`, `studio <UIBranch IRI>`, and so on. Without the scope, UI-type pages render as before.
  - Workbench URLs always carry `?ui-scope=studio` (R4).
  - Panes use narrower scopes inside it: `studio studio-items`, `studio studio-status`, `studio studio-editor`, `studio studio-push`.
- **Layer CSS and JS load on every page (call made while planning).** They are layer items, so they are effective on every branch, so:
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
  - `layer/component/studio:page.json`
  - `layer/css/studio.json`
  - `layer/js/studio.json`
- Modify:
  - `scrud_django/ui/studio/apps.py`: `scrud_ui_layer`;
  - `scrud_django/ui/renderer.py` (`_page` chrome): the studio link;
  - `scrud_django/ui/studio/__init__.py`: a `studio_branch()` setting reader;
  - `tests/settings.py`
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

    - `{studio}` is the studio root's prefix plus `ui-branches/<pk>/?ui-scope=studio`, linking to the studio root's own workbench. It is omitted when the setting is unset or the branch has no prefix.
    - `{safe}` is the current URL with `ui-safe=1`.
    - `{bootstrap}` is `reverse("scrud-studio:types")`.

    It is server chrome, like the banner: a stored page component that leaves it out is replaced by the built-in page.

- [ ] **Step 1: Write the failing tests:**
  - `test_layer_installed_with_studio_app`
  - `test_studio_page_only_in_studio_scope`: a UI-type page without `?ui-scope=studio` is byte-identical to before.
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
- Produces additions to MR 1's computed types:
  - **`ui-branch-items`.** Each item that isn't T's own gains `override: dict | null`: the exact body that creates the override on T (content plus `branch: T` plus `rbac_context = authorizing_context(kind, content, T)`). It's `null` when T is published or the user may not POST that item in that context (`decisions_for`).
  - **`ui-branch-status` gains:**
    - `may_write: bool`: T is working and the user may PUT T;
    - `may_publish: bool`: PUT on `ui-branches` in context `""`;
    - `pushes: [{"href", "source", "target", "state", "label"}]`, the latest 20 involving T as source or target that the user may GET, newest first;
    - `versions: [{"number", "href"}]`, T's latest 20, newest first;
    - `actions`, fixed JSON bodies the layer sends unchanged with R3:
      - `pull: {...}`, the PUT body for `pull: true` with T's ETag;
      - `push_to_parent`, the `ui-pushes` POST body in `applied`;
      - `propose_to_parent`, the same body in `proposed`;
      - each is `null` when not allowed.

- [ ] **Step 1: Write the failing tests:**
  - `test_override_body_creates_the_override`: POST it and get 201, with the server setting `base`.
  - `test_override_null_on_published_or_without_grant`
  - `test_status_may_flags`
  - `test_status_pushes_and_versions_readable_only`
  - `test_status_actions_round_trip`: sending each body succeeds.
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; update the types' schemas and `docs/ui.md`.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): override bodies, permissions and actions in studio data`).**

---

### Task 3: The workbench shell, items tree and branch strip

**Files:**
- Create, in `layer/component/`:
  - `studio:workbench.json`: the root of `UIBranch` in scope `studio`;
  - `studio:items.json`: the root of `UIBranchItems` in `studio studio-items`;
  - `studio:status.json`: the root of `UIBranchStatus` in `studio studio-status`.
- Modify:
  - `layer/stylesheet/studio.json` (rules)
  - `layer/css/studio.json`
  - `layer/js/studio.json`
- Test: `tests/test_studio_workbench.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes: `embed` (R9); `ui-branch-items`, `ui-branch-status` (Task 2).
- Produces:
  - **`studio:workbench`** renders:
    - `<div class="studio-workbench" data-studio-workbench data-studio-branch="{{ value.name }}" data-studio-published="{{ value.prefix is not none }}">`;
    - a top bar with the Items / Editor / Preview toggles;
    - three panes, `<section class="studio-pane" data-studio-pane="items|editor|preview">`;
    - a bottom strip.

    It embeds `ui-branch-items/<T>/` with scope `["studio", "studio-items"]` into the Items pane, and `ui-branch-status/<T>/` with scope `["studio", "studio-status"]` into the strip.
  - **`studio:items`** renders a tree grouped by kind:
    - each entry has a `data-studio-open` JSON attribute, `{"kind", "name", "applies_to", "href", "override"}`;
    - each is marked with a class: `studio-own`, `studio-inherited` or `studio-packaged`;
    - each conflict gets a `studio-conflict-parent` or `studio-conflict-layer` class.
  - **`studio:status`** renders:
    - the chain;
    - the changed and conflict counts;
    - Pull, Push and Propose buttons as R3 fixed-body elements from `actions`, present only when non-null;
    - "Browse app on this branch" (`browse_url`).
  - **`studio.js` workbench behaviour:**
    - pane toggles collapse a pane to a rail. The state is stored in `localStorage` under `studio:panes`, wrapped in try/catch, and the preview can't be hidden;
    - workbench state lives in the URL query (`type`, `item`, `pointer`, `mode`, `context`, `scope`, `instance`, `pinned`), via `history.replaceState`;
    - clicking a `data-studio-open` entry sets `item` and loads the editor (Task 4);
    - after any successful write (`htmx:afterRequest` with a 2xx on a non-GET), it re-fetches the embedded items and strip with `hx-get` (same URL, same scope header) and refreshes the preview.

- [ ] **Step 1: Write the failing tests:**
  - `test_workbench_renders_for_branch_user_may_get`, and 404/403 otherwise, as `ui-branches` answers.
  - `test_workbench_embeds_items_and_strip_for_T_not_S`: render T=`alice-wip` with S=`studio`; the items listed are `alice-wip`'s.
  - `test_reader_without_ui_grants_sees_no_write_controls` (Review Focus 4)
  - `test_published_T_strip_has_no_write_actions`
  - Node:
    - `test_toggles_persist_and_preview_cannot_hide`
    - `test_state_round_trips_through_url`
    - `test_write_refreshes_items_strip_preview`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): workbench shell, items tree and branch strip`).**

---

### Task 4: Template, CSS and JS editors with live typing

**Files:**
- Create, in `layer/component/`:
  - `studio:component-editor.json`: the root of `UIComponent` in `studio studio-editor`, edit mode;
  - `studio:component-view.json`: the same, view mode;
  - `studio:text-editor.json` and `studio:text-view.json`: `UICss` and `UIJs`.
- Modify: `layer/stylesheet/studio.json`, `layer/js/studio.json`, `layer/css/studio.json`
- Test: `tests/test_studio_editors.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes: R3, R10, live preview (E1), `override` (Task 2).
- Produces:
  - **Loading the editor.** `studio.js` loads the open item's editor with `hx-get` of its `href`, with `Scrud-UI-Scope: studio studio-editor`, and `Scrud-UI-Mode: view` when T is published or the item isn't T's own. The resource view then decides edit or view by the user's PUT grant as usual.
  - **Inherited or packaged items** show "Override on this branch": an R3 fixed-body POST of `override` to the item type's collection URL. On 201 the editor reloads the new own row.
  - **The edit components.**
    - Template, CSS or JS: a monospace `<textarea name="/template">` (or `/css`, `/js`).
    - **Save:** the frame's PUT with `If-Match`, through the existing `scrud-json`.
    - **Revert to inherited:** an R3 `hx-delete` of the item with `If-Match`.
    - A Django-engine component always renders read-only, with a note.
  - **Live typing.** On `keyup` with a 300 ms debounce (call), `studio.js` submits the preview form (Task 6) with `drafts: [{kind, content}]`, the editor's current content. It sends none when T is published.
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
- Create, in `layer/component/`:
  - `studio:stylesheet-editor.json`: the root of `UIStylesheet` in `studio studio-editor`;
  - `studio:rules-editor.json`: the `/rules` array in edit mode;
  - `studio:rule-row.json`: a rule item;
  - view-mode twins of each.
- Modify: `layer/stylesheet/studio.json`, `layer/js/studio.json`, `layer/css/studio.json`
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
  - **Counts.** Under the rows, the bridge's reply fills "matches N nodes, wins on M". The preview bridge posts `{"type": "scrud-preview:marks", "wins": M, "loses": L}` on load; add this message to `preview-bridge.js` (MR 1 Task 11).

- [ ] **Step 1: Write the failing tests:**
  - `test_stylesheet_editor_fields_and_rows`
  - `test_rule_rows_have_array_controls_and_index`
  - `test_bad_selector_error_on_its_row`
  - `test_view_mode_twin_has_no_inputs`
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
- Modify: `layer/component/studio:workbench.json`, `layer/js/studio.json`, `layer/css/studio.json`
- Test: `tests/test_studio_preview_pane.py`; node: `tests/test_studio_layer_client.py`

**Interfaces:**
- Consumes:
  - E1 `scrud-studio:live-preview`;
  - the bootstrap endpoints `scrud-studio:explain`, `scrud-studio:new-component-options` and `scrud-studio:new-component` (unchanged);
  - `ui-type-catalog`.
- Produces:
  - **The preview pane** holds:
    - a hidden `<form method="post" target="studio-preview" action="{live-preview url}">` with the CSRF token and a `request` field;
    - `<iframe name="studio-preview" sandbox="allow-scripts" class="studio-preview">`;
    - the controls: a type picker (embed of `ui-type-catalog/all/` in `studio studio-types`, rendering a `<select>`), mode, data context, scope, instance pk and pinned.

    The workbench needs the endpoint URLs; it gets them from the studio computed `ui-branch-status` (add `endpoints: {"live_preview", "explain", "options", "new_component"}` in this task, `reverse()`d).
  - **The inspector** sits under the preview:
    1. On `message` events where `event.source === iframe.contentWindow` and `data.type === "scrud-preview:select"`, it sets `pointer` in the URL state and fetches `explain` for T.
    2. It renders the explanation as text: pointer, terms, winner, and the losers with their outcomes.
    3. "Edit this component" opens the winning component in the editor.
    4. "New rule for this node" shows the bootstrap studio's new-component form fields (from `options`), and POSTs `new-component` as JSON with `fetch` and the CSRF header. On 201 it refreshes everything.

    These actions are absent when T is published.

- [ ] **Step 1: Write the failing tests:**
  - `test_preview_pane_markup`: the iframe has exactly `sandbox="allow-scripts"`, the form targets it, the CSRF token is present, and there's no inline script.
  - `test_type_picker_lists_catalog`
  - `test_endpoints_in_status_reverse_correctly`
  - Node:
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
- Create, in `layer/component/`:
  - `studio:push-preview.json`: the root of `UIPushPreview` in `studio studio-push`;
  - `studio:push.json`: the root of `UIPush` in scope `studio`, a page.
- Modify: `layer/component/studio:status.json`, `layer/stylesheet/studio.json`, `layer/js/studio.json`
- Test: `tests/test_studio_promote.py`

**Interfaces:**
- Consumes: `ui-push-previews` (E3); E1 `after_push`; R3.
- Produces:
  - **Promote in the strip.** For each step of T's chain the user may push to (the parent by default, any ancestor in `chain`), "Promote to <name>…" opens a review panel. The panel:
    - embeds `ui-push-previews/<T>/?target=<name>` (changes with before and after, and conflicts);
    - has "Preview after push", which submits the preview form with `after_push: {"source": T}` and branch = target;
    - has "Apply" and "Propose", R3 fixed-body POSTs to `ui-pushes` with `state` `applied` and `proposed` respectively.

    When there are conflicts, Apply is disabled and the panel says why.
  - **`studio:push` page** for a push:
    - source, target, state, label and result;
    - for an unapplied push, an embed of `ui-push-previews/<source>/?target=<target>`;
    - a state form: a text input with `datalist` `proposed`, `applied`, plus a PUT through the frame. A refused transition shows the server's error, through the runtime's refused-PUT re-render.
  - **The strip's `pushes`** link to these pages, with `?ui-scope=studio`.

- [ ] **Step 1: Write the failing tests:**
  - `test_promote_panel_shows_changes_and_conflicts`
  - `test_apply_push_from_panel_round_trip`: the target changes, and T is emptied.
  - `test_propose_then_apply_from_push_page`
  - `test_refused_transition_shows_server_error`: use a `WorkflowPolicy` that forbids `proposed` → `applied` for the user.
  - `test_apply_disabled_with_conflicts`
  - `test_after_push_preview_request_shape`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): promote with review, propose and apply`).**

---

### Task 8: Roll back and presentations (publishing 3–4)

**Files:**
- Create, in `layer/component/`:
  - `studio:version.json`: the root of `UIBranchVersion` in scope `studio`;
  - `studio:branch-form.json`: the edit component of the `UIBranch` fields, in `studio studio-branch`.
- Modify: `layer/component/studio:status.json`, `layer/component/studio:workbench.json`, `layer/stylesheet/studio.json`
- Test: `tests/test_studio_rollback_presentations.py`

**Interfaces:**
- Consumes: `versions`, `may_publish`, `may_write` (Task 2); R3; R10.
- Produces:
  - **History.** The strip lists `versions`. A version page shows its items. "Restore to this version" is an R3 fixed-body POST of `{"source": null, "target": T, "restore": N, "state": "applied", "rbac_context": <T's context>}` to `ui-pushes`.
  - **Branch settings.** The workbench has a "Branch settings" panel, an embed of the branch itself in `studio studio-branch`. It shows `label`, `description` and `base` (live or pinned), which are editable when `may_write`.
    - `prefix` is an input only when `may_publish`; otherwise it renders as text.
    - Server prefix errors show beside the field through the refused-PUT re-render.
  - **New branch.** A "New branch" form, `data-scrud-json="form"` POST to `ui-branches`, with `name`, `parent` (default T) and `rbac_context` (default T's context).

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
- Produces, in the seed (idempotent, like the rest):
  - a published root branch `studio`, at prefix `/workbench/`, context `ui-studio`;
  - a working branch `studio-wip` under it, context `ui-studio-wip`;
  - grants for `author`: GET on `studio`; write on UI item types and `ui-pushes` in `ui-studio-wip`; push into `studio` (`ui-pushes` in `ui-studio`); GET on `alice-wip`'s tree, as today;
  - `owner` holds the site-level `ui-branches` grant in `""`.
  - It prints the workbench URLs: `/workbench/ui-branches/<alice-wip pk>/?ui-scope=studio` and the `studio-wip` one.
- The demo sidebar's move from `hx-get` to `embed` is the builder's choice; keep it if it's free.

- [ ] **Step 1: Write the failing test: `test_seed_creates_studio_tree_and_grants`.**
- [ ] **Step 2: Run it; expect failure.**
- [ ] **Step 3: Implement; update CLAUDE.md's demo commands to include `seed_todo_demo` and the workbench URL.**
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
    - with `?ui-safe=1`, the full layer workbench renders;
    - `/studio/` (bootstrap) still works, and its revert removes the override.
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
- [ ] **Step 6: Commit (`docs(studio): the self-hosted studio; acceptance`).**
