# django-ninja front end — design

Date: 2026-11-11
Status: builder-checked; awaiting owner approval
Builds on:
- scrudful 0.10.1's `scrudful.ninja` (`ScrudfulRouter`, `scrudful`,
  `scrudful_create`, `scrudful_list`);
- django-scoped-rbac 0.12.1's `scoped_rbac.ninja` (building blocks and
  `crud_router`) and its two-front-end design,
  `django-scoped-rbac/docs/superpowers/specs/2026-10-25-semantic-http-drf-ninja-design.md`;
- `scrud_django.ui` phase 1, branches and the studio (`docs/ui.md`).

Notation: **(owner)** marks a decision the owner made. **(call)** marks a decision
made while writing this, which the owner can overrule. Anything unmarked follows
from the existing code.

## Intent

The owner wants to build scrudful endpoints with django-ninja on top of
scrud-django **(owner)**. Three outcomes:

1. **ninja is an option, and the default,** for scrud-django's built-in resource
   types and for the types an app registers (`registration.json_resource_type`
   and friends) **(owner)**.
2. **DRF stays fully supported, with the same answers** **(owner)**, as
   scoped-rbac does with its two front ends.
3. **Apps can write their own scrudful ninja APIs over their own Django models**
   that authorize with scoped-rbac **and with scrud-django's workflow
   authorization** **(owner)**. Workflow is no longer limited to scrud
   `Resource`s.

Along the way, the owner wants **the htmx UI separated from the API**: no longer
served by content negotiation on `/api/...`, but **mounted at `/` like a regular
Django app**, with the studio's published-branch prefixes serving the other
branches under their prefixes **(owner)**.

**One design, three phases (owner):**

1. **Core.** Extract a framework-neutral core out of the DRF viewsets. The DRF
   answers stay byte-for-byte the same.
2. **UI separation.** Move the UI onto the core, mounted at `/` with its
   prefixes. The API becomes JSON-only.
3. **ninja.** Add a ninja front end on the core and make it the default. Ship
   the toolkit for apps' own ninja APIs, including workflow over their own
   models.

Each phase has its own implementation plan and its own merge requests.

## Goals

- An app that `include`s `scrud_django.urls` gets ninja, with no DRF in the
  request path **(owner: the default switches in a breaking release)**.
- An app that wants DRF includes the DRF URLconf explicitly and gets exactly
  today's answers.
- The same request gets the same answer from either front end. That covers the
  status, headers (ETag, Last-Modified, Link, Allow, Location), the body (list
  envelope, item envelope, problem documents) and the order of checks: 401/403
  before 404 before 412, hidden == missing, workflow before the save. Only the
  differences listed under **Tolerated differences** are allowed.
- Workflow authorization is a reusable, framework-neutral check that runs on:
  - scrud resources, on both front ends;
  - an app's own Django models, served through ninja with scoped-rbac's
    building blocks or its `crud_router`.
