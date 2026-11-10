# UI branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Branches of the UI presentation (components, component stylesheets
and CSS). A branch is a union over a live or pinned base, has a version
history, and is pushed and pulled. Some branches are published at URL
prefixes. Authorization is scoped RBAC only.

**Architecture:**
- **Branches, versions and pushes are ordinary scrud resource types**
  registered by `scrud_django.ui`, as UI components already are. There are no
  new Django models and no migrations.
- **Items stay `Resource` rows** of their type. Each row carries the branch it
  belongs to (`branch`) and the parent item it overrode (`base`). Its
  `rbac_context` must equal the item's *authorizing context*, so scoped RBAC
  authorizes every item write natively.
- **Rendering** keeps reading every row of a type once per response
  (`cascade._stored`) and filters them in Python to the active branch's
  *effective state*. `main` with no other branch costs no extra query.
- **Published prefixes** are a middleware that rewrites `path_info` to the
  canonical `/api/` route, so the same view and the same checks serve every
  prefix.

**Tech Stack:** Django 5.2, DRF, scoped-rbac, scrudful, safetydance 0.5 tests,
htmx 2.0.4 with `scrud-ui.js`.

**Spec:** `docs/superpowers/specs/2026-11-09-ui-branches-design.md`. Read it
first; this plan argues from it. Phase 1 context: `docs/ui.md` and
`docs/superpowers/specs/2026-11-09-ui-components-design.md`.

## Global Constraints

- **Base branch:** work on `feature/ui-branches` (off `feature/ui-components`,
  MR !8). Version `0.17.0` in `pyproject.toml` (minor).
- **New type IRIs and slugs** (new IRIs only; never change existing
  `api.openteams.com` IRIs):
  - `http://api.openteams.com/json-ld/UIBranch`, slug `ui-branches`;
  - `http://api.openteams.com/json-ld/UIBranchVersion`, slug `ui-branch-versions`;
  - `http://api.openteams.com/json-ld/UIPush`, slug `ui-pushes`;
  - `http://api.openteams.com/json-ld/UICss`, slug `ui-css`.
- **Names fixed by this plan:**
  - header `Scrud-UI-Branch`, query parameter `ui-branch`, confirmation
    parameter `ui-branch-confirm=1`, cookie `scrud_ui_branch`;
  - a selection value is `<name>` or `<name>@pinned`;
  - the published branch at the API URLs has `prefix: ""`; the upgrade's
    branch is named `main`.
- **Branch names** match `^[a-z0-9][a-z0-9._-]{0,63}$`. **Prefixes** are `""` or
  match `^/(?:[a-z0-9][a-z0-9_-]*/)*$`.
- **Authorizing context** of an item on branch `B` (spec, "Authorization on
  branches"):
  - on a working branch, `B`'s context;
  - on a published branch, a component or CSS item uses `B`'s context;
  - a stylesheet whose `applies_to` is `""` uses `B`'s context;
  - a stylesheet that applies to `X` uses `X`.
- **No extra queries** for a request with no prefix and no selection. The
  `tests/test_query_budget.py` and phase-1 render tests must pass unchanged,
  apart from the `Vary` assertion in Task 8.
- **Every write goes through the ordinary scrud write path.** Server-side
  writes of items (push, pull, restore) use `ResourceRegistration` inside the
  request's transaction. Only the per-item grant checks of Task 7 are new
  authorization code.
- **Repo conventions** (`CLAUDE.md`):
  - safetydance Given/When/Then tests;
  - steps live only in `tests/steps.py`;
  - `@pytest.mark.django_db` goes above `@test_context`;
  - settings live only in `tests/settings.py::SETTINGS`;
  - code must work on SQLite, PostgreSQL, MySQL and MariaDB. JSON lookups are
    limited to the `content__<key>=` equality that phase 1 already uses.
- **Verify every task** with `uv run pytest -q`, `uv run ruff check .` and
  `uv run ruff format --check .`. Task 11 also runs
  `uv run --group docs sphinx-build -W -b html docs docs/_build`.

## Review Focus

1. **A branch whose parent is deleted or renamed**, or a cycle in parents. The
   branch must not render as an error page; it falls back to its published
   branch, with the banner note. Tests are in Task 1 (refusal at write) and
   Task 2 (render fallback).
2. **Two concurrent writes to one branch.** Version numbers must not collide.
   `record_version` locks the branch row. Test in Task 4.
3. **A push whose source is also its target, or into its own descendant.** It
   is refused with a 400 before any write. Test in Task 7.
4. **A selection value with odd bytes** (`Scrud-UI-Branch: ../x@pinned@`, an
   over-long cookie). It is ignored, the same way an invalid
   `Scrud-UI-Scope` is ignored. Test in Task 8.
5. **A mounted request whose rest of path isn't a registered type slug**
   (`/msp/resources/nope/`). It falls through to the project's URLconf (a
   normal 404), and a prefix never claims unknown paths. Test in Task 9.

---

