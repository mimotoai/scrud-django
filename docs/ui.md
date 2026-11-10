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
`scrud:collection`. Scopes work as for any rule, so `sidebar scrud:collection`
restyles lists under `?ui-scope=sidebar` only. The component gets the
`scrud:collection` context plus `href` (the list's URL) and `scope` (the scope
chain as a list). With no matching rule, or if the stored component fails to
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
  stylesheet, `applies_to` `""`). On a child branch the override's `base` is
  the layer item's ETag, the sha256 hex of the file's content as canonical
  JSON (sorted keys, no spaces), when no ancestor overrides it. Deleting the
  override shows the packaged item again.
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
- **Checks.** A layer declaration that is not valid, or a layer file that is
  unreadable or not a JSON object, whose name lacks the layer's prefix or is
  not a valid name, that has a branch field or another file's name, a
  stylesheet with a non-empty `applies_to`, or a CSS or JS item whose text has
  `</style` or `</script`, is reported by the system check
  `scrud_django_ui.E002` (`manage.py check`) and is left out at runtime.

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
* In edit mode the built-in `array-edit` gives every item of every array a
  **Remove** button, and **Move up** and **Move down** buttons (none for the
  first item's Move up or the last item's Move down). They are
  `<button type="button" data-scrud-array-op="remove|move"
  data-scrud-pointer="/rules/1" data-scrud-to="0">`; `data-scrud-to` is the
  index a move puts the item at. View mode renders no controls. A click builds the
  frame's body as a change does (the inputs applied at their current pointers),
  applies the operation (`ScrudUI.applyArrayOp(body, op)`), and sends the
  frame's own `PUT` with `If-Match`: a stale page gets the 412 reload, and the
  list is never silently overwritten. The page shows the saved array after the
  response. A custom array component gets the same behaviour by rendering such
  buttons inside the frame.

Custom components that want to be editable need only render inputs named by
their JSON Pointer inside the frame.

### Refused writes

A 400 or 403 PUT re-renders the submitted body (never the stored content) in
edit mode with the messages, provided the user may read the stored resource
(as JSON would let them) and may PUT it; any other user, including one who may
PUT but not GET, gets the notices only: no form, no ETag, no content. Its decisions, contexts and ETag come from
the stored resource, never from the request. Policy condition messages are
shown only to a user who may read the stored resource, and only in HTML; the
JSON error bodies are unchanged. A stylesheet whose rule has a selector that does not parse is a 400 problem
with `name: "rules[<i>].select"` (the field, as JSON Schema's errors name
theirs), so the re-render shows the message beside rule `<i>`. A 412 (PUT or DELETE) shows a notice and reloads
the frame with the current state, and so does a 409 DELETE.

## Fragment protocol

| Header | Meaning |
|---|---|
| `HX-Request: true` | Set by htmx: answer with a fragment (the frame) instead of a page. |
| `Scrud-UI-Scope` | The root scope chain, outermost first: up to 16 space- or comma-separated scope names. An invalid value is ignored. The same chain may come in the URL as `?ui-scope=sidebar,main`; the header wins. |
| `Scrud-UI-Mode` | `view` renders the resource (or each item of a list) read-only even for a user who may PUT it: no form, no `hx-put`. It only lowers what the user may do: `view` is the only valid value, any other (`edit`) is a 400, before a PUT writes anything. The same may come as `?ui-mode=view`; the header wins. |
| `Scrud-UI-Pointer` | The JSON Pointer of the node to render (default `""`). An invalid pointer (or one over 1024 characters) is a 400, before a PUT writes anything. |

These select presentation only, never what is authorized. HTML responses carry
`Vary: Accept, HX-Request, Scrud-UI-Scope, Scrud-UI-Pointer, Scrud-UI-Mode,
Scrud-UI-Branch, Cookie` (the branch a page browses comes from a header or a cookie) and
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
  item it replaces (`null` for an addition), as the branch shows it: on a
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
  selector, component or stylesheet name. A component the user may not read
  has no "inside this type" option. The scope chain of a node is shown in full,
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

The studio page is built on a JSON API, and a later self-hosted studio would
use the same endpoints. Paths are relative to where you include the URLs.
A studio write calls the resource viewsets in-process (`call_view`), not
through a new HTTP request, so the site's middleware does not run for that
inner call: a middleware-only check on the API's URLs (an IP allowlist on
`/api/`, say) does not cover the studio's writes. The studio's own URL passes
through the middleware as any request does; put such a check on it too, or
leave the studio out of the deployment that needs it.
Every endpoint needs an authenticated user (401 otherwise). The writes are
`POST` with a JSON object body (a body that is not an object is a 400) and are
protected by CSRF when a session is used. A success from a write also carries
`HX-Trigger: scrud-studio-saved`. Errors are problem documents (the API's
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
| `ui-studio-branch-read-only` | 400 | a write names a published branch (`main` or one with a prefix); refused before anything is dispatched |
| `ui-studio-context-unavailable` | 400 | a sample or preview in a data context whose UI stylesheets the user may not read |
| `ui-pointer-not-found` | 400 | `explain` (or the options) for a pointer to no node the preview renders |

The problem `type` is `https://api.openteams.com/http-problem/<name>`.

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
`testing-admin` for the studio. The demo adds
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
`ui-alice`) and may open both prefixes; `reader` may open `/todo/` (it may GET
the `customer` branch) and gets a 403 at `/manage/`; `owner` may open `/todo/`
too. To try the flow: as `author`, write a component on `testing-admin` (a POST
of a `ui-components` item with `"branch": "testing-admin"` and
`"rbac_context": "ui-testing"`), browse
`/manage/todo-lists/<id>/?ui-branch=testing-admin` (confirm the switch), then
POST a `ui-pushes` item with `source` `testing-admin`, `target` `admin`,
`rbac_context` `ui-admin` and `state` `applied`.

The demo also includes the studio at `/studio/`. Sign in at `/api-auth/login/` as
`author`, open `/studio/types/todo-lists/`, pick `alice-wip`, click an item in the
preview, write a "New component for this node" (choose the `row` scope under
"where", so the rule is as specific as the existing `row` rules), then push to
`testing-admin` and browse `/manage/todo-lists/<id>/?ui-branch=testing-admin`.

Known limitations of phase 1 are recorded in the [follow-ups](follow-ups.md),
under the `ui-` names.
