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
| UI JS | `http://api.openteams.com/json-ld/UIJs` | `ui-js` |
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

A stored Jinja component also gets three functions: `embed` (see
[Embeds](#embeds)), `in_mount(href)` and `api_url(slug, key=None)`:

- `in_mount(href)` is `href`, an API URL, as the page's published prefix
  serves it: on a page under a branch's prefix, `in_mount("/api/todo-lists/7/")`
  is that prefix's `todo-lists/7/`; at the API's URLs it is `href` unchanged.
  It maps links as the server maps a frame's own (`selection.in_mount`), so an
  API path found in the data (an item's `href` in the studio's computed types)
  links within the page's prefix. A value that is not a string is returned
  unchanged.
- `api_url(slug, key=None)` is the API path of the registered type `slug`'s
  list (`api_url("todo-lists")` is `/api/todo-lists/`), or of its detail for
  `key` (`api_url("todo-lists", 7)`), computed types included
  (`api_url("ui-branch-items", "main")`; a computed type has no list). It is
  always the API's path, which `embed` maps into the page's prefix, so
  `embed(api_url("ui-branch-items", value.name))` works under a prefix too.
  A `slug` that is not a registered type's (no `<slug>-list` or
  `<slug>-detail` route serving that type) raises, so the component fails and
  falls back, as any component that fails does.

Django components have neither, nor `embed`.

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

### Embeds

A stored Jinja component (a node's, a list's or the page's) can render another
resource in its place on the server: `embed(path, scope=(), mode=None)`
returns that resource's HTML, the fragment its own URL answers.

```jinja
<aside>{{ embed(value.sidebar, "sidebar") }}</aside>
<section>{{ embed("/api/todo-lists/?page=2", ["card"], mode="view") }}</section>
```

- `path` is an absolute URL path on this site, with an optional query (a list's
  `?page=2`), such as a link's `href` from the node's data. An `http` or
  `https` URL whose host is the request's own (`request.get_host()`) is taken
  as its path, so `embed(resource.href)` works in a list item's component,
  whose `href` is absolute. The path is split once (`urllib.parse.urlsplit`):
  its fragment is dropped (`/x#a?b` has no query), surrounding whitespace is
  stripped, and the path and query it checks are the ones it requests. The
  path is decoded as the server decodes a request's (`%2F` is `/`) and is not
  normalized (a `..` segment matches no route).
- A path under a published branch's prefix is served as that prefix serves it:
  at the API's URL, with the prefix's checks (`check_mount`), and the embedded
  frame's links stay in the prefix. On a page under a prefix, an API path
  (`/api/...`) is embedded under that prefix, as the page's links are
  (`selection.in_mount`), so it renders on the prefix's branch with the
  prefix's checks; an API path the prefix doesn't serve (the API root) is
  refused.
- `scope` is the scope chain to render it in: a string of space- or
  comma-separated names, or a list of names; `none` is no scope, as the
  default `()` is. One that is not a scope chain (not a string or a list of
  strings, more than 16 names, or a name that is not a scope name) is refused
  (below), and so is an undefined variable, so a misspelt one is never read
  as no scope.
  With no scope, the path's own `?ui-scope=` applies, as at its URL
  (`embed("/api/todo-lists/7/?ui-scope=sidebar")`); a `scope` given wins, as
  the header does.
- `mode="view"` renders it read-only (as `Scrud-UI-Mode: view`), with no
  Delete button; without a mode, the mode is the user's own (in a studio
  preview requested in view mode, view mode: see below). Any other value is
  refused (below), as any other `Scrud-UI-Mode` is a 400 at the resource's
  URL: a typo never puts edit controls in a card meant to be read-only.

The embed is a GET made in process as the same user
(`scrud_django.ui.dispatch.dispatch_path`), so the resource's view, its RBAC,
workflow, masking and `check_mount` apply unchanged. It sends `HX-Request:
true`, `Scrud-UI-Scope` when a scope is given, `Scrud-UI-Mode: view` when asked, and
`Scrud-UI-Branch` with the embedding render's branch (as `name` or
`name@pinned`), so the embed renders on the same branch, and the embedded
frame's own requests stay there. A response of 400 or more is embedded as the
notice the resource's URL answers with (a 403 or a 404 for a resource the
viewer can't read): never its content. Safe mode and the studio's preview carry
over to what is embedded (as context variables, not headers): an embed in a
preview has no write attributes. In a preview requested in view mode (the
studio's `preview` and `live-preview` with `mode` `view`, their default), every
embed is in view mode too, as with `mode="view"` (it sends `Scrud-UI-Mode:
view`): the embedded resource shows no edit inputs and no array operations,
whatever the user's own mode on it; in an edit-mode preview an embed with no
`mode` has the user's own mode. Safe mode and drafts are render-only: what the
embedded view reads as data (a list of UI items, a studio computed type) is the
stored state, as its URL answers it, so an embedded list of UI items in safe mode lists the
stored items, and one in a live preview lists the stored item a draft replaces.
This holds by construction: `dispatch_path` runs the embedded view in
`stored_state()` and renders its response after, outside it, so that the
embedded presentation still sees safe mode and the drafts.
Django components have no `embed`. Build an embed's path with `api_url`
(`embed(api_url("todo-lists", value.list_id))`) rather than by hand, and link
an API path from the data with `in_mount` (see
[The node context](#the-node-context)).

These are refused, and render
`<div class="scrud-notice scrud-notice-error" role="alert">This embed was refused.</div>`
in the embed's place (each refusal is logged as a warning by
`scrud_django.ui.embed`):

- a `path` that isn't a string or isn't absolute, or that has a scheme or a
  host other than an `http` or `https` URL of the request's own host
  (`https://other.example/...`, `//other.example/...`, `ftp://...`,
  `http:/api/...`, or a backslash such as `/\other.example/...`, which is
  a path that resolves to nothing);
- a path that doesn't resolve to a resource view: a stored or computed resource
  type's list or detail route. A project's page, the studio, the API root and
  the CSS and JavaScript file routes are refused, and so is a response that
  isn't the UI's HTML (`?format=json`, `?format=api`);
- a `scope` that is not a scope chain, and a `mode` other than `"view"`;
- a path and scope chain already being rendered, which would be a cycle: the
  top-level request (its URL and its scope chain: `Scrud-UI-Scope`, else
  `?ui-scope=`) and each embed it is inside.
  A resource embedding itself in the scope it is rendered in, or two components
  embedding each other, stop there. The same resource in another scope is not a
  cycle;
- an embed deeper than 3 (`MAX_EMBED_DEPTH`: an embed in an embed in an embed is
  the deepest);
- every embed after the first 20 (`MAX_EMBEDS`) of a top-level response, nested
  ones included.

A view that raises renders "This embed failed." the same way, logged at ERROR
with its traceback, and `got_request_exception` is sent (for error reporters),
as for a view that raises at its URL. The embedded view runs in a savepoint on
each database already in an atomic block (`ATOMIC_REQUESTS`, a studio
preview), so a database error in it (which aborts the transaction on
PostgreSQL) is rolled back alone and the rest of the page still renders. Each embed reads the UI items of its branch again (it may be refused
the branch the embedding render shows, and then renders on its prefix's
published branch): up to 20 renders per response.

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

## JavaScript items

A JavaScript item (type `ui-js`) holds the text of a script a page runs: `name`,
`js` (at most 262144 characters, and never containing `</script`, in any case,
which is refused with a 400 `ui-js-invalid`), `order` (default 0), and the
branch fields every UI item has. A name is unique on its branch (400
`ui-js-name-taken` otherwise). The built-in page links the JS items effective
on the branch it shows, by `order` and then by name, after `scrud-ui.js`, as
`<script src="..." defer></script>`, at `<api or prefix>ui-js-files/<name>/<etag>.js`
(`Content-Type: text/javascript; charset=utf-8`). Caching and gating are those
of [CSS items](#css-items): the ETag in the URL makes a file immutable
(`private, max-age=31536000, immutable`), a stale URL is a 404, and a viewer who
may not GET the branch is refused. The page component's context has `scripts`,
the list of these URLs (empty when there are none, and then the page is
unchanged). Writing a JS item needs the grant on `ui-js`, in the branch's
context, not on `ui-components`. JS items are versioned, pushed, pulled and
restored like the other kinds.

**JavaScript items are code execution for the branch's viewers.** Whoever can
write `ui-js` on a branch runs script with the rights of everyone who views
pages on that branch, and on a published branch that is the public. Grant
`ui-js` write narrowly. The files are served from same-origin versioned URLs,
so the recommended `script-src 'self'` stays valid, and gives no protection
against them.

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

A list's wrapper is resolved the same way, with two terms, most specific first:
the collection IRI of the list's type
(`https://api.openteams.com/json-schema/ResourceCollection?contents_type=<type uri>`,
which is an absolute IRI and is used as written, or through a prefix) and
`scrud:collection`. The rules are those of the stylesheets in context `""`
only (a list has no contexts of its own), in view mode. Scopes work as for any
rule, so `sidebar scrud:collection` restyles lists under `?ui-scope=sidebar`
only. The component gets the `scrud:collection` context plus `href` (the
list's URL) and `scope` (the scope chain as a list), and `embed` when it is a
Jinja component. With no matching rule, or if the stored component fails to
render, the built-in `scrud:collection` is used (after the usual
`<!-- scrud-ui: component "x" failed -->` comment on a failure).

### The cascade

Rules are collected from the stylesheets that apply to the resource, each rule
with a rank (higher wins):

1. the default stylesheet `scrud:default` (`(0, 0, rule index)`);
2. the stylesheets of the [packaged UI layers](#packaged-ui-layers), in
   `INSTALLED_APPS` order, then by `order` (`(0, 1, layer index, order, rule
   index)`);
3. stylesheets in context `""`, by `order`, then by creation (`(1, order, pk,
   rule index)`);
4. stylesheets in each of the resource's RBAC contexts, in the order of the
   resource's context list, later contexts winning (`(2, context index, order,
   pk, rule index)`);
5. stylesheets named by the `stylesheets` of the components above the node
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
from the next response. A stored component, stylesheet or CSS item whose
`rbac_context` is not the context its write is authorized in on its branch
(see [Authorizing contexts](#authorizing-contexts)), which only a write around
the API can store, is not used, and is logged as a warning: on a branch, the
parent's item shows instead.

## Packaged UI layers

An installed app can ship a **UI layer**: components, stylesheets, CSS items
and JS items as files in a package, read at runtime as the `scrud:` built-ins
and the default stylesheet are. Unlike those, a layer item can be overridden by
name on any branch. An app declares its layer with an attribute of its
`AppConfig`:

```python
class StudioConfig(AppConfig):
    name = "myapp.studio"
    scrud_ui_layer = {"prefix": "studio:", "package": "myapp.studio.ui_layer"}
```

The package holds `component/*.json`, `stylesheet/*.json`, `css/*.json` and
`js/*.json`. Each file is one item's content without the branch fields
(`branch`, `rbac_context`, `deleted`, `base`): for example
`component/card.json` is `{"name": "studio:card", "engine": "django", "mode":
"view", "template": "..."}`. Every item's name starts with the layer's prefix
(lowercase letters, digits and `-`, starting with a letter, ending with `:`;
never `scrud:`). A layer stylesheet applies everywhere: its `applies_to`, if
any, is `""`. Ship the files as package data. Layers are read once per process,
and again after a setting changes.

- **Where it sits.** Layer items are on the virtual branch `@layers`, at the
  bottom of every chain, beneath every root branch (`main` and any parentless
  published branch) and above the built-ins. A key (a name, and for a
  stylesheet its `applies_to`) that no branch of the chain has shows the layer
  item. Several layers stack in `INSTALLED_APPS` order; a later layer's
  stylesheet outranks an earlier one's.
- **Overriding.** A branch overrides a layer item as it does any inherited
  item: write an item of the same kind with the same name (and, for a
  stylesheet, `applies_to` `""`). The override's `base` is the layer item's
  ETag, the sha256 hex of the file's content as canonical JSON (sorted keys,
  no spaces), on a root branch (`main` too) and on a child branch where no
  ancestor overrides the item. Deleting the override shows the packaged item
  again. A deletion marker hides the layer item on its branch (and the
  branches beneath it) only.
- **Pulls and pushes.** The layer is beneath the parent there too: an
  override of a layer item that the parent does not override is compared
  with the layer item, so it is no conflict while the layer is unchanged,
  and pushes cleanly. A push of such an override into a root branch stores it
  with the layer item's ETag as its `base`; a pushed deletion marker that
  replaces the root's own override of a layer item hides the layer item, based
  on its ETag.
- **Ranking.** A layer stylesheet's rules rank after the default stylesheet's
  and before every stored stylesheet's (see [The cascade](#the-cascade)). An
  override of a layer stylesheet is a stored row and ranks as one: a stored
  stylesheet in context `""`. Between two stylesheets of one layer with the
  same `order`, a tie goes to the file whose name sorts first.
- **Names.** An item name may carry a prefix (`abc:name`). A write whose name
  has a prefix that is not an installed layer's, or that names no item of that
  layer of the same kind (and, for a stylesheet, `applies_to`), is refused with
  a 400 `ui-layer-name-reserved`. So a new item cannot use a layer's prefix,
  and `scrud:` stays reserved outright.
- **Not in resource lists.** Layer items are not rows: they never appear in
  `/ui-components/` and the other item lists or details, so lists, pagination
  and RBAC filtering are unchanged. Presentation reads them with system rights,
  as it reads stored items.
- **Versions.** Branch versions do not record layer items. A pinned branch,
  and a branch viewed `@pinned`, sees the installed package's layer.
- **Upgrades and layer conflicts.** A library upgrade can change a layer
  item that a branch overrides. Root branches have no parent to pull from, so
  conflicts with the layer are computed live, not recorded by a pull: an own
  item of a branch is in conflict with the layer when the nearest item beneath
  it with its key is the layer's and its `base` is not that item's current
  ETag (`scrud_django.ui.layers.layer_conflicts(branch)`, which lists
  `{"kind", "key", "base", "current"}` like a branch's `conflicts`). Resolve
  one by keeping the override, re-saved with `rebase: true` (now valid on a
  root branch for an item that overrides an installed layer item; the server
  moves its `base` to the layer item's current ETag), or by deleting it, which
  shows the upgraded layer item. On a child branch a pull also lists the
  conflict against the parent, as for any parent item, and `rebase: true`
  resolves both. The `conflicts` field and pull keep their meaning.
- **Orphaned overrides.** When an upgrade removes a layer item (or its layer
  is uninstalled), an override of it is orphaned: it keeps rendering as an
  ordinary item, is listed by `layer_conflicts` with `current` `null`, and
  stays editable under its name (an update that keeps the name and
  `applies_to` is accepted). A new item, or a rename, with the name is still
  refused. Delete it, or rename it to a name without a prefix, to resolve it.
- **Checks.** A layer declaration that is not valid, or a layer file that is
  unreadable or not a JSON object, whose name lacks the layer's prefix or is
  not a valid name, that has a branch field or another file's name, a
  stylesheet with a non-empty `applies_to`, or a CSS or JS item whose text has
  `</style` or `</script`, is reported by the system check
  `scrud_django_ui.E002` (`manage.py check`) and is left out at runtime. So is
  a file its item's viewset would refuse: one its kind's JSON Schema refuses,
  one without a field its kind needs (a component's `engine`, `mode` and
  `template`, a stylesheet's `rules`, a CSS item's `css`, a JS item's `js`), a
  component whose template does not compile, and a stylesheet with a rule
  whose selector does not parse; so the runtime's fallbacks (a skipped rule, a
  component that fails to compile) never apply to a packaged item. A layer
  folder that cannot be listed is reported too. Each of these errors is also
  logged at ERROR (logger `scrud_django.ui.layers`) when the layers are read,
  once per process, so a site that runs no checks still says why an item is
  missing. The database check `scrud_django_ui.W002` (`manage.py check
  --database default`, and `migrate`) lists, per branch, its layer conflicts
  and its orphaned overrides, as `scrud_django_ui.W001` lists prefixes; a
  branch it cannot check (its chain is broken, or its pinned snapshot is
  missing: it renders its published branch) gets a Warning that says so. With
  no layer installed it first reads the stored items' names (one query of its
  own per database) and stops there unless one has a layer prefix (`abc:`):
  only such an item can be an orphan, so a site without layers pays one query,
  not several per branch.

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
* The Delete button (shown when the user may DELETE the resource, in edit
  mode or in the view mode of a user who may not PUT it, but never under a
  requested view mode: `?ui-mode=view`, `Scrud-UI-Mode: view`, or an embed's
  `mode="view"`) asks for confirmation and sends a `DELETE` with `If-Match`. On success (a 204) the
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
* In edit mode the built-in `array-edit` gives every item of every array a
  **Remove** button, and **Move up** and **Move down** buttons (none for the
  first item's Move up or the last item's Move down). They are
  `<button type="button" data-scrud-array-op="remove|move"
  data-scrud-pointer="/rules/1" data-scrud-to="0">`; `data-scrud-to` is the
  index a move puts the item at. View mode renders no controls. A click triggers
  the frame's own `PUT` with the operation in the triggered event's detail
  (`htmx.trigger(frame, "change", {scrudArrayOp: ...})`), so the operation
  belongs to that one request: a save already in flight cannot drop it, and a
  request htmx drops leaves nothing for the next save. The PUT builds the
  frame's body as a change does (the inputs applied at their current
  pointers), applies the operation (`ScrudUI.applyArrayOp(body, op, errors)`)
  and is sent with `If-Match`: a stale page gets the 412 reload, and the list
  is never silently overwritten. An operation that cannot be applied (a
  pointer that is no element of an array, an index out of range, a bad
  `data-scrud-to`, an unknown operation) cancels the PUT and shows "This
  action is misconfigured." in the frame; a button outside a frame that PUTs
  logs an error to the console. Only the frame the button is in applies the
  operation, to its own content. The page shows the saved array after the
  response. A custom array component gets the same behaviour by rendering such
  buttons inside the frame.

Custom components that want to be editable need only render inputs named by
their JSON Pointer inside the frame.

### Writes from the browser

Besides the frame's own `PUT`, any element can send a JSON body with any
method (POST, PUT, PATCH or DELETE) to an existing resource route. It needs
`hx-ext="scrud-json"` (inherited from an ancestor works), an `hx-<method>`
attribute with the URL, and `data-scrud-json`. The element may sit outside any
`.scrud-resource` frame, in a stored page component for example.

* `data-scrud-json="form"` builds the body from the inputs inside the element
  that are named by JSON Pointer, onto `{}`, with the rules of the frame's PUT:
  `/items/-/text` appends to an array, a checkbox is a boolean, a number input
  a number, and `data-scrud-type="number"` (or `checkbox`, `text`, ...) on an
  input overrides the type taken from its `type`. Disabled inputs and unchecked
  radios are left out. This is how a create is sent:

  ```html
  <form hx-ext="scrud-json" hx-post="/todo-lists/" data-scrud-json="form">
    <input name="/title"> <input name="/items/-/text">
    <button>Create</button>
  </form>
  ```

* `data-scrud-json="fixed"` sends the JSON in `data-scrud-body` unchanged (the
  attribute's text is the body, byte for byte):

  ```html
  <button hx-ext="scrud-json" hx-post="/ui/push/" data-scrud-json="fixed"
          data-scrud-body='{"source": "wip", "target": ""}'>Push</button>
  ```

* A DELETE with `If-Match` is a fixed element with a `data-scrud-etag`
  (its own, else its frame's when it deletes the frame's own resource):

  ```html
  <button hx-ext="scrud-json" hx-delete="/ui-branches/5/" data-scrud-json="fixed"
          data-scrud-body="{}" data-scrud-etag="&quot;abc&quot;">Revert</button>
  ```

These requests send `Content-Type: application/json`, `X-CSRFToken`,
`Scrud-UI-Branch` (the frame's, else the page's `.scrud-branch-page`) and
`If-Match` from the element's own `data-scrud-etag`, else from its frame's
`data-scrud-etag` only when the request goes to the frame's own
`data-scrud-href`. A request to another URL from inside a frame (a create form
posting to a list, the DELETE of another resource) sends no frame tag, which
the server would refuse with 412; an empty `data-scrud-etag=""` on the element
sends no `If-Match` at all. The same rule applies to any other write from
inside a frame. The frame's `PUT` is otherwise unchanged. An unparsable or missing `data-scrud-body`, a
`data-scrud-json` other than `form` or `fixed`, or an input that cannot be
applied cancels the request: the element's frame shows "This action is
misconfigured." (or the invalid-field notice), and outside a frame the message
is logged with `console.error`. Forms are never sent as form bodies: the server
answers those with 415.

### Refused writes

A 400 or 403 PUT re-renders the submitted body (never the stored content) in
edit mode with the messages, provided the user may read the stored resource
(as JSON would let them) and may PUT it; any other user, including one who may
PUT but not GET, gets the notices only: no form, no ETag, no content. Its decisions, contexts and ETag come from
the stored resource, never from the request. Policy condition messages are
shown only to a user who may read the stored resource, and only in HTML; the
JSON error bodies are unchanged. A stylesheet whose rule has a selector that does not parse is a 400 problem
with `name: "rules[<i>].select"` (the field, as JSON Schema's errors name
theirs), so the re-render shows the message beside rule `<i>`. So is a
`ui-branches` write refused for its `prefix` (a malformed one, one another
branch has, or one that overlaps another branch's or the API's URLs: 400
`ui-branch-invalid` with `name: "prefix"`).
A 412 (PUT or DELETE) shows a notice and reloads
the frame with the current state, and so does a 409 DELETE.

## Computed resource types

A computed type (`scrud_django.computed.ComputedResourceViewSet`, registered
with `registration.computed_resource_type`; see the README) renders through
the same cascade as a stored resource: a stylesheet rule on its type's IRI
selects a component, and negotiating HTML at its URL, under a published
branch's prefix too, renders it. Its content is view-only: components get the
decisions `{"GET": "allowed", "PUT": "denied", "DELETE": "denied"}`, whatever
the user's grants, so edit controls never show. Its contexts, whose
stylesheets apply, are those of the content's `rbac_context` when it has one,
else none (only stylesheets in the default context). It has no ETag-based
writes: PUT, POST and DELETE are a 405.

## Fragment protocol

| Header | Meaning |
|---|---|
| `HX-Request: true` | Set by htmx: answer with a fragment (the frame) instead of a page. |
| `Scrud-UI-Scope` | The root scope chain, outermost first: up to 16 space- or comma-separated scope names. An invalid value is ignored. The same chain may come in the URL as `?ui-scope=sidebar,main`; the header wins. |
| `Scrud-UI-Mode` | `view` renders the resource (or each item of a list) read-only even for a user who may PUT it: no form, no `hx-put`, and no Delete button, even for a user who may DELETE it. It only lowers what the user may do: `view` is the only valid value, any other (`edit`) is a 400, before a PUT writes anything. The same may come as `?ui-mode=view`; the header wins. |
| `Scrud-UI-Pointer` | The JSON Pointer of the node to render (default `""`). An invalid pointer (or one over 1024 characters) is a 400, before a PUT writes anything. |
| `Scrud-UI-Safe` | `1` renders in [safe mode](#safe-mode), as `?ui-safe=1` does; any other value is ignored. scrud-ui.js sends it on the requests of a safe page. |

These select presentation only, never what is authorized. HTML responses carry
`Vary: Accept, HX-Request, Scrud-UI-Scope, Scrud-UI-Pointer, Scrud-UI-Mode,
Scrud-UI-Branch, Scrud-UI-Safe, Cookie` (the branch a page browses comes from a header or a cookie) and
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
  overrides (with each one's ETag then, in `etags`) and of the ancestor
  versions it was resolved against. Versions are
  read-only resources (`ui-branch-versions`).

A branch is a resource of type `ui-branches`:

| Property | Meaning |
|---|---|
| `name` | Required. Unique site-wide, `^[a-z0-9][a-z0-9._-]{0,63}$`. A branch cannot be renamed. |
| `rbac_context` | Required. The branch's context. It cannot change once the branch exists. |
| `parent` | The name of the branch it is based on, or `null`. Any chain is allowed. Setting or changing it needs GET on the parent: a parent you may not GET is refused (400) exactly as one that does not exist. `main` is no exception: it is in the context `""`, so an author branching from it needs GET on `ui-branches` there. |
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
  Without either it is `main`, or, under a prefix, the prefix's branch. A
  JSON list whose header or parameter names a branch that is refused (one
  that does not exist, that you may not GET with its working ancestors, that
  is not in the tree of the URL's published branch, or that is pinned to a
  missing version) is refused with 400, problem type `ui-branch-unavailable`,
  with the same text whatever the reason; it never lists another branch's
  items instead. (An HTML page renders the published branch and says so in
  its banner.) A value that is not a branch name is ignored.
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
  item it replaces (`null` for an addition; a packaged layer's item counts,
  on a root branch too: see [Packaged UI layers](#packaged-ui-layers)), as the
  branch shows it: on a
  pinned branch, the item as of the branching point, with the ETag its version
  recorded (a version recorded before versions kept ETags gives its items one
  of its own, which conflicts with the parent's until the branch is pulled and
  the override rebased). It stays while the override is edited, and only
  `rebase: true` (below) moves it.

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
  parent, or of a root branch's item that overrides an installed layer item
  (it resolves the layer conflict: see
  [Packaged UI layers](#packaged-ui-layers));
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
   item that the target has changed since (its `base` is not the ETag of the
   target's item as the target shows it: on a pinned target, as of its
   branching point). The body lists the `conflicts`, but only those on items the
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
component that leaves it out is replaced by the built-in page, after the
comment `<!-- scrud-ui: page component "x" leaves out the branch banner or
the safe mode notice -->` (a page component that fails to render gets the
usual `failed` comment). A full page also carries the
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
project behind a branch's grants. Setting a branch's `prefix`, changing it,
removing it, or deleting a published branch therefore needs **PUT on
`ui-branches` in the context `""`** (the site's), besides the grants in the
branch's own context; without it the request is refused (403). A grant in a
branch's own context lets you write, push to, pull and delete a working
branch, never publish or unpublish one.

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

### Safe mode

When a branch's stored UI breaks rendering (the studio's own pages included),
add `?ui-safe=1` to the page's URL. The page is then rendered from the packaged
layers, the built-ins and the default stylesheet alone: stored components,
stylesheets, CSS and JavaScript items are ignored on every branch, including the
selected one. Any other value of the parameter is ignored.

Safe mode removes presentation only. Every gate stays: a prefix whose branch
the viewer may not GET is still refused, a branch the viewer may not select is
still refused (a JSON request for it still gets its 400), and authorization is
unchanged. The page shows a notice,
`<div class="scrud-safe-mode" role="status">Safe mode: this page ignores stored UI items.</div>`,
that a page component can't leave out (as with the branch banner, a page
component that does is replaced by the built-in). The page's CSS and JavaScript
URLs carry `?ui-safe=1` and serve the packaged layers' items only. Links inside
the page do not carry the parameter: add it again to follow them.

The page's own requests stay safe: a safe page marks its frames and its hidden
`.scrud-branch-page` element with `data-scrud-safe="1"`
(`<div class="scrud-branch-page" data-scrud-safe="1" hidden></div>`, with
`data-scrud-branch` too when the page has a branch), and scrud-ui.js sends
`Scrud-UI-Safe: 1` on every request from such a page (a frame's `PUT`, the
reload after a 412, a refresh), which the server honours as `?ui-safe=1`, so
the URLs stay clean. Like the parameter, the header only changes HTML and the
CSS and JavaScript file responses (which then serve the packaged layers'
items only): a JSON answer is the same with or without it.

In code, `scrud_django.ui.branches.effective.using_safe_mode()` turns it on for
a block (a context variable, so in-process work during the render sees it too),
and `safe_mode()` reads it. `stored_state()` (same module) turns safe mode and
any live-preview drafts off for a block: an embed runs its view in it
(`dispatch_path`), and views also open it around their data reads (a list of
UI items, the studio's computed types), so that what an embedded request reads
is what its URL answers, while its rendering stays safe.

### CSS

A branch's CSS items are effective like the other items, and `scrud:page` links
them (see [CSS items](#css-items); JavaScript items are linked the same way,
see [JavaScript items](#javascript-items)). Under a prefix the files are served at the
prefix's URL. Every file's URL includes its ETag, and the file is cached for a
year as private.

## Studio

The studio (`scrud_django.ui.studio`) is a page for authoring a type's
presentation on a working branch. Pick a type, pick a branch, and see the type
rendered with a sample (or a real instance). Click a node in the preview and
the inspector says which rule chose its component, which rules matched and
lost, and why. Write a new component for the node and its rule in one action,
edit an item on the branch, and pull or push the branch. The studio is a
client of the resource API: every write goes through the resource viewsets
in-process, as the signed-in user, so scoped RBAC, the `base` checks and
versions apply as they do for a direct API call. It adds no rights.

This is the *bootstrap studio*: its pages are package templates, not UI
items, so nothing stored changes it. The [self-hosted studio](#self-hosted-studio)
is built from UI items on the same API, and the bootstrap studio stays as its
way out.

### Setup

Add the app after `scrud_django.ui` (a system check fails without it) and
include its URLs at a path you choose:

```python
INSTALLED_APPS = [
    ...
    "scrud_django.ui",
    "scrud_django.ui.studio",
]

urlpatterns = [
    ...
    path("studio/", include("scrud_django.ui.studio.urls")),
]
```

The studio's pages, scripts and styles are its own templates and static files
(`scrud_studio/`), with no inline scripts, so a `script-src 'self'` policy
works. The URL names are in the `scrud-studio` namespace.

### Signing in

Every studio view is a DRF view that needs an authenticated user. An anonymous
request gets a 401, not a redirect to a login page. Sign in first at your API's
login (the demo has `/api-auth/login/`), then open the studio; API clients
send their usual credentials.

### What the studio shows

What a user can see or do comes from their grants, whatever the studio shows:

- The **type browser** (`/studio/`) lists every registered type with a slug.
  Reading a type's items is checked where they are read.
- The **branch picker** lists the branches the user may select: `GET` on the
  branch and on its working ancestors, an intact chain and its pinned
  snapshots. `main` and published branches are listed too, read-only: the
  studio edits working branches only, and changes reach a published branch by
  a push. The page opens on the first working branch the user may select.
- A rule from a stylesheet the user may not `GET` shows in the inspector as its
  rank only (`{"readable": false, "rank": [...], "outcome": ...}`): no
  selector, component or stylesheet name. A packaged
  [layer](#packaged-ui-layers)'s item is public: a layer stylesheet's rules
  are readable to everyone, here, in the live preview's mark, in the editor's
  item read, in the studio's computed data, in New component (its "inside this
  type" option and its copy of an inherited item) and in a push's preview and
  conflicts alike (`scrud_django.ui.branches.reads.may_read_item` is the one
  check). A packaged item is one with no stored row: a stored row whose
  content names the layers' branch (`@layers`, which the viewsets refuse) is
  checked as any stored row. A component the user may not read (a packaged
  one is readable) has no "inside this type" option. The scope chain of a node is shown in full,
  though, even when a scope came from a component the user cannot read: scope
  names are presentation identifiers an author needs to target rules.
- Previewing in a data context other than `""` needs `GET` on UI stylesheets in
  that context, else a 400 (`ui-studio-context-unavailable`).

### The preview

The preview is an `<iframe>` with `sandbox="allow-same-origin"` and **no**
`allow-scripts`, so scripts in a branch's components do not run there. This is
deliberate: the studio page's own script handles clicks inside the frame (it
reaches into it because of `allow-same-origin`), and branch code, which anyone
who may write to the branch controls, cannot reach the studio's controls or the
user's session. The preview shows the markup and CSS of the branch, not its
behaviour (no htmx, no editing). Every `preview` response, an error too,
also carries `Content-Security-Policy: sandbox allow-same-origin` (and
`X-Frame-Options: SAMEORIGIN`), so a preview URL opened on its own, outside
the iframe, is just as sandboxed: no script runs in it.

Each node is marked with its JSON Pointer: it is wrapped in a
`<scrud-node data-pointer="...">` element with `display: contents`. The
wrapper has two caveats. A branch's CSS that uses a child combinator (`ul > li`)
does not match across it, so the preview can look different from the app
there. And the HTML parser moves an unknown element out of a table, so a node a
component renders as a table row or cell (`<tr>`, `<td>`) has no wrapper around
it and cannot be selected in the preview; select its table, or a node inside a
cell. A component that fails to render
shows the built-in fallback in the preview, but the inspector reports the
stored rule as the winner: it ranks rules and does not run templates.

A node whose parent template does not print its child HTML is not shown in the
preview, so a rule on it can report `wins: true` and change nothing visible; in
the demo, `/items/0/text` is such a node (the item template prints
`value.text` itself).

The `preview` endpoint is negotiated by DRF and answers 406 to a request that
accepts only `text/html`; browsers send `*/*`, so iframes are fine.

A branch-browsing confirmation token (`ui-branch-confirm`) needs the
`csrftoken` cookie, including for HTTP Basic clients: keep the cookie from the
first response.

### The editor

A click on a node opens its winning component in the editor, when it is a
stored one you may read; the branch strip opens any item the branch changed.
After a save or a revert the editor reopens the item it was showing, not the
node's winner; after **New component** it opens the node's winner, the new
component when its rule wins. A save refused with 412 (the item changed since
you opened it) reloads the item and keeps your draft beside it, read-only, to
copy from. **New component** is off while the preview is pinned: it would be
made on the live branch, not on the snapshot the preview shows. A broad match
names the data context the rule is for ("every string in acme on this
branch").

### The studio API

The studio page is built on a JSON API, which the
[self-hosted studio](#self-hosted-studio) uses too. Paths are relative to where you include the URLs.
A studio write calls the resource viewsets in-process (`call_view`), not
through a new HTTP request, so the site's middleware does not run for that
inner call: a middleware-only check on the API's URLs (an IP allowlist on
`/api/`, say) does not cover the studio's writes. The studio's own URL passes
through the middleware as any request does; put such a check on it too, or
leave the studio out of the deployment that needs it.
Every endpoint needs an authenticated user (401 otherwise). The writes are
`POST` with a JSON object body (a body that is not an object is a 400) and are
protected by CSRF when a session is used. A success from a write also carries
`HX-Trigger: scrud-studio-saved`. `live-preview` is a `POST` too, but
form-encoded, and it writes nothing. Errors are problem documents (the API's
`application/problem+json`) and a refusal by the resource view is passed
through with its status.

Shared query parameters (for `sample`, `preview`, `explain` and
`new-component/options`):

| Parameter | Meaning |
|---|---|
| `branch` | a selection value: a branch name (default `main`), optionally pinned at its branching point |
| `mode` | `view` (default) or `edit`; anything else is a 400 |
| `scope` | the scope chain, space separated |
| `context` | the data context of the sample (default `""`) |
| `instance` | the pk of a real instance, read through its resource view with the user's rights (a refused read answers as the view does) |
| `pointer` | a JSON Pointer to a node (default `""`, the resource) |

A `branch` that is unknown, unreadable, has a broken chain, a missing pinned
snapshot or is not a selection value gives 400 `ui-branch-unavailable`, the same
for every cause.

#### Pages and fragments

| Endpoint | Response |
|---|---|
| `GET` on the include's root (e.g. `/studio/`) | the type browser (HTML); `?q=` filters by slug, title or type IRI; with `HX-Request` only the list |
| `GET types/<slug>/` | the studio page for the type, from the query above plus `pinned=1`; unknown slug gives 404 |
| `GET types/<slug>/strip` | the branch strip alone (HTML): `?branch=` names the branch; `UIBranchUnavailableError` for one the user may not select |

#### `GET types/<slug>/sample`

The sample instance of the type, generated from its JSON Schema: a `default`,
else the first `examples` or `enum` value, else a value by type (a string by
its `format`, an integer or number at its `minimum`, `false`, a one-item array,
an object of its properties). A schema without a `type` infers it from
`properties`, `items`, `allOf`, `oneOf` or `anyOf`; a `$ref` cycle, a node
deeper than the API's body depth limit and any construct not understood give
`null`. Parameters: `context` (it becomes the
sample's `rbac_context`). Response: the JSON. Errors: 400
`ui-studio-context-unavailable`.

#### `GET types/<slug>/preview`

The HTML page the iframe shows: the sample (or `instance`) rendered on `branch`
in preview mode, linking that branch's CSS. A sample is shown as editable and
not deletable; an instance has the user's own decisions. Parameters: `branch`,
`mode`, `scope`, `context`, `instance`. Errors: 400 `ui-branch-unavailable`,
400 `ui-studio-context-unavailable`, 400 for a bad `mode`, and the resource
view's own refusal for an `instance`. Every response, errors included, carries
`Content-Security-Policy: sandbox allow-same-origin` and `X-Frame-Options:
SAMEORIGIN`.

#### `POST live-preview`

The live preview: what a branch would show with unsaved drafts of its items,
or what a branch will show after a push, with the matches of one rule marked.
It is the preview the [self-hosted studio](#self-hosted-studio)'s workbench
shows while its author types. It is a `POST` because it carries a body, and it never writes: drafts
are layered over the branch for one render and never stored.

The body is form-encoded (`application/x-www-form-urlencoded`) with one field,
`request`, holding JSON, so that a `<form method="post" target="<iframe
name>">` can submit it into the preview frame together with the CSRF token
(`csrfmiddlewaretoken`; the session's CSRF check applies). It is not a
resource view, so the API's JSON-only rule does not apply.

```text
{"type": "todo-lists",          // the type's slug (required)
 "branch": "wip",               // a selection value (default "main")
 "mode": "view",                // or "edit" (default "view")
 "scope": ["row"],              // the scope chain (default [])
 "context": "",                 // the sample's data context (default "")
 "instance": 12,                // a pk, or null for the sample
 "drafts": [{"kind": "component",
             "content": {"name": "todo-row", "engine": "jinja2",
                         "mode": "view", "template": "..."}}],
 "mark": {"stylesheet": "studio", "applies_to": "", "index": 0},  // or null
 "after_push": {"source": "wip"}}                                  // or null
```

`branch`, `mode`, `scope`, `context` and `instance` are the `preview`
parameters, read and checked the same way.

- **Drafts.** At most 50 per request (more is 400
  `ui-studio-preview-invalid`); one per item (a second draft of an item is
  listed as an error). `kind` is `component`, `stylesheet`, `css` or `js`; `content`
  is the item as `save-item` takes it, with its `name` (and a stylesheet's
  `applies_to`). Each draft is shown as the branch's own row of the item, in
  place of its stored one, and ranks in the cascade where its save would put
  it (an own row's place, or after every stored stylesheet for a new one).
  A draft is shown only if its save would be accepted: the item's resource
  view is asked the checks of the write `save-item` would make (a create of
  the branch's override, or an update of its own row: the permission checks
  on the body and the row, the item's own checks, the type's JSON Schema),
  without making it. A draft that fails is left out and listed with its
  errors (below). Embedded resources render in process, so they show the
  drafts too; the data they read does not (a list of the branch's items lists
  the stored item, not the draft).
- **Drafts on a published branch** (`main`, or one with a prefix) are refused
  with 400 `ui-studio-branch-read-only`: the studio edits working branches.
- **After a push.** With `after_push`, the branch is shown with the source
  branch's stored overrides layered on as the push would write them (its
  deletion markers hiding items): "the site after this push". It needs `GET`
  on the source and on the branch, and no write: proposers and reviewers
  often lack write on the target. A source the user may not `GET`, or that
  does not exist, is 403, alike. It takes no drafts of its own: `after_push`
  with drafts is 400 `ui-studio-preview-invalid`.
- **Markers.** With `mark`, each `<scrud-node>` whose node the rule matches
  gets `data-scrud-match="wins"` when it is the rule that node's component
  comes from, else `data-scrud-match="loses"`, and
  `data-scrud-match-outcome` with the inspector's outcome word (`wins`,
  `fewer scopes`, `less specific term`, `lower cascade rank`, `missing
  component`, `wrong mode`). The rule is the one at `index` (the last part of
  its cascade rank) of the stylesheet `stylesheet` that applies to
  `applies_to`, as the branch (drafts included) resolves it; the default
  stylesheet's rules can be marked by its name. Markers are computed with the
  cascade's own ranking, so they agree with the render and with `explain`. A
  stylesheet the branch does not show marks nothing, and so does one the user
  may not `GET` (as `explain` hides its rules): the response is then the same
  as for a stylesheet that does not exist.

The response is the preview document: the frame rendered as `preview`
renders it (no write attributes, each node in a `<scrud-node
data-pointer>`), the branch's CSS in `<style>` elements and its JS in
`<script>` elements, inlined, `scrud_studio/preview.css`, htmx and
`scrud-ui.js` as a page has them, and `scrud_studio/preview-bridge.js`. The
drafts left out are listed in
`<script type="application/json" id="scrud-preview-errors">` as
`[{"kind", "name", "messages"}]`, where `messages` are what the save would
answer. A check that fails as a server error (status 500 or more) is listed
the same way and also logged at ERROR (logger `scrud_django.ui.studio.drafts`).

**Isolation.** Every response, errors included, carries

```text
Content-Security-Policy: sandbox allow-scripts; default-src 'none';
  script-src 'self' 'nonce-<n>'; style-src 'self' 'nonce-<n>';
  img-src 'self' data:; connect-src 'self'
X-Frame-Options: SAMEORIGIN
```

(on one line), with a nonce `<n>` new with each response. That includes the
CSRF and login refusals and the errors Django itself answers (a body over
`DATA_UPLOAD_MAX_MEMORY_SIZE` or with too many fields, 400; an unexpected
error, 500), which the view answers as Django would, with these headers. The branch's
scripts run (unlike in `preview`), but the document has an opaque origin
(`allow-scripts` without `allow-same-origin`): they cannot reach the studio's
page, the user's cookies or their session, whoever wrote them. A request made
from an opaque origin is cross-site, so it carries no login: the branch's CSS
and JS files, which need `GET` on the branch and are cached privately, would
not load by URL. That is why they are inlined, and why the nonce exists: it
lets exactly those inline elements (and the errors list) run or apply, and
nothing else inline, such as a `<script>` a component template prints. The
frame's JSON content blocks (`scrud-content`) carry no nonce; they are data,
which a browser never runs. Embeds are composed on the server before the
sandbox applies, so they show; only the requests the previewed page's own
scripts make at run time go without the login.

**The bridge.** The studio page cannot read an opaque-origin frame's DOM, so
`preview-bridge.js` posts to `window.parent`, with target origin `"*"` (the
frame's own origin is opaque; only a same-origin page may frame it):

- `{"type": "scrud-preview:errors", "errors": [...]}` once, when loaded: the
  drafts left out, as listed above, read when the bridge runs (before the
  branch's scripts, which could change the element). When the list cannot be
  read, it is `{"type": "scrud-preview:errors", "errors": null, "failed":
  true}`, never an empty list (which would say every draft was shown);
- `{"type": "scrud-preview:marks", "wins": <M>, "loses": <L>}` once, when
  loaded, after the errors message: the number of nodes marked
  `data-scrud-match="wins"` and `"loses"` (see **Markers** above), counted
  when the bridge runs, before the branch's scripts. It is posted for every
  preview, with `0` and `0` when the request had no `mark`;
- `{"type": "scrud-preview:select", "pointer": "<JSON Pointer>", "href":
  "<URL>" | null}` on a click inside a node: the nearest `<scrud-node>`'s
  pointer, and the `data-scrud-href` of the resource frame it is in (its
  nearest `.scrud-resource`; `null` outside one). Inside an embedded
  resource the nearest node is the embedded resource's, its pointer relative
  to that resource, and `href` is that resource's: compare it with the
  preview's own frame to tell them apart. The click's default is prevented,
  so a link in the preview does not navigate it.

The receiving page must check `event.source` is its preview frame and accept
these shapes only. The branch's scripts share the document and can post
messages too, so treat them all as data, never as instructions.

Errors: 400 `ui-studio-preview-invalid` (the `request` field missing, not
JSON (nested too deep included), or a field of the wrong shape; more than 50
drafts; `after_push` with drafts), 400
`ui-branch-unavailable`, 400 `ui-studio-context-unavailable`, 400
`ui-studio-branch-read-only` (drafts on a published branch), 403 (an
`after_push` source the user may not `GET`), 404 for an unknown type, and the
resource view's own refusal for an `instance`.

#### `GET types/<slug>/explain`

The inspector's explanation of the node at `pointer`, with the preview's other
parameters:

```text
{
  "pointer": "/items/0",
  "terms": ["http://example.org/todo#TodoItem", "object"],
  "scope": ["row"],
  "winner": 1,
  "fallback": null,
  "rules": [
    {"readable": true, "rank": [1, 0, ...], "outcome": "wins",
     "selector": "row ex:TodoItem", "component": "todo-row",
     "stylesheet": "todo", "branch": "", "applies_to": ""}
  ]
}
```

`terms` run from most to least specific and `rules` from best, as the cascade
tries them. `outcome` is `wins`; for a rule below the winner, the first part
of its rank that is lower than the winner's: `fewer scopes`, `less specific
term` (a later term of the node) or `lower cascade rank`; for a rule above the
winner, or when there is none, `missing component` or `wrong mode`. A rule the
user may not read says why it lost too. `winner` is an
index into `rules`, or `null` with `fallback` naming the built-in component
(`scrud:json` for a node that renders as JSON whatever the rules). A rule the
user may not read has only `readable: false`, `rank` and `outcome`. Errors:
those of `preview`, and 400 `ui-pointer-not-found` for a pointer to no node the
preview renders.

#### `GET types/<slug>/new-component/options`

What "New component for this node" offers for the node at `pointer`:

```json
{
  "matches": [
    {"select": "http://example.org/todo#text", "label": "ex:text", "broad": false},
    {"select": "string", "label": "every string on this branch", "broad": true}
  ],
  "inside_type": null,
  "where": ["row"],
  "applies_to": [""],
  "name": "todo-lists-items-text-view"
}
```

- `matches`: one per term of the node, most specific first; the first is the
  default. A schema-type term is `broad` and labelled `every <type>[ format=…|
  enum] on this branch`; an IRI is shortened with the type's context prefixes.
- `inside_type`: `{scope, root_component, property, creates_root}` or `null`.
  It is offered for a node with no IRI term below the root, so that a part of
  a type can be styled without restyling every string. `scope` is
  `<slug>-<top property>` (suffixed with a number if another property of the
  root already maps that name; when the root component already maps the
  property to a scope, that scope is reused, never overwritten).
  `root_component` is the root's component now. `creates_root` is true when it
  is a built-in: the action then writes a Jinja copy of the built-in as the
  type's root component on the branch, selected by the root's most specific
  class term. It is `null` when the user may not read the root component.
- `where`: the scopes of the node's chain; one may narrow the rule.
- `applies_to`: `""` and the preview's data context, if it has one.
- `name`: a unique name on the branch for the new component.

Errors: those of `explain`.

#### `POST new-component`

Body: `branch` (a working branch), `slug` (the type), the preview's `mode`,
`scope`, `context` and `instance` (strings, all optional), and:

| Field | Meaning |
|---|---|
| `pointer` | the node |
| `template` | the Jinja template of the new component (required) |
| `match` | one of the options' `select` (default: the first) |
| `inside_type` | boolean; only when the options offered it |
| `where` | one scope of the node's `where` |
| `applies_to` | one of the options' `applies_to` (default `""`) |

In one transaction it optionally gives the root component the scope (or writes
a new root component), creates the Jinja component in the preview's mode, and
adds the rule to the stylesheet named `studio` on the branch for the chosen
`applies_to` (created on first use). Response, 201:
`{"written": [{kind, name[, applies_to]}, ...], "wins": bool, "explanation":
{...}}`, where `wins` says whether the new rule now wins for the node and
`explanation` is the inspector's, re-read after the write. A rule that does
not win is not an error: for instance a rule with no scope loses to an
existing rule scoped to `row`, so pass `where`. Any write refused rolls
everything back and its answer is passed through. Errors: 400
`ui-studio-branch-read-only` and `ui-branch-unavailable` for the branch, 400
for a bad field, and the resource views' (403, 400, 412) as they are.

#### `GET item`

The item `{kind, name, applies_to?}` (query parameters; `kind` is `component`,
`stylesheet` or `css`) as `branch` (default `main`) shows it live, for the
editor: `{kind, name, applies_to, own, from, etag, content}`, with the ETag
header. `own` says that it is the branch's own row (a save is then a `PUT`);
`from` is the branch the row comes from. Deletion markers are shown. 404 when
the branch shows no such item or one the user may not read, alike. 400
`ui-branch-unavailable`.

#### `POST save-item`

Body: `kind`, `name`, `applies_to` (a stylesheet's, default `""`), `branch`
(a working branch) and `content` (an object). The studio sets the item's
`name`, `branch` and authorizing `rbac_context`. If the branch has no row of
its own it creates one (a `POST`, which sets its `base`); otherwise it updates
the row, sending the request's `If-Match`. Response: the resource view's, with
its `ETag`. Errors: 400 `ui-studio-branch-read-only`, 400
`ui-branch-unavailable`, 400 for a bad field, and the view's (403, 412, 428,
400) as they are.

#### `POST revert-item`

Body: `kind`, `name`, `applies_to`, `branch`. Deletes the branch's own row, so
the branch shows its parent's item again (the undo, and "take theirs" for a
conflict). **`If-Match` is required**: the studio sends it as given, so
without one the answer is 428 and a stale one gets 412; a revert must not
silently delete a newer edit on a shared working branch. 404 when the branch
has no row of its own for the item. Errors besides: as `save-item`.

#### `POST pull`

Body: `branch`. Pulls the working branch (moves its branching point onto its
parent's current versions), recomputing conflicts. It reads the branch and
writes it back with `pull: true`, using its own fresh ETag. Response: the
branch resource. Errors: as `save-item`.

#### `POST push`

Body: `branch`, and optionally `target`. It creates an applied UI push in the
target's context. The target defaults to the branch's parent and may be the
parent, `main`, or a branch the user may `GET`; any other, or an unknown one,
gives 400 `ui-branch-unavailable`. A branch with no parent and no target gives
400. Response: 201, the push resource. A push a `WorkflowPolicy` gates answers
as the push API does. Errors: as `save-item`.

#### Errors

| Problem type | Status | When |
|---|---|---|
| `ui-branch-unavailable` | 400 | the branch (or a push target) is unknown or the user may not select it; one answer for both |
| `ui-studio-branch-read-only` | 400 | a write names a published branch (`main` or one with a prefix); refused before anything is dispatched; also drafts in a `live-preview` of one |
| `ui-studio-preview-invalid` | 400 | a `live-preview` whose `request` field is missing, not JSON, has a field of the wrong shape, sends more than 50 drafts, or sends `after_push` with drafts |
| `ui-studio-context-unavailable` | 400 | a sample or preview in a data context whose UI stylesheets the user may not read |
| `ui-pointer-not-found` | 400 | `explain` (or the options) for a pointer to no node the preview renders |

The problem `type` is `https://api.openteams.com/http-problem/<name>`.

### Studio data as computed resources

The studio's data that is not a stored resource is also served as
[computed resource types](#computed-resource-types), so a studio built as a UI
layer renders it through the cascade, and a branch can restyle it, like any
resource. They are part of the API (under `scrud_django.urls`, not the
studio's include), registered when `scrud_django.ui.studio` is installed, and
left out of the service catalog as every computed type is. Each is read-only
(`GET`, `HEAD` and `OPTIONS`; any other method is a 405), needs an
authenticated user, has an ETag for `If-None-Match`, links its JSON Schema and
JSON-LD context, and answers only what its user may `GET` as JSON: a branch the
user may select (else 404, the same for every cause; the push preview instead
needs `GET` on each of its two branches, [below](#what-a-push-will-change)),
and an item or conflict on a row the item's own read check allows. The type
catalog checks nothing: it lists every registered type, as the type browser
does, and reading a type's items is checked where they are read. Packaged
[layer](#packaged-ui-layers) items are public. The bootstrap studio's strip and picker read the same functions
(`scrud_django.ui.studio.page`), so they agree on the items, changes and
branches, but not on conflicts: the strip lists every conflict a pull recorded
on the branch (its `conflicts` field, which a user who may GET the branch reads
anyway), while the computed types list only conflicts on the branch's own
rows the user may read (and add its layer conflicts).

| Path | Type IRI | Content |
|---|---|---|
| `ui-branch-items/<branch>/` | `http://api.openteams.com/json-ld/UIBranchItems` | every item the branch shows, and how to override it |
| `ui-branch-status/<branch>/` | `http://api.openteams.com/json-ld/UIBranchStatus` | the branch's chain, changes, conflicts, browse URL, rights, pushes, versions, actions and the studio's endpoints |
| `ui-type-catalog/all/` | `http://api.openteams.com/json-ld/UITypeCatalog` | the registered types |
| `ui-selectable-branches/mine/` | `http://api.openteams.com/json-ld/UISelectableBranches` | the branches the user may select |
| `ui-push-previews/<source>/?target=<name>` | `http://api.openteams.com/json-ld/UIPushPreview` | what a push would change ([below](#what-a-push-will-change)) |

`<branch>` is a selection value: a branch name, or `<name>@pinned` for the
branch at its branching point. The catalog's only key is `all` and the picker's
only key is `mine`; any other key is a 404.

**Actions.** Where studio data offers a write, it gives it as an *action*,
`{"method", "href", "body", "etag"}`: what a control sends unchanged, as a
[fixed-body element](#writes-from-the-browser) does. `method` is `POST` or
`PUT`; `href` is an API path (a component maps it into its prefix with
`in_mount(href)`); `body` is the JSON body, sent as it is (`data-scrud-body`);
`etag` is the quoted `If-Match` to send (`data-scrud-etag`), or `null` for
none (`data-scrud-etag=""`). An action is offered only when the user's grants
allow it: the method on the body's type in the body's contexts, the body read
there, counting what a condition allows only inside a write's checks (a
`scrud_workflow` condition) as allowed. Offering one is a convenience: the
write's own checks decide, and a refused write answers as it would to any
client. An action is `null` when it is not offered.

**Branch items** lists the components, stylesheets, CSS and JS items the
branch shows (live, or pinned), by kind, each kind in the cascade's order:

```json
{"branch": "wip", "items": [
  {"kind": "component", "name": "row", "origin": "own", "from": "wip",
   "href": "/api/ui-components/12/", "etag": "\"3f…\"",
   "content": {"name": "row", "engine": "jinja2", "…": "…"},
   "conflict": "parent", "override": null, "reverts_to": "inherited"},
  {"kind": "component", "name": "head", "origin": "inherited", "from": "main",
   "href": "/api/ui-components/7/", "etag": "\"9a…\"", "content": {"…": "…"},
   "conflict": null,
   "override": {"method": "POST", "href": "/api/ui-components/",
                "body": {"name": "head", "engine": "jinja2", "…": "…",
                         "branch": "wip", "rbac_context": "alice"},
                "etag": null},
   "reverts_to": null},
  {"kind": "stylesheet", "name": "studio:workbench", "applies_to": "",
   "origin": "packaged", "from": null, "href": null, "etag": "\"c0…\"",
   "content": {"…": "…"}, "conflict": null, "override": {"…": "…"},
   "reverts_to": null}
]}
```

- `origin` is `own` (the branch's row), `inherited` (an ancestor's, named by
  `from`) or `packaged` (an installed layer's; `from` is `null`).
- `href` is the item's resource, `null` for a packaged item and for an
  ancestor's item a pinned branch reads from a version.
- `etag` is quoted, as an `If-Match` header carries it.
- `applies_to` is given for stylesheets only.
- `conflict` is set on own items only: `parent` for a conflict a pull listed on
  the branch (the branch's `conflicts` field), `layer` for an override of a
  packaged item that changed since the override was based on it, or whose
  layer item is gone ([layer conflicts](#packaged-ui-layers)). Where both
  apply, it is `parent`.
- `override` is the action that creates the branch's override of an item it
  shows but does not own (inherited or packaged): a `POST` to the item type's
  list of the item's content without `base` and `deleted`, with `branch` set
  to the branch, a stylesheet's `applies_to`, and `rbac_context` set to the
  item's [authorizing context](#authorizing-contexts) on the branch, as a
  studio save sends it. The server sets `base` (the inherited item's ETag, or
  the packaged item's). It is `null` on the branch's own items, on a
  published branch (`main`, or any with a prefix: changes reach it by a
  push), on a `@pinned` view (it shows the parent as of the branching point,
  but the server bases an override on what the branch shows itself, so it
  would silently revert the parent's changes since), for a deletion marker,
  and when the user may not `POST` that body.
- `reverts_to` is set on own items only: what the branch shows once its row
  is deleted, `inherited` (an ancestor's item, as the branch shows its
  parent) or `packaged` (an installed layer's); `null` when the branch added
  the item and there is nothing beneath it, and when what is beneath is an
  item the user may not `GET` (the entry reveals no more than their JSON
  would). It is `null` for items the branch does not own.
- A row the user may not `GET` is left out entirely, as its JSON list leaves
  it out. So is a row that rendering skips (stored with another context than
  its branch authorizes).

**Branch status**:

```json
{"branch": "feat", "published": false,
 "chain": [{"name": "feat", "published": false, "prefix": null},
           {"name": "shop", "published": true, "prefix": "/shop/"},
           {"name": "main", "published": true, "prefix": ""}],
 "changed": [{"kind": "component", "name": "row", "applies_to": "", "deleted": false}],
 "conflicts": [{"kind": "component", "name": "row", "applies_to": "", "with": "parent"},
               {"kind": "stylesheet", "name": "studio:workbench", "applies_to": "", "with": "layer"}],
 "browse_url": "/shop/todo-lists/?ui-branch=feat",
 "may_write": true, "may_publish": false,
 "pushes": [{"href": "/api/ui-pushes/31/", "source": "feat", "target": "shop",
             "state": "proposed", "label": null}],
 "versions": [{"number": 4, "href": "/api/ui-branch-versions/58/",
               "restore": {"method": "POST", "href": "/api/ui-pushes/",
                           "body": {"source": null, "target": "feat",
                                    "restore": 4, "state": "applied",
                                    "rbac_context": "project-a"},
                           "etag": null}},
              {"number": 3, "href": "/api/ui-branch-versions/51/",
               "restore": {"…": "…"}}],
 "actions": {
   "pull": {"method": "PUT", "href": "/api/ui-branches/9/",
            "body": {"name": "feat", "parent": "shop", "…": "…", "pull": true},
            "etag": "\"5d…\""},
   "promote": [
     {"target": "shop",
      "apply": {"method": "POST", "href": "/api/ui-pushes/",
                "body": {"source": "feat", "target": "shop", "state": "applied",
                         "rbac_context": "project-a"},
                "etag": null},
      "propose": {"method": "POST", "href": "/api/ui-pushes/",
                  "body": {"source": "feat", "target": "shop", "state": "proposed",
                           "rbac_context": "project-a"},
                  "etag": null}},
     {"target": "main", "apply": null, "propose": null}]},
 "endpoints": {"live_preview": "/studio/live-preview",
               "sample": "/studio/types/{slug}/sample",
               "explain": "/studio/types/{slug}/explain",
               "options": "/studio/types/{slug}/new-component/options",
               "new_component": "/studio/new-component"}}
```

- `chain` is the branch and its ancestors, nearest first: the branch, its
  working ancestors (selecting it needs `GET` on each) and its published root
  (which `browse_url` names anyway), then each ancestor above that root only
  when the user may `GET` it (`main`, which everyone may select, always). A
  published ancestor the user may not `GET` is left out.
- `changed` are the branch's own rows the user may read (overrides, additions
  and deletion markers), as the strip lists them; none on `main`.
- `conflicts` are those a pull listed (`with: "parent"`), then the layer
  conflicts (`with: "layer"`), each only on an own row the user may read.
- `browse_url` needs `?type=<slug>`: that type's list on the branch, under its
  published root's prefix, as the strip's link. Without `type`, or for a slug
  with no list, it is `null`.
- `may_write` is `true` when the branch is a working branch and the user may
  `PUT` it (a published branch is never written in the studio).
  `may_publish` is `true` when the user may `PUT` UI branches in the context
  `""`, the grant that sets, changes or removes a prefix
  ([Who may publish](#prefixes)).
- `pushes` are the latest 20 [pushes](#pushing-and-restoring) with the branch
  as `source` or `target` (restores included), newest first, each one the user
  may `GET` (the check its detail route makes; the others are left out, not
  counted). `label` is the push's, or `null`.
- `versions` are the branch's latest 20 versions the user may `GET`, newest
  first; none when they may not `GET` its versions. Each one's `restore` is
  the `POST` to `ui-pushes` of `{"source": null, "target": <branch>,
  "restore": <number>, "state": "applied", "rbac_context": <the branch's
  context>}` (a [restore](#pushing-and-restoring)) when the user may `POST`
  that body, on a published branch too (`main` included): rolling back a
  publish is what a restore is for; else `null`.
- `actions` are what the studio may send to change the branch:
  - `pull`: the `PUT` of the branch's stored content with `"pull": true` to
    the branch's resource, with its ETag, when it has a parent and
    `may_write`;
  - `promote`: one entry per ancestor in `chain`, nearest first,
    `{"target", "apply", "propose"}`, where `apply` and `propose` are the
    `POST`s to `ui-pushes` of `{"source": <branch>, "target": <ancestor>,
    "state": "applied" | "proposed", "rbac_context": <the ancestor's
    context>}`, each when the user may `POST` that body. Ancestors the chain
    leaves out are not offered.

  On a published branch, `pull` is `null` and `promote` is empty. A
  `@pinned` view has the same actions as the live one: they act on the
  branch's stored rows, not on what the snapshot shows.
- `endpoints` are the bootstrap studio's own endpoints a studio built as a
  layer calls, reversed from the `scrud-studio` namespace (so they include
  where its URLs are included, and are not API paths; see
  [The studio API](#the-studio-api)): `live_preview` (`POST live-preview`),
  `sample` (`GET types/<slug>/sample`, which is also the `data-scrud-href`
  of a sample's frame in a preview), `explain` (`GET types/<slug>/explain`),
  `options` (`GET types/<slug>/new-component/options`) and `new_component`
  (`POST new-component`).
  `sample`, `explain` and `options` take the type's slug: they are templates
  in which `{slug}` stands for it (replace it, URL-encoded). `endpoints` is
  `null` when the studio's URLs are not included.

**Type catalog**: `{"types": [{"slug", "title", "type_uri"}]}`, every
registered type with a slug, by slug, as the type browser lists them
(unfiltered: reading a type's items is checked where they are read).

**Selectable branches**: `{"branches": [{"name", "published", "href"}]}`, the
branches the branch picker lists (`main` first, then by name). `href` is the
branch's resource, or `null` when the user may not `GET` it (as for `main`,
which everyone may select) or it is not stored yet.

### What a push will change

`ui-push-previews/<source>/?target=<name>` shows what a
[push](#pushing-and-restoring) of branch `<source>` into `<name>` (default:
`<source>`'s parent) would write there, and the conflicts it would hit, before
the author applies or proposes it. It computes the same plan the push writes
(`scrud_django.ui.branches.sync.plan_push`), without writing anything:

```json
{"source": "wip", "target": "main",
 "changes": [
   {"kind": "component", "key": ["row"], "action": "update",
    "before": {"name": "row", "template": "<b>main</b>", "rbac_context": "", "…": "…"},
    "after": {"name": "row", "template": "<i>wip</i>", "branch": "main",
              "rbac_context": "", "base": null, "…": "…"}},
   {"kind": "component", "key": ["fresh"], "action": "create",
    "before": null, "after": {"name": "fresh", "…": "…"}},
   {"kind": "component", "key": ["gone"], "action": "delete",
    "before": {"name": "gone", "…": "…"}, "after": null}],
 "unreadable_changes": 1,
 "conflicts": [{"kind": "component", "key": ["row"],
                "base": "3f…", "current": "9a…"}],
 "unreadable_conflicts": 1}
```

- `changes` are the push's writes into the target, in the order it makes
  them, one per row. They describe the target only: applying the push also
  deletes the source's own rows (its overrides) and clears its conflicts, as
  [Pushing and restoring](#pushing-and-restoring) describes. Each is a
  `create` (a new row), an `update` (of the target's row with the key) or a
  `delete` (of it). A deletion marker in the source deletes the target's row
  and, where the target's parent (or a packaged layer) has the item, also
  creates a marker (`"deleted": true`) that hides it: two entries.
- `before` is the target's row a write updates or deletes; for a create, the
  item the target shows there now, from its parent or a packaged layer
  (`null` for none). `after` is the row exactly as the push stores it (its
  `branch`, `rbac_context` and `base` on the target); `null` for a delete.
- A write on an item the user may not `GET` (the target's row, the row as
  written, the source's row it comes from, or, for a create, the item the
  target shows there now: its `before`) is left out of `changes`;
  `unreadable_changes` counts them. A packaged layer's item is public.
- `conflicts` are those the push would be refused for (409), on items the
  user may read in the target, as the 409 lists them (a packaged layer's item
  is public here too, as in `changes`); `unreadable_conflicts` counts the
  others. A push with any conflict, readable or not, is refused.
- It needs `GET` on both branches (`main` needs none, as for a push): a
  branch (or `?target=`) the user may not `GET` is a 404, the same as an
  unknown one, before anything else is checked. A push the server would
  refuse anyway (from a branch with no parent, into itself or a descendant,
  into a branch pinned to a missing version) is a 400 `ui-branch-invalid`, as
  the push is. It does not check the grants the
  push's writes need: applying it checks them (403 `ui-push-forbidden`).

## Self-hosted studio

The self-hosted studio is a studio made of the UI itself: its pages are the
UI types' own resources (a branch, a component, a stylesheet, a push, a
version) rendered through the cascade in the scope `studio`, and styled by
the packaged layer of `scrud_django.ui.studio`. Since the studio's look is UI
items like any other, an author edits the studio with the studio, on a
working branch, and promotes the change as any other. It is built on runtime
features this page describes ([embeds](#embeds),
[computed resource types](#computed-resource-types),
[safe mode](#safe-mode), the requested view mode of
[Modes, editing and the write path](#modes-editing-and-the-write-path)), on
the [studio API](#the-studio-api) (the live preview, `explain`, the new rule)
and on the [studio data](#studio-data-as-computed-resources). Like the
[bootstrap studio](#studio), it adds no rights: every write is an ordinary
request to a resource view, which makes its own checks, and a control is shown
only where the data offers its action. The design is in
`docs/superpowers/specs/2026-11-10-ui-self-hosted-studio-design.md`.

Two branches are at work on a studio page:

- **S** is the branch the request selects, as for any page (the prefix, a
  header, the parameter or the cookie; see [Browsing](#browsing)): normally the
  studio root under its prefix. It decides how the studio looks.
- **T** is the branch whose data is shown: the `ui-branches` resource rendered
  as the workbench, and the branch named in the URL of each studio resource
  the workbench embeds. It is any branch the user may `GET`, in any tree.

Normally S is the published studio and T an author's working branch. The
layer's components and scripts never name S: they never set
`Scrud-UI-Branch`, and `scrud-ui.js` keeps sending the page's own branch.

### Setting up the self-hosted studio

**The layer.** Installing `scrud_django.ui.studio` (as in [Setup](#setup))
installs its packaged UI layer, prefix `studio:`
(`scrud_django.ui.studio.layer`), beneath every branch (see
[Packaged UI layers](#packaged-ui-layers)). Its stylesheet `studio:studio`
selects only in the scope `studio` (`studio scrud:page` takes the page to
the component `studio:page`), so a page without `?ui-scope=studio` renders as
before. `studio:page` keeps what every page needs (the CSRF meta tag, both
static scripts, the CSS and JS items' links, and the chrome) and wraps the body
in `<div class="studio" data-studio>`. The layer's CSS item `studio:studio`
and JS items `studio:studio` (order 0, [the workbench](#the-workbench)'s),
`studio:rules` (order 1, [the rule editor](#the-rule-editor)'s),
`studio:inspector` (order 2,
[the preview pane and the inspector](#the-preview-pane-and-the-inspector)),
`studio:promote` (order 3, [promote and push records](#promote-and-push-records))
and `studio:branch` (order 4, [branch settings](#branch-settings)), the last
four using the API the first sets up, are effective on every branch, so every
page links them: every selector of the CSS is under a `.studio` class or a
`[data-studio…]` attribute, and each script does nothing unless the page has a
`[data-studio-workbench]` element, with one exception: on a push's page (a
`[data-studio-push]` element) `studio:promote` stops the `change` of the
push's state input, and does nothing else. No layer component has an inline
script or an event handler attribute, so a `script-src 'self'` policy works. A
branch overrides any of these items by name, as it does any layer item (see
[Editing the studio](#editing-the-studio)).

**The studio root.** The studio is served from a published branch, the
*studio root*: a root branch (no parent) with a prefix, which a setting names.

```python
SCRUD_UI_STUDIO_BRANCH = "studio"  # default: None
```

`scrud_django.ui.studio.studio_branch()` reads it. Make it a root of its own,
not a branch of `main`'s tree, with its own working branches: CSS and JS items
apply to every page of a branch, so under `main`'s tree the app's CSS would
restyle the studio and the app's JS would run on the studio's pages, in the
sessions of the people who publish (see [Editing the studio](#editing-the-studio)).
Grant its context to the studio's authors as for any branch: `GET` on the root
(and its versions, for its history), and, to push into it, `ui-pushes` and the
item writes a push applies there.

Studio pages are always in the scope `studio` (their URLs carry
`?ui-scope=studio`); the studio root's own workbench is its `ui-branches`
detail under its prefix, for example
`/studio-root/ui-branches/3/?ui-scope=studio&ui-mode=view`. A workbench URL
also requests view mode (`ui-mode=view`): the workbench is a view of its
branch, and a user who may `PUT` the branch would otherwise get the branch's
edit form, in which the view-mode workbench component is not used (and a
requested view mode leaves out the frame's Delete button).

The demo seeds such a tree: the root `studio` at `/workbench/` (context
`ui-studio`) and its working branch `studio-wip` (context `ui-studio-wip`); see
[The to-do demo](#the-to-do-demo).

### The workbench

A branch's workbench is its `ui-branches` detail in the scope `studio`, in
view mode: `<prefix>ui-branches/<pk>/?ui-scope=studio&ui-mode=view`. The
branch it shows is *T*; the page itself renders on the branch its URL selects
(*S*, the studio root under its prefix). The layer's stylesheet
`studio:studio` takes the root of `UIBranch`
(`http://api.openteams.com/json-ld/UIBranch`) in the scope `studio` to the
component `studio:workbench`, which renders:

- `<div class="studio-workbench" data-studio-workbench
  data-studio-branch="<T>" data-studio-published="true|false">`; T is
  published when it is `main` or its content has a `prefix`;
- a top bar with T's name and the Items, Editor and Preview toggles;
- "Branch settings" and "New branch" (see [Branch settings](#branch-settings));
- a notice (`data-studio-notice`, `role="alert"`, hidden until a write is
  refused);
- three panes, `<section class="studio-pane" data-studio-pane="items|editor|preview">`;
- a bottom strip (`<footer class="studio-strip">`).

The Items pane embeds `api_url("ui-branch-items", T)` in the scope
`studio studio-items`, which the layer renders with `studio:items`; the strip
embeds `api_url("ui-branch-status", T)` in `studio studio-status`
(`studio:status`); the branch settings embed T itself. Each embedding
element carries `data-studio-embed` (`items`, `status`, `branch`),
`data-studio-src` (the path as the page's prefix serves
it, `in_mount`) and `data-studio-scope` (its scope chain), from which the
script fetches it again. The panes read T's studio data as the user's own
JSON would, so items the user may not read are absent, and a write control
is shown only where the data offers its action (see
[Studio data as computed resources](#studio-data-as-computed-resources)).

**Items** (`studio:items`) are a tree by kind (components, stylesheets, CSS,
JS). Each entry is an `<li class="studio-item studio-own|studio-inherited|studio-packaged">`,
with `studio-conflict-parent` or `studio-conflict-layer` for a conflict, and
holds:

- a button with `data-studio-open`, the JSON `{"kind", "name",
  "applies_to", "href", "override", "reverts_to"}` (`applies_to` is `null` but for a
  stylesheet; `href` is the item's resource as the page's prefix serves it,
  or `null` for a packaged item and an ancestor's item a pinned view reads
  from a version);
- for an item with no `href`, a hidden `<pre data-studio-content>` with its
  content as indented JSON (Jinja's `tojson`, so `<`, `>`, `&` and `'` are
  `\u` escapes: parse it, don't show it as is);
- when the item's `override` is offered, an "Override on this branch" button
  that sends it.

**The strip** (`studio:status`) shows T's chain, its changed and conflict
counts (`data-studio-changed`, `data-studio-conflict-count`), Pull
(`actions.pull`, only when offered), "Browse app on this branch" when
`browse_url` is set, a "Promote to <target>…" panel per `actions.promote`
entry, T's pushes (see [Promote and push records](#promote-and-push-records))
and T's history (see [History and restore](#history-and-restore)).
Its root carries the studio's `endpoints` as JSON (`data-studio-endpoints`),
and `published` and `may_publish` as `data-studio-published` and
`data-studio-may-publish` (`true` or `false`). `browse_url` is linked as it is: it
already names the published root's prefix, which is not the studio's.

Every action button is a [fixed-body element](#writes-from-the-browser):
`hx-<method>` with the action's `href` (`in_mount`), `hx-ext="scrud-json"`,
`data-scrud-json="fixed"`, `data-scrud-body` (the body, `tojson`),
`data-scrud-etag` (the action's ETag, or empty for none) and
`hx-swap="none"`: the script refreshes the panes after it.

**The preview pane** holds the preview's controls, a hidden
`<form data-studio-preview-form method="post" target="studio-preview">` with
`csrfmiddlewaretoken` and `request` inputs, and
`<iframe name="studio-preview" sandbox="allow-scripts" class="studio-preview">`,
for the live preview (`POST live-preview`, see [The studio API](#the-studio-api)),
and the inspector under it (see
[The preview pane and the inspector](#the-preview-pane-and-the-inspector)).

**The script** (`studio:studio`'s JS) runs only on a page with a
`[data-studio-workbench]` element:

- **Panes.** A toggle collapses its pane to a rail
  (`data-studio-collapsed`; `aria-pressed` on the toggle). Which panes are
  collapsed is remembered per viewer in `localStorage` under `studio:panes`
  (`{"items": true, "editor": true}`); without storage, or when it refuses,
  every pane starts open and the toggles still work. The preview can't be
  hidden.
- **State in the URL.** The workbench state is in the page's query: `type`
  (the previewed type's slug), `item` (the open item:
  `<kind>:<name>`, and `@<applies_to>` for a stylesheet that applies to a
  context), `pointer`, `mode`, `context`, `scope`, `instance` and `pinned`.
  The script rewrites them with `history.replaceState`, and keeps every
  other parameter (`ui-scope=studio`, `ui-mode=view`, `ui-safe`), so a
  workbench URL can be shared and reloaded.
- **The strip with a type.** When the state has a `type` (on load, and when
  it changes), the strip is fetched again with `?type=<type>`, for its browse
  link.
- **Opening an item.** A click on an entry sets `item` and dispatches
  `studio:open` on the workbench (`detail.open` is the entry's
  `data-studio-open`, `detail.entry` the entry), which the editor handles
  (see [The editors](#the-editors)). The `item` in the URL opens on load.
- **After a write.** After any write sent from inside the workbench
  (`htmx:afterRequest` of a non-GET) that succeeds, the items and the strip
  are fetched again (an htmx GET of `data-studio-src`, with the pane's own
  `Scrud-UI-Scope`) and the preview is submitted again. After a POST or a
  DELETE (an override, a revert, a push), the open item is opened again from
  the refreshed tree; after a save it is not (the save's response is the
  editor). A refused write shows
  the server's error text in the workbench's notice, as text: a problem's
  `title` and `detail`, else the notices of the HTML it answered.
- **The preview.** `window.ScrudStudio.submitPreview(extra)` sets the form's
  action to the strip's `endpoints.live_preview`, its CSRF token from the
  page's `csrf-token` meta tag, and its `request` to the URL state (`type`,
  T as `branch`, or `T@pinned` when `pinned` is `1`; `mode`, default `view`;
  `scope` as a list; `context`; `instance`, a number when it is one, else
  `null`) with `drafts`, `mark` and `after_push` from `extra`, and submits it
  into the frame; `extra.branch`, when given, is sent as `branch` instead
  (the target of "Preview after push"). On a published T it sends no
  drafts, nor with `after_push` (the server refuses drafts with it). Without a `type`, or
  without `endpoints`, nothing is submitted (it returns `false`). The
  preview is submitted on load when the state has a type.

The script never sets `Scrud-UI-Branch`: its requests go through htmx, and
`scrud-ui.js` sends the page's branch (S) and, on a safe page,
`Scrud-UI-Safe`. `window.ScrudStudio` also has `state()`, `setState(changes)`
(`null` or `""` removes a key), `itemKey(open)`, `refresh(pane)`, `reopen()`
(open the state's `item` from the tree as it is), `contentOf(frame)` (a
frame's own `scrud-content`, parsed, or `null`), `branch` and `published`;
and, for the layer's other scripts, `endpoints()` (the strip's, or `null`),
`csrfToken()` (the meta tag's), `safe()` (whether the page is in safe mode,
as `scrud-ui.js` decides it), `fromPreview(event)` (whether a `message`
event's `source` is the preview frame's window), `expand(pane)` (open a
collapsed pane), `parseOpen(entry)`, `entryFor(item)` (the tree's entry for
an `item` key, or `null`), `open(entry)` (open a tree entry, as a click on
it does) and `errorText(xhr)` (a refused request's text).

### The editors

The Editor pane holds a header (`data-studio-editor-head`), the editor
itself (`data-studio-editor`) and, under it, the drafts the preview left out
(`data-studio-draft-errors`). Opening an item fills them:

- **The header** names the item (`<kind> <name>`), where it comes from (the
  tree's `own`, `from <branch>` or `packaged`) and, for an item T does not
  own, a copy of the tree's "Override on this branch" button, processed by
  htmx: its body is the server's (`override`, see
  [Studio data as computed resources](#studio-data-as-computed-resources)),
  never built by the script. After the override (a 201) the panes are
  fetched again and the new own row opens.
- **An item with an `href`** is loaded with an htmx GET of it into the
  editor, with `Scrud-UI-Scope: studio studio-editor`, and `Scrud-UI-Mode:
  view` when T is published or the item is not T's own (its entry is not
  `studio-own`). The resource view then decides the mode as for any request:
  edit only for a user who may `PUT` the item. The script never sets
  `Scrud-UI-Branch`.
- **An item with no `href`** (a packaged item, an ancestor's item a pinned
  view reads from a version) shows its content, the entry's
  `<pre data-studio-content>` parsed and indented, as text.

The layer's stylesheet selects, in the scope `studio studio-editor`, the root
of `UIComponent` (`http://api.openteams.com/json-ld/UIComponent`) for
`studio:component-editor` (edit mode) or `studio:component-view` (view
mode), and the roots of `UICss` and `UIJs` for `studio:text-editor` or
`studio:text-view`. Each renders a `<div class="studio-editor"
data-studio-editor-item data-studio-kind="component|css|js"
data-studio-mode="edit|view">`:

- **Edit mode.** The editor is the item's own frame, a `<form>` (see
  [Modes, editing and the write path](#modes-editing-and-the-write-path)).
  It holds a monospace `<textarea class="studio-code" data-studio-code>`
  named by the field's pointer (`/template`, `/css` or `/js`) and a Save
  button, the form's submit: the frame's `PUT` of its `scrud-content` with
  the textarea applied, with `If-Match`. A `change` of the textarea is not
  a save (the script stops it before the frame sees it), so the text is
  saved only by Save. A save that is refused re-renders the editor with the
  messages (Refused writes); a stale one (another tab saved first) gets
  the notice and the frame that reloads the current state, in the editor's
  scope, and the workbench's notice says why.
- **Revert to inherited, or Delete.** The frame's own Delete button is the
  one control that deletes T's row of the item. When the entry's
  `reverts_to` is set (deleting the row shows an ancestor's or a packaged
  item), the script relabels it "Revert to inherited", with a confirmation
  that says the branch's own version is deleted. When it is `null` (T added
  the item, or what is beneath is not readable by the user), it stays
  "Delete", with a confirmation that says the item is deleted from the
  branch with nothing inherited to show instead. Either sends `If-Match`, a
  stale one gets the 412 reload, and on success the panes are fetched again
  and the item opens again: the inherited or packaged item, or a note that
  it is not on the branch.
- **View mode** (a published T, an item T does not own, a user who may not
  `PUT` it) shows the text in a read-only `<textarea>` with no `name`: no
  form, no Save. A requested view mode also has no Delete button.
- **A Django-engine component** is read-only in both modes, with a note: its
  textarea has no `name`, and there is no Save.

**Live typing.** A `keyup` in the editor's code starts a 300 ms timer; when
the typing pauses, the editor's content (the frame's `scrud-content` with the
textarea's value at its pointer) is submitted to the live preview as
`drafts: [{kind, content}]` (`ScrudStudio.submitPreview`). Nothing is sent
when T is published, when the editor is in view mode, when the state has no
`type`, or when the draft is the one the preview already shows. A draft is
never stored: only Save writes.

**Draft errors.** The script accepts a `message` only from the preview
frame's own window (`event.source`), and only `{"type":
"scrud-preview:errors"}`; any other message is ignored. Each draft the
preview left out is listed under the editor as `<kind> <name>: <messages>`,
as text. `failed: true` shows "The preview could not report its errors: a
draft may not be shown.", never an empty list; an empty list hides them.

### The rule editor

A stylesheet opens in the rule editor. The layer's stylesheet selects, in
the scope `studio studio-editor`:

| Selector | Edit mode | View mode |
|---|---|---|
| `UIStylesheet` (`http://api.openteams.com/json-ld/UIStylesheet`) | `studio:stylesheet-editor` | `studio:stylesheet-view` |
| `studio-rules` + its `rules` (`…/UIStylesheetRules`) | `studio:rules-editor` | `studio:rules-view` |
| `studio-rule object` (a rule) | `studio:rule-row` | `studio:rule-row-view` |
| `studio-field string`, `studio-field integer` | `studio:field` | (the built-ins) |

The stylesheet's components put `rules` in the scope `studio-rules`, each
rule in `studio-rule`, and `applies_to`, `order` and a rule's `select` and
`component` in `studio-field`. The editor is a `<div class="studio-editor
studio-stylesheet" data-studio-editor-item data-studio-kind="stylesheet"
data-studio-mode="edit|view" data-studio-stylesheet="<name>"
data-studio-applies-to="<applies_to>">`:

- **Edit mode.** The editor is the stylesheet's own frame, a `<form>`. The
  `name` is shown, not an input. `applies_to` and `order` are inputs
  (`studio:field`: a labelled `<input>` named by the node's pointer, with
  its messages from a refused write beside it); each prefix is a row of its
  key, as text, and its IRI, an input named `/prefixes/<key>` (the key
  escaped as a JSON Pointer token). One more row adds a prefix: its key and
  IRI inputs have no name until the author types a key, when the script
  names the IRI input `/prefixes/<key>`. A prefix cannot be removed here.
- **The rules** are an `<ol>` of `<li class="studio-rule"
  data-studio-rule-index="<i>">` rows, each the rule's selector and
  component inputs (`/rules/<i>/select`, `/rules/<i>/component`) and the
  runtime's array operations: Remove, Move up (not on the first) and Move
  down (not on the last), `data-scrud-array-op` buttons (see
  [Modes, editing and the write path](#modes-editing-and-the-write-path)).
  Under them are the counts (`<p data-studio-rule-counts role="status">`,
  hidden) and the append row, `/rules/-/select` and `/rules/-/component`
  with an "Add rule" submit. A selector the server refuses (a 400 with
  `name: "rules[<i>].select"`) re-renders the editor with the message
  beside rule `<i>`'s selector, which keeps the submitted text.
- **Saving.** Every input carries `data-studio-field`, and its `change` is
  stopped before the frame sees it, as the code editors' is: typing never
  saves. Save (and Add rule, and Enter in a field) is the frame's `PUT` of
  its `scrud-content` with every input applied, with `If-Match`. An array
  operation is an explicit click and saves at once: the runtime triggers its
  `change` on the frame itself, which the script lets through.
- **View mode** shows the fields and prefixes as text and each rule as
  `select → component` in a row that takes focus (`tabindex="0"`), with no
  input, no array operation and no Save.

**Marking a rule's matches** (`studio:rules`, the layer's second JS item).
Focus on a row (an input or button in it, or a view-mode row) submits the
live preview with `mark: {"stylesheet": <name>, "applies_to":
<applies_to>, "index": <i>}` and, in edit mode, `drafts: [{"kind":
"stylesheet", "content": <the form>}]`, the frame's content with its
inputs applied as its save would apply them (`ScrudUI.buildBody`), so
unsaved edits are marked too. On a published T, or in view mode, it sends
the mark only, of the stored stylesheet. Focus moving within a row keeps the
mark; to another row it marks that row's rule. Leaving the rows submits the
preview again without the mark, with the form as a draft only if it differs
from the stored stylesheet. Typing in a field sends the form again (300 ms
after the last key) with the row's mark, unless nothing changed. The index
is the rule's index in the stylesheet, which the server's markers use (see
**Markers** under `POST live-preview` in [The studio API](#the-studio-api)).

The preview's bridge answers each load with `scrud-preview:marks`. The
script accepts it only from the preview frame's own window (`event.source`)
and only with counts that are non-negative integers, and only while a row is
marked; it writes "matches N nodes, wins on M" (N the winning and losing
nodes together, M the winning ones) as text into the counts under the rows.
A new mark hides the counts until its preview answers; leaving the rows
hides them.

### The preview pane and the inspector

**The controls.** Above the preview, the workbench embeds
`api_url("ui-type-catalog", "all")` in the scope `studio studio-types`,
which the layer's stylesheet takes to `studio:type-picker`: a
`<select data-studio-control="type">` with "Choose a type" (empty) and one
option per type (`<title> (<slug>)`, its value the slug). Beside it are a
mode select (`data-studio-control="mode"`, `view` or `edit`), text inputs for
the data context, the scope chain and the instance's key
(`data-studio-control="context"`, `"scope"`, `"instance"`) and a "Pinned at
the branching point" checkbox (`data-studio-control="pinned"`). None has a
`name`. The script `studio:inspector` (the layer's third JS item) shows the
URL state in them on load (a component cannot read the query). A change of
one sets its key of the state (trimmed; empty removes it; `pinned` is `1`
when checked), clears `pointer` and the inspector (the node was the old
preview's), and submits the preview again; a new type also fetches the strip
again with `?type=`.

**The inspector** sits under the preview: `<section class="studio-inspector"
data-studio-inspector>` with `<div data-studio-explain aria-live="polite">`
and, on a working branch, `<div data-studio-new-rule hidden>` for the new
rule form. The script accepts a `message` only from the preview frame's own
window (`ScrudStudio.fromPreview`) and only `{"type":
"scrud-preview:select", "pointer": <string>, "href": ...}`; anything else is
ignored. The select names a node of the previewed resource when its `href`
is that resource's own frame: the sample's (the strip's `endpoints.sample`
with the type's slug) or, with an `instance`, the instance's detail (a path,
not an absolute URL, ending in `/<slug>/<instance>/`). Any other `href` is an embedded resource,
whose pointers are relative to it: the inspector says "Inside embedded
resource" and the `href` (or "Not inside the previewed resource." without one),
clears `pointer`, and does not ask `explain`. A node of the previewed
resource sets `pointer` in the URL state (the resource itself, `""`, is no
key) and is explained; so is the `pointer` of the URL on load.

The explanation is a `GET` of `endpoints.explain` for the state's type with
the preview's own parameters: `branch` (T, or `T@pinned` when `pinned`),
`mode` (default `view`), `scope` (space-separated), `context`, `instance`
(when set) and `pointer`, encoded with `URLSearchParams`, so a pointer with
`~0`, `~1` or any reserved character reaches the server exactly. It is sent
with `fetch` (`credentials: "same-origin"`, `Accept: application/json`, and
`Scrud-UI-Safe: 1` on a safe page; never `Scrud-UI-Branch`); only the latest
answer is shown. It is rendered as text only (`textContent`): the node's
pointer, its terms and scope chain, the verdict (the winning component and
the rule that chose it, a rule the user can't read, or the built-in
fallback), and every rule that matched, best first, as `<outcome>:
<selector> -> <component> (stylesheet <name>[ on <branch>][, applies to
<context>])`, or `<outcome>: a rule you can't read (rank [...])`. A refused
request shows the server's text (a problem's `title` and `detail`).

Under it, on a working branch T (none at all on a published T):

- **Edit this component**, when the winner is a rule the user can read, its
  component is not a built-in (`scrud:`), and the items tree lists it: it
  opens that tree entry in the editor, as a click on it does (`item` and
  `studio:open`), opening the editor pane if it was collapsed.
- **New rule for this node**, disabled while `pinned` is set (with a note: a
  new rule is made on the live branch). It asks `endpoints.options` for the
  node (the same parameters as `explain`) and shows the bootstrap studio's
  form, built as elements: Match (the options' `matches`, the first chosen;
  a broad label names the chosen data context), "Inside this type" (a
  checkbox, when `inside_type` is offered), Where (anywhere, or inside one of
  the node's scopes), Applies to (every context, or the preview's data
  context), the name it will have, and the Jinja template, prefilled with
  `<span>{{ value }}</span>`. Create `POST`s `endpoints.new_component` as JSON
  (`fetch`, `credentials: "same-origin"`, `X-CSRFToken` from the page's
  `csrf-token` meta tag, `Scrud-UI-Safe` on a safe page) with `branch` (T),
  `slug`, the preview's `mode`, `scope`, `context` and `instance`, the
  `pointer`, and the form's `match`, `inside_type`, `where`, `applies_to`
  and `template`. On 201 it says whether the new rule wins for the node, and
  the items, the strip, the preview and the explanation are fetched again; a
  refusal shows the server's text under the form.

### Promote and push records

Changes leave a working branch as everywhere else: by a push into an
ancestor (see [Pushing and restoring](#pushing-and-restoring)). The workbench
reviews a push before it is made, and shows the push records that result.

**Promote panels.** The strip has one "Promote to <target>…" control per
entry of T's `actions.promote` (the parent first, then the ancestors in
`chain`; none on a published T), each a `<div class="studio-promote"
data-studio-promote="<target>" data-studio-promote-src="<path>">` holding the
opener (`data-studio-promote-open`, `aria-expanded`) and its panel
(`data-studio-promote-panel`, hidden). `data-studio-promote-src` is T's push
preview, `in_mount(api_url("ui-push-previews", T))`. The panel holds:

- the review (`data-studio-promote-review`), empty at render: the push
  preview is not embedded (embeds are limited), but loaded when the panel
  opens, each time it opens;
- "Preview after push" (`data-studio-after-push`);
- Apply and Propose: the entry's `apply` and `propose` actions, as the
  strip's other [fixed-body](#writes-from-the-browser) buttons (`hx-post` of
  `ui-pushes` with the body the server gave, `hx-swap="none"`), labelled
  "Apply to <target>" and "Propose to <target>", each only when offered. Apply
  is rendered `disabled`. A user who may read the branches but not push gets
  the review alone;
- a reason (`data-studio-promote-reason`) and a note
  (`data-studio-promote-note`), hidden, for the script's text.

The script `studio:promote` (the layer's fourth JS item) opens and closes a
panel on its opener and loads the review: an htmx GET of
`data-studio-promote-src` with `?target=<target>` (URL-encoded), with
`Scrud-UI-Scope: studio studio-push`, into the review. The layer's stylesheet
takes the root of `UIPushPreview`
(`http://api.openteams.com/json-ld/UIPushPreview`) in the scope
`studio studio-push` to `studio:push-preview`, which renders `<div
class="studio-push-preview" data-studio-push-preview>` with the counts as
attributes (`data-studio-source`, `data-studio-target`,
`data-studio-changes`, `data-studio-conflicts`,
`data-studio-unreadable-changes`, `data-studio-unreadable-conflicts`), a
summary, "N changes / N conflicts on items you can't read" when either count
is set, the conflicts (`<li class="studio-push-conflict">`, the item and
that it changed on the target since the source's version was based on it),
and the changes (`<li class="studio-push-change"
data-studio-change-action="create|update|delete">`), each with the target's
row before and after as its fields, as text (a field the push changes is
`studio-push-field-changed`; "Nothing" before a create, "Deleted" after a
delete). See [What a push will change](#what-a-push-will-change).

Apply waits for the review ("Apply waits for the review to load."). Once it
has loaded, Apply is turned on when the review has no conflict: when
`data-studio-conflicts` or `data-studio-unreadable-conflicts` is more than
0, it stays off and the reason says how many conflicts the push has (and how
many are on items the user can't read): a push with any conflict is refused.
A review that could not be loaded (a 404 for a branch the user may not
`GET`, a 400 `ui-branch-invalid`) leaves Apply off, with the server's text.
Propose is never turned off: a proposal is reviewed again when it is
applied. An Apply or Propose that succeeds fetches the panes again (the
strip, and with it the panels, closed); one that is refused, for example
with 403 `ui-push-forbidden` for an item the user may not write in the
target, shows the server's text in the workbench's notice.

"Preview after push" submits the live preview with `after_push: {"source":
T}` and the target as `branch`, and no drafts (`ScrudStudio.submitPreview`),
and says "The preview shows <target> after this push." Without a type in the
URL state there is nothing to preview, and the panel says so.

**The pushes** are listed under the panels (`<details
class="studio-pushes">`), newest first, as `ui-branch-status`'s `pushes`
lists them: `<li class="studio-push-entry" data-studio-push-state="<state>">`
with a link, `<source> → <target>` (`restore` for a restore), to the push's
page, `in_mount(href)` with `?ui-scope=studio`, then its state and label.

**The push page** is the push's `ui-pushes` detail in the scope `studio`,
in the mode the user gets (no `ui-mode=view`: the state form needs edit
mode). The layer's stylesheet takes the root of `UIPush`
(`http://api.openteams.com/json-ld/UIPush`) in the scope `studio` to
`studio:push` (edit mode) or `studio:push-view` (view mode). Each renders
`<div class="studio-push" data-studio-push data-studio-mode="edit|view">`
with the push's source (`data-studio-push-source`; for a restore, "None (a
restore of version N)"), target, state, label and result ("Version N of
<target>", or "Not applied yet"), and, for a push with a source that is not
yet applied (it has no `result`), an embed of
`api_url("ui-push-previews", source)` with `?target=<target>` in the scope
`studio studio-push`: what it will change, as the panel shows it.

In edit mode (a user who may `PUT` the push), a push not yet applied also has
a state form in the frame's `<form>`: a text input named `/state`, with a
`<datalist>` of `proposed` and `applied`, and a Save button. Saving is the
frame's `PUT`, which applies the push when it moves it to `applied`. Only
Save (or Enter in the input) saves: the input carries
`data-studio-push-state-input`, and on a page with `[data-studio-push]` the
script `studio:promote` stops its `change` on the document (in the capture
phase) before the frame's form sees it, so choosing `applied` and leaving
the field writes nothing. A move the server refuses, such as one a
workflow policy forbids (Requiring approval, under
[Pushing and restoring](#pushing-and-restoring)), is re-rendered with the
server's message and the submitted state ([Refused writes](#refused-writes)).
An applied push is a record: no form, no preview. A user who may only read
the push gets the view: no form, no Save.

### History and restore

**History.** The strip lists T's `versions` (`ui-branch-status`'s latest 20
the user may read, newest first) under `<details class="studio-versions">`:
each `<li class="studio-version-entry" data-studio-version-number="<N>">`
links "Version N" to the version's page, `in_mount(href)` with
`?ui-scope=studio&ui-mode=view` (a version is read-only), opened in a new tab
(`target="_blank" rel="noopener"`) so the workbench stays open, and, when the entry
has a `restore` action, "Restore to version N": a
[fixed-body](#writes-from-the-browser) button like Pull and Apply (`hx-post`
of `in_mount(href)`, `data-scrud-body` the action's body, `data-scrud-etag=""`,
`hx-swap="none"`), with an `hx-confirm`. It is offered on a published T too,
to a user who may `POST` the restore in T's context: a restore is a push
record into T, as a promote is, and rolling back a publish is its point (see
[Pushing and restoring](#pushing-and-restoring): the restore records T's next
version, and no version is lost). After it succeeds the panes are fetched
again, as after any write from the workbench; a refusal shows the server's
text in the workbench's notice.

**A version's page** is its `ui-branch-versions` detail; the layer's
stylesheet takes the root of `UIBranchVersion`
(`http://api.openteams.com/json-ld/UIBranchVersion`) in the scope `studio` to
`studio:version`: `<div class="studio-version" data-studio-version
data-studio-branch="<T>" data-studio-number="<N>">`, the ancestors' versions
it was resolved against, and its items by kind (components, stylesheets, CSS,
JavaScript), each `<li class="studio-version-item"
data-studio-version-item="<name>">` with its template, CSS or JavaScript as
text (a stylesheet's rules as `select → component`), and "deleted on this
branch" for a deletion marker. Only the items the user may read are listed,
as the version's JSON lists them. It has no write control: its hint says to
restore it from the history of the workbench, which is still open in the
tab the link came from.

### Branch settings

The workbench has a "Branch settings" panel
(`<details data-studio-branch-panel>`) that embeds T's own resource,
`resource.href`, in the scope `studio studio-branch` (a different scope
chain from the workbench's, so no cycle), in the user's own mode, or in view
mode when T is published (`data-studio-embed="branch"`,
`data-studio-mode="view"` then). The layer's stylesheet takes the root of
`UIBranch` in that scope to `studio:branch-form` (edit mode, a user who may
`PUT` T) or `studio:branch-view` (view mode). Both render `<div
class="studio-branch-settings" data-studio-branch-settings
data-studio-mode="edit|view">` with T's name, context and parent. The form
has inputs for `label`, `description` and `base` (a select of `live` and
`pinned`) on a working T, and shows them as text on a published one (a
published T is read-only in the studio); the view shows them all as text.
Save is the frame's submit (`PUT`). As in the editors, a change of a field
saves nothing (the script `studio:studio` stops the `change` of anything in
the pane): only Save, or Enter in a text field, writes. The frame also has
the runtime's Delete button (with its confirmation) for a user who may
`DELETE` T: the API decides (a branch another names as its parent is a 409,
a published one needs the site's grant); after T is deleted from there, the
workbench is loaded again (and is then not found).

**The prefix** is an input only for a user who may publish: the form renders
it only in the scope `studio studio-branch studio-publish`, and the script
`studio:branch` asks for the pane in that scope (in the user's own mode, on a
published T too) when the strip's `data-studio-may-publish` is `true`, and
in `studio studio-branch` (view mode on a published T) when it is not, on load
and each time the strip is fetched again. Otherwise the prefix is text
(`data-studio-branch-prefix-text`), "None: a working branch" when there is
none. A scope only presents: a crafted request in the publisher's scope gets
the input, and its save is refused by the API (403, PUT on UI branches in the
context `""` is needed; see [Prefixes](#prefixes)), shown in the refused
re-render. A set prefix is rendered by `studio:branch-prefix` (the form puts
`prefix` in the scope `studio-branch-prefix`), so the API's refusals of a
prefix (`name: "prefix"`: malformed, taken, overlapping, under the API's URLs)
show beside the field, with `aria-invalid` and the message, in the re-render
of the submitted body. On a published T other than `main`, a publisher also
gets "Remove the prefix": a fixed-body `PUT` of T's content with `"prefix":
null` to T's own URL (so with the frame's `If-Match`), `hx-swap="none"`.

`studio:branch` also fetches the pane again after a write from the strip
succeeds (a pull, an Apply, a Propose: they change T's row and its ETag), and
not after other writes, so what was typed in it stays. When the strip, once
fetched again, says T is published and the workbench was not (or the
reverse), as after a save of its prefix, the script reloads the page: what
the workbench offers depends on it.

**New branch.** For a user who may `PUT` T (a best guess at who may create a
branch beside it; the server decides), the workbench has "New branch"
(`<details>`), a `<form data-studio-new-branch data-scrud-json="form">`
outside the settings' frame that `POST`s to `in_mount(api_url("ui-branches"))`
the inputs `/name` (empty), `/parent` (T) and `/rbac_context` (T's context),
with `data-scrud-etag=""` (no `If-Match`: it is not the frame's URL). The
context input has `data-scrud-type="string"`, which is not a text-like type,
so it is always sent, `""` included (`main`'s, the site's): an empty text
input is left out of a form body, and `rbac_context` is required. An empty
name or parent is left out (no parent: a root branch). It is
offered on a published T too: a branch under a published one is how work on
it starts. It sends `Scrud-UI-Scope: studio studio-new-branch` and
`Scrud-UI-Mode: view`, and its answer goes into `data-studio-new-branch-result`:
the new branch as `studio:branch-created` renders it (the layer's rule for
`UIBranch` in that scope), "Created branch <name>." and a link to its
workbench; a refusal (a taken name, a parent the user may not `GET`) shows
the server's notice there. On success the panes are fetched again, as after
any write from the workbench.

### Editing the studio

The studio is drawn from, bottom to top:

1. **the package**: the `scrud:` built-ins, the default stylesheet and the
   studio layer, which no stored item can break;
2. **the studio root** (`studio` in the demo) and the branches published
   under it: overrides of studio items live there, as any UI item does;
3. **S**, the branch the studio renders itself from;
4. **T**, the branch being edited and previewed.

**The studio's own tree.** Under the studio root's prefix a page links the
studio root's CSS and JS items and the layers' (`studio:studio`), never
`main`'s: the app's CSS and JS don't load under the studio's prefix. The
workbench there still lists, edits and previews any branch the user may
`GET`, in any tree, as T: the branch shown is data, not the page's
presentation. The layer sits under every root, so a UI-type page opened at
the API's URLs in the scope `studio` also renders with the studio's
components, but with `main`'s CSS and JS: use the studio under its own
prefix, where the [studio link](#ways-out) points. Studio changes are
promoted along the studio tree's chain, app changes along the app's.

**Changing the studio**, say the rule editor's view of a rule
(`studio:rule-row-view`):

1. Open the workbench of a working branch of the studio's tree (`studio-wip`
   in the demo) under the studio root's prefix. T is the working branch; S
   stays the published studio.
2. In Items, the layer's items are listed as packaged (or as the studio
   root's, where it overrides them). Open the item and choose "Override on
   this branch", then edit T's own row in the editor and save it.
3. Preview a resource the item renders: choose the type `ui-stylesheets`, the
   scope `studio studio-editor` (the editor pane's) and the pk of a
   stylesheet as the instance. The preview renders it as T would, with the
   override, and with the editor's unsaved text as a draft while you type.
   S is unchanged: the studio itself, the editor pane included, still renders
   from S, so a broken edit breaks only the preview (a component that fails
   to render falls back to the built-in there, after the usual `failed`
   comment).
4. To try the whole studio on T, browse it as any working branch: add
   `ui-branch=<T>` to a workbench URL under the studio root's prefix, which
   asks you to confirm the switch (see [Browsing](#browsing)). The page then
   renders on T, under the branch banner.
5. Promote T into the studio root ("Promote to studio…", review, "Apply to
   studio"; see [Promote and push records](#promote-and-push-records)). From
   then on the studio under its prefix uses the change, for every branch it
   shows.

`tests/test_studio_self_editing.py` follows this path.

### Ways out

A change that reaches S, the published studio, can break the studio itself:
a page component that fails, a workbench with nothing in it. The anti-goal is
an author locked out of the studio with no way back but the database or
hand-made API calls, so every studio page keeps three ways out that no stored
item can remove.

**The studio links.** When the studio app is installed, every page of the
seven UI types (`ui-components`, `ui-stylesheets`, `ui-branches`,
`ui-branch-versions`, `ui-pushes`, `ui-css`, `ui-js`, lists and details) and of
the studio's computed types carries, after the banner and the safe mode
notice:

```html
<nav class="scrud-studio-links">
  <a href="/studio-root/ui-branches/3/?ui-scope=studio&amp;ui-mode=view">Studio</a>
  <a href="/api/ui-components/9/?ui-safe=1">Safe mode</a>
  <a href="/studio/">Bootstrap studio</a>
</nav>
```

(on one line, without the whitespace). *Studio* links to the studio root's
workbench; it is left out when `SCRUD_UI_STUDIO_BRANCH` is unset, names no
stored branch, or names a branch with no prefix. *Safe mode* is the current
URL with `ui-safe=1` (its other parameters kept). *Bootstrap studio* is the
bootstrap studio's types page (`scrud-studio:types`), left out when the project
does not include the studio's URLs. The links are server chrome, like the
banner: a page component that leaves them out is replaced by the built-in page,
as is one that fails to render (after the comment `<!-- scrud-ui: component
"studio:page" failed -->`). A fragment (an htmx request) has no links. As with
the banner, a branch's CSS can still hide them (follow-up
[ui-banner-hideable](follow-ups.md#ui-banner-hideable)).

**Safe mode** (see [Safe mode](#safe-mode)). The *Safe mode* link renders the
same workbench from the package only: the layer's `studio:page`,
`studio:workbench` and panes, with the safe mode notice, whatever S stores.
The page's own requests stay safe: `scrud-ui.js` sends `Scrud-UI-Safe: 1` on
the htmx requests of a safe page (a pane fetched again, an item opened in the
editor, a promote review), and the layer's scripts send it on their `fetch`
requests (`explain`, the new rule). The live preview is not in safe mode: it
shows T as T renders, which is what it is for. Every write from a safe page is
the same request as from a normal one, with the same checks.

**The bootstrap studio** at its mount (`/studio/` in the demo) is package
templates and static files, not UI items, so nothing stored changes it. It
writes working branches only: on a published branch, the studio root
included, its saves and reverts are refused with 400
`ui-studio-branch-read-only` (changes reach a published branch by a push).

**Undoing a break on S.** Each of these is a click in a page that the break
cannot reach:

- **Restore S** from its own workbench in safe mode: open the *Studio* link's
  page (S's workbench), follow *Safe mode*, and in the strip's history choose
  "Restore to version N" for the version before the break (see
  [History and restore](#history-and-restore)). The restore is a push record
  into S that makes S's own items those of version N again (an override the
  break added is deleted), and history is kept. It needs `POST` on
  `ui-pushes` and the item writes in S's context.
- **Delete the override** from the items' own pages. The UI items'
  resources under S's prefix are not in the scope `studio`, so they render
  with the runtime's page, which the break does not touch (add `?ui-safe=1`
  if S's other items break it too): the list (`/workbench/ui-components/` in
  the demo) shows each of S's components as its own frame, and the item's
  page (`<prefix>ui-components/<pk>/`) shows it, each with the frame's Delete
  button for a user who may `DELETE` it, which sends the frame's `If-Match`.
  Deleting the override shows the packaged item again. This works when S has
  no version from before the break, as when the break was S's first change.
- **Repair it through a working branch**, in the bootstrap studio (or the
  workbench, in safe mode): on `studio-wip`, save the item with content that
  works (the packaged file's, under `scrud_django/ui/studio/layer/component/`,
  is a start), then push `studio-wip` into the studio root. S then has a
  working override rather than none.

`tests/test_studio_self_editing.py` breaks `studio:page`, `studio:workbench`
and `studio:items` on S and takes each of these ways back.

## Security

* **Viewing a branch means trusting everyone who can write to it.** There is no
  separate trusted role: writes are authorized by scoped RBAC in the context of
  the branch (see [Authorizing contexts](#authorizing-contexts)), so trust is
  per RBAC context. Whoever may write components, CSS or JavaScript on a branch controls
  the markup of every page that shows it. A component can carry client
  scripts, and they run in the page with the viewer's rights. A user with write
  grants in a personal branch's context could send a link to a colleague with
  broader rights. Untrusted authors are phase 3.
* The mitigations: a viewer sees only the branches they may GET; the banner
  always names a working branch, and the server renders it outside stored
  components; a link that would switch the viewer to a working branch asks first
  (confirmation, with a token only the server can issue for that viewer, which
  needs `CsrfViewMiddleware`); and the CSP below blocks inline scripts. The
  confirmation is a click on a page the server renders, so it holds only while
  other sites cannot frame that page and steer the click (clickjacking): keep
  the CSP's `frame-ancestors` (below), or Django's `XFrameOptionsMiddleware`
  (`X-Frame-Options: DENY` by default). `frame-ancestors 'self'` still lets
  the site's own pages, a branch's components among them, frame it; `'none'`
  (or `DENY`) does not. The
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
compact sidebar list, and a page with a layout), and four branches: the
published `admin` branch at `/manage/` (with an extra component and its own CSS),
the published `customer` branch at `/todo/` (with its own CSS), and a working
branch `testing-admin` over `admin`, and a working branch `alice-wip` over
`testing-admin` for the studio. It also seeds the self-hosted studio's tree: the
published root `studio` at `/workbench/` (context `ui-studio`, named by
`SCRUD_UI_STUDIO_BRANCH = "studio"` in the demo's settings) and its working
branch `studio-wip` (context `ui-studio-wip`). The demo adds
`PublishedBranchMiddleware` to its settings. The admin prefix is `/manage/`, not
`/admin/resources/`, because Django's admin has a catch-all route under
`/admin/`, which a prefix there would hide.

```bash
cd demo
uv run python manage.py migrate
uv run python manage.py seed_todo_demo
uv run python manage.py runserver
```

Sign in at `/api-auth/login/` and open `/api/todo-lists/` with a browser (or
use HTTP Basic auth). The users are `owner`/`owner` (reads and edits the list),
`reader`/`reader` (reads it) and `author`/`author` (writes the UI components
and stylesheets); these passwords are for the demo only. `author` writes the UI
in the four branches' contexts (`ui-admin`, `ui-customer`, `ui-testing`,
`ui-alice`) and may open both prefixes. For the studio's tree, `author` may GET
the `studio` root, write the UI items and `ui-pushes` in `ui-studio-wip`, and
push into `studio` (it holds `ui-pushes` and the item writes a push applies in
`ui-studio`: applying a push needs the grants a direct write would, so it can
also write items there directly); `owner` holds PUT on `ui-branches` in `""`,
the grant that publishing a prefix needs; `reader` may open `/todo/` (it may GET
the `customer` branch) and gets a 403 at `/manage/`; `owner` may open `/todo/`
too. To try the flow: as `author`, write a component on `testing-admin` (a POST
of a `ui-components` item with `"branch": "testing-admin"` and
`"rbac_context": "ui-testing"`), browse
`/manage/todo-lists/<id>/?ui-branch=testing-admin` (confirm the switch), then
POST a `ui-pushes` item with `source` `testing-admin`, `target` `admin`,
`rbac_context` `ui-admin` and `state` `applied`.

The command prints the self-hosted studio's workbench URLs, as `author`:
`/workbench/ui-branches/<pk>/?ui-scope=studio&ui-mode=view`, for `alice-wip`
and for `studio-wip` (see [Self-hosted studio](#self-hosted-studio); to edit
the studio itself, work on `studio-wip` and push it to `studio`).

The demo also includes the studio at `/studio/`. Sign in at `/api-auth/login/` as
`author`, open `/studio/types/todo-lists/`, pick `alice-wip`, click an item in the
preview, write a "New component for this node" (choose the `row` scope under
"where", so the rule is as specific as the existing `row` rules), then push to
`testing-admin` and browse `/manage/todo-lists/<id>/?ui-branch=testing-admin`.

Known limitations of phase 1 are recorded in the [follow-ups](follow-ups.md),
under the `ui-` names.
