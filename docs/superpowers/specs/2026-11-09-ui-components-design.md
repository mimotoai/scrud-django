# UI components as resources — design

Date: 2026-11-09
Status: builder-checked; awaiting owner approval

Notation: **(owner)** marks a decision the owner made. **(call)** marks a decision
made while writing this, which the owner can overrule. Anything unmarked follows
from the existing code.

## Intent

scrud-django stores resource types as data, described by JSON Schema and JSON-LD,
and authorizes every request with django-scoped-rbac. It has no UI. The owner wants
the application to render **any** JSON ResourceType as HTML. The rendering is built
from small **htmx components that are themselves resources** in the application, and
it shows each resource **exactly as the viewing user is authorized to see and change
it**. A field the user can't edit must not render as editable. An action the user
can't perform must not be offered.

Prior art is gitlab.com/openteams/scrud-nuxt, a Vue prototype abandoned in 2021.
Ideas it contributes:

- discovery from schema and context links;
- looking up components by JSON-LD term;
- separate components for display and input;
- If-Match / ETag handling.

Its issue #23 planned "endpoints for component resources in the backend". It never
read the JSON Schema, never recursed into nested objects, and had no authorization.
The **stylesheet-like scoped selection** below was the original plan for scrud-nuxt
**(owner)**, but no code for it exists in any branch.

## The motivating example (owner)

- A **TodoList** resource type holds items.
- The **list owner** creates the list and edits its state, and is the same person
  doing both.
- A **component author**, a different person or an agent, writes the components and
  stylesheets used to render to-do lists and their items. The component author needs
  no rights on anyone's lists.

**Phase 1 is done when the owner can add, check off and rename items in the
browser (owner).** The page must be rendered from component and stylesheet
resources, and every change must be authorized and validated by the server.

## Goals

1. Two new built-in resource types:
   - **UI components**: a template plus its engine.
   - **UI stylesheets**: ordered selection rules.

   Both are stored, versioned, ETagged and RBAC-protected like every other resource.
2. Any registered ResourceType renders as HTML with no authoring, using built-in
   generic components for the JSON Schema types.
3. **Components are chosen by JSON-LD `@type`, with a fallback to the JSON Schema
   type (owner).**
4. Selection is **scoped, like CSS (owner)**. Pages and components open named
   scopes, such as `sidebar`. Rules in separate **stylesheet resources (owner)** pick
   the component for a type within a scope.
5. **Stylesheets are per RBAC context, layered on the defaults (owner).**
6. Rendering follows the user's authorization:
   - A resource renders editable only when the user may write it.
   - Actions the user can't perform are not shown.
7. Edits made in the browser go through the same authorization, workflow, validation
   and conditional-request checks as JSON API writes.
8. A demo to-do app meets the phase 1 bar above.

## Non-goals (for now)

- **Rendering per-field editability before submission.** This is phase 2 (owner).
  In phase 1, field-level rules are enforced at validation time: the server rejects
  the write and the UI shows why.
- **Sandboxing untrusted component authors.** This is a goal for phase 3 (owner).
  Until then, component authors are trusted, roughly like people who edit a site's
  templates.
- **Fallback through RDF parent types** (`rdfs:subClassOf`). Later.
- **Generic editing of every JSON Schema construct (call).**
  - **Editable in phase 1:** the generic components edit `properties`, `items`,
    primitives, `format` and `enum`.
  - **View-only in phase 1:** `$ref`/`definitions`, `additionalProperties` maps
    and `oneOf`/`anyOf`/`allOf` render read-only. Where no component applies,
    they render as pretty-printed JSON.
  - **Still possible:** a component author can still provide an edit component
    for such a type. Only the demo type has to be editable in phase 1.
- **Replacing the JSON API.** HTML is an extra representation of the same resources.

## Anti-goals

These would make the work a failure even if it technically ran:

- **Showing a user anything they couldn't GET as JSON.** Templates receive only
  authorized content.
- **Any path where a browser write skips a check that a JSON write gets.** That
  includes RBAC (before and after the save), the workflow, schema validation, and
  If-Match.
- **A parallel set of "UI endpoints"** whose authorization has to be kept in step
  with the API by hand.
- **Rendering a field or action as available when the server would refuse it**, in
  cases where the policy decides it outright. "Undetermined" may render as possibly
  allowed.

## Phases

