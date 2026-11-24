# API Versioning and Mount Location Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve several versions of a scrud ninja API side by side, each at a location the app chooses, with links, documents, OpenAPI, deprecation headers and the UI each bound to one version, while authorization and workflow stay one per type. A hand-written ninja API writes governed models through the same checks.

**Architecture:** `api(version=...)` records an `ApiVersion` in a neutral registry (`core/versions.py`). It builds every router fresh per version from declarations: specs, plus own-model router *factories* that receive an `ApiContext`. One all-pattern wrapper per version sets the serving label (a ContextVar beside `acting_as`) and adds the deprecation headers.
- **Registries:** the representation-dependent ones are looked up by `(serving label, IRI/slug)`. With no label they fall back to today's unversioned dicts, which stay byte-for-byte as they are.
- **Namespaces:** a version's full namespace (including any outer namespace) is discovered lazily from the URLconf. Within a version, `absolute_reverse` reverses only in that namespace: no bare-name or "every noted namespace" fallback.
- **Workflow:** `govern` keeps one canonical state function per model across versions. `ModelWorkflow.write` is the documented entry point for hand-written routes.

**Tech Stack:** Python 3.11-3.13, Django, django-ninja 1.7.3 (pinned `>=1.7.0,<1.7.4`), scrudful 0.10.1, django-scoped-rbac 0.12.1, safetydance tests, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-api-versioning-and-mount-design.md` (approved 2026-11-24). Read it first: this plan argues from it, and its section numbers (§1-§8) are cited below.

## Global Constraints

- The baseline is 0.18.0 (`origin/develop` @ `19bcbe1`). Do not bump the version. Release notes go under a new `## Unreleased` above `## 0.18.0` in `docs/release-notes.md`, with `### Breaking changes`, `### Added`, `### Changed` and `### Fixed`. Each entry is a `*` bullet with a **bold lead sentence**.
- **Unversioned `api()` is unchanged byte-for-byte:**
  - namespace `scrud`;
  - the same registries (`_OPTIONS`, `_SLUG_IRIS`, `_IRI_SLUGS`, `_api_routes`, `services.services`);
  - the same `absolute_reverse` fallback order.
  - `tests/two_ninja_apis_urls.py`, `tests/test_core_urls.py`, `tests/test_front_end_parity.py` and the whole existing suite pass with no assertion edited.
- Versioning is ninja-only. The DRF adapter is untouched (spec non-goal).
- Domain logic goes in `scrud_django/core/`, with no DRF imports there (`tests/test_core_imports.py`). The ninja and UI adapters call the core.
- Never edit scoped-rbac, scrudful, ninja or safetydance. Work around a gap in scrud and record it in `docs/follow-ups.md` (`<a id="slug"></a>` then `## slug: title`, plus the test that would expose it).
- No new dependency on private ninja names. Override only public `NinjaAPI` methods (`get_openapi_schema`) and the `urls_namespace` attribute. Extending scrud's existing `_get_urls` wrapping (`ninja-private-upstream-use`) is allowed.
- Version labels match `^[a-z0-9][a-z0-9._-]*$`. The default namespace is `f"scrud-{version}"`; an explicit `urls_namespace` wins.
- New check ids, in order: `scrud_django.E014`-`E018` and `scrud_django_ui.E003`. Register them in `scrud_django/apps.py` (`ui/apps.py` for the UI one) with `checks.Tags.urls`, shaped like E010 (`checks.py:711-748`).
- Deprecation header formats:
  - `Deprecation: @<int unix seconds>` (RFC 9745);
  - `Sunset: <IMF-fixdate>` via `django.utils.http.http_date` (RFC 8594);
  - `Link: <url>; rel="deprecation"; type="text/html"` and `<url>; rel="sunset"; type="text/html"`, **appended** to any existing `Link` value with `", "`.
- SQLite, PostgreSQL, MySQL 8.4 and MariaDB 11.4 (CI). The one migration (Task 7) must pass `makemigrations scrud_django scrud_django_outbox --check --dry-run`.
- Test conventions per `CLAUDE.md`:
  - `@pytest.mark.django_db`, then `@test_context`;
  - steps only in `tests/steps.py`;
  - select a URLconf with `@pytest.mark.urls(...)`;
  - never weaken an assertion.
- **Public repo:** examples and tests use generic nouns (articles, posts, notes, todo-lists, tags, comments). Name no product, company or customer.
- **Git:**
  - Work on `feature/api-versioning`, stacked on `docs/api-versioning-design`.
  - Push only to the `github` remote until the owner says otherwise.
  - Never push to `origin` (GitLab), open a merge request, or merge without the owner.
  - Never run two pytest suites at once.

## Review Focus

- **The URLconf imported twice** (pytest `urls` marks, `runserver` autoreload) runs `api(version="v1")` again with the same namespace. It must rebuild, not raise. Only a *different* namespace for a used label raises. Task 2 tests this.
- **A deprecated version's response that already carries `Link`** (schema/context, HSR `describedby`). The deprecation relations are appended, and every earlier relation survives. Task 10 tests a 200 list, a 304 and an HSR document.
- **A version nested two levels deep** (`a:b:scrud-v2`). Serving detection, links, HSR documents and OpenAPI docs all resolve, including when the docs page is the process's first request. Tasks 1, 4 and 11 test this.
- **A stored type served at `todos` in v1 and `todo-items` in v2.** The `ResourceType` row keeps `todos`. v2's `href`, `Location`, catalog entry and the UI bound to v2 all use `todo-items`. Task 3 tests this.
- **A v2 own-model whose `schema_out` omits a field the canonical `state_of` reads.** The same transition is authorized through v1, v2 and v0. Task 6 tests this.

---

## Part A: Declaring and identifying versions

### Task 0: Branch setup and commit the plan (controller, with the owner)

