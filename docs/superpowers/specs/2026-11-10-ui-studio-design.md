# UI studio — design

Date: 2026-11-10
Status: builder-checked; awaiting owner approval
Builds on:
- `scrud_django.ui` phase 1 (MR !8; `docs/ui.md`);
- UI branches (MR !10; `docs/superpowers/specs/2026-11-09-ui-branches-design.md`).

Notation: **(owner)** marks a decision the owner made. **(call)** marks a decision
made while writing this, which the owner can overrule. Anything unmarked follows
from the existing code.

## Intent

The author role styles resource types: it writes the components and the
stylesheet rules that decide how each node of a resource renders. Today it does
that through the JSON API and by reloading pages. The owner wants **a UI editor,
"the studio" (owner)**, whose central feature is **a live preview of the type
being styled (owner)**. The studio is the second of two projects. The first,
branches, is merged, so all unpublished work lives in branches.

The person using it is **the author role, styling a new resource type
(owner)**.

### The loop (owner)

1. The author navigates to, or searches for, the type.
2. They select it.
3. They edit a Jinja template for a part of it.
4. They save, and the preview re-renders (**save then preview (owner)**).

Every save goes to the author's personal branch **(owner, from the
branches design)**. From the studio the author pulls, pushes to testing, and
opens the real app on their branch.

### Layout (owner)

**Preview-first inspector (owner, option B of the layout mockups).**
- The preview of the type fills the screen.
- Clicking any part of it selects that node.
- A side panel shows which rule picked the node's component, which rules lost
  and why, and lets the author edit that component or make one for the node.

## Goals

1. **Find a type.** Browse and search every registered resource type (call: the
   type catalog is already public, and filtering by data grants would hide the
   brand-new types the studio exists for).
2. **Preview a type without an instance (owner).** The preview renders a
   **sample built from the type's JSON Schema (owner)**, so a brand-new type
   with no instances works at once.
   - The author can switch the preview to a real instance they may read
     (owner: "yes, that's what I meant", to the sample-or-real question).
3. **Inspect.** Select a node in the preview and see:
   - its JSON Pointer and type terms;
   - the component chosen and the rule that chose it (stylesheet, branch,
     context);
   - the rules that matched but lost, and why: fewer scopes, a less specific
     term, a lower cascade rank, a missing component, or the wrong mode.
4. **Edit a Jinja template (owner).** Edit the chosen component if it is the
   author's (or make it theirs on their branch); see "Editing".
5. **New component for this node (owner approved the first cut).** It creates
   a Jinja component for the node in the current mode and the rule that selects
   it, both on the author's branch, in one action. The author never types a
   selector for the common case.
   - **Most specific first (owner).** The *match* dropdown lists the node's terms
     from most to least specific: class IRI, property IRI, format, enum, bare
     type. It defaults to the most specific. Broad matches are labelled with
     what they affect, for example "every string on this branch" (call).
   - **"Inside this type" (call).** When the node has no type-specific term,
     the dropdown offers this option. It adds a named scope for the node's
     property to the parent's component, overriding that component on the
     branch if it is inherited, and selects inside that scope.
   - **View-specific overrides (owner).** A *where* dropdown narrows the rule to
     a scope of the node's chain, such as `sidebar` or the page. That is a
     selector scope.
   - **Domain-specific overrides (owner).** An *applies to* dropdown narrows the
     rule to a data context, such as `acme`. That is the stylesheet's
     `applies_to`. "Domain" means the data's context **(owner: "you have it
     right")**.
   - **Where the rule goes (call).** The branch's stylesheet named `studio` for
     the chosen `applies_to` (identity `(name, applies_to)`), created on first
     use.
   - **Does it win? (call).** After saving, the inspector says plainly whether
     the new rule wins for that node and, if not, why. Examples: a rule with
     more scopes, or a context's stylesheet outranking an app-wide one. The
     studio never reorders or edits other rules to force a win.