| Phase | Scope |
|---|---|
| 1 | Component and stylesheet resources, scoped resolution, per-context cascade, server-rendered HTML, htmx edits, field rules surfaced as validation errors, the to-do demo |
| 2 | Per-field editability before submission: server-side analysis of the policy and workflow conditions; client-side use of js-scoped-rbac (depends on its track below) |
| 3 | A sandboxed mode for untrusted authors (Jinja2 `SandboxedEnvironment`); `rdfs:subClassOf` fallback |

**Parallel track: js-scoped-rbac (owner asked for the issues).** The library is
the 2020 version 0.0.2 and can't evaluate today's policies. For example, a
superuser's `{"*":{"*":{"*":true}}}` denies everything. The work is filed under
tracking issue openteams/js-scoped-rbac#7:

- #1 the wire format;
- #2 the `"*"` union;
- #3 CEL conditions, three-valued decisions and explain;
- #4 operators;
- #5 merging and the `addAll` bug;
- #6 toolchain and a browser ESM bundle.

It is done when every django-scoped-rbac conformance fixture passes. Phase 1 does
not depend on it. Phase 2's client side does.

## Concepts

### UI component resource

The content holds:

- **template source**;
- **`engine`**: `django` or `jinja2` **(owner: components are Django or Jinja
  templates)**;