- [ ] Rebase `docs/api-versioning-design` onto `origin/develop` (v0.18.0), keeping the commits' authors (the owner's decision, 2026-11-24), then force-push the branch to `github`. It is a fork-only branch.
- [ ] Commit this plan and the spec's approved status line on that branch, then push to `github`.
- [ ] `git switch -c feature/api-versioning` off it. Run `uv sync` and confirm the baseline `uv run pytest -q` is green before any task.

### Task 1: The version registry, the serving label and full-namespace discovery

**Files:**
- Create: `scrud_django/core/versions.py`
- Modify: `scrud_django/core/urls.py` (`serving_api_namespace` 104-114; add discovery helpers; move `_namespace_counts` from `checks.py:693-708` here and import it back into `checks.py`)
- Test: `tests/test_core_versions.py` (plain pytest)

**Interfaces:**
- Produces, in `scrud_django.core.versions`:
  - `@dataclass(frozen=True) class ApiVersion`:
    - fields: `label: str`, `urls_namespace: str` (short), `deprecated: datetime | None = None`, `sunset: datetime | None = None`, `deprecation_link: str | None = None`, `sunset_link: str | None = None`;
    - `headers() -> list[tuple[str, str]]`: `Deprecation`, then `Sunset`, then one `("Link", ...)` per set link, or `[]` when not deprecated;
    - `__post_init__` raises `ImproperlyConfigured` for: a label not matching the global pattern; a naive `deprecated` or `sunset`; `sunset` without `deprecated`; `sunset < deprecated`; a link without its date.
  - `register_version(version: ApiVersion) -> None`: re-registering a label with the same `urls_namespace` replaces it. A different namespace raises `ImproperlyConfigured` naming both.
  - `versions() -> Mapping[str, ApiVersion]` (registration order), `version_for_namespace(short: str) -> ApiVersion | None`.
  - `serving_as(label: str | None)` (contextmanager over a ContextVar) and `serving_label() -> str | None`. Mirror `core/signals.py`'s `acting_as` / `acting_user`.
- Produces, in `scrud_django.core.urls`:
  - `full_namespace(short: str) -> str`. It walks `get_resolver().url_patterns` qualifying `outer:inner`, and returns the one routed namespace whose last segment is `short`. It raises `ImproperlyConfigured` if there is none or several.
    - Cache the result keyed by `id(get_resolver())`, so pytest `urls` marks and `override_settings(ROOT_URLCONF=...)` each get a fresh answer.
    - Guard against re-entry: a call made while the URLconf module is still importing returns `short` and caches nothing.
  - `known_full_namespace(short: str) -> str`: the cached answer if there is one, else `short`. It never walks the URLconf.
  - `serving_api_namespace(request)`: matches the last segment of `match.namespace` against the noted names. This fixes nesting for unversioned APIs too (the `ninja-hsr-link-targets` half).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_deprecated_versions_headers():
    v = ApiVersion("v1", "scrud-v1", deprecated=datetime(2027, 1, 1, tzinfo=UTC),
                   sunset=datetime(2027, 6, 30, tzinfo=UTC),
                   deprecation_link="https://example.org/deprecation")
    assert v.headers() == [("Deprecation", "@1798761600"),
                           ("Sunset", "Wed, 30 Jun 2027 00:00:00 GMT"),
                           ("Link", '<https://example.org/deprecation>; rel="deprecation"; type="text/html"')]

@pytest.mark.parametrize("kwargs", [
    {"deprecated": datetime(2027, 1, 1)},                                  # naive
    {"sunset": datetime(2027, 1, 1, tzinfo=UTC)},                          # sunset alone
    {"deprecated": datetime(2027, 2, 1, tzinfo=UTC), "sunset": datetime(2027, 1, 1, tzinfo=UTC)},
    {"sunset_link": "https://example.org/s", "deprecated": datetime(2027, 1, 1, tzinfo=UTC)},
])
def test_a_misconfigured_version_is_refused(kwargs):
    with pytest.raises(ImproperlyConfigured):
        ApiVersion("v1", "scrud-v1", **kwargs)

