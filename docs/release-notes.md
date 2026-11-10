# Change Log

## Unreleased

### Added

* UI branches in `scrud_django.ui`. A branch is a named set of UI components,
  stylesheets and (new) CSS items that overrides its parent branch, live or
  pinned to a version. Branches are resources (`ui-branches`, with read-only
  `ui-branch-versions` and `ui-pushes`), authorized by scoped RBAC in their own
  context. A branch can be published at a URL prefix
  (`scrud_django.ui.branches.middleware.PublishedBranchMiddleware`, after
  `AuthenticationMiddleware`), where the resource views serve the same resources
  with the branch's presentation; working branches are browsed with a header,
  a parameter or a cookie, a banner and a confirmation step. Pushes (which a
  `WorkflowPolicy` can gate), pulls with conflict detection, restores and
  per-branch versions move changes between branches. See the Branches chapter
  of the UI page and the `seed_todo_demo` command.

### Changed

* In `scrud_django.ui`, writing a component or stylesheet is authorized by RBAC
  in the item's branch context instead of a global trusted role; on `main`
  (context `""`) the rules are the same as before. Viewing a branch means
  trusting everyone who can write to it: read the Security section.
* A JSON list of UI items that names a branch (`Scrud-UI-Branch` header or
  `ui-branch` parameter) it may not show answers 400 (`ui-branch-unavailable`).
* The UI's `Vary` header adds `Scrud-UI-Branch` and `Cookie`. `scrud:page`
  gets a `css` list of the CSS file URLs; a custom page component should link
  them.
* Setting, changing or removing a branch's prefix (publishing it at site
  URLs), and deleting a published branch, need PUT on `ui-branches` in the
  context `""`. Setting or changing one refuses a prefix that overlaps
  another, hides the project's URLs or lies under the API's. A system check,
  `scrud_django_ui.W001` (a Warning), reports stored prefixes with those
  problems.

Behaviour for a site that creates no branch, and without the middleware, is
unchanged. The middleware costs one query per request outside the API's URLs.

### Fixed

* On MySQL, a resource whose JSON content is larger than the server's
  `sort_buffer_size` (256 KB by default) no longer breaks the reads that
  sorted it (error 1038, "Out of sort memory"): UI rendering, CSS files, item
  lists, versions, pull, push, restore and branch deletion read such rows
  unsorted and sort them in Python, and the resource list's page is found
  among the keys alone, then read by key (one more query per list page, on
  MySQL only). See `mysql-sort-buffer` in the follow-ups for what remains.

## 0.16.0

### Added

* `scrud_django.ui`, an optional app (add it to `INSTALLED_APPS` and install
  the new `ui` extra, `scrud-django[ui]`, which adds Jinja2) that serves every
  resource URL as `text/html`, chosen by `Accept: text/html`, rendered on the
  server exactly as the viewer is authorized. Templates (UI components, Django
  or sandboxed Jinja2) and CSS-like stylesheets that select them, layered per
  RBAC context, are stored as resources of two new types, `UIComponent` and
  `UIStylesheet`. Resources are edited in the browser with htmx 2.0.4 and
  `scrud-ui.js` through ordinary JSON PUTs. See the new UI page of the
  documentation, and the `seed_todo_demo` command of the demo project.
  The known limitations are in the follow-ups under the `ui-` names.

Behaviour without `scrud_django.ui` in `INSTALLED_APPS` is unchanged.

## 0.15.0

