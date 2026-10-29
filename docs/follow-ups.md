# Follow-ups

<a id="list-not-rbac-filtered"></a>
## list-not-rbac-filtered: `ResourceViewMixin.list` is not RBAC-filtered

`scrud_django/mixins/view_mixins.py:204` (`ResourceViewMixin.list`) pages
`Resource.objects.filter(resource_type=...)` directly, never through
`self.filter_queryset()`, so scoped-rbac's `RbacFilter` never runs on it. This
predates the `modernize-uv` branch (same code at `develop`).

Impact: any user who passes the view-level check for `http.GET` on a type's list
(a grant in any context) sees every resource of that type, in every context.
Since the switch to scrudful, detail routes authorize per object through `get_object()`. A
resource can therefore be listed to a user who gets 404 for it on its detail URL.

Suggested fix: build the list from
`self.filter_queryset(self.get_queryset())`, as DRF's `ListModelMixin` does,
so that `RbacFilter` → `access.filter_authorized` scopes it. `Resource` has no
`rbac_context` column (its contexts come from `ResourceMixin.rbac_contexts`), so
the list is filtered per object in Python. Conditional grants are capped by
`SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]` (default 10 000; past it
`ConditionalListTooLarge`, a 400). Decide whether to raise the cap, add an
`rbac_context` column to `Resource`, or paginate before filtering. Add a test in
which a user with GET only in another context lists an empty page.