def test_a_label_keeps_its_namespace(): ...       # same ns replaces; other ns raises
def test_serving_as_nests_and_restores(): ...     # serving_label() None -> "v1" -> "v2" -> "v1" -> None
```

Add URL tests marked `@pytest.mark.urls("tests.nested_api_urls")`. That new URLconf mounts an unversioned `api(urls_namespace="inner", ui_types=False)` at `path("a/", include(([path("b/", include(([path("api/", API.urls)], "b")))], "a")))`.
- `full_namespace("inner") == "a:b:inner"`;
- `known_full_namespace` returns `"inner"` before the first call and `"a:b:inner"` after;
- `serving_api_namespace` of a request resolved at `/a/b/api/services/` is the noted `inner`.

- [ ] **Step 2:** `uv run pytest tests/test_core_versions.py -v`. Expect FAIL (ImportError).
- [ ] **Step 3:** Implement the interfaces above.
- [ ] **Step 4:** `uv run pytest tests/test_core_versions.py tests/test_core_urls.py tests/test_default_urls.py -q`. Expect PASS, with the E010 tests unchanged.
- [ ] **Step 5:** Commit: `feat(core): version registry, serving label and full-namespace discovery`.

### Task 2: `api()` declares versions; router factories; carry-forward

**Files:**
- Create: `scrud_django/ninja/versions.py`
- Modify: `scrud_django/ninja/api.py` (signature 97-103, build steps 172-261), `scrud_django/ninja/routers.py` (`type_router` 115, the closures 418-448 and 532-600), `scrud_django/ninja/computed.py` (`computed_router` 54), `scrud_django/checks.py`, `scrud_django/apps.py`
- Create: `tests/versioned.py` (the specs and factories), `tests/versioned_urls.py`
- Modify: `tests/models.py` (add `Post`)
- Test: `tests/test_versioned_api.py`

**Interfaces:**
- Consumes: Task 1.
- Produces, in `scrud_django.ninja.versions`:
  - `@dataclass(frozen=True) class ApiContext`: `version: str | None`, `urls_namespace: str` (short). Methods:
    - `full_namespace() -> str`, which is `core.urls.full_namespace`;
    - `reverse(name: str, *, args=(), kwargs=None, request=None) -> str`: absolute, and only within this version;
    - `schema_link(type_iri) -> str` and `context_link(type_iri) -> str` (Task 5);
    - `register_documents(type_iri, *, schema, context) -> None` (Task 5).
  - `RouterFactory = Callable[[ApiContext], ninja.Router]`.
  - `@dataclass(frozen=True) class VersionDeclaration`: `types: tuple[TypeSpec, ...]`, `routers: Mapping[str, RouterFactory]`. It is stored on the built API as `scrud_declaration`.
  - `class ScrudNinjaAPI(ninja.NinjaAPI)`:
    - holds `scrud_version: ApiVersion | None` and `scrud_context: ApiContext`;
    - `urls_namespace` is a property with a setter. It returns `known_full_namespace(short)` after first trying `full_namespace(short)` outside URLconf import, so ninja's `api.urls` (last segment) and `get_root_path` (full name) both work.
- Changes `scrud_django.ninja.api` to:

  ```python
  api(*, types=(), urls_namespace=None, ui_types=True, version=None,
      routers=None, based_on=None, add=(), add_routers=None, replace=None, remove=(),
      deprecated=None, sunset=None, deprecation_link=None, sunset_link=None, **ninja_kwargs)
      -> ScrudNinjaAPI
  ```

  - `urls_namespace` defaults to `"scrud"` when `version is None`, else `f"scrud-{version}"`.
  - `version` is passed to `NinjaAPI(version=...)` for OpenAPI `info.version`.
  - `routers` maps a prefix (`"articles/"`) to a `RouterFactory`. A plain `ninja.Router` instance is accepted only when `version is None`; with a version it is a `TypeError`.
  - `based_on` requires `version`. It copies `based_on.scrud_declaration`, then applies `remove`, then `replace`, then `add` / `add_routers`. Keys are slugs (types) or prefixes (routers).
    - `remove` or `replace` of a key the base lacks raises `ImproperlyConfigured`. So does `add` of a key it has.
  - The deprecation kwargs require `version`, and go into the `ApiVersion` (Task 1 validates them).
  - Each declared spec registers with `spec.register(version=version)` (Task 3).
  - `type_router`, `computed_router`, `services_router` and the role routes are built per call. The closures take `ctx: ApiContext` in place of `urls_namespace`, and keep `urls_namespace=` working for the unversioned path.
- Checks:
  - **E014:** a version label routed under two namespaces, or a version not routed at all. Use `versions()` against the routed namespaces.
  - **E015:** within one version, a slug served twice, or one IRI under two slugs.

- [ ] **Step 1: Write the fixtures.**
  - Add `Post(title, body, state="draft", rbac_context)` to `tests/models.py`. The tests app is unmigrated, so no migration is needed.
  - In `tests/versioned.py`:
    - `todo_lists_spec` (a stored type);
    - computed `word_count_v1` and `word_count_v2` (the same IRI, different output fields);
    - `tags_spec` (stored, v2 only);
    - own-model factories `posts_v1(ctx)` and `posts_v2(ctx)` (`PostOutV1` has `title`, `state`; `PostOutV2` has `headline` for `title`, `status` for `state`, and no `body`);
    - `notes_v1(ctx)` and `comments_v2(ctx)`.
  - The `V0` plain `ninja.NinjaAPI(urls_namespace="legacy")` gets its routes in Task 7.
  - In `tests/versioned_urls.py`:
    - `V1 = api(version="v1", types=[todo_lists_spec, word_count_v1], routers={"posts/": posts_v1, "notes/": notes_v1}, docs_url="/docs", openapi_url="/openapi.json")`;
    - `V2 = api(version="v2", based_on=V1, replace={"posts/": posts_v2, "word-count": word_count_v2}, add=[tags_spec], add_routers={"comments/": comments_v2}, remove=["notes/"], docs_url="/docs", openapi_url="/openapi.json")`;
    - `urlpatterns = [path("v0/", V0.urls), path("v1/", V1.urls), path("x/", include(([path("v2/", V2.urls)], "x"))), path("semantic/", include("scoped_rbac.semantic_urls")), path("", include("scrud_django.ui.urls"))]`.
- [ ] **Step 2: Write the failing tests** (`@pytest.mark.urls("tests.versioned_urls")`):
  - `test_each_version_serves_its_own_resources`:
    - v1 serves `notes/` and not `comments/` or `tags/`;
    - v2 serves `comments/` and `tags/` and not `notes/`;
    - the `todo-lists` list answers 200 at `/v1/todo-lists/` and `/x/v2/todo-lists/`.
  - `test_a_carried_forward_resource_is_built_for_the_new_version`: `V2.scrud_declaration.types` contains `todo_lists_spec`, and the v2 router object is not v1's.
  - `test_declaring_against_a_missing_base_key_is_refused`: `remove=["nope/"]`, `replace={"nope/": f}` and `add=[todo_lists_spec]` against V1 each raise `ImproperlyConfigured`.
  - `test_a_router_instance_needs_an_unversioned_api`: `TypeError`.
  - `test_reimporting_a_versioned_urlconf_rebuilds`: `importlib.reload(tests.versioned_urls)` raises nothing, and `versions()["v1"].urls_namespace == "scrud-v1"`.
  - `test_a_second_namespace_for_a_label_is_refused`: `api(version="v1", urls_namespace="other")` raises `ImproperlyConfigured`.
  - The E014 and E015 tests, shaped like `tests/test_default_urls.py:142-228`, with small dedicated URLconfs.
- [ ] **Step 3:** Run them. Expect FAIL.
- [ ] **Step 4:** Implement. Update the comment at `ninja/api.py:192`: ninja clones a router per API, but scrud builds per version because of its link closures.
- [ ] **Step 5:** `uv run pytest tests/test_versioned_api.py tests/test_ninja_api.py tests/test_ninja_routers.py tests/test_core_urls.py -q`. Expect PASS.
- [ ] **Step 6:** Commit: `feat(ninja): versioned api() with declarations, router factories and carry-forward`.

### Task 3: Representation-dependent registries keyed by version

**Files:**
- Modify: `scrud_django/core/types.py` (274-281, 342-494, `requirement_for` 663), `scrud_django/registration.py` (`TypeSpec.register` 1145, `_register_type` 1181-1213, `register_or_defer` 449-531, `register_role_services` 1548, add `get_resource_type_for_iri`), `scrud_django/__init__.py` (`ScrudServices` 73-135), `scrud_django/core/lists.py:567`, `scrud_django/core/writes.py:1188`, `scrud_django/ninja/routers.py` (`_resource_type` 603-615, `collections_router` 338), `scrud_django/checks.py`
- Test: `tests/test_versioned_registries.py`

**Interfaces:**
- Consumes: `serving_label()` (Task 1); `api(version=...)` calling `spec.register(version=...)` (Task 2).
- Produces, in `scrud_django.core.types`:
  - `register_type_options(type_iri, options, *, slug, version=None)`.
  - `type_options(type_iri, *, version=SERVING)` and `type_options_for_slug(slug, *, version=SERVING)`. The sentinel `SERVING` reads `serving_label()`.
    - With a label that has versioned entries, read `_VERSIONED_OPTIONS[(label, iri)]` / `_VERSIONED_SLUG_IRIS[(label, slug)]`.
    - With no label, or a label without an entry for the IRI, read the unversioned dicts.
    - If those lack the IRI too, read the first version that registered it. Stored types are identical across versions (E016), so any version's entry is correct.
  - `slug_for(type_iri, *, version=SERVING) -> str` and `route_name(resource_type, kind: str, *, version=SERVING) -> str`, which returns `f"{slug_for(resource_type.type_uri)}-{kind}"`.
  - The unversioned `_OPTIONS`, `_SLUG_IRIS`, `_IRI_SLUGS` and their rules are untouched. A versioned registration never writes them.
- `TypeSpec.register(*, urls_func=None, version=None)`.
- `ScrudServices.add_service(type_uri, value, *, version=None)`. `catalog(request)` reads the serving label's entries (falling back to unversioned when there is no label).
- `get_resource_type_for_iri(type_uri, using=None) -> ResourceType | None`, with `get_resource_type_for`'s retry semantics. The routers' `_resource_type` and `collections_router` resolve slug → IRI (version-aware) → row.
- Link sites use `route_name(rtype, "detail")`, not `rtype.route_name_detail()`: `core/lists.py:567` and `core/writes.py:1188`. The json-schema and json-ld built-ins in `headers.py` keep theirs.
- The `ResourceType` row keeps the slug of the first registration (spec §2). Record in `docs/follow-ups.md` `type-spec-nits` that routing no longer reads the column.
- **E016:** a stored (non-computed) IRI registered by several versions with `TypeOptions` or schema/context functions that differ.

- [ ] **Step 1: Write the failing tests.**
  - Extend `tests/versioned_urls.py`: v2 serves `todo_lists_spec` under the slug `todo-items` (`replace={"todo-lists": todo_lists_spec.with_slug("todo-items")}`). If `TypeSpec` has no `with_slug`, add `TypeSpec.with_slug(slug) -> TypeSpec`, which copies with a new slug.
  - `test_a_stored_type_keeps_its_row_slug_and_routes_by_version`:
    - `ResourceType.objects.get(type_uri=TODO_LISTS).slug == "todo-lists"`;
    - a v2 POST's `Location` starts with `http://testserver/x/v2/todo-items/`;
    - a v2 list item `href` starts with the same;
    - the v2 `services/` entry for the IRI is `.../x/v2/todo-items/`;
    - v1's are `/v1/todo-lists/`.
  - `test_a_stored_type_is_the_same_resource_in_both_versions`: the same body, and the same `ETag`, at `/v1/todo-lists/<pk>/` and `/x/v2/todo-items/<pk>/`.
  - `test_a_computed_type_computes_per_version`: `word-count` answers v1's fields at `/v1/word-count/` and v2's at `/x/v2/word-count/`.
  - `test_unversioned_registries_are_untouched`: after importing `tests.versioned_urls`, `types._OPTIONS` and `types._SLUG_IRIS` equal a snapshot taken before.
  - The E016 test: two versions registering a stored IRI with different `content_filter` hooks.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q`. The full suite must pass; the unversioned path is unchanged.
- [ ] **Step 5:** Commit: `feat(core): version-keyed type, slug and service registries`.

---

## Part B: Links stay in their version

### Task 4: Strict reversing within a version, HSR under any mount

**Files:**
- Modify: `scrud_django/core/urls.py` (`absolute_reverse` 141-197, `first_absolute_reverse` 200-225), `scrud_django/exceptions.py` (`StoredPolicyReference.url` 895-909), `scrud_django/ninja/routers.py` (describe-mode closures), `scrud_django/checks.py` (extend E010), `docs/follow-ups.md`
- Test: `tests/test_versioned_links.py`

**Interfaces:**
- Consumes: Tasks 1-3; `ApiContext.reverse`.
- `absolute_reverse(...)`, when the namespace resolves to a **versioned** API:
  - The namespace comes from the request's full namespace, else from `namespace=`. A short versioned name is mapped through `full_namespace`.
  - It tries only `f"{full}:{name}"`. On `NoReverseMatch` it logs an ERROR on the `scrud_django.urls` logger naming the version and route, then re-raises.
  - It never falls back to a bare name or another API.
- The unversioned order is unchanged, except that the bare-name fallback now logs at DEBUG, which closes `ninja-namespace-fallback-logging`.
- `first_absolute_reverse` follows the same rule.
- `StoredPolicyReference.url(request=None)` goes through `core.urls.absolute_reverse`, so a policy URL in an error body stays in the version.
- Describe-mode closures (`func(None, None)`) reverse with `ctx.full_namespace()`. This closes `ninja-hsr-link-targets` for ninja; move both follow-ups to `# Done` with this commit's summary.