- **`mode`**: `view` or `edit` **(call; follows scrud-nuxt's render/input pairs)**;
- an optional **label** and **description**.

Components may include client-side JavaScript. **The restriction is on the server
(owner): templates are trusted for now, and a sandboxed version is a goal.**

Templates are compiled with `engines[engine].from_string(source)` and cached by
component identity and ETag **(call)**.

**Who writes components in phase 1 (owner): a global trusted role only.**
An unsandboxed template can run code on the server, and a component's scripts run
in every viewer's page. So:

- Components are stored only in the default context `""`.
- Writing them requires the global component-author role.
- Contexts customise rendering only through **stylesheets**, which choose among
  existing components.
- A create or update of a component in any other context is refused.
- Per-context component authoring waits for the phase 3 sandbox.

**Component identity (call).** A component has a `name` slug in its content, which
is unique site-wide because all components live in `""`. Rules refer to components
by name and resolve to the default context's components, then the built-ins.
Built-in names are reserved under a `scrud:` prefix and can't be claimed. The
built-in default stylesheet ships as a file that refers to built-in names, so it
doesn't depend on database ids.

**Template context is plain JSON (call).** It contains:

- the authorized content of the node;
- the node's JSON Schema;
- the node's JSON Pointer within the resource;
- the resource's URL and ETag;
- its `Allow` methods;
- the policy decisions for the actions the component may offer;
- the current scope chain;
- a helper for rendering child nodes (below).

It never contains model instances or the request. This is the part of sandboxing
that would be expensive to add later, because templates would come to depend on
method calls.

### UI stylesheet resource

An ordered list of rules **(call: JSON rules, not CSS-like text, so the stylesheet
validates against a JSON Schema)**:

```json
{"rules": [
  {"select": "ex:TodoList",           "component": "<component IRI>"},
  {"select": "sidebar ex:TodoList",   "component": "<component IRI>"},
  {"select": "sidebar ex:TodoItem",   "component": "<component IRI>"},
  {"select": "string[format=date]",   "component": "<component IRI>"}
]}
```

A selector is zero or more scope names followed by one **type term**:

- an `@type` IRI or compact IRI; or
- a JSON Schema type, optionally narrowed by `[format=…]`, and possibly
  `[enum]`.

The builder settles the exact grammar. It must stay small enough to validate and
explain.

### Scopes and pages

- **Pages are components (call).** A page is the root of the render tree.
- **Any component can open a named scope** around part of its output. Scopes nest,
  and a node's **scope chain** is the list of scopes opened above it.
- A request may also open a root scope, the replacement for a "view name".

## Resolving a component for a node

A node is the resource itself, or any value inside it reached by walking the
schema.

1. **Type terms for the node, most specific first:**
   - the node's JSON-LD `@type`, decided as follows **(call)**:
     - **The resource** has its ResourceType's type IRI.
     - **A field node** (the value under a property, including an array as a
       whole) matches the **property IRI**, i.e. the term's `@id` in the JSON-LD
       context.
     - **Each element of an array, and each object value,** matches a **class
       IRI** taken from:
       1. its own `@type` member, if the content has one;
       2. otherwise the term definition's `@type`. This follows the scrud
          contexts' existing convention of putting `@type` in term definitions,
          e.g. `WorkflowPolicy-ld.json`.

     So `sidebar ex:TodoItem` styles each row, and a rule on the property IRI
     (`ex:items`) styles the list container. The demo's JSON-LD context gives
     `items` both an `@id` and a `@type`.
   - then the JSON Schema type, with `format` before the bare type. `enum` gets a
     select.
2. **Candidate rules:** every rule in the effective stylesheets (below) whose type
   term matches and whose scope names all appear, in order, in the node's scope
   chain. Scope names need not be adjacent, as with CSS descendant selectors.
3. **Precedence (call; CSS-like), first difference wins:**
   1. more scope names matched;
   2. an `@type` term over a JSON Schema term;
   3. the later stylesheet in the cascade;
   4. the later rule within a stylesheet.
4. **The mode** is `edit` if the user may write the enclosing resource (phase 1),
   otherwise `view`. If the winning rule's component has the wrong mode, the
   builder decides between two options:
   - rules name a view/edit pair;
   - resolution keeps searching for a component with the right mode.
5. **Built-in fallback:** the built-in `object` component walks `properties` and
   the `array` component walks `items`, resolving each child with the same steps.
   So an unstyled TodoList renders as a generic object holding a generic array of
   generic objects.

A component renders children through the helper in its context. The helper can
open a scope for them, for example the sidebar TodoList rendering items under
`row`.

## Cascade and RBAC contexts (owner: per-context stylesheets over the defaults)

The effective stylesheets, lowest priority first:

1. scrud-django's **built-in default stylesheet**: JSON Schema types mapped to the
   built-in generic components. It's an ordinary resource that can be overridden.
2. **App-wide stylesheets**, in the default context `""`.
3. **Context stylesheets**: stylesheets stored in the RBAC context being rendered.
4. **Stylesheets a page links itself** **(call)**.

**Which context applies (call):** the context of the resource being rendered, not
the viewer's. It is re-decided where one resource embeds another, like a shadow DOM
boundary. A resource in several contexts applies them in `rbac_context` list order
**(call)**: later contexts win ties. This is one precedence step and easy to change.

Since a context's stylesheets live in that context, whoever may write stylesheets
in `project-a` styles `project-a` and nothing else. The existing policy model does
this with no new administrator role. Stylesheets can only choose among the
default context's components and the built-ins.

**Caching:** cache the resolved rule set per (context, ETags of its stylesheets)
**(call)**.

## Authorization in rendering

- Templates get only content the user may GET. Embedded resources are authorized
  separately, for the same user.
- **Actions** (edit, delete, add) are shown from the same decision the API makes:
  - the `Allow` methods scrudful computes for the user; or
  - `scoped_rbac.access.policy_for(request).evaluate(Permission(...), ctx,
    content)`.
  - `UNDETERMINED` renders as possibly allowed; the server decides on submit.
- **Workflow:** whether a resource is editable also depends on the workflow:
  - its current state;
  - `restrict_update`;
  - the transitions the user is authorized for, via the `scrud_workflow` operator.

  Workflow rules can restrict which fields may change. Superusers bypass workflow
  authorization. Phase 1 surfaces a workflow refusal as a validation error. Phase 2
  works out allowed edits ahead of time.
- **Field-level rules (owner).** scoped-rbac conditions are CEL over the resource's
  fields. Operators also receive the stored object. Workflow states are CEL over the
  content. Together these already restrict field edits on write. Phase 1 reports
  them as follows.
  - **Refusal messages: readers only, HTML only (owner).** When a browser write
    is refused, the HTML response shows the condition messages, but only if the
    user may read the resource. This mirrors how workflow refusals are already
    hidden from non-readers (`HiddenWorkflowTransitionError`).
  - **Where messages come from:** scoped-rbac's problem detail is generic and
    can't be changed from here, so the HTML write path re-runs
    `policy_for(request).explain(...)` on the refused permission to get the
    messages.
  - **Where they appear:**
    - RBAC condition messages render at the top of the form, because conditions
      carry no field path.
    - Workflow refusals render next to a field when a path is known, and at the
      top otherwise.
  - **The JSON API's error bodies don't change.**
- **Presentation is resolved with system rights; data is not (call).**
  - **Rendering:** resolving stylesheets and components while rendering a
    resource the viewer may read uses the system's rights, limited to the
    rendered resource's context, the default context and the built-ins.
  - **Consistency:** everyone who can read a resource sees it styled the same
    way, and the resolved-rules cache key needs nothing about the viewer.
  - **No data leaks:** templates only ever receive content the viewer may read,
    so presentation resources can't leak data.
  - **Direct access:** GET or write on component and stylesheet resources is
    authorized normally. Write access is the component-author role (see the open
    question on who may hold it).
  - Collection schemas and contexts are already public by design (follow-up
    `public-type-metadata`); component and stylesheet resources are not unless
    granted.

## HTTP shape

- **HTML representation at the resource's own URL (call).** A request with
  `Accept: text/html` gets HTML; JSON clients are unaffected. There are no separate
  UI routes (see anti-goals).
  - **Browsable API (call):**
    - With `scrud_django.ui` installed, the UI renderer takes `text/html` on
      resource routes, and DRF's browsable API stays reachable with
      `?format=api`.
    - Projects without the app keep today's behaviour, so the minor release
      stays backward compatible.
    - The demo keeps `/api-auth/login/`.
- **Full page or fragment:**
  - An `HX-Request` request gets the fragment for the requested node.
  - Otherwise the response is the full page, wrapped in the page component that
    resolution picks for the root.
- **Scope chain on fragment requests (call).** A partial update has lost its
  surrounding page, so the request carries its scope chain and JSON Pointer
  (header or `hx-vals`). The builder chooses the carrier. Without it, a
  re-rendered item could come back styled differently.
- **Writes.** Request bodies are JSON-only today (`RbacJSONParser`; form bodies get
  415). The browser path must end in the same update pipeline as a JSON PUT:
  - RBAC before and after the save;
  - `WorkflowValidator`;
  - schema validation;
  - If-Match.

  **(call)** Browser forms send changes keyed by JSON Pointer (`/items/2/done`,
  `/items/-` to append). The server applies them to the stored content at the ETag
  the form was rendered with (If-Match), then sends the full result through that
  pipeline. Fields the form didn't render are left untouched.

  The builder chooses where this lives: a parser for form bodies on PUT and POST
  when the request asks for HTML, or a conversion step. Known pitfalls:
  - a detail-route POST `@action` ignores If-Match (follow-up
    `detail-post-action-preconditions`);
  - new `@action` writes are refused by `checks_workflow` (`view_mixins.py:1080`).
- **Results:**
  - **Success** returns the re-rendered fragment with its new ETag.
  - **412** re-renders the current state with a notice.
  - **400 or 403** re-renders the submitted form with messages.
- **htmx and htmx's json-enc** (if used) are served as pinned static assets by
  scrud-django. Components don't load their own copies.

## Packaging (call)

- An optional Django app inside scrud-django, e.g. `scrud_django.ui`, plus an extra
  for `jinja2`.
- Built-in types, schemas and contexts go in `scrud_django/static/` and are
  registered in `built_in_resource_type_urls()` alongside `WorkflowPolicy` and
  `IndexingPolicy`.
- New type IRIs follow the existing `api.openteams.com` IRI pattern. Existing IRIs
  are stored data and must not be changed.
- **Linking a ResourceType to default components** isn't needed: the default
  stylesheet covers it. If it's added later, mirror `indexing_policy` and fix
  follow-up `json-resource-type-indexing-policy` first.
- **Release:** this is a feature, so bump the minor version. Publishing is
  automatic on merge (see `CLAUDE.md`).

## Demo

A to-do app in `demo/`:

- a `TodoList` type with items, each with text and done (`ex:TodoList` / `ex:TodoItem`
  or real IRIs);
- an owner policy, e.g. owner-scoped grants in the list's context;
- a global component-author role that writes components and stylesheets in `""`;
- a context-scoped role that may write only stylesheets in its own context;
- a page with a `sidebar` scope and a detail region;
- one context stylesheet that restyles `sidebar ex:TodoItem`.

**Acceptance:** with the owner logged in, adding, checking off and renaming items
works in a browser. A second user without write access sees the same lists
read-only, with no edit controls. A stale tab gets the 412 path.

## Testing expectations

Follow the repo's safetydance Given/When/Then conventions; steps go in
`tests/steps.py` only. Cover:

- resolution precedence;
- the cascade across contexts;
- the anti-goals (no unauthorized content in HTML; browser writes refused exactly
  where JSON writes are, workflow included);
- 412 handling;
- fragment rendering keeping its scope chain.

## Left to the builder

- the selector grammar's details;
- how rules handle view/edit pairs;
- the scope-chain carrier;
- where form-to-JSON conversion lives;
- the template-context helper API;
- names of the built-in components;
- the demo's IRIs;
- how a page component is addressed and mounted.

## Open questions for the owner

None. The builder check's questions were settled: two by the owner (who writes
components; refusal messages) and the rest as calls marked in the text.
