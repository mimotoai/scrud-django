# API versioning and mount location — design

Date: 2026-11-23
Status: approved by the owner 2026-11-24
Plan: `docs/superpowers/plans/2026-11-24-api-versioning-and-mount.md`
Builds on:
- scrud-django 0.18.0 (the workflow decisions rework: `WorkflowDecision`,
  `on_transition` actions, the acting user, the outbox, `__OVERRIDE__`);
- the ninja front end (`docs/superpowers/specs/2026-11-11-ninja-front-end-design.md`),
  `scrud_django.ninja.api()` and own-model routers (`ninja.workflow_check`);
- scrudful 0.10.1 (`scrudful.ninja`, HSR) and django-scoped-rbac 0.12.1
  (`scoped_rbac.ninja`);
- the UI's mount and branch machinery (`docs/ui.md`, `ui/branches/selection.py`).

Notation: **(owner)** marks a decision the owner made. **(call)** marks a
decision made while writing this, which the owner can overrule. Anything
unmarked follows from the existing code.

## Intent

Production APIs built on scrud-django need to evolve without breaking the
clients they already have. A typical path: an app migrating an existing,
hand-written API onto scrud-django keeps that API answering at `/v0` while
a scrud-shaped `/v1` appears beside it, and later a `/v2`. scrud-django
needs two things it does not have:

1. **API versioning** **(owner)**: several versions of a scrud API served
   side by side by one Django process, so that a new version can add
   resources and make breaking changes to existing ones while older versions
   keep answering exactly as before.
2. **A developer-chosen API location** **(owner)**: the app decides where each
   API is mounted (`/v1/`, `/api/v1/`, `/svc/api/`…), and every link scrud
   emits — and the UI — follow that choice.

The motivating case (owner): an existing `/v0` (a hand-written, non-scrud
ninja API) keeps working for its callers while the scrud-shaped API appears
as `/v1`. The design must also hold when `/v2` arrives.

## Goals

1. **A version is a mounted scrud API** with its own set of resources, its own
   prefix, its own URL namespace and its own OpenAPI document. Several
   versions coexist in one process.
2. **A later version can** (owner):
   - add resources the earlier one lacks;
   - change the representation of a resource in breaking ways (field names,
     nesting, schema) — for **own-model** and **computed** resources;
   - **(call)** drop a resource, or serve it under a different slug.
3. **Unchanged resources carry forward** **(call)**: a version is declared as
   "the previous version, plus these additions, minus these removals, with
   these replacements". A resource nobody changed is not redeclared.
4. **Same resource, same rules** (owner, "assume right for now"): a resource
   served by two versions is one underlying thing — same type IRI, same row,
   same primary key (`/v1/articles/7` and `/v2/articles/7` are the same
   article). RBAC grants, workflow policies and the workflow state machine
   belong to the type, not to a version. A role that may edit articles may do
   so through any version that serves them; a transition is authorized the
   same way whichever version carried the write.
5. **Links stay in their version**: every link a version emits — item `href`s,
   `Location`, `Link` headers (schema, context, HSR `describedby`), pagination
   links, the `services/` catalog, workflow-policy links in errors, own-model
   `detail_iri`s — points into the version that served the request. Nothing
   silently resolves into another version.
6. **Any mount location works**, including nested prefixes and an outer URL
   namespace (`path("v1/", include(([path("api/", v1.urls)], "v1")))`). Today
   that case 500s on HSR links (follow-up `ninja-hsr-link-targets`); it must
   work.
7. **Deprecation is signalled** **(call)**: a version can be marked deprecated
   and given a sunset date; every response it serves then carries
   `Deprecation` (RFC 9745) and `Sunset` (RFC 8594) headers, and optionally
   `Link` headers with `rel="deprecation"` / `rel="sunset"` to a policy page.
8. **The UI targets one chosen version** **(call)**, normally the newest,
   instead of "whichever API reversed first".
9. **Coexists with non-scrud ninja APIs**: an app's hand-written `NinjaAPI`
   (for example a legacy `/v0`) mounted in the same URLconf is untouched by scrud's
   link resolution, checks and UI — **but its writes to governed models go
   through the same workflow check** (owner, §8). A hand-written API is not
   a side door around the workflow.

## Non-goals