- [ ] **Step 1: Write the failing tests.** For each version, over v1 and nested v2, assert that every URL starts with that version's prefix (`http://testserver/v1/` or `http://testserver/x/v2/`) and contains no other version's prefix. The surfaces are:
  - the item `href`, `Location`, and list `Link`;
  - the schema and context `Link` of a stored type;
  - the computed `Link`;
  - pagination `next`/`previous` (create 3 items, `?page_size=1`);
  - the `services/` catalog values;
  - the HSR document of the list and item routes (fetch the `describedby` target, then walk every `href` in it);
  - the policy URL in a refused transition's error body;
  - the role routes' item hrefs (scoped-rbac's `detail_iri` follows the request namespace; pin it).

  Add `test_a_route_missing_from_a_version_is_an_error_not_a_neighbour`: with a v2 type whose detail route was removed, `absolute_reverse` under v2 raises `NoReverseMatch` and logs ERROR. It does not return v1's URL.
- [ ] **Step 2:** Run them. Expect FAIL on the nested v2 HSR and the policy URL.
- [ ] **Step 3:** Implement. Extend E010 so a versioned namespace routed twice is reported, as the spec says ("extends E010").
- [ ] **Step 4:** `uv run pytest tests/test_versioned_links.py tests/test_core_urls.py tests/test_exceptions.py tests/test_ninja_routers.py tests/test_core_computed.py -q`. Expect PASS.
- [ ] **Step 5:** Commit: `feat: links and HSR documents stay in the serving version under any mount`.

