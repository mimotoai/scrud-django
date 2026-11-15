# UI components as resources (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render any scrud ResourceType as server-side HTML. The HTML is built from
component and stylesheet resources, shows only what the viewer may do, and is edited
in the browser through htmx. Phase 1 is done when a to-do list owner can add, check
off and rename items.

**Architecture:**
- A new optional Django app, `scrud_django.ui`, registers two built-in resource
  types:
  - **UI components**: Django or Jinja2 templates stored as resources.
  - **UI stylesheets**: CSS-like rules choosing a component by scope chain and
    type term.
- The app adds a `text/html` DRF renderer to the existing resource views. That
  renderer walks the resource's JSON Schema and JSON-LD context, resolves a
  component for every node, and renders it with plain-JSON context.
- Browser writes go through the **unchanged JSON PUT pipeline**. A small client
  extension, `scrud-ui.js`, builds the full JSON body from the rendered content
  plus inputs named by JSON Pointer, and sends it with `If-Match`.

**Tech Stack:** Django 5.2, DRF 3.15+, scoped-rbac 0.12, scrudful 0.10, Jinja2 3.1,
htmx 2.0.4 (vendored), safetydance 0.5 tests, Playwright (acceptance only).

**Spec:** `docs/superpowers/specs/2026-11-09-ui-components-design.md`

## Global Constraints

- **Versions:** Python ≥3.11 (3.13 pinned), Django `>=5.2,<6.0`; code must work on
  SQLite, PostgreSQL, MySQL and MariaDB.
- **New dependency:** `jinja2>=3.1,<4` goes only in a new `ui` extra. The dev group
  becomes `"scrud-django[search,ui]"`. Run `uv lock` after any `pyproject.toml`
  change.
- **Type IRIs:**
  - UI components: `http://api.openteams.com/json-ld/UIComponent`, slug
    `ui-components`.
  - UI stylesheets: `http://api.openteams.com/json-ld/UIStylesheet`, slug
    `ui-stylesheets`.
  - Never change existing `api.openteams.com` IRIs.
- **Components:**
  - stored only in context `""`;
  - written only through the policy (no new role model);
  - names are unique site-wide;
  - the `scrud:` prefix is reserved for built-ins.
- **Template context is plain JSON** (dicts, lists, strings, numbers, bools,
  `None`) plus `SafeString` child HTML. Never pass a model instance, the request,
  or a callable.
- **Escaping:** both engines autoescape. Stored Django templates compile on a
  standalone `django.template.Engine` with **no loaders and no libraries**, so
  `{% include %}`, `{% extends %}` and `{% load %}` of app tags are unavailable.
- **One URL:** HTML is served at the resource's own URL, chosen by
  `Accept: text/html`. `?format=api` still reaches DRF's browsable API. Projects
  without `scrud_django.ui` in `INSTALLED_APPS` behave exactly as before.
- **Writes:** browser writes are JSON PUTs (`application/json`) with `If-Match`.
  No form-encoded parser is added, and the `RbacJSONParser` stays the only parser.
- **Fragment protocol (request headers):**
  - `HX-Request` (htmx sets it): answer with a fragment, not a page.
  - `Scrud-UI-Scope`: a space-separated scope chain, outermost first.
  - `Scrud-UI-Pointer`: the JSON Pointer of the node to render (default `""`).
  - HTML responses carry `Vary: Accept, HX-Request, Scrud-UI-Scope,
    Scrud-UI-Pointer`.
- **Refusal messages:**
  - shown only in HTML, and only to users who may GET the stored resource;
  - RBAC messages go at the top of the form;
  - JSON API error bodies are unchanged.
- **Repo conventions:**
  - Tests use safetydance Given/When/Then.
  - Steps live only in `tests/steps.py`.
  - Decorator order is `@pytest.mark.django_db` then `@test_context`.
  - Settings live only in `tests/settings.py::SETTINGS`.
  - ruff with `quote-style = "preserve"`; format only touched files.
- **Commits:** conventional prefixes (`feat:`, `test:`, `docs:`). Each commit
  message ends with the line
  `Claude-Session: https://claude.ai/code/session_01SAZeWwR2hRvftQm9KnTy42`.
- **Verify every task** with `uv run pytest -q`, `uv run ruff check .` and
  `uv run ruff format --check .`. Docs tasks also run
  `uv run --group docs sphinx-build -W -b html docs docs/_build`.

## Review Focus

These failure modes are implied by the spec but no feature test exercises them.
Each has its test pinned in the task named.

1. **Content that doesn't match its schema:** extra keys, missing properties, or a
   wrong-typed value in legacy data. The page still renders: missing → nothing,
   extra → ignored, wrong type → the JSON fallback, never a 500. (Task 6)
2. **A stored component that fails at render time,** e.g. a Jinja `{{ value.x.y }}`
   on a string. The node renders the built-in component for its mode, with an HTML
   comment naming the component, never a 500. A template that doesn't compile is
   refused at save with 400. (Tasks 2 and 6)
3. **Markup in content:** a to-do text of `<script>alert(1)</script>` renders
   escaped in both view and edit modes, in built-in and stored Django and Jinja
   components. (Task 6)
4. **Recursive or deep schemas:** a `$ref` cycle, or nesting deeper than
   `max_body_depth()`. These render the JSON fallback at the limit and never
   recurse without bound. (Task 6)
5. **Anonymous or unauthorized HTML requests:** GET with `Accept: text/html` and no
   login, or for a resource the user can't read. The response is the same status
   as JSON (401/403/404), with `Content-Type: text/html` and a notice body. It
   never includes resource content. (Task 7)

---

## File Structure

