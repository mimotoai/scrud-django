# UI branches — design

Date: 2026-11-09
Status: builder-checked; revised for published branches at URL prefixes;
awaiting owner approval
Builds on: MR !8, `scrud_django.ui` phase 1
(`docs/superpowers/specs/2026-11-09-ui-components-design.md`, user docs
`docs/ui.md`).

Notation: **(owner)** marks a decision the owner made. **(call)** marks a decision
made while writing this, which the owner can overrule. Anything unmarked follows
from the existing code.

## Intent

In phase 1, components and stylesheets are global. Saving one changes every page
that uses it on the next request. There is no safe place to work on the
presentation of a new resource type, and no way to let someone look at a change
before everyone gets it. There is also only one presentation per site.

The owner wants a **UI editor ("the studio") for components and stylesheets**,
built as a Django + htmx app on top of MR !8. Its central feature is **a live
preview against a real instance of the type being styled (owner)**. The person
using it is **the author role, styling a new resource type (owner)**.

Working through that showed the studio needs somewhere for unpublished work to
live, and that a site needs **several published presentations of the same
resources (owner)**. That is this design: **branches of the UI presentation
(owner)**. The work is two projects, done in order **(owner)**:

1. **UI branches** (this document).
2. **The studio**, a separate design written after this one is built.

### The motivating example (owner)

One site, one to-do list type, three published presentations:

| URL | Audience | What differs |
|---|---|---|
| `/todo-list/` | customers | its own CSS |
| `/admin/resources/todo-list/` | internal staff; "like Django's admin, but built on resource types" | CSS, and additional capabilities |
| `/msp/resources/todo-list/` | a managed service provider | a restricted view, with CSS and component stylesheets customized for them |

**These are "just branches that are published", and scoped RBAC and the
stylesheets determine the rest (owner).** What anyone may read or do comes from
their grants, the same under every prefix. A mount changes presentation only.

### The working flow (owner)

1. An author works on UI changes in a **personal branch**.
2. They push those changes to a **testing branch**.
3. **Testers browse the real app on the testing branch.**
4. The author, or another authorized person, **pushes testing to the published
   branch**.

Anyone can **interact with the current state of any branch they are authorized
to see (owner)**.

## Goals

