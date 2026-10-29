# Follow-ups

<a id="list-not-rbac-filtered"></a>
## list-not-rbac-filtered: `ResourceViewMixin.list` is not RBAC-filtered

`scrud_django.mixins.view_mixins.ResourceViewMixin.list` pages
`Resource.objects.filter(resource_type=...)` directly, never through
`self.filter_queryset()`, so scoped-rbac's `RbacFilter` never runs on it. This
predates the `modernize-uv` branch (same code at `develop`).

Impact: any user who passes the view-level check for `http.GET` on a type's list
(a grant in any context) sees every resource of that type, in every context.
Since the switch to scrudful, detail routes authorize per object through `get_object()`. A
resource can therefore be listed to a user who gets 404 for it on its detail URL.

The same hole exposes stored `WorkflowPolicy` content, CEL conditions included,
of other contexts: `WorkflowPolicy` is a scrud type, so its list is not
filtered either, and a user with `http.GET` on `WorkflowPolicy` in any one
context lists every policy of every context. That makes the 0.12.0 rule that
errors give a stored policy's URL, or a generic error, instead of its content
moot until this is fixed.

**The library is not safe for multi-tenant use until this is fixed.** Do not
rely on RBAC contexts to keep resources, or workflow policies, apart from
users who may list their type.

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

<a id="workflow-validator-defaulted-copy"></a>
## workflow-validator-defaulted-copy: the workflow check reads the schema-defaulted copy of the body

