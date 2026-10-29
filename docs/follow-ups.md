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