| Path | Responsibility |
|---|---|
| `scrud_django/ui/__init__.py` | `is_enabled() -> bool` (`apps.is_installed("scrud_django.ui")`) |
| `scrud_django/ui/apps.py` | `ScrudUIConfig`, name `scrud_django.ui`, label `scrud_ui` |
| `scrud_django/ui/types.py` | Type IRIs, registration functions, `ui_resource_type_urls()` |
| `scrud_django/ui/documents/*.json` | Schemas and JSON-LD contexts of the two types; `default-stylesheet.json` |
| `scrud_django/ui/views.py` | `UIComponentViewSet`, `UIStylesheetViewSet`: write-time checks |
| `scrud_django/ui/engines.py` | Compiling stored templates (both engines), compile cache |
| `scrud_django/ui/selectors.py` | `TypeTerm`, `Selector`, parsing, matching, specificity |
| `scrud_django/ui/ld.py` | JSON-LD term lookup and IRI expansion |
| `scrud_django/ui/cascade.py` | Loading stylesheets and components; `effective_rules`, `resolve_component` |
| `scrud_django/ui/builtins/*.html` | Built-in components (Django templates) |
| `scrud_django/ui/authz.py` | Per-resource decisions, refusal messages |
| `scrud_django/ui/render.py` | Tree walk: schema + content → HTML |
| `scrud_django/ui/renderer.py` | `ScrudHTMLRenderer` (DRF) and error/page handling |
| `scrud_django/ui/static/scrud_ui/htmx.min.js` | Vendored htmx 2.0.4 |
| `scrud_django/ui/static/scrud_ui/scrud-ui.js` | Client extension `scrud-json` + htmx config |
| `scrud_django/urls.py` | Appends `ui_resource_type_urls()` when enabled |
| `scrud_django/mixins/view_mixins.py` | `ResourceViewMixin.get_renderers` / `finalize_response` hooks |
| `tests/static/todo_list/{schema,ld}.json` | The `tests://TodoList` test type |
| `tests/test_ui_*.py` | One module per task |
| `demo/…` | To-do demo type, seed command, settings |
| `docs/ui.md`, `docs/release-notes.md`, `pyproject.toml` | Docs, 0.16.0 notes, version |

---

### Task 1: App skeleton and the two built-in resource types

**Files:**
- Create: `scrud_django/ui/{__init__,apps,types,views}.py`
- Create: `scrud_django/ui/documents/{UIComponent-schema,UIComponent-ld,UIStylesheet-schema,UIStylesheet-ld}.json`
- Modify: `scrud_django/urls.py` (append UI routes when enabled)
- Modify: `pyproject.toml` (`ui` extra, dev group), `uv.lock`, `tests/settings.py` (add `"scrud_django.ui"` after `"scrud_django"`)
- Test: `tests/test_ui_types.py`

**Interfaces:**
- Produces:
  - Constants: `UI_COMPONENT_TYPE_URI`, `UI_STYLESHEET_TYPE_URI`.
  - Registration: `ui_component_resource_type(force=False) -> ResourceType` and
    `ui_stylesheet_resource_type(force=False) -> ResourceType`. They follow
    `workflow_policy_resource_type` (`registration.py:733`), with schema and
    context read from `scrud_django.ui.documents`.
  - `ui_resource_type_urls() -> list[URLPattern]`, which uses
    `register_or_defer` like `built_in_resource_type_urls()`.
  - `scrud_django.ui.is_enabled() -> bool`.
- **Component content schema** (draft-04):
  - required: `name`, `engine`, `mode`, `template`;
  - `name`: pattern `^(?!scrud:)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`;
  - `engine`: enum `["django", "jinja2"]`;
  - `mode`: enum `["view", "edit"]`;
  - `template`: string;
  - `scopes`: object mapping a property name (or `"*"` for array items) to a
    scope name;
  - `stylesheets`: array of stylesheet names;
  - `label` and `description`: strings;
  - `rbac_context`: enum `[""]`.
- **Stylesheet content schema:**
  - required: `name`, `rules`;
  - `name`: same pattern as components;
  - `prefixes`: object of string → string;
  - `order`: integer, default 0;
  - `rules`: array of `{select: string, component: string}`, with
    `additionalProperties: false`;
  - `rbac_context`: string.
- **Write-time checks** in `UIComponentViewSet` / `UIStylesheetViewSet`
  (subclasses of `views.ResourceViewSet`, overriding `create_resource` and
  `update` and calling `super()`). The checks run before `save_checked` and each
  raises a `ScrudException` subclass returning 400:
  - `UIComponentNameTakenError`: a component name already used by another
    resource of the type;
  - `UIStylesheetNameTakenError`: a stylesheet name already used *in the same
    context*;
  - `UISelectorInvalidError`: a selector that doesn't parse (from Task 3);
  - `UITemplateInvalidError`: a template that doesn't compile (from Task 2).

  Tasks 2 and 3 add the last two checks. This task adds the name checks.

- [ ] **Step 1: Write failing tests** in `tests/test_ui_types.py`:
  - `test_ui_types_registered_and_listed_in_services`: GET `services/` includes both
    type IRIs.
  - `test_component_crud_as_superuser`: POST a valid component to
    `ui-components/` → 201. Then GET it → `content["name"] == "todo-row"`.
  - `test_component_outside_default_context_is_400`: POST with `"rbac_context":
    "project-a"` → 400.
  - `test_component_reserved_name_is_400`: `"name": "scrud:object"` → 400.
  - `test_component_duplicate_name_is_400`: a second POST with the same `name` →
    400 with problem `type` ending `ui-component-name-taken`. A PUT that keeps its
    own name → 200.
  - `test_stylesheet_same_name_other_context_is_201`: name `theme` in `""` and in
    `project-a` → both 201. The same name twice in `project-a` → 400.
  - `test_without_ui_app_no_ui_routes`: with `override_settings(INSTALLED_APPS=
    [... without "scrud_django.ui"])` and a reloaded URLconf, `reverse` of the UI
    list route raises `NoReverseMatch`. Use `django.test.utils.override_settings`
    plus `clear_url_caches()` and `importlib.reload(scrud_django.urls)`, then
    restore.
- [ ] **Step 2:** `uv run pytest tests/test_ui_types.py -q` → FAIL (no module
  `scrud_django.ui`).
- [ ] **Step 3: Implement** the files above.
  - The duplicate-name check queries
    `Resource.objects.filter(resource_type=…, content__name=name)`, excluding the
    instance on update, on `self.locked_write_db or write_db()`.
  - Add `ui = ["jinja2>=3.1,<4"]`, change the dev entry to
    `"scrud-django[search,ui]"`, and run `uv lock`.
- [ ] **Step 4:** `uv run pytest tests/test_ui_types.py -q` → PASS. Run
  `uv run pytest -q` → all pass. Also run
  `uv run python manage.py makemigrations scrud_django --check --dry-run` → "No
  changes".
- [ ] **Step 5: Commit** `feat(ui): UI component and stylesheet resource types`.

---

