# Server-rendered UI

`scrud_django.ui` is an optional app that serves every resource URL as
`text/html` as well as JSON. The HTML is rendered on the server, node by node,
through templates (UI components) that are themselves stored as resources, and
chosen by CSS-like rules (UI stylesheets) layered per RBAC context. A page is
rendered exactly as the viewer is authorized: it offers editing only where the
policy lets that user PUT, and it never includes content the user cannot read.
The browser side is [htmx](https://htmx.org/) and a small script, `scrud-ui.js`.

Without `scrud_django.ui` in `INSTALLED_APPS`, scrud-django behaves exactly as
before.

This is phase 1 of the UI, with branches. The later phases are planned, not
built:

* **Phase 1** (this release): server-rendered HTML for every resource, stored
  components and stylesheets, [branches](#branches) of them written by anyone
  the policy lets write there, and the browser writing whole resources with
  plain JSON `PUT`s.
* **Phase 2**: per-field editability, and policy evaluation in the browser
  through js-scoped-rbac.
* **Phase 3**: components written by untrusted authors, in a sandbox.

## Enabling the app

Install the `ui` extra (it adds Jinja2) and add the app. It needs
`django.contrib.staticfiles` for htmx and `scrud-ui.js`:

```bash
uv add "scrud-django[ui]"
# or: pip install "scrud-django[ui]"
```

```python
INSTALLED_APPS = [
    ...
    "django.contrib.staticfiles",
    "scrud_django",
    "scrud_django.ui",
]
```

`scrud_django.urls` then serves more resource types, each with its own schema,
JSON-LD context and collection, written and read like any other resource under
the same policy:

| Resource type | IRI | Slug |
|---|---|---|
| UI component | `http://api.openteams.com/json-ld/UIComponent` | `ui-components` |
| UI stylesheet | `http://api.openteams.com/json-ld/UIStylesheet` | `ui-stylesheets` |
| UI CSS | `http://api.openteams.com/json-ld/UICss` | `ui-css` |
| UI branch | `http://api.openteams.com/json-ld/UIBranch` | `ui-branches` |
| UI branch version (read-only) | `http://api.openteams.com/json-ld/UIBranchVersion` | `ui-branch-versions` |
| UI push | `http://api.openteams.com/json-ld/UIPush` | `ui-pushes` |

HTML is chosen by content negotiation (`Accept: text/html`) at the resource's
own URL. `?format=api` still reaches DRF's browsable API.

## UI components

A component is a template that renders one node of a resource's JSON in one
mode. Its content:

| Property | Required | Meaning |
|---|---|---|
| `name` | yes | Unique site-wide. `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; the `scrud:` prefix is reserved for the built-ins. |
| `engine` | yes | `django` or `jinja2`. |
| `mode` | yes | `view` or `edit`. |
| `template` | yes | The template source. A template that does not compile is refused with a 400. |
| `scopes` | no | Maps a property name (`"*"` for array items) to the scope name its child is rendered in. |
| `stylesheets` | no | Names of stylesheets that also apply to the component's descendants. |
| `label`, `description` | no | Free text. |
| `rbac_context` | no | The component's authorizing context on its branch: the branch's context (`""` on `main`). |
| `branch`, `deleted`, `base` | no | The [branch fields](#branches) every UI item has. |

Whoever may write a component on a branch is trusted with the rendering of
every page that shows that branch (see [Security](#security)). Stored Jinja
templates run in `jinja2.sandbox.SandboxedEnvironment`. Stored Django templates run on a standalone engine with no loaders and no tag
libraries, so `{% load %}` of app tags is unavailable and `{% include %}` and
`{% extends %}` are refused at save. Both engines autoescape, and child HTML
passes through unescaped. Letting untrusted authors write components is phase
3.

### The node context

Every template is given plain JSON, plus its children's already rendered HTML:

| Key | Value |
|---|---|
| `value` | The node's JSON value. |
| `schema` | The node's JSON Schema. |
| `pointer` | The node's JSON Pointer in the resource (`""` for the resource). |
| `mode` | `"view"` or `"edit"`. |
| `label` | The schema's `title`, else the property name. |
| `children` | An object node: `{property: html}`. An array node: `[html, ...]`. |
| `child_items` | An object node: `[[property, html], ...]` in schema order. Empty otherwise. |
| `resource` | `{href, etag, type, contexts, decisions}`; `decisions` is `{GET, PUT, DELETE}`, each `allowed`, `possibly` or `denied`. |
| `scope` | The node's scope chain, a list of names. |
| `errors` | The messages for this node from a refused write. Always empty for the resource itself: its messages (and those for nodes that are not rendered) are shown as notices at the top of the frame. |
| `value_json` | The value as indented JSON (filled for stored components and `scrud:json`). |
| `input_id` | An HTML id unique to the node, for labels. |
| `append_row` | For an array node: whether the append row has an input to render. It is false when every item property is an object or array, or when the items are objects without properties (or empty `properties`) or arrays; `scrud:array-edit` then renders no `scrud-append` element. |
| `append_fields` | For an array node: `[{name, type, pointer_name}]` of its items' properties, for an append form. `type` is the property's schema type (the first non-null one of a list, through a local `$ref`), or `None`; `pointer_name` is `name` escaped as a JSON Pointer reference token (`~` as `~0`, `/` as `~1`), for input names such as `{{ pointer }}/-/{{ field.pointer_name }}`. |

Be careful with the name `items`. In a Django template `children.items`
resolves to a property named `items` (as the to-do demo's schema has), so
iterate `child_items` for every property. In Jinja, `children.items` is the
dict method; write `children["items"]`.

A node the schema does not describe is not rendered (an unknown key is ignored,
a missing property renders nothing); a value whose JSON type is not its
schema's, a `$ref` that cannot be resolved or loops, and nesting deeper than
`max_body_depth()` render as JSON (`scrud:json`). A stored component that fails
while rendering is replaced by the built-in for its node and mode, after an
HTML comment naming it.

### Built-in components

The built-ins are named `scrud:...`. They are Django templates and cannot be
overwritten.

| Mode | Components |
|---|---|
| view | `scrud:object`, `scrud:array`, `scrud:string`, `scrud:number`, `scrud:boolean` |
| edit | `scrud:object-edit`, `scrud:array-edit`, `scrud:string-edit`, `scrud:number-edit`, `scrud:boolean-edit`, `scrud:enum-edit`, `scrud:date-edit`, `scrud:uri-edit` |
| any | `scrud:null`, `scrud:uri`, `scrud:json`, `scrud:page`, `scrud:collection`, `scrud:notice` |

When no rule picks a usable component, the node gets the built-in for its
schema type (an integer uses the number one). In edit mode that is the `-edit`
built-in when there is one; a type without one (`null`) uses its any-mode
built-in; anything else renders as `scrud:json`, as does a node with no schema
type. `scrud:uri` links only values that start with `http://`, `https://` or
`mailto:` (in any case), or with a single `/` (not `//` or `/\`); any other
value is shown as text. In edit mode, `string[format=uri]` uses
`scrud:uri-edit`, an `<input type="url">`.

`scrud:string-edit` renders a `<textarea>` when the value contains a line break
(CR or LF), or when the string may be long: its schema has neither `format` nor
`maxLength`, or a `maxLength` over 200. Otherwise it renders
`<input type="text">`. A browser strips line breaks from a text input's value,
so multi-line text must not be edited in one. A custom string component should
follow the same rule.

`scrud:array-edit`'s append row renders a number input (`step="1"` for an
integer, `step="any"` for a number) or a checkbox for numeric and boolean item
properties, and a text input otherwise. A property whose type is `object` or
`array` gets no input (a text input cannot produce one), so a new item is
created without it and a required one fails server validation with a notice.
An array whose items are objects without `properties`, or arrays, gets no
append row at all, as does one whose item properties are all objects or arrays. For an array of items that are not
objects it renders one input named `<pointer>/-` of the item type, from the
`append_item_type` context key (`None` for object items), so the client sends
a number or a boolean, not a string.

`scrud:page` wraps a page (not a fragment). Its context is
`{title, csrf_token, static{htmx, scrud_ui}, css, body}`. `css` is the list of
the URLs of the page's CSS files (see "CSS items" below), in order: a custom
page component should link each one
(`{% for href in css %}<link rel="stylesheet" href="{{ href }}">{% endfor %}`).
A custom page component,
chosen by a rule on `scrud:page`, must be `mode: view` (one in edit mode is
ignored, and the built-in is used). It must include the CSRF meta tag
(`<meta name="csrf-token" content="{{ csrf_token }}">`) and both scripts
(`static.htmx` and `static.scrud_ui`), or the browser cannot write.

`scrud:collection` renders a page of a list. Its context is
`{children, count, next, previous, label}`: `children` is the list of each
resource's rendered frame, `count` the list's total count, `next` and
`previous` the URLs of the neighbouring pages (or `None`), and `label` the
schema's `title` (or `""`). Each resource in it is rendered in its own frame,
with its own contexts' stylesheets.

`scrud:notice` renders one message. Its context is `{message, level}`, where
`level` is `"error"` for every notice the server renders.

## CSS items

A CSS item (type `ui-css`) holds the text of a page's CSS: `name`, `css` (at
most 262144 characters, and never containing `</style`, which is refused with a
400), `order` (default 0), and the branch fields (`branch`, `deleted`, `base`,
`rbac_context`) every UI item has. A name is unique on its branch. The page
links the CSS items effective on the branch it shows, by `order` and then by
name, at `<api or prefix>ui-css-files/<name>/<etag>.css`. The ETag in the URL
makes a file immutable (`Cache-Control: private, max-age=31536000,
immutable`: a shared cache would not check the GET on the branch, so it must
not keep it): after an edit the old URL is a 404 and the page links the new
one. Anyone who can see a page can fetch its files; under a
published branch's prefix a viewer who may not GET the branch is refused as
for any other GET there, and a working branch's CSS is served only to a viewer
who may select it.

## UI stylesheets

A stylesheet maps selectors to components:

```json
{
  "name": "todo",
  "rbac_context": "",
  "prefixes": {"ex": "http://example.org/todo#"},
  "order": 0,
  "rules": [
    {"select": "ex:TodoList", "component": "todo-list-detail"},
    {"select": "sidebar ex:TodoList", "component": "todo-list-compact"},
    {"select": "string[format=date]", "component": "my-date"}
  ]
}
```

On a branch, a stylesheet's `name` is unique per context it applies to
(`applies_to`, see [Branches](#branches)); `order` (default 0) orders
stylesheets within a context; `prefixes` expand the IRIs in the selectors. The
selectors of every rule are parsed at save, and a rule that does not parse is
refused with a 400.

### Selector grammar

A selector is whitespace-separated tokens. Every token but the last is a scope
name (`[A-Za-z][A-Za-z0-9_-]*`). The last is the type term, one of:

* an IRI (a token containing `:`), expanded with the stylesheet's `prefixes`: a
  class from the JSON-LD context (the node's `@type`, or its term definition's
  class) or a property IRI;
* a schema type: `object`, `array`, `string`, `number`, `integer`, `boolean` or
  `null`, optionally followed by `[format=name]` or `[enum]`.

The scopes must be a subsequence of the node's scope chain (descendant
semantics). A component puts its children into scopes with its `scopes`; the
root scope chain comes from the `Scrud-UI-Scope` header.

A node's terms, most specific first, are its class IRIs, its property IRI, a
format term, an enum term and the bare schema type (an integer is also a
number). A selector matches a node term when both are the same IRI; or the
selector has a `format` and the node term has the same type and format; or the
selector is `[enum]` and the node term is an enum of that type; or the selector
is a bare type and the node term has that type, whatever its format or enum.
`scrud:page` is the term the page component is resolved with.

### The cascade

Rules are collected from the stylesheets that apply to the resource, each rule
with a rank (higher wins):

1. the default stylesheet `scrud:default` (`(0, 0, rule index)`);
2. stylesheets in context `""`, by `order`, then by creation (`(1, order, pk,
   rule index)`);
3. stylesheets in each of the resource's RBAC contexts, in the order of the
   resource's context list, later contexts winning (`(2, context index, order,
   pk, rule index)`);
4. stylesheets named by the `stylesheets` of the components above the node
   (`(3, position, rule index)`), looked up in `""` and the resource's contexts.

For a node, the best rule is the one with the most scopes, then the earliest
matching node term (a class beats a property, which beats a format, an enum and
the bare type), then the highest rank. The best rule whose component exists and
serves the render mode is used: every better rule whose component is missing or
does not serve the mode is skipped.

Presentation is resolved with system rights: reading stylesheets and components
does not need a grant on their types. A user who can read a resource sees it
styled even if the policy gives them no access to the stylesheets. Stylesheets in
the resource's contexts are applied, so a context's own presentation applies to
the resources in it. Stylesheets and components are read once per rendered
response, whatever the number of contexts on the page; an edit to one applies
from the next response.

## Modes, editing and the write path

The whole resource renders in one mode: edit when PUT is `allowed`, or
`possibly` (the policy allows it only inside a write's checks, as with a
`scrud_workflow` condition, and the workflow check decides on submit), else
view. Components of the other mode are skipped.

The server's write path is unchanged. In edit mode the browser sends an
ordinary JSON `PUT` with `If-Match`, built client-side by `scrud-ui.js` from the
content embedded in the frame (`<script class="scrud-content">`) and the inputs
named by JSON Pointer (`name="/items/0/text"`):

* In edit mode a root frame is a `<form>`; a node frame (a fragment of the
  resource) is a `<div>`, and so is every frame in view mode. Only the
  innermost frame acts on an event.
* A frame applies only the inputs of its own resource: those whose closest
  frame has the same `data-scrud-href`. A root submit so includes the inputs of
  its node frames, but never those of an embedded frame for another resource.
* A text-like input (`text`, `textarea`, `url`, `date`, `number`) that still
  shows the value embedded at its pointer is not applied, so an untouched field
  leaves the content exactly as it was (a text input has lost its line breaks;
  a textarea's line breaks are compared as LF).
* An input named `<pointer>/-` appends an element to an array. Inputs that share
  the prefix (`/items/-/text`, `/items/-/done`) form one new element; a group
  whose inputs are all empty is dropped.
* Array indexes are strict. An index equal to the array's length appends; an
  index greater than the length, or one that is not a canonical non-negative
  integer (`01`, `-1`, `length`), is an error, and the input is skipped.
* The CSRF token comes from the page's `csrf-token` meta tag.
* The Delete button (shown when the user may DELETE the resource) asks for
  confirmation and sends a `DELETE` with `If-Match`. On success (a 204) the
  resource's frame is removed from the page, with the list item that held only
  it, and a `scrud:deleted` event is dispatched on `document` with
  `detail.href`, the resource's URL. A failed delete never removes the frame.
  A 409 (in use) or 412 (stale) is answered with a notice and a frame that
  reloads the current state, as a stale PUT is; any other failed DELETE is not
  swapped in, and a client notice is shown in the frame instead.
* A request that fails with nothing to swap in (a PUT or DELETE answered 401,
  404, 415 or 5xx, or a network failure) shows a client notice at the top of
  the frame, with the markup of `scrud:notice` (`role="alert"`, an extra class
  `scrud-client-notice`): "Your change was not saved (HTTP 404)." or "... (network
  error).", and "The item was not deleted ..." for a DELETE. The text is set
  as text, never as HTML. A new notice replaces the previous one; the frame is
  not replaced, so the input keeps the user's change.
* An input that cannot be applied cancels the PUT: an invalid JSON Pointer
  name, an unsafe segment (`__proto__`, `constructor`, `prototype`), or a
  number input that is empty or not a number over a stored number. The frame
  shows "This form could not be saved: invalid field <name>." and nothing is
  sent, so no edit is lost silently. The stored number is never deleted by a
  cleared or half-typed number input; to clear a number the schema must allow
  `null`, which this client does not yet write.
* The root edit `<form>` has `novalidate`: the browser's constraint validation
  (for example an `<input type="url">` holding a stored URI that is not
  absolute) must not block a submit. The server's JSON Schema validation is the
  authority.
* An empty text, textarea, url, date or number input over a `null` or missing
  value is untouched: it stays `null` or missing instead of becoming `""`.
  An empty number input over a stored number is an invalid field (above).
* A checkbox in an append row that is unchecked appends nothing.

Custom components that want to be editable need only render inputs named by
their JSON Pointer inside the frame.

### Refused writes

A 400 or 403 PUT re-renders the submitted body (never the stored content) in
edit mode with the messages, provided the user may read the stored resource
(as JSON would let them) and may PUT it; any other user, including one who may
PUT but not GET, gets the notices only: no form, no ETag, no content. Its decisions, contexts and ETag come from
the stored resource, never from the request. Policy condition messages are
shown only to a user who may read the stored resource, and only in HTML; the
JSON error bodies are unchanged. A 412 (PUT or DELETE) shows a notice and reloads
the frame with the current state, and so does a 409 DELETE.

## Fragment protocol

| Header | Meaning |
|---|---|
| `HX-Request: true` | Set by htmx: answer with a fragment (the frame) instead of a page. |
| `Scrud-UI-Scope` | The root scope chain, outermost first: up to 16 space-separated scope names. An invalid value is ignored. |
| `Scrud-UI-Pointer` | The JSON Pointer of the node to render (default `""`). An invalid pointer (or one over 1024 characters) is a 400, before a PUT writes anything. |

These select presentation only, never what is authorized. HTML responses carry
`Vary: Accept, HX-Request, Scrud-UI-Scope, Scrud-UI-Pointer, Scrud-UI-Branch,
Cookie` (the branch a page browses comes from a header or a cookie) and
`Cache-Control: private, no-cache, no-store` (a page and its fragments share
one ETag, so a cache must not keep either; the ETag stays, and the client sends
it as `If-Match`). An error is the same status as JSON (401, 403, 404, ...) with
a `text/html` notice, and never includes stored resource content: a refused 400
or 403 PUT may echo the body the user submitted as the edit form, and a 412 adds
a frame that reloads the current state with the viewer's rights (see Refused
writes, above). A success whose HTML fails to render is answered with a 500 and
a notice.

## Branches

A branch is a named set of UI items (components, stylesheets and CSS items)
that changes the presentation of the site, not its data: a viewer on any branch
sees the same resources, with the same authorization. Use branches to work on
the presentation of a new resource type without changing what everyone sees,
to let testers browse a change before it is published, and to publish several
presentations of the same resources, each at its own URL prefix.

A site that creates no branch behaves as it did without them: the same URLs,
the same rendering, no banner and no extra queries.

### Concepts

* **`main`** is the published branch at the API's URLs (`/api/...`), in the
  RBAC context `""`, holding every item written before branches. The name is
  reserved: `main` has no parent and no prefix, cannot be deleted, and has no
  stored row until something is written on it.
* A **published** branch has a `prefix` and is served at it (see
  [Prefixes](#prefixes)). A **working** branch has none; it belongs to the
  tree of its nearest published ancestor and is browsed under that ancestor's
  prefix.
* A branch stores only its own changes, its **overrides**: a changed item, an
  added one, or a **deletion marker** (`deleted: true`) that hides the
  parent's item of that name. It shows its parent's state for everything it has
  not changed: its **effective state**. A branch with no parent stores all its
  items.
* A **live** branch (`base: "live"`, the default) shows its parent's current
  state: a fix published to the parent shows on the branch at once, for every
  item the branch has not changed. A **pinned** branch (`base: "pinned"`)
  shows its parent as it was at the branch's **branching point**. A viewer can
  also flip a live branch to its branching point for a single view (see
  [Browsing](#browsing)).
* The **branching point** (`branching_point`, server-managed) is the version of
  every ancestor of the branch when it was created or last pulled, for example
  `{"testing": 12, "admin": 40}`.
* Every change to a branch's own items, a push into it, a pull and a restore
  record the branch's next **version**: a numbered, immutable snapshot of its
  overrides and of the ancestor versions it was resolved against. Versions are
  read-only resources (`ui-branch-versions`).

A branch is a resource of type `ui-branches`:

| Property | Meaning |
|---|---|
| `name` | Required. Unique site-wide, `^[a-z0-9][a-z0-9._-]{0,63}$`. A branch cannot be renamed. |
| `rbac_context` | Required. The branch's context. It cannot change once the branch exists. |
| `parent` | The name of the branch it is based on, or `null`. Any chain is allowed. Setting or changing it needs GET on the parent: a parent you may not GET is refused (400) exactly as one that does not exist. |
| `base` | `live` (default) or `pinned`. `pinned` needs a parent. |
| `branching_point` | Server-managed, as above. |
| `prefix` | Present only on a published branch: its URL prefix, `/segment/.../` (lower-case letters, digits, `_` and `-`), unique. Setting or changing it (or removing it) needs PUT on `ui-branches` in the context `""`; see [Prefixes](#prefixes). |
| `conflicts` | Server-managed: the overrides a pull found changed in the parent (see [Pull and conflicts](#pull-and-conflicts)). |
| `pull` | Write-only: `true` on an update pulls the parent's changes. |
| `label`, `description` | Free text. |

A published branch's `prefix` can change to another prefix, but a branch cannot
be switched between published and working while it has its own stylesheets
that apply to a context (they would be authorized in another context). A
branch that another names as its `parent` cannot be deleted (409). Deleting a
branch deletes its own items and its versions in the same transaction, so a
branch created again under the name starts again at version 1.

### Authorizing contexts

There is no separate role for who may change the presentation: a branch is a
resource with an RBAC context, and the scoped RBAC grants in that context
decide who may see (GET), change (PUT), push from and pull it, like any other
resource.

* **Reading** a branch's items as JSON (the lists and details of
  `ui-components`, `ui-stylesheets` and `ui-css`) shows its effective state.
  An item that comes from an ancestor is read-checked as it is on the branch it
  comes from, and a version shows only the items its reader may read. The
  branch a JSON list shows is chosen with the `Scrud-UI-Branch` header or the
  `ui-branch` parameter (the header first; the cookie is not read for JSON).
  Without either it is `main`, or, under a prefix, the prefix's branch.
* **Writing** an item names its branch in the body, `"branch": "<name>"`
  (`main` when absent). A branch you may not GET is answered as one that does
  not exist (400, naming nothing of it), but for `main` and for a stylesheet
  on a published branch that applies to another context (below), which need
  no grant in the branch's context. The item's `rbac_context` must be its **authorizing
  context** on that branch, or the write is refused with a 400 that names it:
  * on a working branch, the context is the branch's, whatever the item
    applies to, so the grants needed are those on the item's type in the
    branch's context;
  * on a published branch, a component, a CSS item and a stylesheet that
    applies everywhere (`applies_to: ""`) are authorized in the branch's
    context, and a stylesheet that applies to a context `X` in `X`. Whoever
    may style `acme` styles it on every published branch, and needs no grant
    in the branch's context. On `main`, which is in context `""`, this is the
    rule before branches.

  A stylesheet's `applies_to` names the context it applies to (`""`:
  everywhere). When it is absent, a stylesheet on a published branch applies to
  its `rbac_context` if that is not the branch's context. An item cannot move
  to another branch.
* **`base`** on an item is server-managed: on create, the ETag of the parent's
  item it replaces (`null` for an addition); it stays while the override is
  edited, and only `rebase: true` (below) moves it.

### Pull and conflicts

A pull is a PUT of the branch with `"pull": true`; it needs PUT on the branch.
It moves the branching point to the ancestors' current versions, keeps the
overrides, and records a version. An override whose parent item changed since
the override's `base` is a **conflict**: the pull still happens, and the
conflicts are listed in the branch's `conflicts` (`{kind, key, base, current}`
each) until they are resolved:

* keep the override: PUT the item with `"rebase": true`, which moves its `base`
  to the parent's current item and clears the conflict. The flag is
  write-only, and valid only on an update of an item of a branch that has a
  parent;
* take the parent's: DELETE the override (its own row; deleting a deletion
  marker does not clear a conflict);
* an ordinary edit does not clear it.

### Pushing and restoring

A push is a resource of type `ui-pushes`:

| Property | Meaning |
|---|---|
| `source` | The branch whose overrides are pushed, or `null` for a restore. |
| `target` | The branch they are written into. |
| `restore` | The version of `target` to restore, or `null` for a push. Exactly one of `source` and `restore` is given. |
| `state` | `proposed` or `applied`; a workflow policy may add states between them. |
| `rbac_context` | Must be the target's context (400 otherwise): the grant to push into a branch is a grant in the target's context. |
| `result` | Server-managed: `{"version": N}`, the target's new version, once applied. |

**A write that moves a push into `applied` applies it, in the same
transaction.** Create it directly in `applied`, or create it `proposed` and PUT
it to `applied` later. The server never writes `applied` itself. Applying a
push:

1. needs the ordinary grants on `ui-pushes` in the target's context; GET and
   PUT on the source (applying empties its overrides); and, for each item, the
   grant a direct write of that item into the target would need: PUT where the
   target has its own row, POST for a new one, DELETE for a deletion marker,
   on the item's type in its authorizing context on the target. If any is
   missing the push is refused (403) and nothing is written. A source the
   pusher may not GET, or a target they may not GET named with the wrong
   `rbac_context`, is answered as a branch that does not exist (400);
2. is refused with 409 when any of the source's overrides was made against an
   item that the target has changed since (its `base` is not the target's
   current ETag). The body lists the `conflicts`, but only those on items the
   pusher may GET in the target; it counts the others in
   `unreadable_conflicts`. Nothing is written. Pull the source, resolve the
   conflicts, and push again;
3. otherwise writes the overrides into the target through the ordinary write
   path, records the target's next version, empties the source's overrides
   (the branch stays), clears its conflicts, and, when the target is its
   parent, moves its branching point as a pull does.

A push whose source has no parent (`main`, or any root) is refused (400): it
would delete the live site's items. A source cannot be the target or one of the
target's ancestors. An **applied push is a record**: it cannot be updated
(400) or deleted (409); to push again, create a new push, which a workflow
policy gates afresh. Pushing into a branch other than the source's parent is
allowed.

A **restore** is a push with `"restore": N` and no `source`: it makes the
target's own items those of its version `N` (404 if there is none), with the
same grants and the same atomic write, and records a new version. History is
never rewritten; a restore is how a bad publish is rolled back.

#### Requiring approval

A push is an ordinary resource, so a `WorkflowPolicy` gates it without any
change to the branching code. To require that a push is proposed, approved,
and only then applied, store a policy for the push type (a `workflow-policies`
resource):

```json
{
  "resource_type": "http://api.openteams.com/json-ld/UIPush",
  "rbac_context": "",
  "initial_states": {"proposed": "state == \"proposed\""},
  "states": {
    "approved": "state == \"approved\"",
    "applied": "state == \"applied\""
  },
  "transitions": {
    "approve": {"from": "proposed", "to": "approved"},
    "apply": {"from": "approved", "to": "applied"}
  }
}
```

A push can then be created only `proposed`, and moved to `approved`, then to
`applied`, which applies it as above; a move that skips a state is refused
(400). Grant the `approve` and `apply` transitions to the roles that may
approve and apply, with `scrud_workflow` conditions on their grants, as for any
workflow. Superusers bypass workflow, as everywhere else.

### Browsing

The prefix selects the published branch. Within its tree, the working branch
shown in a page comes from, in order:

1. the `Scrud-UI-Branch` request header (htmx requests inside a page always
   send it, so a page never changes branch halfway);
2. the `ui-branch` query parameter of a link;
3. the cookie of the prefix, set by confirming a switch;
4. none: the published branch itself.

A value is `<name>`, or `<name>@pinned` to view the branch at its branching
point instead of live (this changes that viewer's rendering only, not the
branch's `base`). A value that is not one of these is ignored. An empty header
or parameter names the published branch. A branch that does not exist, is not
in the prefix's tree, is pinned to a version that is missing, or that the
viewer may not GET (the branch, or any working branch among its ancestors: a
branch shows its ancestors' items) is **refused**: the page renders the published branch and
the banner says the chosen one was not available. It never fails the page.
Browsing changes HTML only: the cookie does not affect JSON.

**The banner** is shown on every HTML page off the published branch (and when a
branch was refused). It names the branch and its mode (`live`, or `pinned at
<parent> v<N>`); lists the working branches in the prefix's tree that the
viewer may select, as a switcher; offers a link to view live or at the branching
point; a link to share; and the way back to the published branch. The server
renders it outside stored components, from package templates, so a page
component that leaves it out has it added anyway. A full page also carries the
branch in a hidden element, so that htmx requests made from outside a frame
stay on it. The published branch has no banner.

**Confirmation.** Opening a link with `?ui-branch=<name>` that would switch you
to a working branch other than the one you are on first shows a page asking
"You're about to browse branch X", with Continue and Cancel: the components and
CSS of a branch's writers run in your browser with your rights. Only the links
the server renders for you (Continue, and the banner's) skip it. They carry
`ui-branch-confirm=<token>`, an HMAC of the selection and your CSRF secret,
which a link planted in content cannot have. So confirmation needs Django's
`CsrfViewMiddleware`, and a confirmed link works only for the viewer whose CSRF
secret signed it (any other is asked again). Confirming sets the cookie. The
confirmation page is only for full-page HTML GETs, never for htmx requests, and
a request from another site (`Sec-Fetch-Site`) is always asked.

**The cookie** is `scrud_ui_branch` at the API URLs, and
`scrud_ui_branch_<first 12 hex digits of sha256(prefix)>` under a prefix, with
the path set to the prefix. (A cookie for `/` is sent to every prefix; its own
name keeps it from being read, or shadowing another, there.) It is only a
preference, checked again on every request.

### Prefixes

Add the middleware after `AuthenticationMiddleware`:

```python
MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "scrud_django.ui.branches.middleware.PublishedBranchMiddleware",
    ...,
]
```

With it, a request for `<prefix><type slug>/...` (or for the CSS files), where
`prefix` is a published branch's, reaches **the same resource views, with the
same authorization, preconditions, workflow and write path** as
`/api/<type slug>/...`. Only the presentation differs. A prefix claims nothing
else: `/` does not swallow the rest of the site. The longest matching prefix
wins. Under a prefix:

* a viewer needs GET on the prefix's branch, in HTML and JSON alike, or gets
  the refusal a refused GET gets (401 or 403), with no fallback presentation;
* HTML links and htmx requests stay under the prefix; JSON keeps the canonical
  `/api/` hrefs; JSON item lists default to the prefix's branch;
* there is no trailing-slash redirect: `<prefix><slug>` without the slash is
  not a mount URL (a 404 unless your URLconf serves it);
* a project that serves its API at `/` gets no prefixes: the API's URLs are
  never mounted.

**Cost:** the middleware reads the stored branches, **one extra query**, on
every request outside the API's URLs, to find out whether the path is under a
prefix. Requests to the API's URLs read none. Without the middleware, or
without `scrud_django.ui` installed, there is no cost. A database error reading
the branches is logged and the request falls through, so that other pages still
answer.

**Who may publish.** A prefix claims URLs of the whole site: the viewers of
those URLs then need GET on the branch, so a prefix can put a page of the
project behind a branch's grants. Setting a branch's `prefix`, changing it, or
removing it therefore needs **PUT on `ui-branches` in the context `""`** (the
site's), besides the grants in the branch's own context; without it the write
is refused (403). A grant in a branch's own context lets you write, push to
and pull it, never publish it.

A prefix is refused (400) when it is set or changed if it lies under the API's
URLs; overlaps another branch's (the URLs one claims, `<prefix><type
slug>/...`, include the other's: `/shop/` and `/shop/todo-lists/` do; `/` and
`/admin/resources/` do not); or claims URLs your URLconf serves with another
view, which it would hide. A project that serves its API at `/` can set no
prefix. Django's admin has a catch-all route, so a prefix under `/admin/` is
refused; the demo uses `/manage/`.

A system check reports the same problems in stored prefixes (stored before
this check, by other means, or made wrong by a later change of the URLconf) as
a **Warning**, `scrud_django_ui.W001` (not an error: it reports stored data,
and an error would stop `migrate`; routing stays deterministic, the longest
prefix wins).

### CSS

A branch's CSS items are effective like the other items, and `scrud:page` links
them (see [CSS items](#css-items)). Under a prefix the files are served at the
prefix's URL. Every file's URL includes its ETag, and the file is cached for a
year as private.

## Security

* **Viewing a branch means trusting everyone who can write to it.** There is no
  separate trusted role: writes are authorized by scoped RBAC in the context of
  the branch (see [Authorizing contexts](#authorizing-contexts)), so trust is
  per RBAC context. Whoever may write components or CSS on a branch controls
  the markup of every page that shows it. A component can carry client
  scripts, and they run in the page with the viewer's rights. A user with write
  grants in a personal branch's context could send a link to a colleague with
  broader rights. Untrusted authors are phase 3.
* The mitigations: a viewer sees only the branches they may GET; the banner
  always names a working branch, and the server renders it outside stored
  components; a link that would switch the viewer to a working branch asks first
  (confirmation, with a token only the server can issue for that viewer, which
  needs `CsrfViewMiddleware`); and the CSP below blocks inline scripts. The
  banner is not a guarantee: a branch's CSS, or a page component that wraps
  its body in a hidden element, can hide it (see the follow-up
  [ui-banner-hideable](follow-ups.md#ui-banner-hideable)). Trust the writers
  of the branches you let people browse.
* A branch never changes what a viewer may read or write through the API's
  views: under a prefix the same views answer with the same checks. A prefix
  does add one: its URLs need GET on its branch. So publishing a prefix can
  put URLs behind a branch's grants, and is site-level: it needs PUT on
  `ui-branches` in the context `""` (see [Prefixes](#prefixes)), and a prefix
  that would hide the project's own URLs, overlap another or lie under the
  API's is refused. Grant it to the site's administrators only. A mount that
  hides a field is presentation, not access control.
* Stored Jinja templates run in Jinja's sandbox; stored Django templates run on
  a restricted engine (no loaders, no tag libraries), which is not a sandbox.
  Both escape everything they are given.
* Components are presentation, not access control. A component is only given
  what the viewer may read, so it cannot reveal more; but hiding a field in a
  component does not keep it from the viewer, who can read it as JSON.
* The page loads only its own scripts. Recommended Content-Security-Policy:
  `script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self';
  frame-ancestors 'self'`. `scrud-ui.js` sets htmx's `selfRequestsOnly`.
  `script-src 'self'` blocks inline scripts, those in components included:
  ship a component's scripts as static files, or have the site allow them with
  CSP nonces or hashes.
* The vendored htmx is **2.0.4** (`scrud_ui/htmx.min.js`), sha384
  `HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+`.

## The to-do demo

`demo/` includes a seed command that creates users, a to-do list, and the
components and stylesheets that render it (a detail page with editable rows, a
compact sidebar list, and a page with a layout), and three branches: the
published `admin` branch at `/manage/` (with an extra component and its own CSS),
the published `customer` branch at `/todo/` (with its own CSS), and a working
branch `testing-admin` over `admin`. The demo adds
`PublishedBranchMiddleware` to its settings. The admin prefix is `/manage/`, not
`/admin/resources/`, because Django's admin has a catch-all route under
`/admin/`, which a prefix there would hide.

```bash
cd demo
uv run python manage.py migrate
uv run python manage.py seed_todo_demo
uv run python manage.py runserver
```

Sign in at `/api-auth/login/` and open `/api/todo-lists/` with a browser (or use HTTP Basic auth). The
users are `owner`/`owner` (reads and edits the list), `reader`/`reader` (reads
it) and `author`/`author` (writes the UI components and stylesheets); these
passwords are for the demo only. `author` writes the UI in the three branches'
contexts (`ui-admin`, `ui-customer`, `ui-testing`) and may open both prefixes;
`reader` may open `/todo/` (it may GET the `customer` branch) and gets a 403 at
`/manage/`; `owner` may open `/todo/` too. To try the flow: as `author`, write
a component on `testing-admin` (a POST of a `ui-components` item with
`"branch": "testing-admin"` and `"rbac_context": "ui-testing"`), browse
`/manage/todo-lists/<id>/?ui-branch=testing-admin` (confirm the switch), then
POST a `ui-pushes` item with `source` `testing-admin`, `target` `admin`,
`rbac_context` `ui-admin` and `state` `applied`.

Known limitations of phase 1 are recorded in the [follow-ups](follow-ups.md),
under the `ui-` names.