- The UI is served at `/` (the `main` branch's UI and any site styling) and
  under each published branch's prefix. It is rendered through the same
  authorization as the API: a page never shows what the API would not serve
  that user.

## Non-goals

- `async def` ninja handlers. scoped-rbac and scrudful are sync-only on ninja.
- Serving the HTML UI from ninja. After phase 2, the UI is neither ninja nor DRF.
- Changing the HTTP semantics of any route beyond what is listed under
  **Breaking changes**.
- New workflow features (named regions #40, the superuser bypass #31, payloads
  #20). They should land on the neutral core rather than on DRF, but they are
  separate work.
- Supporting ninja versions outside scrudful's and scoped-rbac's pin
  (`django-ninja>=1.7.0,<1.7.4`).

## Anti-goals (failure even if it works)

- **The front ends drift.** Any answer that differs between DRF and ninja
  outside the tolerated list is a bug, and the parity suite must catch it.
- **A second copy of the domain logic.** If phase 3 re-implements writes,
  preconditions, list ETags or workflow for ninja instead of calling the
  core, the design has failed.
- **Failing open.**
  - A ninja route that skips a check DRF makes.
  - A UI page that renders content the API would hide.
  - A model API whose workflow check can be bypassed by an endpoint that forgot it.

  Each of these is worse than not shipping. The core must make the safe path the
  easy one: the toolkit's documented routes always include the check.
  Unchecked saves are logged for now; whether to refuse them is issue #56.
- **Editing upstream from this repo.** Changes scoped-rbac or scrudful need are
  made in their own repos and released there. Per CLAUDE.md, scrud-django only
  works around and records them.

## Current state (summary)

These are the findings that shape the design. File references are to
`scrud_django/` unless noted.

- **The domain logic is DRF viewset methods.**
  - `mixins/view_mixins.py`:
    - `LockedWriteMixin`: transactions, the conditional-create lock, refusing
      rewritten OPTIONS.
    - `ListValidatorsMixin`: the list ETag formula (`list_etag`, ~876), list
      preconditions, 404-vs-403, `filter_queryset`.
    - `ResourceViewMixin` (~1071): `checks_workflow`, `write_checks`,
      `get_object`, `check_post_save_permissions`, `save_checked`, `create`,
      `update`, `destroy` (409 `ResourceInUseError`), `list_last_modified`.
  - They key on `self.action`, `get_object()`, `request.data` and serializers.
- **Workflow runs as a DRF serializer validator.**
  - `validators.WorkflowValidator` (~246) is in `ResourceSerializer.Meta.validators`.
  - It reads `context.instance` and `initial_data`, and the view's `IsAuthorized`
    leaves (`rbac_permissions_of(view)`) for the narrowing step.
  - The `scrud_workflow` grant operator (`permissions.authorized_transitions`) is
    true only inside a write bracket (`_checking_write`), and the post-save
    recheck needs `request._scrud_workflow_authorized`.
  - The decision logic itself is neutral: `workflow.WorkflowPolicy`,
    `workflow_policies_for`, `determine_transition`, and `allowed_on`.
- **DRF is needed to import the models.** `ScrudException` subclasses DRF's
  `APIException` (`exceptions.py:31`), and `models` → `resource_mixin` →
  `workflow` → `exceptions`.
- **App customization is DRF-shaped.**
  - Apps subclass `IsAuthorized` and compose the classes with `&`, `|` and `~`
    (`permissions.py`, `tests/urls.py`).
  - The workflow calls each leaf's `has_context_permission`.
  - `viewset_class=` swaps the viewset.
- **The UI is DRF.**
  - `ui.renderer.ScrudHTMLRenderer` is a DRF renderer, inserted by
    `ResourceViewMixin.get_renderers`.
  - `ui/dispatch.py` runs viewsets in-process with forced DRF authentication.
  - The studio reads `Response.data`.
  - The UI resource types are `ResourceViewSet` subclasses.
  - The studio endpoints are `@api_view` functions.
  - `PublishedBranchMiddleware` rewrites `<prefix><slug>/...` into the API's
    resource views.
- **Neutral already:**
  - registration's database side (`ResourceTypeRegistration`,
    `ResourceRegistration`, `register_or_defer` with its `urls_func` seam);
  - `models/`, `scrud_signals.py`, `conf.py`;
  - `validators.compile_json_schema`, and `filters.filter_readable_resources`
    (SQL over `ResourceContext`);
  - `headers.get_schema_uri_for` / `get_context_uri_for` (apart from DRF `reverse`).
- **Upstream ninja support** (scoped-rbac 0.12.1):
  - `crud_router` serves Django model classes only. It has a fixed IRI and no
    extra-check hook.
  - The building blocks (`rbac_load`, `item_check`, `list_check`,
    `checked_list`, `authorize_write`, `save_checked`, `install_error_handlers`,
    `resolve_user`) accept any model or queryset.
  - `authorized_list` checks the list permission on `"<iri>/list"`, while scrud
    grants it on `collection_type_uri_for(iri)` (`ResourceCollection?contents_type=<iri>`).
  - Its `authorized_list` filters on a model `rbac_context` column, while scrud
    filters through `ResourceContext` rows.
- **The list envelopes are nearly equal.**
  - scrud (`paginations.py:43`): `{count, page_count, next, previous, content}`.
  - scrudful's ninja `scrudful_list`: the same keys plus `first` and `last`.
- **Tests.**
  - The suite has 1520 tests, ~1281 of them making HTTP calls (667 in
    `test_ui_*`/`test_studio_*`).
  - They go through safetydance-django `Http` steps (DRF `APIClient`
    underneath), plus `force_authenticate` (~719 uses) and `format="json"`.

## Architecture

```
            ┌─────────────── apps' own ninja APIs (own models) ───────────────┐
            │  scoped_rbac.ninja blocks / crud_router  +  scrud workflow_check │
            └──────────────────────────────┬──────────────────────────────────┘
 /api/ (ninja, default)      /api/ (DRF, opt-in)           /  and /<prefix>/  (UI)
 scrud_django.ninja          scrud_django.drf               scrud_django.ui
   ScrudfulRouter per type     ResourceViewSet (thin)         plain Django views
            │                         │                              │ reads; htmx
            └──────────────┬──────────┴──────────────┬───────────────┘ writes → /api/
                           ▼                         ▼
              scrud_django.core (framework-neutral)
                resources: load / list / create / update / destroy, validators, links
                workflow:  workflow_check, the write bracket
                errors:    neutral exception hierarchy (+ problem bodies)
                           │
              scoped_rbac.access (neutral)   scrud models, registration, filters
```

The adapters own only what is genuinely front-end-specific:
- parsing and negotiation;
- mapping neutral errors to responses;
- wiring the scrudful decorators.

## Phase 1 — the framework-neutral core

**Acceptance: the whole existing suite passes unchanged, apart from tests that
pin moved internals (`test_upstream_drift.py`, imports). No answer changes.
This phase is not breaking and can merge to `develop` on its own.**

### 1.1 Neutral errors
- `scrud_django.exceptions.ScrudException` stops subclassing DRF's
  `APIException`. It keeps `status_code`, `default_code`, and
  `response_data(request)` (the problem body).
- The DRF adapter's `scrud_exception_handler` maps it as today.
- Importing `scrud_django.models` must not import `rest_framework`. A test
  asserts this by importing the models in a subprocess with `rest_framework`
  blocked.
- `drf_error(...)` call sites in neutral code (`workflow`, `validators`) raise
  neutral exceptions or scoped-rbac's `access.AccessDenied` / `ObjectNotFound`.
  Each adapter maps those.

### 1.2 The resource service
A module `scrud_django.core.resources` **(call: name and shape)**. Its functions
take a Django `HttpRequest`, the `ResourceType` (or slug), and already-parsed
inputs, and they return neutral results:

| Function | What it does (moved from) |
|---|---|
| `load_for_read(request, rtype, pk)` | lookup under `filter_readable_resources`, 404-vs-403 (`get_object`, `object_readable`, `check_object_permissions`) |
| `list_queryset(request, rtype, params)` | readable queryset, content filters, ordering, MySQL key-first paging hint (`list_queryset`, `filter_queryset`, `ResourceContentFilter` logic) |
| `list_validators(request, rtype, page, params)` | `list_etag` (same formula), `list_last_modified` incl. removals |
| `check_body(request, body)` | depth / NUL / object checks (`check_body_*`, `check_written_contexts`) |
| `create(request, rtype, body)` | schema validation, write checks, workflow, `ResourceRegistration.register_instance`, post-save recheck, locks (`create`, `create_resource`, `save_checked`) |
| `update(request, rtype, obj, body)` | same, plus the `update_locked` path (`update`) |
| `destroy(request, rtype, obj)` | `delete_locked`, 409 `ResourceInUseError` (`destroy`) |
| `item_validators(obj)`, `item_links(request, rtype, obj)` | ETag, Last-Modified, schema/context Link targets (`ScrudfulMeta`, `headers`) |
| `allowed_methods(request, rtype, obj)` | what OPTIONS' `Allow` lists, decided as today |

- **Transactions and locks** (the PUT/DELETE transaction, the conditional-POST
  collection lock) become context managers in the core. Each adapter enters them
  at the point DRF's `dispatch`/`initial` does today.
- **Link URLs** are built without DRF's `reverse`. The core takes a
  `url_for(route_name, **kwargs)` callable from the adapter, so the routes keep
  their names `<slug>-list`/`<slug>-detail` on every front end.
- **Content filtering** (`ResourceContentFilter`'s `KeyTransform` per query
  parameter) moves into the core. It is opted into per type. On DRF,
  `ResourceViewSetGenericFilter` keeps working. `DjangoFilterBackend` stays
  DRF-only: it is a no-op on `Resource`.

### 1.3 Workflow as a neutral check
A module `scrud_django.core.workflow` **(call)**:

- **`workflow_check(request, *, type_iri, prior, proposed, contexts_before, contexts_after, narrow=())`**
  - Finds the policies of every context involved (`workflow_policies_for`,
    `default_workflow_policy_for`).
  - Determines each transition (initial on a create or on entering a context;
    `determine_transition` otherwise).
  - Authorizes it with `allowed_on`.
  - Hides errors unless the prior is readable.
  - Raises neutral errors and marks the request authorized.
- **`prior` and `proposed`** are plain JSON-like mappings; `None` means a create,
  or a delete when `proposed` is `None`.
- **`narrow`** is a sequence of callables `(request, body, context) -> bool`. It
  replaces reaching into the view's `IsAuthorized` leaves.
- **`authorizing_write(request)`** is the context manager behind today's
  `_checking_write` bracket. Every caller must hold it around its own scoped-rbac
  request and object checks, or the `scrud_workflow` grants deny.
- **On DRF, `WorkflowValidator`** becomes a thin adapter. It builds `prior`,
  `proposed` and the contexts from the serializer context, and passes `narrow`
  from `rbac_permissions_of(view)` (each leaf's `has_context_permission`). It
  keeps today's DRF behaviour exactly.
- **Delete** keeps today's behaviour: no workflow check (follow-up
  `delete-no-workflow-check`). The neutral check only supports `proposed=None`
  so that a later fix lands in one place.

### 1.4 Customization, front-end-neutral
- **Today's customization stays on DRF.** `IsAuthorized` subclasses and their
  `&`/`|`/`~` compositions keep working on DRF unchanged.
- **The neutral equivalent (call)** is a small set of per-type hooks passed at
  registration and honoured by every front end:
  - `checks=`: callables `(request, rtype, body, context) -> bool`, the
    counterpart of `has_context_permission`, which also feed `narrow`;
  - `object_checks=`: callables `(request, rtype, obj) -> bool`;
  - `content_filter=True`: today's `ResourceViewSetGenericFilter`.
- **Choosing between them (call):** on DRF, a type registered with
  `viewset_class=` keeps that viewset. The neutral `TypeSpec` (§3.1) has no
  `viewset_class`, so ninja never sees one.
- **Computed types (call):** a neutral `compute=` callable `(request, key) ->
  content` on `computed_resource_type_spec`. `ComputedResourceViewSet.compute`
  becomes a thin DRF adapter over the same callable. The studio's computed types
  move to `compute=`.
- **The UI and DRF-only authorization (call, fail closed).** UI pages read
  through the core, never the viewset. So in DRF mode, the UI refuses to serve a
  type registered with a custom `viewset_class` or with `IsAuthorized`
  subclasses (a 404 page, and a `scrud_django.ui` system check warning naming
  the type), unless the type also declares the neutral `checks=` and
  `object_checks=`, or `ui_authorization="core"` to say the core's checks are
  complete for it. The UI never shows more than the API, at the cost of making
  such apps opt in.

## Phase 2 — the UI, separated from the API

**Breaking (owner).** It ships in the same release as phase 3, 0.16.0
**(owner)**, so apps migrate once.

### 2.1 The API is JSON only
- Resource routes stop negotiating `text/html`.
  - `ScrudHTMLRenderer` leaves `get_renderers`.
  - `finalize_html`, `note_negotiation`, `check_ui_headers` and `check_mount`
    leave the API views.
- A request to `/api/...` with `Accept: text/html` gets what an API gives for
  that `Accept` today. On DRF that is JSON, or `?format=api` for the browsable
  API (a DRF-only feature); there is no redirect to the UI **(call)**.
- `Vary: Accept` and the shared-ETag concerns go away
  (`ui-json-vary-accept`, `ui-html-shared-etag` follow-ups close).
- The UI's resource types (`ui-components`, `ui-stylesheets`, `ui-css`, `ui-js`,
  `ui-branches`, `ui-branch-versions`, `ui-pushes`) stay API resource types,
  served by whichever front end the API uses. They move onto the core like any
  type. Their custom behaviour (`UIContentValidationMixin`: `create_resource`,
  `update`, `destroy`, `save_checked`, `filter_queryset`) becomes core hooks for
  those types.