### Task 2: Compiling stored templates

**Files:**
- Create: `scrud_django/ui/engines.py`
- Modify: `scrud_django/ui/views.py` (compile check on write)
- Test: `tests/test_ui_engines.py`

**Interfaces:**
- Produces:
  - `compile_template(engine: str, source: str) -> CompiledTemplate`.
    `CompiledTemplate.render(context: dict) -> SafeString`.
  - `TemplateCompileError(ValueError)`.
  - `cached_template(key: tuple, engine: str, source: str) -> CompiledTemplate`,
    an LRU of 512 entries keyed by `(resource pk, etag)` or `("builtin", name)`.
- **Engines:**
  - **Django:** a module-level
    `django.template.Engine(loaders=[], libraries={}, builtins=[], autoescape=True,
    debug=False)`, so only the default builtins apply.
  - **Jinja2:** `jinja2.Environment(autoescape=True, undefined=jinja2.Undefined,
    loader=None)`. `jinja2` is imported lazily. If it isn't installed, a
    `jinja2` component raises `TemplateCompileError("jinja2 is not installed")`.
  - **`SafeString` output:** for Django, `mark_safe` the rendered string.
    `SafeString` children render unescaped in both engines: Jinja honours
    `__html__`.

- [ ] **Step 1: Write failing tests:**
  - `test_django_autoescapes_values`:
    `compile_template("django", "{{ value }}").render({"value": "<b>"}) == "&lt;b&gt;"`.
  - `test_jinja_autoescapes_values`: the same check with `"jinja2"`.
  - `test_safe_children_unescaped` (both engines): `{"children": {"a":
    SafeString("<i>x</i>")}}` with template `{{ children.a }}` → contains
    `<i>x</i>`.
  - `test_django_include_and_load_unavailable`: `{% load static %}` and
    `{% include "x.html" %}` each raise `TemplateCompileError`.
  - `test_bad_syntax_raises`: `{% if %}` (Django) and `{{ ` (Jinja) raise
    `TemplateCompileError`.
  - `test_component_with_bad_template_is_400` (API): POST a component whose
    template is `{% if %}` → 400, problem type ending `ui-template-invalid`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** `engines.py`. In both component viewsets, call
  `compile_template` on create and update, and raise `UITemplateInvalidError` on
  `TemplateCompileError`.
- [ ] **Step 4:** `uv run pytest tests/test_ui_engines.py -q` → PASS, then the full
  suite.
- [ ] **Step 5: Commit** `feat(ui): compile stored Django/Jinja templates safely`.

---

### Task 3: Selectors and type terms

**Files:**
- Create: `scrud_django/ui/selectors.py`, `scrud_django/ui/ld.py`
- Modify: `scrud_django/ui/views.py` (selector check on stylesheet write)
- Test: `tests/test_ui_selectors.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) TypeTerm(iri: str | None = None, schema_type: str | None
    = None, format: str | None = None, enum: bool = False)`. Exactly one of `iri`
    and `schema_type` is set.
  - `@dataclass(frozen=True) Selector(scopes: tuple[str, ...], term: TypeTerm)`.
  - `parse_selector(text: str, prefixes: dict[str, str]) -> Selector`, which
    raises `SelectorError(ValueError)`.
  - `selector_matches(sel: Selector, scope_chain: tuple[str, ...], terms:
    list[TypeTerm]) -> int | None`. It returns the index in `terms` of the first
    term the selector matches, or `None`.
  - `ld.expand(term: str, context: dict) -> str`, which expands `prefix:local`
    using the context's string-valued prefix entries. Absolute IRIs and unknown
    prefixes are returned unchanged.
  - `ld.term_definition(context: dict, key: str) -> tuple[str | None, str | None,
    dict]`, giving `(property_iri, class_iri, nested_context)`. The
    `class_iri` is the term definition's `@type` (the scrud convention). The
    nested context is the definition's `@context` merged over the parent's.
- **Grammar:** whitespace-separated tokens. Every token but the last is a scope
  name, matching `[A-Za-z][A-Za-z0-9_-]*`. The last token is the type term:
  - containing `:`: an IRI, expanded with `prefixes`;
  - otherwise one of `object array string number integer boolean null`;
  - optionally followed by `[format=<name>]` or `[enum]`.
- **Matching:**
  - The selector's scopes must be a *subsequence* of `scope_chain` (descendant
    semantics).
  - A schema term with `format` matches only a node term with the same format.
  - A bare schema term (no format, no enum) matches any node term with that
    `schema_type`.
  - `integer` nodes also carry a `number` term.

- [ ] **Step 1: Write failing tests:**
  - `test_parse_scoped_iri`:
    `parse_selector("sidebar ex:TodoItem", {"ex": "http://ex/"}) ==
    Selector(("sidebar",), TypeTerm(iri="http://ex/TodoItem"))`.
  - `test_parse_schema_format`: `"string[format=date]"` →
    `TypeTerm(schema_type="string", format="date")`.
  - `test_parse_errors`: `""`, `"sidebar"` (no type term), `"Sidebar! ex:X"`,
    `"strng"` and `"string[format=]"` each raise `SelectorError`.
  - `test_descendant_scope_match`: `("sidebar",)` matches the chain
    `("page", "sidebar", "row")`. `("row", "sidebar")` does not.
  - `test_term_index`: for terms `[iri X, schema string format date, schema string]`,
    the selector `string` matches at index 2, `string[format=date]` at index 1,
    and `X` at index 0.
  - `test_term_definition_class_and_nested`: with the WorkflowPolicy context,
    `term_definition(ctx, "initial_states")` returns `(
    "http://api.openteams.com/json-ld/InitialStates",
    "http://api.openteams.com/json-ld/States", …)`.
  - `test_stylesheet_bad_selector_is_400` (API) → problem type ending
    `ui-selector-invalid`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run → PASS, then the full suite.
- [ ] **Step 5: Commit** `feat(ui): stylesheet selectors and JSON-LD type terms`.

---

### Task 4: Cascade and component resolution

**Files:**
- Create: `scrud_django/ui/cascade.py`, `scrud_django/ui/documents/default-stylesheet.json`
- Test: `tests/test_ui_cascade.py`

**Interfaces:**
- Consumes: `parse_selector` and `selector_matches` (Task 3);
  `cached_template` (Task 2); the type IRIs (Task 1).
