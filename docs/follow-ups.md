# Follow-ups

<a id="cached-property-called"></a>
## cached-property-called: `SerializerMixin.resource_type` calls a `cached_property` value

`scrud_django.mixins.serializer_mixins.SerializerMixin.resource_type`
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

`scrud_django.headers.get_link_header_for` builds the `link` field of
each envelope (`scrud_django.mixins.serializer_mixins`, used in list pages)
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

<a id="public-type-metadata"></a>
## public-type-metadata: services, collection schemas and contexts are public

`get_service_list`, `ResourceCollectionSchemaView` and
`ResourceCollectionContextView` (`scrud_django.views`) are
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

<a id="root-manage-py-memory-db"></a>
## root-manage-py-memory-db: the root `manage.py` uses the in-memory test database

`manage.py` at the repository root configures `tests.settings.SETTINGS`, whose
database is SQLite `:memory:`. `migrate` and `runserver` run, but nothing they
write persists. It is only useful for `makemigrations --check`.

Suggested fix: say so in `manage.py`'s docstring, or point it at the demo, and
use `demo/manage.py` for a persistent server (as `CLAUDE.md` describes).

<a id="version-uninstalled"></a>
## version-uninstalled: `scrud_django.__version__` needs the package installed

`scrud_django.__version__` is set from `version("scrud-django")` in the
package's `__init__`, which raises `PackageNotFoundError` when the package is imported from a source
tree that is not installed (for example a checkout on `PYTHONPATH`).

Suggested fix: catch `PackageNotFoundError` and fall back to `"0+unknown"`, and
test importing with the distribution metadata missing.

<a id="resource-no-to-cel"></a>
## resource-no-to-cel: operators get the `Resource` model instance on object checks

`ResourceMixin.to_cel()` (added with the RBAC-filtered lists) gives a CEL
condition the resource's content (`{}` when it is not a mapping), so a
CEL-conditional grant now reads the content on a detail route's object check
and on a list's per-object check alike
(`tests/test_list_rbac.py::test_conditional_grant_lists_matching_resources_and_outright_ones_are_uncapped`
checks a conditional `http.GET` on both). A conditional-only `http.GET` grant
still cannot read a detail route (403): scoped-rbac's request check refuses a
grant it cannot decide without the object, while the list, which checks each
object, shows the matching resources.

Operators are still affected. On a detail route's object check, and on a
list's per-object check, an operator receives the `Resource` model instance,
not a mapping, so an adopter's operator that reads the body
(`resource.get("name")`) raises `AttributeError` (a 500) on every GET, PUT and
DELETE of a detail route, and every list, its grant conditions. (Where the
workflow check authorizes a transition, operators get the read-only body
mapping `TransitionCheck`, so creates are not affected:
`tests/test_custom_operators.py`.)

Suggested fix: pass the object checks a `scoped_rbac.policy.RequestResource`
whose instance is a read-only mapping over the content (as `TransitionCheck`
is), for example through a `ResourceMixin.cel_resource(request)` (which
`scoped_rbac.access.cel_resource`, and so the list filter, reads too), or
document that operators get the model instance there; test a CEL-conditional
PUT and a body-reading operator on a detail-route PUT and on a list.

The post-save check (`ResourceViewMixin.check_post_save_permissions`) asks
each permission's `has_context_permission` with the stored content, a
mapping, in place of the object, so that operators read the content there as
on the request check, and calls `has_object_permission` with the saved
`Resource` only when an `IsAuthorized` subclass overrides it (the default one
would give operators the instance). Once operators get a mapping on object
checks, it can pass the resource itself to both and call
`has_object_permission` unconditionally, as scoped-rbac's own
`check_post_save_permissions` does.

<a id="eager-callable-default-policies"></a>
## eager-callable-default-policies: a callable default that raises fails every type

`SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` is checked per type
(`scrud_django.conf.DefaultWorkflowPolicies`, system check
`scrud_django.E001`), and one invalid default only affects its own type, when
the setting gives policy content (mappings) or `WorkflowPolicy` objects that
were built successfully. A dotted-path function that builds `WorkflowPolicy`
objects eagerly and raises on an invalid one cannot be isolated: the setting
cannot be loaded, E001 reports it, and every type's default fails.

