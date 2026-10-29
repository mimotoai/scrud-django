# Follow-ups

<a id="list-not-rbac-filtered"></a>
## list-not-rbac-filtered: `ResourceViewMixin.list` is not RBAC-filtered

`scrud_django/mixins/view_mixins.py:146` (`ResourceViewMixin.list`) pages
`Resource.objects.filter(resource_type=...)` directly, never through
`self.filter_queryset()`, so scoped-rbac's `RbacFilter` never runs on it. This
predates the `modernize-uv` branch (same code at `develop`).

Impact: any user who passes the view-level check for `http.GET` on a type's list
(a grant in any context) sees every resource of that type, in every context.
Since Task 7, detail routes authorize per object through `get_object()`. A
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