- Produces:
  - `@dataclass(frozen=True) Component(name: str, engine: str, mode: str,
    template: str, scopes: dict[str, str], stylesheets: tuple[str, ...],
    cache_key: tuple)`.
  - `@dataclass(frozen=True) Rule(selector: Selector, component: str, rank:
    tuple[int, ...])`. `rank` is the cascade position, with higher winning.
  - `effective_rules(contexts: list[str], linked: tuple[str, ...] = ()) ->
    tuple[Rule, ...]`.
  - `resolve_component(rules, scope_chain, terms, mode) -> Component`.
  - `builtin_component(name: str) -> Component`. Built-ins are read from
    `scrud_django/ui/builtins/<name minus "scrud:">.html` (Task 5 creates them).
  - `load_components(names: set[str]) -> dict[str, Component]`.
- **Data access:** presentation is resolved with system rights. Read `Resource`
  rows of the two types directly through `Resource.objects`, which uses the
  router's read database. Never call view or permission code here.
- **Cascade rank** (tuple, compared lexicographically):
  1. `(0, 0, i)`: the built-in `default-stylesheet.json`, by rule index `i`.
  2. `(1, order, pk, i)`: stylesheets in `""`.
  3. `(2, k, order, pk, i)`: stylesheets in the k-th context of `contexts`
     (excluding `""`), in list order.
  4. `(3, j, i)`: stylesheets named in `linked`, in order. Their names are looked
     up in `""` and then in `contexts`.
- **Precedence** for candidates that match, compared in order, highest wins:
  1. `len(selector.scopes)`;
  2. `-term_index`, so an earlier, more specific node term wins;
  3. `rank`.

  Take the best candidate whose component exists and has the requested `mode`.
  If none does, use the built-in for the node's first schema term:
  `scrud:<schema_type>` for view, `scrud:<schema_type>-edit` for edit, falling
  back to `scrud:json`.
- **Unknown names:** a rule whose `component` name is unknown is skipped. A
  component name is resolved in `""` and then among the built-ins.
- **Per-request memo:** `effective_rules` is memoised for the duration of one
  request on `(tuple(contexts), linked, frozenset((pk, etag) of the stylesheets
  read))`. Hold the memo in a `contextvars.ContextVar` that the renderer sets per
  request.

- [ ] **Step 1: Write failing tests** (create resources with `ResourceRegistration`
  or the API as superuser):
  - `test_more_scopes_wins`: rules `ex:L → a` and `sidebar ex:L → b`. In the chain
    `("sidebar",)` the result is `b`; in `()` it is `a`.
  - `test_type_iri_beats_schema_type`: `object → a` and `ex:L → b`, same scopes →
    `b`.
  - `test_context_beats_default_and_list_order`: the same selector in `""` → `a`,
    in `project-a` → `b`, in `project-b` → `c`.
    - contexts `["project-a", "project-b"]` → `c`;
    - `["project-b", "project-a"]` → `b`;
    - `["other"]` → `a`.
  - `test_wrong_mode_falls_through`: the best rule's component is view-only and
    mode is `edit`. A lower rule with an edit component wins. With none, the
    result is the built-in `scrud:object-edit`.
  - `test_stylesheet_cannot_use_other_context_component`: not applicable, because
    components exist only in `""`. Instead, assert that a rule naming
    `scrud:string` resolves to the built-in, and that a rule naming a missing
    component is skipped.
  - `test_rules_memo_invalidated_by_etag`: change a stylesheet (new etag) within
    the same memo scope → the new rules apply.
  - `test_linked_stylesheet_ranks_highest`: a stylesheet `extra` in `""` named in
    `linked=("extra",)` beats the same selector in a `project-a` stylesheet, for
    contexts `["project-a"]`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `default-stylesheet.json` maps each schema term to
  its built-in, view and edit pairs included. The built-in names are `scrud:object`,
  `scrud:array`, `scrud:string`, `scrud:number`, `scrud:boolean`, `scrud:null` and
  `scrud:json`, plus the `-edit` variants and `scrud:enum-edit`, `scrud:date-edit`
  and `scrud:uri`.
- [ ] **Step 4:** Run → PASS, then the full suite.
- [ ] **Step 5: Commit** `feat(ui): CSS-like cascade across RBAC contexts`.

---

### Task 5: Built-in components

**Files:**
- Create: `scrud_django/ui/builtins/{object,object-edit,array,array-edit,string,string-edit,number,number-edit,boolean,boolean-edit,null,enum-edit,date-edit,uri,json,page,collection,notice}.html`
- Test: `tests/test_ui_builtins.py`

**Interfaces:**
- **Template context contract** (Task 6 builds it; component authors rely on it):

  | Key | Value |
  |---|---|
  | `value` | the node's JSON value (authorized content) |
  | `schema` | the node's JSON Schema (`{}` if none) |
  | `pointer` | the node's JSON Pointer in the resource (`""` at the root) |
  | `mode` | `"view"` or `"edit"` |
  | `label` | the schema `title`, else the property name, else `""` |
  | `children` | object node: `{property: SafeString}`; array node: `[SafeString]`; otherwise `{}` |
  | `resource` | `{"href", "etag", "type", "contexts", "decisions": {"GET","PUT","DELETE": "allowed"/"possibly"/"denied"}}` |
  | `scope` | list of scope names (outermost first) |
  | `errors` | list of message strings for this pointer |

- **Edit-mode input protocol** (consumed by `scrud-ui.js`, Task 8):
  - Inputs carry `name="<pointer>"`.
  - Checkboxes use `type="checkbox"` and are read as booleans.
  - `number` and `integer` inputs use `type="number"` and are read as numbers.
  - `scrud:array-edit` renders an "add" row whose inputs are named
    `<pointer>/-/<prop>`.
  - Each edit component wraps its inputs in no `<form>`. The resource frame
    (Task 6) is the form.
- **Built-in templates** use only the Django default builtins: `{% for %}`,
  `{% if %}`, `{{ … }}`, `|default` and `|json_script`.
  - `json.html` shows `value` pretty-printed in `<pre>`. Task 6 passes the dumped
    string as `value_json`, which is also in the context, for this one template.
  - `page.html` renders a full HTML document:
    - `<meta name="csrf-token" content="{{ csrf_token }}">`;
    - `<script src="{{ static.htmx }}">` and `{{ static.scrud_ui }}`, with the
      static URLs passed in by Task 7;
    - `{{ body }}` as the main element.
  - `collection.html` renders `children` (a list of resource frames) with
    `count`, `next` and `previous` links.
  - `notice.html` renders `{{ message }}` with a `role="alert"` region.