### Task 5: Version-local schema and context documents

**Files:**
- Modify: `scrud_django/ninja/routers.py` (`collections_router` 300-332; add `type_documents_router(ctx)`), `scrud_django/ninja/api.py` (mount it per version), `scrud_django/ninja/versions.py` (`ApiContext.schema_link` / `context_link` / `register_documents`), `scrud_django/core/computed.py` (`computed_link` 147-163), `scrud_django/ninja/api.py` `RESERVED_SLUGS` (add `"type-documents"`)
- Test: `tests/test_versioned_documents.py`

**Interfaces:**
- **Routes, in versioned APIs only:**
  - `type-documents/{slug}/json-schema/` (url_name `type-json-schema`);
  - `type-documents/{slug}/json-ld/` (url_name `type-json-ld`).
  - They serve the version's generated documents from a per-version registry `(label, iri) -> (schema, context)`. The values are dicts or zero-arg callables. No storage and no migration.
- **Computed specs** register their spec's schema/context functions there, per version.
- **Own-model factories** call `ctx.register_documents(iri, schema=PostOutV2.json_schema, context=...)` and use `ctx.schema_link(iri)` / `ctx.context_link(iri)` in their `@scrudful` `schema_link` / `context_link`.
- In a versioned API, `computed_link` and the own-model links point at these routes. Stored types keep their stored rows, served under each version's `json-schema-detail` / `json-ld-detail` (the same document at a version-local URL).
- Unversioned APIs keep today's links: the stored rows, and scoped-rbac's global `/semantic/`.
- scoped-rbac's role routes keep their global `/semantic/` links (spec §4). Assert that, so the exception is deliberate.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_computed_types_documents_are_its_versions`: v1's `word-count` schema `Link` resolves to a document with v1's properties, and v2's to v2's. The URLs differ only by prefix.
  - `test_an_own_models_documents_are_its_versions`: the same for `posts/`, against `PostOutV1` and `PostOutV2`.
  - `test_the_role_routes_keep_the_global_semantic_documents`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_documents.py tests/test_versioned_links.py -q`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(ninja): per-version schema and JSON-LD documents for computed and own-model types`.

---

## Part C: One workflow and one set of grants across versions

### Task 6: The canonical state across versions

**Files:**
- Modify: `scrud_django/core/transitions.py` (`Governed` 209-217, `govern` 235-267), `scrud_django/ninja/workflow.py` (`workflow_check` 353-388), `scrud_django/checks.py`
- Modify: `tests/versioned.py` (`POST_WORKFLOW`)
- Test: `tests/test_versioned_workflow.py`

**Interfaces:**
- `workflow_check(iri, *, model, schema_out=None, state_of=None, contexts_for=None, version=None) -> ModelWorkflow`. Factories pass `version=ctx.version`.
- `Governed` gains `source: object`: the user's `state_of` callable, or the `schema_out` class.
- `govern(model, iri, *, state_of, contexts_for, source=None, version=None)`:
  - **Unversioned** (`version=None` and no versioned registration of the model): today's last-wins, unchanged.
  - **Versioned:** the first registration governs. A later one with `source` and `contexts_for` equal (`==`) to the first is a no-op. Any other is recorded in `_CONFLICTS[model]` and does **not** replace the first.
- **E017:** a model with a conflict. The message says the model is served by versions X and Y without one explicit `state_of`.
- `ninja/workflow.py`'s docstring (374-375) and `transitions.py:255-257` state the rule.
- `tests/versioned.py`: `POST_WORKFLOW = workflow_check(POST, model=Post, state_of=lambda p: {"state": p.state, "title": p.title}, version=...)`, called from both factories with the same module-level `state_of` and the same `contexts_for`.

