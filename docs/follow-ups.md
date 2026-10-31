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