- [ ] **Step 1: Write failing tests:** render each built-in through
  `builtin_component(...)` and `cached_template` with handmade contexts.
  - `test_string_view_and_edit`: view → contains `Buy milk`. Edit → contains
    `name="/items/0/text"` and `value="Buy milk"`.
  - `test_boolean_edit_checked`: `value=True` → `type="checkbox"` and `checked`.
  - `test_array_edit_has_append_row`: contains `name="/items/-/text"` when
    `schema.items.properties` has `text`.
  - `test_enum_edit_options`: has `<option` per enum value, with the current one
    `selected`.
  - `test_errors_rendered`: `errors=["Too long"]` → contains `Too long` in the
    `role="alert"` region.
  - `test_builtins_escape`: `value="<script>x</script>"` → no raw `<script>x` in
    view or edit output (Review Focus 3, built-ins).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** the templates. Keep the markup semantic, with labels
  and `aria-invalid` on errors. Use no inline styles beyond class names.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(ui): built-in generic components`.

---

### Task 6: The render tree

**Files:**
- Create: `scrud_django/ui/render.py`, `scrud_django/ui/authz.py`
- Create: `tests/static/todo_list/{__init__.py,schema.json,ld.json}`
- Modify: `tests/resource_types.py` (register `tests://TodoList`, slug `todo-lists`), `tests/fixtures.py` (fixtures below)
- Test: `tests/test_ui_render.py`

**Interfaces:**
- Consumes: `effective_rules` and `resolve_component` (Task 4); `ld.*` and
  `TypeTerm` (Task 3); `cached_template` (Task 2); the built-ins (Task 5).
- Produces:
  - `render_resource(content: dict, *, resource_type: ResourceType, href: str,
    etag: str | None, request, scope_chain: tuple[str, ...] = (), pointer: str =
    "", errors: dict[str, list[str]] | None = None, mode: str | None = None) ->
    SafeString`. It renders the node at `pointer`, wrapped in a **resource frame**
    if `pointer == ""`; otherwise it renders a frame around just that node.
  - `render_collection(envelope: dict, *, resource_type, href, request,
    scope_chain) -> SafeString`. Each item is rendered with `render_resource`
    using that item's own contexts.
  - `authz.decisions_for(request, type_uri: str, content: dict) -> dict[str, str]`.
  - `authz.refusal_messages(request, type_uri: str, body: dict, stored: dict) ->
    list[str]`.
- **Resource frame** (emitted by `render_resource`, not by component authors):
  ```html
  <form class="scrud-resource" data-scrud-href="…" data-scrud-etag="…"
        data-scrud-scope="sidebar row" data-scrud-pointer=""
        hx-put="…" hx-ext="scrud-json" hx-trigger="change, submit"
        hx-target="this" hx-swap="outerHTML">
    <script type="application/json" class="scrud-content">…json_script…</script>
    …component HTML…
  </form>
  ```
  - `hx-put` / `hx-ext` / `hx-trigger` are present only in edit mode. In view
    mode the element is a `<div>` with the same data attributes.
  - A root frame gets a delete button
    `<button type="button" hx-delete="…" hx-ext="scrud-json"
    hx-confirm="Delete?">` only when `decisions["DELETE"] == "allowed"`.
  - `scrud-ui.js` adds `If-Match` to every request from a frame, the delete
    included.
  - Errors with pointer `""` render through `scrud:notice` at the top of the
    frame.
- **Walk:** this is the only algorithm the tests don't fully determine.
  ```
  # contexts = the enclosing resource's rbac_contexts_of(content); every recursive call below
  # also passes contexts and linked_for_children (omitted from the calls for brevity)
  render_node(value, schema, pointer, scope_chain, ld_ctx, property_iri, class_iri, depth, contexts, linked=()):
    if depth > max_body_depth(): component = scrud:json
    schema = resolve local "$ref" (with a visited set; a cycle → scrud:json)
    terms = [class_iri?, property_iri?] + schema terms (format, enum, type; integer adds number)
            where "type" is the schema's, or inferred from the value if absent,
            and a value whose JSON type ≠ schema type gets only the inferred term + scrud:json
    component = resolve_component(effective_rules(contexts, linked), scope_chain, terms, mode)
    linked_for_children = linked + component.stylesheets   # a component's stylesheets apply to its subtree
    children:
      object & schema has "properties": for each property present in value:
         (p_iri, c_iri, sub_ctx) = ld.term_definition(ld_ctx, prop)
         child_scope = scope_chain + (component.scopes[prop],) if prop in component.scopes
         children[prop] = render_node(value[prop], schema.properties[prop], pointer+"/"+escape(prop),
                                      child_scope, sub_ctx, p_iri, None, depth+1)
      array & schema has "items": for i, item:
         child_scope = scope_chain + (component.scopes["*"],) if "*" in component.scopes
         children.append(render_node(item, schema.items, pointer+"/"+i, child_scope, ld_ctx,
                                     None, class_iri_of_items, depth+1))
         where class_iri_of_items = item["@type"] (expanded) if present, else the enclosing term's class_iri
      oneOf/anyOf/allOf/additionalProperties-only: no children (component falls to scrud:json unless styled)
    render with the context contract; on any exception from a stored component's render,
      log a warning and render the built-in for the same mode, prefixed by
      <!-- scrud-ui: component "<name>" failed -->
  ```
  - The array node itself is rendered with `property_iri` from its parent and no
    class.
  - The term definition's class IRI applies to the array's elements, not to the
    array.
- **Mode:** `edit` iff `decisions["PUT"] in ("allowed", "possibly")`, unless the
  caller forces a mode. Nodes whose schema has no editable built-in
  (`$ref` cycles, `oneOf`, etc.) still resolve with the mode; the default
  stylesheet maps them to `scrud:json` for both modes (read-only).
