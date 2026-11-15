# Self-hosted studio, MR 1: runtime growth and studio data — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The runtime features (R1–R10) and studio data and endpoints (E1–E3) that the self-hosted studio needs. Each is useful on its own and documented as a runtime feature. The bootstrap studio is unchanged.

**Architecture:**
- **New runtime features.** They go into `scrud_django/ui/` beside the code they extend:
  - item kinds (`ui-js`);
  - effective resolution (packaged layers, safe mode, drafts);
  - the renderer (URL scope, view mode, list chrome, embeds);
  - `scrud-ui.js` (JSON bodies, array operations).
- **Computed resource types.** They are a new view and registration path in `scrud_django/`, next to `registration.json_resource_type`.
- **Studio data and endpoints.** The studio's computed types and new endpoints live in `scrud_django.ui.studio`. They reuse the existing explain, ranking and push code, never copies of it.

**Tech Stack:** Django 5.2, DRF, scoped-rbac, scrudful, Jinja2 (sandboxed), htmx 2.0.4 (vendored), safetydance 0.5 tests, node for client tests.

**Spec:** `docs/superpowers/specs/2026-11-10-ui-self-hosted-studio-design.md`. This plan covers its "Delivery" item 1: sections "Runtime growth" (R1–R10) and "Studio data and endpoints" (E1–E3), plus the preview parts of "Preview". Read the spec first. Background:
- `docs/ui.md`;
- `docs/superpowers/specs/2026-11-09-ui-branches-design.md`;
- `docs/superpowers/specs/2026-11-10-ui-studio-design.md`.

## Global Constraints

- **Branch and releases.**
  - Work on `feature/ui-self-hosted-studio` in `/Users/davidcharboneau/dev/scrud-django`.
  - Don't bump the version: work merges into `develop`, and releasing goes through a `develop` → `main` merge request (memory: publish only via develop→main).
- **Repo conventions (CLAUDE.md).**
  - safetydance tests, with `@pytest.mark.django_db` above `@test_context`;
  - steps only in `tests/steps.py`;
  - settings only in `tests/settings.py::SETTINGS`;
  - tests pass in any order and alone, and on SQLite, PostgreSQL, MySQL and MariaDB;
  - large-row rule: read content unsorted (`scrud_django.rows`);
  - never edit upstream packages;
  - never weaken an assertion;
  - a real bug not fixed now goes in `docs/follow-ups.md`.
- **Commits** end with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.
- **Verify every task** with `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`. Client-side tasks also run with `SCRUD_TEST_REQUIRE_NODE=1`. The last task also runs `uv run pytest -q --reverse`, the sphinx `-W` build, `uv run python manage.py check` and the migrations check.
- **Fixed IRIs and names.** Don't change any existing `api.openteams.com` IRI. New type URIs follow the existing pattern: `http://api.openteams.com/json-ld/<Name>`. New problem types: `https://api.openteams.com/http-problem/<slug>`.
- **No new authorization logic.**
  - Every new path asks the existing checks (scoped RBAC through the resource views, `decisions_for`, `may_read_in_context`, `selection.selectable`, `check_mount`).
  - A computed type or an embed never returns what the user couldn't GET as JSON.
- **Rendering unchanged unless asked.** With none of the new features in use (no layer installed, no `ui-js`, no `?ui-scope`, `?ui-mode` or `?ui-safe`, no collection rule, no embeds), every existing render test passes byte-identically. Run `tests/test_ui_render.py`, `tests/test_ui_branch_render.py` and `tests/test_ui_builtins.py` unchanged after every task that touches rendering.
- **CSP.** The recommended `script-src 'self'` stays valid: no inline scripts in pages or package templates. The one exception is E1's preview document, which carries its own nonce CSP (Task 11).
- **Docs travel with code.** Each task adds or updates its section of `docs/ui.md` (runtime features under the components and branches chapters; studio API additions under "Studio"). The last task adds the release notes.

## Review Focus

1. **A library upgrade removes a packaged item that a branch overrides.** The override must keep rendering as an ordinary item, and the system check must report it as orphaned. It must never cause a 500 or a silent drop. Test in Task 6.
2. **An embed that points at its own resource, a two-component cycle, a resource the viewer can't read, or a URL with another host or a non-resource path.** Each renders a refusal notice in place of the embed, never the content and never a 500. Test in Task 9.
3. **A draft template that compiles but raises while rendering.** It falls back to the built-in with the usual `<!-- scrud-ui: component "x" failed -->`, as a stored one does. It never makes the preview a 500. Test in Task 11.
4. **Removing or reordering a rule while another tab has saved the same stylesheet.** The array operation sends `If-Match` and gets the 412 reload. It never silently overwrites. Test in Task 4.
5. **Safe mode on a published prefix the viewer may not GET, or with a working-branch cookie.** Safe mode ignores stored items but keeps every gate: `check_mount` still refuses, and an unselectable branch is still refused for JSON. Test in Task 7.

---

### Task 1: JavaScript items (`ui-js`) (R2)

**Files:**
- Create:
  - `scrud_django/ui/documents/UIJs-schema.json` and `UIJs-ld.json`, modelled on `UICss-*` with `js` in place of `css`;
  - `scrud_django/ui/branches/js.py`, modelled on `branches/css.py`.