- **Representation changes for stored JSON `Resource` types** (owner: "fine
  for them to stay unchanged for now"). A stored type has one schema, one
  JSON-LD context and one representation in every version that serves it. A
  version may include or omit it, nothing more.
- **Version negotiation by header or media type** **(call)**. Versions are
  URL-path only.
- **Automatic translation between representations.** Each version's
  own-model/computed resource has its own schemas written by the app.
- **DRF front-end versioning** **(call)**. DRF is the `[drf]` extra since 0.17;
  versioning is a ninja feature. DRF's `DEFAULT_VERSIONING_CLASS` stays
  unsupported, as 0.17 documents.
- **Per-version RBAC grants or per-version workflow policies.**
- **A canonical body mapping for RBAC conditions** (owner: deferred). For
  now the field-stability rule in §3a applies instead; see "Deferred".
- **Data migrations** of any kind.

## Anti-goals (would make this a failure even if it technically works)

- A link in a `/v2` response that points into `/v1` (or vice versa), in any
  header, body, HSR document or error.
- An authorization or workflow decision that differs depending on which
  version carried the request.
- Adding a version requiring the app to copy-paste every unchanged resource.
- The UI or the HSR documents silently binding to the first-registered API
  when several exist (today's behaviour, see "Current state").

## Current state (what the builder will change)

Mapped on 0.18.0 (develop @ 0062760, whose code 0.18.0 releases unchanged).
Citations are to that tree.

**One `api()` call builds one API.** `scrud_django.ninja.api(*, types=(),
urls_namespace="scrud", ui_types=True, **ninja_kwargs)` (`ninja/api.py:97`)
builds a `NinjaAPI(urls_namespace=…, default_router=ScrudfulRouter())`,
registers every spec (`spec.register()`), builds a fresh type router per JSON
type (`routers.type_router`), the `services/` route, scoped-rbac's role
routes, computed routers and the collection json-schema / json-ld routes, then
wraps the views and calls `note_api_routes(slugs, namespace=urls_namespace)`.
Two `api()`s with distinct namespaces already coexist
(`tests/two_ninja_apis_urls.py`, `tests/test_core_urls.py:185-243`), but only
with **disjoint** types.

**Process-global state keyed by type** — collides when one type is served
by two versions with different hooks:
- `core/types.py:342-379`: `_OPTIONS[type_iri]`, `_SLUG_IRIS`, `_IRI_SLUGS`.
  Re-registering an IRI with the same slug replaces the hooks (last wins, a
  warning); the same IRI under another slug is `ImproperlyConfigured` unless
  the hooks are identical.
- `services` catalog `type_uri -> slug prefix` (`__init__.py:73-135`).
- `_PENDING_REGISTRATIONS[slug]` (`registration.py:434-528`).
- `_GOVERNED[model] -> (iri, state_of, contexts_for)`
  (`core/transitions.py:255-267`); `workflow_check` re-registering replaces
  `state_of` (`ninja/workflow.py:387`).
- scoped-rbac `semantic._generated_schemas[iri][front_end]`, last wins
  (`scoped_rbac/semantic.py:36-71`).
- `_api_routes: namespace -> {slug}` (`core/urls.py:16-35`), keyed by the
  **short** namespace given to `api()`.

**Type identity is already URL-free.** `ResourceType` is keyed by `type_uri`;
RBAC checks `Permission(action, type_iri)` (`core/requirements.py:685`);
workflow policies are looked up by `type_uri` (`workflow.py:478-563`). Goal 4
needs no change to RBAC or policy lookup — except that grant conditions read
the raw body, which §3a addresses.

**Workflow state is read from the representation.** For own models the CEL
variables default to `schema_out.model_dump(mode="json")`
(`ninja/workflow.py`, README "Your own models with workflow"). Two versions
with different `schema_out`s would evaluate the same policy over different
shapes — which breaks goal 4.

**Link resolution.** `absolute_reverse` (`core/urls.py:141-197`) takes the
namespace from `request.resolver_match.namespace`, else a `namespace=`
argument, tries `"<ns>:<name>"`, then the **bare global name**, then every
noted namespace. `serving_api_namespace` (`core/urls.py:104-114`) compares the
**full** `match.namespace` (`"v1:scrud"`) against the **short** noted name
(`"scrud"`) and fails under an outer namespace; embed `_resolve`
(`ui/embed.py:306`) has the same flaw. The fallback to a bare name is silent
(follow-up `ninja-namespace-fallback-logging`).

**HSR documents are built without a request** and cached per router per
(path, `id(api_error)`) (`scrudful/ninja.py:2327-2351, 2622-2626`); their
relative links resolve against the single process-wide
`SCRUDFUL["BASE_URL"]`. When scrud describes without a request it reverses
with the short `urls_namespace` (`ninja/routers.py:115-124, 418-448`), which
is the cause of `ninja-hsr-link-targets`. The per-response HSR `Link` is
reversed with the full `match.namespace` (`scrudful/ninja.py:2862-2873`) and
already works under nesting.

**No code hard-codes `/api/`** (only docstrings: `ui/urls.py:7`,
`ui/embed.py:29`, `ui/branches/selection.py:84`). The real assumption is a
**single API base**: `selection.api_base()` (`ui/branches/selection.py:83-110`)
picks the first route that reverses; `in_mount`, `Mount.href`, `cookie_path`,
`find_mount` (`ui/branches/middleware.py:105-108`) and the branch checks use
it; `pages.route_views` (`ui/pages.py:185-204`) and `embed.api_url` /
`_api_path` (`ui/embed.py:149-170, 330-333`) reverse without a request and so
get the first noted namespace.

**Own-model routers** are module-level `ScrudfulRouter`s added with
`API.add_router("articles/", router)` (`tests/own_model_urls.py`,
`demo/demo/urls.py`). The comment at `ninja/api.py:192` says ninja mounts a
router on one API only. ninja ≥1.7 is documented by scrudful's tests to
clone a router's operations when it is mounted in several APIs
(`scrudful/tests/test_ninja_renderer.py:211-213`) — **unverified against the
pinned 1.7.3 source**. Even if mounting twice works, scrudful's HSR document
for a shared router is shared across mounts and its link closures do not know
which API is describing them.

**OpenAPI**: `docs_url` / `openapi_url` pass through `ninja_kwargs` per
`api()` (`ninja/api.py:170-171`); ninja reverses `"<urls_namespace>:api-root"`
for the path prefix, which fails under an outer namespace unless
`urls_namespace` is colon-qualified (inferred). No `Deprecation`/`Sunset`
support exists anywhere.

## Design

### 1. Declaring versions

`api()` grows versioning parameters (names are the builder's; shape is the
**(call)**):

```python
from scrud_django import ninja as scrud_ninja

v1 = scrud_ninja.api(
    version="v1",
    types=[todo_lists_type, word_count_v1],          # stored + computed specs
    routers={"articles/": articles_router_v1,        # own-model routers
             "notes/": notes_router_v1},
    docs_url="/docs", openapi_url="/openapi.json",
)

v2 = scrud_ninja.api(
    version="v2",
    based_on=v1,                                     # carry everything forward
    replace={"articles/": articles_router_v2},       # breaking change
    add=[tags_type], add_routers={"comments/": comments_router_v2},  # new
    remove=["notes/"],
)
# v1 can later be marked deprecated (§6): api(version="v1", ..., deprecated=...)

urlpatterns = [
    path("v0/", legacy_v0.urls),          # plain NinjaAPI, not scrud
    path("v1/", v1.urls),
    path("v2/", v2.urls),
    path("semantic/", include("scoped_rbac.semantic_urls")),
    path("", include("scrud_django.ui.urls")),
]
```

Rules:
- `version` is a short label, unique per process, and the default URL
  namespace becomes `scrud-<version>` **(call)** (an explicit `urls_namespace`
  still wins). Unversioned `api()` keeps today's behaviour exactly (namespace
  `scrud`), so existing apps are unaffected.
- `based_on` copies the earlier version's **declarations** (specs and router
  factories), not its built objects; each version builds its own routers so
  link closures and HSR documents are per version (§4).