1. **Branches of the presentation.** Each branch is a set of **components,
   component stylesheets, and CSS** (call: CSS as a third item kind, from the
   owner's "the CSS varies").
2. **Published branches (owner).** Any number of branches can be published,
   each at its own **URL prefix (owner)**. The prefix is data on the branch, so
   publishing a new presentation needs no deploy (call).
3. **A branch is a union over a base (owner).** It stores only its own changes
   and shows the base's state for everything it hasn't changed.
   - **Live base (the default, owner):** the base is the parent branch's current
     state. A fix published to the parent shows up on a live branch at once, for
     every item the branch hasn't changed.
   - **Pinned base (owner):** the base is a fixed version of the parent. This
     gives git's behaviour.
   - **The base mode can be chosen (owner)**, per branch, and per view (goal 7).
   - Published branches can have parents too (call): the MSP branch can be live
     over admin, plus the MSP's own changes.
4. **Every branch records its branching point (owner):** the versions of its
   ancestors it was started from (or last pulled). Switching a live branch to
   pinned pins it at its branching point. This gives authors and testers an easy
   way out "if things get weird" **(owner)**.
5. **Push and pull.**
   - A **push** moves a branch's changes into another branch, normally
     personal → testing → published.
   - A **pull** brings a branch up to date with its parent.
   - A push is a **resource that the workflow authorization layer can gate
     (owner)**, so approvals can be added later without changing branching code.
6. **Authorization is scoped RBAC only (owner).** A branch is a resource with an
   RBAC context. Whoever has permissions in that context can see it (and write
   to it, push from it, or pull it, as their grants say), like any other
   resource. This also decides who may open a published branch's prefix. It
   **replaces phase 1's "trusted global author role" (owner)**; see Security.
7. **Browsing the real app on a working branch (owner).**
   - A **switcher** and a **banner** that names the active branch.
   - **Links carry the branch.**
   - The future studio is an entry point: open the dev tools, pick a branch,
     and browse the app on it **(owner)**.
   - Testers and authors can flip a view between live and pinned at the
     branching point **(owner)**.
8. **History and rollback.** Every change to a branch makes a new version. A
   version can be restored, which rolls a bad publish back.

## Non-goals

- **Branching resource data.** Branches change presentation only. A viewer on
  any branch or prefix sees the same resources, with the same authorization.
- **Using a mount as access control.** Restricting what an MSP may do is done
  with their grants (owner). A mount that hides a field or an action is
  presentation, as phase 1 already says of components.
- **Line-level merges of templates or CSS (call).** Conflicts are found and
  resolved per item, as whole items.
- **An approval step now (owner).** Pushing is a permission for now. The design
  only has to leave room for a workflow policy to add approval states.
- **The studio.** It is the next project. This one must give it what it needs:
  branch selection, a way to render with a branch, and the branching point.
- **Sandboxing untrusted authors** (phase 3 of the UI plan). This design moves
  the trust boundary to RBAC contexts; it does not add a sandbox.

## Anti-goals

These would make the work a failure even if it technically ran:

- **Anything changing for a viewer who never chose a working branch**, apart
  from a push into the published branch they are on. That includes extra
  queries or noticeably slower rendering.
- **A push that silently overwrites a change it didn't know about.**
- **A prefix or a branch changing what a viewer is authorized to read or
  write.** They only select presentation.
- **Rendering a branch the viewer isn't authorized to see.**
- **A branch link that runs someone else's components without the viewer
  knowing which branch they are on.**
- **A second authorization path.** Mounted URLs reach the same views, with the
  same checks, as `/api/` (phase 1's anti-goal of "a parallel set of UI
  endpoints").

## Concepts

### Branch (a new resource type, `ui-branches`)

| Property | Meaning |
|---|---|
| `name` | Unique site-wide (call). |
| `rbac_context` | The branch's context. Grants there decide who may see, change, push from, or pull it, and for a published branch, who may open its prefix **(owner)**. |
| `parent` | The branch it is based on, or none. Any chain is allowed **(call)**. Setting or changing it needs GET on the parent as stored; a parent the writer may not GET gets the same 400 as an unknown one (ruling R36, during implementation). |
| `base` | `live` (default, **owner**) or `pinned`. Ignored without a parent. |
| `branching_point` | The ancestor versions the branch started from, or last pulled (see Branching point). Always recorded **(owner)**. When `base` is `pinned`, this is also the state it unions with. |
| `prefix` | Present only on a published branch: its URL prefix, e.g. `/`, `/admin/resources/`, `/msp/resources/` **(owner: published branches at prefixes)**. Unique. Setting or changing it (null to a value or back included) needs PUT on `ui-branches` in the context `""`, a site-level grant: a prefix claims site URLs and gates them with GET on its branch (ruling R37, during implementation). |
| `label`, `description` | Free text. |

The upgrade creates one published branch, `main`, in context `""`, at the API
prefix, so HTML keeps being served at `/api/...` as in 0.16 (see Upgrade).

A **working branch** is any branch without a `prefix`. It belongs to the tree
of its **nearest published ancestor**, and is browsed under that ancestor's
prefix (call).

### Items

A branch holds three kinds of items (call):

| Kind | Identity | Notes |
|---|---|---|
| UI component | `name` | as phase 1 |
| UI stylesheet (component rules) | (`name`, `rbac_context`) | as phase 1; `rbac_context` means *where it applies* |
| CSS (new type, `ui-css`) | `name` | plain CSS text plus an `order`. A published branch's effective CSS is served under its prefix at a URL that includes the content's version, so browsers can cache it forever (call). The files are cached `private, max-age=31536000, immutable`, never `public`, because a mount's CSS is gated by GET on its branch, which a shared cache would not check (ruling R33, during implementation). `scrud:page` gets the list of CSS URLs in its context and links them. |

### Version

Every branch has a numbered, immutable history. **Every change to a branch's
own state creates its next version (call)**: a direct write to one of its
items, a push into it, a pull, and a restore. A version records the branch's
**overrides** and the **ancestor versions it was resolved against**, so any
version's effective state can be rebuilt exactly. Versions are read-only
resources (or a read-only view on branches; builder's choice). They support
pinned bases, branching points, conflict checks and rollback.

### Branching point

A branching point records **the version of every ancestor** at the moment the
branch was started or last pulled, e.g. testing v12 and admin v40, not only the
parent's version **(call, from the owner's intent)**. "Pinned at the branching
point" therefore shows exactly what the author saw then, even when the parent
was itself a live branch whose own version hadn't changed while its parent
moved. The banner shows it as the parent's version with the full chain on
hover (call).

### Override

A branch stores only its **overrides** **(owner: union)**:

- a **changed** item (one the base also has);
- an **added** one (the base doesn't have it);
- a **deletion marker** that hides the base's item on this branch (call).

Each override records the **base version of the item** it replaced: the
version of the parent's item when the override was made, or "none" for an
addition. The push conflict check uses it. A branch without a parent stores
all its items, so its overrides are its whole state.

### Effective state

The items visible on a branch: its overrides over its base. The base is the
parent's current effective state when live, or the parent's branching-point
version when pinned. This is recursive: a personal branch over a live testing
branch over a published branch sees the published branch's latest fixes, then
testing's changes, then its own.

## Published branches and URL prefixes

- **Routing (call).** Under a published branch's prefix, `<type-slug>/` and
  `<type-slug>/<id>/` reach **the same resource views** as `/api/<type-slug>/...`:
  the same authorization, preconditions, workflow and write path. Only the
  presentation differs. The owner's `/todo-list/` example is illustrative; the
  segment is the type's slug.
- **What a prefix claims.** A prefix claims only `<prefix><registered type
  slug>/...` (plus the CSS URLs). A prefix of `/` doesn't swallow the rest of
  the site. A system check reports a prefix that overlaps another prefix or a
  URL in the project's URLconf (call). Setting or changing a prefix refuses
  (400) one that overlaps another, is shadowed by the project's URLconf, or
  lies under the API's URLs (ruling R37, during implementation). It reports a Warning,
  `scrud_django_ui.W001`, not an Error: it is a database check that runs at
  `migrate`, and stored data must not block migrations; routing stays
  deterministic, the longest prefix wins (ruling R26, during implementation).
- **Opening a prefix needs GET on its branch (owner: scoped RBAC).** Without
  it, the response is the same refusal (401 or 403, as an HTML notice) as any
  refused GET (call). It doesn't fall back to another presentation. A
  customer-facing branch grants GET on itself to customers (or to anonymous
  users, if the site's policy does that).
- **Links stay in the mount (call).** HTML links and htmx requests made under a
  prefix use that prefix's URLs. JSON bodies keep the canonical `/api/` hrefs,
  so the JSON API is unchanged.
- **No banner** on a published branch at its own prefix: that is the normal
  site.

## Browsing a working branch

- **The prefix selects the published branch. Within its tree, the active
  working branch** comes from, in order:
  1. a request header (htmx fragment requests always send it, so a page never
     changes branch halfway);
  2. a query parameter on a link (name left to the builder; e.g.
     `?ui-branch=testing-admin`);
  3. a cookie set by the switcher (scoped to the prefix, call). Its name is
     `scrud_ui_branch` at the API URLs and
     `scrud_ui_branch_<first 12 hex digits of sha256(prefix)>` under a prefix,
     so that a cookie for `/` cannot shadow another prefix's
     (ruling R28, during implementation);
  4. none: the published branch itself.
- **Authorization:** the viewer must be allowed to GET the working branch, and
  every working branch among its ancestors (it shows their items; published
  ancestors are the site's presentation, gated at their own prefix) (ruling
  R36, during implementation), and it must be in the prefix's tree. If not, the request renders on the published
  branch, and the banner says the chosen branch was not available (call). It
  never fails the page.
- **Banner:** every HTML page on a working branch shows a banner naming the
  branch and its mode (live, or pinned at version N), with the switcher and a
  link back to the published branch **(owner: switcher and banner)**. It is
  rendered by the server outside stored components, so that a branch's
  components cannot replace it (call). It is not a guarantee: a branch's CSS,
  or a page component that wraps the body in a hidden element, can still hide
  it (follow-up `ui-banner-hideable`).
- **Links carry the branch (owner).** Links and htmx requests made while on a
  branch keep it. A shareable link includes the query parameter.
- **Shared-link confirmation (call; the owner can drop it).** Opening a link
  that would switch you to a working branch other than the one you are on
  shows a one-click "You're about to browse branch X" page first. This exists
  because of the security tradeoff below. The link that confirms carries an
  unforgeable token, `ui-branch-confirm=<HMAC of the selection and the
  viewer's CSRF secret>`, issued only by the server's own confirmation and
  banner links, so a link planted in content cannot skip the page; it needs
  Django's `CsrfViewMiddleware` (ruling R23, during implementation).
- **Live or pinned per view (owner).** The banner can flip the view between
  the branch's live state and the branch pinned at its branching point. This
  changes only that viewer's rendering, not the branch's `base` setting. It is
  carried with the branch selection (header, parameter, cookie).
- **JSON is not affected by browsing.** The active branch selects presentation
  for `text/html` only.

## Pull

A pull moves a branch's branching point to its ancestors' current versions and
keeps the branch's overrides. A pull is a PUT of the branch resource (it
changes `branching_point`), so it needs PUT on the branch in its context
(call); no separate pull type.

- An override whose item changed in the parent since its recorded base
  version is a **conflict**. The pull still happens, but the conflict is
  listed on the branch until it is resolved (call).
- An author resolves a conflict by keeping theirs (the override's recorded
  base moves forward), taking the parent's (the override is dropped), or
  editing it.

For a live branch, a pull changes no rendering. It updates the branching
point and surfaces the conflicts that a push would hit.

## Push (a new resource type, `ui-pushes`)

A push is a resource **(owner: so workflow can gate it)**:

| Property | Meaning |
|---|---|
| `source`, `target` | Branch names. |
| `state` | `proposed` or `applied`. A policy may add states in between. |
| `rbac_context` | The target branch's context (call), so the grant to push into a branch is a grant in that branch's context. |
| `result` | Set by the server: the target's new version once applied. |

**The trigger is fixed (call): a write that moves a push into `applied`
applies it, in the same transaction.** A client may create a push directly in
`applied` (a create, `__INITIALIZE__` → `applied`) or create it `proposed` and
PUT it to `applied` later. With no workflow policy, either works for anyone
with the grants. A later `WorkflowPolicy` can require `proposed` → `approved`
→ `applied`, with the approve and apply transitions granted to whoever it
names, and the branching code doesn't change. The server never writes
`applied` itself.

Grants (call, except the last):

- creating or moving a push: the ordinary grants on `ui-pushes` in the
  target's context, plus workflow;
- GET on the source branch (the push reads its overrides);
- PUT on the source branch, because applying empties its overrides. So
  pushing testing to admin needs write on testing as well as the push grant
  on admin;
- **for each item pushed, the grant a direct write of that item into the
  target would need (owner).** See Authorization on branches.

Applying a push:

1. Compare each source override's recorded base version with the target's
   current version of that item. If any differ, the write is **refused with
   409 and the list of conflicts**; the list names only conflicts on items the
   pusher may GET in the target, and the rest are counted
   (`unreadable_conflicts`) (ruling R20, during implementation). Nothing is
   written and the push stays as it was (there is no `refused` state, so a future policy needn't define
   one) (call). Resolve on the source (pull, then keep or take), then push
   again.
2. Otherwise, write every override into the target as one atomic step and
   create the target's next version.
3. The source branch is left **empty over its base**: its overrides now live in
   the target. The branch itself stays for the next round of work (call).

The push goes through the ordinary scrud write path (RBAC before and after the
save, workflow, schema validation), so a policy written later applies without
code changes. Superusers bypass workflow, as everywhere else.

Pushing into a branch other than the source's parent is allowed (call).

An applied push is a record: any update of it is refused (400) and a delete
is a 409; pushing again takes a new push, which a workflow policy gates
afresh (ruling R18, during implementation).

A push whose source has no parent (`main`, or any root) is refused (400),
because it would delete the live site's items (ruling R19, during
implementation).

The conflict check doesn't depend on the parent link.

## Deleting a branch

`main` cannot be deleted (400), and neither can a branch that another names as
its parent (409). Deleting a branch deletes its own items and its versions in
the same transaction, so a branch created again under the name starts at
version 1 (ruling R10, during implementation).

## Rollback

Restoring version N of a branch writes its content as the branch's next
version (N+k). History is never rewritten. A restore brings back the branch's
**overrides** as of N and keeps its current base mode (call). On a live branch,
inherited items stay live, so a restore doesn't freeze the branch against later
fixes in its parent. On a branch with no parent, overrides and effective state
are the same thing. Restoring a published branch is how a bad publish is rolled
back. The restore is a push-like resource, or an option on a push (builder's
choice), so it is authorized and gateable the same way.

## Items on branches

Components, stylesheets and CSS stay resources, written and read through the
resource API **(call)**:

- A read or write names a branch (by the same header or parameter as browsing).
  Without one it means `main`.
- A **read** returns the branch's effective state (overrides over the base).
  Each item says whether it is inherited, changed, added, or hidden on this
  branch, and its base version.
- A **write** on a branch creates or updates that branch's override and never
  touches the base. Deleting an inherited item creates a deletion marker.
- Phase 1's write-time checks (template compiles, selectors parse, name unique)
  apply per branch, against the branch's effective state. A CSS item is checked
  only for size and encoding (call).
- ETags and `If-Match` work per item per branch, as for any resource.

How overrides and versions are stored (extra models, extra content fields,
copy-on-write rows) is left to the builder, within the constraints in this
document.

### Rendering

Every lookup of stored components and stylesheets goes through `_stored()` in
`scrud_django/ui/cascade.py`. Inside a render, the active branch (from the
prefix and any working-branch selection, plus the view mode) decides what it
returns. The per-request memo (`rules_memo`) stays one read of the effective
state per rendered response, keyed by branch.

The cascade, selectors and precedence of phase 1 don't change. A branch
changes only which items exist.

HTML responses add the working-branch selection to `Vary`. They are already
`no-store`.

## Security (owner: scoped RBAC only)

Phase 1 trusted whoever could write `UIComponent` resources in context `""`
with the markup of every page. With branches, **writes are authorized by
scoped RBAC in the relevant context, and there is no separate trusted role
(owner)**.

The consequence, stated here so it's a known tradeoff:

- **Viewing a branch means trusting everyone who can write to it.** Their
  components and CSS can include scripts or styling that run in your browser
  with your rights. For example, a user with write grants in a personal
  branch's context could send a link to a colleague with broader rights.
- **Mitigations in this design:**
  - you see only branches you may GET;
  - the banner always names a working branch;
  - the shared-link confirmation (call) appears before you switch;
  - the CSP recommended in `docs/ui.md` still applies (it blocks inline
    scripts, though not a component's scripts shipped as static files).
- `docs/ui.md`'s Security section must be rewritten to say this.

### Authorization on branches (owner: "a branch is a playground; publishing needs the real rights")

- **On a working branch**, writing an override (change, addition or deletion
  marker) needs only the grants on the item's type in the **branch's**
  context. A stylesheet's own `rbac_context` keeps meaning *where it applies*;
  on a working branch it doesn't decide who may write it. How an override row
  records the two contexts is left to the builder.
- **On a published branch**, a direct write of an item is authorized in one
  context, its **authorizing context** (call, generalizing the owner's
  "components on main stay in `""`" to several published branches):
  - a component or a CSS item: the published branch's context;
  - a stylesheet that applies to `""` (everywhere on that branch): the
    published branch's context;
  - a stylesheet that applies to a context `X`: `X`. Whoever may style `acme`
    styles `acme` on every published branch, and needs no grant in the
    branch's context.

  For the upgraded `main` (context `""`) this is exactly phase 1's rule:
  components and app-wide stylesheets in `""`, an `acme` stylesheet in `acme`.
  (An earlier draft required both the branch's context and `X`; that broke
  phase 1's delegation on `main`.)
- **Publishing**: a push into a target is allowed only if the pusher also holds,
  for every item in the push, the grant a direct write of that item into the
  target would need (owner). Pushing a stylesheet that styles `acme` into a
  published branch needs write on stylesheets in `acme`. This keeps phase 1's
  delegation: whoever may style `acme` can publish `acme` styles and nothing
  else. A push failing this check is refused (403) and writes nothing.
- **Naming a branch you may not see.** An item write naming a branch the
  writer may not GET is answered exactly as one naming an unknown branch
  (400, naming nothing of it), but on `main` and for a stylesheet on a
  published branch authorized in the context it applies to. A push naming a
  source the pusher may not GET, or a target they may not GET with the wrong
  `rbac_context`, is answered the same way (ruling R36, during
  implementation).
- **Reading through a branch** (JSON): inherited items are subject to the same
  read check as on the branch they come from (call). A viewer who can GET
  branch `alice` but has no read on stylesheets in `acme` doesn't see `acme`'s
  stylesheets in branch `alice`'s JSON, nor in a version of it. Rendering
  still resolves presentation with system rights, as in phase 1, and a
  published branch's CSS is served to anyone who may open its prefix.
- `UIComponent-schema.json`'s `rbac_context: enum [""]` is replaced by the
  rules above.

## Upgrade from 0.16

- `main` is published at the API prefix, in context `""`, holding every
  existing component and stylesheet, and no CSS. Existing rows have no
  `branch` field and belong to `main`. Until something is written, `main` is
  virtual (no stored row) and is stored, with its version 1, on the first UI
  write. This needs no data migration (plan's call: the UI resource types are
  registered from the URLconf, not by migrations).
- A site that creates no other branch behaves exactly as 0.16 did: the same
  URLs, the same content, the same rendering, and no banner.

## Release

This builds on MR !8, which is not yet merged. It goes on a new branch,
`feature/ui-branches`, off `feature/ui-components`, and its MR targets the
release branch after MR !8 merges. It adds resource types and changes the
security model of an optional app, so it is a **minor** bump (0.17.0) (call).
Work now merges into `develop` with no version bump; the release notes entry is
under an unreleased heading, and releasing is a separate `develop` to `main`
merge request (ruling R5, during implementation).

## Testing expectations

Follow the repo's conventions (`CLAUDE.md`: safetydance tests, any order, all
four databases). At least:

- union over a live base picks up a parent change for untouched items, and not
  for overridden ones;
- pinned base ignores later parent versions; switching live ↔ pinned at the
  branching point, per branch and per view;
- a push with no conflicts writes atomically and creates a version; a push with
  a conflict writes nothing and lists the conflict; a deletion marker pushes as
  a delete;
- a push lacking the per-item grant for one item is refused and writes nothing;
- pull moves the branching point and lists conflicts;
- rollback creates a new version equal to the restored one;
- two published branches at different prefixes render the same resource
  differently, with the same authorization; a published branch live over
  another picks up its parent's changes;
- a prefix refuses a viewer without GET on its branch; a prefix of `/` claims
  only type slugs; overlapping prefixes are reported by the check;
- under a prefix, writes go through the same checks as `/api/` (RBAC,
  workflow, `If-Match`), and links and htmx requests stay under the prefix;
- CSS is served under the prefix at a versioned URL and linked by `scrud:page`;
- browsing: header > parameter > cookie > published branch; a branch outside
  the prefix's tree, or one the viewer can't GET, renders the published
  branch; htmx requests keep the branch; the banner appears only on working
  branches;
- a push is gated by a test `WorkflowPolicy` with an `approved` state, with no
  code change;
- after the upgrade, with no branches created, `/api/` HTML renders
  byte-for-byte as before and makes no extra queries per render.

## Left to the builder

- Storage of branches, overrides and versions.
- Names of the header, query parameter and cookie.
- URL layout of CSS under a prefix (it must include the version).
- Whether versions are their own resource type or a view on branches.
- Whether a restore is its own resource type or a push option.
- The switcher's and the banner's markup, and how they get into pages (outside
  stored components, per the call above).
- Whether to create branches automatically for new authors (not required).

## Open questions for the owner

None. The builder check's questions are answered above: versions and
branching points (Concepts), the push trigger and grants (Push), restore
(Rollback), and publishing rights (Authorization on branches, owner). Published
branches at prefixes were added after the builder check, from the owner's
three-surface example.