- [ ] **Step 1: Write the failing tests.**
  - A stored policy on the `Post` IRI: `draft -> published` is allowed only when `title != ""`.
  - `test_the_same_policy_decides_through_every_version`: the same body through `/v1/posts/<pk>/` (fields `title`, `state`) and `/x/v2/posts/<pk>/` (`headline`, `status`) gets the same status: 200 for an allowed transition, 400 with the same error for a refused one.
  - `test_etags_are_per_representation`: the v1 and v2 `ETag`s for one post differ. A v2 PUT makes v1's previous `If-Match` answer 412.
  - `test_differing_schema_out_without_state_of_is_e017`: a URLconf whose factories use `schema_out=PostOutV1` and `schema_out=PostOutV2` yields `scrud_django.E017`, and `_GOVERNED[Post]` is still the first.
  - `test_an_unversioned_reregistration_still_replaces`: pins today's behaviour.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_workflow.py tests/test_ninja_own_models.py tests/test_core_transitions.py -q`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(workflow): one canonical state per governed model across versions`.

### Task 7: The serving label in decisions, signals and the outbox

**Files:**
- Modify: `scrud_django/core/decisions.py` (`WorkflowDecision` 155-229), `scrud_django/core/transitions.py:143`, `scrud_django/core/workflow.py:464-475` (`_decision`), `scrud_django/core/signals.py` (comment 18-29), the four signal sends (`mixins/resource_mixin.py:152-164,189-197`, `models/resource.py:248`, `ninja/workflow.py` save and delete), `scrud_django/outbox/models.py`, `scrud_django/outbox/receivers.py`
- Create: `scrud_django/outbox/migrations/000N_outboxevent_api_label.py` (generated)
- Modify: `scrud_django/ninja/versions.py` (the version wrapper sets `serving_as(label)`), `scrud_django/ninja/api.py` (install it)
- Test: `tests/test_versioned_workflow.py`, `tests/test_outbox.py`

**Interfaces:**
- `WorkflowDecision.api_label: str | None = None`: a trailing defaulted field, so existing constructors keep working. Both builders fill it from `serving_label()`.
- `scrud_post_save` and `scrud_post_delete` get the keyword `api_label`. `on_transition` actions receive it through `**kwargs`.
- `OutboxEvent.api_label = models.CharField(max_length=64, null=True, blank=True)`, filled by `store_event`.
- `ninja.versions.serve_version(api: ScrudNinjaAPI) -> None` is the all-pattern wrapper, modelled on `errors.answer_natively_when_off` (`ninja/errors.py:125-145`).
  - It wraps every `pattern.callback` of the API in `serving_as(label)`.
  - It is installed in `api()` after `services.as_drf_api_view_function` (`api.py:255`) and **before** `routers.mark_core_routes` (260). Route marks need `mark.view is view` (`core/types.py:418-448`; `tests/test_resource_view_mark.py`).
  - Task 10 adds the headers to the same wrapper.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_writes_signals_and_decision_carry_the_version`: a receiver connected per test sees `api_label == "v1"` for a v1 PUT and `"v2"` for a PUT to the nested v2 (the outer namespace is not part of the label), and `decision.api_label` equals it.
  - `test_an_unversioned_write_has_no_label`: `None`.
  - `test_the_outbox_records_the_label`.
  - `test_the_route_marks_survive_the_version_wrapper`: `route_authorization` of a v1 type route is recorded.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement. Generate the migration with `uv run python manage.py makemigrations scrud_django_outbox`.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_workflow.py tests/test_outbox.py tests/test_resource_view_mark.py tests/test_core_decisions.py -q`, then `uv run python manage.py makemigrations scrud_django scrud_django_outbox --check --dry-run`. Expect PASS and "No changes detected".
- [ ] **Step 5:** Commit: `feat: decisions, signals and outbox events carry the serving API label`.

### Task 8: A hand-written route writes governed models through the checks (§8)