- **Own-model routers must be supplied as factories** **(call)** —
  `callable(ctx) -> Router`, where `ctx` carries the version's namespace and
  label — so that a router carried forward is built fresh for the new
  version. A plain `Router` instance stays accepted for an unversioned API.
  Builder: if ninja 1.7.3 really clones mounted routers, a router instance
  may also be accepted, but only if links and HSR documents are proven
  version-correct (§4); otherwise factories only.
- `remove` takes slugs/prefixes; removing something the base lacks is an
  error. `replace` of something the base lacks is an error.
- A system check rejects: duplicate version labels; a slug served twice
  within one version; a type IRI served under two different slugs **within
  one version**.

### 2. Type identity across versions

- A type IRI may be served by several versions, **with a different slug per
  version** **(call)**. Process-global maps keyed by IRI or slug
  (`_OPTIONS`, `_SLUG_IRIS`, `_IRI_SLUGS`, `services`, `_PENDING_REGISTRATIONS`,
  scoped-rbac's `_generated_schemas`) become **keyed by (version, IRI)** /
  **(version, slug)** for everything representation-dependent: `compute`,
  `read_view`, schemas, slugs, the services catalog. Anything
  representation-independent (the `ResourceType` row, RBAC, workflow policy
  lookup) stays keyed by IRI alone.
- The `ResourceType` row keeps one slug: the slug of the **first** version
  that registers it **(call)**; nothing may depend on that column for
  routing (it already doesn't, per the follow-up `type-spec-nits`; the builder
  should confirm and record it).
- **Stored JSON types are representation-invariant.** A stored type served by
  several versions must use the same spec (schema/context functions); a
  version may only include or omit it. A system check enforces this.

### 3. Workflow and the canonical state

To keep goal 4 — one state machine whatever the version — a governed model
needs **one canonical state function** that does not depend on any version's
`schema_out`:

- `workflow_check` for a model served by more than one version **must** be
  given an explicit `state_of` (over model fields), or every version's
  `schema_out` must be the same class. A system check fails otherwise
  **(call)**. Single-version use keeps today's `schema_out` default.
- `_GOVERNED[model]` stays one entry per model; registering it from two
  versions with different `state_of`/`contexts_for` is an error, not
  last-wins.
- Policies, `authorized_transitions` grants, `SCRUD_WORKFLOW_ACTIONS`,
  signals and the outbox are version-agnostic. **(call)** Signals and
  `WorkflowDecision` gain the serving version label, for audit only.
- ETags are per representation: each version's own-model router supplies its
  own `etag_func`, so `/v1/articles/7` and `/v2/articles/7` have different
  ETags; a write through one version invalidates the other's `If-Match`.
  Stored types have one representation and therefore one ETag in every
  version.

### 3a. RBAC conditions and contexts across versions (owner: rule now, mapping later)

scoped-rbac evaluates a write's grant conditions against the **raw request
body** (`authorize_write(..., access.body_mapping(content))`, `rbac_load` /
`authorized_object` with `resource=` the body) and reads the target
contexts from it (`contexts_from(content)`). Those see the version's own
shape, so a field renamed or nested in v2 could make one grant answer
differently by version.

**Rule (owner, option b):** across every version that serves a type,
`rbac_context` and every body field that the type's grant conditions read
keep the **same name, position and meaning**. A version may change any
other part of the representation.

- **(call)** A system check verifies what it can: for an own-model type
  served by several versions, each version's input schema has `rbac_context`
  at the same path with the same type. Fields read inside CEL conditions are
  not checked statically (grants are data); the README states the rule and
  the deferred follow-up below records the gap.
- Tests: a conditional grant on a body field allows/denies identically
  through v1 and v2 for a type whose other fields differ between them.

**Deferred (owner, option a):** a per-version mapping from the version's
body to one canonical form, against which conditions and contexts are
evaluated — removes the rule above but needs a scoped-rbac API change. The
builder records it in `docs/follow-ups.md` as `versioned-rbac-canonical-body`
with the test that would expose the gap (a v2 that renames a
condition-read field, the same grant answering differently).

### 4. Links stay in their version

- **One source of truth for "which API am I in"**: when `api()` builds a
  version it records the version label; every route it builds is noted
  under that label (replacing the short-namespace `_api_routes` key). At
  request time the serving API is found from `request.resolver_match`'s
  **full** namespace mapped back to the label, so outer namespaces work.
- **Without a request** (HSR description, UI reverse at render time, system
  checks), link builders receive the version explicitly — through the
  router factory's `ctx` and the per-version router closures — and reverse
  with the version's **full** namespace. The full namespace is learned once,
  lazily, from the URLconf (the first reverse/resolve of the version's
  `api-root`), not guessed from the short name. If a version is included
  twice, a system check errors (extends E010).
- The silent fallback to a bare global name and to "every noted namespace"
  is removed for versioned APIs: a failed reverse within a version is an
  error (logged and 500 in DEBUG; a system check catches statically
  reversible cases) **(call)**, closing follow-ups `ninja-hsr-link-targets`
  and `ninja-namespace-fallback-logging` for ninja.
- **Schema and JSON-LD context documents are per version** for computed
  and own-model types **(call, after the builder check)**: each version
  serves the documents generated from its own code (its ninja schemas /
  computed spec) under its own mount, and its `Link` headers point there —
  not at the single stored `ResourceType.schema`/`context` row and not at
  the global `/semantic/` route. No new storage and no migration. Stored
  JSON types keep their one stored schema and context, served under each
  version's mount (same document, version-local URL).
  scoped-rbac's `semantic._generated_schemas[iri]["ninja"]` and its global
  `/semantic/` routes remain for unversioned apps; for a versioned type the
  version-local route wins (this may need a scoped-rbac hook, else a scrud
  wrapper — see Risks).
- **HSR documents are per version.** Because §1 builds routers per version,
  scrudful's per-router cache is naturally per version. `SCRUDFUL["BASE_URL"]`
  stays the process-wide origin; the version prefix comes from reversing, not
  from `BASE_URL`.
- Call sites that must route through this: item `href` (`core/lists.py:567`),
  `Location` (`core/writes.py:1188`), schema/context `Link`
  (`headers.py`, `core/resources.py:492-505`), computed `Link`
  (`core/computed.py:147-160`), workflow-policy URLs in errors
  (`exceptions.py:895-909`), the services catalog (`__init__.py:96-132`),
  scoped-rbac's own-model `detail_iri` (`scoped_rbac/ninja/blocks.py:422-427`
  — needs a matching scoped-rbac change or a scrud wrapper), and the role
  routes' semantic links. Pagination links already use
  `request.build_absolute_uri()` and need only a test.