### Task 1: Branch type, branched items, authorizing context

**Files:**
- Create: `scrud_django/ui/branches/__init__.py` (constants), `scrud_django/ui/branches/model.py`, `scrud_django/ui/branches/store.py`, `scrud_django/ui/branches/views.py`
- Create: `scrud_django/ui/documents/UIBranch-schema.json`, `UIBranch-ld.json`
- Modify: `scrud_django/ui/types.py` (register `ui-branches`; extend `REGISTRATION_CACHES` and `ui_resource_type_urls`), `scrud_django/ui/views.py` (item validation), `scrud_django/ui/documents/UIComponent-schema.json` and `UIStylesheet-schema.json`, `scrud_django/exceptions.py`, `tests/conftest.py` (register and forget the new type)
- Test: `tests/test_ui_branch_types.py`, plus new fixtures in `tests/fixtures.py`

**Interfaces:**
- Produces, in `branches/__init__.py`:
  - the type URI and slug constants from Global Constraints;
  - `MAIN = "main"`;
  - `ITEM_KINDS = {"component": UI_COMPONENT_TYPE_URI, "stylesheet": UI_STYLESHEET_TYPE_URI}` (Task 10 adds `"css"`).
- Produces, in `model.py`:
  - `@dataclass(frozen=True) class Branch: name: str; context: str; parent: str | None; base: str  # "live" | "pinned"; branching_point: Mapping[str, int]; prefix: str | None; pk: int | None` (`pk` is `None` for the virtual `main`);
  - `kind_of(type_uri: str) -> str | None`;
  - `branch_of(content) -> str`: `content["branch"]`, else `MAIN`;
  - `applies_to(content, branch: Branch) -> str`;
  - `item_key(kind: str, content, branch: Branch) -> tuple`: `(name,)` for a component or CSS item, `(name, applies_to)` for a stylesheet;
  - `authorizing_context(kind: str, content, branch: Branch) -> str`.
- Produces, in `store.py`:
  - `branches(using=None) -> dict[str, Branch]`: every branch, plus a virtual `main` (`context=""`, `prefix=""`, no parent) when none is stored. It is memoised in `cascade.rules_memo` under a new key `"branches"`.
  - `ensure_main(using) -> Branch`: stores `main` if it is virtual.
  - `ancestors(name: str, all_branches) -> list[Branch]`: nearest first. It raises `BranchChainError` on a missing parent or a cycle.
  - `published_root(name, all_branches) -> Branch`: the nearest ancestor with a `prefix`, or the branch itself if it has one.

- [ ] **Step 1: Write the failing tests** in `tests/test_ui_branch_types.py`:
  - `test_branch_create_needs_grant_in_its_context`: a user with `ui-branches` POST in `alice` creates `{"name": "alice-wip", "rbac_context": "alice", "parent": "main"}` (201). The same body in `bob` gets a 403.
  - `test_branch_refusals`: each gets a 400 with a distinct `detail`:
    - a duplicate name;
    - `parent: "nope"`;
    - a parent cycle (re-parenting `a` under its child `b`);
    - a bad name (`"Main!"`);
    - a duplicate `prefix`;
    - a prefix `"/admin"` (no trailing slash);
    - `base: "pinned"` without a `parent`.
  - `test_item_branch_defaults_to_main_and_phase1_bodies_still_work`: the phase-1 `make_component`/`make_stylesheet` helpers still produce 201s. A stored component reads back with `branch == "main"`. An `acme` stylesheet reads back with `applies_to == "acme"` and `rbac_context == "acme"`.
  - `test_item_rbac_context_must_be_the_authorizing_context`: on working branch `alice-wip` (context `alice`), a component with `rbac_context: ""` gets a 400 naming the expected context `alice`. With `rbac_context: "alice"` it gets a 201, by a user holding grants only in `alice`.
  - `test_published_stylesheet_authorized_in_applies_to`: on `main`, a stylesheet with `applies_to: "acme"` and `rbac_context: "acme"` is written by `ui_stylesheet_author_login`-style grants in `acme` alone (201). The same with `rbac_context: ""` gets a 400.
  - `test_names_are_unique_per_branch`: component `todo-row` on `main` and on `alice-wip` both get 201. A second `todo-row` on `alice-wip` gets a 400.
  - `test_deletion_marker_needs_only_name`: `{"name": "todo-row", "branch": "alice-wip", "deleted": true, "rbac_context": "alice"}` gets a 201. A non-deleted component without `template` gets a 400.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ui_branch_types.py -q`
Expected: FAIL (`ui-branches-list` is not a URL name).

- [ ] **Step 3: Implement**
  - **`UIBranch-schema.json`** (draft-04):
    - properties `name`, `rbac_context` (string), `parent` (string or null), `base` (enum `live`, `pinned`; default `live`), `branching_point` (object of integers; server-managed), `prefix` (string or null), `conflicts` (array; server-managed, Task 6), `pull` (boolean; write-only, Task 6), `label`, `description`;
    - required: `name` and `rbac_context`.
  - **Item schemas:**
    - add `branch` (string), `deleted` (boolean) and `base` (string or null);
    - add `applies_to` (string) to stylesheets;
    - drop the component's `rbac_context` `enum [""]`;
    - make only `name` required. `engine`, `mode` and `template` move to `validate_content`, which requires them when `deleted` is not true.
  - **`UIBranchViewSet(UIContentValidationMixin, ResourceViewSet)`** in `branches/views.py`:
    - validates the name, unique name, parent, no cycle (`ancestors`), prefix format and unique prefix;
    - rejects `pinned` without a parent;
    - sets `branching_point` on create (Task 4 fills it in; until then `{}`).
  - **`UIContentValidationMixin.validate_content` in `ui/views.py`** gains a branch step, run before the existing checks and only for item kinds:
    1. Resolve the branch (`branches()`); 400 if it is unknown.
    2. Default `applies_to`:
       - on a published branch, use `rbac_context` if that differs from the branch's context, else `""`;
       - on a working branch, use `""`.
    3. Require `rbac_context == authorizing_context(...)`. Otherwise raise `UIBranchContextError(expected)` (400).
    4. Set `branch`, `applies_to` and `base` (the parent's effective item ETag for `item_key`, through Task 2's `effective_rows`; `None` until then) in `request.data` before saving.
    5. Call `ensure_main(db)` when writing to `main`.
  - **Phase-1 uniqueness** becomes per branch: `(branch, name)` for components and `(branch, name, applies_to)` for stylesheets.
  - **New exceptions** in `scrud_django/exceptions.py`, all `ScrudException` subclasses with status 400: `UIBranchInvalidError(reason)`, `UIBranchContextError(expected)`.

- [ ] **Step 4: Run the new tests and the phase-1 UI tests**

Run: `uv run pytest tests/test_ui_branch_types.py tests/test_ui_authoring.py tests/test_ui_types.py -q`
Expected: PASS. A phase-1 assertion on the old schema message for a component outside `""` changes. Add a new assertion for the `UIBranchContextError` detail, and say why in the commit message (CLAUDE.md: never weaken one).

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): branch resource type and branched UI items`).