- The studio's computed types stay API resources.

### 2.2 The UI app at `/`
- **Mounting.** `scrud_django.ui.urls` is included at the site root by the app
  **(owner: "served from `/` for the app, like a regular Django application")**:

  ```python
  urlpatterns = [
      path("api/", include("scrud_django.urls")),
      path("", include("scrud_django.ui.urls")),
  ]
  ```

  - It serves `<slug>/` and `<slug>/<pk>/` pages for every registered type, the
    UI CSS/JS files, and the studio.
  - It claims only registered slugs and its own segments, like prefixes do today,
    so it does not swallow the app's other URLs.
  - Route names are namespaced (`scrud_ui:<slug>-list`, `scrud_ui:<slug>-detail`)
    **(call)**.
- **Views.** Pages are plain Django views.
  - They read through the core (`load_for_read`, `list_queryset`, the item and
    list validators for their own ETag/304), with Django's session user.
  - Embeds call the core instead of `ui/dispatch.py`'s in-process viewset calls,
    and `dispatch.py` goes away.
- **Writes.**
  - htmx writes keep going to the JSON API (`PUT`/`POST`/`DELETE` on `/api/...`)
    as plain JSON. The rendered HTML carries API URLs for writes and UI URLs for
    navigation. This is already the browser's model ("plain JSON `PUT`s").
  - The studio's form endpoints (`studio/views.py`, `@api_view` with
    `FormParser`) become plain Django views under the UI mount. They call the
    core for their writes, with Django's CSRF protection.