**Files:**
- Modify: `scrud_django/ninja/workflow.py` (`ModelWorkflow`: add `write` and `delete`, fix `save_checked`'s body gap 239-259), `scrud_django/exceptions.py` (add `CheckedBodyMismatch`)
- Modify: `tests/versioned.py` (the `V0` routes, on a router with `router.add_decorator(entry, mode="view")` as §8 allows: `POST /v0/posts/` creates, and `POST /v0/posts/{pk}/` updates, deliberately not PUT; `DELETE /v0/posts/{pk}/`), with each route's error mapped to a legacy body `{"error": <code>, "message": <str>}`
- Modify: `docs/follow-ups.md` (close `core-save-checked-mark-body` for own models; keep it open for `core.writes.save_checked` if that path is not fixed here, and say so)
- Test: `tests/test_versioned_v0.py`

**Interfaces:**
- `ModelWorkflow.write(request, *, instance: Model | None, values: Mapping[str, Any], save: Callable[[], Model], queryset: QuerySet | None = None, api_label: str | None = None) -> Model`.
  - It authorizes as a scrud own-model route does for the operation: a create as POST to the list, an update as PUT to the item, whatever the hand-written route's method.
    - Run the RBAC write check (`authorize_write` over the target contexts read from `values`) and `save_checked` against `copy.copy(request)` with `method` set to `"POST"` or `"PUT"`, inside `covering(...)`.
    - scoped-rbac's post-save recheck reads `request.method` (`scoped_rbac/ninja/blocks.py:903-946`), so the copy carries through.
  - It sets `serving_as(api_label)` when one is given.
  - It raises exactly the exceptions the own-model route in `tests/own_models.py` raises for the same refusals (an RBAC refusal and a workflow refusal), without translating them. The README section lists the classes, and the tests assert them.
- `ModelWorkflow.delete(request, *, instance, delete=None, api_label=None) -> None`: the RBAC DELETE check, then `delete_checked`. No workflow check (`delete-no-workflow-check` is decided).
- `save_checked` now raises `CheckedBodyMismatch` (rolling the transaction back) when `self.state(saved) != decision.proposed`, instead of logging. This is a breaking change; the release notes say so.
- Such saves are checked. No "skipped the workflow check" WARNING is logged.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_v0_write_the_policy_forbids_is_refused`: `POST /v0/posts/<pk>/` with `state=published, title=""` answers the legacy 400 body, and the row is unchanged.
  - `test_a_v0_write_the_policy_allows_fires_the_same_actions`:
    - 200;
    - the `SCRUD_WORKFLOW_ACTIONS` `on_transition["publish"]` action ran once;
    - the decision `checked is True` and `api_label == "v0"`;
    - no WARNING on `scrud_django.workflow`.
  - `test_a_v0_post_update_is_authorized_by_put_grants`: a user with an RBAC grant for PUT and none for POST on the type gets 200.
  - `test_a_v0_write_without_a_grant_is_refused`: the legacy 403 body.
  - `test_a_save_that_saves_other_content_than_checked_is_refused`: a `save` that sets `state="archived"` raises `CheckedBodyMismatch`, and nothing is stored.
  - `test_the_legacy_api_is_no_scrud_api`: `V0`'s namespace is not in `api_namespaces()`, E010/E011 are silent, and the UI never resolves into it.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_v0.py tests/test_ninja_own_models.py tests/test_readme_own_models.py -q`. Expect PASS. Update any existing test that asserted the old mismatch WARNING with a new assertion for the raise, and give the reason in the commit message.
- [ ] **Step 5:** Commit: `feat(workflow): ModelWorkflow.write/delete for hand-written routes; a mismatched checked body is refused`.

### Task 9 (§3a): RBAC conditions read the same fields in every version

**Files:**
- Modify: `scrud_django/checks.py` (E018), `docs/follow-ups.md` (add `versioned-rbac-canonical-body`)
- Test: `tests/test_versioned_rbac.py`

**Interfaces:**
- **E018:** for an own-model IRI served by several versions, each version's registered input schema (`ctx.register_documents` from Task 5, or the factory's ninja input schema) must have `rbac_context` at the same JSON path with the same JSON Schema type. Fields read inside CEL conditions are not checked statically.
- The follow-up `versioned-rbac-canonical-body` records the deferred option (a): a per-version body mapping. Give it the exposing test: a v2 that renames a field a condition reads, with one grant answering differently.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_conditional_grant_answers_alike_in_every_version`: a grant on `Post` writes conditioned on `body.rbac_context == "team-a"`, with v1 and v2 differing in other fields. It allows and denies identically through `/v1/posts/` and `/x/v2/posts/`.
  - `test_a_moved_rbac_context_is_e018`: a v2 input schema nesting `rbac_context` under `meta` yields `scrud_django.E018`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_rbac.py -q`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(checks): rbac_context stays put across versions (E018); record the canonical-body follow-up`.

---

## Part D: Deprecation and OpenAPI

### Task 10: Deprecation and sunset headers on every response (§6)

**Files:**
- Modify: `scrud_django/ninja/versions.py` (`serve_version` adds `ApiVersion.headers()`), `ScrudNinjaAPI.get_openapi_schema`
- Test: `tests/test_versioned_deprecation.py`, with a URLconf `tests/deprecated_urls.py` (v1 deprecated with sunset and both links, v2 not)

**Interfaces:**
- `serve_version` sets `Deprecation` and `Sunset` on the callback's response, and appends the `Link` relations to any existing `Link` (Global Constraints).
- `ScrudNinjaAPI.get_openapi_schema(self, *, path_prefix=None, path_params=None)` calls `super()` and, for a deprecated version, sets `"deprecated": True` on every operation. It uses no private ninja names.
- Not covered, and documented in the README as such: Django's 404 for a path that matches no route of the version, and an exception that escapes as a 500 outside DEBUG.

- [ ] **Step 1: Write the failing tests.** For v1, each response class carries the three headers:
  - a list 200;
  - an item 200;
  - a 201 POST;
  - a 304 (`If-None-Match` with the ETag);
  - a 412;
  - a 404 for an unknown pk;
  - a 405;
  - an OPTIONS 204;
  - the HSR document;
  - `services/`;
  - `openapi.json`.

  Every response that had a `Link` before still contains each of its earlier relations, plus `rel="deprecation"` and `rel="sunset"`. For v2, `Deprecation` is absent. `openapi.json` for v1 marks every operation `deprecated: true`; v2's marks none.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_deprecation.py tests/test_options.py tests/test_conditional.py -q`. Expect PASS.
- [ ] **Step 5:** Commit: `feat(ninja): RFC 9745 Deprecation and RFC 8594 Sunset headers for a deprecated version`.

### Task 11: OpenAPI per version under any mount (§7)

**Files:**
- Test: `tests/test_versioned_openapi.py`. Code changes only if a test fails: the `urls_namespace` property (Task 2) should already make ninja's `get_root_path` work.

- [ ] **Step 1: Write the tests.**
  - `test_each_version_has_its_own_openapi`: `/v1/openapi.json` and `/x/v2/openapi.json` have `info.version` `"v1"` and `"v2"`, and their paths include only that version's resources (`/x/v2/comments/` is in v2 only).
  - `test_the_nested_docs_page_is_the_first_request`: in a fresh resolver (`clear_url_caches()`, then the URLconf), `GET /x/v2/docs` answers 200, and its page references `/x/v2/openapi.json`.
  - `test_operation_ids_are_unique_within_a_version`.
- [ ] **Step 2:** Run them. Fix whatever fails, in `ScrudNinjaAPI`.
- [ ] **Step 3:** `uv run pytest tests/test_versioned_openapi.py -q`. Expect PASS.
- [ ] **Step 4:** Commit: `test(ninja): OpenAPI per version, docs under an outer namespace`.

---

## Part E: The UI binds to one version (§5)

### Task 12: `SCRUD_UI["API_VERSION"]`

