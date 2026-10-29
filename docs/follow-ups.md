# Follow-ups

<a id="list-not-rbac-filtered"></a>
## list-not-rbac-filtered: `ResourceViewMixin.list` is not RBAC-filtered

`scrud_django/mixins/view_mixins.py:146` (`ResourceViewMixin.list`) pages
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

<a id="workflow-validator-undefined-state"></a>
## workflow-validator-undefined-state: `WorkflowValidator` uses undefined attributes

`scrud_django/validators.py:93-98` (`WorkflowValidator`) reads and writes
`self.workflow_policies`, which is never defined, and keys it on
`policy.version`, which `Resource` does not have. This predates the
`modernize-uv` branch.

Impact: when a resource type has a stored workflow policy, validating a write
raises `AttributeError`. No test currently reaches this path, so the suite does
not notice. Types that rely only on the default policies are unaffected.

Suggested fix: initialise `self.workflow_policies = {}` in `__init__` and key the
cache on a field `Resource` really has (for example `(policy.id, policy.modified_at)`
or the content hash). Add a test that stores a workflow policy for a type and
writes a resource of that type; it should fail with `AttributeError` first.

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