---

### Task 2: Effective state, and rendering on a selected branch

**Files:**
- Create: `scrud_django/ui/branches/effective.py`
- Modify: `scrud_django/ui/cascade.py` (`_stylesheet_rows`, `_stored_components`, `_stored`), `scrud_django/ui/renderer.py` (`ScrudHTMLRenderer.render` enters the selection)
- Test: `tests/test_ui_branch_render.py`

**Interfaces:**
- Consumes: Task 1's `branches()`, `ancestors()`, `item_key()`, `branch_of()`.
- Produces, in `effective.py`:
  - `@dataclass(frozen=True) class Selection: branch: str = MAIN; pinned: bool = False`;
  - `using_selection(selection: Selection)`: a context manager that sets a contextvar;
  - `active_selection() -> Selection`: the default is `Selection()`;
  - `effective_rows(type_uri: str, selection: Selection | None = None) -> list[tuple[int, str, dict]]`: `(pk, etag, content)` lowest pk first, with deletion markers removed. It reads rows through `cascade._stored` (one query per type per response, as now). Task 5 adds the pinned path.
- Produces, in `cascade.py`:
  - `_stylesheet_rows` and `_stored_components` read `effective_rows(type_uri, active_selection())` instead of `_stored`;
  - a stylesheet's "where it applies" is `applies_to` (falling back to `rbac_contexts_of` for a row without one);
  - the component check "not in the default context" is removed.

Effective-state algorithm, given the chain `[B, parent, grandparent, …]`:

```
seen = {}
for branch in chain:
    for row in rows where branch_of(row) == branch.name:
        seen.setdefault(item_key(kind, row, branch), row)
return [r for r in seen.values() if not r.content.get("deleted")]
```

It lives in one function, and pinned (Task 5) replaces only the rows for the
ancestors.

- [ ] **Step 1: Write the failing tests:**
  - `test_live_branch_inherits_untouched_and_overrides_touched`. Setup:
    - `main` has components `row` (`<b>`) and `title` (`<h1>`);
    - branch `wip` (live, parent `main`) overrides `row` as `<i>`.

    A GET of the to-do list with `Scrud-UI-Branch: wip` by `todo_owner_login`, who may GET `wip`, renders `<i>` and `<h1>`. A later edit of `title` on `main` to `<h2>` shows on `wip`.
  - `test_deletion_marker_hides_inherited_item`: a marker for `row` on `wip` makes the node fall back to its built-in.
  - `test_unauthorized_or_unknown_selection_renders_main`: with no GET on `wip`, or `Scrud-UI-Branch: nope`, the response is 200 and renders `main`.
  - `test_broken_chain_renders_main` (Review Focus 1): a `wip` whose parent row was deleted renders `main`, and logs at WARNING.
  - `test_base_is_server_managed`: a client-sent `base` is replaced by the parent's current effective ETag for the item's key, or `null` when the parent has no such item. This completes Task 1's step 4 of `validate_content`.
  - `test_main_without_branches_makes_no_extra_queries`: `django_assert_num_queries` gives the same count for a render with no header as on `feature/ui-components`. Record that number in the test.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement**
  - `effective.py` as above.
  - In `ScrudHTMLRenderer.render`, inside `request_memo()`, wrap the body in `using_selection(selection_for_request(request))`.
  - `selection_for_request(request) -> Selection` is a first, minimal version in `effective.py`: header only, ignored unless the branch exists, its chain is intact, and `may_read_in_context(request, UI_BRANCH_TYPE_URI, content, [context])` holds. Task 8 replaces it.

