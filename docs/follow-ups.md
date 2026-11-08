# Follow-ups

Open follow-ups come first; the entries under `# Decided` are limitations
kept by a decision, each saying what was decided and why. Record a new known
issue or deferred bug here, above `# Decided`, as a section with an anchor
(`<a id="short-slug"></a>` then `## short-slug: title`).

<a id="mariadb-content-filter-padding"></a>
## mariadb-content-filter-padding: on MariaDB a list's query-parameter filter ignores trailing spaces

`ResourceContentFilter` (the lists of `ResourceViewSetGenericFilter`) keeps
the resources whose content's key equals a query parameter's value with a
JSON key lookup (`KeyTransform`). On MariaDB, Django compares the two
`JSON_EXTRACT` results as text in a collation that pads with spaces, so
`?name=foo` also lists a resource whose `name` is `"foo "` (and `?name=foo%20`
one whose `name` is `"foo"`). Case and accents are compared exactly, and
SQLite, PostgreSQL and MySQL (whose JSON type compares strings exactly) are
not affected. It narrows a list that is already RBAC-filtered, so it shows no
resource the requester may not read. Found in K4 with a throwaway test on
MariaDB 11.4 (`"foo"`, `"foo "`, `"FOO"` stored; `?name=foo` and
`?name=foo%20` each listed `"foo"` and `"foo "`).

