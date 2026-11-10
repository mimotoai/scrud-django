# Self-hosted studio — design

Date: 2026-11-10
Status: builder-checked; awaiting owner approval
Builds on:
- `scrud_django.ui` phase 1 (MR !8; `docs/ui.md`);
- UI branches (MR !10; `docs/superpowers/specs/2026-11-09-ui-branches-design.md`);
- the bootstrap studio (MR !11; `docs/superpowers/specs/2026-11-10-ui-studio-design.md`).

Notation: **(owner)** marks a decision the owner made. **(call)** marks a decision
made while writing this, which the owner can overrule. Anything unmarked follows
from the existing code or the earlier designs.

## Intent

The bootstrap studio spec promised "a basic **bootstrap studio** … which works
on any site whatever its branches hold" and, later, "a **self-hosted studio**,
built from stored components and stylesheets on a branch and edited in the
studio itself". This is that later project.

The self-hosted studio is a second studio whose screens are UI components
rendered by the scrud runtime: the same templates, stylesheets, cascade and
branches that style every other app. The bootstrap studio stays unchanged, as
the way in and the way to repair things **(owner)**.

**The studio is a forcing function for the runtime (owner).** When the studio
needs something the runtime can't do, grow the runtime with a general feature
every scrud app gets, where that is reasonable. Only when a general feature
isn't reasonable does the studio get a special-purpose endpoint **(owner:
"bias toward growing the runtime, every app should get these features where
reasonable, otherwise give the studio its own special-purpose endpoints")**.
The runtime features are a larger lasting payoff than the studio's screens
(call; the owner did not correct this).

The person using it is the author role, as in the bootstrap studio: someone
who styles resource types on branches and publishes the result.

### What the runtime can't do today

Found while reading the code; each limit below is addressed in "Runtime
growth":
- HTML exists only at a resource or list URL. A component sees only its own
  node's data; the page component gets none. There are no screens that aren't
  a resource. (R8: computed resource types.)
- Templates can't include each other. Panels compose only client-side, with
  `hx-get` embeds, which carry no login inside a sandboxed preview. (R9:
  server-side embeds.)
- A viewer who may PUT always gets edit mode. (R10: a requested view mode.)
- The browser sends JSON only as a PUT of its own frame's resource
  (`scrud-json` in `scrud-ui.js`). Anything else is form-encoded, which scrud
  refuses with 415, so the browser can't create, push or delete with a body.
- No JavaScript can be stored as data, and the recommended CSP
  (`script-src 'self'`) blocks inline scripts.
- A scope chain comes only from the `Scrud-UI-Scope` header, so a plain link
  can't open a view of a resource.
- List chrome is hard-wired to `scrud:collection` (`render.py`
  `render_collection`), not resolved through the cascade.
- Editing arrays of objects is weak: append rows were recently withdrawn for
  some object arrays (commits `f613adb`, `d2adc2d`).
- The bootstrap studio's catalog, branch list and strip data exist only as
  server-rendered HTML. (Studio data as computed resources.)

## Goals