- [ ] **Step 4: Run tests: PASS.** Also run `tests/test_ui_cascade.py tests/test_ui_render.py tests/test_query_budget.py`.

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): render from a branch's effective state`).

---

### Task 3: Effective JSON reads of items

**Files:**
- Modify: `scrud_django/ui/views.py` (`UIContentValidationMixin` gets a list override)
- Test: `tests/test_ui_branch_reads.py`

**Interfaces:**
- Consumes: `effective_rows`, `Selection`, and the header parsing from Task 2.
- Produces: `GET /api/ui-components/` (and stylesheets, and CSS later) with `Scrud-UI-Branch: <b>` or `?ui-branch=<b>` lists `b`'s effective items, deletion markers included (so the hidden status is visible). Without a selection it lists `main`'s effective items. Each item's `branch` and `base` fields tell whether it is inherited (`branch != b`), changed (`base` not null) or added (`base` null).

- [ ] **Step 1: Write the failing tests:**
  - `test_list_is_effective_state_of_selected_branch`: the `wip` list holds `wip`'s `row` (`branch == "wip"`) and `main`'s `title` (`branch == "main"`).
  - `test_inherited_items_keep_their_read_check`: a user with GET on `ui-components` in `alice` only, and GET on branch `alice-wip`, sees only `alice-wip`'s own items. Inherited `main` items need GET in `""`.
  - `test_list_without_selection_is_main`: rows of other branches are absent.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement.** `filter_queryset` restricts to the pks of `effective_rows(..., include_deleted=True)`; add the `include_deleted: bool = False` parameter to `effective_rows`. The existing RBAC list filtering then applies per row. Pagination and list ETags are unchanged.

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): list a branch's effective items`).

---

### Task 4: Versions and branching points

**Files:**
- Create: `scrud_django/ui/branches/versions.py`, `scrud_django/ui/documents/UIBranchVersion-schema.json` and `-ld.json`
- Modify: `scrud_django/ui/types.py` (register `ui-branch-versions`), `scrud_django/ui/apps.py` (`ready()` connects the receivers), `scrud_django/ui/branches/views.py` (`UIBranchVersionViewSet`, read-only; `UIBranchViewSet` sets `branching_point`)
- Test: `tests/test_ui_branch_versions.py`

**Interfaces:**
- Produces, in `versions.py`:
  - `record_version(branch_name: str, using: str) -> int`:
    - locks the branch's `Resource` row (`select_for_update`);
    - writes a `ui-branch-versions` resource with content `{"branch", "number", "rbac_context": <branch context>, "items": {kind: [content, ...]}, "ancestors": {name: number}}`. `items` is the branch's own rows, deletion markers included; `ancestors` is the branching point;
    - returns `number`, which is the previous `number` + 1.
  - `current_version(branch_name, using) -> int`: the latest number, recording one if the branch has none.
  - `snapshot(branch_name, number, using=None) -> dict`: a version's content. It raises `UIBranchVersionMissingError` (404) if absent.
  - `batched_versions()`: a context manager. Inside it, item saves only mark their branch; one version per marked branch is recorded on exit.
- Receivers on `scrud_post_save` and `scrud_post_delete` for the item type URIs call `record_version(branch_of(instance.content), using)`, or mark the branch inside `batched_versions`.
- `UIBranchViewSet` sets `branching_point = {a.name: current_version(a.name) for a in ancestors(name)}` on create.
- `UIBranchVersionViewSet` answers POST, PUT and DELETE with 405. GET is RBAC-filtered by the version's `rbac_context`, which is the branch's context.