- **Prefixes.**
  - `PublishedBranchMiddleware` maps `<prefix><slug>/...` to the **UI** views
    (not the API views), with the branch's `Mount`, and keeps today's rules:
    - the longest prefix wins;
    - GET on the branch is needed;
    - no trailing-slash redirect;
    - links and htmx requests stay under the prefix.
  - `/` is the `main` branch's UI, the site's normal presentation, like any
    Django app. Other branches stay reachable by prefix and by today's
    `ui-branch` selection, confirmation included **(owner)**.
- **JSON under a prefix goes away (call).** Today `<prefix><slug>/` also serves
  JSON (canonical `/api/` hrefs; item lists default to the prefix's branch).
  After phase 2 a prefix is UI only, and JSON is only at `/api/`, where the
  branch is chosen by `Scrud-UI-Branch` or `ui-branch` as today.

### 2.3 What the UI must keep
These are all re-tested at the new URLs:
- authorization identical to the API's;
- conditional GETs (ETag/304) on pages;
- `no-store` where it is set today;
- safe mode;
- confirmation tokens;
- the studio and the self-hosted studio (workbench URLs move from
  `/api/workbench/...`-style paths to the UI mount).

`seed_todo_demo` prints the new URLs.

## Phase 3 — the ninja front end

**Breaking (owner): `scrud_django.urls` switches to ninja.**

### 3.1 Packaging
- **Dependencies (call):**
  - Base: `django-scoped-rbac[ninja]` and `scrudful[ninja]`, which bring
    `django-ninja>=1.7.0,<1.7.4`.
  - A `drf` extra: `django-scoped-rbac[drf]`, `scrudful[drf]`,
    `djangorestframework` and `django-filter`.
- **Modules:**
  - `scrud_django.ninja`: the API factory and routers.
  - `scrud_django.drf`: the viewsets, the exception handler, and
    `scrud_django.drf.urls`.
  - `scrud_django.views` and `scrud_django.exceptions.scrud_exception_handler`
    remain as import aliases for one release, with a `DeprecationWarning`
    **(call)**.
- **URLconfs:**
  - `scrud_django.urls` is the ninja API **(owner)**.
  - `scrud_django.drf.urls` is today's DRF URLconf, unchanged.
- **Registration (call).** The front end is chosen per project, not per type,
  by how the app builds its URLconf:
  - **The registration API splits in two.**
    - `registration.json_resource_type_spec(...)` and
      `computed_resource_type_spec(...)` return a neutral `TypeSpec`. It holds
      the arguments of today's functions, minus `viewset_class`.
    - The existing `json_resource_type(...)`, `computed_resource_type(...)` and
      `resource_types(...)` keep returning DRF `path()`s, unchanged, as the DRF
      adapter's API. DRF apps' URLconfs keep working as they are.
  - **ninja apps own one `NinjaAPI`, built by scrud.** The app writes
    `api = scrud_django.ninja.api(types=[json_resource_type_spec(...), ...])`,
    then `path("api/", api.urls)`.
    - The factory adds the built-in types, scoped-rbac's role routes, `services/`
      and the collection schema and context views. It calls
      `install_error_handlers` before binding any router.
    - It returns the `NinjaAPI`, so the app can `add_router` its own routers
      (for example its own models, §3.4) before `api.urls` is read.
  - **`scrud_django.urls` becomes `api(types=[]).urls`**, for apps with no types
    of their own. An app with types replaces the include with its own `api(...)`.
    Two scrud `NinjaAPI`s in one project need distinct `urls_namespace`s.
  - **`scrud_django.drf.urls`** is today's `scrud_django.urls`.
  - **Slug registry.** Registration, which runs at URLconf import on either
    front end, also records each slug in a process-wide registry. The UI at `/`
    claims the slugs in that registry.

### 3.2 The API
- **One `NinjaAPI`** **(call)**, built by `scrud_django.ninja.api()`:
  - `install_error_handlers(api)` is called before any router is bound. scrud's
    `ScrudException` handler is registered alongside, producing the same problem
    bodies as DRF.
  - scoped-rbac's role routes (`scoped_rbac.ninja.api.router()`) are mounted
    where DRF mounts `scoped_rbac.urls`.
  - `services/` and the collection schema and context views (`@described`
    operations) are included.
- **One `ScrudfulRouter` per type slug** (built-in, registered, computed and UI
  types), built from scoped-rbac's building blocks and the core:
  - **Item routes:** `@scrudful(load=…, etag_func=…, last_modified_func=…, before=…)`.
    - `load` is the core's `load_for_read`. It is side-effect free, as scrudful's
      OPTIONS requires.
    - `before` is `item_check`'s, with scrud's type IRI.
    - Handlers read the body with `request_json`, never as a typed parameter.
  - **List routes:** `scrudful_list(...)` with `list_check`-style hooks.
    - The list permission is checked on `collection_type_uri_for(iri)`, not
      `"<iri>/list"`. This needs a scoped-rbac change (§3.5).
    - Filtering goes through `filter_readable_resources`, not scoped-rbac's
      column-based `authorized_list`.
    - The list ETag is the core's `list_validators`.
  - **Create:** `scrudful_create(location_func=…, list_load=…)`, with the
    conditional-create lock taken as DRF takes it.
  - **OPTIONS:** an `allowed=` hook that calls the core's `allowed_methods`, so
    `Allow` matches DRF.
- **The type is looked up per request by slug,** as DRF does. Deferred
  registration (`register_or_defer`) works as today: routers are built at
  URLconf import, and types are read when requests arrive.
- **The list envelope matches scrudful's on both front ends (call).**
  - The DRF paginator adds `first` and `last`.
  - Its `previous` for page 1 becomes `?page=1` (scrudful's `page_envelope`)
    instead of DRF's URL without `page`. This changes an existing DRF answer, so
    it is listed under **Breaking changes**.
  - The MySQL key-first paging stays a core concern under both.
- **The list ETag (call).** scrud's `list_etag` hashes the page's items and the
  query parameters, but `scrudful_list` calls `list_etag_func` with the queryset
  only. The plan is to propose upstream that `list_etag_func` and
  `list_last_modified_func` may take `(request, queryset, page)` (§3.5).
  - Until that lands, scrud's ninja list wraps `scrudful_list`'s handler and
    passes the request's page through a request attribute.
  - The parity corpus pins the ETags either way.
- **Authentication:**
  - Users come from scoped-rbac's `resolve_user`: `SCOPED_RBAC["NINJA_USER"]`,
    else `request.auth`, else the session user. Unsafe methods are CSRF-checked
    for session users.
  - `NinjaAPI(auth=...)` must not be set on scrud's API, or anonymous grants
    never apply. This is documented, and a system check flags it **(call)**.
- **HSR:** each `ScrudfulRouter` serves its own `_hsr` routes. `hsr_urls()` is
  only needed for DRF. `SCRUDFUL["HSR_URL"]` gets per-router `hsr_url=` when
  both front ends are mounted in one project.

### 3.3 Tolerated differences
These are the only differences allowed, the same list as scoped-rbac's:
- the mount prefix;
- `Date`, `Vary`, `WWW-Authenticate`, `Set-Cookie`;
- the HSR link target;
- DRF-only features: `?format=api` (browsable API), format-suffix URLs, and
  `viewset_class=` customization;
- ninja's 422 for invalid `page`/`page_size` query parameters, if scoped-rbac
  tolerates it too. Otherwise those map to DRF's 404/400 **(builder: check what
  scoped-rbac's corpus does and match it)**.

### 3.4 Own models with workflow (the toolkit)
This is for an app's own Django models, served through ninja **(owner)**:

- **`scrud_django.ninja.workflow_check(iri, *, contexts_for, state_of)`**
  returns hooks that compose with scoped-rbac's blocks:
  - a `before`/`save` wrapper that runs `core.workflow.workflow_check` inside
    `authorizing_write`, in the same transaction as `save_checked`;
  - `prior = state_of(stored_obj)` and `proposed = state_of(body applied)`.
- **`state_of` defaults to the output schema's `model_dump(mode="json")` (call).**
  Policies' CEL conditions see the same JSON the API returns.
- **Policies** for a model are stored `WorkflowPolicy` resources whose
  `resource_type` is the model's IRI (`Model.resource_type.iri`), or defaults
  from settings, exactly as for scrud types. The `scrud_django` system checks
  (`E002`/`E003`/`E005`/`W001`) cover them.
- **`crud_router` support** comes once scoped-rbac lets `crud_router` take extra
  checks (§3.5). The documented form is then
  `crud_router(..., checks=[workflow_check(...)])`. Until then the docs show the
  hand-written router built from the blocks.
- **Saves that skip the check are logged, not refused, for now (owner).** A
  governed model saved without the workflow check (an endpoint that forgot it,
  the admin, the shell, a management command) logs the same warning a scrud
  `Resource` logs today (`ResourceMixin.save`, logger `scrud_django.workflow`).
  - The rule for both resources and own models is to be researched together in
    issue #56 **(owner)**.
  - Within a request that did run the check, the post-save recheck still
    requires the request to be marked workflow-authorized, as `recheck_saved`
    does for resources.
- **Transitions fire scrud's signals and workflow actions (owner).** An
  authorized write of an own model sends `scrud_post_save` and runs the
  `SCRUD_WORKFLOW_ACTIONS` entries for its IRI on commit, as a resource write
  does. A delete sends `scrud_post_delete`.
  - **Arguments.** `sender` is the model class and `instance` the model
    object. `resource_type_uri` is the model's IRI. `using` is the write's
    database. `transitions`, `transition_data` and `transition_error` are
    computed from `state_of`, as for resources.
  - **Where they are sent (call).** The toolkit sends them from its save wrapper,
    not from a model mixin, so that apps' models need no scrud base class.
    `scrud_signals` and its action runner (`WorkflowActionCall`) move to the
    neutral core, and nothing in them may assume a `Resource`.
  - **#20.** The workflow-action payload (actor, prior content, actions keyed
    by transition; #20, high priority) is designed against this neutral signal,
    so resources and models get it together.
- **Examples:** a worked example lives in `demo/` (a model with a two-state
  workflow) and in the docs.

### 3.5 Changes needed in django-scoped-rbac
These are made in its own repo. Each merge to its `master` publishes a release,
and these are additive, so patch or minor per its release skill. scrud-django
pins the release.
1. **The list permission is configurable.** `list_check` / `authorized_list`
   take `list_iri=` (default `"<iri>/list"`), and `crud_router` passes it
   through.
2. **Extra checks.** `crud_router(..., checks=[...])` and the building blocks
   accept extra item and write checks, called inside the same transaction and in
   a documented order: after scoped-rbac's request check, before the save.
3. **A public OPTIONS-denial helper** (today's private `_options_denials`), so
   wrapped `before`/`load` hooks answer scrudful's OPTIONS copies correctly.
4. **Stale docs:** `docs/ninja.rst` still says ninja 1.7.0–1.7.1 and `<1.8`.

In scrudful's repo, the same way: `scrudful_list`'s list validator functions may
take the request and the page (§3.2).

## Testing

- **Phase 1:** the existing suite is the oracle, unchanged. Add unit tests for
  `core.resources` and `core.workflow` called without any front end, and the
  "models import without DRF" test.
- **Phase 2:** the UI tests (`test_ui_*`, `test_studio_*`) move to the new URLs
  and the Django test client. Add tests that `/api/...` never answers
  `text/html`, that the page and the API answer the same 403/404 for the same
  user, and the prefix rules.
- **Phase 3:**
  - **Parametrize the generic suites over `FRONT_ENDS = ["drf", "ninja"]`**,
    with per-front-end URLconf fixtures: `test_rest`, `test_conditional*`,
    `test_list_rbac`, `test_workflow_*`, `test_error_media_types`,
    `test_options`.
  - **Auth works on both:** `force_login` (session plus CSRF client) or a
    `NINJA_USER` test hook instead of `force_authenticate`, and
    `django.test.Client`, not ninja's `TestClient`.
  - **safetydance steps:** a safetydance step variant (in `tests/steps.py`) so the
    `Http` steps run on either.
  - **A parity corpus** like scoped-rbac's (`tests/semantic/test_parity.py`):
    both front ends mounted at once, the same exchanges replayed on fresh data,
    and responses compared exactly apart from §3.3.
  - **Toolkit tests:** a model with a workflow, served by a hand-written router
    and, once available, by `crud_router`. They cover:
    - allowed and refused transitions;
    - the warning for an unchecked save;
    - `scrud_post_save`/`scrud_post_delete` and the workflow actions firing on
      commit, and not on rollback;
    - OPTIONS `Allow`.
  - **CI:** all four databases, as today.
- **CI** gains a job running the suite with only the base dependencies (no
  `drf` extra) for the ninja path.

## Breaking changes (the release that ships phases 2 and 3)

- `scrud_django.urls` serves ninja. DRF users include `scrud_django.drf.urls`
  and install `scrud-django[drf]`.
- HTML is no longer served under `/api/`. Mount `scrud_django.ui.urls` at `/`.
  Prefixes serve UI only.
- The `scrud_django.views` and `scrud_django.exceptions.scrud_exception_handler`
  import paths are deprecated aliases.
- The list envelope gains `first` and `last`, and page 1's `previous` link
  carries `?page=1`.
- On DRF, the UI refuses types with DRF-only authorization unless they declare
  the neutral hooks (§1.4).
- ninja apps with their own types build their API with
  `scrud_django.ninja.api(types=[...])` instead of `include()`ing
  `registration.resource_types(...)`.
- On ninja, customization is through the neutral hooks, not `IsAuthorized`
  subclasses or `viewset_class=`.

**Versioning (owner): fold it into 0.16.0.**
- 0.16.0 is the unreleased version held on `develop`, and it already holds the
  UI components work.
- All three phases merge to `develop` with no version bump, and 0.16.0's release
  notes gain these breaking changes and a migration section in `README.md`.
- The release stays the owner's decision, through a `develop` → `main` merge
  request.
- **Until phase 3 is merged, `develop` must not be released (call),** because
  phase 2 alone would ship the UI split without ninja. The phase 2 merge request
  says so.

## Left to the builder

- Exact module and function names in the core (the tables above give the shape).
- How the core passes the request: a Django `HttpRequest` with the resolved user
  attached, or a small neutral request object.
- The order of merge requests within each phase, and whether phase 1 lands as
  one MR or several (by module).
- How the DRF adapter is slimmed: whether the mixins stay as wrappers or
  collapse.
- Whether scrud's problem handler for ninja wraps or extends scoped-rbac's
  `install_error_handlers`.
- The demo changes beyond the workflow example and the printed URLs.

## Risks

- **Phase 1 is a large refactor of `view_mixins.py`** (1837 lines) and the UI's
  viewset subclasses. The unchanged suite is the safety net. Keep each extraction
  a move-then-delegate step.
- **ninja is pinned to 1.7.0–1.7.3** by upstream's internal-code digests. A ninja
  upgrade needs scrudful and scoped-rbac releases first.
- **scrud's list check differs from scoped-rbac's** (the list IRI and the
  `ResourceContext` filtering). If §3.5 item 1 is refused upstream, scrud writes
  its own `before`/`after_throttles` for lists from the neutral `access`
  functions.
- **Private upstream APIs** already used (`ScrudfulViewFunc._early_response`,
  `scrudful._django`) need ninja counterparts, or the core must avoid them. Pin
  any that remain in `test_upstream_drift.py`.
