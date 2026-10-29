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
7. Workflow policies: check your stored `WorkflowPolicy` resources (at most one
   per type and context, in one context each, reported by
   `manage.py check --database default`; transition names must not start with
   `__`; `restrict_update` must be a list), and your
   `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` (run `manage.py check`; give
   policies as mappings).
8. Read the security caveat below: lists are not filtered per resource, so do
   not use the library for multi-tenant deployments yet.

### Security caveat: lists are not filtered per resource

List endpoints do not check each resource's RBAC context. A user who may list a
resource type (a grant in any context) sees every resource of that type in the
list, including resources in contexts where they have no access, and which
their detail URL hides from them with a 404. Do not rely on RBAC contexts to
keep resources of one type apart from users who may list that type. The same
goes for stored `WorkflowPolicy` resources (CEL conditions included): a user who
may list them sees those of every context. The library is not safe for
multi-tenant use until this is fixed. See
[list-not-rbac-filtered](follow-ups.md#list-not-rbac-filtered).

### Requirements and packaging

* Python 3.11 or newer and Django 5.2 are required.
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
* The `Link` header now uses `rel="describedby"` with the type
  `application/schema+json`.
* A failed precondition returns 412 with a JSON body.
* OPTIONS bodies use scrudful's Semantic HTTP format, with `x-rdf-context`.

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
* Unauthenticated requests now get 401.
* Request bodies must be JSON. `ResourceViewSet` and
  `ResourceViewSetGenericFilter` set `parser_classes = [RbacJSONParser]`
  (`scoped_rbac.rest`): a form-encoded or multipart body gets 415, and a JSON
  body that is not an object (for example a list) gets 400. A form body could
  name `rbac_context` twice; scoped-rbac authorized the request by the last
  value while the resource was stored in both contexts. Give your own
  `GenericViewMixin` views the same `parser_classes`.
* A create or update is checked again after the save, before it commits: the
  request's method must be allowed in **every** context the stored resource is
  in, or the write is rolled back with 403.
* `"rbac_context": ""` now means the default context everywhere, exactly as
  leaving the key out does. Before, a body with `"rbac_context": ""` skipped the
  default workflow policy, so its initial-state check, transition validity and
  transition authorization did not apply.
* Workflow transitions are authorized in every context the resource is in
  **and** every context the write moves it to (see Workflows below). A user
  who may only make some transitions in one context can no longer make others
  by moving the resource into a context where they may write.

### API changes

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
  lookup instead of `get_instance`.
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
* `ResourceViewMixin.get_instance(request, slug)` now returns `get_object()`
  and ignores `slug`. scrudful reads the validators (ETag, Last-Modified) from
  the same object, so an override of `get_instance` no longer affects them.
* `GenericViewMixin` subclasses must put scoped-rbac's `AccessControlledAPIView`
  before `GenericAPIView` in their bases, so that `get_object()` is scoped and
  permission-checked.
* `WorkflowValidator.apply`, `default_policies` and `cached_policies` are
  removed. A subclass that overrides `apply` is silently ignored. Default
  policies are built once in `scrud_django.conf`
  (`default_workflow_policies()`), and stored ones come from
  `scrud_django.workflow.workflow_policy_for`.
* `WorkflowTransitionError.policy` may be a stored-policy reference (an object
  with the policy's URL) rather than a dict, and is `None` in a generic error.
* A JSON Schema's `$ref` may point only inside the schema (`#/...`). A remote,
  `file:` or relative `$ref` is refused with 400
  (`https://api.openteams.com/http-problem/json-schema-invalid`) when a JSON
  Schema is written, and a stored schema that does not compile fails closed:
  every write of its type is a 400 with a generic detail.
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
  policy of every context the resource is in and every context the body names.
  So moving a resource between contexts needs the transition in both.
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
* A workflow-conditioned grant (`scrud_workflow` operator) no longer allows a
  write in a context where no workflow policy applies: the write is refused
  with 403. Unconditional and CEL grants are unaffected. A CEL condition on a
  grant that authorizes a transition now reads the request body, as in
  scoped-rbac's request check. The workflow check passes operators a
  `scrud_django.permissions.TransitionCheck`, a read-only mapping over the
  request body with a `transition` attribute: your own operators read it as
  the body, as they do in scoped-rbac's request check.
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
  400 (nothing is stored) when a state condition is not valid CEL, a transition
  is not an object with `from` and `to` naming defined states, a transition's
  name starts with `__` (reserved for `__INITIALIZE__` and `__SAME_STATE__`),
  `restrict_update` is not a list of defined state names, or `rbac_context` is
  present but not a string.
* There is at most one stored `WorkflowPolicy` per resource type and context. A
  create, or an update moving a policy into a context, that would make a second
  one is refused with 400 (not 409), without naming the existing policy;
  update or delete the existing policy instead. `WorkflowPolicy` writes lock the
  `WorkflowPolicy` resource type's row (`select_for_update`) and check again
  after the save, so of two concurrent writes the second is refused. If several
  were stored before (or past the API), none is picked: writes of the type in
  that context fail with `ImproperlyConfigured` until all but one are deleted.
  The database system check (`manage.py check --database default`, also run
  by `migrate`) reports them (`scrud_django.E002`), and policies stored with a
  list `rbac_context` (`scrud_django.W001`). The `WorkflowPolicy`
  schema now also declares `restrict_update` as an array of strings. These
  checks catch the policy errors known to make later writes fail; they do not
  prove that a policy which passes them is usable. A policy stored by an
  earlier version is not rechecked
  ([stored-invalid-workflow-policy](follow-ups.md#stored-invalid-workflow-policy)).
* A workflow error caused by a stored `WorkflowPolicy` now gives that policy's
  absolute URL in `policy` (and in `detail`, where the policy used to be)
  instead of its content. When the writer may not read that policy (no
  `http.GET` on `WorkflowPolicy` in its context), for example because it is
  another context's policy the resource is also in, the error is generic: the
  same status, `type` and `title`, a generic `message` and `detail`, and
  `policy`, `prior_state` and `proposed_state` null. Errors from a settings
  default policy still embed the policy
  ([workflow-error-embeds-policy](follow-ups.md#workflow-error-embeds-policy)).
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

### Known issues

See [follow-ups](follow-ups.md) for known issues. In particular:

* An empty body sent as `application/json` is read as `{}`
  ([empty-json-body](follow-ups.md#empty-json-body)).
* Search-index and workflow-action side effects of a write that is rolled back
  with 403 are not undone
  ([post-save-side-effects](follow-ups.md#post-save-side-effects)).