- Modify:
  - `scrud_django/ui/branches/__init__.py`: add `UI_JS_TYPE_URI = "http://api.openteams.com/json-ld/UIJs"` and `UI_JS_SLUG = "ui-js"`, and add `"js": UI_JS_TYPE_URI` to `ITEM_KINDS`;
  - `scrud_django/ui/types.py`: `ui_js_resource_type()`, plus an entry in `ui_resource_type_urls`;
  - `scrud_django/ui/views.py`: `UIJsViewSet`;
  - `scrud_django/exceptions.py`: `UIJsInvalidError`, 400, type `ui-js-invalid`;
  - `scrud_django/urls.py`: the JS file route;
  - `scrud_django/ui/renderer.py` (`_page`) and `scrud_django/ui/builtins/page.html`: link the scripts.
  - Also grep for every other place that enumerates kinds or special-cases `"css"`. Known places: `UIBranchVersion-schema.json` `items`, `branches/sync.py`, `branches/versions.py`, `branches/checks.py`. Extend each to `js`.
- Test: `tests/test_ui_branch_js.py`.

**Interfaces:**
- Produces:
  - `js_files(selection: Selection | None = None) -> list[tuple[str, str, str]]`, giving `(name, etag, js)` ordered by `order` then name;
  - `js_urls(request) -> list[str]`;
  - `UIJsFileView` at `ui-js-files/<str:name>/<str:etag>.js`, URL name `ui-js-file`. It answers `Content-Type: text/javascript; charset=utf-8`, and its caching and gating are exactly as `UICssFileView`'s.
  - The page component's context gains `scripts: list[str]`.
  - The built-in page links each one as `<script src="…" defer></script>`, after `scrud-ui.js`.
  - `UIJsViewSet` refuses `js` containing `</script` (case-insensitive) with `UIJsInvalidError`, and a duplicate name on a branch with `UIJsNameTakenError` (400, `ui-js-name-taken`), as CSS does.

- [ ] **Step 1: Write the failing tests:**
  - `test_js_item_crud_with_branch_fields`
  - `test_js_files_served_versioned_private_immutable`: caching header `private, max-age=31536000, immutable`, plus `nosniff`, and a stale ETag gives 404.
  - `test_js_gated_by_branch_get_under_prefix`
  - `test_page_links_scripts_in_order`
  - `test_js_refuses_closing_script_tag`
  - `test_js_write_needs_ui_js_grant_not_ui_components`: a user with POST on `ui-components` only, in the branch context, gets 403 on `ui-js`.
  - `test_push_pull_restore_and_conflicts_cover_js`: reuse the helpers in `test_ui_branch_push.py`.
- [ ] **Step 2: Run `uv run pytest tests/test_ui_branch_js.py`; expect failures (`NoReverseMatch` / 404).**
- [ ] **Step 3: Implement as in Interfaces; add a "JavaScript items" section to `docs/ui.md`, including the security paragraph from the spec's "Security".**
- [ ] **Step 4: Run the new tests, then the full suite; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): ui-js items served at versioned URLs`).**

---

### Task 2: Scope chain in the URL and a requested view mode (R4, R10)

**Files:**
- Modify: `scrud_django/ui/renderer.py`, `scrud_django/ui/render.py` (collection items get the mode too)
- Test: `tests/test_ui_url_scope_mode.py`

**Interfaces:**
- Produces:
  - `SCOPE_PARAMETER = "ui-scope"`. `scope_chain_of(request)` reads `SCOPE_HEADER` first, else `?ui-scope=`. The value is space- or comma-separated, and the same validation applies: invalid gives `()`.
  - `MODE_HEADER = "Scrud-UI-Mode"`, `MODE_PARAMETER = "ui-mode"`, and `requested_mode_of(request) -> str | None`.
    - The header wins over the parameter.
    - The only valid value is `"view"`. Absent gives `None`.
    - Any other value is refused in `check_ui_headers` with `UIHeaderInvalidError` (400), before a write.
  - `_body` passes `mode="view"` to `render_resource` and to `render_collection` (which passes it to each item) when `requested_mode_of` is `"view"`.
  - `VARY` gains `Scrud-UI-Mode`.

- [ ] **Step 1: Write the failing tests:**
  - `test_url_scope_equals_header`
  - `test_header_scope_wins`
  - `test_invalid_url_scope_ignored_like_header`: covers 17 names and a bad name.
  - `test_view_mode_downgrades_a_writer`: a user with PUT gets no `hx-put` and no form.
  - `test_view_mode_never_upgrades_a_reader`: `?ui-mode=edit` gives 400; a reader with `?ui-mode=view` is unchanged.
  - `test_view_mode_applies_to_list_items`
  - `test_mode_header_in_vary`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document both in `docs/ui.md` "Fragments".**
- [ ] **Step 4: Run them and the unchanged render tests; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): scope chain in the URL and a requested view mode`).**

---

### Task 3: List chrome through the cascade (R5)

**Files:**
- Modify: `scrud_django/ui/render.py` (`render_collection`)
- Test: `tests/test_ui_collection_cascade.py`