- **`decisions_for`:**
  - For each of `GET`, `PUT` and `DELETE`, compute `scrud_django.permissions.
    may_act_in_context(request, Permission(f"http.{m}", type_uri), content,
    rbac_contexts_of(content))`.
  - For `PUT` only, if it's False, evaluate it again inside
    `scrud_django.permissions._checking_write()`. `True` there yields
    `"possibly"`, because the `scrud_workflow` operator allows and the workflow
    check decides on submit.
  - Otherwise each result is `"allowed"` or `"denied"`.
- **`refusal_messages`:**
  - Returns `[]` unless `may_read_in_context(request, type_uri, stored,
    rbac_contexts_of(stored))`.
  - Otherwise, for each context of `body`, calls
    `access.policy_for(request).explain(Permission("http.PUT", type_uri), ctx,
    condition_resource(body, ctx))`.
  - Collects the `message` of each condition whose decision isn't `ALLOWED`, in
    order, without duplicates.
- **Test fixtures** (in `tests/fixtures.py`):
  - **`tests://TodoList` schema:** `title` (string), `rbac_context` (string),
    `items` (array of `{text: string, done: boolean}`, required `text`).
  - **ld.json:** `"ex": "http://example.org/todo#"`,
    `"@type": "http://example.org/todo#TodoList"` and
    `"items": {"@id": "ex:items", "@type": "ex:TodoItem"}`.
  - **`todo_owner_login`:** a role granting `http.GET`, `http.PUT` and
    `http.DELETE` on `tests://TodoList` and `http.POST` on its list type, plus
    `http.GET` on the UI types, assigned in `project-a`.
  - **`todo_reader_login`:** GET only, in `project-a`.
  - **`todo_list`:** a TodoList in `project-a` with two items, one of them done.
  - **`ui_author_login`:** a role with `http.GET`, `http.POST`, `http.PUT` and
    `http.DELETE` on both UI types (and their list types), assigned in `""`.

- [ ] **Step 1: Write failing tests:**
  - `test_unstyled_list_renders_generic` (owner): HTML contains the title, both
    item texts, `name="/items/1/done"` with `checked`, and `hx-put="<detail url>"`.
  - `test_reader_gets_view_mode`: same list for `todo_reader_login` → no `<input`,
    no `hx-put`, no `hx-delete`. For the owner, `hx-delete` is present.
  - `test_component_stylesheets_apply_to_subtree`: the TodoList component lists
    `stylesheets: ["rows"]`. A rule from `rows` styles the items. A sibling
    TodoList rendered without that component isn't affected.
  - `test_styled_row_by_class_iri`: add a Django component `todo-row` (view,
    template `<li class="row">{{ value.text }}</li>`) and a `""` stylesheet rule
    `ex:TodoItem → todo-row`. Use prefixes `{"ex": "http://example.org/todo#"}`
    and view mode (reader). → two `<li class="row">`.
  - `test_property_iri_styles_container`: rule `ex:items → my-list` (template
    `<ul class="mine">{% for c in children %}{{ c }}{% endfor %}</ul>`) → one
    `<ul class="mine">` containing the rows.
  - `test_component_opens_scope_for_children`: the TodoList component has
    `scopes: {"items": "row"}`. Rule `row ex:TodoItem → compact` beats
    `ex:TodoItem → todo-row`.
  - `test_context_stylesheet_applies_only_there`: a `project-a` stylesheet styles
    the list in `project-a`; a TodoList in `project-b` keeps the default.
  - `test_jinja_component_renders`: same as the styled row, with engine `jinja2`.
  - `test_workflow_put_is_possibly` (Review: workflow): `todo_creator_login`
    viewing a `tests://ToDo` → `decisions["PUT"] == "possibly"`, so the frame has
    `hx-put`.
  - `test_refusal_messages_readers_only`: a policy with
    `{"http.PUT": {"tests://TodoList": {"condition": "size(items) < 3", "message":
    "At most 3 items"}}}`:
    - a reader with that grant gets `["At most 3 items"]` for a 3-item body;
    - a user who can't GET gets `[]`.
  - **Review Focus 1** — `test_content_not_matching_schema`: an item with
    `"done": "yes"` and an unknown key `"x": 1` → 200-equivalent HTML. The
    unknown key is absent, and `"yes"` appears inside a `<pre>` (scrud:json).
  - **Review Focus 2** — `test_failing_component_falls_back`: a Jinja component
    `{{ value.text.upper().nope() }}` → the output contains `<!-- scrud-ui:
    component "bad" failed -->` and the generic rendering of the item.
  - **Review Focus 3** — `test_stored_components_escape`: an item text
    `<script>alert(1)</script>` → not present raw, in Django and Jinja stored
    components alike.
  - **Review Focus 4** — `test_recursive_ref_terminates`: a schema with
    `definitions.Node.properties.next.$ref = "#/definitions/Node"` and content
    nested 3 levels → renders. A cyclic `$ref` resolves to scrud:json at the cycle
    and doesn't raise `RecursionError`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** `render.py` and `authz.py`. Escape JSON Pointer
  segments per RFC 6901 (`~` → `~0`, `/` → `~1`).
- [ ] **Step 4:** Run → PASS, then the full suite on SQLite.
- [ ] **Step 5: Commit** `feat(ui): render resources through resolved components`.

---

### Task 7: The HTML renderer, pages, fragments and errors

**Files:**
- Create: `scrud_django/ui/renderer.py`
- Modify: `scrud_django/mixins/view_mixins.py` (`ResourceViewMixin`: add
  `get_renderers` and `finalize_response` overrides that delegate to
  `scrud_django.ui.renderer`, only when `scrud_django.ui.is_enabled()`)
- Test: `tests/test_ui_http.py`

**Interfaces:**
- Consumes: `render_resource`, `render_collection` and `authz.*` (Task 6); the
  built-ins `scrud:page` and `scrud:notice` (Task 5).
- Produces:
  - `ScrudHTMLRenderer(BaseRenderer)`, with `media_type = "text/html"`,
    `format = "html"` and `charset = "utf-8"`.
  - `ui_renderers(renderers: list) -> list`, which inserts `ScrudHTMLRenderer()`
    immediately before DRF's `BrowsableAPIRenderer` if present, else appends it.
  - `finalize_html(request, response) -> None`. It sets `Content-Type:
    text/html; charset=utf-8` and adds `Vary` when the accepted renderer is
    `ScrudHTMLRenderer`, overriding the `application/problem+json` that the
    exception handlers set.