- [ ] **Step 1: Write the failing tests:**
  - `test_every_item_write_creates_a_version`: create, update and delete of a `wip` component give versions 1, 2 and 3 of `wip`, each with the matching `items`.
  - `test_branching_point_records_every_ancestor`: `testing` (parent `main`) and `alice` (parent `testing`) give `alice.branching_point == {"testing": n, "main": m}`, the current versions at creation.
  - `test_versions_are_read_only`: POST, PUT and DELETE on `ui-branch-versions` all give 405.
  - `test_concurrent_writes_get_distinct_numbers` (Review Focus 2): two `record_version` calls in separate transactions on PostgreSQL or MySQL give distinct numbers. Skip on SQLite with a reason. Pattern: `tests/test_delete_locking.py`.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): branch versions and branching points`).

---

### Task 5: Pinned bases, per branch and per view

**Files:**
- Modify: `scrud_django/ui/branches/effective.py`
- Test: `tests/test_ui_branch_pinned.py`

**Interfaces:**
- Consumes: `snapshot()`, `Branch.branching_point`, `Selection.pinned`.
- Produces: `effective_rows` takes a branch's ancestors' rows from `snapshot(ancestor, branching_point[ancestor])["items"]` when `branch.base == "pinned"` or `selection.pinned`. Only the selected branch's own rows stay live.
  - Snapshot rows have no pk and no ETag. Use `pk=None` and the ETag `"v<number>:<branch>:<key>"`, and make `cascade._stored_component` accept `pk=None`.
  - Snapshots load one query per pinned ancestor per response, through the memo.

- [ ] **Step 1: Write the failing tests:**
  - `test_pinned_branch_ignores_later_parent_changes`: `wip` pinned at `main` vN. A later `main` edit of `title` is not shown on `wip`.
  - `test_pinned_view_of_live_branch`: `Scrud-UI-Branch: wip@pinned` on a live `wip` shows the branching-point state. Without `@pinned` it shows the live state.
  - `test_pinned_chain_uses_every_ancestor_version`: `alice` over `testing` over `main`, with `main` edited after `alice` branched. Then `alice@pinned` shows `main` as of `branching_point["main"]`, even though `testing` didn't change.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement.** Extend `selection_for_request` to parse `@pinned`.

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): pinned bases`).

---

### Task 6: Pull and conflict resolution

**Files:**
- Create: `scrud_django/ui/branches/sync.py`
- Modify: `scrud_django/ui/branches/views.py` (`UIBranchViewSet.validate_content` handles `pull`), `scrud_django/ui/views.py` (item `rebase` flag)
- Test: `tests/test_ui_branch_pull.py`

**Interfaces:**
- Produces, in `sync.py`:
  - `conflicts(source: str, against: str, using=None) -> list[dict]`: `[{"kind", "key": [..], "base", "current"}]` for each own row of `source` whose `base` differs from `against`'s effective ETag for that key. A `base` of `null` conflicts if `against` now has the key.
  - `pull(content: dict, using) -> dict`: new branch content with `branching_point` set to the ancestors' current versions and `conflicts = conflicts(name, parent)`, and `pull` removed.
