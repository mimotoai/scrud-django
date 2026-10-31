# Change Log

## 0.12.0

This release moves scrud-django to Django 5.2, uv, scrudful 0.8 and
django-scoped-rbac 0.11. It has several breaking changes.

### Upgrading

1. Use Python 3.11 or newer and Django 5.2.
2. `INSTALLED_APPS`: remove `django_jsonfield_backport`; make sure
   `django.contrib.auth`, `django.contrib.contenttypes`, `rest_framework`,
   `reversion`, `scoped_rbac` and `scrud_django` are listed, and `haystack` if
   you use search.
3. `REST_FRAMEWORK`: set `DEFAULT_PERMISSION_CLASSES` to
   `["scoped_rbac.permissions.IsAuthorized"]` and `EXCEPTION_HANDLER` to
   `scrud_django.exceptions.scrud_exception_handler` (see the README).
4. Extras: install `scrud-django[search]` if you use haystack search.
5. Migrations of your own that import `django_jsonfield_backport.models.JSONField`
   must import `django.db.models.JSONField` instead, before you uninstall the
   backport.
6. Clients must send JSON bodies (form and multipart bodies get 415). Give your
   own `GenericViewMixin` views `parser_classes = [RbacJSONParser]`.
7. Workflow policies: your stored `WorkflowPolicy` resources must be at most
   one per type and context, in one context each, and each must pass the
   checks a write now makes (transition names must not start with `__`;
   `restrict_update` must be a list). In this order:
   1. run `manage.py migrate` (step 9). While scrud_django's migrations are
      not all applied, the database check of stored policies reports
      nothing, so a plain `migrate` of a 0.11 database goes through;
   2. run `manage.py check --database <alias>`, for the alias your router
      gives for `Resource` writes. It reports the policies to fix as
      `scrud_django.E002`, `E003`, `E005` (errors) and `W001` (a warning);
   3. fix or delete each one it reports, through the API (a PUT checks a
      fixed policy) or `ResourceRegistration` (`delete`, `update`), and run
      the check again. Until then every write of the type in their contexts
      fails with `ImproperlyConfigured` (a 500), and later `migrate` runs on
      that database stop on the errors (`migrate --skip-checks` gets past
      them).

   Check your `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` too
   (`manage.py check`, `scrud_django.E001`; give policies as mappings).
8. Code: read "API changes" below for the removed modules
   (`scrud_django.decorators`, `settings`, `wsgi`, `asgi`), what
   `GenericViewMixin` subclasses must now provide, and the exception handler
   and error body changes.
9. Stop every 0.11 process before you run `migrate`, and run none against
   the migrated database (no rolling deploy across this upgrade): 0.11 code
   does not keep `ResourceContext` rows, so a resource it moves stays listed
   in the context it left (to readers of that context), one it creates is
   listed to superusers only, and its deletes fail (a 500). If one did
   write, `manage.py scrud_sync_contexts --check` lists the resources whose
   rows disagree with their content (exit status 1), and
   `manage.py scrud_sync_contexts` re-syncs them (see Lists below).
   Run `manage.py migrate`. Migration `0008_resourcecontext` creates the
   `ResourceContext` table and fills it from every stored resource (one row
   per context; it reads every resource once, so plan for it on large
   tables). Lists are filtered by these rows: code that writes `Resource` rows
   with `QuerySet.update()`, `bulk_create()` or `bulk_update()` must save them
   with `save()` instead, or their lists go stale (see Lists below).
   Migration `0009_resourceremoval` creates the (empty) `ResourceRemoval`
   table, which records when a resource leaves a context, so that lists'
   `Last-Modified` moves on removals from then on;
   `0010_resource_base_manager` changes model options only (no SQL). Then
   run the database check of step 7. A resource stored with a context longer
   than 2048 bytes in UTF-8 gets no row for that context (see Lists below):
   the check reports it as `scrud_django.W003`; give it a shorter context or
   delete it.
10. Lists now show only what the requester may read (see Lists below). Give
    users who list a type `http.GET` on the type itself, in each context whose
    resources they should see, as well as on its list type; check your roles
    if they grant only the list type somewhere.
11. Clients: a body whose `rbac_context` is `[]` now gets 400 (it got 403),
    and lists of `ResourceViewSetGenericFilter` filter on their query
    parameters as JSON key lookups (see Lists below).
12. Backups and fixtures: `dumpdata` / `loaddata` of `scrud_django` round-trip,
    except that every loaded resource gets a new `ETag` and `Last-Modified`
    (as does a reversion revert; see Conditional requests below), so clients
    refetch it once after a restore.
    A dump may hold the `ResourceContext` rows or leave them out
    (`dumpdata scrud_django --exclude scrud_django.ResourceContext`, which
    makes it smaller); either way, loading rebuilds each resource's rows from
    its content, and a dumped row that disagrees with the content is dropped.
    A dump also loads into a database that already holds some of its
    resources (a staging copy, a partial restore): of two `ResourceRemoval`
    stamps of one type and context, the later is kept (loading one used to
    fail with `IntegrityError` there).
    On PostgreSQL, a fixture that lists `ResourceContext` rows before the
    resources they belong to, with rows that disagree with the content, can
    fail with `IntegrityError`: dump `Resource` first (`dumpdata scrud_django`
    does by default) or leave the context rows out of the dump.
    `loaddata` refuses (`ValueError`) a resource whose context is longer than
    2048 bytes in UTF-8 (see Lists below), so a 0.11 dump holding one does
    not load: before dumping, give such resources a shorter context (or
    delete them) in the 0.11 database, or migrate it first (`migrate` keeps
    them and `check --database` names them as `scrud_django.W003`), fix them
    there and dump that.
    A raw save of a `Resource` (`loaddata`, reversion's revert) must run inside
    a transaction: reversion's `Version.revert()` called outside
    `transaction.atomic()` now raises `TransactionManagementError` (wrap it, or
    use `Revision.revert()`, which is atomic).
13. If you use `DATABASE_ROUTERS`, route `Resource`, `ResourceType`,
    `ResourceContext` and `ResourceRemoval` writes to one alias, and migrate
    them all there: `manage.py check` now reports a split as
    `scrud_django.E004`.
14. Run your database at READ COMMITTED, the supported transaction isolation
    level (`manage.py check --database <alias>`, for the alias your router
    gives for `Resource` writes, reports another as `scrud_django.W002`). Give
    a file SQLite database that takes concurrent writes
    `"OPTIONS": {"transaction_mode": "IMMEDIATE"}`.
15. Search: set `HAYSTACK_SIGNAL_PROCESSOR =
    "scrud_django.search.OnCommitSignalProcessor"`. Workflow actions
    (`SCRUD_WORKFLOW_ACTIONS`) now run after the write commits; move one that
    must run inside the write, or veto it, to a `scrud_post_save` receiver
    (see Side effects on commit below).
16. A PUT or DELETE now holds its row lock until its response is ready, on
    scrud's views and on your `GenericViewMixin` views alike (see Conditional
    requests below). Subclasses of `ResourceViewMixin` that override
    `dispatch`, `initial` or `get_queryset`, and `GenericViewMixin` subclasses
    that override `dispatch`, `get_object` or `filter_queryset`, must call
    `super()`; a `GenericViewMixin` view without a `queryset` attribute opens
    its write transaction on the default database unless it overrides
    `locked_write_database()`. A `GenericViewMixin` view locks the row with a
    query of its own (the model's base manager filtered by the URL's lookup;
    a `lookup_field` only the view's queryset has, an annotation, is first
    read as keys through that queryset), not through its queryset, so a queryset with `distinct()`, an annotation
    or a `select_related` of a nullable relation keeps working on PostgreSQL
    (which refuses `FOR UPDATE` on those). Server code that updates or deletes a resource
    should go through `ResourceRegistration.update` / `delete`, which lock it
    the same way. Server code that locks a `WorkflowPolicy` resource's row
    itself (`select_for_update()`) and then saves or deletes it must call
    `scrud_django.registration.lock_if_workflow_policy` in that transaction,
    on every database, before the `select_for_update()`, with the type read
    without the row lock:
    `lock_if_workflow_policy(get_resource_type_for("workflow-policies", using=db), db)`
    (both in `scrud_django.registration`; the README has the full example).
    Otherwise it deadlocks with an API write of the policy (see Workflows
    below).
17. `GenericViewMixin` lists: their `ETag` and `Last-Modified` now come from
    what the requester may list (see API changes below), so the view needs an
    `RbacFilter` among its `filter_backends` (`manage.py check` reports one
    without as `scrud_django.E006`, and its list is `ImproperlyConfigured`),
    and its `list` should serve `list_page()` (or `list_queryset()` without a
    paginator).