- scoped-rbac's role routes (`roles/`, `role-assignments/`,
  `user-rbac-policy/`) are mounted in every version **(call)**; their
  semantic schema/context routes stay global via `scoped_rbac.semantic_urls`
  (included once). Their data is version-independent.

### 5. Mount location and the UI

- Any prefix and any outer namespace work for every version (goal 6). The
  docstrings that say `/api/` are corrected.
- **The UI binds to one named version**: a setting, e.g.
  `SCRUD_UI = {"API_VERSION": "v2"}` **(call)**. With exactly one scrud API
  in the process it is inferred; with several and no setting, a system check
  errors. `selection.api_base()`, `in_mount`, `Mount.href`, `cookie_path`,
  `find_mount`, the branch checks, `pages.route_views` and `embed.api_url` /
  `_api_path` / `_resolve` all use that version's full namespace instead of
  "first route that reverses".
- Stored component templates keep `api_url(slug)` / `in_mount(href)` /
  `embed(...)`; they resolve within the UI's version. **(call)** No
  per-template version override in this design.
- Changing `API_VERSION` is a deploy-time decision; stored UI items whose
  `href`s point into the old version are the app's to migrate (the UI's own
  items are served by whichever version the UI binds to, so their routes
  must be present in that version — a system check verifies the UI's types
  are served by it).