- **Render dispatch** (in `ScrudHTMLRenderer.render(data, media_type,
  renderer_context)`):

  | Situation | Output |
  |---|---|
  | 2xx detail (retrieve / update) | `render_resource(data, …)`, with `href` = the request's absolute path without query, `etag` from `response["ETag"]`, `scope_chain` from `Scrud-UI-Scope`, `pointer` from `Scrud-UI-Pointer` |
  | 2xx list | `render_collection(data, …)` |
  | 201 create | `render_resource(data, …)` of the created resource |
  | 4xx/5xx problem, no write | `scrud:notice` with the problem `title`/`detail`, and never content |
  | 400/403 on PUT | re-render the **submitted** body (`request.data`) in edit mode at the stored ETag, with `errors[""]` = problem detail + `refusal_messages(...)` (403 only), and `errors[pointer]` for each problem `errors` key (dotted path → JSON Pointer) |
  | 412 on PUT | `scrud:notice` "This item changed since you opened it." plus a frame `<div hx-get="<href>" hx-trigger="load" hx-swap="outerHTML" hx-headers='{"Scrud-UI-Scope": …, "Scrud-UI-Pointer": …}'>` that reloads the current state |

  - Not `HX-Request`: wrap the output in `scrud:page`. For page components,
    resolve the term `scrud:page` in the root scope; the default maps it to the
    built-in.
  - `HX-Request`: return the fragment.
  - The stored-ETag lookup for 400/403 is the `If-Match` the request sent.

- [ ] **Step 1: Write failing tests** (safetydance `When.http.get(url,
  HTTP_ACCEPT="text/html")`, etc.):
  - `test_html_detail_page_and_fragment`:
    - without `HX-Request` → `<!DOCTYPE html>`, `csrf-token`, and
      `/static/scrud_ui/htmx.min.js`;
    - with `HTTP_HX_REQUEST="true"` → no `<!DOCTYPE`;
    - `Vary` includes `Accept`, `HX-Request` and `Scrud-UI-Scope`.
  - `test_json_unchanged_and_browsable_api_reachable`:
    - `Accept: application/json` → the same JSON as before;
    - `?format=api` with `Accept: text/html` → DRF's browsable API (contains
      `Django REST framework`).
  - `test_scope_header_selects_component`: `Scrud-UI-Scope: sidebar` with a
    `sidebar ex:TodoList` rule → that component's markup.
  - `test_pointer_header_renders_node`: `Scrud-UI-Pointer: /items/1` → only that
    item's frame. `data-scrud-pointer="/items/1"`.
  - `test_put_json_returns_html_fragment`: the owner PUTs the full JSON with
    `If-Match`, `Accept: text/html` and `HX-Request` → 200 HTML showing the new
    text, and a frame `data-scrud-etag` equal to the response `ETag`.
  - `test_put_refused_shows_messages_html_only`: the condition policy from Task 6
    and a 3-item body:
    - HTML → 403, `text/html`, contains `At most 3 items`;
    - the same PUT with `Accept: application/json` → the problem body has **no**
      `At most 3 items` (unchanged JSON API).
  - `test_put_stale_etag_html_412`: → 412, `text/html`, contains `hx-get` and the
    notice.
  - `test_list_html`: GET `todo-lists/` as HTML → one frame per readable list.
  - **Review Focus 5** — `test_unauthorized_html_has_no_content`:
    - anonymous GET of a list's detail as HTML → 401 or 403 (whatever the JSON
      status is), `text/html`, without the list title;
    - `login_other_context` → 404 `text/html`.
  - `test_without_ui_app_html_is_browsable_api`: with `scrud_django.ui` removed
    from `INSTALLED_APPS` (override) → `Accept: text/html` gives the browsable API.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
  - The `ResourceViewMixin` hooks import from `scrud_django.ui` lazily, inside
    the `is_enabled()` branch.
  - `DocumentMediaTypeMixin` views keep their document renderer first for GET;
    the HTML renderer is still reachable with `Accept: text/html`.
- [ ] **Step 4:** Run → PASS, then the full suite. Also `uv run pytest -q
  --reverse`.
- [ ] **Step 5: Commit** `feat(ui): text/html representation of resources`.

---

### Task 8: Client extension and static assets

**Files:**
- Create: `scrud_django/ui/static/scrud_ui/htmx.min.js` (htmx **2.0.4**, from
  `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`; record its sha384 in a
  comment in `scrud-ui.js` and in `docs/ui.md`)
- Create: `scrud_django/ui/static/scrud_ui/scrud-ui.js`
- Test: `tests/test_ui_static.py` (Python-side checks only; Task 9 covers browser
  behaviour)