6. **CSS items as plain text (call; owner approved).** Create and edit the
   branch's CSS items. The preview links them, as pages do.
7. **Revert to inherited (call; owner approved).** Delete the author's
   override of an item on their branch. This is the undo button, and "take
   theirs" for a conflict.
8. **Preview controls.**
   - view or edit mode;
   - data context (for the sample);
   - a scope chain (as `Scrud-UI-Scope`);
   - live, or pinned at the branching point;
   - sample or real instance.
9. **Branch strip.**
   - The current branch, its parent and its published root.
   - Its changed items and its conflicts.
   - Pull.
   - Push, to its parent by default (call).
   - "Browse app on this branch", which opens the real app with the branch
     selected and goes through the confirmation flow.
10. **Grants decide everything (owner).**
    - What a user may view or do with data comes from their grants on each
      resource type, whatever UI shows it.
    - Displaying a branch's pages needs GET on the branch.
    - Reading or changing how a branch is built needs grants on the UI item
      types (`ui-components`, `ui-stylesheets`, `ui-css`) in the branch's
      context.
    - The studio shows only what the user's grants allow, and adds no rights.

## Non-goals (first version)

These are out for now **(owner approved the first cut)**:
- hand-writing selectors, editing rules other than through goal 5,
  reordering stylesheets, editing `prefixes`;
- Django-engine templates, so the studio creates and edits Jinja only (it shows
  a Django-engine component read-only);
- deletion markers through the UI;
- live preview while typing **(owner: save then preview is enough for a start)**;
- a JavaScript code editor. The template editor is a plain monospace textarea
  (call).
- editing a published branch. The branch picker lists every branch the user
  may GET, but only working branches (no prefix) are editable; published ones
  are read-only in the studio, so changes go working branch → push
  **(owner, accepted recommendation)**. Branches have no owner field, and
  writing to a published branch from the studio would bypass that flow.
- creating branches. The author picks one of the working branches; creating one stays
  in the API, or a small form may be added if trivial (builder's choice).
- the "edit vs display grants" rework. The current grant model already separates
  them (goal 10); nothing new is built.
- publishing, prefixes, approvals and the workflow, which stay in the API.

## Anti-goals

- **The studio writes outside the author's selected branch.** Every write is an
  ordinary item write on that branch through the resource API's checks
  (scoped RBAC, R36's masking, R14's base, versions).
- **The studio shows a template, rule or CSS item the user may not GET as
  JSON.** The same per-row read checks apply. In the inspector, a matching rule
  in a stylesheet the user can't read appears as "a rule you can't read", with
  only its rank parts. It shows no stylesheet name, selector or component, so
  "why it lost" still makes sense (call).
- **A preview writes data.** A sample is never stored. A preview in edit mode
  renders inputs but never submits. A real instance is only read.
- **The studio is a second authorization path.** It has no permission logic of
  its own beyond asking the existing checks what the user may do.
- **The studio renders itself from stored components.** Its own pages are
  package templates, so a branch can't break or impersonate the editor.

## Concepts

### The sample instance

A JSON value built from the type's JSON Schema, deterministic so that previews
are stable **(call)**:
- `default`, then the first of `examples`, then the first of `enum`;
- otherwise by type:
  - string: placeholder text (`"Lorem ipsum"`), or one valid for its `format`
    (date, date-time, uri, email);
  - number and integer: 0, or `minimum`;
  - boolean: `false`;
  - array: one item, so that the item's component shows;
  - object: every property in `properties`;
  - local `$ref`s resolved;
  - `oneOf`/`anyOf`: the first branch.
- depth limited by `max_body_depth()`.

The sample's `rbac_context` is a preview control, the *data context*, defaulting
to `""` **(call)**. It decides which context stylesheets apply. The branch's own
context only controls who may edit, so the sample doesn't use it. A real
instance uses its own contexts.
A sample that the schema refuses is still previewed, with a notice.

The sample's frame uses decisions that allow PUT, so edit mode can be
previewed, but no `hx-put` is wired **(call)**. The builder chooses the
mechanism, for example a preview flag that renders the frame without write
attributes.

### Preview

- Renders through the existing renderer (`render_resource`) with the
  author's branch selected (`using_selection`) and the chosen mode and scope
  chain.
- The preview is an htmx fragment inside the studio page. Its CSS links are
  the branch's CSS items.
- Each node's rendered HTML carries its JSON Pointer, so a click can select
  it (builder's choice how: attributes on node wrappers in preview mode
  only).

### Inspector explanation

A function next to `resolve_component` (`scrud_django/ui/cascade.py`)
returns, for a node:
- every rule that matched, with its rank parts (scopes, term index, cascade
  rank) and its source (stylesheet name, branch, context);
- the winner;
- for each loser, why it lost.

It must agree with `resolve_component` exactly. Test them against each
other.

### Editing

An item the author edits that comes from an ancestor branch is first copied to
the author's branch as an override, by an ordinary POST with the server
setting `base`. After that, saves are PUTs with `If-Match`. A 412 shows a
notice and reloads. Validation errors, such as a template that doesn't compile
or a selector, are shown next to the editor.

## Where it lives (call)

- **The app:** a separate optional app, `scrud_django.ui.studio`, which needs
  `scrud_django.ui`.
- **URL and access:** a project includes its URLs under a path it chooses,
  such as `/studio/`. Any signed-in user may open it, and it shows what their
  grants allow.
- **Implementation:** Django views and package templates with htmx, using the
  vendored htmx 2.0.4. It is not built from stored components.
- **Writes:** the studio calls the resource views' logic, or makes internal
  requests to the API with the user's credentials (builder's choice). It never
  writes models directly.