### 6. Deprecation and sunset

- `api(..., deprecated=<datetime|None>, sunset=<datetime|None>,
  deprecation_link=<url|None>, sunset_link=<url|None>)` **(call)**.
  `deprecated` must be an aware datetime: RFC 9745 has no "deprecated, no
  date" form **(call, after the builder check)**. `sunset` without
  `deprecated` is a configuration error.
- Every response of that version — success, error, 304, OPTIONS, HSR — gets
  `Deprecation: @<unix-seconds>` (RFC 9745) and, when set,
  `Sunset: <HTTP-date>` (RFC 8594) and the `Link` relations. The version's OpenAPI document marks every operation
  `deprecated: true`.
- Nothing stops serving at the sunset date; turning a version off is
  removing it from the URLconf.

### 7. OpenAPI per version

- Each version keeps its own `docs_url`/`openapi_url`, and its OpenAPI
  `info.version` is the version label. The `api-root` reverse uses the full
  namespace (§4), so docs work under any prefix/outer namespace.
- Operation ids may repeat across versions (separate documents), never
  within one.

### 8. Coexisting with a plain NinjaAPI

An app's own `NinjaAPI` (no scrud types) in the same URLconf must:
- never be noted as a scrud API or picked by the UI;
- not trip E010/E011;
- be able to use scrud's `entry` decorator (`router.add_decorator(entry,
  mode="view")`) for user resolution, CSRF and transactions, as today.

**Its writes to governed models go through the workflow (owner, option a).**
scrud provides a **supported, documented entry point** for a hand-written
route to create, update and delete an instance of a governed model with
the same checks a scrud own-model route applies: RBAC on the target
contexts, the workflow transition derived from the canonical `state_of`
(§3), `authorized_transitions` grants, the decision sent with
`scrud_post_save`, `SCRUD_WORKFLOW_ACTIONS` and the outbox. Today
`ModelWorkflow.save_checked` is public but undocumented for this use and
assumes a scrud router's request plumbing (the `covered`/`covering`
marking); the builder makes the path usable from any ninja route given
the request, the instance (or prior/proposed state) and the type IRI, and
returns or raises the same errors scrud's routes answer, so a `/v0` route
can map them to its legacy response shapes.
- Such saves are checked saves: no "skipped the workflow check" warning.
- Decisions and signals carry the caller's label (e.g. `"v0"`) for audit,
  like the version label in §3.
- Deletes go through the same entry point; they get the RBAC check today
  and the workflow check whenever own-model deletes do (follow-up
  `delete-no-workflow-check` — not in scope here).
- Close follow-up `core-save-checked-mark-body` as part of this, or prove it
  does not apply to the new entry point: the checked body must be the saved
  body.

A test URLconf covers `/v0` (plain), `/v1`, `/v2` (scrud), and the UI; a
`/v0` write that `/v1`'s policy forbids is refused, one it allows succeeds
and fires the same actions.

## Testing expectations

- A test URLconf with `v0` plain ninja, `v1` and `v2` scrud versions, one
  under an outer namespace (`path("x/", include(([path("v2/", v2.urls)],
  "x")))`), and the UI bound to `v2`.
- For every link-bearing surface listed in §4: a request to each version
  produces links only into that version (assert prefixes on headers, bodies,
  HSR documents, error bodies, services catalog).
- One own-model type with different `schema_out` in v1 and v2 and a
  canonical `state_of`: the same workflow policy and grants allow/deny the
  same transitions through both versions; ETags differ; a v2 write makes v1's
  `If-Match` fail with 412.
- A stored type served by both versions: identical content and ETag.
- Carry-forward: a v1 resource not mentioned in v2's declaration is served by
  v2 with v2 links.
- Deprecation headers on every response class of a deprecated version;
  absent on the others.
- System checks: duplicate labels, same slug twice in a version, stored type
  with differing specs across versions, governed model without canonical
  `state_of` across versions, several scrud APIs with no `API_VERSION`.
- The existing unversioned tests pass unchanged (backward compatibility),
  including `two_ninja_apis_urls.py` and the parity harness.
- Run on MySQL 8.4 and PostgreSQL per existing CI.

## Risks

- **ninja internals.** scrud already patches private ninja names
  (`ninja-private-upstream-use`); per-version router building and namespace
  discovery must not add new private dependencies if avoidable. Verify
  router mounting behaviour against the pinned 1.7.3 source before choosing
  between factories-only and accepting router instances.
- **Upstream changes.** scoped-rbac's `detail_iri` and `semantic` registry
  need version awareness (a scoped-rbac release, then a scrud pin bump), or
  a scrud-side wrapper.
- **Per-version semantic documents.** scoped-rbac serves generated schemas
  from one global registry and route; version-local documents need either a
  scoped-rbac hook (a release, then a pin bump) or a scrud-side route that
  shadows it.
- **Global registries.** Re-keying the type registries by version touches
  much of `core/`; keep the unversioned path's keys and behaviour
  byte-for-byte to avoid breaking 0.18 apps.

## Left to the builder

- Parameter and setting names; whether `based_on`/`replace`/`add`/`remove`
  are `api()` kwargs or a small `Version` builder object.
- How the full namespace is discovered (lazy URLconf resolve vs. an explicit
  `include` helper), provided outer namespaces work and no guessing remains.
- Whether the role routes are per version or mounted once (default above:
  per version).
- Release sequencing across scrudful / scoped-rbac / scrud-django and the
  release-notes "Upgrading" entries.
- Documentation: a README section "Versioned APIs" and `docs/ui.md` updates
  for `API_VERSION`.