**Interfaces:**
- Consumes: the resource frame and the input protocol (Tasks 5 and 6).
- Produces: the htmx extension `scrud-json`. Its `encodeParameters` and
  `onEvent` (`htmx:configRequest`) do the following:
  1. Start from the `JSON.parse` of the frame's `script.scrud-content`.
  2. For each named input in the frame, apply its value at its JSON Pointer:
     - checkbox → `checked`;
     - `type=number` → `Number(value)`, with empty → omit;
     - otherwise the string.
  3. Resolve `-` segments, one new element per distinct prefix per submission. An
     append row whose inputs are all empty is ignored.
  4. Send it with `Content-Type: application/json`.
  5. Set headers:
     - `If-Match` = `data-scrud-etag` (quoted as an ETag, if it isn't already);
     - `Accept: text/html`;
     - `Scrud-UI-Scope` and `Scrud-UI-Pointer` from the frame;
     - `X-CSRFToken` from `meta[name=csrf-token]`.

  A frame with a non-empty `data-scrud-pointer` applies its inputs to the
  **whole** resource content. Its script block holds the whole content, and the
  renderer emits it so. The response replaces only that frame.
  - Also sets `htmx.config.responseHandling` so that 400, 403 and 412 swap like
    200 and other errors don't.
  - Also sets `htmx.config.selfRequestsOnly = true`.

- [ ] **Step 1: Write failing tests:**
  - `test_static_assets_present`: both files exist under the app's `static/`.
    `htmx.min.js` starts with the htmx 2.0.4 banner or contains `version:"2.0.4"`.
  - `test_page_references_static`: rendered page HTML references
    `{% static %}`-resolved URLs for both files (uses `STATIC_URL`).
  - `test_fragment_frame_embeds_whole_content`: a `Scrud-UI-Pointer: /items/1`
    fragment's `script.scrud-content` parses to the full resource content.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** Download htmx with `curl -fsSL` into the path. Write
  `scrud-ui.js` as an ES2017 IIFE with no dependencies besides htmx. Adjust
  `render_resource` so a node frame embeds the whole content.
- [ ] **Step 4:** Run → PASS, then the full suite.
- [ ] **Step 5: Commit** `feat(ui): htmx client extension for JSON-pointer edits`.

---

### Task 9: To-do demo and browser acceptance

**Files:**
- Create: `demo/demo/TodoList-schema.json`, `demo/demo/TodoList-ld.json`
  (the same shape as the test type, type IRI `http://example.org/todo#TodoList`)
- Create: `demo/demo/management/__init__.py`, `demo/demo/management/commands/__init__.py`, `demo/demo/management/commands/seed_todo_demo.py`
- Modify: `demo/demo/urls.py` (register `todo-lists`), `demo/demo/settings.py` (`"scrud_django.ui"` in `INSTALLED_APPS`)
- Test: manual Playwright run (below); no new pytest module

**Interfaces:**
- **`seed_todo_demo`** (idempotent) creates:
  - users and roles:
    - `owner` / `owner`: GET, PUT, DELETE and list POST on TodoList in `project-a`;
      GET on the UI types in `""` and `project-a`;
    - `reader` / `reader`: GET only;
    - `author` / `author`: global writes to the UI types in `""`;
  - the list "Groceries" in `project-a` with 2 items;
  - components:
    - `todo-list-detail`, with `scopes: {"items": "row"}`;
    - `todo-row-edit`: a checkbox and a text input, named per the protocol;
    - `todo-row`;
    - `todo-list-compact`;
    - `todo-page`, which renders `body` in a `<main>` and an `<aside>` with
      `hx-get="/api/todo-lists/" hx-trigger="load"` and `hx-headers='{"Scrud-UI-Scope":
      "sidebar", "Accept": "text/html"}'`;
  - stylesheets:
    - in `""`: `ex:TodoList → todo-list-detail`;
      `sidebar ex:TodoList → todo-list-compact`; `row ex:TodoItem → todo-row`
      and `todo-row-edit`; `scrud:page → todo-page`;
    - in `project-a`: `sidebar ex:TodoItem → todo-row` (restyles the sidebar
      rows).

- [ ] **Step 1:** Implement the type registration, settings and the seed command.
  Run `cd demo && uv run python manage.py migrate && uv run python manage.py
  seed_todo_demo` → it exits 0. Run it a second time → also exits 0 with no
  duplicates.
- [ ] **Step 2:** `uv run python manage.py runserver 8765` in the background.
- [ ] **Step 3: Browser acceptance** (Playwright MCP). Log in at
  `/api-auth/login/` as `owner`, open `/api/todo-lists/<id>/`, then:
  - **add** an item "Eggs" via the append row → it appears after the swap;
  - **check off** the first item → the checkbox stays checked after a reload;
  - **rename** the second item → the new text persists after a reload.

  Then:
  - log in as `reader` → the same URL has no inputs;
  - open two tabs as `owner`, edit in one, then edit in the other → the 412
    notice appears and the frame reloads.

  Take a screenshot of each state into the scratchpad.
- [ ] **Step 4:** Stop the server. Confirm `demo/db.sqlite3` is untracked
  (gitignored).
- [ ] **Step 5: Commit** `feat(demo): to-do list rendered from UI resources`.

---

### Task 10: Docs, release notes, version

**Files:**
- Create: `docs/ui.md` (MyST), added to the `docs/index.md` toctree after `installation`
- Modify: `docs/release-notes.md` (a new `## 0.16.0` section at the top), `pyproject.toml` (`version = "0.16.0"`), `uv.lock`
- Modify: `README.md` (one paragraph and a link to the UI docs)

**Content of `docs/ui.md`:**
- enabling the app (`pip install scrud-django[ui]`, `INSTALLED_APPS`);
- the two resource types and their schemas;
- the selector grammar and the cascade, with rank and precedence as in Task 4;
- the template context contract and input protocol (Tasks 5 and 6);
- the fragment headers;
- the security model:
  - components are trusted, global-only, unsandboxed;
  - phase 3 adds a Jinja sandbox;
  - recommended CSP;
- the htmx version and its sha384.

**Release notes:** "Added". The behaviour with `scrud_django.ui` absent is
unchanged.

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2:** `uv run --group docs sphinx-build -W -b html docs docs/_build` →
  succeeds with no warnings.
- [ ] **Step 3:** Run the full verification set from Global Constraints → all
  pass.
- [ ] **Step 4: Commit** `docs: UI components; release 0.16.0 notes`.

---

## Parallel track: js-scoped-rbac (openteams/js-scoped-rbac, tracking #7)

This is a separate repo, so it gets its own plan when it starts. It doesn't block
phase 1. **Phase 2's client-side editability depends on it.** The order follows the
dependencies between the issues:

| Order | Issue | Deliverable | Done when |
|---|---|---|---|
| 1 | #6 | TS 5, ESLint, maintained runner, ESM + browser bundle, GitLab CI, corrected `repository.url` | CI green on the existing tests |
| 2 | #1 | `RootPolicy.fromJson` / `toJson` for the wire format; rejecting malformed documents | `wire.json`, `reject.json`, `numbers.json` pass |
| 3 | #2 | `"*"` union at every level | `wildcards.json` passes; the superuser policy allows |
| 4 | #3 | CEL via a JS CEL library; `evaluate` (three-valued), `explain`, messages | `grammar.json`, `undetermined.json`, `messages.json`, `pool.json` pass |
| 5 | #4 | `registerOperator`; unregistered and invalid conditions are errors | `operators.json` passes |
| 6 | #5 | Server-equivalent merging; `addAll` fixed | `merging.json` passes; all fixtures pass (closes #7) |

The fixture runner loads the files from a pinned django-scoped-rbac tag, using a
sync script that copies `tests/conformance/fixtures/`.

**Phase 2 (outside this plan) then adds:**
- serving the bundle from `scrud_django/ui/static/`;
- `resource.decisions` computed client-side for unsaved edits;
- a `scrud_workflow` client operator returning `undetermined`;
- server-side per-field editability analysis.