1. **Parity.** Everything the bootstrap studio does, the self-hosted studio
   does (call; the owner did not correct "success is parity plus editing the
   studio in itself"): find a type, preview a sample or a real instance with
   the same controls, inspect a node, edit a Jinja template, make a new
   component for a node, edit CSS, revert to inherited, pull, push, and browse
   the app on a branch.
2. **The workbench layout (owner).** Three panes, the navigator and the editor
   each collapsible to a rail (owner: "having the tree collapsible gives us
   both A and B"):
   - **Top bar:** branch picker; Items / Editor / Preview toggles. The preview
     can't be hidden.
   - **Items (left):** the branch's effective items as a tree: types,
     stylesheets, components, CSS, JS. Own items (this branch's overrides) are
     marked apart from inherited ones (call).
   - **Editor (middle):** a component's template, a stylesheet's rules, a CSS
     or JS item.
   - **Preview (right):** a type's sample or a real instance, with the
     bootstrap studio's controls (mode, data context, scope chain, live or
     pinned, sample or instance). The inspector sits under the preview
     (call); clicking a node offers "edit this component" and "new rule for
     this node".
   - **Branch strip (bottom):** changed items, conflicts, pull, push.
   - All panes open is the "workbench"; Items collapsed gives editor | preview;
     Items and Editor collapsed gives the bootstrap studio's preview-first
     inspector, and choosing "edit" on a node reopens the editor.
   - Which panes are collapsed is remembered per viewer in browser storage,
     not stored as data (call).
   - Mockups: `.superpowers/brainstorm/` (`layout-v2.html`), not committed.
3. **Live preview while typing (owner).** The preview re-renders as the
   author types in a template, rule or CSS/JS item, without saving. Saving
   stays an explicit action.
4. **The rule editor (owner).** A stylesheet opens as its rules: an editable
   list of selector → component rows that can be added, removed and
   reordered. Focusing a row marks, in the preview, the nodes the rule matches,
   showing where it wins and where it matches but loses (owner: "yes, that's
   the picture"). Because the editor is the stylesheet's own edit page (see
   "Where the studio lives"), its `order`, `prefixes` and `applies_to` are
   editable beside the rules (call; the bootstrap studio left these out).
5. **Authors ship real JavaScript (owner).** A new `ui-js` item type; see
   "Runtime growth".
6. **Publishing (owner).** In priority order, **promote to the live site first
   (owner)**:
   1. promote: push a working branch up its chain to a published branch,
      seeing what will change first;
   2. pushes as reviewable records: propose a push, review it, apply it;
   3. roll back: a branch's version history and "restore to version N";
   4. put a presentation online: create a branch, set, change or remove its
      URL prefix.
7. **The studio edits itself, safely.** An author can change how the studio
   looks or behaves on a working branch, try it, and push it, without being
   able to lock themselves out of the studio (see "Self-editing").

## Non-goals (first version)

- Changing the bootstrap studio. It stays as built in MR !11 (owner: "retain
  the original studio as a bootstrap and a fallback"). The builder may move
  its views onto the new JSON endpoints if that is free of behaviour change,
  but doesn't have to.
- Editing Django-engine templates. The self-hosted studio creates and edits
  Jinja only and shows Django-engine components read-only, as the bootstrap
  studio does (call; owner kept it out by choosing only publishing).
- Deletion markers through the UI (call, as above).
- A code editor with syntax highlighting or completion. A textarea plus live
  preview is the first version (call, as above).
- Writing workflow policies. Push states that a site's policy adds are shown
  and can be moved through, but policies stay in the API (call, as above).
- Showing which workflow transitions a viewer may take before they try. A
  refused transition shows the server's error (call; exposing allowed
  transitions to templates is a reasonable later runtime feature).
- Untrusted authors (phase 3 of `docs/ui.md`). Whoever can write `ui-js` on a
  branch runs code for that branch's viewers; see "Security".
- A preview that runs live data requests as the author (see "Preview").

## Anti-goals

- **A change made in the self-hosted studio locks its author out with no route
  back but the database or hand-made API calls.** Safe mode and the bootstrap
  studio must always work (call; owner approved the safety model: "Looks
  good").
- **A second authorization path.** The studio asks the existing checks what a
  user may do and adds no rights. Every write is an ordinary resource write.
- **A preview writes data.** Drafts and samples are never stored; a preview in
  edit mode never submits; a real instance is only read.
- **Showing what the user may not GET as JSON.** The bootstrap studio's rules
  carry over, including "a rule you can't read" with rank parts only.
- **A studio-only escape hatch where a general runtime feature was
  reasonable** (owner's bias). Each special-purpose endpoint below says why
  it isn't general.
- **The packaged studio layer changing a site that didn't ask for it.** It is
  present only when `scrud_django.ui.studio` is installed.
- **`ui-js` weakening the CSP.** Scripts are served from same-origin versioned
  URLs; the recommended `script-src 'self'` stays as it is.

## Where the studio lives (call; owner: "keep going")

**The studio is the UI types, styled well.** Opening a branch's own URL,
`/api/ui-branches/<pk>/`, in a browser renders the workbench. A stylesheet's
URL renders the rule editor; a component's URL, the template editor; a CSS or
JS item, its text editor; a push, the push review; a branch version, the
version page. These are ordinary resource renders: the packaged studio layer
(below) holds the rules and components that style `UIBranch`,
`UIStylesheet`, `UIComponent`, `UICss`, `UIJs`, `UIPush` and
`UIBranchVersion`, and the studio's computed resource types (below). The
workbench's panes are those resources, composed on the server with embeds
(R9); `hx-get` is used only to refresh a pane after a change. No new routes
are needed for screens.

- **Two branches at once.** **S** is the UI branch selected for the request
  (cookie, header, query or published prefix, as today): it decides how the
  studio looks. **T** is the branch whose data is shown: the branch resource
  being rendered, and the branch named in the URL of each embedded studio
  resource (for example a branch's effective items). T is always explicit in
  a resource URL, so no new branch-selection mechanism is needed and the
  selection, cookie and confirmation code is unchanged. Normally S is the
  published studio and T is the author's working branch. To edit the studio,
  T is a working branch in the studio's tree that holds studio overrides, and
  S stays on the published studio.
- **The studio is its own published tree (owner: "Yes" to the
  recommendation).** CSS and JS items apply to every page of a branch, so the
  studio doesn't share `main`'s tree: the app's CSS would restyle it and
  `main`'s `ui-js` would run on studio pages under the session of people who
  can publish. A site publishes a separate root branch for the studio (the
  demo's is `studio` at `/workbench/`, call), with its own working branches
  (say `studio-wip`). The workbench there still lists, edits and previews any
  branch the user may GET, in any tree, as T. Studio changes are promoted
  along the studio tree's chain, app changes along the app's.
  - The studio layer (R1) sits under every root, so a UI-type resource opened
    under `/api/` also renders with the studio's components, but with
    `main`'s CSS and JS. `docs/ui.md` says to use the studio under its own
    tree; the server-rendered studio link (see "Self-editing") points there
    when the site names its studio branch (builder's choice how: a setting is
    fine).
- **Published branches stay read-only in the studio (owner, from the
  bootstrap studio spec: changes go working branch → push).** When T is
  published, the workbench embeds item editors in view mode (R10) and offers
  no save, revert or new-component actions. This is the studio's flow, not a
  new authorization rule: the API still allows a direct write by anyone with
  the grants, as today.
- **Inherited and packaged items.** An item T inherits opens read-only as the
  ancestor's row (or, for a packaged item, the studio's computed view of it),
  with "override on this branch", which POSTs an override with the server
  setting `base`, as the bootstrap studio does. Saves after that are PUTs of
  T's own row.
- **Workbench state** (which type is previewed, which item is open, which node
  is selected, the preview controls) lives in the workbench URL's query, so a
  link reproduces the view (call).
- **Mounting.** A site publishes a branch at a prefix such as `/workbench/`,
  so `/workbench/ui-branches/<pk>/` is the friendly URL. This is the existing
  published-branch mechanism. The demo gets such a branch (call).
- **Opting in.** The packaged studio layer is part of
  `scrud_django.ui.studio`, the app that already holds the bootstrap studio
  (call). Without it, UI types render plainly as today.

## Runtime growth (general features, every app gets them)

Each feature is documented in `docs/ui.md` as a runtime feature, not as part
of the studio.

### R1. Packaged, overridable UI layers

An installed app can ship a **UI layer**: components, stylesheets, CSS and JS
items as package files, read at runtime like the `scrud:*` built-ins and the
default stylesheet are today (`cascade.py` `builtin_component`,
`_default_stylesheet`).
- **Overridable.** Unlike the reserved `scrud:*` names, a branch may override
  a layer item by name, like any inherited item. The override records the
  layer item's ETag (a hash of its packaged content) as its `base`. Deleting
  the override reverts to the packaged item.
- **Layer conflicts (call).** A library upgrade can change an item a branch
  overrides. Branch conflicts today are recorded only by a pull, and roots
  (`main`, the studio root) have no parent to pull from, so layer conflicts
  are computed live instead: an override whose `base` differs from the
  installed layer item's ETag is in conflict with the layer. They appear in
  the branch's effective items (a computed resource) and its strip, and a
  database system check reports them, like `scrud_django_ui.W001`. They are
  resolved by re-saving the override (the server moves its `base` to the
  current layer ETag) or reverting it. The `conflicts` field and pull keep
  their current meaning.
- **Not in resource lists (call).** Layer items aren't rows and don't appear
  in `/api/ui-components/` and the other item lists or details, so the JSON
  list contract, pagination and RBAC list filtering don't change. They appear
  in the studio's computed "effective items of a branch" resource, marked
  packaged, with their content.
- **Names.** Layer items' names carry the layer's prefix (the studio's is
  `studio:`, call). Today's name patterns (`UIComponent-schema.json`,
  `UIStylesheet-schema.json`, `UICss-schema.json`) refuse `:`; they change so
  a row may use a layer's prefix only to override an existing layer item of
  that kind. A new item may not use a layer prefix, and `scrud:` stays
  reserved outright.
- **Where it sits.** Below every root branch (`main` and any parentless
  published branch, such as the studio root), above the `scrud:*`
  built-ins. Its stylesheets rank after the default stylesheet and before the
  context `""` stylesheets (call). Several layers stack in `INSTALLED_APPS`
  order (call).
- **Versions.** Layer items aren't rows, so branch versions don't record them.
  A pinned branch sees the installed package's layer (call; documented).
- **How an app declares one:** builder's choice (an `AppConfig` attribute, a
  setting, or a registry call).

### R2. JavaScript items (`ui-js`) (owner)

A new item type shaped like `ui-css`: `name`, `js`, `order`, plus the branch
fields (`branch`, `rbac_context`, `deleted`, `base`).
- Served at versioned, immutable, privately cached same-origin URLs, like CSS:
  `ui-js-files/<name>/<etag>.js`, and under a published branch's prefix. The
  same gating as CSS: GET on the branch.
- The page component gets a `scripts` list beside `css`, in `order`, and links
  them. Whether they load as modules is the builder's choice.
- **The grant is the gate.** It is its own resource type, so a site grants
  write on `ui-js` separately from `ui-components`. No new permission logic.
- Branch machinery (effective items, versions, push, pull, restore,
  conflicts, revert) covers `ui-js` exactly as it covers `ui-css`.
- Packaged layers (R1) may ship JS items.

### R3. The browser sends JSON for any method

`scrud-ui.js` grows so a frame or element can send a JSON body with POST,
PUT or DELETE, not only a PUT of its own resource:
- **create:** POST to a collection, the body built from inputs named by JSON
  Pointer, as the PUT body is today;
- **a fixed body:** an element declares a JSON body (for example a push:
  `{source, target, state, rbac_context}`) and the method and URL to send it
  to.

It keeps sending `If-Match` where an ETag is known, the CSRF token and the UI
headers. The exact attributes are the builder's choice.

### R4. A scope chain in the URL

`?ui-scope=a,b` sets the root scope chain, as `Scrud-UI-Scope` does, so a
plain link can open a view of a resource. The header wins if both are present
(call). Same limits and validation as the header.

### R5. List chrome through the cascade

The collection wrapper is resolved through stylesheets instead of always
using `scrud:collection`, so a rule (with scopes) can restyle a type's lists.
The term it matches on is the builder's choice; with no matching rule the
built-in is used, so existing sites don't change.

### R6. Editing arrays of objects

In edit mode, arrays of objects support adding, removing and reordering
items, and validation errors appear next to the item and field they belong
to. The rule editor is the first user; any type with an array of objects
benefits.

### R7. Safe mode

A request flag (`?ui-safe=1`, call) renders using only packaged layers,
built-ins and the default stylesheet, ignoring every branch's stored items. It
reveals nothing new (package files are public) and changes no authorization.
General: any site can use it to look at a resource when a branch breaks
rendering. A page rendered in safe mode says so in the server-rendered banner.

### R8. Computed resource types (owner: "Yes")

A resource type whose content Python computes on GET, instead of reading a
stored row. It has a JSON Schema and JSON-LD like any registered type, is
read-only (GET only; other methods 405), and renders through the cascade like
any resource, so a branch can style it.
- **Authorization** stays with scoped RBAC: the computing code is given the
  request and may return only what the user may GET (the type decides how,
  typically by asking the same checks the underlying resources use). A
  computed type adds no rights.
- **Addressing.** Its URL carries its parameters (for example
  `<slug>/<branch>/` or a query), so a computed resource can name T
  explicitly.
- **Conditional requests.** It has an ETag (a hash of the computed content is
  enough) so scrudful's `If-None-Match` works; no `If-Match` (read-only).
- **Lists** are optional per type.
- How an app declares one is the builder's choice, alongside
  `registration.json_resource_type`. Any app can define its own (a dashboard
  or a report, say).

### R9. Server-side embeds (owner: "Yes")

A component can embed another resource URL, rendered on the server in a given
scope chain (and optionally view mode, R10), with the viewer's own
authorization, and get the result as HTML in its context.
- The embed is resolved by an in-process request as the same user (as the
  bootstrap studio's `call_view` does), so the embedded resource's view,
  RBAC, workflow and masking apply unchanged. A refused embed renders the
  same notice the resource's own URL would.
- Only same-site resource URLs (resource and computed-type routes) may be
  embedded; anything else is refused when the component is saved if it is
  literal, and at render otherwise (call).
- Limits: a depth limit and a per-response count limit, and cycles are
  refused (values the builder's choice, documented).
- The UI branch (S) of the embedding request applies to the embed.
- Inside the sandboxed preview, embeds still work: the server composes the
  page with the author's login before the sandbox sees it.
- The template syntax is the builder's choice. The embed names a URL, which
  may come from the node's data (for example `resource.href` of a link), so
  the demo's sidebar can move from `hx-get` to an embed.

### R10. A requested view mode

`?ui-mode=view` (and the equivalent header, call) renders a resource in view
mode even for a viewer who may PUT. It can only downgrade: it never grants
edit mode. Embeds (R9) can pass it. The studio uses it for items on a
published T and for inherited items.

## Studio data and endpoints (special-purpose)

The studio's data that isn't a stored resource becomes computed resource
types (R8), registered by `scrud_django.ui.studio`, so the studio layer styles
them like everything else. Its actions that aren't resource writes stay
endpoints in the studio's URLs, beside the bootstrap studio's existing JSON
endpoints (sample, explain, new-component options and create, item,
save-item, revert-item, pull, push), which the self-hosted studio also uses.
Each is authorized like those: the user's own grants, published branches
read-only for writes, branch selectability as today.

### E1. Preview with drafts and match markers

Why not general: rendering unsaved templates, and marking a rule's matches,
only make sense for an author previewing their branch.

- **Drafts.** A preview request (POST, since it carries a body; it never
  writes) may include unsaved versions of items: a component's template, a
  stylesheet's content, a CSS or JS item. They are layered over T for that one
  render and never stored. A draft of an item is accepted only if the user
  may write that item on T (call), so the preview never shows what the user
  couldn't save. On a published T (read-only in the studio) arbitrary drafts
  are refused; only the after-push mode below applies. Invalid drafts (a template that doesn't compile, a bad
  selector) return the same validation errors a save would, and the preview
  renders without that draft.
- **Live typing** is the editor sending drafts on a short debounce
  (`keyup changed delay:…`); the exact delay is the builder's choice.
- **Match markers.** Given a rule (an index in a stylesheet, saved or draft),
  the preview marks each node the rule matches as **wins** or **matches but
  loses**, with the same outcome words the inspector uses. It must agree with
  `resolve_component` and `explain` exactly; test them against each other.
- **After a push.** A preview of a target branch with a source branch's
  stored overrides layered on shows "the live site after this push" (call;
  serves goal 6.1). It needs GET on the source and the target, not write on
  the target (call): the overrides are stored items, and the proposers and
  reviewers who use it often lack direct write on the target, which is why
  they propose. It accepts no other drafts.
- E1 stays an endpoint, not a computed resource: it takes a body and renders
  a preview document, which isn't a resource read.

### E2. Studio data as computed resources (R8)

- **Effective items of a branch** (`T` in the URL): every component,
  stylesheet, CSS and JS item T shows, each marked own, inherited (from which
  branch) or packaged, with its content, and its conflicts (with the parent
  and with the layer). Only items the user may GET are listed; packaged items
  are public.
- **Branch status** (`T` in the URL): the chain from T to its root, which are
  working or published, T's changed items and conflicts, and the browse URL.
- **Type catalog:** registered types with slug, title and type URI, as the
  bootstrap studio lists them (unfiltered, as there).
- **Branches the user may select**, marked working or published.

Why computed resources and not plain endpoints: so the studio layer renders
them through the cascade and a branch can restyle them (owner). Where an
existing resource list already answers a question with the right filtering,
use it instead (builder's choice).

### E3. What a push will change (a computed resource, R8)

For a source and target branch: the items the push would write, change or
delete in the target, each with before and after, and the conflicts it would
hit (only those on items the user may read there, as `apply_push` reports
them). It needs GET on both branches and never writes. Why not general: it is
specific to branch pushes.

## Preview

- **Isolation (call).** The preview iframe runs T's scripts (goal 5) but has
  an opaque origin: `sandbox` with `allow-scripts` and without
  `allow-same-origin`. A branch's scripts can't reach the studio's page, the
  user's cookies or their session.
- **Clicks.** The preview reports "node at pointer P clicked" to the workbench
  with `postMessage`. The workbench checks the message's source window and
  accepts only that fixed message shape. The bootstrap studio reads the
  iframe's DOM directly, which an opaque origin forbids, so this protocol is
  new.
- **Known constraint.** Requests from an opaque-origin frame are cross-site
  for cookie purposes, so htmx calls inside the preview don't carry the
  author's login, and the branch's CSS and JS files (gated by GET on the
  branch, privately cached) may not load by URL. The builder chooses how the
  preview document gets T's CSS and JS (for example inlined into the preview
  document under a per-response CSP nonce, or short-lived signed URLs) and
  documents the reason. Server-side embeds (R9) are composed before the
  sandbox, so a previewed workbench or editor shows its panes; only requests
  the previewed page's own scripts make at run time go without the login.

## Self-editing (call; owner: "Looks good")

The layers, bottom to top:
1. **Package layer:** `scrud:*` built-ins, the default stylesheet, and the
   studio layer (R1). Can't be broken from the database.
2. **The studio root** (`studio` in the demo) and its published branches:
   overrides of studio items live here like any UI item.
3. **S:** the branch the studio renders itself from.
4. **T:** the branch being edited and previewed.

How an author edits the studio:
- Open the workbench of a working branch in the studio tree (say
  `studio-wip`) while S stays on the published studio. Override a studio item (say the rule editor's row
  component) on T. The preview of a UI-type resource (a stylesheet, say)
  shows it as T would render it. A broken edit breaks only the preview.
- To try the whole studio, "open the studio on this branch" is the existing
  browse-on-branch flow with T as S, which goes through the confirmation page.
- Push to promote it.

Ways out when S itself breaks:
- **Safe mode (R7)** renders the self-hosted studio from the package layer
  only.
- **The bootstrap studio** at its mount (`/studio/` in the demo), unchanged.
- **A server-rendered link to both** on every page that renders a UI-type
  resource while the studio app is installed (call). It is server chrome, like
  the branch banner, so stored components can't replace it. (A branch's CSS
  can still hide it, as with the banner: follow-up `ui-banner-hideable`.)

### The rule editor and other stylesheets

The rule editor edits one stylesheet in place (call). Rules that bear on a
node can sit in several stylesheets; the inspector lists them all (as the
bootstrap studio's does) and links each readable one to its stylesheet's
editor. Match markers (E1) are for the focused row of the open stylesheet.

## Publishing (owner: in v1; promote first)

All of these are renders of existing resources (`UIBranch`, `UIPush`,
`UIBranchVersion`) by the studio layer, plus E1 and E3. No new server logic
for writes: pushes, restores and prefix changes go through the existing
resources and their checks.

1. **Promote (first).** The workbench shows the chain from T to its published
   root. The author pushes one step at a time (to the parent by default, or to
   any ancestor they may push to, call). Before applying, they see E3's
   changes and conflicts and can preview the target after the push (E1). They
   can apply it at once (create the push in `applied`) or propose it (create
   it in `proposed`).
2. **Push records.** A branch's page lists incoming and past pushes. A push's
   page shows its changes (E3) and the after-preview, and moving it to a
   later state is a PUT of its `state`. States a workflow policy adds appear
   as values of the same field; a refused transition shows the server's error.
   Superusers bypass workflow, as everywhere.
3. **Roll back.** A branch's version history (`ui-branch-versions`) with
   "restore to version N", which creates a restore push.
4. **Presentations.** Create a branch (R3's create), and set, change or remove
   a branch's `prefix`. Prefix controls appear only for users with the
   site-level grant (PUT on `ui-branches` in context `""`); the server's
   prefix checks report errors beside the field.

## Security

- Everything in `docs/ui.md`'s security section and the bootstrap studio spec
  still applies: CSP with `script-src 'self'`, no inline scripts in studio
  pages, the branch confirmation flow and banner, the Jinja sandbox.
- **`ui-js` is code execution for a branch's viewers.** Whoever can write
  `ui-js` on a branch runs script with the rights of everyone who views pages
  on that branch, and on a published branch that is the public. `docs/ui.md`
  says so plainly and recommends granting `ui-js` write narrowly. This is the
  same trust model as components with scripts, minus the CSP's protection.
- The preview's opaque origin keeps a branch's scripts away from the
  studio's own page and the author's session.
- The studio adds no permission logic. Hiding a control a user lacks the grant
  for is a convenience; the server's refusal is the check.

## Delivery (call)

One design, two merge requests into `develop`, in order:
1. **Runtime and endpoints:** R1–R10 and E1–E3, with docs. Useful on their
   own; the bootstrap studio is unchanged. This is the larger of the two
   (R8 and R9 especially). The builder may split it further, for example R8
   and R9 first, if that makes review easier.
2. **The studio layer:** the packaged components, stylesheet, CSS and JS;
   the workbench, editors, rule editor, publishing screens and safe-mode
   link; the demo's `studio` root at `/workbench/`, a `studio-wip` working
   branch, and seed data.

Within the second, promote comes before the other publishing screens
(owner's priority).

## Testing expectations

Follow CLAUDE.md: safetydance tests, any order and alone, four databases,
the large-row rules. At least:
- R1: a branch overrides a layer item by name; reverting restores the packaged
  item; a changed layer item makes an override a layer conflict (live, on a
  root branch too, and in the system check), and re-saving resolves it; new
  items can't use a layer prefix and `scrud:` stays reserved; layer items
  don't appear in item lists; ranking among layers and stylesheets.
- R2: `ui-js` CRUD with branch fields, versioned URLs and caching headers,
  gated by GET on the branch; the page's `scripts` list in order; push, pull,
  restore and conflicts cover it; write needs a `ui-js` grant, not a
  `ui-components` one.
- R3: a create from inputs and a fixed-body POST from the browser reach the
  API as JSON with CSRF (the JS is exercised at least by a test of the
  attributes the server renders, and by the demo).
- R4: `?ui-scope=` equals the header; the header wins; invalid values refused
  like the header's.
- R5: with no rule, lists render exactly as before; a rule restyles a type's
  list chrome.
- R6: add, remove, reorder object items; errors land on the right item.
- R7: safe mode ignores stored items on every branch and changes no
  authorization.
- R8: a computed type renders through the cascade, refuses writes with 405,
  answers `If-None-Match`, and shows a user only what they may GET.
- R9: an embed renders with the viewer's authorization (a refused embed shows
  the refusal, never the content); non-resource URLs, cycles and the limits
  are refused; the embedding request's UI branch applies; embeds compose
  inside a preview.
- R10: `?ui-mode=view` downgrades a writer to view mode and never upgrades a
  reader.
- E1: drafts are never stored; a draft the user couldn't save is refused;
  drafts on a published T are refused; the after-push mode needs GET on both
  branches only;
  invalid drafts report the save's errors; match markers agree with
  `resolve_component` and `explain` across scopes, terms, ranks, missing
  components and modes; the preview response has the sandbox CSP without
  `allow-same-origin`.
- E2/E3: no item, rule or conflict appears that the user couldn't GET as JSON;
  E3 agrees with what `apply_push` then writes.
- The studio layer: the workbench renders for a branch the user may GET and
  refuses otherwise; S and T differ as described; a published T gets
  view-mode editors and no write actions; a broken studio override on T
  breaks only the preview; safe mode and the bootstrap studio still work with
  a broken S; the app's CSS and JS don't load under the studio's prefix.
- Publishing: promote, propose then apply, restore, and prefix changes go
  through the existing resources; a user without the site-level grant sees no
  prefix controls and is refused if they try.

## Left to the builder

- Markup, styling and the exact structure of the studio layer's components,
  within the workbench layout above.
- How an app declares a UI layer or a computed type; the term R5 matches on;
  R3's attribute names; R9's template syntax and limits; whether `ui-js`
  loads as modules.
- How the site names its studio branch for the server-rendered studio link.
- How the preview gets T's CSS and JS under an opaque origin.
- The live-typing debounce.
- Whether the bootstrap studio's views move onto E2.
- Whether E2's pieces are better answered by existing resource lists.

## Calls made while writing this

From the builder check, for the owner to check first:
- Layer items aren't in the item lists; they appear in the computed
  "effective items" resource. Name patterns allow a layer prefix only for
  overrides of existing layer items.
- Layer conflicts are computed live (and by a system check), not recorded by
  pull; re-saving or reverting resolves them.
- Published T: view-mode editors (R10), no write actions, no arbitrary
  drafts.
- The after-push preview needs GET on both branches, not write on the target.
- The rule editor edits one stylesheet in place; the inspector links to the
  others.
- Embeds may name only same-site resource URLs.

From the conversation:
- Success is parity plus the studio editing itself.
- Own vs inherited marked in the Items tree; inspector under the preview;
  pane state in browser storage; workbench state in the URL.
- The studio is the UI types' own pages, styled by a packaged layer; S is the
  request's UI branch, T the rendered branch.
- The packaged layer ships with `scrud_django.ui.studio`; layer prefix
  `studio:`; layers rank below branches, above built-ins; library changes to
  overridden items surface as conflicts.
- Safe mode as a general runtime flag, `?ui-safe=1`.
- Drafts need the write grant for that item; preview runs scripts with an
  opaque origin and talks to the workbench by `postMessage`.
- Pushes go one step at a time, to the parent or any ancestor the user may
  push to.
- Django-engine editing, deletion markers, a code editor and workflow policy
  authoring stay out; showing allowed transitions is later.
- Two merge requests: runtime and endpoints, then the studio layer.