- **Resolving a conflict:**
  - *Keep yours:* PUT the override with `"rebase": true`. The server sets `base` to the parent's current ETag, drops `rebase`, and removes the key from the branch's `conflicts`.
  - *Take theirs:* DELETE the override row (it is the branch's own row, so this is not a marker).
  - Each resolution removes the key from `conflicts`, in the same transaction.

- [ ] **Step 1: Write the failing tests:**
  - `test_pull_moves_branching_point_and_lists_conflicts`: `wip` overrides `row`, and `main` then edits `row`. A PUT of `wip` with `"pull": true` gives 200. `branching_point["main"]` is the new version, and `conflicts == [{"kind": "component", "key": ["row"], ...}]`.
  - `test_pull_needs_put_on_branch`: GET-only on `wip` gives a 403.
  - `test_rebase_resolves_conflict` and `test_dropping_override_resolves_conflict`.
  - `test_pull_of_live_branch_changes_no_rendering`: the HTML is byte-identical before and after.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): pull and conflict resolution`).

---

### Task 7: Push and restore (`ui-pushes`)

**Files:**
- Create: `scrud_django/ui/documents/UIPush-schema.json` and `-ld.json`
- Modify: `scrud_django/ui/branches/sync.py` (`apply_push`, `apply_restore`), `scrud_django/ui/branches/views.py` (`UIPushViewSet`), `scrud_django/ui/types.py`, `scrud_django/exceptions.py`
- Test: `tests/test_ui_branch_push.py`

**Interfaces:**
- **Push content:** `{"source": str | null, "target": str, "restore": int | null, "state": "proposed" | "applied", "rbac_context": <target's context>, "result": {...}}`. `result` is server-managed. Exactly one of `source` and `restore` is set; `restore: N` restores the target's version N.
- **`UIPushViewSet(UIContentValidationMixin, ResourceViewSet)`:**
  - `validate_content` refuses with 400 when the source equals the target, when the target is a descendant of the source (Review Focus 3), when `rbac_context` isn't the target's context, or when a branch is unknown.
  - `create_resource` and `update` run inside `transaction.atomic` (a POST isn't a locked write; see `view_mixins.dispatch`). After `super()` saves, `apply_push` runs if the stored state moved into `applied` (a create in `applied` counts). It then writes `result: {"version": n}` with `ResourceRegistration.update_locked`.
- **`apply_push(request, content, using) -> int`**, run inside `batched_versions()`:
  1. GET on the source branch, else `PermissionDenied`.
  2. PUT on the source branch, else `PermissionDenied`. Applying empties the source.
  3. `conflicts(source, target)` non-empty: raise `UIPushConflictError(conflicts)`, a 409 whose `detail` lists them. The dispatch rollback then discards every write.
  4. For each own row of the source:
     - compute the target content (`branch = target`, `rbac_context = authorizing_context(kind, content, target)`);
     - check `may_act_in_context(request, Permission("http.PUT" if the target has its own row else "http.POST", type_uri), content, [ctx])`, else raise `UIPushForbiddenError(kind, key)` (403);
     - write the row with `ResourceRegistration` (update the target's own row, or register a new one). A deletion marker becomes a delete of the target's own row, plus a marker if the target's parent still has the key.
  5. Delete the source's own rows.
  6. Return `current_version(target)` after the batch records it.
- **`apply_restore(request, content, using) -> int`:** replace the target's own rows with `snapshot(target, N)["items"]`, with the same per-item grant check, in one batch.
- **New exceptions:** `UIPushConflictError` (409), `UIPushForbiddenError` (403), `UIBranchVersionMissingError` (404).

- [ ] **Step 1: Write the failing tests:**
  - `test_push_applies_atomically_and_empties_source`: `wip` → `main` with no conflicts gives 201. `main` has `wip`'s `row`, `wip` has no own rows, `result.version` is `main`'s new version, and exactly one `main` version was added.
  - `test_push_conflict_writes_nothing`: 409 with the conflict in `detail`, and `main` and `wip` are unchanged.
  - `test_push_needs_per_item_grant`: a pusher with `ui-pushes` POST and component PUT in `""`, but no stylesheet grant in `acme`, pushes an `acme` stylesheet. The result is 403, and nothing is written.
  - `test_push_needs_put_on_source`: 403.
  - `test_deletion_marker_pushes_as_delete`.
  - `test_push_into_descendant_or_self_is_refused` (400).
  - `test_proposed_then_applied`: a POST with `proposed` changes nothing. A PUT to `applied` applies the push.
  - `test_workflow_can_require_approval`: register a `WorkflowPolicy` for `UIPush` with `proposed → approved → applied`. A direct `proposed → applied` gives the workflow's 400, and the path through `approved` applies. No code change: the test only adds the policy. Follow `tests/test_rest_workflow.py`.
  - `test_restore_brings_back_overrides_and_keeps_base_mode`: restoring `wip` vN on a live `wip` yields `wip`'s own rows equal to vN's, `wip` still live, and a new version.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): push and restore`).

---

### Task 8: Browsing a working branch (selection, banner, confirmation, client)

**Files:**
- Create: `scrud_django/ui/branches/selection.py`, `scrud_django/ui/branches/chrome.py`, `scrud_django/ui/branches/templates/banner.html` and `confirm.html`
- Modify: `scrud_django/ui/renderer.py` (`VARY`, `render`, `finalize_html`, `_page`), `scrud_django/ui/render.py` (`_frame` adds `data-scrud-branch`), `scrud_django/ui/static/scrud_ui/scrud-ui.js` (`htmx:configRequest`)
- Test: `tests/test_ui_branch_browsing.py`, and new cases in `tests/test_ui_client.py` (node)

**Interfaces:**
- Produces, in `selection.py`:
  - `@dataclass(frozen=True) class Chosen: selection: Selection; source: str  # "header" | "param" | "cookie" | "none"; refused: str | None; confirmed: bool`;
  - `choose(request) -> Chosen`:
    - order: header, then param, then cookie, then the published branch of the request's prefix (`main` at the API URLs; Task 9 adds prefixes);
    - a value matching `^[a-z0-9][a-z0-9._-]{0,63}(@pinned)?$` and ≤ 80 bytes is used; anything else is ignored (Review Focus 4);
    - a branch that is unknown, has a broken chain, isn't GET-able, or isn't in the prefix's tree is refused: `refused` holds its name and the published branch is used.
  - This replaces Task 2's `selection_for_request`; keep that name as an alias of `choose(request).selection`.
- Produces, in `chrome.py`, rendered on the built-in engine (`engines.compile_template("django", ...)`) from package templates, never through stylesheet resolution:
  - `banner(chosen: Chosen, request) -> SafeString`: the branch name and mode (live, or "pinned at <parent> v<n>"), a switcher (a GET form listing the branches the viewer may GET in this prefix's tree, submitting `ui-branch` plus `ui-branch-confirm=1`), a live/pinned toggle link, a "copy link" anchor carrying `?ui-branch=`, and a "back to <published>" link (`?ui-branch=&ui-branch-confirm=1`);
  - `confirm_page(chosen, request) -> SafeString`: "You're about to browse branch X", with Continue (the same URL plus `ui-branch-confirm=1`) and Cancel.
- **Renderer changes:**
  - On a full-page HTML GET whose `chosen.source == "param"`, which isn't `confirmed`, and whose branch differs from the cookie's, return `confirm_page` in place of the body (status 200).
  - Otherwise, when the selection isn't the published branch, prepend `banner` to `body` before `_page`.
  - `finalize_html` sets or deletes the cookie `scrud_ui_branch` (path = the prefix's URL path, `HttpOnly`, `SameSite=Lax`) when the param came with `ui-branch-confirm=1`.
  - Add `Scrud-UI-Branch` and `Cookie` to `VARY`.
- **Frames** get `data-scrud-branch="<value>"` when the selection isn't the published branch. `scrud-ui.js` sets the `Scrud-UI-Branch` request header from the closest `[data-scrud-branch]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_precedence_header_param_cookie`;
  - `test_param_from_shared_link_asks_first_then_sets_cookie`: the first GET is the confirm page and the response sets no cookie. GET with `ui-branch-confirm=1` sets the cookie and renders the branch. A later plain GET stays on the branch (cookie).
  - `test_banner_only_on_working_branch`, and `test_banner_not_hideable_by_rules`: a rule selecting `scrud:page` for a stored page component that omits it still has the banner inside `body`.
  - `test_refused_branch_falls_back_with_note`: the banner says "branch … isn't available".
  - `test_odd_selection_values_are_ignored` (Review Focus 4), parametrized over `../x`, `x@pinned@`, 200 bytes, and NUL.
  - `test_vary_names_branch_header_and_cookie`: update the phase-1 `Vary` assertion in `tests/test_ui_negotiation.py` with a new expected value, and say why in the commit.
  - Client test in `tests/test_ui_client.py`: a frame with `data-scrud-branch="wip"` makes `configRequest` set `Scrud-UI-Branch: wip`.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run tests: PASS** (with `SCRUD_TEST_REQUIRE_NODE=1` for the client test).

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): browse a working branch`).

---

### Task 9: Published branches at URL prefixes

**Files:**
- Create: `scrud_django/ui/branches/middleware.py`, `scrud_django/ui/branches/checks.py`
- Modify: `scrud_django/ui/apps.py` (register the check), `scrud_django/mixins/view_mixins.py` (`initial`: call `check_mount` next to `check_ui_headers`), `scrud_django/ui/render.py` (`render_collection` rewrites item hrefs), `scrud_django/ui/branches/selection.py` (the prefix's published branch), `tests/settings.py` (add the middleware after `AuthenticationMiddleware`)
- Test: `tests/test_ui_branch_prefixes.py`

**Interfaces:**
- **`PublishedBranchMiddleware`:**
  - For a request path `P`, find the longest published `prefix` (a non-empty prefix) such that `P` starts with `prefix + slug + "/"` for a **registered type slug**, or with `prefix + "ui-css-files/"` (Task 10).
  - If found, set `request.scrud_ui_mount = Mount(prefix, branch_name, api_base)` and rewrite `request.path_info` to `api_base + P[len(prefix):]`. `api_base` is the path of `reverse("<slug>-list")` minus `slug + "/"`. Leave `request.path` unchanged.
  - Otherwise do nothing (Review Focus 5).
  - Prefixes are read with one query per request, only for paths that don't start with `api_base`. Document this cost in `docs/ui.md`.
- **`check_mount(request)`:** under a mount, GET on the mount's branch is required, else `NotAuthenticated` or `PermissionDenied`. This applies to JSON and HTML alike (spec: same refusal as any GET).
- **Mount helpers:** `Mount.href(canonical: str) -> str` rewrites `api_base…` to `prefix…`. `render_collection` uses it for item hrefs. `request.path` is already the mounted URL for frames.
- **`choose()` (Task 8):** the published branch is `request.scrud_ui_mount.branch`, else the branch with `prefix == ""` (`main`). The cookie path is the mount's prefix, or `api_base`.
- **System check `scrud_django_ui.E001`:** two prefixes overlap (one starts with the other), or a prefix plus a registered slug resolves in the project's URLconf to something other than the scrud view (`django.urls.resolve` succeeds).

- [ ] **Step 1: Write the failing tests:**
  - `test_two_prefixes_render_same_resource_differently`: `admin` (`/admin/resources/`) and `customer` (`/`) each have a different component for `ex:TodoList`. Both URLs render the same list with their own component, and the same `decisions`.
  - `test_published_branch_live_over_another`: `msp` (parent `admin`, live) inherits an `admin` edit.
  - `test_prefix_refuses_without_get_on_branch`: 403 for HTML and JSON alike, with no fallback.
  - `test_root_prefix_claims_only_type_slugs` (Review Focus 5): `/` mounted, and `/nope/` gives the project's 404.
  - `test_writes_under_prefix_use_same_checks`: a PUT to `/admin/resources/todo-lists/<id>/` without `If-Match` gives the same 428 as under `/api/`. A refused PUT gives the same 403.
  - `test_links_stay_in_mount`: collection item hrefs and the frame's `hx-put` start with `/admin/resources/`. The JSON `href` stays `/api/...`.
  - `test_working_branch_browsed_under_its_root_prefix`: `testing-admin` (parent `admin`) is selectable under `/admin/resources/`, and refused with the banner note under `/`.
  - `test_overlapping_prefixes_reported_by_check`.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): published branches at URL prefixes`).

---

### Task 10: CSS items

**Files:**
- Create: `scrud_django/ui/documents/UICss-schema.json` and `-ld.json`, `scrud_django/ui/branches/css.py`
- Modify: `scrud_django/ui/types.py`, `scrud_django/ui/branches/__init__.py` (`ITEM_KINDS["css"]`), `scrud_django/ui/views.py` (`UICssViewSet`), `scrud_django/urls.py` (route `ui-css-files/<str:name>/<str:etag>.css`), `scrud_django/ui/renderer.py` (`_page` context `css`), `scrud_django/ui/builtins/page.html`
- Test: `tests/test_ui_branch_css.py`

**Interfaces:**
- **CSS content:** `{"name", "css": str (max 262144 chars), "order": int, "branch", "deleted", "base", "rbac_context"}`. Its validation is size only, plus a 400 if the text contains `</style` (case-insensitive).
- **`UICssFileView`** (`GET` only):
  - the active selection is chosen with Task 8's `choose`, so a working branch's CSS works too;
  - the view finds `name` in `effective_rows(UI_CSS_TYPE_URI, selection)`;
  - a missing name or an ETag mismatch is a 404;
  - a hit is `text/css; charset=utf-8` with `Cache-Control: public, max-age=31536000, immutable`, or `private, …` on a working branch;
  - it applies `check_mount`.
- **Page context:** `_page`'s context gains `css: [url, ...]`, each `Mount.href(reverse("ui-css-file", args=[name, etag]))`, ordered by (`order`, name). The built-in `page.html` adds a `<link rel="stylesheet">` for each. Document the new key for custom page components in `docs/ui.md`.

- [ ] **Step 1: Write the failing tests:**
  - `test_css_served_under_prefix_with_versioned_url`;
  - `test_old_etag_url_is_404_after_edit`;
  - `test_page_links_effective_css_in_order`;
  - `test_css_on_working_branch_overrides_published`;
  - `test_css_refused_without_get_on_branch`.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run tests: PASS.**

- [ ] **Step 5: Full suite, lint, commit** (`feat(ui): CSS items`).

---

### Task 11: Docs, demo, release

**Files:**
- Modify: `docs/ui.md`, `docs/release-notes.md`, `docs/follow-ups.md`, `pyproject.toml` (version `0.17.0`), `uv.lock` (`uv lock`), `demo/` settings (middleware) and `demo/.../management/commands/seed_todo_demo.py`
- Test: `tests/test_ui_demo_seed.py` if a seed test exists in phase 1; otherwise a smoke run of the command in the verification step.

- [ ] **Step 1: `docs/ui.md`.** Add a "Branches" chapter covering:
  - concepts;
  - authorizing contexts;
  - the push trigger, and how to add an approval `WorkflowPolicy`;
  - browsing (header, parameter, cookie, banner, confirmation);
  - prefixes and the middleware setup line;
  - CSS;
  - the one prefix query per request.

  Rewrite **Security** for "viewing a branch means trusting its writers" (spec, Security). Remove "global only" from the UI components section.

- [ ] **Step 2: `docs/follow-ups.md`.** Add anchored sections:
  - `ui-branches-row-scan`: each render reads every branch's rows of a type. The test that would expose it is a query and time budget with 1,000 branches.
  - `ui-banner-css-hideable`: branch CSS can hide the banner.
  - `ui-prefix-query`: one query per mounted request; cache it with invalidation.

- [ ] **Step 3: Demo.** `seed_todo_demo` also creates:
  - `admin` at `/admin/resources/`, with an extra component;
  - `customer` at `/todo/`, with its own CSS;
  - `testing-admin` (parent `admin`).

  Grants: `author` writes in all three, and `reader` may GET `customer` only. Add the middleware to the demo settings.

- [ ] **Step 4: Release notes and version.** `0.17.0` entry. Bump the version, then run `uv lock`.

- [ ] **Step 5: Verify:**

```bash
uv run pytest -q
uv run pytest -q --reverse
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run --group docs sphinx-build -W -b html docs docs/_build
cd demo && uv run python manage.py migrate && uv run python manage.py seed_todo_demo
```

Expected: everything passes, and the seed prints the three branches.

- [ ] **Step 6: In-browser acceptance** (owner's flow, spec "The working flow"):
  1. `author` edits a component on `testing-admin`.
  2. `author` browses `/admin/resources/todo-lists/` with the switcher and sees the change, with the banner.
  3. `author` pushes `testing-admin` → `admin`, and `/admin/resources/` shows it with no banner.
  4. `/todo/` is unchanged.
  5. `reader` gets 403 at `/admin/resources/`.

  Record the result in the MR description.

- [ ] **Step 7: Commit** (`docs(ui): branches; demo; release 0.17.0`).