This release fixes the workflow authorization and post-write notification
issues found in a design review of 0.14.0 (scrud-django#5 to #18). Some of
the fixes refuse writes and policies that 0.14.0 accepted, and change what
workflow actions receive and when they run: work through Upgrading before
you deploy it.

### Fixes

* A workflow write is authorized into the state its content is stored in,
  and a state no longer depends on the order of the policy's conditions.
  A resource's state was the first state (the initial states, then
  `states`, in the policy's order) whose condition its content met, but an
  update's new state was looked up only among the states a transition leads
  to from the stored one. Where state conditions overlap, an update could
  be authorized (and reported to `scrud_post_save` and workflow actions) as
  one transition while its content was stored in another state, one that
  no transition the writer may make leads to. And "the policy's order" was
  the database's: a stored policy lives in a JSON column, and PostgreSQL's
  `jsonb` and MySQL's JSON do not keep an object's keys in order, so the
  same content could be in different states on SQLite or MariaDB and on
  PostgreSQL or MySQL. Content is now in the one state whose condition it
  meets: every condition is evaluated, and content that meets more than
  one is ambiguous (the new
  `scrud_django.exceptions.WorkflowAmbiguousStateError`, a
  `WorkflowInvalidStateError`). An update's new state is its content's
  state, as for stored content, and a transition must lead there from the
  stored state. Under a policy whose conditions overlap, writes that were
  accepted before are refused (400, `workflow-transition-invalid`; the
  body names the states the content meets in `matching_states`, unless the
  error is hidden from a writer who may not read the policy or the
  resource):
  * a create or update whose body meets the conditions of more than one
    state, whichever of them a transition leads to;
  * an update of a resource whose stored content meets the conditions of
    more than one state (Upgrading).

  `scrud_post_save` reports such a write (one that skipped the
  `WorkflowValidator`) as `None` in `transitions`, with the error as
  `transition_error`, as an invalid transition. Policies whose state
  conditions do not overlap are unaffected. A create (or a move into a
  context) is in the state it is stored in: a policy may no longer name a
  state in both `initial_states` and `states` (below) (#5).

* A grant conditioned on `scrud_workflow` no longer authorizes a custom
  write action. Whether the operator allowed a request (leaving the
  transition to the workflow check) depended on its method alone, so any
  `scrud_workflow` grant for `http.POST`, even with `authorized_transitions:
  []`, authorized a POST `@action` of a `ResourceViewSet` subclass, whose
  write the workflow check never sees: the action ran and wrote, and its
  OPTIONS offered the POST. The operator now allows only the scrud
  viewset's `create` and `update` actions (DRF's `view.action`, which
  `ResourceViewMixin.checks_workflow` reads), and refuses every other
  action (403), a view that is not a viewset included. A role that should
  call such an action needs a grant for its method that is not conditioned
  on `scrud_workflow`; a subclass whose own action writes only through
  `create` or `update` (so the workflow check runs) may override
  `checks_workflow` to include it. The constant
  `scrud_django.mixins.view_mixins.WORKFLOW_METHODS` (the methods the
  operator allowed) is removed; `WORKFLOW_VIEW_ACTIONS` (action to method)
  replaces it.

* `restrict_update` is no longer bypassed by a transition whose lists lead
  from a restricted state to itself. A transition leads from every state of
  its `from` to every state of its `to`, and one leading from a state to
  itself was taken before `restrict_update` was asked, so its holders could
  edit the state in place: `republish: {from: [published, archived], to:
  published}` let them edit a `published` resource under `restrict_update:
  [published]`. A state in `restrict_update` is now updated in place only
  through a transition whose `from` and `to` each name that state alone
  (`{"from": "published", "to": "published"}`, or one-element lists); any
  other in-place update of it is refused (400, `workflow-transition-invalid`,
  "Update not allowed on this state"). The other pairs of such a transition,
  and self-loops of states not in `restrict_update`, are unchanged.
* Two transitions that lead from one state to another (the same pair, such
  as `publish: {from: [draft, archived], to: published}` and `republish:
  {from: archived, to: published}`) silently overwrote each other, the last
  one winning, so the grant a write needed depended on their order. Such a
  policy is now invalid (`ImproperlyConfigured` naming both transitions and
  the pair): a `WorkflowPolicy` POST or PUT is a 400
  (`workflow-policy-invalid`), a stored one is reported by `manage.py
  check --database` (`scrud_django.E005`) and a settings default by
  `manage.py check` (`scrud_django.E001`).
* A workflow write no longer reads every stored `WorkflowPolicy` of its
  type. The policy lookup (`scrud_django.workflow.stored_policies_for`)
  read all of a type's policies, in every context, and kept those of the
  checked context in Python, once per context a write is checked in, again
  for the `scrud_post_save` report and in a policy write's uniqueness
  check: with one policy per tenant, every write cost O(tenants). It now
  selects them in SQL by their `ResourceContext` rows, and the contexts a
  write is checked in are looked up in one query (new
  `stored_policies_in(resource_type, contexts)` and
  `workflow_policies_for(resource_type, contexts)`). So that a policy
  whose rows are stale (a `QuerySet.update()` or `bulk_create()` past
  `Resource.save()`) still governs its contexts, the query also selects
  policies by content: one without any row, one whose `rbac_context` is
  the checked context or a list, and, in the default context, one that
  names none. This reduces the policies returned, decoded and compiled to
  those of the checked contexts (and the stale ones); the database still
  evaluates the filter on every `WorkflowPolicy` row of the type, since
  the content fallbacks are OR'd into it.
* A workflow policy that names a state in both `initial_states` and
  `states` is invalid: the state had two conditions, a create reading one
  and an update the other. It is refused like the other invalid policies
  (`ImproperlyConfigured` naming the state): a `WorkflowPolicy` POST or PUT
  is a 400 (`workflow-policy-invalid`), a stored one is reported by
  `manage.py check --database` (`scrud_django.E005`) and a settings default
  by `manage.py check` (`scrud_django.E001`). A create's initial state is
  now the state it is stored in, when that is an initial state.
* The `WorkflowPolicy` JSON Schema no longer allows a transition to be a
  list of transition specifications, which no version of the compiler
  accepted: such a policy is still a 400, now refused by the schema
  (`json-invalid`) rather than by the policy check
  (`workflow-policy-invalid`).
* The new `scrud_django.W005` database check (`manage.py check --database
  <alias>`, and `migrate`) warns when a state condition of a settings
  default policy reads a top-level property that the JSON Schema of its
  type does not define. State conditions compile without type checking,
  so a misspelled property was accepted and made its state unreachable.
  `rbac_context` is always allowed; stored policies are not checked. It
  reports nothing for a type that is not registered on the database
  checked, or whose schema is not stored there; for a schema without a
  `properties` object, or one that may allow other properties
  (`additionalProperties` `true` or a schema; absent counts as `false`)
  or add some through `patternProperties`, `allOf`, `anyOf`, `oneOf`,
  `$ref`, `if`/`then`/`else`, `dependencies`, `dependentSchemas` or
  `unevaluatedProperties`; for a default that is invalid (E001 reports
  it); and on a database that is not migrated. The README's new "Workflow
  policies" section also says that a state condition reads `rbac_context`
  as stored (a list for a resource in several contexts), unlike a grant's
  condition, which reads the context being checked. A state condition
  that fails to evaluate (a property the content lacks) or gives no
  boolean is no match, as before, and is now logged at DEBUG on the
  `scrud_django.workflow` logger, naming the policy and the state. A
  failure of W005's type checker on a condition reports nothing for it,
  and is logged there at DEBUG with its traceback.
* A resource read from a read replica (a router whose `db_for_read` names
  another alias than its `db_for_write`) and then deleted ran its `on_delete`
  workflow action at once, on the replica's connection, before the delete
  committed and even if it rolled back. `scrud_post_save` and
  `scrud_post_delete` are now sent with `using` the database the write went
  to (`using`, else the router's `db_for_write`), and `ScrudSignalProcessor`
  registers the action there; without a `using` it asks the router, no
  longer the instance's `_state.db` (the database it was read from) first.
  Receivers that read `using` get that alias where they used to get `None`
  (#6).
* An `on_delete` workflow action (and any `scrud_post_delete` receiver)
  could not tell which resource was deleted: the signal is sent after
  Django's delete, which sets the instance's `pk` to `None`.
  `scrud_post_delete` now also sends `pk`, the deleted resource's primary
  key, taken before the delete; `instance` is unchanged (#7).
* `SCRUD_WORKFLOW_ACTIONS` entries are checked. A type's entry that is not
  a mapping made every write of the type a 500 (`TypeError` inside the
  write); it now runs no action, and is logged on the new
  `scrud_django.workflow_actions` logger. A misspelled key or type URI, or a
  value that is not callable (a dotted path), ran nothing, silently or with
  an anonymous log at commit. New system checks report them:
  `scrud_django.E007` (an entry that is not a mapping, or a setting that
  cannot be loaded), `E008` (a key other than `on_save` and `on_delete`),
  `E009` (a value neither callable nor `None`) and the database check
  `W006` (`check --database <alias>`: a type URI that is not a registered
  resource type). An action that raises is logged once, on
  `scrud_django.workflow_actions`, with its qualified name, the type URI,
  `on_save` or `on_delete` and the resource's pk; it is no longer logged by
  `transaction.on_commit` on `django.db.backends.base` (#10).
* A `Resource.objects` queryset's `delete()` (bulk deletes, and the admin's
  "delete selected") sent no `scrud_post_delete`, so no `on_delete` action
  ran for the resources it deleted. It now sends one per deleted resource,
  inside the delete, with `pk`, `using`, `resource_type_uri` and
  `action='DELETE'`, as `Resource.delete()` does, and the actions run when
  it commits. Should Django report more resources deleted than were
  signalled (the `post_delete` receiver `scrud_django.stamp_deletion`
  disconnected), the missing pks are logged as an error on
  `scrud_django.workflow_actions`. Raw saves (`loaddata`,
  django-reversion's revert), `QuerySet.update()`, `bulk_create()` and
  `bulk_update()` still send no scrud signal; this is now documented
  (README "Workflow actions") (#11).
* README "Workflow actions" now states how actions are delivered (at most
  once, synchronously after the commit and before the response, in write
  order per connection, with the live instance), what `transition_data`
  holds (`None` without a policy, `__SAME_STATE__`, `__INITIALIZE__`), how
  to get the requesting user (a middleware-set `ContextVar`), and shows a
  transactional outbox for events that must not be lost (#12).
* `scrud_post_save`'s `transition_data` lost information: a move into a
  context was reported as `__INITIALIZE__` without the state left, only the
  first context with a policy was reported, and `None` meant either "no
  policy applies" or "the transition is invalid" (a server-side write the
  workflow check did not see). `scrud_post_save` (and so every `on_save`
  action) now also sends `transitions`, a read-only mapping
  (`types.MappingProxyType`, the same one for every receiver) of each
  context the stored resource or the new content is in, where a policy
  applies, to the write's `TransitionData` there: the transition where the
  resource stays (`None` if invalid there), `__INITIALIZE__` where it
  enters, and the new reserved `__LEAVE__` (no proposed state; the prior
  state is the one it left) where it leaves; and `transition_error`, the
  workflow error that made `transition_data` `None`. `transition_data`
  itself changed as described under #5 (content that meets the conditions
  of more than one state is `None`, with the error) and the
  `restrict_update` fix (#9: an in-place update of a restricted state
  through a transition whose lists lead from it to itself is `None`, with
  the error, no longer that transition), and in two more cases: a create,
  or a move into the context, whose content is in no initial state is
  `None` with a `WorkflowInvalidInitialStateError` as `transition_error`
  (it was `TransitionData(None, None, "__INITIALIZE__")`, with no error),
  and is `None` in `transitions` there too; and a save whose row is not on
  the database written to (an instance copied with `save(using=...)`) is
  reported as a create instead of raising `AttributeError` (#15).
* A server-side save (the admin, `ResourceRegistration`, server code) of
  an invalid transition, one the workflow check would have refused, was
  saved silently: its `transition_data` was `None`, and nothing was
  logged. `Resource.save()` now logs each context where the save is
  invalid as a WARNING on the new `scrud_django.workflow` logger, once the
  row is written and before `scrud_post_save` is sent, naming the
  resource's pk, its type URI, the context, the policy and the error (and
  the states the content meets, when it meets several).
* `ScrudSignalProcessor` read `SCRUD_WORKFLOW_ACTIONS` once, when it was
  built, so `override_settings(SCRUD_WORKFLOW_ACTIONS=...)` did not change
  the actions the app ran. It now reads the setting on first use and again
  after it changes (`setting_changed`); a setting changed to one that
  cannot be loaded runs no action and is logged, never failing the write.
  Its receivers were connected weakly and without a `dispatch_uid`: a
  processor nobody kept was garbage-collected at once and did nothing, and
  a second one kept alive ran every action twice. They are now connected
  strongly under a fixed `dispatch_uid` per signal: building a processor
  replaces the one connected before (`connect()` connects one again). The
  class attribute `workflow_actions = {}`, shadowed by every instance, is
  now a property: a read-only view of the setting's actions, which every
  processor shares (`processor.workflow_actions[uri] = ...` raises
  `TypeError` rather than changing them for all), unless a mapping of the
  processor's own is assigned to it, which it returns as assigned;
  the dead `ScrudSignalProcessor()` in the test app's `ready()` is gone
  (#16).
* A resource type whose registration failed with a database error when the
  URLconf was imported (`json_resource_type` and the built-in types of
  `scrud_django.urls`) had no routes until the process restarted: a worker
  that started while the database was briefly unreachable answered 404 for
  those types, with only a warning ("expected for migrations") logged.
  The routes need no database, so they are now always returned, and the
  registration is retried when the type is next read (by a request or by
  `ResourceRegistration`): the process serves the type once the database is
  back. Until then, a request for the type fails with the retry's error (a
  500), not a 404. A retry inside a transaction that is rolled back is
  retried again. Two threads reading a pending type at once may each
  register it, as two processes starting at once may
  ([type-registration-race](follow-ups.md#type-registration-race)).
  `migrate`, `makemigrations` and `check` on a database not yet migrated
  still work.
* A process start rewrote every existing type's row: under `DEBUG` with a
  new ETag and modification time, otherwise as it was, each time with a
  django-reversion revision. A type whose row and documents are unchanged
  is now not written. Under `DEBUG`, a type whose row does change still
  gets a new ETag and modification time.
* A slug's "latest revision" (`get_resource_type_for`, where a slug has
  more than one row) was the greatest `revision` string, so `"9"` was read
  before `"10"`. It is now the highest number (a longer revision first,
  then the greater string, which orders revisions written as numbers
  without leading zeros numerically on every database), and rows of the
  same revision are read oldest first, so that every process reads the
  same one ([type-registration-race](follow-ups.md#type-registration-race))
  (#18). A process start updates that same row; it updated whichever row
  the database returned first, which reads might not use.
* The `W003`, `W004` and stored workflow policy (`E002`, `E003`, `E005`,
  `W001`) database checks passed silently when reading a migrated database
  failed, hiding a fault such as a missing grant. They now warn
  `scrud_django.W007`, naming the checks, the database and the error. The
  new `W005` and `W006` report such a failure with `W007` too, and so does
  `W006` when the URLconf cannot be imported. A database that is not
  migrated is still not checked, so that `migrate` runs, and neither is
  one that holds none of scrud_django's tables (a router's `allow_migrate`
  keeps them out), though Django records scrud_django's migrations as
  applied there.

### Upgrading

* A workflow policy in which two transitions cover the same (from, to) pair
  of states is now invalid. Run `manage.py check --database <alias>` for
  each database that stores `WorkflowPolicy` resources (and `manage.py
  check` for `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]`) before
  deploying, and give each pair one transition: a stored invalid
  policy fails every write of its type in its contexts closed
  (`ImproperlyConfigured`, a 500) until it is fixed or deleted, and an
  invalid default fails every write of its type.
* The workflow policy of a context is now found through the policies'
  `ResourceContext` rows, with the content as a fallback for stale rows
  (Fixes). The one case it misses is a policy whose `rbac_context` server
  code changed past `Resource.save()` (`QuerySet.update()`,
  `bulk_update()`) to a number, boolean or object (the default context)
  while its rows still name another context. If your code writes
  `WorkflowPolicy` resources that way, run `manage.py scrud_sync_contexts
  --check` (and without `--check` to repair) after deploying.
* A resource whose content meets the conditions of more than one state of
  its workflow policy can no longer be updated (400,
  `workflow-transition-invalid`) until the policy's conditions are made
  mutually exclusive (README "Workflow policies"), or its content is fixed
  by server code (`Resource.save()` past the API). Such content was stored
  before this release, or before a policy edit made the conditions
  overlap; it is not detected by any check. Before deploying, review your
  policies (stored `WorkflowPolicy` resources and
  `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]`) for conditions that can
  both hold, and give each state a condition that excludes the others'.
* A workflow policy that names a state in both `initial_states` and
  `states` is now invalid. Before deploying, run `manage.py check
  --database <alias>` for each database that stores `WorkflowPolicy`
  resources, and `manage.py check`, and give each such state one name and
  one condition: a stored invalid policy fails every write of its
  type in its contexts closed (`ImproperlyConfigured`, a 500) until it is
  fixed or deleted, and an invalid default every write of its type.
* An update in place of a state in `restrict_update` through a transition
  whose `from` or `to` lists other states as well is now refused. To keep
  allowing such edits, add a transition from that state to itself alone
  (`{"from": "published", "to": "published"}`) and grant it to the roles
  that should make them.
* A grant conditioned on `scrud_workflow` no longer authorizes a write
  other than a scrud viewset's `create` (POST to the list) and `update`
  (PUT to the detail route): a POST or PUT `@action` of a `ResourceViewSet`
  subclass, or a view of your own that is not a viewset, is now a 403 for a
  role holding only such a grant, and its OPTIONS no longer offers the
  method (#8). Give a role that should call such an action a grant for its
  method that is not conditioned on `scrud_workflow` (and check its writes
  in the action), or, for an action that writes only through `create` or
  `update` (so the workflow check runs), override
  `ResourceViewMixin.checks_workflow` to include it.
* Run `manage.py check --database <alias>` for each database alias: it
  also runs the checks that take no database (`scrud_django.E001` and
  `E007`-`E009` among them), while the database checks look only at the
  alias passed (`E002`, `E003`, `E005`, `W005`: the policies and types
  stored there), and `W006` only when that alias is the one `ResourceType`
  is written to (`router.db_for_write`). The new `E007`-`E009` errors fail
  `check` (and `runserver`, `migrate`) on a `SCRUD_WORKFLOW_ACTIONS` entry
  that never worked; fix or remove it. A
  `None` action is accepted (no action). Route the
  `scrud_django.workflow_actions` logger where you watch errors; a log
  filter or alert on `django.db.backends.base` for action failures no longer
  sees them.
* `on_delete` actions and `scrud_post_delete` receivers now also run for
  queryset and admin bulk deletes, once per deleted resource: make sure they
  can take a bulk delete's volume.
* A server-side save (`Resource.save()`: the admin,
  `ResourceRegistration`, your code) now reads the workflow policy of every
  context the stored resource and the new content are in, to report
  `transitions` (#15); it read only the first one's. Like an API write, it
  now fails closed (`ImproperlyConfigured`, before it writes) where any of
  those contexts has several stored `WorkflowPolicy` resources for its type,
  also a secondary context or one it moves the resource out of, or an
  invalid one. Before deploying, run `manage.py check --database <alias>`
  for each database that stores `WorkflowPolicy` resources, and delete all
  but one policy per type and context (`scrud_django.E002`) and fix or
  delete the invalid ones (`scrud_django.E005`).
* `scrud_post_save` sends two more keyword arguments, `transitions` and
  `transition_error`: an `on_save` action (or receiver) that names its
  keyword arguments must also take `**kwargs`.
* `scrud_django.workflow.TransitionData` is now immutable (a frozen
  dataclass; the same instance may reach several receivers): code that
  assigned to its `proposed_state`, `prior_state` or `transition` must build
  a new one (`dataclasses.replace`). The reserved transition names are the
  new constants `scrud_django.workflow.INITIALIZE`, `SAME_STATE` and
  `LEAVE`; a policy's refused reserved name (`__` prefix) now lists all
  three.
* `ScrudSignalProcessor.run_on_commit(action, sender, kwargs, kind)` takes
  the kind of action (`on_save` or `on_delete`, which a failure's log
  names) as a required fourth argument: a subclass that overrides or calls
  it must take or pass it.
* `ScrudSignalProcessor.workflow_actions` is now a read-only view of the
  `SCRUD_WORKFLOW_ACTIONS` setting's mapping, which every processor
  shares: a subclass that changed it in `__init__`
  (`self.workflow_actions[uri] = ...`) now gets a `TypeError`. Assign the
  processor a mapping of its own (`self.workflow_actions = {...}`), or put
  the actions in the setting.
* Route the new `scrud_django.workflow` logger where you watch warnings:
  it reports server-side saves (the admin, `ResourceRegistration`, your
  code) that are invalid under a workflow policy and were saved anyway.
* A `ScrudSignalProcessor` (or subclass) instance built in addition to the
  default one now replaces it rather than running alongside it: build it
  in the `ready()` of an app listed after `scrud_django`, and do not count
  on both running. The signals hold it: one built without keeping a
  reference now runs, until another is built.
* A resource type registration that fails with a database error at URLconf
  import is now logged by the `scrud_django.registration` logger (it was
  the root logger), naming the type, and `scrud_django.urls` no longer logs
  an error for it. It is a warning while the database is unreachable, not
  migrated, or its migration state cannot be read (a database error), and
  an error (no longer "expected") on a database that is reachable and
  migrated, where it is a fault, such as a missing grant, that fails every
  request for the type, or when its migration state cannot be told for
  another reason (a broken migration graph, of any app, which is logged
  with its traceback): alert on errors from that logger. `manage.py check`
  on a database that is not migrated reports a `scrudful.W001` warning for
  more routes (those of the types
  that could not be registered, which were missing before). They are
  warnings only (`migrate` runs), and are not reported once the database
  is migrated.

### Documentation

* `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]` gives fallbacks, not
  floors: a stored `WorkflowPolicy` for a type in a context replaces the
  default there, and may be looser. Writing one needs grants on the
  `WorkflowPolicy` type only, not on the type it governs, and it can rename
  or redefine the transitions that the context's `scrud_workflow` grants
  name. The behaviour is unchanged; the README ("Workflow policies") now
  says so
  ([policy-defaults-are-fallbacks](follow-ups.md#policy-defaults-are-fallbacks)).
  Treat writes of `WorkflowPolicy` as administrative grants (#17).

## 0.14.0

This release requires django-scoped-rbac 0.12.1 and scrudful 0.10.1
(Semantic HTTP 0.1 at spec commit `37ec1ce`), which are breaking: OPTIONS is
a 204 without content linking an HTTP Schema Resource (HSR), `Allow` lists
the methods the requester's grants allow, and a create's preconditions are
evaluated by scrudful. Work through Upgrading before you deploy it.

### Upgrading

1. URLs: include scrudful's HSR route once, in the root URLconf:
   `path("", include(scrudful.rest_framework.hsr_urls()))`. Without it every
   request of a scrud view raises `ImproperlyConfigured` (`manage.py check`
   reports `scrudful.E003`).
2. Settings: set `SCRUDFUL["BASE_URL"]` to the URL the API is served at.
   The HSR is built without a request; its schema and context links, and
   the HSR link each response carries, resolve against it. scrudful
   resolves only relative links against it: scrud's other response Links
   are absolute URLs built from the request, as before.
3. Migrate: django-scoped-rbac's `0008_collectionlock` adds the table of its
   collection lock (`scoped_rbac.CollectionLock`). A `GenericViewMixin`
   view's conditional create locks it on the view's `locked_write_database()`
   (the database its model is written to, below): run `migrate` there, and,
   with a database router, let its `allow_migrate` put `scoped_rbac`'s
   `CollectionLock` on that database. scoped-rbac's own `roles/` and
   `role-assignments/` lists (below) take that lock too, on the database
   their models are written to. `locked_write_database()` names the model
   by the view's `queryset` attribute, else its `serializer_class`
   attribute's `Meta.model`, else it is the default database (it runs
   before DRF wraps the request, so it calls no `get_queryset()`). A view
   whose model only `get_queryset()` names, written elsewhere, overrides
   it: once the request is authorized, the inherited one's transaction on
   another database than the one the router writes the model to is
   refused (`ImproperlyConfigured`) before any lock or write. So is one
   whose `get_queryset()` selects another database (`using()`), unless
   the view says how: a `get_queryset()` reading from a replica of the
   write database sets `reads_replica = True` on the view (new). That only
   lifts the refusal: inside the transaction the lookup, the list's
   validators and the locks read the write database whatever
   `get_queryset()` selects, so a list `ETag` a client got from the
   replica may lag behind, and a POST evaluated against it then gets a
   412. A view that selects its database by hand overrides
   `locked_write_database()` to return it, and must create there too (a
   `ModelSerializer` creates where the router writes); that is not
   checked. A replica view that sets neither gets `ImproperlyConfigured`
   on every PUT, PATCH, DELETE or conditional POST it is authorized for.
   (A `ResourceViewMixin` view has no `reads_replica`: its locked lookup
   reads the write database whatever its `get_queryset()` selects.)
4. Clients: an OPTIONS is a 204 without content or `Content-Type`; GET the
   document its HSR `Link`
   (`rel="https://w3id.org/semantic-http/ns#http-schema-resource-document"`)
   names for what the OPTIONS body held (`get`, `post`, ... with their
   schemas and `x-rdf-context`); it is the same for every requester.
   `Allow` lists the methods the requester's grants allow there (an
   anonymous user, or one without grants, may get `OPTIONS` alone). A
   list's POST is checked without a body, so it is listed when they may
   write in some context, whatever conditions their grants put on the body.
   A detail route's methods are checked against the stored resource, which
   the OPTIONS loads as the first method the requester may use somewhere
   loads it (GET, when they may read in some context): a resource that is
   missing, or that method may not reach, is a 404 (for a reader, as its
   GET is). A requester who may use no method there gets 204 with
   `Allow: OPTIONS` whether or not it exists. Every response of a scrud
   resource route but a CORS preflight carries the HSR link after its
   other Links; a list envelope's `link` is unchanged (the resource's schema
   and context). The collection schema and context documents
   (`collections-json-schema`, `collections-json-ld`) are plain DRF views:
   their OPTIONS is DRF's 200, with no HSR.
5. Clients of scoped-rbac's `roles/` and `role-assignments/` lists, which
   `scrud_django.urls` mounts: they now send `ETag` and `Last-Modified`, and
   a POST to them evaluates `If-Match`, `If-None-Match` and
   `If-Unmodified-Since` (412, 400), as scrud's own lists do.
6. Your code: a subclass of a scrud viewset that overrides a handler
   (`list`, `create`, ...) must be decorated with `@scrudful_viewset` again,
   so that scrudful handles the override, and an `@action` of one needs
   `@scrudful(...)` (`scrudful.E007` otherwise).
7. Your code: on a `GenericViewMixin` view, scrudful now evaluates a POST's
   preconditions against the list (as the requester would GET it) before
   your `create` runs, and when they need the list's validators the view
   holds scoped-rbac's collection lock of its model
   (`conditional_create_lock`) from after the request is authorized until
   the response. A `create` that scrudful serves need not call
   `list_precondition_response` or take a lock (calling it gives `None`:
   scrudful has evaluated). If your `create` took a lock of its own to
   serialize creates (the 0.13 pattern), move it into an override of
   `conditional_create_lock`: it must be held before the list is read,
   which is now before `create` runs.

   Every POST to the list (any POST but one to an item: `create`, an
   `@action(detail=False)` or the handler `@<action>.mapping.post` adds to
   one, or a POST handler of your own on a URL naming no item) carrying
   `If-Match`, `If-None-Match` or `If-Unmodified-Since` (but an
   `If-Match: *` without `If-None-Match`, which always passes on a list)
   now runs in one
   transaction, holding the lock when they need the list's validators,
   that an error response (4xx, 5xx) rolls back, and its preconditions
   must be evaluated. scrudful evaluates them before a scrudful handler
   with list validators runs (a viewset's `create`, which
   `scrudful_viewset` gives `Meta`'s, yours or scrud's), also when an
   undecorated handler of yours calls it. It evaluates nothing for a
   handler whose `@scrudful` has none (as for an `@action`, unless you
   pass it `list_etag_func=ScrudfulMeta.list_etag_func` and
   `list_last_modified_func=ScrudfulMeta.list_last_modified_func`), nor
   for a `create` such a handler calls (only the outer of two nested
   scrudful handlers evaluates; so also a viewset that a `@scrudful`
   function view calls), nor for a `create` override you did not decorate
   again with `@scrudful_viewset` that does not call `super().create()`.
   Call `list_precondition_response` first there, or in your `create`, as
   `ResourceViewMixin.create` does, and return the response it gives. A
   response that is no error (a success, a redirect) whose preconditions
   nothing evaluated, whether or not the handler wrote (a list action
   that searches too), raises `ImproperlyConfigured`, and what it wrote is
   rolled back. What an evaluation answers is your handler's to return: a
   handler that gets a 412 or 400 (from a scrudful handler it calls, or
   from `list_precondition_response`), or an exception an evaluation
   raised, must return it or let it propagate. One that drops it and
   answers otherwise may go undetected, and what it wrote then commits.
   A `dispatch` or `initial` of your own must call `super()`: a
   conditional create whose lock was not taken raises
   `ImproperlyConfigured` rather than run unserialized, whichever the list
   callbacks (yours too). A POST to an item is not covered: see
   Known issues. A handler of your own (no `@action`) routed with
   `as_view` on a collection nested under a parent whose URL keyword is
   the view's lookup keyword (`slug` for scrud's views:
   `parents/<slug>/notes/`) is taken for a POST to an item: pass
   `detail=False` to `as_view`, as a router does (a view that is no
   viewset: set `detail = False` on it), or make it an
   `@action(detail=False)`.
8. `manage.py check` warns `scrudful.W001` for the routes of a resource type
   whose HSR cannot be built, `json-schema-list` and `json-schema-detail`
   among scrud's own (Known issues). On a database not migrated yet
   `scrud_django.urls` registers no resource-type route, as before, so it
   warns nothing for them. A scrud view you route yourself (with `as_view`
   or a router) is still checked: its routes whose link callables read the
   resource type (one served by the viewset's `list`, and one served by
   `create`, `retrieve`, `update` or `destroy` with a
   `resource_type_name`) warn `scrudful.W001`; one served only by an
   `@action` whose `@scrudful` has no link callables does not. Migrate,
   then check.

### Changed

* OPTIONS, the HSR and `Allow` come from scrudful 0.10 (above). The body
  checks (an empty body, one nested too deep or holding a NUL, an
  `rbac_context` that cannot be stored) leave alone the body-less copies of
  an OPTIONS on which scrudful checks each method for `Allow`
  (`scoped_rbac.access.checks_allow`): without this every OPTIONS of a
  scrud route would be a 400 `body-missing`.
* A create's preconditions: scrudful 0.9 evaluates them before the view's
  `create` runs, against `Meta.list_etag_func` and
  `Meta.list_last_modified_func`, which for a POST give the list as the
  requester would GET it (`ListValidatorsMixin.create_list_validators`):
  the same 412, 400 and `conditional_list_too_large` 400 as before, the
  same problem details. The transaction of a POST to the list with
  preconditions to evaluate (any but an `If-Match: *` with no
  `If-None-Match`) now opens in `LockedWriteMixin.dispatch`. A conditional
  create (preconditions that need the list's validators) takes its lock
  (`conditional_create_lock`: the type's row for `ResourceViewMixin`, as
  before) in `initial`, after authentication, permissions and throttles,
  so scrudful evaluates under it. scrud records
  that scrudful evaluated them as it calls a handler's list callbacks
  (whichever its class or callbacks; `gate_uniform_errors` wraps them),
  which scrudful calls for a POST only to evaluate; whether they passed is
  not recorded. `ResourceViewMixin.create` evaluates them itself
  (`list_precondition_response`) when nothing has, so that a create called
  from an `@action` is evaluated too, and once.
* A locked write (PUT, PATCH, DELETE) rolls back on an error response
  (4xx, 5xx) only; a redirect (3xx) commits. It rolled back on any status
  outside 2xx, so a write answered with a redirect to what it wrote was
  lost. A conditional POST's transaction (above, new) follows the same
  rule.
* `GenericViewMixin` views' creates evaluate their preconditions (they
  evaluated none unless your `create` called `list_precondition_response`),
  and conditional ones run one at a time under scoped-rbac's
  `lock_collection` of the view's model.
* A request rewritten from an OPTIONS (middleware setting `request.method`,
  say from `X-HTTP-Method-Override`), which looks like scrudful's body-less
  copy of an OPTIONS, is refused (403, 401 for the anonymous) by
  `LockedWriteMixin.initial` too, whatever the order of a view's bases.

### Fixed

* `docs/follow-ups.md`, `scrudful-create-preconditions`: closed. scrudful
  evaluates a create's preconditions, and django-scoped-rbac 0.12 its own
  role and role-assignment lists'.
* Writes of resources (`ResourceViewMixin` views, `ResourceRegistration`)
  and the registration of resource types read the type, and the schema a
  body is validated against, on the type's write database (`type_db()`),
  not through the router's `db_for_read`, which a primary/replica router
  ignoring hints (as Django's documented example does) sends to a replica
  that may lag behind. The type's schema, context and indexing policy are
  read with it, on that database (`registration.TYPE_DOCUMENTS`). Through
  the router, in 0.13.0:
  - a create (conditional or not) and `ResourceRegistration.register` read
    their type;
  - a create and a PUT loaded the type's schema and context (as related
    objects): a body the current schema refuses could be validated against
    an older one, and stored;
  - `ResourceRegistration.delete`'s `scrud_post_delete` loaded the type
    again;
  - `ResourceTypeRegistration` (a type registered at startup) read the
    type, and the schema, context and indexing policy it compares with the
    files: a schema edited since was left in place, a type the replica did
    not have yet was stored again as a second row, and (outside `DEBUG`)
    the replica's copy of the type row was saved back;
  - after an API create or PUT (whose save check refreshes the saved
    resource, emptying its related objects), its type was loaded again by
    what read it later: the `Location`, and what runs on commit with it,
    the `SCRUD_WORKFLOW_ACTIONS` `ScrudSignalProcessor` defers and search
    indexing (which then indexed by the replica's indexing policy,
    or not at all where the replica lacked the type: the error is only
    logged);
  - a refused transition's link to its workflow policy loaded the policy's
    type (a 500 where the replica lacked it).

  A conditional create locks the type row it read: read through the router,
  two creates of a slug with several rows (which the registration above
  could store) could lock different rows, and not run one at a time.
  `GenericViewMixin.get_resource_type` still reads through the router: its
  type gives the view's links and the serializer's `resource_type` only.

### Known issues

* A POST `@action(detail=True)` that delegates to `update` (or writes the
  item itself) does not evaluate the item's `If-Match`: scrudful evaluates
  only the action's own `@scrudful`, and a POST takes no row lock
  (`docs/follow-ups.md`, `detail-post-action-preconditions`). Give the
  action's `@scrudful` the item's validators
  (`etag_func=ScrudfulMeta.etag_func`,
  `last_modified_func=ScrudfulMeta.last_modified_func`,
  `object_validators=True`): scrudful then loads the object first (its
  404 or 403 comes before any 412) and evaluates the POST against it
  (`If-None-Match: *` then fails). Or evaluate it in the action. Either
  way the row is not locked, so a concurrent write can still slip
  between.
* The HSR of a resource type with a schema and no JSON-LD context, such as
  `json-schema`, cannot be built: its GET is a 500 (`docs/follow-ups.md`,
  `hsr-context-less-types`). Its resources are served as before.

## 0.13.0

A grant's CEL condition that reads `rbac_context` now reads the context it
is evaluated in, in every check (scrud-django#2). This changes what existing
conditions on `rbac_context` allow, and a `has_context_permission` of your
own now only narrows what the grants allow on the detail route and after a
write: work through Upgrading before you deploy it.

### Upgrading

1. Roles: review grants whose CEL condition reads `rbac_context`. Each
   check now gives the condition the content with the context it is
   evaluated in as its `rbac_context`, where 0.12.1 gave it the value as
   written or stored: a list, or a value that is not a string (`null`, a
   number, an object), which stores the resource in the default context
   `""`. A resource without an `rbac_context` is read with `""` in `""`, so
   `rbac_context == ""` now holds for it there.
   * `rbac_context != "x"` now refuses a resource in `"x"` however it is
     stored, a write into `""` with an `rbac_context` that is not a string
     (under `!= ""`), and a workflow transition that moves a resource out
     of `"x"`. It is still no exclusion: a resource stored as `["x", "y"]`
     is readable, and may be updated (and moved out of `"x"` where no
     workflow policy applies there) or deleted, by a grant that holds in
     `"y"`, as access to a resource in several contexts needs any one of
     them. Keep a grant out of a context by assigning it in the contexts
     it covers (`RoleAssignment.rbac_context`).
   * `rbac_context == "a"` now holds for a resource stored as `["a", "b"]`
     where it is checked in `"a"`: such a grant in `"a"` can read, update
     and delete it, and move it out of `"b"` where no workflow policy
     applies there (removing a resource from a context needs no grant
     there, as before; a grant there conditioned on `scrud_workflow` still
     refuses it, `docs/follow-ups.md`, `workflow-grant-blocks-leaving`).
   * A condition written for a list now reads one context: `"a" in
     rbac_context` and `size(rbac_context) > 1` no longer hold as they did,
     and string functions such as `rbac_context.contains("a")` now match
     against that one context. Rewrite them to compare `rbac_context` with
     one context.
2. Your code: an operator function, and a `has_context_permission`,
   `has_object_permission` or `contexts_for` of your own, get the content
   as stored (or, in the workflow check, the body as sent), its
   `rbac_context` the whole value, as before. A `has_object_permission` of
   your own that asks `super()` reaches scoped-rbac's
   `has_context_permission`, whose CEL conditions read each context. A
   `has_context_permission` of your own, and its `super()` call, read the
   stored content, so on a view with one the scrud views now also ask the
   grants themselves, each context read as the resource is there, on the
   detail route and in the post-save check, as its list already did: such
   an override narrows what the grants allow, and one that allowed what the
   grants refuse no longer does. To have its `super()` call read each
   context too, pass it
   `scrud_django.permissions.condition_resource(obj, context)`.
3. Your code: a view of your own that composes scoped-rbac's
   `AccessControlledAPIView` with `ResourceViewMixin` or `GenericViewMixin`
   should put `scrud_django.mixins.view_mixins.ReadableMixin` first, as the
   scrud viewsets now do. Otherwise a refused write of a resource readable
   in one of its contexts is a 404, not a 403.

### Authorization

* Every check scrud-django runs gives a CEL condition the content as it is
  in each context, and an operator the content itself
  (`scrud_django.permissions.condition_resource`, a scoped-rbac
  `RequestResource`), as scoped-rbac's request check reads a list
  `rbac_context` since 0.11.2:
  * the object checks of a detail route and the post-save check, through
    the `has_context_permission` of each `IsAuthorized` that keeps
    scoped-rbac's (`reading_content`), and beside one with its own
    (`ListValidatorsMixin.check_grants_in_each_context`,
    `scrud_django.filters.grants_allow`);
  * whether a refused resource is a 404 or a 403: the scrud viewsets'
    `is_readable` is `object_readable` (`ReadableMixin`), whose grants check
    (`scrud_django.filters.readable_by_grants`) is scoped-rbac's
    `AccessControlledAPIView.is_readable` reading each context so;
  * the workflow check, in every context it checks, those an update moves
    the resource out of included;
  * lists, on both their SQL and per-object paths;
  * whether a workflow error names a stored policy or resource.
* Closed by this: a writer whose grant refuses `""` by a condition
  (`rbac_context != ""`) could write into `""` with an `rbac_context` that
  is not a string; a grant whose condition refuses `"x"` could read, list,
  overwrite and move a resource stored only in `"x"` as a list; and a
  writer under such a condition could move a resource out of `"x"` under
  `"x"`'s workflow policy.
* A context that a `contexts_for` of your own names and that is not one of
  the resource's leaves the object as it is (`docs/follow-ups.md`,
  `custom-contexts-conditions`).

## 0.12.1

This release requires django-scoped-rbac 0.11.2, which closes a way to write
a resource into a context a conditional grant refuses.

### Upgrading

1. Install django-scoped-rbac 0.11.2 or newer, below 0.12. The new floor
   comes with scrud-django 0.12.1; raise any pin of your own.
2. Clients: a POST or PUT whose `rbac_context` is a list holding an array or
   an object (`["a", ["b"]]`, `["a", {"@id": "x"}]`) now gets 400, for
   superusers too. Send a list of strings. scrud-django 0.12.0 stored such
   a resource in the default context `""`.
3. Roles: scoped-rbac's request check now reads each value of a list
   `rbac_context` on its own when it evaluates a condition on
   `rbac_context` (see Authorization). A create or update that a condition
   such as `rbac_context != "x"` let into `"x"` through a list is now a
   403. Resources already stored that way stay where they are, and such a
   grant can still read, overwrite and move them (see Known issues): look
   for resources whose `rbac_context` is a list that includes a context
   such a grant refuses.
4. Your code: a context that is not a `str`, such as a lazy string
   (`gettext_lazy`) or a number from a `contexts_for` of your own, now names
   no context. Only a grant in every context (`"*"`) reaches it. Return
   `str` contexts.

### Authorization

* The request check of a POST or PUT whose `rbac_context` is a list of
  strings checks each value as the object written there: the body with that
  value as its `rbac_context` (django-scoped-rbac #56). A condition that
  reads `rbac_context` reads one context at a time. In 0.12.0 it read the
  whole list, and a negated condition was true of it: a writer granted in
  every context but `"x"` (`rbac_context != "x"` in `"*"`) could store a
  resource in `"x"` by also naming another context. The workflow and
  post-save checks, the object checks and lists still give a condition the
  whole value (see Known issues).
* A list `rbac_context` holding an array or an object is refused with a 400
  problem (code `invalid`, an `errors` entry for `rbac_context`: "A context
  is a string, not an array or object."), before any grant is evaluated.
* Contexts are read as django-scoped-rbac 0.11.2 reads them
  (`access.context_name`): a `str` subclass by its characters, any other
  value as no context. Lists that a `contexts_for` of your own decides
  (`IsAuthorized` subclasses) read them so too, and agree with the detail
  route again. `rbac_contexts_of` gives exact `str`s, so a `StrEnum`
  member in content written from Python is stored and checked as its
  value.

### Packaging

* Releases are published to PyPI with trusted publishing from GitLab CI;
  no PyPI token is stored in the project.

### Known issues

* Outside scoped-rbac's request check, a grant condition that reads
  `rbac_context` reads the value as written or stored, not the context it
  is evaluated in (scrud-django#2; also in 0.12.0; fixed in 0.13.0). With
  a negated condition (`rbac_context != "x"`):
  * a writer still reaches `""` with an `rbac_context` that is not a string
    (`null`, a number, an object; a list holding a number, bool or null
    needs a grant in `"*"`);
  * a resource stored as a list naming `"x"` can be read, listed,
    overwritten and moved by that grant.

  In 0.12.1, keep such grants out of those contexts by context
  (`RoleAssignment.rbac_context`), not by a condition, or upgrade to 0.13.0.

## 0.12.0

This release moves scrud-django to Django 5.2, uv, scrudful 0.8 and
django-scoped-rbac 0.11. It has several breaking changes.

### Upgrading

Work through these in order: the first group before you deploy 0.12.0, the
second while you deploy it, then your roles, your code and your clients.
Each item points to the section below that has the details.

#### Before you deploy

1. Use Python 3.11 or newer and Django 5.2.
2. Dependencies: install `scrud-django[search]` if you use haystack search
   (django-haystack and Whoosh are no longer installed by default).
   scrud-django also no longer installs django-cors-headers,
   python-decouple, python-dotenv, rdflib or safetydance-test: declare
   those your project uses yourself (a project with `corsheaders` in
   `INSTALLED_APPS` otherwise fails to start). Raise any pins of your own
   below the new floors, or the install does not resolve:
   django-scoped-rbac 0.11.1 (below 0.12), scrudful 0.8 (below 0.9),
   djangorestframework 3.15, django-filter 25.1, django-reversion 5.1,
   jsonpath-ng 1.6 and fastjsonschema 2.19, and with the `search` extra
   django-haystack 3.3 and whoosh-reloaded 2.7.5 (see Requirements and
   packaging below).
3. Migrations of your own that import `django_jsonfield_backport.models.JSONField`
   must import `django.db.models.JSONField` instead, before you uninstall the
   backport.
4. `INSTALLED_APPS`: remove `django_jsonfield_backport`; make sure
   `django.contrib.auth`, `django.contrib.contenttypes`, `rest_framework`,
   `reversion`, `scoped_rbac` and `scrud_django` are listed, and `haystack` if
   you use search.
5. `REST_FRAMEWORK`: set `DEFAULT_PERMISSION_CLASSES` to
   `["scoped_rbac.permissions.IsAuthorized"]` and `EXCEPTION_HANDLER` to
   `scrud_django.exceptions.scrud_exception_handler` (see the README).
6. Search: if you use haystack, set `HAYSTACK_SIGNAL_PROCESSOR =
   "scrud_django.search.OnCommitSignalProcessor"`. Workflow actions
   (`SCRUD_WORKFLOW_ACTIONS`, a list or tuple; any other shape fails at
   startup) now run after the write commits; move one that must run inside
   the write, or veto it, to a `scrud_post_save` receiver (see Side effects
   on commit below).
7. Run your database at READ COMMITTED, the supported transaction isolation
   level (`manage.py check --database <alias>`, for the alias your router
   gives for `Resource` writes, reports another as `scrud_django.W002`). Give
   a file SQLite database that takes concurrent writes
   `"OPTIONS": {"transaction_mode": "IMMEDIATE"}`.
8. MySQL and MariaDB: use mysqlclient, InnoDB and a utf8mb4 database (MariaDB
   stores resource content as text in the database's character set) and
   connection, at READ COMMITTED (step 7). On MariaDB, clients must keep
   request bodies within 31 levels of nesting (see API changes below). A
   MySQL or MariaDB database migrated with a pre-release 0.12 build must be
   recreated: its context columns compare inexactly, and nothing detects it.
9. If you use `DATABASE_ROUTERS`, route `Resource`, `ResourceType`,
   `ResourceContext` and `ResourceRemoval` writes to one alias, and migrate
   them all there: `manage.py check` now reports a split as
   `scrud_django.E004`.

If you will load a dump of the 0.11 database into 0.12 (a backup, a
fixture), read item 15 first: a resource whose context is longer than 2048
bytes in UTF-8 must be given a shorter one in the 0.11 database before you
dump it, or the dump does not load.

#### Deploying

10. Stop every 0.11 process before you run `migrate`, and run none against
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
    run the database check of step 11. A resource stored with a context longer
    than 2048 bytes in UTF-8 gets no row for that context (see Lists below):
    the check reports it as `scrud_django.W003`; give it a shorter context or
    delete it.
11. Workflow policies: your stored `WorkflowPolicy` resources must be at most
    one per type and context, in one context each, and each must pass the
    checks a write now makes (transition names must not start with `__`;
    `restrict_update` must be a list). In this order:
    1. run `manage.py migrate` (step 10). While scrud_django's migrations are
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
    (`manage.py check`, `scrud_django.E001`; give policies as mappings). A
    dotted-path function given there is now called once per process, so one
    whose result changes over time needs stored policies or a restart.
    State conditions now run on scoped-rbac's CEL engine, whose equality
    differs from `celpy`'s in a few cases (see Workflows below).

#### Roles and administration

12. Detail routes and lists now check each resource (see Authorization and
    Lists below). In 0.11 a user with a method on a type in any context could
    read, overwrite or delete every resource of the type. Now a GET needs
    `http.GET` on the type in one of the resource's contexts (a 404
    otherwise); a PUT or DELETE needs its method in one of them (refused, it
    is a 404, or a 403 if the user may read the resource), and a PUT needs
    it, after the save, in every context the resource ends up in (a 403, and
    nothing changes); a `WorkflowPolicy` resource's PUT or DELETE needs it in
    every context the policy is in. A list shows only what the requester may read: give users who
    list a type `http.GET` on the type itself, in each context whose
    resources they should see, as well as on its list type. Check your roles
    if they grant a method in one context and expect it to reach resources
    stored in another, or grant only the list type somewhere.
13. Roles: a grant conditioned on `scrud_workflow` now allows workflow
    transitions only (see Workflows below); it no longer gives `http.GET`
    (detail or list) or `http.DELETE`. Roles that should read need their own
    `http.GET` grant on the type, and roles that should delete their own
    `http.DELETE` grant. Check roles with an action of `"*"` conditioned on
    `scrud_workflow` in particular, and make each `authorized_transitions` a
    list of transition names.
14. Django admin: `Resource` and `ResourceType` are now shown to superusers
    only. Staff who are not superusers no longer see them, whatever model
    permissions they hold (a write there bypasses RBAC, the workflow and JSON
    Schema validation).
15. Backups and fixtures: `dumpdata` / `loaddata` of `scrud_django` round-trip,
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

#### Your code

16. Code: read "API changes" below for what is removed (the modules
    `scrud_django.decorators`, `settings`, `wsgi` and `asgi`,
    `utils.get_string_or_evaluate`, `ScrudServices`' validators,
    `WorkflowValidator.apply` and `permissions.TransitionCheck`), what `GenericViewMixin` subclasses must
    now provide, and the exception handler and error body changes. If you
    mount scrud-django's views without including `scrud_django.urls`, include
    `scoped_rbac.semantic_urls` once.
17. A PUT or DELETE now holds its row lock until its response is ready, on
    scrud's views and on your `GenericViewMixin` views alike (see Conditional
    requests below). Subclasses of `ResourceViewMixin` that override
    `dispatch`, `initial` or `get_queryset`, and `GenericViewMixin` subclasses
    that override `dispatch`, `get_object` or `filter_queryset`, must call
    `super()`; a `GenericViewMixin` view without a `queryset` attribute opens
    its write transaction on the default database unless it overrides
    `locked_write_database()`. A `GenericViewMixin` view locks the row with a
    query of its own (the model's base manager filtered by the URL's lookup;
    a `lookup_field` only the view's queryset has, an annotation, is first
    read as keys through that queryset), not through its queryset, so a
    queryset with `distinct()`, an annotation or a `select_related` of a
    nullable relation keeps working on PostgreSQL (which refuses
    `FOR UPDATE` on those). Server code that updates or deletes a resource
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
18. `GenericViewMixin` lists: their `ETag` and `Last-Modified` now come from
    what the requester may list (see API changes below), so the view needs an
    `RbacFilter` among its `filter_backends` (`manage.py check` reports one
    without as `scrud_django.E006`, and its list is `ImproperlyConfigured`),
    and its `list` should serve `list_page()` (or `list_queryset()` without a
    paginator).
19. Your own `IsAuthorized` subclasses and operators on scrud views (see
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
    have (an `AttributeError`, a 500, failing closed). The request check of a
    POST or PUT gives the permission classes (their `has_permission` and
    `contexts_from`) and your operators the parsed body itself
    (`request.data`), and the workflow check gives their
    `has_context_permission` and your operators the body as sent: a check
    must only read what it gets (`body.get("owner")`, never
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
20. Code that reads a `ResourceType`'s `etag` or `modified_at` to see whether
    its resources changed must read a list's `ETag` and `Last-Modified`
    instead: writes of resources no longer change them (see API changes
    below).
21. Your resource types' JSON Schemas: a `$ref` may only point inside the
    schema (`#/...`). A type whose stored schema has another `$ref` (remote,
    `file:` or relative) refuses every write of its resources with 400 until
    the schema is changed, and JSON Schema `default` values are no longer
    applied (see API changes below).

#### Clients

22. Clients must send a JSON object as the body of a POST or PUT: form and
    multipart bodies get 415, and on the scrud resource views (and your
    views whose serializer uses `GenericSerializerMixin`) a JSON array or
    scalar, which 0.11 stored, gets 400, a validation error (scoped-rbac's
    `about:blank` problem with `code` `invalid`, or
    `{"non_field_errors": ["Expected a JSON object."]}`, or `["No data
    provided"]` for `null`, with uniform errors off). Give your own `GenericViewMixin` views
    `parser_classes = [RbacJSONParser]`.
23. Clients: a PUT or DELETE without `If-Match` or `If-Unmodified-Since`
    gets 428 (it got 400 with `{"missing-required-headers": [...]}`), and
    either header is now enough (both were required). ETags lost their
    version suffix, so every `ETag` a client kept from 0.11 is stale: its
    `If-Match` gets 412 and its `If-None-Match` a 200, so refetch first.
    `services/` sends no `Last-Modified` (revalidate it with
    `If-None-Match`), and OPTIONS bodies use scrudful's Semantic HTTP
    format (see Conditional requests below). An unauthenticated request
    gets 401, and a resource the client may not read is a 404 for every
    method (step 12; see Authorization below). The Role and RoleAssignment
    endpoints serve scoped-rbac 0.11's shapes (see API changes below).
24. Clients: workflow policies now apply in every context, not only `""`
    (see Workflows below). A create whose body is not a valid initial state
    under the policy of each context it names, or an update that is not a
    valid transition, or one the user may not make, in each context the
    resource is in, gets 400 (a ToDo
    with no `status` outside `""`, say); a body with `"rbac_context": ""` is
    checked as one without `rbac_context` (it skipped the default policy). A
    PUT that moves a resource into a context must be an initial state there,
    and the user's `http.PUT` grant there (not `http.POST`) must allow
    `__INITIALIZE__` if it is conditioned on `scrud_workflow`;
    `scrud_post_save` (and so workflow actions) reports such a move as
    `__INITIALIZE__`. A write that only a `scrud_workflow`-conditioned grant
    allows, in a context where no workflow policy applies, gets 403.
    Workflow error bodies changed: a stored policy is given as its URL in
    `policy` and `detail` (it was its content), and a settings default
    policy as `{"resource_type": ..., "default": true}`; `prior_state` and
    `proposed_state` are no longer swapped, and a create's `prior_state` is
    null; and the error is generic (`policy`, `prior_state` and
    `proposed_state` null) when the writer may not read the policy, or the
    stored resource. A refused `WorkflowPolicy` write's 400 is a
    `workflow-policy-invalid` problem whose `detail` says why.
25. Clients: on the scrud resource views, a body whose `rbac_context` is
    `[]` now gets 400 (it got 403), and lists of
    `ResourceViewSetGenericFilter` filter on their query parameters as JSON
    key lookups (see Lists below).
26. Clients: JSON Schema `default` values are not applied; send every value
    you want stored (and that a workflow state condition reads); a default
    no longer makes a body valid either. On the scrud resource views, a POST
    or PUT with an empty body, or with no `Content-Length` (a chunked body),
    gets 400 (`body-missing`), and one nested more than 100 levels deep
    (objects and arrays) gets 400 (`body-too-deep`), as does one with a NUL
    character (`\u0000`) in any string (`body-nul-character`). A DELETE of
    a resource a type uses as its schema, context or indexing policy gets
    409 (`resource-in-use`; it was a 500).
27. Clients: with `SCOPED_RBAC["UNIFORM_ERRORS"]` on (scoped-rbac's
    default), a 400 (malformed `If-Match` or `If-None-Match`), 412 or 428 is
    now problem details served as `application/problem+json` (it was
    `{"detail": ...}` as `application/json`; with uniform errors off it still
    is), and so is every other error that is not scrud-django's own, on
    every view `scrud_exception_handler` handles (step 5): for example a
    validation error's field messages become "This value is not valid." (see
    API changes below). A list envelope's `link` now spells the schema link
    `rel="describedby"` with `type="application/schema+json"`, as the `Link`
    header does (see Conditional requests below).
28. Clients: a POST to a list now evaluates `If-Match`,
    `If-Unmodified-Since` and `If-None-Match` against the list (see
    Conditional requests below). Drop `If-None-Match: *` from creates (it
    now always fails with 412, since the list exists), and send `If-Match`
    only with the list's current `ETag`. Schema and JSON-LD documents are
    now `application/schema+json` and `application/ld+json`; a client that
    requires `application/json` must send a plain `Accept: application/json`
    (or `?format=json`). DRF's content negotiation ignores q-values, so an
    `Accept` that also lists the `+json` type, at any weight, may get it.

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
  the `search` extra (Whoosh as whoosh-reloaded). If `haystack` is in your
  `INSTALLED_APPS`, install `scrud-django[search]`.
* scrud-django no longer installs django-cors-headers, python-decouple,
  python-dotenv, rdflib or safetydance-test (0.11 did); declare
  them yourself if you use them.

### Conditional requests

Conditional request handling now comes from scrudful 0.8.

* A PUT or DELETE with no precondition returns 428 instead of 400. Either
  `If-Match` or `If-Unmodified-Since` is enough.
* ETags no longer carry a version suffix.
* A POST to a list evaluates its preconditions, as RFC 9110 requires,
  against the list's `ETag` and `Last-Modified` as the requester would GET
  them from that URL: `If-Match` (or else `If-Unmodified-Since`) that fails,
  and `If-None-Match: *` or the list's own `ETag`, are a 412, and a
  malformed `If-Match` or `If-None-Match` a 400, as on the detail routes,
  and nothing is created. They were ignored (scrudful evaluates none for a
  create;
  `scrudful-create-preconditions`, closed in 0.14.0).
  A POST without preconditions is unchanged. Computing the list's
  validators is a GET of the list: past
  `SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]` such a POST is the same 400
  `conditional_list_too_large`, except with only `If-Match: *` (it creates,
  as an unconditional POST does) or `If-None-Match: *` (a 412), which need
  no validators. A POST whose preconditions need the validators evaluates
  them and creates in one transaction that first locks the type's
  `ResourceType` row (`FOR NO KEY UPDATE` on PostgreSQL, `FOR UPDATE` on
  MySQL and MariaDB; `scrud_django.registration.type_locked`), so two such
  POSTs with the same `If-Match` give one 201 and one 412. Other writes do
  not take that lock first (a MySQL or MariaDB write that inserts a row
  referring to the type may wait for it). A `create` of your own on
  `GenericViewMixin` should call `list_precondition_response(request)`
  first, and, to hold the list the same way, run it with `using=` and the
  create in such a transaction.
* Schema and JSON-LD documents are served as the media type their `Link`
  gives: a list's collection schema (`collections-json-schema/<slug>/`) and a
  JSON Schema resource (`json-schema/<id>/`) as `application/schema+json`, a
  list's collection context (`collections-json-ld/<slug>/`) and a JSON-LD
  context resource (`json-ld/<id>/`) as `application/ld+json` (on a GET,
  HEAD, or PUT of the resource), with `Vary: Accept`. They were
  `application/json`. A client that sends `Accept: application/json` (or
  `?format=json`) still gets the same body as `application/json`
  (`?format=schema-json` and `?format=ld-json` name the typed ones); lists,
  OPTIONS and errors keep their types. The collection context's content is unchanged
  ([collection-context-terms](follow-ups.md#collection-context-terms)).
* The service list's (`services/`) `ETag` is a hash of the catalog it
  serves, the same in every process and worker that registered the same
  services; it changes when the catalog does. It sends no `Last-Modified`.
  Both were the time the process last registered a service, so they changed
  at every process start and differed between workers (and `Last-Modified`
  was that time's local wall-clock time read in `TIME_ZONE`, hours off).
  `scrud_django.ScrudServices` loses `etag`, `last_modified`,
  `update_last_modified()`, `get_etag()` and `get_last_modified()`; use
  `etag_for(request)` and `catalog(request)`.
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
  `{"detail": ...}` as `application/json`. (The other errors that are not
  scrud-django's own, through `scrud_exception_handler`, are scoped-rbac's,
  with `about:blank` and a `code`, as these are.) With uniform errors off,
  they and those other errors keep their native bodies (`{"detail": ...}`,
  or field errors such as `{"non_field_errors": [...]}` for a body that is
  not a JSON object) as `application/json`, without the problem-details
  `Link`s, as scoped-rbac's own viewsets have it (the setting is read per
  request). scrud-django's own errors (the `body-missing`, `body-too-deep`,
  `body-nul-character`, `rbac-context-invalid`, `json-invalid`,
  `json-schema-invalid`, `resource-in-use` and workflow problems) are
  problem details whatever the setting, served as `application/problem+json`
  under the JSON renderer, with
  `https://api.openteams.com/http-problem/...` types
  (`http://api.openteams.com/http-problem/workflow-transition-invalid` for
  workflow transitions), no `code` and no problem-details `Link`s, as in
  0.11.
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
  rolled back). On PostgreSQL, the second of two concurrent writes of one
  resource fails with a serialization error, a 500, instead of a 412 or 400,
  at REPEATABLE READ and SERIALIZABLE. At REPEATABLE READ, two POSTs to a
  list with the same `If-Match` can both create, and two `WorkflowPolicy`
  creates for one type and context can both be stored, with no error; at
  SERIALIZABLE the second of each fails with a serialization error (a 500)
  instead of a 412 or 400. On MySQL or MariaDB at REPEATABLE READ, the
  `WorkflowPolicy` uniqueness re-check can miss a concurrent duplicate
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
* Unauthenticated requests now get 401, through `scrud_exception_handler`
  (DRF's own handler answers 403 when the first authentication class, such
  as `SessionAuthentication`, sends no `WWW-Authenticate`).
* On the scrud resource views, a POST or PUT body whose `rbac_context` is
  an empty list gets 400
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
* `scrud_django.ScrudServices` (`scrud_django.services`) loses `etag`,
  `last_modified`, `update_last_modified()`, `get_etag()` and
  `get_last_modified()`; use `etag_for(request)` and `catalog(request)` (see
  the `services/` bullet under Conditional requests).
* `scrud_django.decorators` is removed. Use `scrudful.rest_framework` instead.
* `scrud_django.settings`, `scrud_django.wsgi` and `scrud_django.asgi` are
  removed: the package no longer ships a Django project. Use your own project's
  settings, WSGI and ASGI modules.
* `scrud_django.utils.get_string_or_evaluate` is removed.
* `ResourceViewSet` and `ResourceViewSetGenericFilter` set
  `permission_classes = [IsAuthorized]`. A subclass that relied on a different
  `DEFAULT_PERMISSION_CLASSES` must set `permission_classes` itself.
* `scrud_django.urls` now also mounts scoped-rbac's semantic URLs at
  `semantic/`, which serve the schemas and JSON-LD contexts of scoped-rbac's
  own resources (Role, RoleAssignment and Policy), which the role endpoints'
  `Link` headers point to. A scrud resource type's schema and context are its
  `json-schema/<id>/` and `json-ld/<id>/` resources and its
  `collections-json-schema/<slug>/` and `collections-json-ld/<slug>/` views,
  as before. If you mount URLs yourself, include `scoped_rbac.semantic_urls`
  once.
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
* On the scrud resource views (`ResourceViewMixin`; your
  `GenericViewMixin` views do not make these body checks), a POST or PUT
  whose body is empty, or has no `Content-Length` (a chunked body, which DRF
  does not read), gets 400
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
* On the scrud resource views, a POST or PUT body with a NUL character
  (`\u0000`) in any string, a key or a value at any depth, gets 400
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
  before that `select_for_update()` (Upgrading 17). If several
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
  Upgrading step 11). The `WorkflowPolicy` schema now also declares
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
A POST to scoped-rbac's own Role and RoleAssignment lists ignores its
preconditions
(`scrudful-create-preconditions`, closed in 0.14.0).
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
([eager-callable-default-policies](follow-ups.md#eager-callable-default-policies)),
the JSON Schema compiler's message in a 400
([schema-error-echoes-compiler-text](follow-ups.md#schema-error-echoes-compiler-text))
and the collection JSON-LD context's terms
([collection-context-terms](follow-ups.md#collection-context-terms)).
