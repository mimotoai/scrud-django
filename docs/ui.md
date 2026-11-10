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

This is phase 1 of the UI. The later phases are planned, not built:

* **Phase 1** (this release): server-rendered HTML for every resource, stored
  components and stylesheets written by a trusted role, and the browser writing
  whole resources with plain JSON `PUT`s.
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

`scrud_django.urls` then serves two more resource types, each with its own
schema, JSON-LD context and collection, written and read like any other
resource under the same policy:

| Resource type | IRI | Slug |
|---|---|---|
| UI component | `http://api.openteams.com/json-ld/UIComponent` | `ui-components` |
| UI stylesheet | `http://api.openteams.com/json-ld/UIStylesheet` | `ui-stylesheets` |

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
| `rbac_context` | no | Only `""`: components are stored only in the default context. |

Component authors are a trusted role, and components are global only (context
`""`); whoever may write the `UIComponent` type is trusted with the rendering of
every page. Stored Jinja templates run in `jinja2.sandbox.SandboxedEnvironment`.
Stored Django templates run on a standalone engine with no loaders and no tag
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
makes a file immutable (`Cache-Control: public, max-age=31536000, immutable`;
`private` on a working branch): after an edit the old URL is a 404 and the page
links the new one. Anyone who can see a page can fetch its files; under a
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

`name` is unique within its `rbac_context`; `order` (default 0) orders
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

## Security

* Components are written by a trusted, global-only role: anyone who may write
  `UIComponent` resources controls the markup of every page. A component can
  carry client scripts, and they run in the page with the viewer's rights.
  Untrusted authors are phase 3.
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
compact sidebar list, and a page with a layout):

```bash
cd demo
uv run python manage.py migrate
uv run python manage.py seed_todo_demo
uv run python manage.py runserver
```

Sign in at `/api-auth/login/` and open `/api/todo-lists/` with a browser. The
users are `owner`/`owner` (reads and edits the list), `reader`/`reader` (reads
it) and `author`/`author` (writes the UI components and stylesheets); these
passwords are for the demo only.

Known limitations of phase 1 are recorded in the [follow-ups](follow-ups.md),
under the `ui-` names.