This is documented (the 0.12.0 release notes and
`scrud_django.conf.DefaultWorkflowPolicies`, which recommend mappings), and
`tests/test_default_workflow_policies.py::test_a_callable_that_raises_is_one_error_for_the_setting`
pins it. The test settings' defaults (`tests/workflow_defaults.py`) are
mappings.

Suggested fix, if wanted: accept a mapping of type URI to a function per
policy, called and checked per type.

<a id="delete-no-workflow-check"></a>
## delete-no-workflow-check: DELETE is not checked against the workflow

`ResourceViewMixin.destroy` checks object and request RBAC but runs no
workflow check: a user who may `http.DELETE` a resource may delete it in any
state, whatever `restrict_update` says. This is a design choice (a policy
governs updates), recorded here so it is not mistaken for an oversight.

Suggested fix: if deletes must follow the workflow, add a delete transition to
the policy model and authorize it in `destroy`.

<a id="django-admin-bypass"></a>
## django-admin-bypass: the Django admin registers Resource and bypasses RBAC and workflow

`scrud_django/admin.py` registers `Resource` and `ResourceType` with the Django
admin. Staff who may change them there write resources without scoped-rbac's
checks, workflow validation or JSON Schema validation, and see every context.

Suggested fix: unregister `Resource` (and `ResourceType`) in projects that
expose the admin to non-superusers, or make the admin read-only; decide whether
the library should register them at all.

<a id="policy-grants-are-admin"></a>
## policy-grants-are-admin: CEL cost, ReDoS and JSONPath cost need an administrative grant to exploit

Workflow state conditions are CEL evaluated by `scoped_rbac.cel`; a condition
can be expensive (a regular expression with catastrophic backtracking, a large
comprehension), as can a JSONPath expression in a type's configuration. A
`WorkflowPolicy`, a role's CEL condition or a type's JSON Schema can only be
written by someone with an administrative grant, so this is not reachable by
ordinary writers. There is no cost limit.

Suggested fix: if untrusted users may hold those grants, bound CEL evaluation
(time or cost) and reject patterns that scoped-rbac or `re` cannot run in
bounded time.

<a id="schema-error-echoes-compiler-text"></a>
## schema-error-echoes-compiler-text: a refused JSON Schema's 400 echoes fastjsonschema's message

A JSON Schema written through the API must compile
(`scrud_django.validators.compile_json_schema`); when it does not, the 400
`JsonSchemaInvalidError` detail is "The JSON Schema does not compile: " followed
by fastjsonschema's exception text, which quotes parts of the schema (a refused
`$ref` URI, an invalid keyword value). It is the writer's own schema, so it
reveals nothing they did not send, and no fetched content is included (remote
`$ref`s are refused before any fetch). Recorded only: the text is
fastjsonschema's and may change between its versions. A stored schema that
does not compile gives other writers a generic detail.

<a id="list-last-modified-deletes"></a>
## list-last-modified-deletes: a list's `Last-Modified` does not move on a deletion

A list's `Last-Modified` is the newest `modified_at` of the resources the
requester may list (`ResourceViewMixin.list_last_modified`). Deleting a
resource, or moving it out of the requester's contexts, does not make it
newer; deleting the newest one makes it older. A client that revalidates with
`If-Modified-Since` alone can get 304 for a list that lost resources. The
list's `ETag` (`list_etag`: the page's resources, the count and the query
parameters) does change, and `If-None-Match` takes precedence over
`If-Modified-Since` (RFC 9110 §13.2.2).

Impact: stale list caches for clients that send only `If-Modified-Since`. No
information about unreadable resources leaks either way.

Suggested fix: keep a per-context "last change" stamp (for example on
`ResourceContext`, or a table of deletions per context) and take the newest
over the requester's readable contexts, or send no `Last-Modified` on lists.
Test: delete the newest listed resource and expect a 200 to
`If-Modified-Since` set to the previous `Last-Modified`.

<a id="generic-view-list-validators"></a>
## generic-view-list-validators: `GenericViewMixin`'s list validators are the type's

`scrud_django.mixins.view_mixins.GenericViewMixin` (for models of your own)
still gives its list the resource type's `etag` and `modified_at` as `ETag` and
`Last-Modified`. Writes of your own model do not change them (only
`ResourceRegistration` writes do), so a list served through it can answer 304
after it changed. `ResourceViewMixin`'s lists no longer use them.