**Interfaces:**
- Produces: the collection wrapper resolved with `resolve_component(effective_rules([]), scope_chain, terms, "view")`.
  - The terms are, most specific first:
    1. `TypeTerm(iri=collection_type_uri_for(resource_type.type_uri))`, from `scrud_django.collection_type_uri_for`;
    2. `TypeTerm(iri="scrud:collection")`.
  - With no matching rule, or a matching component that fails to render, `scrud:collection` is used as today. A failure is logged and adds the `<!-- scrud-ui: component "x" failed -->` comment, as in `render.py`'s node fallback.
  - The component gets today's context (`children`, `count`, `next`, `previous`, `label`) plus `href` and `scope` (the chain as a list).
  - The outer `.scrud-collection-frame` div is unchanged.

- [ ] **Step 1: Write the failing tests:**
  - `test_list_unchanged_without_rule`: the HTML is identical to before.
  - `test_rule_on_collection_iri_restyles_one_type_list`: another type's list is unchanged.
  - `test_scoped_collection_rule`: applies only with `?ui-scope=sidebar`.
  - `test_failing_collection_component_falls_back`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document in `docs/ui.md` (selectors: the collection term).**
- [ ] **Step 4: Run them and the unchanged render tests; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): list chrome through the cascade`).**

---

### Task 4: Editing arrays of objects (R6)

**Files:**
- Modify:
  - `scrud_django/ui/builtins/array-edit.html`
  - `scrud_django/ui/render.py` (the array context)
  - `scrud_django/ui/static/scrud_ui/scrud-ui.js`
  - `scrud_django/ui/views.py` (selector errors carry their field)
  - `scrud_django/exceptions.py` (`UISelectorInvalidError` gains the field)
- Test: `tests/test_ui_array_edit.py` (server); `tests/test_ui_client.py` (node)

**Interfaces:**
- Produces:
  - **Item controls.** In edit mode, each array item of the built-in `array-edit` has:
    - `<button type="button" data-scrud-array-op="remove" data-scrud-pointer="{item pointer}">Remove</button>`;
    - Move up and Move down buttons with `data-scrud-array-op="move"`, `data-scrud-pointer="{item pointer}"` and `data-scrud-to="{index}"`. They are left out at the ends.

    The item pointers come from the existing `child_items` context.
  - **Applying an operation.** On click, `scrud-ui.js`:
    1. builds the frame's body as for a PUT (inputs applied at their current, pre-operation pointers);
    2. applies the operation (remove index; move from index to `to`) with `applyArrayOp(body, op) -> body`, exported on `window.ScrudUI`;
    3. sends the frame's PUT with `If-Match`, through the same path as a change.
  - **Unchanged.** The append row for object items (`append_fields`) stays as it is. Arrays of property-less objects and arrays of arrays still get no append row (commits `f613adb`, `d2adc2d`).
  - **Selector errors.** A selector error carries the field `rules[<i>].select`, in the shape `_field_errors` already maps to a pointer (`/rules/<i>/select`). The refused PUT's re-render then shows it beside that rule.

- [ ] **Step 1: Write the failing tests:**
  - Server:
    - `test_array_items_get_remove_and_move_controls`: there's no Move up on the first item and no Move down on the last.
    - `test_view_mode_has_no_array_controls`
    - `test_bad_selector_error_lands_on_its_rule`: a PUT of a stylesheet whose `rules[1].select` is invalid re-renders with the error inside the `/rules/1` item.
  - Client:
    - `test_apply_array_op_remove_and_move`: pure `applyArrayOp`.
    - `test_array_op_sends_put_with_if_match`
    - `test_array_op_after_stale_etag_gets_reload` (Review Focus 4): a 412 response swaps in the reload notice.
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document under `docs/ui.md` "Edit mode".**
- [ ] **Step 4: Run them (node required) and the unchanged render tests; expect PASS.** Phase-1 builtin tests that snapshot `array-edit` change. Update them with explicit new assertions for the controls and say why in the commit message. Don't loosen them.
- [ ] **Step 5: Lint, then commit (`feat(ui): remove and reorder array items in edit mode`).**

---

### Task 5: Packaged UI layers: loading, resolution, ranking, names (R1, part 1)

**Files:**
- Create: `scrud_django/ui/layers.py`
- Modify:
  - `scrud_django/ui/branches/effective.py` (`effective_rows`, `_rows_for`, `parent_state`)
  - `scrud_django/ui/cascade.py` (`effective_rules` rank of layer stylesheets)
  - `scrud_django/ui/views.py` (name rules)
  - the three item schemas and `UIJs-schema.json` (name pattern)
  - `scrud_django/exceptions.py`
- Test: `tests/test_ui_layers.py`, plus a test layer package `tests/ui_layer/` with an app config in `tests/apps.py`, added to `tests/settings.py` INSTALLED_APPS.

**Interfaces:**
- Produces:
  - **Declaring a layer.** An app declares a layer with an `AppConfig` attribute: `scrud_ui_layer = {"prefix": "<name>:", "package": "<dotted package>"}`. Layers are read from `apps.get_app_configs()` in `INSTALLED_APPS` order.
  - **Layer files.** A layer package holds `component/*.json`, `stylesheet/*.json`, `css/*.json` and `js/*.json`. Each file is one item's content without branch fields.
  - **The layer API**, in `layers.py`:
    - `@dataclass(frozen=True) class UILayer: label: str; prefix: str; package: str`
    - `installed_layers() -> tuple[UILayer, ...]`, cached and cleared on `setting_changed`
    - `layer_rows(type_uri: str) -> list[tuple[None, str, dict]]`, giving `(None, etag, content)`. `content["branch"] == LAYER_BRANCH_NAME` and `content["rbac_context"] == ""`. The etag is `sha256` hex of the canonical JSON (sorted keys, no spaces) of the file's content.
    - `layer_item(kind: str, key: tuple) -> tuple[str, dict] | None`
    - `LAYER_BRANCH_NAME = "@layers"`. It can't collide with a branch, whose names match `^[a-z0-9]`.
    - `LAYER_BRANCH = Branch(name=LAYER_BRANCH_NAME, context="", parent=None, base="live", branching_point=MappingProxyType({}), prefix=None, pk=None)`
  - **Validation.** A layer item whose name doesn't start with its layer's prefix, a stylesheet with a non-empty `applies_to`, or an unreadable file is a system check Error, `scrud_django_ui.E002`, and is left out at runtime.
  - **Resolution.** `effective_rows` and `parent_state` append `LAYER_BRANCH` to the end of every chain and `layer_rows(type_uri)` to the rows, so a layer item is the bottom of every chain, beneath every root, and pinned branches see it too.
  - **Ranking.** Layer stylesheets rank `(0, 1, layer_index, order, i)`: after the default stylesheet's `(0, 0, i)` and before every stored stylesheet's `(1, …)`. Their `RuleSource` has `pk=None`, `branch=LAYER_BRANCH_NAME` and `content` set, so they're readable.
    - An override of a layer stylesheet on a branch is a stored row and ranks as one. Document this.
  - **Names.**
    - The name pattern becomes `^(?!scrud:)(?:[a-z][a-z0-9-]*:)?[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
    - A write whose name has a prefix that isn't an installed layer's, or that names no existing layer item of that kind (and `applies_to`, for a stylesheet), is refused with `UILayerNameReservedError` (400, `ui-layer-name-reserved`).
  - **Lists unchanged.** Layer items never appear in item lists or details (spec: "Not in resource lists").

- [ ] **Step 1: Write the failing tests:**
  - `test_layer_items_render_beneath_main`: a layer rule and component style a type with no stored items.
  - `test_branch_overrides_layer_item_by_name`
  - `test_deleting_override_reverts_to_layer`
  - `test_layer_stylesheet_ranks_between_default_and_stored`: use `rank_rules` directly.
  - `test_new_item_cannot_use_layer_prefix`
  - `test_scrud_prefix_still_reserved`
  - `test_unknown_prefix_refused`
  - `test_layer_items_not_in_lists`
  - `test_layer_check_reports_bad_files`: `E002`.
  - `test_pinned_branch_sees_layer`
  - `test_no_layer_installed_changes_nothing`: run with the test layer removed from settings.
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement as in Interfaces; add a "Packaged UI layers" section to `docs/ui.md`.**
- [ ] **Step 4: Run them, the cascade tests and the unchanged render tests; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): packaged, overridable UI layers`).**

---

### Task 6: Layer overrides: base, layer conflicts, rebase on roots (R1, part 2)

**Files:**
- Modify:
  - `scrud_django/ui/views.py` (`base_of`, `validate_rebase`)
  - `scrud_django/ui/branches/sync.py` (`conflicts`, push and restore treat the layer as the bottom of the chain)
  - `scrud_django/ui/layers.py`
  - `scrud_django/ui/branches/checks.py`
  - `scrud_django/ui/apps.py` (register the new check)
- Test: `tests/test_ui_layer_conflicts.py`

**Interfaces:**
- Consumes: `layer_item`, `LAYER_BRANCH` (Task 5).
- Produces:
  - **`base_of`.** On a root branch, an override's `base` is the layer item's etag, and `None` when there is none. On a child branch, `parent_state` already includes the layer (Task 5), so `base` is the layer's etag when no ancestor overrides the key.
  - **`layer_conflicts(branch: Branch, using=None) -> list[dict]`**, in `layers.py`. It returns `{"kind", "key", "base", "current"}` for each own row of `branch` whose nearest provider beneath it is the layer, and whose `base` differs from the layer item's current etag. When the layer item is gone, `current` is `None`: an orphaned override.
  - **`rebase: true`.** Now also valid on a root branch for an item that overrides a layer item. It moves `base` to the current layer etag. Everywhere else it is unchanged.
  - **Push.** Push and restore compute `_parent_etags` and conflicts with the layer at the bottom. A push into a root of an override of a layer item records that layer etag as its `base`.
  - **System check.** A database check, `scrud_django_ui.W002`, lists layer conflicts and orphaned overrides per branch, like `W001`.
  - **Orphans.** An orphaned override keeps rendering as an ordinary item.

- [ ] **Step 1: Write the failing tests:**
  - `test_root_override_records_layer_etag`
  - `test_child_override_records_layer_etag_when_no_ancestor_overrides`
  - `test_changed_layer_item_is_a_layer_conflict_on_main_and_on_a_child`: swap the test layer's file content with a fixture that reloads `installed_layers`.
  - `test_rebase_on_root_resolves_layer_conflict`
  - `test_revert_resolves_layer_conflict`
  - `test_removed_layer_item_leaves_orphan_rendering_and_reported` (Review Focus 1)
  - `test_w002_lists_layer_conflicts`
  - `test_push_override_of_layer_item_into_root`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; extend `docs/ui.md` "Packaged UI layers" with conflicts and upgrades.**
- [ ] **Step 4: Run them and every `tests/test_ui_branch_*` module; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): layer conflicts and overrides on root branches`).**

---

### Task 7: Safe mode (R7)

**Files:**
- Modify:
  - `scrud_django/ui/branches/effective.py`: `using_safe_mode()` and the `safe_mode()` contextvar;
  - `scrud_django/ui/renderer.py`: the flag, and a banner line in `_page` chrome;
  - `scrud_django/ui/branches/css.py` and `js.py`: in safe mode, file lists hold only layer items;
  - `scrud_django/ui/branches/chrome.py`.
- Test: `tests/test_ui_safe_mode.py`

**Interfaces:**
- Produces:
  - `SAFE_PARAMETER = "ui-safe"`; `?ui-safe=1` turns it on, and any other value is ignored.
  - Inside `using_safe_mode()`, `effective_rows` returns the layer rows only, on the chain `[LAYER_BRANCH]`. The page then uses packaged layers, built-ins and the default stylesheet only.
  - `choose()`, `check_mount` and authorization run exactly as without the flag.
  - The page chrome gains a server-rendered notice, `<div class="scrud-safe-mode" role="status">Safe mode: this page ignores stored UI items.</div>`. The `_page` banner check covers it like the branch banner.
  - CSS and JS URLs in safe mode carry `?ui-safe=1`, so the file views serve the layer's items.
  - `VARY`-relevant: the flag is a query parameter, so no new header.

- [ ] **Step 1: Write the failing tests:**
  - `test_safe_mode_ignores_stored_items_on_every_branch`
  - `test_safe_mode_uses_layer_items`
  - `test_safe_mode_keeps_mount_gate` (Review Focus 5)
  - `test_safe_mode_keeps_branch_refusal_for_json` (Review Focus 5)
  - `test_safe_mode_notice_present_and_required`
  - `test_safe_mode_css_and_js_are_layer_only`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document a "Safe mode" section in `docs/ui.md`.**
- [ ] **Step 4: Run them and the unchanged render tests; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): safe mode renders from packaged layers only`).**

---

### Task 8: Computed resource types (R8)

**Files:**
- Create: `scrud_django/computed.py`
- Modify:
  - `scrud_django/registration.py`: `computed_resource_type(...)`;
  - `scrud_django/ui/renderer.py`: `_body` uses view-only decisions for computed views.
- Test: `tests/test_computed_types.py`, with a test type in `tests/resource_types.py` that echoes the request user's readable to-do lists.

**Interfaces:**
- Produces:
  - **The base class:**

    ```python
    class ComputedResourceViewSet(ResourceViewMixin, viewsets.ViewSet):
        computed = True
        http_method_names = ["get", "head", "options"]
        permission_classes = [IsAuthenticated]
        def compute(self, request, key: str) -> Mapping: ...  # subclasses; may raise Http404 / PermissionDenied
        def retrieve(self, request, slug: str) -> Response: ...
    ```

    - `retrieve` answers the computed content with `ETag: "<sha256 hex of canonical JSON>"`, and 304 for a matching `If-None-Match`.
    - Any other method gives 405 with the uniform problem body.
  - **Registration:** `registration.computed_resource_type(resource_type_uri, slug, schema_func, context_func, viewset_class, register_service=True) -> list[URLPattern]`.
    - It registers the `ResourceType`, so published-branch prefixes claim its slug, and it goes through `register_or_defer`.
    - It routes `f"{slug}/<str:slug>/"` only: the type's slug, then the key in the URL keyword `slug` (`ResourceViewMixin.lookup_url_kwarg`), which `retrieve` passes to `compute` as `key`. There's no list route, and the type adds no reversion.
  - **HTML.** For a view with `computed = True`, `_body` renders with decisions `{"GET": "allowed", "PUT": "denied", "DELETE": "denied"}` and contexts from the content's `rbac_context` if present, else `[]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_computed_type_json_and_etag_304`
  - `test_computed_type_renders_through_cascade`: a rule on its type IRI selects a stored component.
  - `test_computed_type_refuses_writes_405`
  - `test_computed_type_shows_only_readable`: two users with different grants get different content.
  - `test_computed_type_under_published_prefix`
  - `test_anonymous_refused`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document "Computed resource types" in `README.md`'s registration section and `docs/ui.md`.**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat: computed, read-only resource types`).**

---

### Task 9: Server-side embeds (R9)

**Files:**
- Create: `scrud_django/ui/dispatch.py` (moved from `scrud_django/ui/studio/dispatch.py`, which re-exports `call_view`), `scrud_django/ui/embed.py`
- Modify: `scrud_django/ui/render.py` (Jinja node and page contexts get `embed`), `scrud_django/ui/renderer.py` (`_page` context), `scrud_django/ui/engines.py` if the sandbox needs the callable allowed
- Test: `tests/test_ui_embeds.py`

**Interfaces:**
- Consumes: `call_view` (moved, unchanged behaviour); `requested_mode_of` / `MODE_HEADER` (Task 2).
- Produces:
  - **Dispatching a path.** `dispatch_path(request, path: str, *, headers: dict) -> Response` in `ui/dispatch.py`. It resolves an absolute path against the URLconf, mapping a path under the current mount prefix back to the API as `PublishedBranchMiddleware` does, and runs the view as the same user with `Accept: text/html`.
  - **The template callable.** `embed(path: str, scope: str | list = (), mode: str | None = None) -> Markup`. It is available to stored **Jinja** components (node and page contexts) only; the Django engine gets no embeds. It dispatches a GET with:
    - `HX-Request: true` (a fragment);
    - `Scrud-UI-Scope`;
    - `Scrud-UI-Mode` when `mode == "view"`;
    - `Scrud-UI-Branch`, the active selection's value.

    It returns the response's HTML. A status ≥ 400 returns that response's own rendered notice.
  - **Refusals.** These render `scrud:notice` with "This embed was refused.":
    - a path that isn't absolute, or has a scheme or host;
    - a path that resolves to a view that isn't a `ResourceViewMixin` subclass (computed types included);
    - a path and scope already on the embed stack (a cycle);
    - depth over `MAX_EMBED_DEPTH = 3`;
    - more than `MAX_EMBEDS = 20` per top-level response.

    Each refusal is logged at warning.
  - **Context carried over.** Embeds inside a preview (`preview=True`) render in preview mode. Embeds inside safe mode stay in safe mode. Both are carried by contextvars, not headers.
  - **The demo's sidebar** may move from `hx-get` to `embed`; that move is optional and belongs in MR 2.

- [ ] **Step 1: Write the failing tests:**
  - `test_embed_renders_resource_with_viewers_rights`
  - `test_embed_of_unreadable_resource_shows_refusal_not_content` (Review Focus 2)
  - `test_embed_cycle_refused` (Review Focus 2): a self-embed, and two components embedding each other.
  - `test_embed_foreign_host_and_non_resource_path_refused` (Review Focus 2)
  - `test_embed_limits`
  - `test_embed_uses_embedding_branch`
  - `test_embed_in_preview_has_no_write_attributes`
  - `test_embed_view_mode`
  - `test_django_engine_has_no_embed`
  - `test_studio_dispatch_still_works`: the existing `tests/test_studio_dispatch.py` passes unchanged.
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document "Embeds" in `docs/ui.md`, including the limits.**
- [ ] **Step 4: Run them, the studio tests and the unchanged render tests; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): server-side embeds of resources`).**

---

### Task 10: The browser sends JSON for any method (R3)

**Files:**
- Modify: `scrud_django/ui/static/scrud_ui/scrud-ui.js`
- Test: `tests/test_ui_client.py` (node); `tests/test_ui_json_bodies.py` (server round trip through rendered markup)

**Interfaces:**
- Produces:
  - **Which elements send JSON.** The `scrud-json` extension encodes any element that has `hx-ext="scrud-json"` and a `data-scrud-json` attribute, for any method:
    - **`data-scrud-json="form"`:** builds a body from the element's inputs named by JSON Pointer onto `{}`. It applies the same rules as `buildBody`: `/-` appends, and types come from the `data-scrud-type` attribute or the input's type. This is how a create is sent (`hx-post` to a collection).
    - **`data-scrud-json="fixed"`:** sends the JSON in the element's `data-scrud-body` attribute, which must parse, unchanged.
  - **Headers.** These requests send `Content-Type: application/json`, `X-CSRFToken`, `Scrud-UI-Branch`, and `If-Match` when the element or its frame has `data-scrud-etag`.
  - **The existing PUT path** is unchanged.
  - **Bad JSON.** An unparsable `data-scrud-body` cancels the request and shows the frame notice, "This action is misconfigured."

- [ ] **Step 1: Write the failing tests:**
  - Client:
    - `test_form_json_post_builds_body_from_pointers`
    - `test_fixed_json_post_sends_body_unchanged`
    - `test_json_delete_with_if_match`
    - `test_bad_fixed_body_cancels_with_notice`
    - `test_existing_put_unchanged`
  - Server: `test_create_through_rendered_form_markup_returns_201_html`.
- [ ] **Step 2: Run them (node required); expect failures.**
- [ ] **Step 3: Implement; document under `docs/ui.md` "Writes from the browser".**
- [ ] **Step 4: Run them; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(ui): JSON bodies for create, fixed-body and delete requests`).**

---

### Task 11: Preview with drafts, match markers and an opaque origin (E1)

**Files:**
- Create:
  - `scrud_django/ui/studio/drafts.py`
  - `scrud_django/ui/studio/live_preview.py`
  - `scrud_django/ui/studio/templates/scrud_studio/live_preview.html`
  - `scrud_django/ui/studio/static/scrud_studio/preview-bridge.js`
- Modify:
  - `scrud_django/ui/branches/effective.py`: `using_drafts(...)`, overlaid in `effective_rows`;
  - `scrud_django/ui/render.py`: the `mark` option in preview mode;
  - `scrud_django/ui/studio/urls.py` and `views.py`.
- Test: `tests/test_studio_live_preview.py`; node test for the bridge in `tests/test_studio_client.py`

**Interfaces:**
- Consumes: `rank_rules`, `explain`'s outcome words (`studio/explain.py`), `selection.selectable`, `call_view`.
- Produces:
  - **Drafts:**
    - `@dataclass(frozen=True) class Draft: kind: str; content: dict`
    - `validate_draft(request, branch: Branch, draft: Draft) -> list[str]`. It runs the item viewset's own content validation (`validate_content` of the matching `UI*ViewSet`, without saving) plus the write check for the override or own row on `branch`.
    - `using_drafts(branch: str, drafts: list[Draft])`. Inside it, `effective_rows` treats each draft as T's own row for its key: `(None, "draft:<sha256>", content | {"branch": branch})`. Embeds (Task 9) see the drafts, since they run in-process.
  - **The endpoint.** `POST studio/live-preview`, URL name `scrud-studio:live-preview`.
    - **Request.** A form-encoded body with one field, `request`, holding JSON:

      ```
      {"type": slug, "branch": selection value, "mode": "view"|"edit",
       "scope": [str], "context": str, "instance": pk|null,
       "drafts": [{"kind", "content"}],
       "mark": {"stylesheet": name, "applies_to": str, "index": int} | null,
       "after_push": {"source": branch name} | null}
      ```

      It's form-encoded so a `<form target=iframe>` can submit it with the CSRF token; this isn't a resource view, so the JSON-only rule doesn't apply.
    - **Response.** The preview document: the frame rendered with `preview=True`, T's CSS and JS inlined under a per-response nonce, and `preview-bridge.js` loaded by URL. Headers:
      - `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; script-src 'self' 'nonce-<n>'; style-src 'self' 'nonce-<n>'; img-src 'self' data:; connect-src 'self'`
      - `X-Frame-Options: SAMEORIGIN`
    - **Draft validation.** A draft that fails `validate_draft` is left out of the render. Its errors go into `<script type="application/json" id="scrud-preview-errors" nonce>` as `[{"kind", "name", "messages"}]`.
    - **Refusals**, each with the existing problem types:
      - drafts on a published T: 400, `ui-studio-branch-read-only`;
      - `after_push` with any other drafts: 400, `ui-studio-preview-invalid`, a new problem type;
      - `after_push` without GET on the source: 403;
      - a branch that isn't selectable: 400, `ui-branch-unavailable`.
    - **After push.** `after_push` turns the source's own stored rows into drafts on T (the target). It needs GET on both, and no write.
    - **Markers.** With `mark`, each `<scrud-node>` whose ranked rules include that rule gets `data-scrud-match="wins"` when it is the winner, else `data-scrud-match="loses"`. The rule is identified by its source (stylesheet name, applies_to, branch as resolved on T) and its index, the last element of its rank. The winner is computed with `rank_rules`, exactly as `resolve_component` does it.
  - **The bridge.** `preview-bridge.js` posts `{"type": "scrud-preview:select", "pointer": str}` to `window.parent` on a click inside a `<scrud-node>`, using the nearest one. On load it posts `{"type": "scrud-preview:errors", "errors": […]}`. The target origin is `"*"`, because the frame's origin is opaque; the receiving side checks `event.source`.
  - **Unchanged.** The existing GET `preview` endpoint (bootstrap studio) stays as it is.

- [ ] **Step 1: Write the failing tests:**
  - `test_drafts_render_and_are_never_stored`
  - `test_draft_user_could_not_save_is_refused`: the render ignores it, and the errors list says why.
  - `test_invalid_draft_reports_save_errors`: a template that doesn't compile, and a bad selector.
  - `test_draft_raising_at_render_falls_back` (Review Focus 3)
  - `test_drafts_on_published_branch_refused`
  - `test_after_push_needs_get_only_and_shows_source_overrides`
  - `test_after_push_with_drafts_refused`
  - `test_match_markers_agree_with_resolve_component`: a parametrised table of scopes, terms, ranks, a missing component and the wrong mode, asserting `wins` exactly where `resolve_component` picks the rule.
  - `test_preview_csp_is_opaque_and_nonce_only`: no `allow-same-origin`, and every inline script and style carries the nonce.
  - `test_embeds_in_live_preview_compose_server_side`
  - node: `test_bridge_posts_select_and_errors`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document `live-preview` in `docs/ui.md` "Studio" (the studio API), with the CSP reasoning.**
- [ ] **Step 4: Run them and every `tests/test_studio_*` module; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): live preview with drafts, match markers and an opaque origin`).**

---

### Task 12: Studio data as computed resources (E2)

**Files:**
- Create:
  - `scrud_django/ui/studio/computed.py`
  - `scrud_django/ui/studio/documents/`, holding `UIBranchItems`, `UIBranchStatus`, `UITypeCatalog` and `UISelectableBranches` (`-schema.json` and `-ld.json` each)
- Modify:
  - `scrud_django/urls.py`: when `scrud_django.ui.studio` is installed, extend with `studio_computed_type_urls()`;
  - `scrud_django/ui/studio/page.py`: factor out the reusable parts of `own_items`, `branch_conflicts`, `css_items` and `browse_url` so both use one implementation.
- Test: `tests/test_studio_computed.py`

**Interfaces:**
- Consumes: `ComputedResourceViewSet`, `computed_resource_type` (Task 8); `layer_conflicts`, `LAYER_BRANCH_NAME` (Tasks 5–6); `effective_rows`, `conflicts`, `selection.selectable`, `may_read_in_context`.
- Produces four types, each `http://api.openteams.com/json-ld/<Name>`:
  - **`ui-branch-items/<branch>/`**, giving:

    ```
    {"branch", "items": [{"kind", "name", "applies_to"?, "origin": "own"|"inherited"|"packaged",
     "from": str|null, "href": str|null, "etag", "content"?, "conflict": null|"parent"|"layer"}]}
    ```

    - It needs the branch to be `selectable`, else 404.
    - Readable rows carry `content` and `href`. A row the user can't GET is left out entirely. Packaged items always carry content.
  - **`ui-branch-status/<branch>/`**, giving:

    ```
    {"branch", "published": bool, "chain": [{"name", "published", "prefix"}],
     "changed": [...], "conflicts": [...], "browse_url": str|null}
    ```

    Conflicts are the ones the user may read, plus layer conflicts.
  - **`ui-type-catalog/all/`**: `{"types": [{"slug", "title", "type_uri"}]}`, unfiltered, as the bootstrap studio lists them.
  - **`ui-selectable-branches/mine/`**: `{"branches": [{"name", "published", "href"}]}` for the branches the user may select.

- [ ] **Step 1: Write the failing tests:**
  - `test_branch_items_marks_own_inherited_packaged`
  - `test_branch_items_hides_unreadable_rows`
  - `test_branch_items_reports_parent_and_layer_conflicts`
  - `test_unselectable_branch_404`
  - `test_branch_status_chain_and_browse_url`
  - `test_type_catalog_matches_bootstrap_list`
  - `test_selectable_branches_per_user`
  - `test_studio_types_render_as_html`
  - `test_bootstrap_strip_unchanged`: the existing `tests/test_studio_page.py` passes.
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document the four types in `docs/ui.md` "Studio".**
- [ ] **Step 4: Run them and every `tests/test_studio_*` module; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): branch items, status, catalog and branches as computed resources`).**

---

### Task 13: What a push will change (E3)

**Files:**
- Modify:
  - `scrud_django/ui/branches/sync.py`: split `apply_push` into `plan_push(request, source, target, using) -> PushPlan`, which checks and computes without writing, and the existing apply, which writes the plan;
  - `scrud_django/ui/studio/computed.py`.
- Create: `scrud_django/ui/studio/documents/UIPushPreview-schema.json` and `UIPushPreview-ld.json`
- Test: `tests/test_studio_push_preview.py`

**Interfaces:**
- Produces:
  - **The plan:**
    - `@dataclass(frozen=True) class PushPlan: writes: list[_Write]; emptied: list[_Write]; conflicts: list[dict]; hidden_conflicts: int`
    - `plan_push` raises nothing for conflicts; `apply_push` raises `UIPushConflictError` from the plan as today. Its behaviour is unchanged, and `tests/test_ui_branch_push.py` passes unchanged.
  - **The computed type:** `ui-push-previews/<source>/?target=<name>`. The target defaults to the source's parent. It gives:

    ```
    {"source", "target", "changes": [{"kind", "key", "action": "create"|"update"|"delete",
     "before": dict|null, "after": dict|null}], "conflicts": [...]}
    ```

    - It needs GET on both branches, else 403.
    - It never writes.
    - Items the user can't GET in the target appear without `before`/`after`.

- [ ] **Step 1: Write the failing tests:**
  - `test_push_preview_matches_what_apply_push_writes`: preview, then apply, then compare the target's rows with the `after` values.
  - `test_push_preview_lists_conflicts_readable_only`
  - `test_push_preview_needs_get_on_both`
  - `test_push_preview_writes_nothing`
  - `test_push_preview_default_target_is_parent`
  - `test_push_of_layer_override_previewed`
- [ ] **Step 2: Run them; expect failures.**
- [ ] **Step 3: Implement; document in `docs/ui.md` "Studio".**
- [ ] **Step 4: Run them and `tests/test_ui_branch_push.py`; expect PASS.**
- [ ] **Step 5: Lint, then commit (`feat(studio): preview what a push will change`).**

---

### Task 14: Release notes, follow-ups, full verification

**Files:**
- Modify:
  - `docs/release-notes.md` (Unreleased): each runtime feature, then the studio additions;
  - `docs/follow-ups.md`: anything deferred, with anchors;
  - `docs/ui.md`: a short "Self-hosted studio (coming)" pointer to the spec;
  - `CLAUDE.md`: only if a new command or gotcha was learned. Add the demo `seed_todo_demo` step, which the branch reader found missing.
- Test: none new. This task is verification.

- [ ] **Step 1: Write the notes and docs; build with `uv run --group docs sphinx-build -W -b html docs docs/_build`.**
- [ ] **Step 2: Run `uv run pytest -q`, then `uv run pytest -q --reverse`, with `SCRUD_TEST_REQUIRE_NODE=1`; both pass.**
- [ ] **Step 3: Run the suite on PostgreSQL and MySQL in throwaway containers (CLAUDE.md commands), and remove the containers. Record the results. MariaDB runs in CI.**
- [ ] **Step 4: Run `uv run python manage.py check` and `uv run python manage.py makemigrations scrud_django --check --dry-run`; both are clean.**
- [ ] **Step 5: Smoke-test the demo, and record the results in the report:**
  - `migrate`, `seed_todo_demo` and `runserver` start cleanly;
  - `/studio/` still works;
  - `/api/ui-branch-items/main/` renders as HTML;
  - a page with `?ui-safe=1` shows the safe-mode notice.
- [ ] **Step 6: Commit (`docs: self-hosted studio runtime — release notes and follow-ups`).**