Fix [list-validators-side-channel](#list-validators-side-channel) at the same
time: once the list is filtered, its `ETag` and `Last-Modified` must not still
come from the newest resource of the type in any context.

<a id="stored-invalid-workflow-policy"></a>
## stored-invalid-workflow-policy: a policy stored before 0.12.0 can still fail every write in its context

Since 0.12.0 a `WorkflowPolicy` is compiled when it is written, and one whose
state conditions are not valid CEL, or whose transitions are malformed or name
undefined states, is refused with 400. A policy stored by an earlier version is
not checked again: `scrud_django.workflow.workflow_policy_for` compiles it when
a resource of its type is written in the policy's context, and the
`ImproperlyConfigured` it raises is a 500 for every create or update of that
type in that context (no longer in other contexts).

Suggested fix: a system check or management command that compiles every stored
`WorkflowPolicy` and reports the invalid ones, run on upgrade. Test: store an
invalid policy with `ResourceRegistration.register` and expect the check to
name it.

<a id="cached-property-called"></a>
## cached-property-called: `SerializerMixin.resource_type` calls a `cached_property` value

`scrud_django/mixins/serializer_mixins.py:71` (`SerializerMixin.resource_type`)
evaluates `super().resource_type()`. `resource_type` is a `cached_property`, so
`super().resource_type` already returns the value and calling it fails. This
predates the `modernize-uv` branch.

Impact: when `_resource_type` is `None`, the fallback branch raises `TypeError`
(or `AttributeError`, depending on the base class). Serializers that set
`_resource_type` are unaffected.

Suggested fix: drop the call parentheses (`super().resource_type`), noting that
`super()` does not work with `cached_property` descriptors on some paths; if it
fails, read the value from the base class explicitly. Add a test with a
serializer whose `_resource_type` is `None`.

<a id="content-contains-sqlite"></a>
## content-contains-sqlite: `content__contains` is unsupported on SQLite

`scrud_django/views.py:55-57` (`ResourceViewSetGenericFilter.list`) filters
with `content__contains=query_filter`. Django's `JSONField` `contains` lookup
raises `NotSupportedError` on SQLite. This predates the `modernize-uv` branch.

Impact: listing through this view with a database that lacks JSON containment
(SQLite, which the test settings use) raises `NotSupportedError`. PostgreSQL
works. No test currently reaches this path.

Suggested fix: on SQLite, filter in Python after fetching, or translate each
query parameter to a `content__<key>=value` key lookup. Add a test that lists
with a query parameter; it should fail with `NotSupportedError` first.

<a id="authorize-write-not-atomic"></a>
## authorize-write-not-atomic: authorization and the write are not one transaction

`ResourceViewMixin.update` and `destroy` (`scrud_django/mixins/view_mixins.py`)
first call `get_object()`, which scopes the lookup with `RbacFilter`, runs
`check_object_permissions`, and lets scrudful evaluate `If-Match` /
`If-Unmodified-Since` against that object. Then they call
`ResourceRegistration.update` / `delete` (`scrud_django/registration.py`),
which fetches the row again by primary key and writes it inside its own
`transaction.atomic()`. The check and the write are not in the same
transaction, and the row is not locked between them.

Impact: a concurrent write that lands between the two can be overwritten even
though the request's `If-Match` named the earlier ETag (a lost update). A write
that changes the resource's `rbac_context` in that window can also be followed
by a write the caller was only authorized for under the old context. The window
is short, and the tests (SQLite, one request at a time) do not exercise it.

Suggested fix: run the lookup, the authorization, the precondition check and
the write in one transaction, and lock the row. For example, enable
`ATOMIC_REQUESTS` (or wrap `update`/`destroy` in `transaction.atomic()`), have
`get_queryset()` use `select_for_update()` for PUT and DELETE, and pass the
locked instance to the write instead of looking it up again. SQLite ignores
`select_for_update`, so a test needs PostgreSQL or a deliberately interleaved
write (patch `ResourceRegistration.update` to change the row before it writes,
and expect 412).

<a id="scrudful-errors-not-problem-json"></a>
## scrudful-errors-not-problem-json: scrudful's 412/428 bodies are not problem details

scrud-django's own errors and scoped-rbac's are RFC 7807/9457 problem details
(`application/problem+json`). scrudful answers its own 400, 412 and 428 itself,
before the view runs and without the DRF exception handler, so those bodies are
scrudful's default `{"detail": ...}` served as `application/json`.

Impact: a client that parses problem details gets a different shape for
precondition failures than for every other error.

Suggested fix: in the `Meta` of `ResourceViewMixin` and `GenericViewMixin`
(`scrud_django/mixins/view_mixins.py`), set
`error_body_func = scrudful.problem.problem_details` and
`error = scrudful.entity.Entity(media_type=scrudful.problem.PROBLEM_JSON)`, so
that scrudful serves RFC 9457 bodies as `application/problem+json` (see
scrudful's `rest_framework.scrudful_viewset` docstring). Update
`tests/test_conditional.py::test_failed_if_match_is_412_with_json_body` and the
428 tests to assert the problem details body and Content-Type, and record the
change in the release notes.

<a id="envelope-link-describedby"></a>
## envelope-link-describedby: the envelope `link` field disagrees with the `Link` header

`scrud_django/headers.py:40` (`get_link_header_for`) builds the `link` field of
each envelope (`scrud_django/mixins/serializer_mixins.py`, used in list pages)
with `rel="describedBy"; type="application/json"` for the schema. The `Link`
header that scrudful sends on the same resource uses `rel="describedby"` with
`type="application/schema+json"`.

Impact: a client that reads the schema link from envelopes and from headers
sees two different relation spellings and media types for the same link. Link
relation types compare case-insensitively (RFC 8288), so the `rel` difference
is cosmetic; the media type difference is not.

Suggested fix: use `"describedby"` and `"application/schema+json"` in
`get_link_header_for`, matching scrudful's header, and add a test that compares
an envelope's `link` with the detail response's `Link` header. Envelope
consumers would see a changed value, so note it in the release notes.

<a id="workflow-error-embeds-policy"></a>
## workflow-error-embeds-policy: workflow error bodies embed the whole policy

`scrud_django/exceptions.py:69-77` (`WorkflowTransitionError.response_data`)
puts the full workflow policy, CEL conditions included, in every 400 for an
invalid state or transition (`data["policy"] = self.policy`; the `TODO` above
it meant to send a URL).

Impact: any user who may write a resource of the type learns the complete
policy, including conditions and transitions for other roles. It is also a
large body for every rejected write.

Suggested fix: reference the policy by URL (its `workflow-policies` detail URL,
or the type's for a default policy) instead of embedding it, and add a test
that asserts the error body has no CEL in it.

<a id="list-validators-side-channel"></a>
## list-validators-side-channel: list ETag and Last-Modified leak the newest resource

The list routes' `ETag` and `Last-Modified` come from the resource type
(`list_etag_func` / `list_last_modified_func` in
`scrud_django/mixins/view_mixins.py`), and every write copies the written
resource's ETag and time onto its type (`scrud_django/registration.py:184-185`
and `224-225`).

Impact: a user who may list a type learns the ETag and modification time of
the newest resource of that type in **any** context, and can tell when a
resource they cannot see changes (304 versus 200 with `If-None-Match`). Today
the list is not filtered at all
([list-not-rbac-filtered](#list-not-rbac-filtered)); once it is, this remains.

Suggested fix: fix together with list-not-rbac-filtered: compute the list's
validators from the page the user actually gets (for example a hash of its
items' ETags and their newest `modified_at`). Add a test in which a write to a
resource in a context the lister cannot read leaves their list's `ETag`
unchanged.

<a id="public-type-metadata"></a>
## public-type-metadata: services, collection schemas and contexts are public

`get_service_list`, `ResourceCollectionSchemaView` and
`ResourceCollectionContextView` (`scrud_django/views.py:101-221`) are
`AllowAny`. `services/` lists every registered type; `collections-json-schema/<slug>/`
embeds the type's whole JSON Schema; and both collection views answer 404 for
an unknown slug and 200 for a known one.

Impact: an anonymous client can enumerate every resource type and read every
schema. That may be intended (clients need schemas to build requests), but it
has not been decided.

Suggested fix: decide. If the metadata is public by design, say so in the
README; if not, give these views `IsAuthorized` (or a grant on the type) and
answer an unknown slug the same as a forbidden one.

<a id="demo-settings-not-deployable"></a>
## demo-settings-not-deployable: the demo hard-codes its secret key and DEBUG

`demo/demo/settings.py` has a committed `SECRET_KEY`, `DEBUG = True` and
`ALLOWED_HOSTS = ['*']`. The file now says it must not be deployed.

Impact: none for the library; a deployment copied from the demo would run with
a public secret key and debug pages.

Suggested fix: read these from the environment in the demo (with
development-only defaults), or leave the warning as the only guard.

<a id="write-side-dict-context"></a>
## write-side-dict-context: a dict `rbac_context` in a body is checked as a context

On the read side a non-string `rbac_context` (for example a JSON-LD context
document's `{"@id": ...}` term definition) is not a context: the resource is
in the default context (`rbac_contexts_of`). scoped-rbac's write-side default
`contexts_from` (`access.body_contexts`) instead passes that dict to the policy
as the context to authorize.

The same goes for `"rbac_context": null`: a default-context writer gets 403
for a body with `null` there and 201 when the key is absent.

Impact: only an asymmetry. No role grants a dict context, so the write is
refused unless the user is a superuser, and the post-save check then requires
the default context. It fails closed.

Suggested fix: give `ResourceSerializer` a `contexts_from` that returns
`rbac_contexts_of(body)`, so both sides read the body the same way; add a test
that a default-context writer may POST a JSON-LD context document.

<a id="empty-context-list"></a>
## empty-context-list: `"rbac_context": []` puts a resource in no context

`rbac_contexts_of` reads an empty list as no contexts. scoped-rbac grants a
resource with no context to superusers only, and refuses to write one for
anyone else.

Impact: none beyond superusers being able to create resources nobody else can
see. It fails closed.

Suggested fix: decide whether `[]` should be the default context or a 400, and
test it.

<a id="root-manage-py-memory-db"></a>
## root-manage-py-memory-db: the root `manage.py` uses the in-memory test database

`manage.py` at the repository root configures `tests.settings.SETTINGS`, whose
database is SQLite `:memory:`. `migrate` and `runserver` run, but nothing they
write persists. It is only useful for `makemigrations --check`.

Suggested fix: say so in `manage.py`'s docstring, or point it at the demo, and
use `demo/manage.py` for a persistent server (as `CLAUDE.md` describes).

<a id="version-uninstalled"></a>
## version-uninstalled: `scrud_django.__version__` needs the package installed

`scrud_django/__init__.py:12` sets `__version__ = version("scrud-django")`,
which raises `PackageNotFoundError` when the package is imported from a source
tree that is not installed (for example a checkout on `PYTHONPATH`).

Suggested fix: catch `PackageNotFoundError` and fall back to `"0+unknown"`, and
test importing with the distribution metadata missing.

<a id="resource-no-to-cel"></a>
## resource-no-to-cel: CEL-conditional grants fail on scrud resources

`Resource` has neither `to_cel()` nor `cel_resource()`. scoped-rbac's object
checks pass the model instance to a grant's CEL condition, and
`scoped_rbac.cel.evaluate` raises `TypeError: CEL data must be a mapping, not
Resource`. The request-level checks read the body (a mapping), so only the
object checks are affected; the post-save check of creates and updates reads
the stored content, so it is not affected either.

Impact: when a role's `http.PUT` grant on a scrud type has a CEL condition,
for example `{"condition": "name == \"x\""}`, every PUT whose body meets it is
a 500. A conditional `http.GET` grant cannot read a detail route
(403). Unconditional grants and operator conditions (`scrud_workflow`) are not
affected.

Suggested fix: add `ResourceMixin.to_cel()` returning the content when it is a
mapping (else `{}`), and test a CEL-conditional GET and PUT on a detail route.

<a id="schema-defaults-not-stored"></a>
## schema-defaults-not-stored: JSON Schema `default` values are never stored

fastjsonschema fills a schema's `default` values into the data it validates,
but DRF runs a serializer's validators on a copy of the body
(`Serializer.run_validators`), and `ResourceSerializer` stores the body itself.
Top-level defaults are therefore validated and dropped; a default nested in an
object the body supplies is shared with the copy, so it is stored.

Impact: inconsistent. A client cannot rely on schema defaults. If defaults are
ever stored (for example by storing the validator's output), a default
`rbac_context` would land a resource in a context other than the one the
request was authorized in; the post-save check added in 0.12.0 then answers
403 (`tests/test_context_smuggling.py::test_schema_default_context_is_not_stored`
documents the current behaviour).

Suggested fix: decide whether defaults are applied. If they are, store the
validator's output and keep the post-save check.

<a id="post-save-side-effects"></a>
## post-save-side-effects: post-save receivers run before the write is checked

`Resource.save()` sends `scrud_post_save` (which runs the configured workflow
actions) inside the write. The post-save context check of creates and updates
(`ResourceViewMixin.save_checked`) runs after it and rolls the database back
on a 403, but an action's effects outside the database (mail, queues, HTTP
calls) are not undone.

The haystack search index is another such effect. The library ships no signal
processor, but haystack's `RealtimeSignalProcessor` (as `tests/settings.py`
configures it) indexes on `post_save` inside the transaction, so a write rolled
back with 403 stays in the search index.

Suggested fix: have actions that leave the database run on
`transaction.on_commit`, and document it for `SCRUD_WORKFLOW` actions. For
search, use a processor that indexes on commit or queues the update.

<a id="empty-json-body"></a>
## empty-json-body: an empty body is read as `{}`

An empty request body sent as `application/json` is read as `{}`.

Impact: a POST creates an empty resource and a PUT wipes the resource's
content; authorization is still checked, in the default context `""`. The
release note "a JSON body that is not an object gets 400" does not cover it.

Suggested fix: answer 400 for an empty body on POST and PUT. Test: PUT with no
body leaves the stored content unchanged.

<a id="post-save-permissions-direct"></a>
## post-save-permissions-direct: the post-save check bypasses `IsAuthorized` overrides

`check_post_save_permissions` (`scrud_django/mixins/view_mixins.py`) calls
`access.may_act_in` directly rather than going through the permission class, so
a custom `IsAuthorized` subclass that overrides permission methods is only
partly honoured by the post-save check.

Suggested fix: route the post-save check through the permission class's own
method (or document that it uses `may_act_in`), and test a subclass that denies
a context.