18. Clients: JSON Schema `default` values are not applied; send every value
    you want stored (and that a workflow state condition reads); a default
    no longer makes a body valid either. A POST or PUT with an empty body, or
    with no `Content-Length` (a chunked body), gets 400 (`body-missing`), and
    one nested more than 100 levels deep (objects and arrays) gets 400
    (`body-too-deep`), as does one with a NUL character (`\u0000`) in any
    string (`body-nul-character`). A refused
    `WorkflowPolicy` write's 400 is a `workflow-policy-invalid` problem whose
    `detail` says why, and a workflow error caused by a settings default
    policy names it as `{"resource_type": ..., "default": true}` instead of
    embedding it (see Workflows below). A PUT that moves a resource into a
    context must be an initial state there, and the user's `http.PUT` grant
    there (not `http.POST`) must allow `__INITIALIZE__` if it is conditioned
    on `scrud_workflow`; `scrud_post_save` (and so workflow actions) reports
    such a move as `__INITIALIZE__`.
19. Clients: with `SCOPED_RBAC["UNIFORM_ERRORS"]` on (scoped-rbac's
    default), a 400 (malformed `If-Match` or `If-None-Match`), 412 or 428 is
    now problem details served as `application/problem+json` (it was
    `{"detail": ...}` as `application/json`; with uniform errors off it still
    is), and a list envelope's `link`
    now spells the schema link `rel="describedby"` with
    `type="application/schema+json"`, as the `Link` header does (see
    Conditional requests below).