## Security

- **CSP:** studio pages follow the CSP in `docs/ui.md` (`script-src 'self'`),
  so there are no inline scripts.
- **Preview isolation:** the preview renders branch components, which may
  carry scripts that run with the author's rights. That is the trust the
  branches design already describes; the author is previewing their own
  branch or a parent they may GET. A branch's components can still reach
  studio controls on the same page, so preview the type in a sandboxed
  `<iframe>` served from the studio. Whether it gets the `allow-scripts`
  flag is the builder's choice, and the reason must be documented.
- **Banner:** the banner and the confirmation flow apply when the author
  opens the real app.

## Testing expectations

Follow CLAUDE.md: safetydance tests, any order, four databases, and the large-row
rules from `scrud_django/rows.py`. At least:
- the sample builder for each schema construct, deterministic;
- a sample is never stored, and a preview in edit mode has no write
  attributes;
- the inspector explanation agrees with `resolve_component` across scopes,
  terms, ranks, missing components and modes;
- "New component for this node" creates a component and a rule on the
  author's branch only, and the preview then uses them;
- a user without read on an item sees rendered output and "a rule you can't
  read" with rank parts only, never its stylesheet, selector, component or
  template; a user without write sees a read-only editor;
- published branches are listed but read-only; a write to one through the
  studio is refused;
- "New component for this node": most specific term by default; a broad term is
  labelled; "inside this type" adds a scope to the parent component (an
  override on the branch); the rule goes to the branch's `studio` stylesheet for
  the chosen `applies_to`; the inspector reports correctly whether it wins;
- edits of an inherited item create an override with the server's `base`;
- a 412 is handled, and revert-to-inherited deletes the override;
- pull and push from the strip go through the existing resources, with
  conflicts shown;
- a working branch and a published root display correctly;
- CSP: no inline scripts in studio pages.

## Left to the builder

- Markup, styling and the exact page structure of the studio, within the
  inspector layout above.
- How preview nodes carry their pointers, and how the preview is isolated.
- How studio writes reach the resource views.
- Whether a small "create my branch" form is included.

## Open questions for the owner

None yet.