Fix: on MariaDB, compare the values as bytes (for instance
`CAST(JSON_UNQUOTE(JSON_EXTRACT(content, path)) AS BINARY)` against the
value's UTF-8 bytes), or re-check the matches in Python. Test: in
`tests/test_rest.py`, store resources named `"foo"` and `"foo "` and assert
that `filtered-partner-profiles-list?name=foo` lists only the first and
`?name=foo%20` only the second; it fails on MariaDB today and passes on the
other databases. The same padding applied to `stored_policies_for`'s
`content__resource_type` lookup, which now re-checks the type in Python.

<a id="type-registration-race"></a>
## type-registration-race: two processes starting at once can each store a new resource type

`ResourceTypeRegistration` (every resource type registered at startup, as
`scrud_django.urls` and `json_resource_type` do) looks the type up by its
`type_uri` and inserts it when there is none, without a lock or a unique
constraint on `type_uri`. Two processes starting together on a database
without the type (a fresh deployment) can both find none and both insert
one: the slug then has two rows, and `get_resource_type_for` takes one of
them (the latest revision, then whichever the database returns first), so
resources may be stored under either. Since 0.14.0 the lookup reads the
write database, not a replica that may lag behind, which narrowed the window
from the replica's lag to the time between the lookup and the insert. Fix:
run the registration in a transaction (`transaction.atomic` on `type_db()`)
that first takes a lock (scoped-rbac's `conditional.lock_collection` of
`ResourceType`, which needs that transaction, or a row of its own) and looks
up again under it, or add a unique constraint on `type_uri` (a migration;
check existing databases for duplicates first) and catch the
`IntegrityError`. Test: on PostgreSQL, with
`@pytest.mark.django_db(transaction=True)` (the threads' connections do not
see the test's transaction, and what they commit must be removed after),
run `ResourceTypeRegistration` for a new type in two threads that both pass
the lookup before either inserts (a barrier in a patched
`QuerySet.__bool__`, waiting after the fetch and only for the `ResourceType`
lookup by `type_uri`), and assert one row for the slug; it fails today.

<a id="json-resource-type-indexing-policy"></a>
## json-resource-type-indexing-policy: `json_resource_type` drops its `indexing_policy_resource`

`registration.json_resource_type` takes an `indexing_policy_resource`
argument but does not pass it to `ResourceTypeRegistration`. A type
registered with one (and no `indexing_policy_func`) has its indexing policy
set to `None` and saved at every start, clearing one stored before, so its
resources are indexed without one. Fix: pass it through. Test: register a
type with `json_resource_type(..., indexing_policy_resource=<a stored
policy>)` and assert the stored type's `indexing_policy` is that resource;
it is `None` today.

<a id="hsr-context-less-types"></a>
## hsr-context-less-types: the HTTP Schema Resource of a type with a schema and no JSON-LD context is a 500

scrudful 0.10 builds each route's HTTP Schema Resource (HSR), which every
OPTIONS links to and every response of the route links after its own Links.
It refuses to build one whose `application/json` content has a schema link
and no RDF context link (`x-rdf-context` would be missing): the GET of that
HSR is a 500 (`scrudful.rest_framework._DataDependentError`), and
`manage.py check` warns `scrudful.W001` for the route. The JSON Schema
resource type (`json-schema`, registered with only a `schema_uri`) is
such a type, as is any type registered with a schema and without
`context_resource`, `context_func` or `context_uri`. Its resources are
served as before (GET, POST, PUT, DELETE, OPTIONS); only the document the
OPTIONS links to fails. Found adopting scrudful 0.10 (scrud-django 0.14.0), in the demo:
`GET /_hsr/json-schema-list` and `GET /_hsr/json-schema-detail`.

Fix: give such types a JSON-LD context (scrud's own, served like the
collection contexts; a decision on the IRIs, which are part of the API's
semantics), or link no schema for them in the HSR and on their responses
alike (scrudful requires the two to agree). Test: in
`tests/test_options.py`, GET `/_hsr/json-schema-list` and expect 200; it
is a 500 today (the test client re-raises it, `_DataDependentError`,
rather than return the 500).

<a id="detail-post-action-preconditions"></a>
## detail-post-action-preconditions: a POST `@action(detail=True)` that writes the item ignores its `If-Match`

A POST to an item, such as `@action(detail=True, methods=["post"])
@scrudful(...) def rename(self, request, slug): return self.update(request,
slug)`, is not evaluated by anyone: the action's `@scrudful` (as
`scrudful.E007` asks for one) has no validators, the nested `update`'s
wrapper is skipped (scrudful evaluates only the outer of two nested
handlers), and a POST is not one of the locked writes
(`LockedWriteMixin.dispatch`: PUT, PATCH, DELETE), so no row lock is taken
either. `If-Match: "stale"` gets 200 and the write happens. Found by the
review of MR !6 (0.14.0); it is as old as `@action` support, not new in
0.14.0. The list's counterpart is refused since 0.14.0
(`LockedWriteMixin.dispatch`).

Fix: treat a POST to an item carrying `If-Match`, `If-None-Match` or
`If-Unmodified-Since` as a locked write (lock the row first), and refuse a response that is no
error whose preconditions nothing evaluated, as for the list. Meanwhile an
action can give its `@scrudful` the item's validators
(`object_validators=True` with `ScrudfulMeta.etag_func` and
`ScrudfulMeta.last_modified_func`), which scrudful evaluates for a POST,
without the row lock. Test: in `tests/test_generic_views.py`, a
`NoteViewSet` subclass with that `rename` action, POST with `If-Match:
"stale"`, and expect 412 (or `ImproperlyConfigured`) with the note
unchanged; it is a 200 today.

<a id="workflow-grant-blocks-leaving"></a>
## workflow-grant-blocks-leaving: a `scrud_workflow` grant refuses moving a resource out of a context without a policy

A PUT that moves a resource out of a context needs no grant there unless a
workflow policy applies there (README). But `WorkflowValidator` runs
`authorize_without_policy` for every context the write concerns, those it
leaves included. In a context without a policy, a writer whose grants there
are conditioned on `scrud_workflow` gets 403, while a writer with no grant
there at all may move the resource out. It fails closed, and it predates
0.13.0. Found in the review of scrud-django#2; scrud-django#4.

Fix: run `authorize_without_policy` only for the contexts the body writes
(`rbac_contexts_of(data)`), not those the update leaves. Test: in
`tests/test_workflow_contexts.py`, a resource stored as `["a", "b"]`
(JSON-LD, no policy in `"b"`), a writer with JSON-LD writes in `"a"` and a
`scrud_workflow` grant in `"b"`, PUTs it with `"rbac_context": "a"`: expect
200, get 403 today.

<a id="conditional-only-detail-reads"></a>
## conditional-only-detail-reads: a reader whose only grant is conditional lists resources but gets 403 on their detail route

A user whose `http.GET` on a type is granted only under a condition (say
`rbac_context != "x"`, in `"*"`) sees the resources the condition allows in
the list, but every detail GET, HEAD and DELETE of the type is a 403 (a PUT
is not: its request check evaluates the condition on the body):
scoped-rbac's request check has no resource to evaluate the condition on,
the decision is undetermined, and it lets an undetermined request through
only where the view defers to its object checks
(`AccessControlledAPIView.defers_to_object_checks`), which scoped-rbac's
viewsets do for `list` alone. It fails closed, and it predates 0.13.0. It
contradicts the README's "a list shows what the detail route shows". Found
in the review of scrud-django#2; scrud-django#3.

Fix: have the scrud viewsets defer `retrieve` (and the writes' object
checks) to their object checks too, once it is shown that nothing produces a
response before those checks run (validators, a 304 or 412, headers from the
object), as `defers_to_object_checks` requires. Test: a reader granted `http.GET` on JSON-LD in `"*"` under
`rbac_context != "x"` (and the list type outright) GETs a resource stored
in `"y"`: expect 200 (and 404 for one in `"x"`); get 403 today.

# Decided

<a id="own-code-reads-stored-content"></a>
## own-code-reads-stored-content: operators and permission classes of your own get the content as stored

Decision: only CEL conditions read a resource as it is in the context they
are evaluated in (`scrud_django.permissions.condition_resource`); an
operator function, and a `has_context_permission` of your own (with its
`super()` call), get the content as stored, or in the workflow check the
body as sent, its `rbac_context` the whole value.

Such code may refuse by every context a resource is in (`"archived" in
rbac_contexts_of(obj)`): giving it the content with one context as its
`rbac_context` would turn that refusal into an allow
(`tests/test_condition_contexts.py::test_an_adopters_has_context_permission_reads_the_stored_content`
and `::test_an_operator_gets_the_stored_content`). So a negated condition
written in a `has_context_permission` of your own, or an operator, on
`rbac_context` reads the whole value, as before 0.13.0; an override reads
each context through its `super()` call by passing
`condition_resource(obj, context)`. Beside a `has_context_permission` of
your own, the scrud views ask the grants themselves, each context read as
the resource is there, on the detail route and in the post-save check, as
the list does (`ListValidatorsMixin.check_grants_in_each_context`), so a
negated CEL condition holds on such views too and the list and the detail
route agree
(`tests/test_condition_contexts.py::test_an_overriding_view_shows_on_its_detail_route_what_it_lists`).

<a id="custom-contexts-conditions"></a>
## custom-contexts-conditions: a context of your own leaves the object as it is

Decision: a condition evaluated in a context that a `contexts_for` of your
own names (an `IsAuthorized` subclass reading, say, a resource's `team`)
reads the object as stored, unless that context is also one of the
resource's `rbac_context` contexts.

`scrud_django.permissions.in_context` sets `rbac_context` to the context a
condition is evaluated in only when the resource is stored there: what
`rbac_context` should read in a context the resource is not stored in is
the view's to decide, not scrud-django's. For such a view, a condition on
`rbac_context` (`rbac_context != "x"`) reads a resource stored as
`["x"]` as the list, as it did before 0.13.0, and the list and the detail
route agree on it
(`tests/test_condition_contexts.py::test_a_view_reading_its_own_contexts_lists_what_its_detail_route_shows`
covers a team that is one of the resource's contexts). Write such
conditions on the fields the view's contexts come from instead.

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

Above it, on PostgreSQL 14 (set with `OPTIONS["isolation_level"]`; probed
with two concurrent writes, the first paused holding its locks while the
second was sent):

- Two PUTs or DELETEs of one resource: at REPEATABLE READ and SERIALIZABLE
  the second fails with "could not serialize access due to concurrent
  update", a 500, instead of a 412 or 400.
- Two POSTs to a list with the same `If-Match`: at REPEATABLE READ both
  create (201), with no error. The transaction's snapshot is taken by its
  first statement, the type lock, before it waits, so the second evaluates
  the list without the first's resource
  (`tests/test_conditional_create.py::test_two_conditional_creates_with_one_etag_give_one_201_and_one_412`
  fails so). At SERIALIZABLE the second fails with "could not serialize
  access due to read/write dependencies among transactions", a 500, and
  creates nothing.
- Two `WorkflowPolicy` creates for one type and context: the same. At
  REPEATABLE READ both are stored (201), a duplicate, with no error: the
  uniqueness re-check (`ResourceViewMixin.serialized_policy_save`) reads a
  snapshot taken before the first committed. At SERIALIZABLE the second fails with the
  read/write dependencies error, a 500, and no duplicate is stored.

Reading the candidate policies with `SELECT ... FOR UPDATE` in the re-check
would not help at REPEATABLE READ: the second's snapshot does not hold the
first's policy, so there is no row to lock. On MySQL or MariaDB at REPEATABLE
READ (the server default, which Django's backend overrides unless
`OPTIONS["isolation_level"]` says otherwise), the re-check reads its
snapshot and can miss a concurrent duplicate (not tested here; nor is
SERIALIZABLE there). Duplicates fail closed (writes of the type in that
context are `ImproperlyConfigured`) and the `scrud_django.E002` database
check reports them.

`stored_policies_for` reads through the write database
(`router.db_for_write(Resource)`), so a read-replica router does not make the
re-check read stale rows
(`tests/test_policy_duplicates.py::test_policy_lookup_reads_the_write_database`).

<a id="list-last-modified-deletes"></a>
## list-last-modified-deletes: a removal moves `Last-Modified` only where it cannot leak

Decision: a list's `Last-Modified` moves when a resource leaves a context in
which the requester may list every resource; where they read only some (a
conditional grant), it does not, and the `ETag` is the validator to use. The
stamps are kept, never pruned.

A resource that is deleted, or saved with content no longer in a context,
stamps that context for its type (`scrud_django.models.ResourceRemoval`), and
`ResourceViewMixin.list_last_modified` takes the newest stamp of the contexts
the requester reads outright (every context for a superuser, every context
not plainly reserved for a grant in every context;
`scrud_django.filters.readable_removal`). A stamp in a context the requester
reads only by a condition could be of a resource the condition refused:
moving `Last-Modified` for it would tell them that a resource they may not
read left, the side channel the per-requester validators closed. So after a
conditionally readable resource is deleted, a client that revalidates with
`If-Modified-Since` alone can get 304; the list's `ETag` (the page's
resources, the count, the query parameters) changes, and `If-None-Match`
takes precedence (RFC 9110 §13.2.2).

The same holds, for the same reason, for a view whose `IsAuthorized`
overrides `contexts_for` (its contexts are not the stamped ones) or
`has_object_permission` or `has_context_permission` (it may have refused the
resource that left), and for `GenericViewMixin` lists (models of your own
have no stamps): a deletion changes their `ETag`, not their `Last-Modified`.
`tests/test_list_removals.py::test_a_removal_a_condition_decides_does_not_move_last_modified`
pins the conditional case.

Possible change, if wanted: store a removed resource's content with its stamp
so that the condition can be evaluated on it, at the cost of keeping deleted
content.

Stamps are not pruned (no command removes them): there is one row per type
and context a resource ever left, which grows with the number of contexts
in use, not with the number of removals. Removing a stamp could move a
list's `Last-Modified` back to before a removal and answer 304 for a list
that lost a resource; a stamp of a context nobody reads anymore costs one
small row.

<a id="public-type-metadata"></a>
## public-type-metadata: services, collection schemas and contexts are public

Decision: resource type metadata is public by design: clients need the
schemas and contexts to build requests, and types are global, not per
context.

`get_service_list`, `ResourceCollectionSchemaView` and
`ResourceCollectionContextView` (`scrud_django.views`) are `AllowAny`.
`services/` lists every registered type; `collections-json-schema/<slug>/`
embeds the type's whole JSON Schema; and both collection views answer 404
for an unknown slug and 200 for a known one. The README says so. Resources
themselves, the schema and context documents among them, stay RBAC-checked
on their own routes.

<a id="delete-no-workflow-check"></a>
## delete-no-workflow-check: DELETE is not checked against the workflow

Decision: a workflow policy governs updates; a DELETE is authorized by RBAC
alone.

`ResourceViewMixin.destroy` checks object and request RBAC but runs no
workflow check: a user who may `http.DELETE` a resource may delete it in any
state, whatever `restrict_update` says. If deletes must follow the workflow,
a delete transition would have to be added to the policy model and
authorized in `destroy`.

<a id="policy-grants-are-admin"></a>
## policy-grants-are-admin: CEL cost, ReDoS and JSONPath cost need an administrative grant to exploit

Decision: no cost limit; writing a `WorkflowPolicy`, a role's condition or a
type's JSON Schema is an administrative grant, and the release notes say to
treat it as one.

Workflow state conditions are CEL evaluated by `scoped_rbac.cel`; a condition
can be expensive (a regular expression with catastrophic backtracking, a large
comprehension), as can a JSONPath expression in a type's configuration. Only
someone with an administrative grant can write them, so this is not reachable
by ordinary writers. If untrusted users may hold those grants, CEL evaluation
would need a time or cost bound, and patterns that scoped-rbac or `re` cannot
run in bounded time would have to be refused.

<a id="policy-defaults-are-fallbacks"></a>
## policy-defaults-are-fallbacks: a stored workflow policy replaces the settings default

Decision: `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` gives a type's
policy only in a context without a stored `WorkflowPolicy` for it; a stored
one replaces it there, and writing one is an administrative grant
([policy-grants-are-admin](#policy-grants-are-admin)).

`scrud_django.workflow.workflow_policies_for` takes a context's stored policy
for the type when there is one (`stored_policies_in`) and the settings
default (`default_workflow_policy_for`) only otherwise; the two are never
combined. A `WorkflowPolicy` write is authorized like any resource's, by
grants on the `WorkflowPolicy` type in the policy's `rbac_context`, and
`WorkflowValidator.validate_policy` checks only that the policy is valid
and the only one for its type there, not the writer's rights on the type it
governs. So whoever may POST (or PUT) a `WorkflowPolicy` in a context can,
with no rights on the governed type, replace the deployment's default for
any type there, loosening it, and rename or redefine transitions, which
changes what every `scrud_workflow` grant in that context allows (a grant's
`authorized_transitions` are names, looked up in whichever policy applies).
Deleting the stored policy restores the default. No deployment-wide workflow
invariant can be enforced through the defaults; the README ("Workflow
policies") says so.

A future "floor" mode could make the default binding: a setting under which
a stored policy may only restrict its type's default (for instance, every
transition it defines must be one the default defines between the same
states, under the same name), checked by `validate_policy` and by the
`scrud_django.E005` database check, or under which a write must pass both
policies. Another option is to require, for a `WorkflowPolicy` write, a
grant on the governed type as well. Neither is planned.

<a id="eager-callable-default-policies"></a>
## eager-callable-default-policies: a callable default that raises fails every type

Decision: kept as documented; give default policies as mappings, which are
checked per type.

`SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` is checked per type
(`scrud_django.conf.DefaultWorkflowPolicies`, system check
`scrud_django.E001`), and one invalid default only affects its own type, when
the setting gives policy content (mappings) or `WorkflowPolicy` objects that
were built successfully. A dotted-path function that builds `WorkflowPolicy`
objects eagerly and raises on an invalid one cannot be isolated: the setting
cannot be loaded, E001 reports it, and every type's default fails. The 0.12.0
release notes and `DefaultWorkflowPolicies` recommend mappings, and
`tests/test_default_workflow_policies.py::test_a_callable_that_raises_is_one_error_for_the_setting`
pins the behaviour. A mapping of type URI to a function per policy, called
and checked per type, would isolate it.

<a id="schema-error-echoes-compiler-text"></a>
## schema-error-echoes-compiler-text: a refused JSON Schema's 400 echoes fastjsonschema's message

Decision: kept; the text is the writer's own schema, reported back to them.

A JSON Schema written through the API must compile
(`scrud_django.validators.compile_json_schema`); when it does not, the 400
`JsonSchemaInvalidError` detail is "The JSON Schema does not compile: " followed
by fastjsonschema's exception text, which quotes parts of the schema (a refused
`$ref` URI, an invalid keyword value). It reveals nothing the writer did not
send, and no fetched content is included (remote `$ref`s are refused before
any fetch). The text is fastjsonschema's and may change between its versions.
A stored schema that does not compile gives other writers a generic detail.

<a id="collection-context-terms"></a>
## collection-context-terms: the collection JSON-LD context keeps its `opententeams:` terms

Decision: kept as it is; the `api.openteams.com` IRIs and the JSON-LD built
on them are part of the API's semantics and of what clients and stored data
already use, so a library release does not change them.

`collections-json-ld/<slug>/` (`ResourceCollectionContextView`) defines the
prefix `openteams` but maps `first`, `previous`, `next` and `last` to
`opententeams:first` and so on, a prefix nothing defines; and its term
definitions are the document itself, not wrapped in `@context`. A JSON-LD
processor therefore does not expand those four terms to
`https://api.openteams.com/json-ld/...`, and one that expects a context
document must wrap the body in `{"@context": ...}` itself. 0.12.0 changes only
the media type it is served as (`application/ld+json`, as its `Link` says).
`first` and `last` are never sent in a list.