20. Your own `IsAuthorized` subclasses and operators on scrud views (see
    Authorization below) get plain data, as scoped-rbac's own checks give it
    (its issue #55 was closed by deciding not to guard checks). Every object
    check (the detail route's, the post-save check and a list's per-object
    check) gives their `has_object_permission`, `has_context_permission` and
    `contexts_for`, and your operators, the resource's content: the `dict`
    the `Resource` holds (`scrud_django.permissions.content_of`; `{}` for
    content that is not a JSON object), not the `Resource` (0.11 gave the
    instance). Read its fields (`obj.get("name")`); an override that read
    `obj.content`, `obj.pk` or `obj.rbac_contexts`, or tested
    `isinstance(obj, Resource)`, must change. scoped-rbac's default
    `contexts_for` reads the resource's contexts from that content on the
    scrud views: each check that gives a permission the content first gives
    every `IsAuthorized` whose class keeps the default
    `scrud_django.permissions.resource_contexts` as its `contexts_for`,
    whatever your `get_permissions` builds (an override of
    `check_object_permissions` that builds its own permissions bypasses that
    and gets a 500, failing closed). A `contexts_for` of your own gets the
    `dict`: call `resource_contexts(obj)` where you called
    `super().contexts_for(obj)`, which reads attributes a `dict` does not
    have (an `AttributeError`, a 500, failing closed). The request check of a POST or PUT gives the permission
    classes (their `has_permission` and `contexts_from`) and your operators
    the parsed body itself (`request.data`), and the workflow check gives
    their `has_context_permission` and your operators the body as sent: a
    check must only read what it gets (`body.get("owner")`, never
    `body.pop("owner")`), since one that changes it changes what is stored,
    a bug in that check which nothing guards. An operator that evaluates CEL
    itself passes the object it got (already plain `dict`s and `list`s). In
    the workflow check, read the transition with
    `scrud_django.permissions.current_transition()`, which replaces
    `TransitionCheck.transition`. They now also decide the post-save check
    (their `has_object_permission` and `has_context_permission` in every
    context), and they can refuse a workflow transition the policy allows,
    but not grant one it refuses (their `has_context_permission` is given
    the request body as sent in each context, not the stored resource). An
    object they refuse to read is a 404 on its detail route, for every
    method (it was a 403; see Authorization below).
    Your `GenericViewMixin` views' request check gives the parsed body the
    same way; their object checks give your model's instance, as
    scoped-rbac does, so an operator there that evaluates CEL itself passes
    the model's `to_cel()` (give the model one).
    `scoped_rbac.access.cel_mapping(None, obj)` reads it too, but an
    operator gets no request, so it leaves out the fields a model's
    `cel_resource(request)` gives the policy's CEL, and for a model without
    `to_cel()` it is `{}`: an operator written as a denial
    (`not cel.is_true(...)`) must refuse when the fields it reads are
    missing, or that undetermined result becomes an allow. Lists ask
    them too: on a view whose `IsAuthorized` subclass overrides
    `has_object_permission` or `has_context_permission`, a list keeps only
    the resources its `has_object_permission` allows (see Lists below), so
    it shows what the detail route shows; every resource of such a list then
    counts against `SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]` (a 400 past
    it), and its `Last-Modified` no longer moves when a resource is removed
    (its `ETag` does). The same holds for your `GenericViewMixin` lists.
21. Django admin: `Resource` and `ResourceType` are now shown to superusers
    only. Staff who are not superusers no longer see them, whatever model
    permissions they hold (a write there bypasses RBAC, the workflow and JSON
    Schema validation).
22. Roles: a grant conditioned on `scrud_workflow` now allows workflow
    transitions only (see Workflows below); it no longer gives `http.GET`
    (detail or list) or `http.DELETE`. Roles that should read need their own
    `http.GET` grant on the type, and roles that should delete their own
    `http.DELETE` grant. Check roles with an action of `"*"` conditioned on
    `scrud_workflow` in particular, and make each `authorized_transitions` a
    list of transition names.
23. Code that reads a `ResourceType`'s `etag` or `modified_at` to see whether
    its resources changed must read a list's `ETag` and `Last-Modified`
    instead: writes of resources no longer change them (see API changes
    below).
24. MySQL and MariaDB: use mysqlclient, InnoDB and a utf8mb4 database (MariaDB
    stores resource content as text in the database's character set) and
    connection, at READ COMMITTED (step 14). On MariaDB, clients must keep
    request bodies within 31 levels of nesting (see API changes below). A
    MySQL or MariaDB database migrated with a pre-release 0.12 build must be
    recreated: its context columns compare inexactly, and nothing detects it.

### Lists

* A list's `page_count` is computed with the page size the request used
  (`?page_size`, at most 1000); it used the default of 100 whatever was
  asked.
* List endpoints are RBAC-filtered. A list shows exactly the resources the
  detail route shows the requester, as far as the view's `IsAuthorized`
  permissions (and subclasses of it) decide: those on which `http.GET` on the
  resource's type is allowed in **any** of the resource's usable contexts (not
  a reserved one), with a condition evaluated against the resource as the
  detail route's object check evaluates it (on the resource's content, a
  `dict`, which CEL reads and an operator gets; a grant that is only
  conditional lists the matching resources,
  while the detail route answers 403 until the user also holds an outright
  `http.GET` somewhere: scoped-rbac's request check refuses a grant it cannot
  decide without the object). The list type's `http.GET`
  (`https://api.openteams.com/json-schema/ResourceCollection?contents_type=...`)
  is still the check that opens the list (403 without it in any context); it
  no longer decides which resources are shown. Before, a user who could list a
  type saw every resource of it, in every context, stored `WorkflowPolicy`
  resources (CEL conditions included) among them.
* A resource with no usable context (`"rbac_context": []` stored server-side,
  or only reserved contexts) is listed, as it is read, by superusers only. A
  resource whose `rbac_context` is a reserved context (`"__x"`, `"*"`) is
  never listed, as its detail route is a 404 to everyone. (A context reserved
  only once normalised, such as `"\u200b__x"`, which only server code can
  store, can still be listed to superusers and to a user whose grant applies
  in every context, while its detail route is a 404, as in scoped-rbac.)
* Contexts match exactly, code point for code point, on every database, as
  on the detail route. MySQL's and MariaDB's default collations ignore case
  and accents, and their binary ones ignore trailing spaces, so there the
  `rbac_context` columns of `ResourceContext` and `ResourceRemoval` hold the
  context's UTF-8 bytes (`VARBINARY(2048)`): a list, and its
  `Last-Modified`, never take `"ACME"`, `"acmé"` or `"acme "` for `"acme"`,
  and a resource can be in contexts that differ only in case.
* Each resource's contexts are stored as `scrud_django.models.ResourceContext`
  rows, kept in step by `Resource.save()` (a new instance saved over an
  existing key included) and by raw saves (`loaddata`, reversion's revert,
  which must run inside a transaction), in the same transaction as the
  resource's row. A context longer than 2048 bytes in UTF-8 (so that its rows
  fit PostgreSQL's btree indexes; 683 characters of CJK are already over) is
  refused on every save path (`ValueError`). A resource stored before 0.12.0
  with a longer context is kept: migration `0008_resourcecontext` gives that
  context no row (it never truncates one), so lists leave the resource out
  there (superusers' lists excepted), its deletion stamps `__SOME_CONTEXT__`
  for it (superusers' lists), and saving its content fails with `ValueError`
  until it gets a shorter context. A new database check,
  `scrud_django.W003` (`manage.py check --database <alias>`, also run by
  `migrate`), names such resources. Lists find the resources of the contexts that
  grant `http.GET` outright in SQL; only the resources of contexts whose grant
  is conditional are checked one by one, at most
  `SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]` of them (default 10 000; past it
  the list is a 400, `conditional_list_too_large`). Resources granted outright
  do not count. The cap counts the candidates before a
  `ResourceViewSetGenericFilter` list's query parameters narrow them, so such
  a list can be a 400 even when few resources match: counting after them
  would let the 400 tell how many resources the user cannot read match a
  filter. `QuerySet.update()`, `bulk_create()` and `bulk_update()` do not
  update the rows. A new management command, `manage.py
  scrud_sync_contexts [--check] [--database <alias>]`, compares every
  resource's rows with its content (it reads every resource) and re-syncs
  those that differ, each read again in its own transaction with the
  resource's row locked (so it can run while the API serves, and reports
  only drift that holds under the lock), stamping the contexts they left;
  `--check` changes nothing and exits with status 1 if any differ.
  Running it again changes nothing. A new database check,
  `scrud_django.W004` (`check --database`, also run by `migrate`), names the
  resources that have no row at all though their content names a context
  (written by a 0.11 process or `bulk_create()`); it cannot see a resource
  whose rows name a context it left (moved by `QuerySet.update()` or a 0.11
  process), which takes the command's full comparison.
* The filter is `scrud_django.filters.ResourceRbacFilter`, which
  `ResourceViewSet` and `ResourceViewSetGenericFilter` now set as their
  `filter_backends` (it is scoped-rbac's `RbacFilter` for detail lookups). A
  `ResourceViewMixin` view whose `filter_backends` holds no `RbacFilter` raises
  `ImproperlyConfigured` on its list instead of listing every resource.
  `ResourceViewMixin.list` now builds its page from
  `filter_queryset(get_queryset())`, so the view's filter backends run. A
  view whose `IsAuthorized` overrides `contexts_for` is listed per object
  with those contexts, each given the resource's content
  (`scrud_django.filters.filter_readable_resources_per_object`), so its list
  agrees with its detail route too.
* A view whose `IsAuthorized` (a subclass of your own) overrides
  `has_object_permission` or `has_context_permission` may refuse what the
  grants allow, which SQL cannot see. Its list is narrowed further, right
  after the RBAC filter (before query parameter filters): each resource is
  given to the permission class's `has_object_permission` as the detail
  route gives it (its content, a `dict`, with the request's `http.GET` on the
  item type; the view's `action` reads `"retrieve"` while it is asked), and
  only those it allows are listed, superusers' lists included. A
  `has_object_permission` that raises DRF's `PermissionDenied`,
  `NotAuthenticated` or `NotFound`, or Django's `Http404` or
  `PermissionDenied`, for a resource refuses that resource only, as on its
  detail route (`scrud_django.filters.REFUSALS`); any other exception
  fails the whole list. Every
  resource it examines counts against
  `SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`, outright-readable ones
  included (past it, the list is a 400 `conditional_list_too_large`). Its
  `ETag` and `Last-Modified` come from what it lists; a removal does not
  move its `Last-Modified` (the class may have refused the resource that
  left), its `ETag` changes
  ([list-last-modified-deletes](follow-ups.md#list-last-modified-deletes)).
  `GenericViewMixin` lists are narrowed the same way, each object given as
  it is. A permission class that overrides neither is listed in SQL as
  before. Only `IsAuthorized` permissions narrow a list: another
  `BasePermission` on the view (an owner check's `has_object_permission`,
  say) is not asked for lists, as in DRF, so its detail route refuses a
  resource that the list still shows, content included. Put such a rule in
  an `IsAuthorized` subclass, or apply it in a filter backend of the view
  (or its `get_queryset`), for the list to follow it.
* A list's `ETag` and `Last-Modified` come from what the requester gets: the
  `ETag` is a hash of the page's resources (their keys and ETags, in order),
  the list's count and the query parameters; `Last-Modified` is the newest
  `modified_at` of the resources the requester may list, or the newest time a
  resource left a context in which they may list every resource (deleted, or
  saved into other contexts: `scrud_django.models.ResourceRemoval`), whichever
  is later (none for an empty list with no such removal). A write to a
  resource they cannot read no longer changes them, so `If-None-Match` no
  longer tells when such a resource changes. They used to be the resource
  type's, which every write of the type changed. A removal from a context the
  requester reads only by a condition does not move `Last-Modified` (it could
  be of a resource they may not read), so use the `ETag` there
  ([list-last-modified-deletes](follow-ups.md#list-last-modified-deletes)).
* `ResourceViewSetGenericFilter` lists filter on their query parameters with
  JSON key lookups, which SQLite supports too (they used `content__contains`,
  which raised `NotSupportedError` there): `?key=value` keeps the resources
  whose content's top-level `key` is the string `value`; every value of a
  repeated parameter must match; a key is always a key, never a lookup
  (`?name__startswith=x` matches the key `"name__startswith"`); `page`,
  `page_size` and `format` are not filters (they used to be, so a paginated
  filtered list matched nothing); an integer key, or a NUL character in a key
  or value, is a 400.

### Requirements and packaging

* Python 3.11 or newer and Django 5.2 are required.
* Supported databases: SQLite, PostgreSQL, MySQL 8.4+ and MariaDB 11.4+, at
  READ COMMITTED (`scrud_django.W002` warns about another level). On MySQL
  and MariaDB use mysqlclient, InnoDB and a utf8mb4 database and connection;
  migrations `0008` and `0009` store contexts there as `VARBINARY(2048)`
  (see Lists). CI runs the suite on all four.
* Minimum versions of the other dependencies: djangorestframework 3.15,
  django-filter 25.1, django-reversion 5.1, jsonpath-ng 1.6 and
  fastjsonschema 2.19; with the `search` extra, django-haystack 3.3 and
  whoosh-reloaded 2.7.5 (the lowest versions the suite is run with).
* `django_jsonfield_backport` is no longer used. Remove it from `INSTALLED_APPS`.
  If your project has migrations of its own that import
  `django_jsonfield_backport.models.JSONField`, change those imports to
  `django.db.models.JSONField` before you uninstall the backport; otherwise
  those migrations no longer import.
* django-haystack and Whoosh are no longer installed by default; they are in
  the `search` extra. If `haystack` is in your `INSTALLED_APPS`, install
  `scrud-django[search]`.

### Conditional requests

Conditional request handling now comes from scrudful 0.8.

* A PUT or DELETE with no precondition returns 428 instead of 400. Either
  `If-Match` or `If-Unmodified-Since` is enough.
* ETags no longer carry a version suffix.
* The service list's (`services/`) `Last-Modified` is the instant its
  catalog last changed. It was that time's local wall-clock time read in
  `TIME_ZONE`, hours off wherever the two zones differ. Its `ETag` changes
  once with the upgrade.
* The `Link` header now uses `rel="describedby"` with the type
  `application/schema+json`. A list envelope's `link` is now the same value
  as the resource's `Link` header (it used `rel="describedBy"` and
  `type="application/json"`).
* While `SCOPED_RBAC["UNIFORM_ERRORS"]` is on for DRF (scoped-rbac's
  default), scrudful's own 400 (a malformed `If-Match` or `If-None-Match`),
  412 (a failed precondition) and 428 (a missing one), which it answers
  before the view runs (on the resource views, your `GenericViewMixin`
  views and the service list, `services/`), are RFC 9457 problem details served as
  `application/problem+json` with the problem-details `Link`s, as
  scoped-rbac's own viewsets answer them: `type` `about:blank`, the reason
  phrase as `title`, `status`, scrudful's `detail` (a 428 also has
  `accepted-precondition-headers`) and a `code` (`malformed_precondition`,
  `precondition_failed`, `precondition_required`). They were
  `{"detail": ...}` as `application/json`. (scrud-django's own errors have
  `https://api.openteams.com/http-problem/...` types and no `code`; the other
  errors, through `scrud_exception_handler`, are scoped-rbac's, with
  `about:blank` and a `code`, as these are.) With uniform errors off, they
  and every other error of the scrud views keep their native
  `{"detail": ...}` bodies as `application/json`, without the
  problem-details `Link`s, as scoped-rbac's own viewsets have it (the setting
  is read per request).
* OPTIONS bodies use scrudful's Semantic HTTP format, with `x-rdf-context`.
* A PUT or DELETE runs in one transaction on the database the router gives
  for `Resource` writes: the permission check, the lookup with the resource's
  row locked (`SELECT ... FOR UPDATE`), the object authorization, the
  `If-Match` / `If-Unmodified-Since` evaluation, the workflow check and the
  write of that same row. A concurrent write of the resource waits until the
  first commits, then evaluates its preconditions, authorization and workflow
  transition against what was committed: an `If-Match` the first write made
  stale is a 412 (a DELETE used to succeed and a PUT to overwrite the other
  write), and a transition the stored state no longer allows (for example
  `canceled` -> `done`) is refused with 400 (it used to commit). A response
  that is not a success rolls the whole request back. The lock is held until
  the response is ready, so concurrent writes of one resource now queue
  behind each other; a `WorkflowPolicy` write takes the `WorkflowPolicy` type
  lock before the row's, as before. The row is locked before the object check
  (so that the check reads the locked row): a user with `http.PUT` or
  `http.DELETE` on the type in some context can lock a row they may not read
  for the length of their request, which then answers 404 or 403. With
  `ATOMIC_REQUESTS = True` this transaction is a savepoint inside the
  request's, and a refused write still rolls back. SQLite ignores the row
  lock and lets one transaction write at a time: on a file database, a second
  concurrent writer fails with "database is locked" (a 500) rather than
  waiting. Give a file SQLite database that takes concurrent writes
  `"OPTIONS": {"transaction_mode": "IMMEDIATE"}` (Django 5.1 and later), so
  that the second writer waits and then gets its 412 or 400. A
  `GenericViewMixin` view (a model of your own) runs its PUT, PATCH and
  DELETE the same way, on the database the router gives for its
  `queryset`'s model, with the row the URL names locked before the lookup:
  two concurrent writes with the same `If-Match` used to both pass (a DELETE
  then a PUT even re-inserted the row).
* A delete outside the API (`Resource.delete()`, a `Resource` queryset's
  `delete()`, the admin's delete and "delete selected") locks the resource
  rows first, in primary key order, as the API's PUT and DELETE do, and so
  waits for a concurrent write of the same resource instead of deadlocking
  with it on PostgreSQL (`deadlock detected`, a 500 for the API request).
  The `WorkflowPolicy` type lock is `SELECT ... FOR NO KEY UPDATE` where the
  database has it (PostgreSQL), so that a policy deleted outside the API
  while an API write of it waits does not deadlock either. MySQL and MariaDB
  lack it and check a foreign key at once, so there a delete, a `save()` and
  a `scrud_sync_contexts` repair of a `WorkflowPolicy` take the type lock
  before the policy's row, as the API's writes do (on every database);
  otherwise one of them and an API write of the policy deadlocked
  (`Deadlock found`, a 500 for the API request). There a removal stamp is
  one `INSERT ... ON DUPLICATE KEY UPDATE`, so concurrent first removals from
  one context do not deadlock either.
* Every raw save of a resource (a reversion revert: `Version.revert()`,
  `Revision.revert()`, the admin's revert and recover; `loaddata` of a dump
  or backup) gives it a fresh `ETag` and `Last-Modified`. It kept the saved,
  older ones: a client revalidating with `If-Modified-Since` alone got 304
  for content that changed, and a list whose newest resource it was, or
  that it came back into after a deletion, could keep an older
  `Last-Modified`. Clients refetch each such resource (and its lists) once.
* READ COMMITTED is the supported transaction isolation level (PostgreSQL's
  default, and Django's for MySQL): a write that waited for a lock then sees
  what the write it waited for committed. A new database check,
  `scrud_django.W002` (`manage.py check --database <alias>`, also run by
  `migrate`), warns when transactions on the database scrud-django writes
  resources to run at any other level, READ UNCOMMITTED included (on MySQL
  and MariaDB it lets the workflow check read a policy whose write is later
  rolled back). On PostgreSQL at REPEATABLE READ or
  SERIALIZABLE, the second of two concurrent writes of one resource (or of two
  `WorkflowPolicy` writes that would make a duplicate) fails with a
  serialization error, a 500, instead of a 412 or 400; on MySQL or MariaDB at
  REPEATABLE READ, the `WorkflowPolicy` uniqueness re-check can miss a
  concurrent duplicate
  ([policy-recheck-isolation](follow-ups.md#policy-recheck-isolation)).

### Authorization

* RBAC is enforced by scoped-rbac's `IsAuthorized` permission class. DRF's
  default permission is `AllowAny`, so a view without `IsAuthorized` is open to
  everyone, anonymous users included. `ResourceViewSet` and
  `ResourceViewSetGenericFilter` now set `permission_classes = [IsAuthorized]`
  themselves, so they fail closed whatever `DEFAULT_PERMISSION_CLASSES` says.
  Still set `"DEFAULT_PERMISSION_CLASSES": ["scoped_rbac.permissions.IsAuthorized"]`
  in `REST_FRAMEWORK` so that your other views fail closed too (see the README).
* Detail routes enforce object-level RBAC through scoped-rbac. An object the
  user cannot read now gives 404. A resource's `rbac_context` names its context
  only when it is a string or a list of strings. Resources with no
  `rbac_context`, or with any other value there (for example a JSON-LD context
  document's `{"@id": ...}` term definition), belong to the default context `""`.
* Whether the user can read a refused object (404 or 403 on its detail
  route, and whether a workflow error names its stored state) also asks the
  object checks of the view's `IsAuthorized` subclasses, as for a GET of the
  detail route whatever the request's method, as its list does: an object
  your `has_object_permission` or `has_context_permission` refuses to read
  is now a 404 for every method, as it is missing from the list (it was a
  403, which revealed that it exists). Object checks only narrow: one that
  allows what the grants refuse makes nothing readable. The same holds for
  `GenericViewMixin` views (`ListValidatorsMixin.object_readable`).
* Unauthenticated requests now get 401.
* A POST or PUT body whose `rbac_context` is an empty list gets 400
  (`https://api.openteams.com/http-problem/rbac-context-invalid`, "Invalid
  rbac_context"), and nothing is stored or changed. It used to get 403 from
  scoped-rbac's request check, superusers included. Leave `rbac_context` out,
  or give `""`, for the default context. Server code (`ResourceRegistration`)
  may still store `[]`: the resource is then in no context, and only
  superusers can read or list it. A context longer than 2048 bytes in UTF-8
  gets the same 400 through the API and a `ValueError` from
  `Resource.save()` (on PostgreSQL a longer multibyte context of 2048
  characters or fewer was a 500).
* Object checks give the permission class and the grants' conditions the
  stored resource's content, the `dict` the `Resource` holds, not a copy
  (`scrud_django.permissions.content_of`; `{}` for content that is not a
  JSON object). That is so on the detail route (GET, PUT and DELETE:
  `ResourceViewMixin.get_object` passes it to `check_object_permissions`,
  and so to `has_object_permission`, `has_context_permission`,
  `contexts_for` and the 404-or-403 readability check), in the post-save
  check and in a list's per-object check. A CEL condition reads the content
  (it raised `TypeError`, a 500, before), and an adopter's operator gets it
  as it gets the body on the request check: it got the `Resource` model
  instance, so an operator that reads the body (`resource.get("name")`)
  raised `AttributeError`, a 500, on every detail route and list its grant
  conditioned. (scoped-rbac's request check of a GET or DELETE still gives
  operators `None`: there is no body.) scoped-rbac's default `contexts_for`
  reads attributes, which a `dict` does not have: each of those checks
  first gives every `IsAuthorized` whose class keeps it (judged by its class,
  as scoped-rbac's `custom_contexts_for` judges it, so lists keep their SQL
  path) `scrud_django.permissions.resource_contexts`, which reads a
  resource's contexts from its content (`rbac_contexts_of`, as
  `Resource.rbac_contexts` does), whatever the view's `get_permissions()`
  returns. A resource stored
  in a context reserved once normalised is a 404 before the object check.
* Checks get plain data, as scoped-rbac's contract has it (its issue #55
  was closed by deciding not to guard them): scoped-rbac's request check of
  a POST or PUT gives the permission classes, their `contexts_from` and the
  grants' operators the parsed body itself (`request.data`), the workflow
  check gives them the body as sent (the serializer's `initial_data`, the
  same `dict`), and object checks the stored content. Each is an exact
  `dict` of plain JSON values, which CEL evaluates. Nested in the data an
  operator passes CEL (`{"resource": obj}`), a read-only view, or any
  mapping or sequence other than an exact `dict` or `list`, evaluates to an
  undetermined result, which an operator written as
  `not cel.is_true(expression, {"resource": obj})` turns into an allow.
  This holds on the `Resource` views; a `GenericViewMixin` view's object
  checks give operators your model's instance, as scoped-rbac does (see
  Upgrading). A check must only read what it gets (`obj.get("owner")`,
  never `obj.pop("owner")`): one that changes the body or the content
  changes what is stored (or what the response shows), which is a bug in
  that check, not something scrud-django guards against.
* Request bodies must be JSON. `ResourceViewSet` and
  `ResourceViewSetGenericFilter` set `parser_classes = [RbacJSONParser]`
  (`scoped_rbac.rest`): a form-encoded or multipart body gets 415, and a JSON
  body that is not an object (for example a list) gets 400. A form body could
  name `rbac_context` twice; scoped-rbac authorized the request by the last
  value while the resource was stored in both contexts. Give your own
  `GenericViewMixin` views the same `parser_classes`.
* A create or update is checked again after the save, before it commits: the
  request's method must be allowed in **every** context the stored resource is
  in, or the write is rolled back with 403. The check asks the view's
  permission classes, so an `IsAuthorized` subclass's overrides apply to it
  (it used to ask the policy directly): each `IsAuthorized`'s
  `has_object_permission` (as scoped-rbac's own post-save check asks it;
  superusers only for a resource in no usable context) and its
  `has_context_permission` in every context `contexts_for` names, both given
  the saved resource's content (a `dict`), as on the detail route.
* The workflow check asks the view's permission classes too: the request's
  policy must allow each transition, and then each `IsAuthorized`'s
  `has_context_permission(request, view, body, context)`, given the request
  body as sent (a `dict`) in that context, with the transition as
  `scrud_django.permissions.current_transition()`, must allow it as well
  (an override was not asked). An `IsAuthorized` subclass can therefore
  refuse a transition, never grant one the policy refuses: in this check
  `obj` is the request body as the writer sent it, not the stored
  resource, so a shortcut that trusts its fields (an owner, say) would be
  trusting the writer. A serializer used without a view asks the policy
  alone. A refusal is the workflow check's 400 (`workflow-transition-invalid`,
  "The user isn't authorized to perform this state transition"), before the
  save.
* The Django admin shows `Resource` and `ResourceType` to superusers only
  (`scrud_django.admin.SuperuserOnlyAdmin`): staff who are not superusers see
  neither, whatever model permissions they hold. A write in the admin
  bypasses scoped-rbac, the workflow and JSON Schema validation.
* A write is authorized in the contexts its body is stored in: a body whose
  `rbac_context` is absent, `null` or not a string (or a list of strings), for
  example a JSON-LD context document's `{"@id": ...}` term definition, is
  authorized in the default context `""`, where it is stored and read
  (`ResourceSerializer.contexts_from`, which `IsAuthorized` uses). A
  default-context writer used to get 403 for `"rbac_context": null` or a dict
  (scoped-rbac's default authorized that value as the context). A list with
  a member that is not a string is stored in `""` too, but is still refused:
  scoped-rbac's request check also needs the method in each member of a list.
* `"rbac_context": ""` now means the default context everywhere, exactly as
  leaving the key out does. Before, a body with `"rbac_context": ""` skipped the
  default workflow policy, so its initial-state check, transition validity and
  transition authorization did not apply.
* Workflow transitions are authorized in every context the resource is in
  **and** every context the write moves it to (see Workflows below). A user
  who may only make some transitions in one context can no longer make others
  by moving the resource into a context where they may write.
* Removing a resource from a context needs no grant in that context unless a
  workflow policy applies there. A PUT is authorized by the object check in
  any one context the stored resource is in, and after the save in every
  context it ends up in; a context it leaves is checked only under that
  context's workflow policy (the transition must be allowed there). So a user
  with `http.PUT` in `a` alone can PUT `"rbac_context": "a"` to a resource in
  `["a", "b"]`, and `b`'s readers lose it, as a DELETE grant in one context
  deletes a resource from all of them. A `WorkflowPolicy` resource is the
  exception: its update or delete needs the method in every context it is
  in.

### API changes

* A list's collection schema (`collections-json-schema/<slug>/`, its
  `describedby`) is now a valid JSON Schema that the list's body validates
  against. Its `content` was `{"properties": {"type": "array", ...}}`, which
  no validator compiles. It now types the body as an object and `content` as
  an array of envelope objects, types `next` and `previous` as a URL or
  `null`, describes each envelope's `link`, and no longer lists `first` and
  `last`, which a list never sends. The type's schema embedded as an
  envelope's `content` drops its `$id`, and its own `#` references point
  into the collection schema (they did not resolve). A type without a schema
  allows any content (`{}`; it was `{"type": "any"}`, not valid draft-04).
* Registering a resource type again, as every process start does, keeps its
  schema, JSON-LD context and indexing policy resources. Under `DEBUG`, which
  rewrites the type, each start stored new ones (a development database's
  `json-schema/` list grew with every `manage.py` command); now they are
  updated in place. Without `DEBUG`, unchanged ones are no longer rewritten,
  so their `ETag` and `Last-Modified` stay. Copies that `DEBUG` left behind
  are not referenced by any type; delete them through the API if you want.
* `scrud_django.decorators` is removed. Use `scrudful.rest_framework` instead.
* `scrud_django.settings`, `scrud_django.wsgi` and `scrud_django.asgi` are
  removed: the package no longer ships a Django project. Use your own project's
  settings, WSGI and ASGI modules.
* `scrud_django.utils.get_string_or_evaluate` is removed.
* `ResourceViewSet` and `ResourceViewSetGenericFilter` set
  `permission_classes = [IsAuthorized]`. A subclass that relied on a different
  `DEFAULT_PERMISSION_CLASSES` must set `permission_classes` itself.
* `scrud_django.urls` now also mounts scoped-rbac's semantic URLs at
  `semantic/`, which serve each resource type's schema and JSON-LD context. If
  you mount URLs yourself, include `scoped_rbac.semantic_urls` once.
* The Role and RoleAssignment endpoints that `scrud_django.urls` includes from
  `scoped_rbac.urls` now serve scoped-rbac 0.11's Role and RoleAssignment
  shapes. Clients of those endpoints must follow scoped-rbac 0.11.
* `GenericViewMixin` subclasses must provide the DRF `get_queryset()` and a
  lookup instead of `get_instance`, and put scoped-rbac's
  `AccessControlledAPIView` before `GenericAPIView` in their bases, so that
  `get_object()` is scoped and permission-checked.
* `scrud_exception_handler` keeps scrud-django's own error bodies and passes
  every other exception to scoped-rbac's problem details handler. Through it,
  non-scrud errors use scoped-rbac's uniform problem details project-wide, not
  only on scrud views: for example a DRF `ValidationError`'s field messages
  become "This value is not valid." and a 404 has a different body shape.
  scrud-django's own errors (`ScrudException` and subclasses) are
  `APIException`s whose `detail` is an `ErrorDetail` with a code, so they render
  as a 400 problem under DRF's own handler and scoped-rbac's handler too; only
  `scrud_exception_handler` sends their full body (`policy`, `prior_state`,
  `proposed_state`).
* `GenericSerializerMixin.to_internal_value` (and so `SerializerMixin`) answers
  400 for a body that is not a JSON object. This also applies to downstream
  serializers that use `GenericViewMixin`, and breaks bulk (list) creates
  through them.
* A write of a resource (`register`, `register_instance`, `update`, `delete`
  and the API's) no longer saves its resource type's row. It used to set the
  type's `etag` and `modified_at` to the write's (except on `update`, which
  saved another instance than the one it set them on) and save the whole row
  as the write had read it, so a copy read through a lagging replica, or
  before a re-registration of the type committed, wrote the old `schema`,
  `context` or `schema_uri` back. Nothing reads the type's `etag` and
  `modified_at` any more (lists compute their own validators, see Lists
  below), and the update locked the type's row until the write's
  transaction ended, so writes of one type queued behind each other. They
  now change only when the type is registered (`ResourceTypeRegistration`).
  `registration.save_type_etag` is gone.
* `ResourceRegistration.update` and `delete` read the resource with its row
  locked and its type on the write database (`type_db()`), never through the
  router's `db_for_read` (a read replica could be stale), and write that
  instance (new `ResourceRegistration.update_locked` and `delete_locked`, for
  a row the caller read with `get_locked_resource` after
  `lock_if_workflow_policy`, in that order, in its transaction on
  `write_db()`). `get_resource_type_for(slug, using=None)` takes the database
  to read. `ResourceViewMixin` gains `dispatch`, `locked_write_db` and
  `locked()` (from the new `LockedWriteMixin`: the locked write of a PUT,
  PATCH or DELETE) and `initial` (the `WorkflowPolicy` type lock), and its
  `get_queryset` locks the rows it reads during one; its `update` and
  `destroy` no longer call `ResourceRegistration.update` / `delete`. A
  subclass that overrides `dispatch`, `initial` or `get_queryset` must call
  `super()`. The prior state `scrud_post_save` reports is read on the save's
  database (`ResourceMixin.get_transition_data(using)`).
* Database routing: `ResourceRegistration` saves a resource, its
  `ResourceContext` rows and its `ResourceRemoval` stamps on one database, the one
  its transaction is on (`write_db()`, `router.db_for_write(Resource)`, given
  the resource as the router's `instance` hint when there is one), and takes
  the `WorkflowPolicy` type lock there. It used to save the type row wherever
  the router sent `ResourceType` writes: with `Resource` and `ResourceType`
  routed to two aliases of one PostgreSQL database, a `WorkflowPolicy` write
  waited on its own lock forever (or until `lock_timeout`, a 500). A new system check, `scrud_django.E004` (run by
  `manage.py check`, no database needed), is an error when the router sends
  `Resource`, `ResourceType`, `ResourceContext` and `ResourceRemoval` writes
  to different aliases. Routing these models by instance hint is not supported: the API's
  writes open their transaction, and look the resource up, on the alias the
  router gives without a hint (there is no instance yet), so a resource a
  router places elsewhere by instance is a 404 to PUT and DELETE, and a POST
  that the router would save elsewhere fails. The check, which asks without a
  hint, cannot see such a router. (`ResourceRegistration.register_instance`
  passes the resource as the hint, so server code gets one transaction on the
  alias its save goes to.)
* `ResourceViewMixin.get_instance(request, slug)` now returns `get_object()`
  and ignores `slug`. scrudful reads the validators (ETag, Last-Modified) from
  the same object, so an override of `get_instance` no longer affects them.
* `GenericViewMixin` and `ResourceViewMixin` share three new bases in
  `scrud_django.mixins.view_mixins`: `ScrudfulMeta`, the `Meta` both read
  (validators, Links, problem-details errors, list validators; a fourth,
  `UniformErrorsMixin`, makes its `as_view` turn the class's scrudful
  handlers into `UniformErrorsViewFunc`s, whose problem-details errors apply
  only while uniform errors are on);
  `LockedWriteMixin` (`dispatch`, `locked_write_database()`, `locked()`,
  `locked_write_db`); and `ListValidatorsMixin` (`list_queryset()`,
  `list_page()`, `list_etag()`, `list_last_modified()`, and
  `filter_queryset()`, which runs the `filter_backends` itself to narrow a
  list by an overriding permission class's object check right after the
  `RbacFilter`, with `lists_by_object_checks()` and `object_for_checks()`;
  `scrud_django.filters.object_check_requirement` and
  `filter_by_object_checks` do the work). `GenericViewMixin`
  now runs a PUT, PATCH or DELETE in one transaction on
  `locked_write_database()` (`router.db_for_write` of its `queryset`'s model;
  the default database without a `queryset` attribute), its `get_object`
  locking the row the URL names first (`lock_object_row`, a query of its own,
  whatever the view's queryset; an annotated `lookup_field` is resolved to
  keys through the view's queryset first, and a lookup that then finds a row
  the lock did not take is a 404; `filter_queryset` only routes the lookup
  to that database), and gives its list the validators of
  `list_queryset()` (it gave the resource type's `etag` and `modified_at`,
  which a write of your model never changed, so a changed list could answer
  304). Its list needs an `RbacFilter` among its `filter_backends`
  (`ImproperlyConfigured` otherwise; the new system check
  `scrud_django.E006`, run by `manage.py check`, reports a `ResourceViewMixin`
  or `GenericViewMixin` viewset routed for `list` without one) and should
  serve `list_page()`. Its
  `Last-Modified` does not move on a deletion (no removal stamps for models of
  your own); its `ETag` does.
* `ResourceViewMixin.get_object` is DRF's lookup with the object check given
  the resource's content (`scrud_django.permissions.content_of`, see
  Authorization); it returns the `Resource`, whose `resource_type` is the
  view's. New `scrud_django.permissions.resource_contexts(obj)`: a
  resource's contexts from its content (a mapping), else scoped-rbac's
  `access.object_contexts(obj)`; the checks that give a permission the
  content give it to each `IsAuthorized` that keeps scoped-rbac's
  `contexts_for`, and a `contexts_for` of your own calls it in place of
  `super().contexts_for(obj)`.
  `ResourceViewMixin.get_resource_type()` (and `GenericViewMixin`'s) reads
  the type once per request and database and returns that instance after
  (`initial` forgets it): a request read it up to 20 times (measured by
  `tests/test_query_budget.py`, a list of a partner profile page now makes
  11 queries, a GET 8 and a PUT 25; they made 19, 14 and 43). `ResourceViewMixin.get_rbac_permissions()` gives the
  `IsAuthorized` permissions of the view (nested ones included), which
  `get_post_save_permissions()` returns and the `WorkflowValidator` asks.
  `check_post_save_permissions` calls each permission's
  `has_object_permission` unconditionally. `scrud_django.permissions.TransitionCheck`
  is removed (the workflow check gives the body as a `dict`). New
  `scrud_django.permissions.current_transition()`: the name of the
  transition the workflow check is authorizing while it asks the policy and
  the permission classes, `None` anywhere else (outside the workflow check,
  and where no workflow policy applies).
* New `scrud_django.models.ResourceRemoval` (migration
  `0009_resourceremoval`): one row per resource type and context, the last
  time a resource left that context. `Resource.save()` (raw saves included)
  stamps the contexts its content left, and a `post_delete` receiver every
  context of a deleted resource (`Model.delete()`, `ResourceRegistration`),
  in the write's transaction. `Resource.objects`, and the base manager
  (`Meta.base_manager_name`, migration `0010_resource_base_manager`, which
  Django's own bulk paths use), is a `ResourceQuerySet`, whose `delete()`
  stamps the contexts of all the resources it deletes on the database it
  deletes on (its `using()` alias, else the router's `db_for_write`, as
  Django's own `delete()`; not a read replica) once, by type then context,
  when it ends (so that two concurrent bulk deletes
  cannot deadlock on the stamps). Stamps are never pruned: there is one per
  type and context ever left, and removing one could make a list's
  `Last-Modified` go back, answering 304 for a list that lost a resource.
  `ResourceViewMixin.list_last_modified` reads them through
  `scrud_django.filters.readable_removal`. `QuerySet.update()` and the bulk
  methods stamp nothing, as they sync no context rows.
* `ResourceSerializer.update` (`serializer.save()` on an existing resource)
  writes through `ResourceRegistration.update_locked` in a transaction on
  `write_db(instance)` (the router's database for resource writes), after the
  `WorkflowPolicy` type lock; it read
  `instance.slug`, which a `Resource` does not have (`AttributeError`).
  `SerializerMixin.resource_type` falls back to `Meta.resource_type_slug`
  (it raised `TypeError` without a `resource_type` keyword).
* `scrud_django.__version__` is `"0+unknown"` when the package is imported
  from a source tree that is not installed (it raised `PackageNotFoundError`).
* `WorkflowValidator.apply`, `default_policies` and `cached_policies` are
  removed. A subclass that overrides `apply` is silently ignored. Default
  policies are built once in `scrud_django.conf`
  (`default_workflow_policies()`), and stored ones come from
  `scrud_django.workflow.workflow_policy_for`.
* `WorkflowTransitionError.policy` is a reference, never a policy's content:
  a `StoredPolicyReference` (a stored policy, given in the body as its URL),
  a `DefaultPolicyReference` (a settings default, given as
  `{"resource_type": "<type URI>", "default": true}`), or `None` in a generic
  error. `WorkflowPolicy` takes `default_for`, the type URI a settings
  default is for (`DefaultWorkflowPolicies` sets it; a `WorkflowPolicy`
  object given in the setting is kept as a copy per type, so
  `default_workflow_policy_for` no longer returns the configured object
  itself). `ResourceMixin.workflow_context_and_policy()` gives the context
  `get_workflow_policy`'s policy is for.
  `WorkflowValidator.transition_for` takes `entering`, and
  `ResourceSerializer` has a `contexts_from`.
* A JSON Schema's `$ref` may point only inside the schema (`#/...`). A remote,
  `file:` or relative `$ref` is refused with 400
  (`https://api.openteams.com/http-problem/json-schema-invalid`) when a JSON
  Schema is written, and a stored schema that does not compile fails closed:
  every write of its type is a 400 with a generic detail.
* JSON Schema `default` values are never applied. fastjsonschema filled them
  into the data it validated; JSON Schema validators are now compiled with
  `use_default=False`, so a body is validated, and stored, exactly as sent: a
  default can neither be stored nor make a body valid (for example through
  `if`/`then`). A default nested in an object the body gave used to be
  stored (the copy DRF validates shared it); a top-level one was dropped but
  seen by the workflow check, which now reads the body as sent (the
  serializer's `initial_data`), so it checks the contexts and the state the
  resource is stored with.
* A POST or PUT whose body is empty, or has no `Content-Length` (a chunked
  body, which DRF does not read), gets 400
  (`https://api.openteams.com/http-problem/body-missing`, "Missing request
  body", `scrud_django.exceptions.RequestBodyMissingError`), before
  authorization; nothing is stored or changed. DRF read such a body as
  `{}`, so a POST created an empty resource and a PUT wiped the stored
  content. `{}` is still a body. A POST or PUT body nested more than
  `scrud_django.mixins.view_mixins.MAX_BODY_DEPTH` (100) levels deep,
  objects and arrays (`MARIADB_MAX_BODY_DEPTH`, 31, when resources are
  written to MariaDB, whose JSON column refuses a deeper document, a 500),
  gets 400
  (`https://api.openteams.com/http-problem/body-too-deep`, "Request body too
  deeply nested", `RequestBodyTooDeepError`), before authorization: code
  that walks a body recursively (reversion's serializer, an operator) failed
  on one a few hundred levels deep with `RecursionError`, a 500. A method the
  route does not serve still gets 405, whatever its body (too deep, an empty
  `rbac_context` list, a form body): its body is neither checked nor parsed.
* A POST or PUT body with a NUL character (`\u0000`) in any string, a key
  or a value at any depth, gets 400
  (`https://api.openteams.com/http-problem/body-nul-character`, "NUL
  character in request body", `RequestBodyNulError`), before authorization,
  on every database. PostgreSQL's `jsonb` cannot hold one: such a POST was a
  500 there (`DataError`) and was stored on SQLite.
* A DELETE of a resource that a resource type uses as its JSON Schema, JSON-LD
  context or indexing policy gets 409 Conflict
  (`https://api.openteams.com/http-problem/resource-in-use`, "Resource in
  use", `scrud_django.exceptions.ResourceInUseError`), whose `used_by` lists
  each type URI and what it uses the resource as (`schema`, `context`,
  `indexing_policy`), and the resource is kept. It was a 500
  (`ProtectedError`). `ResourceRegistration.delete` and `Resource.delete()`
  still raise `ProtectedError`.
* The envelope's `last_modified` (and the search index's `modified_at`) no
  longer depend on `USE_TZ`. With `USE_TZ = False`, a stored modification time
  is naive and in `TIME_ZONE`; it used to be read as UTC, so the envelope
  disagreed with the `Last-Modified` header whenever `TIME_ZONE` was not UTC.
  Both now read it in `TIME_ZONE` (`scrud_django.utils.http_date_for`).

### Workflows

* Workflow policies now apply in every context, not only the default context
  `""`. The policy in effect for a resource type in a context is the stored
  `WorkflowPolicy` for that type whose `rbac_context` is that context (one that
  names no `rbac_context` is in `""`), else the type's default from
  `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]`. A policy stored in one context
  no longer affects any other. Before, writes outside `""` skipped the
  initial-state check, transition validity and transition authorization
  entirely, and storing any `WorkflowPolicy` for a type made every later write
  of that type fail with 500. Resources outside `""` whose bodies are not a
  valid initial state under the default policy (for example a ToDo with no
  `status`) are now refused with 400.
* A create must be a valid initial state under the policy of every context it
  names. An update must be a valid transition, and the user must be authorized
  for it (the `scrud_workflow` operator's `authorized_transitions`), under the
  policy of every context the resource is in. In a context the update moves it
  into (one the body names that it is not stored in), it is checked as a
  create: the body must be a valid initial state under that context's policy,
  and the user authorized for `__INITIALIZE__` there, by the request's
  method (`http.PUT`: a user who may only `http.POST` there cannot move a
  resource in, and an unconditional `http.PUT` grant there allows any move
  in of a resource in an initial state). So a resource cannot be carried into
  a context in a state nobody there could have created or moved it to (a
  "done" ToDo into a context where the writer may not "finish").
  `scrud_post_save`'s `transition_data` (which workflow actions read)
  reports such a move as `__INITIALIZE__` (no prior state) when the policy it
  reports under is that of an entered context.
* `scrud_django.workflow.workflow_policy_for(resource_type, context)` is the
  one policy lookup; the `WorkflowValidator` and
  `ResourceMixin.get_workflow_policy` (which picks the policy whose transition
  `scrud_post_save` reports) both use it.
* `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` is validated once, by a system
  check (`scrud_django.E001`, run by `manage.py check`), and a default must pass
  the same transition and `restrict_update` checks as a stored policy. An
  invalid default makes only writes of its own type fail. A string
  `restrict_update` used to be read as a substring test; it is now an error (it
  must be a list of defined state names). Give each policy as its content (a
  mapping, for example `json.loads` of a file), not as a `WorkflowPolicy`: a
  function that builds `WorkflowPolicy` objects eagerly and raises on an invalid
  one makes the whole setting fail to load, so every type's default fails, not
  only that one
  ([eager-callable-default-policies](follow-ups.md#eager-callable-default-policies)).
  A dotted-path function given as the setting is now called once per process,
  not on every write: its result is cached until `SCRUD_WORKFLOW` changes
  (`setting_changed`, which `override_settings` sends). A function that returned
  different policies over time must be replaced by stored `WorkflowPolicy`
  resources or a restart.
* A workflow-conditioned grant (`scrud_workflow` operator) grants workflow
  transitions and nothing else (it fails closed). It allows the other checks
  of a POST or PUT on a scrud resource view that the workflow check covers
  (the request check, a PUT's object check, and the post-save check once the
  workflow check has authorized the write), and refuses everywhere else: a
  GET or HEAD (detail or list: the list leaves such resources out, as the
  detail route refuses them), a DELETE, a write the workflow check does not
  cover (your own views), and the readability that tells a 403 from a 404.
  It used to allow all of those, so a "may only cancel" role, whose grant
  of every action (`"*"`) on a type is conditioned on `scrud_workflow` with
  `"authorized_transitions": ["cancel"]`, read, listed and deleted every
  resource of the type in its context. Its `authorized_transitions` must be a
  list of transition names: a string (which was a substring test), a mapping
  (a key test) or a list holding anything but strings is an invalid
  condition, which refuses everywhere (a 403) and is logged at ERROR, once
  per value per process.
* A workflow-conditioned grant no longer allows a
  write in a context where no workflow policy applies: the write is refused
  with 403. Unconditional and CEL grants are unaffected. A CEL condition on a
  grant that authorizes a transition now reads the request body, as in
  scoped-rbac's request check. The workflow check passes operators (and the
  permission classes' `has_context_permission`) the request body as sent, a
  plain `dict`, as scoped-rbac's request check does, and the transition out
  of band: the `scrud_workflow` operator, and your permission classes, read
  it as `scrud_django.permissions.current_transition()` (its name while the
  workflow check authorizes it; `None` outside the workflow check and where
  no workflow policy applies). Your own operators read the body as in
  scoped-rbac's request check, and must not change it.
* A reserved transition name (one that starts with `__`) is refused, in stored
  policies and in defaults.
* Workflow error bodies give `prior_state` (the stored resource's state) and
  `proposed_state` (the body's) the right way round; an invalid transition
  used to swap them. A create has no prior state: its errors give
  `prior_state` null (it used to be the whole request body), and the
  `transition_data` that `scrud_post_save` sends for a create has
  `prior_state` `None` and `proposed_state` its initial state (it used to be
  `None` and the content).
* A `WorkflowPolicy` is checked when it is created or updated, and refused with
  400 (nothing is stored; `initial_states`, `states` and `transitions` that
  are not objects, and conditions that are not strings, are refused by the
  `WorkflowPolicy` schema and by `WorkflowPolicy` itself), a
  `https://api.openteams.com/http-problem/workflow-policy-invalid` problem
  ("Invalid workflow policy", `WorkflowPolicyInvalidError`, code
  `workflow_policy_invalid` under any exception handler) whose `detail` says
  why (for example which condition does not compile), when a state condition
  is not valid CEL, a transition is not an object with `from` and `to` naming
  defined states, a transition's name starts with `__` (reserved for
  `__INITIALIZE__` and `__SAME_STATE__`), `restrict_update` is not a list of
  defined state names, or `rbac_context` is present but not a string. (It
  used to be a field error, `{"workflow_policy": [...]}`, which scoped-rbac's
  problem details render as "This value is not valid.", without the reason.)
* There is at most one stored `WorkflowPolicy` per resource type and context. A
  create, or an update moving a policy into a context, that would make a second
  one is refused with 400 (not 409, the same `workflow-policy-invalid`
  problem), without naming the existing policy; update or delete the
  existing policy instead. `WorkflowPolicy` writes lock the
  `WorkflowPolicy` resource type's row (`select_for_update`, `FOR NO KEY
  UPDATE` on PostgreSQL) and check again
  after the save, so of two concurrent writes the second is refused. Every
  `WorkflowPolicy` write through `ResourceRegistration` (`register`,
  `register_instance`, `update`, `delete`, and the API's create, update and
  delete) takes that lock first, inside its transaction on the database
  the router gives for `Resource` writes (see "Database routing" under API
  changes), before it reads or writes the policy's row, so concurrent writes
  of one policy cannot deadlock. A write that saves a
  `Resource` directly (`save()`, bypassing `ResourceRegistration`) takes the
  lock before it writes the row too, but does not check uniqueness; code that
  locks a policy's row itself (`select_for_update()`) before saving or
  deleting it must call `scrud_django.registration.lock_if_workflow_policy`
  before that `select_for_update()` (Upgrading 16). If several
  were stored before (or past the API), none is picked: writes of the type in
  that context fail with `ImproperlyConfigured` until all but one are deleted.
  The database system check (`manage.py check --database <alias>`, for the
  alias your router gives for `Resource` writes, also run by `migrate`)
  reports them (`scrud_django.E002`; a policy whose list `rbac_context`
  names a context twice counts once there), policies whose
  `resource_type` is not a string (`scrud_django.E003`), policies that fail
  the checks a `WorkflowPolicy` write makes, or cannot be checked at all
  (`scrud_django.E005`: stored by an earlier version or server-side, through
  `ResourceRegistration`, which does not check them; every write of the type
  in their contexts fails with `ImproperlyConfigured`, a 500, naming the
  policy, until it is fixed or deleted), and policies stored with a list
  `rbac_context` (`scrud_django.W001`, a warning). A policy the check cannot
  even evaluate is reported as E005 too, rather than crashing the check.
  E002, E003 and E005 are errors: `check --database` fails on them, and so
  does `migrate` of a migrated database. On a database whose scrud_django
  migrations are not all applied (a 0.11 database before `migrate`) the
  check reports nothing, so that `migrate` can create the tables a fix
  needs: migrate, then run `check --database <alias>`, then fix or delete
  the policies it reports (through the API or `ResourceRegistration`;
  Upgrading step 7). The `WorkflowPolicy` schema now also declares
  `restrict_update` as an array of strings. These checks catch the policy
  errors known to make later writes fail; they do not prove that a policy
  which passes them is usable.
* A stored `WorkflowPolicy` is compiled once per version (its pk, ETag and
  `modified_at`), not on every write: `scrud_django.workflow.STORED_POLICY_CACHE`
  keeps up to `STORED_POLICY_CACHE_SIZE` (256) compiled policies per process,
  least recently used first out. The stored policies are still read on every
  write.
* A workflow error caused by a stored `WorkflowPolicy` now gives that policy's
  absolute URL in `policy` (and in `detail`, where the policy used to be)
  instead of its content. When the writer may not read that policy (no
  `http.GET` on `WorkflowPolicy` in its context), for example because it is
  another context's policy the resource is also in, the error is generic: the
  same status, `type` and `title`, a generic `message` and `detail`, and
  `policy`, `prior_state` and `proposed_state` null. An update's workflow
  error is generic in the same way when the writer may not read the stored
  resource (as the detail route decides it: `http.GET` in one of its
  contexts), for example a writer whose only grant is conditioned on
  `scrud_workflow`, so that it does not tell them the resource's stored
  state. A workflow error caused
  by a settings default policy names it by reference too, never by its
  content (its CEL conditions and every role's transitions used to be in
  `policy` and `detail`): `{"resource_type": "<type URI>", "default": true}`,
  the type the default is for, marked as the default. No workflow error body
  carries a policy's conditions or transitions.
* Updating or deleting a stored `WorkflowPolicy` needs `http.PUT` or
  `http.DELETE` on `WorkflowPolicy` in **every** context it applies to (403
  otherwise). The API now stores a policy in one context; this matters for a
  policy stored earlier with a list `rbac_context`.
* A `WorkflowPolicy` governs every resource of its type in its context, for
  every user there. Treat `http.POST`, `http.PUT` and `http.DELETE` on
  `http://api.openteams.com/json-ld/WorkflowPolicy` as an administrative grant.
* Workflow state conditions are evaluated with scoped-rbac's CEL engine,
  `scoped_rbac.cel`, instead of `celpy`. A condition that cannot be evaluated
  for a resource, for example because it reads a property the resource does not
  have, still counts as not matching.
* An invalid workflow state condition (one that does not compile) now raises
  `django.core.exceptions.ImproperlyConfigured` when the policy is loaded.
* CEL equality differs from `celpy` in a few cases. Equality between
  different numeric types and between unrelated types is now defined: `1 == 1.0`
  and `3 != "x"` are true where they used to be evaluation errors (which
  counted as not matching), while `true == 1` is false. Resource data that CEL
  cannot represent (a mapping that is not a `dict`, a tuple, a `Decimal`, or an
  integer of 2**64 or more) now makes the condition not match.

### Side effects on commit

* The `SCRUD_WORKFLOW_ACTIONS` of a resource's type (`on_save`, `on_delete`)
  now run when the write commits (`transaction.on_commit` on the write's
  database), not inside it: a write that is rolled back (the post-save check's
  403, a 412, any error) runs none of them. Outside a transaction they run at
  once, as before. An action that raises never fails the write or the
  response, inside a transaction or not (a server-side `save()` outside one
  used to raise the action's error): Django logs the error on the
  `django.db.backends.base` logger, so route that logger to see it. An action
  can no longer veto a write by raising, nor read the write's uncommitted
  state; do that in a `scrud_post_save` receiver. `OnCommitSignalProcessor`'s
  index errors are logged the same way.
* `SCRUD_WORKFLOW_ACTIONS` may be a tuple as well as a list (a tuple made
  every API write a 500). Any other shape (a string, a mapping, `None`), or
  a path to something other than a mapping, raises `ImproperlyConfigured`
  at startup.
* `scrud_post_save` and `scrud_post_delete` themselves still fire inside the
  write, before the post-save check, with the same arguments
  (`transition_data` included): a receiver may read or change the database in
  the write's transaction, and the post-save check sees what it did. A
  receiver whose work leaves the database (mail, queues, HTTP calls) should
  wrap it in `transaction.on_commit`.
* New `scrud_django.search.OnCommitSignalProcessor` for haystack: it updates
  the search index when the write commits, so a write rolled back with 403
  stays out of the index. haystack's `RealtimeSignalProcessor` indexes inside
  the write; set `HAYSTACK_SIGNAL_PROCESSOR` to the new processor (see the
  README).

### Known issues

On MariaDB a list's query-parameter filter ignores trailing spaces in the
value ([mariadb-content-filter-padding](follow-ups.md#mariadb-content-filter-padding)).
Limitations kept by a decision are listed, with
the decision, under "Decided" in [follow-ups](follow-ups.md): the supported
isolation level
([policy-recheck-isolation](follow-ups.md#policy-recheck-isolation)), when
a removal moves a list's `Last-Modified`
([list-last-modified-deletes](follow-ups.md#list-last-modified-deletes)),
public type metadata
([public-type-metadata](follow-ups.md#public-type-metadata)), DELETE outside
the workflow
([delete-no-workflow-check](follow-ups.md#delete-no-workflow-check)), no cost
limit on administrators' conditions
([policy-grants-are-admin](follow-ups.md#policy-grants-are-admin)), callable
default policies
([eager-callable-default-policies](follow-ups.md#eager-callable-default-policies))
and the JSON Schema compiler's message in a 400
([schema-error-echoes-compiler-text](follow-ups.md#schema-error-echoes-compiler-text)).