`WorkflowValidator.__call__` (`scrud_django.validators`) reads the
contexts and state from the data DRF passes to validators, which is a copy of
the body that `JsonValidator` has filled with the schema's `default` values.
The view stores `request.data` without them (see
[schema-defaults-not-stored](#schema-defaults-not-stored)). A schema default for
`rbac_context` (or for a property a state condition reads) can therefore make
the workflow check run under a different context or state than the one stored.

Impact: none with the shipped schemas, which give `rbac_context` no default.
A type whose schema does could have its transitions checked in the wrong
context.

Suggested fix: have the validator read the serializer's `initial_data` (the
body as sent, which is what is stored), and test a type whose schema defaults
`rbac_context`.

<a id="workflow-move-in-same-state"></a>
## workflow-move-in-same-state: moving a resource into a context is checked as a transition, not as a creation there

For an update, `WorkflowValidator` checks every context of the stored resource
and of the body as a transition from the stored content
(`WorkflowValidator.__call__` in `scrud_django.validators`). A context the resource enters is
checked the same way, so a state that is not an initial state there can be
carried in. Example: a user who holds `__SAME_STATE__` (but not `finish`) in
"v" and write in "w" moves a "done" ToDo from "w" to "v" without changing it;
"done" → "done" is `__SAME_STATE__` under "v"'s policy, so it is allowed, and
"v" now has a "done" ToDo nobody there finished.

Suggested fix: for the contexts in new − stored, require
`validate_initial_state` and authorize `__INITIALIZE__` there (as a create),
in addition to the transition checks in the stored contexts. Test the move
above (400) and a move of a "not started" ToDo (200).

<a id="workflow-policy-error-detail"></a>
## workflow-policy-error-detail: an invalid WorkflowPolicy is refused without saying why

A `WorkflowPolicy` that fails `WorkflowPolicy`'s checks raises a DRF
`ValidationError({"workflow_policy": [...]})`
(`scrud_django.validators.WorkflowValidator.validate_policy`). scoped-rbac's problem-details handler gives
uniform field errors, so the 400 carries only
`{"workflow_policy": [{"code": "invalid", "message": "This value is not valid."}]}`,
not the reason (for example which condition does not compile).

Suggested fix: raise a `ScrudException` subclass (an RFC 9457 problem with
its own `type` and the `ImproperlyConfigured` message as `detail`), which
`scrud_exception_handler` passes through, and assert the detail in
`tests/test_workflow_contexts.py::test_invalid_policy_is_refused_on_create`.

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

<a id="content-contains-sqlite"></a>
## content-contains-sqlite: `content__contains` is unsupported on SQLite

`scrud_django.views.ResourceViewSetGenericFilter` filters its queryset
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

`ResourceViewMixin.update` and `destroy` (`scrud_django.mixins.view_mixins`)
first call `get_object()`, which scopes the lookup with `RbacFilter`, runs
`check_object_permissions`, and lets scrudful evaluate `If-Match` /
`If-Unmodified-Since` against that object. Then they call
`ResourceRegistration.update` / `delete` (`scrud_django/registration.py`),
which fetches the row again by primary key and writes it inside its own
`transaction.atomic()`. The workflow check (`WorkflowValidator`, a serializer
validator) also runs before the write transaction, against the prior state that
`get_object()` read. The check and the write are not in the same
transaction, and the row is not locked between them.

Impact: a concurrent write that lands between the two can be overwritten even
though the request's `If-Match` named the earlier ETag (a lost update). Worse,
the workflow transition is validated against the prior state read at
`get_object()`, outside the write transaction, so a concurrent change can let a
forbidden transition commit. The reviewer reproduced `canceled` -> `done` that
way: the check saw the earlier state and allowed a transition that the state
actually stored does not permit. A write
that changes the resource's `rbac_context` in that window can also be followed
by a write the caller was only authorized for under the old context. The window
is short, and the tests (SQLite, one request at a time) do not exercise it.

Suggested fix: `select_for_update` the row inside the write transaction and
re-run the workflow check (and the precondition check) on the locked row. In
general, run the lookup, the authorization, the precondition check and
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

<a id="workflow-error-embeds-policy"></a>
## workflow-error-embeds-policy: workflow errors from a default policy embed the whole policy

`scrud_django.exceptions.WorkflowTransitionError.response_data` puts the
policy in every 400 for an invalid state or transition (`policy`, and `detail`
when the error has no message). Since 0.12.0 an error caused by a **stored**
`WorkflowPolicy` gives that resource's absolute URL instead, which RBAC
protects (`tests/test_workflow_contexts.py::test_unauthorized_transition_error_references_stored_policy_by_url`).
An error caused by a settings default policy
(`SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]`) still embeds the policy, CEL
conditions included (`test_default_policy_errors_still_embed_the_policy`).

Impact: any user who may write a resource of a type with a default policy
learns that whole policy, including conditions and transitions for other roles.
It is also a large body for every rejected write.

Suggested fix: give default policies a URL too (for example the type's), and
send it instead; then assert that no workflow error body has CEL in it.

<a id="list-validators-side-channel"></a>
## list-validators-side-channel: list ETag and Last-Modified leak the newest resource

The list routes' `ETag` and `Last-Modified` come from the resource type
(`list_etag_func` / `list_last_modified_func` in
`scrud_django/mixins/view_mixins.py`), and every write copies the written
resource's ETag and time onto its type (`ResourceRegistration.register` and `ResourceRegistration.update` in
`scrud_django.registration`).

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
resource with no context to superusers only.

Impact: through the API, scoped-rbac's request check refuses a body with
`"rbac_context": []` for everyone, superusers included, so a client cannot
create such a resource. Only server code (`ResourceRegistration`) can store one,
and superusers can then read it. It fails closed.

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

`scrud_django.__version__` is set from `version("scrud-django")` in the
package's `__init__`, which raises `PackageNotFoundError` when the package is imported from a source
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
(403). Unconditional grants and the `scrud_workflow` operator are not affected.

Operators are affected too. On a detail route's object check an operator
receives the `Resource` model instance, not a mapping, so an adopter's operator
that reads the body (`resource.get("name")`) raises `AttributeError` (a 500)
on every GET, PUT and DELETE of a detail route its grant conditions. (Where
the workflow check authorizes a transition, operators get the read-only body
mapping `TransitionCheck`, so creates are not affected:
`tests/test_custom_operators.py`.)

The workflow check, which authorizes a `TransitionCheck` in every context of
a write, passes it as a `scoped_rbac.policy.RequestResource` whose CEL fields
are the body, so a CEL condition there reads the body as the request check
does (`tests/test_workflow_edge_cases.py::test_cel_conditioned_post_on_a_workflow_type_reads_the_body`).
Only the object check is affected.

Suggested fix: add `ResourceMixin.to_cel()` returning the content when it is a
mapping (else `{}`), and test a CEL-conditional GET and PUT on a detail route.
For operators, pass the object check a `scoped_rbac.policy.RequestResource`
whose instance is a read-only mapping over the content (as `TransitionCheck`
is), or document that operators get the model instance there; test a
body-reading operator on a detail-route PUT.

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

<a id="policy-recheck-isolation"></a>
## policy-recheck-isolation: the policy uniqueness re-check assumes READ COMMITTED

A `WorkflowPolicy` write takes `lock_workflow_policy_type` and, after its
save, re-checks that it is the only policy for its type in its context
(`ResourceViewMixin.serialized_policy_save`). A writer blocked on the lock
sees the policy the first writer committed only if its re-check reads
committed data: true under PostgreSQL's default READ COMMITTED. Under
PostgreSQL REPEATABLE READ or SERIALIZABLE, or MySQL's default REPEATABLE
READ, the blocked writer's snapshot may predate the other commit, so the
re-check misses the duplicate (PostgreSQL SERIALIZABLE may instead fail the
transaction with a serialization error, a 500).

`stored_policies_for` reads through the write database
(`router.db_for_write(Resource)`), so a read-replica router does not make the
re-check read stale rows
(`tests/test_policy_duplicates.py::test_policy_lookup_reads_the_write_database`).

Impact: on those isolation levels, concurrent creates can still store
duplicates. They fail closed (writes of the type in that context are
`ImproperlyConfigured`) and the `scrud_django.E002` database check reports
them.

Suggested fix: document READ COMMITTED as the supported isolation level, or
re-run the re-check in a fresh transaction (or a `SELECT ... FOR UPDATE` on the
policy rows, which PostgreSQL reads at the latest committed version). Test:
needs PostgreSQL with two connections.

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

<a id="policy-recompiled-per-write"></a>
## policy-recompiled-per-write: a stored policy is compiled again on every write

`scrud_django.workflow.workflow_policy_for` builds a `WorkflowPolicy` from the
stored content (compiling every CEL condition) for each context of each write.
Settings default policies are built once; stored ones are not cached.

Impact: CPU per write grows with the number of conditions and the number of
contexts. Suggested fix: cache the compiled policy by the stored resource's
ETag and modification time, as `validator_for` does for JSON Schema validators.

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