Suggested fix: give `GenericViewMixin` the same `list_etag` /
`list_last_modified` over `filter_queryset(get_queryset())` as
`ResourceViewMixin`, or none; test a write followed by `If-None-Match`.

<a id="generic-view-writes-not-locked"></a>
## generic-view-writes-not-locked: `GenericViewMixin` writes are not one locked transaction

`ResourceViewMixin` runs a PUT or DELETE in one transaction with the row
locked (`dispatch`, `get_queryset`), so scrudful's precondition evaluation,
the object check and the write see one row. `GenericViewMixin` (for models of
your own) leaves `update` and `destroy` to the subclass and does neither:
scrudful evaluates `If-Match` against the object `get_object()` read outside
any transaction, so two concurrent writes with the same `If-Match` can both
pass (scrudful's own docstring says so).

Suggested fix: give `GenericViewMixin` the same `dispatch` (one transaction
on the model's write database for PUT, PATCH and DELETE) and a
`select_for_update` in the lookup, or document that subclasses must; test two
interleaved writes as `tests/test_atomic_writes.py` does.

<a id="serializer-update-slug"></a>
## serializer-update-slug: `ResourceSerializer.update` reads a `slug` a resource does not have

`ResourceSerializer.update` calls `ResourceRegistration.update(...,
slug=instance.slug)`, but `Resource` has no `slug`: it raises
`AttributeError`. Nothing in scrud-django calls it (the views write through
`ResourceRegistration.update_locked` / `delete_locked`), so only code that
calls `serializer.save()` on an existing resource hits it.

Suggested fix: write through `ResourceRegistration.update_locked` with the
serializer's (locked) instance, or remove the method. Test: a serializer
given an instance and valid data saves it.

<a id="workflow-check-permissions-direct"></a>
## workflow-check-permissions-direct: the workflow check asks the policy, not the permission class

`WorkflowValidator.allowed` (`scrud_django.validators`) authorizes a
transition in each context with `policy_for(request).should_allow` (and
`access.may_act_on` for a resource in no context), not through the view's
`IsAuthorized` (or subclass). An override of `has_context_permission` is
therefore not asked whether the user may make a transition in a context.

Impact: small. The object check and the post-save check do ask the permission
class, so a write is still refused when it reads or lands in a context the
override denies; only an override that decides by the transition itself (the
`TransitionCheck` it would be given) is not honoured.

Suggested fix: authorize through each `IsAuthorized` leaf's
`has_context_permission(request, view, TransitionCheck(...), context)` when
the serializer context has a view, keeping the policy call for serializers
used without one; test a subclass that refuses one transition.

# Decided

<a id="policy-recheck-isolation"></a>
## policy-recheck-isolation: concurrency control assumes READ COMMITTED

Decision: READ COMMITTED is the supported isolation level; the
`scrud_django.W002` database check warns about any other.

A write blocked on a resource's row lock (a PUT or DELETE) or on the
`WorkflowPolicy` type lock reads, once it gets the lock, what the writer it
waited for committed: its precondition, its workflow check and the
`WorkflowPolicy` uniqueness re-check
(`ResourceViewMixin.serialized_policy_save`) see that write. That holds under
READ COMMITTED, PostgreSQL's default and Django's MySQL backend's default.

Above it, on PostgreSQL 14 (REPEATABLE READ or SERIALIZABLE, set with
`OPTIONS["isolation_level"]`), the second of two concurrent `WorkflowPolicy`
writes that would make a duplicate fails with "could not serialize access
due to concurrent update" when it gets the type lock: a 500, and no
duplicate is stored. Concurrent PUTs or DELETEs of one resource fail the same
way instead of answering 412 or 400. Reading the candidate policies with
`SELECT ... FOR UPDATE` in the re-check changes neither outcome on
PostgreSQL, so the re-check does not do it. On MySQL or MariaDB at REPEATABLE
READ (the server default, which Django's backend overrides unless
`OPTIONS["isolation_level"]` says otherwise), the re-check reads its
snapshot and can miss a concurrent duplicate (not tested here). Duplicates
fail closed (writes of the type in that context are `ImproperlyConfigured`)
and the `scrud_django.E002` database check reports them.

`stored_policies_for` reads through the write database
(`router.db_for_write(Resource)`), so a read-replica router does not make the
re-check read stale rows
(`tests/test_policy_duplicates.py::test_policy_lookup_reads_the_write_database`).