**Files:**
- Create: `scrud_django/ui/conf.py`
- Modify: `scrud_django/core/urls.py` (`api_route_name` 64-97 and `api_reverse` 125-138 take `namespace=`), `scrud_django/ui/branches/selection.py` (`api_base` 83-110, `Mount.href`, `in_mount`, `cookie_path`), `scrud_django/ui/branches/middleware.py` (`find_mount` 105-108), `scrud_django/ui/pages.py` (`route_views` 183-204; set `serving_as` per page view), `scrud_django/ui/embed.py` (`api_url` 149-180, `_resolve` 308, `_api_path` 330-333), `scrud_django/ui/studio/answers.py:219-221`, `ui/studio/views.py:442`, `ui/studio/links.py:90`, `scrud_django/ui/checks.py`, `scrud_django/ui/apps.py`, docstrings at `ui/urls.py:7`, `ui/embed.py:29`, `ui/branches/selection.py:84`
- Test: `tests/test_versioned_ui.py`

**Interfaces:**
- `scrud_django.ui.conf`:
  - `ui_settings() -> Mapping` returns `getattr(settings, "SCRUD_UI", None) or {}` (the `outbox/conf.py` pattern).
  - `ui_namespace() -> str | None` returns the full namespace of `SCRUD_UI["API_VERSION"]`.
    - With no setting and exactly one scrud API, use that API's namespace, so today's apps are unaffected.
    - With no setting and several scrud APIs, return `None`; `scrud_django_ui.E003` reports it.
- Every listed UI call site reverses within `ui_namespace()`. `_resolve` accepts only matches in that namespace.
- UI page views run inside `serving_as(<UI's version label>)`, so the registry lookups (Task 3) see the version's specs.
- `find_mount` exempts every routed scrud API base and the `V0` prefix, not just the UI's: any path inside an API's URL prefix.
- **`scrud_django_ui.E003`:** several scrud APIs and no `API_VERSION`; an `API_VERSION` naming no routed version; or a UI type (`ui_type_specs()`) not served by the bound version.

- [ ] **Step 1: Write the failing tests** (`tests.versioned_urls`, `override_settings(SCRUD_UI={"API_VERSION": "v2"})`):
  - `test_the_ui_reads_and_links_through_its_version`: a UI list page for `todo-items` answers 200. Every `href` into the API in its HTML starts with `/x/v2/`.
  - `test_embeds_resolve_in_the_ui_version_only`: `embed._resolve("/v1/todo-lists/<pk>/")` is refused, and the `/x/v2/` path is accepted.
  - `test_a_published_branch_ignores_every_api_prefix`: with a branch published at `/b/`, requests under `/v0/`, `/v1/` and `/x/v2/` do not read the branches (`find_mount` returns `None`).
  - `test_several_apis_without_a_ui_version_is_e003`, `test_a_ui_version_that_is_not_routed_is_e003`.
  - `test_a_single_unversioned_api_needs_no_setting`: the existing UI suite's URLconf, with no setting, gives no E003.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/test_versioned_ui.py -q`, then the full `uv run pytest -q` (the UI suites are large). Expect PASS.
- [ ] **Step 5:** Commit: `feat(ui): bind the UI to one API version with SCRUD_UI["API_VERSION"]`.

---

## Part F: Documentation and verification

### Task 13: Docs, release notes and the full matrix

**Files:**
- Modify: `README.md` (a new `## Versioned APIs` after "Your own models with workflow (ninja)"), `docs/ui.md` ("Enabling the app": `SCRUD_UI["API_VERSION"]`), `docs/release-notes.md`, `docs/follow-ups.md`, `CLAUDE.md` (the architecture paragraph on namespaces and E010/E011: add E014-E018 and the serving label)
- Test: `tests/test_readme_versioned.py`. Like `tests/test_readme_own_models.py`, it runs the README's example code.

- [ ] **Step 1: Write the README section.** Cover:
  - declaring versions (`version`, `based_on`, `add`/`add_routers`/`replace`/`remove`, factories and `ApiContext`);
  - mount locations and outer namespaces;
  - same resource, same rules: the canonical `state_of` and the §3a field rule;
  - per-version documents;
  - deprecation and sunset, with the uncovered 404/500 cases;
  - OpenAPI;
  - "Writing governed models from a hand-written API" (`ModelWorkflow.write` / `delete`, the method rule, the exceptions raised);
  - the `api_label` signal kwarg.
- [ ] **Step 2:** Add `tests/test_readme_versioned.py`, which runs the section's example and asserts what it shows.
- [ ] **Step 3: Write the release notes under `## Unreleased`.**
  - **Breaking changes:** `save_checked` raises `CheckedBodyMismatch`; a versioned API never falls back to another API's route.
  - **Added:** versioned APIs; `ApiContext`; version-local documents; deprecation headers; `SCRUD_UI["API_VERSION"]`; `ModelWorkflow.write` / `delete`; `api_label`; `OutboxEvent.api_label` (migrate); E014-E018 and `scrud_django_ui.E003`.
  - **Fixed:** an API under an outer namespace (`ninja-hsr-link-targets`); the logged bare-name fallback.
- [ ] **Step 4: Run the full verification**, one suite at a time:
  - `uv run pytest -q`
  - `uv run pytest -q --reverse`
  - `SCRUD_TEST_NO_DRF=1 uv run pytest -q`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run python manage.py check`
  - `uv run python manage.py makemigrations scrud_django scrud_django_outbox --check --dry-run`
  - `uv run --group docs sphinx-build -W -b html docs docs/_build`
  - the PostgreSQL and MySQL 8.4 runs from `CLAUDE.md`, in throwaway containers.

  All must pass.
- [ ] **Step 5:** Read the whole branch diff (`git diff origin/develop...`) for any product, company or customer name, per the Global Constraints. There must be none.
- [ ] **Step 6:** Commit: `docs: versioned APIs, UI API_VERSION, release notes`. Push `feature/api-versioning` to `github` and report to the owner. No merge request until they ask.
