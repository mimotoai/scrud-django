# Follow-ups

Open follow-ups come first; the entries under `# Decided` are limitations
kept by a decision, each saying what was decided and why. Record a new known
issue or deferred bug here, above `# Decided`, as a section with an anchor
(`<a id="short-slug"></a>` then `## short-slug: title`).

<a id="studio-push-list-delete"></a>
## studio-push-list-delete: an HTML list of pushes shows an applied push with a Delete button

An HTML list of `ui-pushes/` (`render_collection` in `render.py`) renders an
applied push with the edit form and a Delete button (the API's 409 protects
it); the `is_record` hook is applied only to a detail frame. Test: in
`tests/test_studio_promote.py`, GET `ui-pushes/` as HTML for a user who may
DELETE pushes, with an applied push stored; the item should have no
`hx-delete`, and has one today.

<a id="studio-branch-tools-squeeze"></a>
## studio-branch-tools-squeeze: opening the branch tools pushes the strip off the first screen

Opening Branch settings or New branch (above the panes) squeezes the panes to
their 14rem floor, and the page scrolls so the strip leaves the first screen.
At viewport heights up to about 700px the strip is one scroll away anyway, and
the 3.5rem chrome reserve in `.studio .studio-workbench` is a constant (wrapped
chrome links at narrow widths add overflow, and so do the branch banner the
page shows when its branch is not the published one and the safe-mode notice;
sizing the workbench from the space left, say with the page body as a flex
column, would fix all three). Test: a browser layout check at
1440x900 with Branch settings open; the document height should not exceed the
viewport.

<a id="studio-push-review-bookkeeping"></a>
## studio-push-review-bookkeeping: the review marks bookkeeping fields as changes

A create over an inherited row sets the target's `branch`, and its `base` and
`rbac_context` where they differ from the inherited row's (main's `""` context
against the target's own, say), and the push review marks them as changed.
They are real differences in the stored row but bookkeeping the author did not
change; consider de-emphasizing them. Test:
render the review of `alice-wip`'s `todo` override into `testing-admin` and
assert those fields are styled as bookkeeping, not as changes.

<a id="studio-promote-nested-scroll"></a>
## studio-promote-nested-scroll: a long review scrolls in two nested containers

The promote panel has its own bounded scroll and sits inside the strip, which
is bounded at 40vh with its own scroll, so a long review scrolls in two nested
containers. Test: a browser check that scrolling the review never scrolls
Apply out of view.

<a id="studio-client-picker-hrefs"></a>
## studio-client-picker-hrefs: the client tests build the picker links by hand

The node client tests' branch picker links are built in the harness, not taken
from the server-rendered picker (`tests/test_studio_workbench.py` pins the
server's shape separately), so the two can drift apart. Test: build the client
fixture from `studio:branch-picker` rendered by the server.

<a id="studio-push-page-embeds-twice"></a>
## studio-push-page-embeds-twice: a push's page embeds the push preview twice

A push's page (`studio:push`, `studio:push-view`) embeds
`ui-push-previews/<source>/?target=<target>` twice: the review (scope
`studio studio-push`) and "Preview after push" (scope `studio
studio-push-after`, `studio:push-after`). Each embed computes the push's
plan, so the second runs even when its `endpoints` are `null` and it
renders nothing. A viewer who may open the push but not `GET` its source or
target gets the push preview's 404 from both embeds: the same refusal shown
twice. Test: in `tests/test_studio_promote.py`, give a user `GET` on
`ui-pushes` (and `PUT`, for edit mode) but not on the source branch, render
the push's page in the scope `studio`, and count the 404 renderings of the
push preview; it should be one, and is two today.

<a id="studio-rule-editor-duplicate-prefix"></a>
## studio-rule-editor-duplicate-prefix: a new prefix with a taken key overrides the stored one silently

In the rule editor (`studio:stylesheet-editor`, `studio:rules`), the new
prefix row's IRI input is named `/prefixes/<key>` from the key the author
types. A key the stylesheet already has gives two inputs with that name;
`ScrudUI.buildBody` applies them in order, so the new row's IRI replaces the
stored one on Save, with no word. Test: in
`tests/test_studio_layer_client.py`, with the rules harness, type a key the
stylesheet already has (with another IRI) in the new prefix row and build
the frame's body; it should be refused as an invalid field (or the new row
named apart and reported) and today carries the new IRI under the old key.

<a id="studio-may-send-workflow-path"></a>
## studio-may-send-workflow-path: `may_send`'s workflow-only ("possibly") path is untested

`scrud_django.ui.authz.may_send` offers a control when the policy allows the
write outright or only inside a write's checks (a `scrud_workflow`
condition, which allows there, as `decisions_for` counts a "possibly" PUT).
Only the outright path is exercised by the studio's tests. Test: in
`tests/test_studio_computed.py`, grant a user `http.POST` on `ui-components`
only through a `scrud_workflow` condition in a working branch's context;
`ui-branch-items/<branch>/` should offer that user's `override` (and
`ui-branch-status` the promote `apply` when the same holds for `ui-pushes`),
and a user with no such condition none.

<a id="studio-reverts-to-pinned"></a>
## studio-reverts-to-pinned: `reverts_to` on a pinned view is untested

`ui-branch-items/<branch>@pinned/` lists the branch's own items with
`reverts_to` from `_beneath`, which reads the parent's effective items as
the branch shows them (`parent_state`), not as of the branching point the
pinned view shows. No test pins what a pinned view's own items say they
revert to. Test: in `tests/test_studio_computed.py`, give a working branch
an own item over one its parent changed after the branching point, and one
over an item its parent added after it; read the pinned items and assert
each own item's `reverts_to` against what deleting the row would show on
the branch (and decide whether a pinned view should offer it at all).

<a id="studio-preview-nested-embeds"></a>
## studio-preview-nested-embeds: nested embeds in a view-mode preview are untested

A preview requested in view mode renders its embeds in view mode
(`embed-preview-mode`, fixed in this release), and the studio's previews
embed editable resources. The tests cover one level: an embed inside an
embedded resource (depth 2 or 3) in a view-mode `preview` or `live-preview`
is not exercised, so a nested embed that went back to the user's own mode
(an edit form, array operations) would not show. Test: in
`tests/test_studio_live_preview.py`, preview a resource whose component
embeds a resource whose component embeds another the user may `PUT`, in
view mode; the inner frame should be a view (no `hx-put`, no
`data-scrud-array-op`).

<a id="studio-items-pane-cost"></a>
## studio-items-pane-cost: the items pane's cost grows with the branch's items

`ui-branch-items/<branch>/` (the workbench's Items pane, fetched again after
every write) evaluates `may_post` once per item it does not own (the
`override` action: a policy evaluation each) and reads `_beneath` (the
parent's effective items, `parent_state`) once per kind that has rows. Both
are linear in the items per render, which is fine at the sizes the studio
targets and not measured beyond them. Test (a budget, not a bug): in
`tests/test_studio_computed.py`, with `django_assert_max_num_queries`, read
the items of a branch with 10 and with 100 inherited components; the query
count should not grow with the item count (the policy evaluations are in
memory).

<a id="ui-inflight-test-real-htmx"></a>
## ui-inflight-test-real-htmx: the in-flight refusal is tested with htmx's events stubbed

`scrud-ui.js` refuses a frame's request while its previous one is in flight
(`htmx:confirm`), says nothing when the refused request would send the body
already in flight, and settles the frame on `htmx:afterRequest` or
`htmx:onLoadError`. The node tests (`tests/test_ui_client.py`) call those
handlers with hand-built events in the order htmx 2.0.4 fires them, since
the suite has no DOM for the vendored `htmx.min.js` to run in (no jsdom). A
change in htmx's order (confirm before or after `beforeSend` for the same
request, which events a throwing swap fires) would not show. Test: drive the
vendored htmx in a real DOM (jsdom, or the browser acceptance run) on a frame
with one text field; type, press Enter (change, then submit), and assert one
PUT and no "not sent" notice; then type, click Move up during the save, and
assert the notice.

<a id="embed-literal-path-save-check"></a>
## embed-literal-path-save-check: a literal non-resource embed path is refused only at render

The spec (R9) asked that an embed whose path is a literal that is not a
resource URL be refused when the component is saved. Only the render-time
refusal is implemented, so such a component saves and then renders the
refusal notice. Test: in `tests/test_ui_embeds.py`, PUT a component whose
template embeds a literal non-resource path; it should be a 400 and is a 200
today.

<a id="push-preview-write-grants"></a>
## push-preview-write-grants: the push preview does not check write grants

`ui-push-previews` lists what a push would write but does not check the user's
per-item write grants, nor the PUT on the source branch that applying the
push needs, so it cannot say whether the user may apply the push (the push
itself still checks them). Test: in `tests/test_studio_push_preview.py`,
preview a push as a user with GET on both branches but no write on one changed
item type; the response should flag that item as not applicable and has no such
field today.

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
one: the slug then has two rows. `get_resource_type_for` takes the highest
revision and, of rows of the same revision, the first stored (lowest primary
key), so every process now reads and stores resources under the same one,
and registration updates that one (before #18 each took whichever the
database returned first, so resources could be stored under either, and a
start could update a row no process read), and the other is left unused. Since 0.14.0
the lookup reads the write database, not a replica that may lag behind,
which narrowed the window from the replica's lag to the time between the
lookup and the insert. A registration that fails with a database error when
the URLconf is imported is retried when the type is next read
(`register_or_defer`, `retry_registration`), without holding the process's
lock (which guards the pending list only: held while the registration
waited for row locks in the database, it could deadlock with the thread
holding them), and stays pending until the reader's transaction commits. So
two threads of one process reading a pending type at once, or one reading it
while another's retry is not committed yet, can each register it, and for a
new type each insert a row, as two processes would. `json_resource_type`
and the built-in types now register in a transaction on `type_db()`
(`register_or_defer`, and `retry_registration`'s), without a lock. Fix: in
that transaction (and around a `ResourceTypeRegistration` made directly),
first take a lock (scoped-rbac's `conditional.lock_collection` of
`ResourceType`, which needs that transaction, or a row of its own) and look
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
resources are indexed without one. `json_resource_type_spec` (the ninja
front end's `TypeSpec`) does not take the argument, so it cannot drop it
(passing it is a `TypeError`); fixing this adds it there as a keyword-only
argument. Fix: pass it through.
Test: register a type with `json_resource_type(..., indexing_policy_resource=<a
stored policy>)` and assert the stored type's `indexing_policy` is that
resource; it is `None` today.

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

<a id="ui-name-uniqueness-race"></a>
## ui-name-uniqueness-race: concurrent creates can duplicate a UI component or stylesheet name

UI component names are unique site-wide and stylesheet names unique per
context, but `UIContentValidationMixin` checks that with a query, without a
lock or a database constraint, so two concurrent creates of the same name can
both succeed. Resolution is deterministic: the lowest pk wins
(`scrud_django.ui.cascade`), so a duplicate is shadowed, not an error. Suggested
fix: serialize the check as `serialized_policy_save` does for policies, or add a
unique index on a generated name column. Test: two threads creating the same
name; at most one is accepted.

<a id="ui-one-mode-per-resource"></a>
## ui-one-mode-per-resource: a resource renders wholly in view or edit mode

Editors get edit mode for the whole resource, so a view-only component (a
compact sidebar list, say) is still served in edit mode and must be written as an
`edit` component (the demo's `todo-list-compact` is). Why: the mode is decided
once from the PUT decision. A fix (with phase 2's per-field editability, see
[Server-rendered UI](ui.md)) could let a rule request a mode.
Test: a rule that asks for `view` in a scope renders its view component for an
editor.

<a id="ui-412-notice-persists"></a>
## ui-412-notice-persists: the 412 notice stays after the next successful edit

A 412 answers with a notice and a frame that reloads itself; the notice is
outside the reloaded frame, so it stays on the page after the next successful
edit. Suggested fix: give the notice an element that `scrud-ui.js` removes on the
next successful swap. Test: a stale edit, then a good one; the notice is gone.

<a id="ui-sidebar-staleness"></a>
## ui-sidebar-staleness: other frames do not refresh after an edit

An edit swaps only its own frame, so another frame showing the same resource
(the demo sidebar's item count) stays as it was until reloaded. Suggested fix:
have the write's response trigger a refresh of frames with the same
`data-scrud-href` (`HX-Trigger`). Test: edit an item; the sidebar count changes
without a reload.

<a id="ui-absolute-hrefs-host"></a>
## ui-absolute-hrefs-host: collection-envelope hrefs are absolute, so a host alias cannot write to them

A list's envelope gives each resource an absolute `href`, which the collection
frame uses as `hx-put`. With `selfRequestsOnly`, a page served at a different
host alias than the one in the href (`localhost` against `127.0.0.1`) is not
allowed to write to them. Suggested fix: render the path of the href (the
renderer already does this for a created resource). Test: a list fetched at one
host whose envelope names another; the edit form's `hx-put` is a path.

<a id="ui-uri-relative-whitespace"></a>
## ui-uri-relative-whitespace: `scrud:uri` treats `/\t/evil.example` as a relative link

`scrud:uri` links a value that starts with one `/` (and not `//` or `/\`). A
tab or LF after the slash (`/\t/evil.example`, `/\n/evil.example`) passes that
test, but browsers strip tab and LF from URLs, which makes it protocol-relative:
an off-site link. It is not script execution (the scheme is still http(s)).
Suggested fix: refuse a value with ASCII control characters or whitespace in the
first three characters. Test: those values render as text.

<a id="ui-json-vary-accept"></a>
## ui-json-vary-accept: JSON from UI-enabled resource views does not send `Vary: Accept`

The same URL now has an HTML representation, but its JSON responses do not
vary on `Accept`, so a cache could serve a stored JSON response to an HTML
request. HTML responses are `private, no-cache, no-store`, so an HTML response
is never stored and served for JSON; but that does nothing for the JSON
responses, which a cache may still store and then serve to an HTML request. Suggested fix: add `Accept` to
`Vary` on the JSON responses of the views when the UI app is installed. Test:
a JSON GET carries `Vary: Accept`.

<a id="ui-html-shared-etag"></a>
## ui-html-shared-etag: HTML responses reuse the resource's strong ETag and Last-Modified

The HTML depends on the stylesheets, components and the viewer's grants as well
as the resource, but carries the resource's ETag and `Last-Modified`, so a
conditional GET can answer 304 for HTML that would now differ. `Cache-Control:
private, no-cache, no-store` keeps browser and proxy caches from storing the
HTML, so they hold no stale copy to revalidate. It does not change the
validators: a client that keeps an ETag itself and resends it (`If-None-Match`)
after a component, stylesheet or grant changed still gets a 304, and keeps
showing the old HTML. Suggested fix: a weak ETag for HTML derived from the
resource's and the presentation's (and the viewer's decisions). Test: change a
component; a conditional GET with the old validator gets a 200.

<a id="ui-number-precision"></a>
## ui-number-precision: a browser edit rewrites numbers JSON.parse cannot keep

`scrud-ui.js` builds a PUT's body from the content embedded in the frame,
parsed with `JSON.parse`. An integer beyond 2^53 is rounded
(`9007199254740993` becomes `9007199254740992`) and `1.0` becomes `1`, and the
body carries those values for every number in the resource, not only the
edited ones, so any edit (a checkbox) writes them back. Untouched inputs are
skipped, but the embedded content itself is already rounded. Suggested fix:
send only the changed pointers (a JSON Patch or merge on the server), or parse
and serialize the embedded content without going through JavaScript numbers.
Test: a resource with `9007199254740993` and `1.0`; tick a checkbox in the
browser; the stored content keeps both as they were.

<a id="ui-inflight-edit-dropped"></a>
## ui-inflight-edit-dropped: an edit made while a PUT is in flight is lost

A frame PUTs on every `change`. A change (or an array operation, or a save)
made while the frame's previous PUT is still in flight is not sent: htmx would
queue it and drop it when the first response swaps the frame, so `scrud-ui.js`
refuses it and the swapped-in frame says "Your change was not sent ... Make the
change again." It is no longer silent, but the change is still lost and must
be made again. Suggested fix: send it after the first response, built from the
swapped-in frame (its content and ETag) with the user's input re-applied, or
disable the frame's inputs while a request is in flight. Test: in a browser,
tick two checkboxes in quick succession against a slow server; both are saved
(today the second gets the notice).

<a id="ui-typeterm-invariants"></a>
## ui-typeterm-invariants: UI types state their invariants in docstrings, not code

`TypeTerm` (`scrud_django/ui/selectors.py`) documents that exactly one of `iri`
and `schema_type` is set, and that `format` and `enum` go only with a
`schema_type`, but nothing enforces it: `TypeTerm(iri="x", schema_type="string")`
builds, and `_term_matches` then reads it as an IRI term. The engine names and
the stored modes are spelled three times (`engines.ENGINE_NAMES`,
`cascade.STORED_MODES`, the `enum`s in `UIComponent-schema.json`), so one can
change without the others. The UI JSON Schemas have no top-level
`additionalProperties: false` (a misspelt `stylesheet` or `scope` key is stored
and silently ignored), and the scope names in a component's `scopes` have no
`pattern`, so a scope the selector grammar cannot name
(`[A-Za-z][A-Za-z0-9_-]*`) is accepted and never matches. Why: the checks
were kept to what rendering needs. Suggested fix: validate in
`TypeTerm.__post_init__` (raise `ValueError`); make the engine and mode names
enums in one module and check the schema documents against them in a test;
add `additionalProperties: false` to both schemas (after checking stored
content for extra keys) and a scope-name `pattern` to `scopes`' values. Test:
`TypeTerm(iri="x", schema_type="string")` and `TypeTerm(iri="x", enum=True)`
raise; a component with `"scopes": {"items": "1bad"}` or an unknown top-level
key is a 400; the schema's `engine` and `mode` enums equal the module's.

<a id="ui-selector-iri-suffix"></a>
## ui-selector-iri-suffix: `ex:Foo[enum]` and `string[format=a:b]` parse as plain IRIs

`parse_selector` takes any last token containing `:` for an IRI before it
looks for a `[format=...]` or `[enum]` suffix. So `ex:Foo[enum]` is the IRI
`http://example.org/todo#Foo[enum]` and `string[format=a:b]` the IRI
`string[format=a:b]`: both are saved without a 400 and never match a node, and
the author is not told why their rule does nothing. Why: the IRI branch was
checked first, and IRIs take no suffix. Suggested fix: check for a bracket
suffix first, and refuse it on an IRI (`SelectorError`: a suffix needs a schema
type), and let a format name contain `:` only if that is intended (else refuse
it). Test: `parse_selector("ex:Foo[enum]", ...)` and
`parse_selector("string[format=a:b]", ...)` raise `SelectorError` (or the
second parses as `string` with format `a:b`), and a stylesheet with either is
a 400.

<a id="ui-number-input-partial"></a>
## ui-number-input-partial: a number input cannot be cleared

A cleared or half-typed number input (the browser reports `1e` as `""`) over a
stored number no longer deletes it: `buildBody` reports it, the PUT is cancelled
and the frame shows "This form could not be saved: invalid field <name>."
What remains:

- The user cannot clear an optional number on purpose, nor write `null` where
  the schema allows it. While the field is empty every edit in that form is
  cancelled with the same notice, because each `change` sends the whole form.
- A half-typed number in the append row is dropped silently (`isEmpty`).
- `buildBody` in the browser receives the real inputs, so
  `field.validity.badInput` is readable there and tells a half-typed number
  from a cleared one (the test stubs lack it). Suggested fix: keep
  `badInput` as an error; for an empty input that is not `badInput`, delete the
  key (the server answers a required one with a 400 and a re-rendered form),
  or write `null` where the schema allows it (embed that as a data attribute
  on the input). Test: a list item with `"count": 3`; clear the field and
  blur; the stored `count` is gone (or `null`), and no later edit is cancelled.

<a id="ui-branches-row-scan"></a>
## ui-branches-row-scan: finding a branch chain's rows scans every branch's

A render reads the stored rows of all UI item kinds in one query, filtered in
SQL to the branches of the rendered chain (`cascade.on_branches`: `branch`
equal to one of them, and for `main` also no `branch` key), so it transfers
and resolves only the chain's rows. But `content` has no index on its `branch`
key, and `main`'s condition (`NOT has_key`) cannot use one, so the database
still evaluates the condition on every row of the item types: the cost of the
query grows with the items on all branches, though far more slowly than when
they were all read.

Fix: an expression index on `content->'branch'` (per database: PostgreSQL and
MySQL 8 support functional indexes, SQLite expression indexes; MariaDB needs a
generated column), or a real `branch` column on resources of the item types,
filled on save. Test: with 1,000 branches of a few items each, `EXPLAIN` the
item query of a render on one of them and assert it uses the index; it does
not today.

<a id="ui-banner-hideable"></a>
## ui-banner-hideable: branch CSS and page components can hide the banner

The branch banner is rendered by the server outside stored components, but it
is part of the page's markup. A CSS item (`.scrud-branch-banner {display:
none}`) can hide it, and so can a custom page component that wraps its `body`
in a hidden element, a `<template>` or an HTML comment (the page is checked for
the banner's presence, not for its visibility). So "no branch can hide it" is
not true: a branch's writers can show its viewers a page that does not say
which branch it is. This is part of the trust in a branch's writers (see
Security in `ui.md`), and the confirmation page, which is a separate page, still
asks before a switch.

Fix: serve the banner in a way CSS from branches cannot reach (a shadow root
or an `!important` rule in an inline `<style>` after the branch CSS), and check
the rendered page for the banner outside `<template>`, comments and hidden
parents. Test: in `tests/test_ui_branch_browse.py`, a branch with a CSS item
hiding `.scrud-branch-banner`, and another whose page component wraps
`{{ body }}` in `<template>`; assert the banner is visible in the rendered
page (a check on the rendered DOM); both fail today.

<a id="ui-prefix-query"></a>
## ui-prefix-query: one query per request outside the API, uncached

`PublishedBranchMiddleware` reads the stored branches (one query) on every
request outside the API's URLs, to learn whether the path is under a prefix.
A mounted request then reads the branch again for some paths (JSON pinned
selections, `choose` outside the render memo). The prefixes change rarely.

Fix: cache the prefix table (name, prefix, context) with invalidation on a
write of a `ui-branches` resource (a signal, and a short timeout for other
processes). Test: a request to a non-API URL runs no query once the table is
cached, and a changed prefix is routed on the next request in every process;
it fails today because every request queries.

<a id="ui-branch-lock-order"></a>
## ui-branch-lock-order: pushes, branch deletes and item writes can lock in different orders

A push locks its source's and target's branch rows and then the item rows it
writes; an item write locks the item row and then records the branch's version
(locking the branch row); a branch delete locks the branch row and then deletes
its items. Two of these running together can take the same locks in opposite
orders, and the database aborts one of them (a deadlock, 500 on PostgreSQL and
MySQL; SQLite serializes writes). Nothing is corrupted: the aborted request
rolls back whole.

Fix: lock the branch rows first, in one order (sorted by name), in every path.
Test: on PostgreSQL, run a push and an item write to its target from two
threads against a barrier, many times, and assert neither request is a 500; it
fails intermittently today.

<a id="ui-version-read-committed"></a>
## ui-version-read-committed: version numbers need READ COMMITTED on MySQL and MariaDB

A branch's next version number is the highest recorded plus one, read under a
lock on the branch's row. That read sees the latest committed version at READ
COMMITTED, which PostgreSQL uses by default, and on SQLite. Django's MySQL
backend (MySQL and MariaDB) also sets READ COMMITTED on its connections, unless
the project sets another `isolation_level` in the database's `OPTIONS`. A
project that sets `"repeatable read"` (the servers' own default) gets a plain
`SELECT` that may read a snapshot taken earlier in the transaction and miss a
version another transaction committed after it, so two versions can get the
same number (the second `UPDATE` of the branch's row is blocked, but the read
is not).

Fix: read the latest number with `select_for_update()` (a locking read sees
the latest committed rows at any isolation level). Test: on MySQL and MariaDB
with `OPTIONS = {"isolation_level": "repeatable read"}`, two transactions each
write an item to the same branch with a barrier between the branch lock and
the version read, and assert the version numbers are distinct; they can be
equal today.

<a id="ui-bulk-delete-versions"></a>
## ui-bulk-delete-versions: a bulk delete records one version per item

`QuerySet.delete()` of many items (an admin action, `discard_branch` aside)
sends one `post_delete` per item, and each records a branch version, so
deleting N items of `main` records N versions and reads the items N times
(quadratic work). The versions are all valid, but history is noisy.

Fix: wrap bulk deletes in `batched_versions()` (one version per branch).
Test: delete 50 items of `main` with `Resource.objects.filter(...).delete()`
and assert one version and a bounded number of queries; it records 50 today.

<a id="ui-discard-cross-context"></a>
## ui-discard-cross-context: deleting a published branch deletes stylesheets in other contexts without a grant there

On a published branch a stylesheet that applies to a context `X` is authorized
in `X`, so writing or deleting it directly needs a grant in `X`. Deleting the
branch (`discard_branch`) deletes its own items, those stylesheets included,
needing only DELETE on the branch in its context. A user who may delete the
branch can so remove a stylesheet they could not delete one by one.

Fix: check, for each such item, the grant a direct delete would need
(`may_act_in_context`), as a push does, before deleting the branch, and refuse
(403) otherwise. Test: a user with DELETE on branch `p` (in context `""`) and
none in `acme`; a stylesheet on `p` that applies to `acme`; assert the branch
DELETE is refused and the stylesheet remains; it is deleted today.

<a id="ui-conflict-key-mismatch"></a>
## ui-conflict-key-mismatch: pull and push key a stylesheet differently when it has no `applies_to`

`conflicts()` (the pull's conflict list) keys each source row on the target's
chain (`item_key(kind, content, chain[0])`), while `apply_push` keys it on the
source branch. A stylesheet stored without `applies_to` (a raw save or
`ResourceRegistration`, which skip validation) gets its `applies_to` from the
branch it is read on: on a published branch from its `rbac_context`, on a
working branch `""`. So a pull may list a conflict that the push does not find,
or the reverse.

Fix: key the source rows on the source branch in both, or store `applies_to`
on every stylesheet. Test: store a stylesheet override with no `applies_to`
and an `rbac_context` other than its working branch's, on a working branch
over a published one whose parent has changed that stylesheet; assert that the
conflict list of a pull and the 409 of a push name the same items. They
differ today.

<a id="ui-confirm-token-in-urls"></a>
## ui-confirm-token-in-urls: the confirmation token travels in URLs

A confirmed branch link carries `ui-branch-confirm=<token>` in its query
string, so it can appear in browser history, server logs and a same-origin
`Referer`. The token is bound to the selection and the viewer's CSRF secret,
not to the prefix, and lasts as long as the CSRF secret. It cannot be used by
another viewer, but it can be replayed by the same viewer's browser, which is
the point; a stolen log line is not enough without the viewer's cookie.

Fix: make the token short-lived and bound to the prefix, or confirm with a
POST. Test: a token for one prefix is refused at another, and an old one is
refused after its lifetime; both are accepted today.

<a id="ui-version-304-after-grant-change"></a>
## ui-version-304-after-grant-change: a filtered version can answer 304 after a grant change

A version is read per requester (items the reader may not read are left out)
and its ETag follows what the reader gets. A client that revalidates with
`If-Modified-Since` only, after its grants changed, can get a 304 for a
representation that would now differ.

Fix: derive `Last-Modified` for a filtered version from the grants too, or
omit it when items were filtered. Test: read a version as a user who lacks one
item's read grant, grant it, then GET with `If-Modified-Since` of the earlier
response and assert 200; it is 304 today.

<a id="mysql-sort-buffer"></a>
## mysql-sort-buffer: reads outside the UI and the paginated list can still sort large content on MySQL

MySQL sorts a result with every column it selects, and a row whose JSON
`content` is larger than `sort_buffer_size` (256 KB by default) fails the sort
with error 1038, "Out of sort memory". `Resource.Meta.ordering` is `["id"]`, so
an unordered queryset sorts too. The UI's reads (rendering, CSS files, item
lists, versions, pull, push, restore, branch deletion) and the resource list's
page (`StandardResultsSetPagination` and the core's `core.lists.page_of`,
which on MySQL page among the keys and read the page's rows by key,
`core.lists.rows_by_keys`) no longer sort content (`scrud_django.rows`;
`tests/test_ui_branch_large_rows.py`, `tests/test_core_lists.py`). Other
reads still use the default ordering: a list view without a paginator, the Django admin's resource
changelist, and code of a project that reads `Resource` rows itself.

Fix: drop the default ordering (`order_by()`) where those read content, or
document a minimum `sort_buffer_size` for MySQL. Test: store a resource whose
content is over 256 KB on `mysql:8.4` with the default configuration and open
the admin's resource changelist; expect error 1038 if it sorts the content
(not yet checked).

<a id="ui-version-storage-growth"></a>
## ui-version-storage-growth: every item write stores a full copy of its branch's items

A version holds all of its branch's own items, not the change, and every write
of an item records one. `main` owns every item written before branches and
every live item, so each edit of any of them stores a full copy of main's
items (and their ETags), and nothing is ever removed: storage grows with the
number of writes times the number of items. Versions are what pinned bases,
branching points, conflict checks and restores read, so they cannot simply be
dropped.

Fix: a retention policy that keeps the versions some branch's branching point,
a push's `result` or a recent window names and deletes the rest (or prunes
their `items`), or versions stored as changes from the previous one. Test:
with 50 items on main, edit one of them 100 times, and assert the stored size
of main's versions stays under a bound; it grows to 100 full copies today.

<a id="ui-stale-proposed-push"></a>
## ui-stale-proposed-push: a proposed push applies to a later branch of the same name

A push names its source and target by name. A push left `proposed` (for
example waiting for approval under a workflow policy) whose source branch is
then deleted, and another branch created under the same name, applies the new
branch's overrides when it is moved to `applied`: whoever approved it approved
other changes. Applying still checks the applier's grants on every item, so it
writes nothing they could not write directly.

Fix: record the source's identity (its pk, or its version when proposed) in
the push and refuse to apply when it changed, or refuse (or cancel) the
proposed pushes naming a branch when it is deleted. Test: propose a push from
`wip`, delete `wip`, create `wip` again with other overrides, move the push to
`applied`, and assert it is refused; it applies the new overrides today.

<a id="ui-css-unconfirmed-pinned"></a>
## ui-css-unconfirmed-pinned: a pinned view by parameter links CSS its files do not serve

A page opened with `?ui-branch=W@pinned` while the prefix's cookie already
holds `W` is not asked to confirm (it is the same branch), and renders `W`
pinned at its branching point, linking the pinned CSS items' URLs. The browser
fetches those CSS files with the cookie but not the parameter, so the CSS view
chooses `W` live, and a CSS item changed since the branching point is not
found there: the page shows without it (a 404 for the file). The banner's own
pinned toggle confirms the switch and sets the cookie to `W@pinned`, so it is
not affected.

Fix: have the CSS links carry the page's selection (a parameter the CSS view
reads), or treat a selection that differs from the cookie only by `@pinned` as
one that asks to confirm. Test: with the cookie set to `W`, change a CSS item of
`W`'s parent after `W`'s branching point, open `?ui-branch=W@pinned`, fetch
each linked CSS URL with the cookie and assert 200; one is 404 today.

<a id="studio-branch-query-cost"></a>
## studio-branch-query-cost: the studio page and strip read the branch tables again for each part

`type_page` and `strip` (`scrud_django.ui.studio.page`) are not run inside
`request_memo()`, so the branch table, the effective rows and the per-branch
selectability checks are read again by each helper: roughly the number of
branches times the depth of their chains in queries per page view. The studio
is an authoring tool for a few people and the counts are small, so it is
slow only with many branches.

Fix: wrap the two views in `request_memo()` (the rules memo is keyed without the
selection, so never render two branches in one `rules_memo`). Test: in
`tests/test_studio_page.py`, count queries of `GET studio/types/<slug>/` with
three chained branches and assert that adding a branch adds no more than a
constant number of queries; it grows with the branch count today.

<a id="studio-own-item-existence"></a>
## studio-own-item-existence: a studio save can reveal whether a branch has an override

`writes.own_item` finds the branch's own row of an item with a system read
(the write database, so that an item just written is seen) before the write is
dispatched. A user who may `GET` the working branch but may not read its items
therefore learns from `save-item`'s answer whether an override exists: with
one the save is a `PUT` of it, without one a `POST`, and which of 428 and 403
comes first can differ between the two, where the resource view alone would
answer alike. It reveals an item's existence and name only, never its
content, and only to someone already allowed to select the branch.
(`revert-item` no longer does: it answers an own row the user may not read as
a missing one, 404, before anything is dispatched; see
`test_revert_reveals_no_row_the_user_may_not_read`.)

Fix: in `save_item`, treat an own row the user may not read as none, or
answer as the view does when not, before choosing `POST` or `PUT`. Test: in
`tests/test_studio_writes.py`, as a user with `GET` on the branch and none on its
item types, `save-item` (no `If-Match`) of an item the branch overrides and of
one it does not must answer the same status.

<a id="studio-replica-lag"></a>
## studio-replica-lag: the answer of new-component reads `wins` and `explanation` from the read database

After `new-component` commits on the write database, it renders the node again
to say whether the new rule `wins` and to give the `explanation`. That read goes
through the read database (`using_selection` and the cascade's own readers), so
with a lagging replica the answer can say `wins: false`, or omit the new rule,
for a rule that was just written. The write itself is right.

Fix: run the post-commit read on the write database (`write_db()`), as
`own_item` does. Test: in `tests/test_studio_new_component.py`, with the read
alias routed to a database that does not see the write (the suite's replica
setup), assert `wins` is true for a rule that wins; it fails today.

<a id="studio-sample-infinite-minimum"></a>
## studio-sample-infinite-minimum: a schema with an infinite `minimum` breaks the sample

`sample_for` (`scrud_django.ui.studio.sample`) rounds an integer `minimum` up
with `math.ceil`, which raises `OverflowError` on `Infinity` and `ValueError`
on `NaN`, though the function is documented never to raise. A stored schema
that compiles with such a `minimum` (JSON allows the Python parser to read
`Infinity`) makes `sample` and `preview` answer 500 for the type.

Fix: use the minimum only when `math.isfinite` holds, else `0`. Test: in
`tests/test_studio_sample.py`, `sample_for({"type": "integer", "minimum":
float("inf")})` and the same with `float("nan")` must return an integer; both
raise today.

<a id="rules-memo-render-state-key"></a>
## rules-memo-render-state-key: the rules memo is not keyed by the render state

`cascade._memo` (`rules_memo`) keeps a request's effective rules and stored
components keyed by contexts and linked stylesheets only, not by the render
state they were read in: the selected branch, safe mode and live-preview
drafts. Today every caller sets that state before it opens the memo (an embed
opens a memo of its own, the live preview sets its drafts first), so the
ordering is load-bearing: a memo opened first would serve rules read without
the drafts, or outside safe mode. Key the memo by `(selection, safe mode,
drafts)`, or refuse a change of that state inside it. Test: in
`tests/test_ui_cascade.py`, inside one `rules_memo()`, read `effective_rules`
for `[""]` with a stored stylesheet on main, then read it again inside
`using_safe_mode()`; the second read should not include the stored
stylesheet's rules, and does today.

<a id="push-write-invariants"></a>
## push-write-invariants: a push's writes and plan do not enforce their own invariants

`sync._Write` takes `method`, `row` and `content` independently, though only
three shapes are valid (`http.PUT`: a row and content; `http.POST`: content
and no row; `http.DELETE`: a row and no content); a `__post_init__` should
check that table. `PushPlan` does not record whether its rows were locked
(`plan_push(..., lock=...)`), so `apply_push` cannot refuse a plan made
without the locks; `refuse_push_between` is called by the push view and the
push preview, not inside `apply_push`, so another caller of `apply_push`
skips it; and the writes that empty the source (`PushPlan.emptied`) carry
the key `()` instead of their item's key. Test: in
`tests/test_ui_branch_push.py`, `_Write("component", UI_COMPONENT_TYPE_URI,
("row",), "http.PUT", branch, row=None, content={...})` should raise
`ValueError`, and `apply_push` given a plan made with `lock=False` should
refuse; both pass silently today.

<a id="rule-index-not-rank-tail"></a>
## rule-index-not-rank-tail: a marked rule is found by the tail of its rank

`render.Mark.matches` finds the live preview's marked rule by `rule.rank[-1]
== index`, which is the rule's index in its stylesheet only because every
rank tuple the cascade builds ends with it; a `Rule.index` field would say
so. Likewise `LiveRequest.mark` stays a dict, checked in `parse_request`
and read again in `_mark`, rather than a parsed type; the drafts a request
names and the drafts accepted for the render share one list; and
`effective.using_drafts` makes no structural check of its drafts (a known
kind, a mapping content, a name). Test: in
`tests/test_studio_live_preview.py`, a rule whose rank does not end with its
index (a cascade change that appends to the rank) should still be the one
marked; and `using_drafts("wip", [Draft("nope", [])])` should raise
`ValueError`, and is accepted today.

<a id="collection-cascade-test-idiom"></a>
## collection-cascade-test-idiom: the collection cascade tests are plain pytest

`tests/test_ui_collection_cascade.py` calls `render_collection` directly in
plain pytest functions (no `@test_context`, no Given/When/Then), like
`tests/test_ui_render.py`, which tests the renderer below HTTP. The test
conventions (CLAUDE.md) describe safetydance tests only. Either convert the
module to the safetydance idiom, or say in CLAUDE.md that unit tests of the
renderer below HTTP may be plain pytest, as these two modules are. Test:
none; a convention to settle.

<a id="studio-safe-links"></a>
## studio-safe-links: the strip's push and version links leave safe mode

On a workbench in safe mode (`?ui-safe=1`), the strip links each push to
`<href>?ui-scope=studio` and each version to
`<href>?ui-scope=studio&ui-mode=view`, without `ui-safe=1`. Following one
from a safe workbench while the studio root's components are broken opens a
page rendered with them (its studio links still offer *Safe mode* again).
The workbench's own requests stay safe (`Scrud-UI-Safe`); only these links do
not. Test: in `tests/test_studio_self_editing.py`, render a workbench with
`?ui-safe=1` and a push and a version in its strip; their links should carry
`ui-safe=1` and do not today.

<a id="studio-editor-drafts"></a>
## studio-editor-drafts: the live preview can lose or miss the editor's unsaved text

`studio:studio` sends the editor's text to the live preview on `keyup` only,
so a paste, cut or drop with the mouse does not update the preview until the
next key. A change of a preview control, and the refresh after a write
(a new rule's 201 included), submit the preview again without the open
editor's draft or the rule editor's mark, so the preview stops showing the
unsaved text. Opening another item, or the same item again after the panes
are fetched again, discards the editor's unsaved text without a warning. And
the draft errors under the editor can still show the previous item's after a
new one opens. Test: in `tests/test_studio_layer_client.py` (node), fire an
`input` event without a `keyup` in the code editor and assert a preview
submit with the draft; change a control with a draft open and assert the
submitted request still has it.

<a id="studio-stylesheet-errors-unshown"></a>
## studio-stylesheet-errors-unshown: some refused fields of a stylesheet show no message

The rule editor renders a refused save's messages beside `applies_to`,
`order` and a rule's `select` and `component`, but not those at `/name`,
`/prefixes/<key>` or `/rules/<i>` itself (a whole rule refused): their nodes
are marked rendered, so the editor's top-level error list skips them, and the
author sees the re-render with no reason. Test: in
`tests/test_studio_rule_editor.py`, PUT the editor's frame with a prefix the
server refuses and assert the message is in the re-render; it is not today.

<a id="studio-rule-editor-preview"></a>
## studio-rule-editor-preview: the rule editor's preview is wrong after editing `applies_to`, and marks nothing on legacy stylesheets

A draft of a stylesheet whose `applies_to` the author changed is previewed as
a second stylesheet beside the stored one, while its save replaces the stored
one (and the open item then names a key that no longer exists). A stylesheet
stored without an `applies_to` field never shows match markers: the mark is
sent with `applies_to` `""`, which does not find it. Test: in
`tests/test_studio_live_preview.py`, preview a stylesheet draft with a changed
`applies_to` and assert the stored rules no longer apply; and mark a rule of a
stylesheet stored without `applies_to` and assert marked nodes.

<a id="studio-new-rule-double-post"></a>
## studio-new-rule-double-post: "New rule for this node" can be created twice

The inspector's Create button is not disabled while its `POST` is in flight,
so a double click creates two components and two rules. Test: in
`tests/test_studio_layer_client.py` (node), click Create twice before the
first answer and assert one request.

<a id="studio-choose-type-stale-preview"></a>
## studio-choose-type-stale-preview: "Choose a type" leaves the old preview

Choosing the type picker's empty option clears `type` from the URL state, but
the previous preview stays in the frame and its clicks are ignored without a
word. Test: in `tests/test_studio_layer_client.py` (node), choose the empty
type after a preview and assert the frame is cleared or a hint is shown.

<a id="studio-strip-refresh-failure"></a>
## studio-strip-refresh-failure: a failed strip refresh stops the preview silently

When fetching the strip again is answered with an error that `scrud-ui.js`
lets htmx swap in (400, 403, 409, 412, 428), the answer replaces the strip,
which removes `[data-studio-endpoints]`; `submitPreview` then returns `false`
and the preview, the explanation and the new rule stop working with no notice
until the page is reloaded. Test: in `tests/test_studio_layer_client.py`
(node), answer the strip's refresh with a 403 and assert the endpoints are
kept (or a notice shown) and the next `submitPreview` sends.

<a id="studio-push-review-layers-branch"></a>
## studio-push-review-layers-branch: the push review shows `branch @layers` before a create over a packaged item

A push that creates the target's row of an item a packaged layer provides
(an override of `studio:rule-row` pushed from `studio-wip` into `studio`, in
the acceptance run) lists the layer's item as the change's `before`, and
`studio:push-preview` shows its fields as they are, `branch` `@layers`
included: an internal name the author never chose, where the review's
"Before" should say the item is packaged. Test: in
`tests/test_studio_promote.py`, render the review (scope
`studio studio-push`) of a push that overrides a layer item and assert its
"Before" says packaged and does not show `@layers`.

<a id="studio-client-notice-unstyled"></a>
## studio-client-notice-unstyled: client notices are unstyled in the studio

The studio's CSS item `studio:studio` styles the workbench's own notice
(`.studio-notice`) but not the runtime's client notices
(`.scrud-client-notice`: "Your change was not saved ...", "Your change was
not sent ...", "This action is misconfigured."), which `scrud-ui.js` puts at
the top of a frame, so in the editors they read as plain text above the form
(seen in the acceptance run's step 9). Test: in `tests/test_studio_layer.py`,
assert the layer's CSS has a rule for `.studio .scrud-client-notice` (or
`[data-studio] .scrud-notice`).

<a id="studio-promote-stale-review"></a>
## studio-promote-stale-review: a stale push review can enable Apply for a moment

Opening, closing and opening a promote panel again sends two review requests;
the first answer, if it arrives last, can turn Apply on from a stale review.
The server still refuses a push with a conflict (409), so nothing wrong is
written. Test: in `tests/test_studio_layer_client.py` (node), answer the
second review with a conflict and then the first without one, and assert
Apply stays off.

<a id="studio-refused-push-state"></a>
## studio-refused-push-state: a refused push shows the state it was refused

A push page whose move to `applied` is refused (a workflow policy, a
conflict) re-renders the submitted body, so the state input reads `applied`
beside the refusal, although the push is still `proposed`. Test: in
`tests/test_studio_promote.py`, refuse the move with a policy and assert the
re-render's state is the stored one, or is marked as not saved.

<a id="studio-after-push-inspector"></a>
## studio-after-push-inspector: after "Preview after push" the inspector explains T, not the target

"Preview after push" shows the target branch in the preview, but a click in it
is explained for T (and "New rule for this node" would create on T), which is
wrong when the target's presentation differs. Test: in
`tests/test_studio_layer_client.py` (node), preview after push and click a
node; the explanation request should name the target with `after_push`, or
the inspector should say it cannot explain that preview.

<a id="studio-remove-prefix-submitted"></a>
## studio-remove-prefix-submitted: "Remove the prefix" in a refused re-render saves the other submitted edits

When a publisher's save of a working branch's settings is refused for its
prefix, the re-render is built from the submitted body, and so is its "Remove
the prefix" button: pressing it saves the other fields as they were
submitted, not as stored. Test: in
`tests/test_studio_rollback_presentations.py`, refuse a save that changed the
label and the prefix, press "Remove the prefix" from the re-render, and
assert the stored label is unchanged.

<a id="ui-page-fallback-comment"></a>
## ui-page-fallback-comment: the page fallback names the banner when the studio links are missing

A page component that leaves out the studio links (and not the banner or the
safe mode notice) is replaced by the built-in page after the comment and log
`page component "x" leaves out the branch banner or the safe mode notice`,
which names the wrong chrome. Test: in `tests/test_studio_layer.py`
(`test_studio_links_required_in_page`), assert a comment that names the
studio links.

<a id="studio-inspector-stuck"></a>
## studio-inspector-stuck: the inspector can be left "Explaining..." or invite a second Create

The inspector's `send` (`studio:inspector`) reads a response's body with
`response.text()` and has no handler for that promise failing, nor for an
exception in what renders the answer. A connection dropped in the middle of
the body, or a render that throws on an odd answer, leaves the explanation
at "Explaining..." or the form at "Loading the options..." with no message.
And a 201 whose body does not parse as JSON is shown under the form as "The
answer could not be read.", like a refusal, though the rule was made:
pressing Create again makes a second component. Test: in
`tests/test_studio_layer_client.py`, answer an explain with a response
whose `text()` rejects, and assert the inspector says the request failed;
answer a Create with a 201 whose body is not JSON, and assert the form
says the rule was made (and the panes are fetched again), not a failure.

<a id="ui-load-error-request-lock"></a>
## ui-load-error-request-lock: after `htmx:onLoadError` a frame's later edits are queued and never sent

When handling a response throws, htmx 2.0.4 fires `htmx:onLoadError` and
does not end the element's request lock (no `endRequestLock`). `scrud-ui.js`
stops counting the frame as in flight then (it settles on
`htmx:onLoadError`), but htmx itself still sees a request in flight on that
element: its later requests are queued behind one that never ends, and so
never sent, with no notice. This pairs with
[ui-inflight-test-real-htmx](#ui-inflight-test-real-htmx): only the
vendored htmx shows it. Test: with the vendored htmx in a real DOM (jsdom,
or the browser acceptance run), make a frame's save answer with markup whose
swap throws, then change a field of the frame; assert a PUT is sent (or a
notice says it was not).

<a id="studio-link-unreadable-root"></a>
## studio-link-unreadable-root: the Studio link shows the studio root to viewers who can't read it

`studio.links.studio_href` builds the *Studio* link from
`SCRUD_UI_STUDIO_BRANCH` without checking that the viewer may GET that
branch: every viewer of a UI type's page gets a link that names the studio
root's primary key under its prefix, and for one who may not read it the
link is dead (a 404). Test: in `tests/test_studio_layer.py`, render a UI
type's page for a user who may GET the page but not the studio root; assert
there is no *Studio* link (or that it says the user can't open it).

<a id="studio-api-url-edge-tests"></a>
## studio-api-url-edge-tests: `api_url`, `in_mount` and `safe_href` edge cases are untested

`embed.api_url` refuses a slug that is not a string or is empty, one with no
such route, one whose route is not under the script prefix, one that does
not resolve (`Resolver404`), and one whose route is not that type's resource
view; a computed type has only a detail route. The tests cover the common
cases only: none passes a non-string or empty slug, asks a computed type's
list (no key), uses a key that needs percent-encoding, or runs under a
non-default script prefix. `in_mount` (outside a mount, a path outside the
API, an absolute URL) and `studio.links.safe_href` (a query with repeated or
encoded parameters, an existing `ui-safe`) are likewise only tested on
their plain path. Test: in `tests/test_studio_layer.py`, call each with
those inputs (`api_url(7)`, `api_url("")`, a computed type with no key,
`api_url("todo-lists", "a/b c")`, under `set_script_prefix("/app/")`) and
assert the `ValueError`s and the exact paths.

<a id="studio-endpoints-all-or-none"></a>
## studio-endpoints-all-or-none: the strip's `endpoints` is null when any one of them does not reverse

`studio.computed._endpoints` reverses the five bootstrap studio endpoints in
one `try`: a `NoReverseMatch` for any one of them (a project that includes
some of the studio's URLs, or renamed one) makes the whole map `null`, so
the workbench loses the live preview and the inspector though most of them
exist. It is documented (docs/ui.md, the status's `endpoints`). Test: in
`tests/test_studio_computed.py`, with a URLconf that includes the studio's
URLs without `new-component`, read the status; it answers
`"endpoints": null` today, where the others (and `null` for the missing one)
would keep the preview working.

<a id="studio-beneath-chain-error"></a>
## studio-beneath-chain-error: an own row's Delete says nothing is inherited when the parent can't be read

`studio.computed._beneath` reads what deleting an own row reverts to from
the parent's state (`parent_state`). When that read fails (a broken chain, a
missing pinned snapshot: `BranchChainError`), it now logs a warning, but the
entries still get `reverts_to: null`, so the editor's Delete asks "The
branch has no inherited version of it" when there may well be one. Test: in
`tests/test_studio_computed.py`
(`test_items_beneath_a_broken_chain_are_logged`), the own row's
`reverts_to` is `null`; it should say that what is beneath is unknown (for
example `"unknown"`), and the workbench's Delete should say so instead of
claiming nothing is inherited.

<a id="studio-workbench-brittle-asserts"></a>
## studio-workbench-brittle-asserts: workbench tests assert whole structures

Some tests in `tests/test_studio_workbench.py` assert exact structures of
the page: the dict of every embed (`data-studio-embed` to tag, source and
scope) and the list of every form in the page. Any new embed or form fails
them though nothing they check changed, which invites loosening them by
hand. Test (a refactor, not a bug): assert each embed and form the test is
about by key, plus a separate assertion that no unexpected write control
appears (`Page.writes()`), so an addition fails only the test that is about
it.

<a id="studio-editor-load-race"></a>
## studio-editor-load-race: an item still loading can land under the next item's header

htmx 2.0.4 queues a second `htmx.ajax` on the same source element (the
editor) with its "last" strategy. When item A is still loading as item B is
opened, A's response swaps into the editor under B's header, replacing
B's "Loading" placeholder; if B's queued GET then fails, `loadFailed`
(`studio:studio`) finds no placeholder, so A's editable frame (which PUTs to
A) stays under B's header. A possible fix: an `htmx:beforeSwap` guard on the
editor that refuses a swap whose `requestConfig.path` is not the current
`data-studio-loading` href. Test: in `tests/test_studio_layer_client.py`,
open A, open B, swap A's frame into the editor (as htmx would deliver the
late answer), then fail B's GET; assert no frame of A is in the editor and
B's error text is.

<a id="studio-new-rule-reopened-duplicate"></a>
## studio-new-rule-reopened-duplicate: a form reopened before its Create answers invites a second Create

Create clicked, the form closed (another node selected), and the same
node's form opened again (restoring the kept template) before the 201
arrives: the success marks only the old form as created. The reopened form
shows the template just created, with "The template you typed for this node
before is back.", counts as unsaved, and invites a duplicate Create. A
possible fix: on success, when the form now shown has the same node key and
its template equals the one sent, mark it created too. Test: in
`tests/test_studio_layer_client.py`, type a template, Create, select another
node, open the first node's form again, then answer the Create with 201;
assert `S.unsaved()` is false and the form does not offer the created
template as unsaved work.

<a id="studio-new-rule-kept-key"></a>
## studio-new-rule-kept-key: kept new-rule templates are keyed by type and pointer only

A template typed in a new rule form that closes is kept by the preview's
type and the node's pointer, not its instance, scope or data context, so a
template typed for `/items/0` of one instance is restored in the form for
`/items/0` of another instance (or another scope or context). Test: in
`tests/test_studio_layer_client.py`, type a template for `/items/0` with
`instance=7`, close the form, change `instance` to 8, open the form for
`/items/0`; assert it has the default template, and that the typed one comes
back with `instance=7` again.

<a id="drf-anyof-list-object-check-only"></a>
## drf-anyof-list-object-check-only: the DRF list and readability ask a `|` option's object check alone

On a DRF view whose permissions compose `IsAuthorized` subclasses with `|`,
the detail route asks each operand's request check before its object check
(DRF's `OR.has_object_permission`), but `scrud_django.filters`
(`meets_object_checks`, `readable_by_object_checks`,
`filter_by_object_checks`) asks the object checks alone. So the list shows
an object that its own detail GET refuses, and a refusal of an object the
requester cannot GET is a 403 rather than a 404. The facade
(`scrud_django.core`) already pairs the two (`request_checks=True`); the DRF
views keep `develop`'s answers until this is decided. Test: in
`tests/test_core_requirements.py`, with a view whose permission is
`RequestRefusingIsAuthorized | ObjectRefusingIsAuthorized`, list json-ld as
a reader in "a" with a resource stored in "a"; the list includes the
resource, and its detail GET answers 403. It should leave the resource out
and answer 404, as the facade does
(`test_anyof_option_refused_at_request_level_is_not_satisfied`).

<a id="studio-computed-to-compute"></a>
## studio-computed-to-compute: the studio's computed types still register a viewset class

The ninja front-end design
(`docs/superpowers/specs/2026-11-11-ninja-front-end-design.md`, §1.4) moves
the studio's computed types to the neutral `compute=` hook, but
`scrud_django/ui/studio/computed.py` (`studio_computed_type_urls`) still
registers each with `viewset_class=`, so a non-DRF front end gets no content
for them. Test: in `tests/test_studio_computed.py`, assert that
`core.types.type_options(slug).compute` is set for each slug of
`STUDIO_COMPUTED_TYPES` (it is `None` today).

<a id="core-save-checked-mark-body"></a>
## core-save-checked-mark-body: `save_checked` trusts the workflow mark, not the body

Before ninja phase 3. The workflow check (`validate_write`) marks the
request authorized for one write, `(type IRI, stored pk)`
(`_mark_workflow_authorized`, `core/authz.py`), and `save_checked`'s
post-save check lets a grant conditioned on `scrud_workflow` allow that
one save, consuming the mark. The mark names the write, not the body:
a caller of `save_checked` itself that validates body A and saves body B
of the same resource passes the `scrud_workflow` grants with B. The
facade's `create` and `update` always validate the body they save, so
front ends must write through them (or validate exactly the body their
own `save` writes). Proposal: carry a fingerprint of the validated body
(and its contexts) in the mark and compare it with what `save` wrote, or
make `save_checked` take the body and validate it itself. Test: in
`tests/test_core_writes.py`, with a JSON-LD policy in "a" where a draft is
published by "publish" and a user holding `may_stay_json_ld()` in "a",
`validate_write` a body that stays a draft, then `save_checked` an
`update_locked` save of a body publishing it; assert it is refused and
the row unchanged (today it saves).

<a id="core-write-no-recheck"></a>
## core-write-no-recheck: the facade's `create` and `update` take no `recheck`

Before ninja phase 3. `scrud_django.core.writes.create` and `update` take a
`type_iri` but no `recheck`: their post-save check is always
`check_post_save` of the requirement they are given. A DRF view threads a
post-save check of its own (`check_post_save_permissions`, its
`object_for_checks`) through `save_checked(recheck=...)`; a front end that
needs one must call `save_checked` itself, with a `recheck`, after
`validate_write` of exactly the body its `save` writes (see
`core-save-checked-mark-body`). Writing through `create` or `update`
silently drops it. Test: in `tests/test_core_writes.py`, with a post-save
check refusing a resource named as `PostSaveRefusingResourceViewSet.REFUSED`
(`tests/urls.py`), write such a resource through `core.writes.update` given
that check; assert it is refused and the row unchanged (today `update` has
no parameter for it, so the front end cannot ask it there).

<a id="core-viewleaf-list-request-check"></a>
## core-viewleaf-list-request-check: a list's GET drops a wrapped `IsAuthorized` subclass's own request check

On a list's GET, `core.resources.check_request` asks each leaf trusted to ask
the grants as a `GrantsLeaf` of its list type (`_as_list_requirement`),
beside the leaf itself only when its class's `has_permission` is not its
registered base's (`_own_request_check`). The DRF `ViewLeaf` is registered
and has its own `has_permission`, which calls the wrapped `IsAuthorized`'s:
a wrapped subclass with a stricter `has_permission` is replaced by the list
grant, and its refusal is dropped. Latent: the facade is never given a
`ViewLeaf` (the DRF views ask their own permissions, and `requirement_for`
builds `GrantsLeaf`s). Fix before a front end hands the facade a view's
requirement: ask a `ViewLeaf` whose permission's class overrides
`has_permission` beside its list grant. Test: in
`tests/test_core_requirements.py`, `check_request` of a list's GET with a
requirement of a `ViewLeaf` wrapping an `IsAuthorized` subclass whose
`has_permission` refuses, as a user holding the list grant; assert
`AccessDenied` (today it allows).

<a id="services-unrouted-types"></a>
## services-unrouted-types: `services/` fails when the serving URLconf does not route every registered type

The service catalog (`scrud_django.services.services`) is process-global: it
lists every type registered in the process, whichever URLconf serves them.
An entry the serving URLconf does not route raises `NoReverseMatch` (a 500)
when `services/` is read, on either front end. It happens when two ninja
APIs with different `types=` are served from one process, and when a
ninja-only URLconf is read after the DRF routes registered other types.
Owner's call: omit the entries that do not reverse on ninja, or state a
one-API-per-process rule. The README states the failure.

Test: GET `/api/services/` under `tests.ninja_only_urls` after `tests.urls`
registered `todos`; assert 200 (it is a 500 today). The ninja-only tests
isolate the catalog (`monkeypatch` of `services.services`) for this reason,
and `tests/test_error_media_types.py::test_the_service_lists_conditional_400_follows_uniform_errors`
stays DRF-only.

<a id="ninja-semantic-urls-include"></a>
## ninja-semantic-urls-include: a ninja-only URLconf must include `scoped_rbac.semantic_urls`

The role routes (`roles/`, `role-assignments/`, `user-rbac-policy/`) add
semantic Links that scoped-rbac reverses by global name. A URLconf that
mounts only `scrud_django.ninja.api().urls` lacks them, and those three
routes answer 500 (`NoReverseMatch`). 0.16.0 documents the
`include("scoped_rbac.semantic_urls")` in the README; a system check, or
`api()` mounting them, belongs to the release that makes ninja the default
`scrud_django.urls`. Test: a URLconf of the ninja API alone; GET `roles/` as
a superuser; assert 200 (a 500 without the include).

<a id="ninja-uniform-errors-per-front-end"></a>
## ninja-uniform-errors-per-front-end: an API built with uniform errors off keeps scoped-rbac's errors native

scrud's own errors, scrudful's 400, 412 and 428, the 406 and every error
scoped-rbac's handlers answered as problem details follow
`SCOPED_RBAC["UNIFORM_ERRORS"]` for each request
(`errors.answer_natively_when_off`, pinned by
`tests/test_front_end_parity.py::test_errors_follow_uniform_errors_per_request_alike`
for an API built with them on). But scoped-rbac's `install_error_handlers`
reads the setting once, when the API is built: built with uniform errors
off, it registers only its native access-error handler, so turning them on
later leaves its errors native, where DRF answers problem details (an
anonymous GET of an item is a 401 `{"detail":"Unauthorized"}`, served as
`application/problem+json`; a PATCH a 405 `{"detail":"Method not
allowed"}`). Test: build `api(types=[partner profiles])` under
`override_settings(UNIFORM_ERRORS=False)` in a URLconf of its own, then,
with uniform errors on, GET an item anonymously; assert a 401 problem
(`"code": "not_authenticated"`); it is native today.

<a id="ninja-hsr-link-targets"></a>
## ninja-hsr-link-targets: an API included under an outer namespace cannot describe its links

scrudful builds a route's HTTP Schema Resource without a request (its link
functions are called with `(None, None)`), and the ninja routers reverse
those links within the API's own `urls_namespace` first
(`namespace=urls_namespace`, `core.urls.absolute_reverse`), so a ninja HSR
names the ninja routes when both front ends are mounted (pinned by
`tests/test_front_end_parity.py::test_each_front_ends_hsr_links_to_its_own_routes`).
An API included under an outer namespace
(`path("v1/", include(([path("api/", api.urls)], "v1")))`) is reversed as
`v1:<urls_namespace>:<name>`, which the describing code does not know: the
plain name is tried next, so the HSR names the DRF routes when they are
mounted, and fails without them (ninja's `ConfigError` from
`NoReverseMatch: 'json-schema-detail'`, a 500), though the item's own
`Link`s (reversed with the request) resolve. Test: that URLconf with a
ninja-only API; GET an item (200), then its HSR link; assert a 200 whose
links start with `/v1/api/`; it raises today.

<a id="ninja-collection-options-drf-defaults"></a>
## ninja-collection-options-drf-defaults: the collection documents' OPTIONS body copies DRF's defaults

The ninja OPTIONS of `collections-json-schema/<slug>/` and
`collections-json-ld/<slug>/` lists DRF's default renderer and parser
classes, and the name and description DRF's `VIEW_NAME_FUNCTION` and
`VIEW_DESCRIPTION_FUNCTION` give those views. A project that sets
`REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]`, `DEFAULT_PARSER_CLASSES` or
those functions gets a different DRF body than ninja's. The ninja OPTIONS
answers JSON where DRF renders HTML for `text/html` (its 406 for a type it
cannot give is DRF's, `scrud_django.ninja.negotiation`), and is throttled as any ninja operation is by the
API's throttles (scrudful's automatic OPTIONS runs none; `api()` sets none).
Test: with `DEFAULT_PARSER_CLASSES` set to JSON only, OPTIONS
`collections-json-schema/partner-profiles/` on both front ends and compare
the bodies; they differ today.

<a id="ninja-allow-of-entry-refusals"></a>
## ninja-allow-of-entry-refusals: a rewritten OPTIONS is refused unlike DRF on some routes

A CSRF-refused POST or PUT to the collection documents, `services/` and the
computed routes answers alike on both front ends, DRF's `Allow` included
(`tests/test_front_end_parity.py::test_csrf_refusals_answer_alike`). What
remains is an OPTIONS whose `request.method` a middleware rewrote
(`tests.middleware.MethodOverrideMiddleware`, `X-HTTP-Method-Override:
PUT`): ninja refuses it on every route (403, 401 for the anonymous;
scoped-rbac's URL-pattern refusal), but DRF's collection documents and
`services/` refuse nothing (their views are no `AccessControlledAPIView`)
and answer the rewritten method's 405; and on the type routes, where both
refuse it, ninja's refusal lacks the HTTP Schema Resource link and the
`Allow` in `Access-Control-Expose-Headers` that DRF's carries. Test: with
that middleware, OPTIONS `collections-json-schema/partner-profiles/`,
`services/` and `partner-profiles/` with the override on each front end
(the parity harness's `exchange`), and compare; they differ today (405 and
403, and the Links).

<a id="ninja-put-reload"></a>
## ninja-put-reload: a ninja PUT's validators come from the written row, with no second check

After a 2xx PUT, scrudful loads the resource again for its validators and
Links; the ninja operation hands it the row the core wrote
(`routers.WRITTEN_ATTR`) without a second request or object check (the
plan's design). DRF reloads through `get_object()` and drops the validators
if that fails. They answer alike unless a grant changes between the write and
the reload. Test: a PUT whose `scrud_post_save` revokes the writer's read
grant; compare `ETag` and `Link` of the answers on both front ends.

<a id="ninja-user-hook-401-message"></a>
## ninja-user-hook-401-message: a `NINJA_USER` hook's own 401 message is not carried

With `UNIFORM_ERRORS` off for ninja, a `NINJA_USER` hook raising ninja's
`AuthenticationError` is answered with the generic "Authentication
credentials were not provided." rather than the hook's message. Test: a hook
raising `AuthenticationError("expired")`; assert the body names it (it does
not).

<a id="ninja-string-content"></a>
## ninja-string-content: a resource whose content is a `str` is returned as stored

DRF's `to_content` runs `json.loads` on a resource whose content is a
`str`; the ninja retrieve returns `obj.content` as stored. No test resource
has string content. A core representation helper would give both the same
body. Test: save a resource whose `content` is a JSON string and GET it on
both front ends; compare the bodies.

<a id="ninja-private-upstream-use"></a>
## ninja-private-upstream-use: the ninja front end leans on private scrudful and ninja internals

Each is worked around here; none has a public replacement upstream yet.

- `routers.py` uses `scrudful._adapter.set_validator_headers` and
  `scrudful._django` (`aware`, `conditional_response`, `precondition_headers`),
  and `scrudful._hsr.is_preflight` for the OPTIONS answer. There is no
  public conditional rendering for a described (plain) operation with an
  error entity, so `_precondition_response` composes
  `scrudful._django.conditional_response` with scoped-rbac's
  `conditional_problem` and `PROBLEM_LINKS`; and no public preflight test or
  way to keep scrudful's automatic OPTIONS while supplying the whole `Allow`
  (its `allowed=` takes a per-method hook on a loaded object, ruled out by
  R4).
- Every URL-level wrapper monkeypatches ninja's private `NinjaAPI._get_urls`:
  `entry.authenticate_first`, `errors.answer_natively_when_off`,
  `negotiation.negotiate_as_drf` and `routers._wrap_patterns` (which feeds
  `vary_on_accept`, `as_drf_api_views`, `method_not_allowed_as_drf`,
  `refuse_the_anonymous` and `as_drf_api_view_function`). It is the largest
  private dependency; its test is `tests/test_ninja_api.py` breaking on a
  ninja upgrade, given the `<1.7.4` pin. `entry.py`, `services.py` and
  `negotiation.py` use `scrudful._hsr.is_preflight` and `scrudful._django`
  too, and `negotiation.py` calls `entry._hsr_link_of` and
  `entry._described_as`, private across modules.
- A ninja handler has no handle on its API, so `_noting_serving_api` finds it
  through `inspect.unwrap(run).__self__.api` (scrudful has a private
  `_SERVING_API`), which holds only while every view decorator before it uses
  `functools.wraps`; `entry` does the same.
- `entry._hsr_link_of` takes the HTTP Schema Resource link from scrudful's 405
  to a copy of the request with an unserved method (`TRACE`), as there is no
  public lookup for a path's link. An unguarded call could turn a
  misconfigured path into a 500; a `try`/`except` falling back to no link
  would stop that.
- scrudful names ninja's HSR routes `scrudful_hsr_<n>_<template>_<m>` (with
  counters), so nothing can `reverse()` them; `tests.front_ends.hsr_url`
  derives the URL from the path template and breaks if the naming changes.
- scrudful's DRF HSR cache (`_HSRS`) is cleared only on `setting_changed`, and
  so is its ninja one: neither follows a change of `ROOT_URLCONF` made by
  pytest-django's `urls` mark (which sends no signal), so a test can be served
  an HSR built under another URLconf. `tests/test_front_end_parity.py` wraps each test in `override_settings` to
  send the signal.

Test: for each, one that fails when upstream renames or removes the helper:
`tests/test_ninja_api.py` and `tests/test_front_end_parity.py` already break
on import or on the first request when a helper goes. For the cache, a test that switches
`ROOT_URLCONF` with the `urls` mark and asserts the second HSR's links; it is
served stale today.

<a id="ninja-openapi-head-operation-id"></a>
## ninja-openapi-head-operation-id: a GET-and-HEAD operation is described twice under one `operationId`

With `docs_url=`/`openapi_url=` turned on, every scrud operation has an id
of its own (`<slug>_list`, `<slug>_retrieve`, ...), and ninja prints no
duplicate warning. But ninja describes one operation serving GET and HEAD
(the lists, items, computed and collection routes, and scoped-rbac's role
routes) under both methods of its path with that one id, which OpenAPI asks
to be unique per method. Splitting GET and HEAD into two operations would
change how scrudful serves them, and scoped-rbac's routes are upstream.
Test: build `api(docs_url="/docs", openapi_url="/openapi.json")`, collect
every `operationId` of `get_openapi_schema()["paths"]` per path and method,
and assert none repeats (`partner-profiles_list` repeats today).

<a id="scoped-rbac-list-iri"></a>
## scoped-rbac-list-iri: proposal, a `list_iri=` parameter

Proposal for django-scoped-rbac (spec §3.5 item 1): `list_check` and
`authorized_list` take `list_iri=` (default `"<iri>/list"`), and
`crud_router` passes it through. scrud's list is granted on the type's list
IRI and filtered by a resource's stored contexts, which scoped-rbac's own
list check does not know; without it scrud cannot use `crud_router`'s list; the
ninja list is a described GET over `core.lists` (R1 of `ninja-3a-rulings`). Test (upstream): `crud_router(..., list_iri="x/list")`; a user
granted `x/list` lists, one granted only `<iri>/list` gets 403.

<a id="scoped-rbac-crud-router-checks"></a>
## scoped-rbac-crud-router-checks: proposal, extra checks on `crud_router` and the blocks

Proposal (spec §3.5 item 2): `crud_router(..., checks=[...])` and the building
blocks take extra item and write checks, called in the same transaction in a
documented order (after the request check, before the save). scrud's
workflow check, body checks and every-context policy check are such checks;
today the ninja routers are hand-written to place them. Test (upstream): a
check that raises refuses a PUT before the save and rolls the write back.

<a id="scoped-rbac-options-denials"></a>
## scoped-rbac-options-denials: proposal, a public `_options_denials`

Proposal (spec §3.5 item 3): make `_options_denials` public, so wrapped
`before`/`load` hooks answer scrudful's OPTIONS copies correctly (an
`AccessError` on a copy must become Django's `PermissionDenied`, which drops
the method from `Allow`, or `Http404`). scrud keeps a five-line equivalent,
`routers._options_denials`, for its own hooks. Test (upstream): a custom
`load` raising `AccessDenied` on an OPTIONS copy drops its method from
`Allow` instead of failing the OPTIONS.

<a id="scoped-rbac-ninja-docs-pin"></a>
## scoped-rbac-ninja-docs-pin: `docs/ninja.rst` states a stale ninja pin

Proposal (spec §3.5 item 4): the page still says ninja 1.7.0 and 1.7.1 and
`<1.8`, where the package pins `>=1.7,<1.7.4`. Test (upstream): a docs test
comparing the page's range with the package metadata's.

<a id="scoped-rbac-ninja-auth-digest"></a>
## scoped-rbac-ninja-auth-digest: the auth-loop digest knows only ninja 1.7.0 and 1.7.1

scoped-rbac copies ninja's `_run_authentication` to record which
authenticator succeeded, guarded by a source digest of ninja 1.7.0 and
1.7.1. On 1.7.2 and 1.7.3 (inside the pin) it logs a warning once and
CSRF-checks every unsafe request of a session user, header credentials
included. It fails closed, but a bearer-token or API-key client that is
exempt on 1.7.0 and 1.7.1 is refused a write with 403 "CSRF Failed". Test
(upstream, on ninja 1.7.2): a PUT with `Authorization: Bearer ...` and no CSRF
token from a `NINJA_USER` hook returning a non-session user; assert it is
allowed (it is refused today).

<a id="ninja-drf-authenticators"></a>
## ninja-drf-authenticators: ninja does not run DRF's authentication classes

Ninja authenticates as spec §3.2 has it: scoped-rbac's `resolve_user`, which
takes the `SCOPED_RBAC["NINJA_USER"]` hook's user, else `request.auth`, else
the session's. It does not read DRF's `DEFAULT_AUTHENTICATION_CLASSES`, so a
Basic or token client of a DRF project is the anonymous user on ninja (a
hidden item is a 404/401, `services/` lists the anonymous user's grants)
until the project writes a `NINJA_USER` hook (the README has one for Basic).
`api()` forbids `auth=`, so ninja's own `HttpBasicAuth` is not an option
either. Spec §3.3 does not say so: that omission is for the owner to settle
in the spec (the spec document is not edited here). Making ninja run DRF's
authenticators would tie it to DRF just as part C moves DRF into an extra;
the option is an optional hook scrud provides that runs DRF's authenticators
(for a project that has DRF installed), to be decided with part C.
Test: `tests/test_ninja_auth.py` pins today's answers (Basic alone is
anonymous; the hook maps it); the hook would add a case where DRF's Basic
authentication alone gives the item GET a 200 on ninja.

<a id="scoped-rbac-ninja-user-hook-csrf-doc"></a>
## scoped-rbac-ninja-user-hook-csrf-doc: the `ninja_user_hook` docstring says a hook's user skips the CSRF check

scoped-rbac's `conf.ninja_user_hook` docstring says a user a `NINJA_USER` hook
gives skips the CSRF check. That is not true for the session's user: a hook
that returns the request's session user (as the README's Basic hook does for a
request with no Basic header) is CSRF-checked on an unsafe method, and so is a
request with a session cookie whose Basic credentials resolve to that same
user. Only a hook's user who is not the session's authenticated user skips it.
This is an upstream docs proposal (scoped-rbac is not edited here); the README
states the behaviour. Test: `tests/test_ninja_auth.py` (the session-user cases).

<a id="ninja-atomic-requests-rollback"></a>
## ninja-atomic-requests-rollback: ninja's error handlers do not mark an `ATOMIC_REQUESTS` transaction for rollback

DRF's `exception_handler` calls `set_rollback()` for every error it
answers, so under `ATOMIC_REQUESTS=True` a request that wrote and then
answered a handled error is rolled back. Ninja's handlers
(`scrud_django.ninja.errors.scrud_error`, and scoped-rbac's upstream) do
not, so such a request commits on ninja. It is not reachable through
scrud's routes (no route writes outside `save_checked`'s atomic block and
then answers a handled error; `entry` rolls back a PUT, PATCH, DELETE and a
conditional POST itself), but an app's own route can. Fix: call
`django.db.transaction.set_rollback(True)` for each atomic database in
`scrud_error`, or document the divergence for app routers. Test: an app
route behind `entry` that writes a row and raises a handled error under
`ATOMIC_REQUESTS=True`; the row should not exist.

<a id="ninja-namespace-fallback-logging"></a>
## ninja-namespace-fallback-logging: a link that falls back across namespaces is silent

`core.urls.absolute_reverse` reverses `<request namespace>:<name>` first and,
failing that, the bare name; the docstrings document it. When both front
ends are mounted, a ninja answer (a `Link`, `Location`, a service catalog
entry, a workflow error's `policy`) can then point at the DRF mount, with
nothing logged, and the parity harness would not catch it because both
mounts route everything. `StoredPolicyReference.url` also returns `None`
when neither reverses (pre-existing). Fix: log at debug (or warn) on the
fallback when a namespace was present, or make the fallback opt-in for the
ninja adapter. Test: reverse a name the namespace lacks and assert the log
record.

<a id="ninja-entry-write-db"></a>
## ninja-entry-write-db: `entry` always writes on scrud's write database

`entry` opens its write transaction on `write_db` (scrud's resource
database, `core.writes.write_transaction`). An app's own models, routed to
another database (part B's), are not in that transaction: a rollback on a
400 does not undo their writes. Part B decides whether `entry` takes the
database as an argument (`entry_for(using=...)`) or an app router opens its
own. Test: a probe route writing a model of a second database and answering
400; the row should be rolled back.

<a id="ninja-computed-link-none"></a>
## ninja-computed-link-none: `computed_link` accepts "no type"

`core.computed.computed_link(request, None)` returns `None` for a missing
resource type. Accepting "no type" is a smell in a typed core function: the
caller should decide. Left as is for now: nothing in scrud's routes passes
`None` (the callers check first). Test: assert that `computed_link(request,
None)` raises `TypeError` once the callers are tightened.

<a id="ninja-negotiation-compact-problem"></a>
## ninja-negotiation-compact-problem: ninja decides a problem's compactness by its Link header

Ninja's negotiation renders a problem compactly when the response carries the
problem-details `Link`, a stand-in for "went through scoped-rbac's handler".
A `SCRUDFUL["ERROR"]` entity whose Links come first would diverge: DRF
renders it compact, ninja indented. Not reachable with the shipped error
entity. Fix: have the handler mark the response (an attribute) and read that.
Test: set `SCRUDFUL["ERROR"]` to an entity with its own Links first, request
a 404 with `Accept: application/json; indent=4`, and compare both front ends.

<a id="scoped-rbac-ninja-http404-message"></a>
## scoped-rbac-ninja-http404-message: the ninja handlers drop an `Http404`'s message

`install_error_handlers` answers any `Http404` with "Not found.", where DRF
keeps the exception's message. scrud re-raises its collection routes' and
its computed routes' (a `compute`'s) as `HttpError(404, message)`, and a
`compute`'s `PermissionDenied` as `access.AccessDenied(message)` (the
handler answers a Django `PermissionDenied` with the default message).
Test (upstream): a handler raising
`Http404("No such schema.")`; assert the problem's `detail`.

# Decided

<a id="ninja-postgres-subprocess-test"></a>
## ninja-postgres-subprocess-test: the first-parity-call test gives its subprocess a database of its own

The subprocess pytest of
`tests/test_front_end_parity.py::test_hooked_types_are_served_alike_on_a_processs_first_parity_call`
used to create `test_scrud` while the parent session held it. Fixed in
bccf2e8: on PostgreSQL and MySQL it sets
`SCRUD_TEST_<ENGINE>_NAME` to `<name>_first_parity_call`.

<a id="scoped-rbac-checks-before-405"></a>
## scoped-rbac-checks-before-405: only `AccessDenied` from `resolve_user` is caught before a 405

Upstream, `scoped_rbac.ninja.blocks._checks_before_405` catches only
`access.AccessDenied` from `resolve_user`, so a `NINJA_USER` hook raising
ninja's `AuthenticationError` on a method a ScrudfulRouter path does not
route escapes as a 500. scrud works around it on every route `api()` mounts
(`entry.authenticate_first`, scoped-rbac's role routes included; 68022e6),
pinned by `tests/test_front_end_parity.py::test_failing_credentials_are_refused_alike`.
A ScrudfulRouter an app adds itself is not covered; the upstream proposal
stands: a hook raising `AuthenticationError`, PATCH a `crud_router` path,
assert 401.

<a id="scoped-rbac-ninja-policy-vary"></a>
## scoped-rbac-ninja-policy-vary: `user-rbac-policy/` OPTIONS now sends `Vary: Cookie` through scrud's `api()`

Both front ends answer `Vary: Cookie` (68022e6): `authenticate_first`
resolves the user, which touches the session, on the `scoped-rbac-*` routes.
scoped-rbac's ninja router alone sends none. Pinned by
`tests/test_front_end_parity.py::test_role_routes_vary_alike`.

<a id="ninja-accept-parameters"></a>
## ninja-accept-parameters: `Accept` media type parameters (`indent=N`)

Ninja's negotiation ignored the parameters of the accepted media type, so
`Accept: application/json; indent=4` came back compact on ninja and indented
on DRF. Fixed in 0157baa: negotiation keeps the parameters and rendering
passes `indent` through as DRF's `JSONRenderer.get_indent` does; parity
cases cover an item, a list, `services/` and the collection documents.

<a id="core-allowed-methods-existence-leak"></a>
## core-allowed-methods-existence-leak: `core.allowed_methods` takes a loaded item

Decision: a front end given the key calls
`core.resources.allowed_methods_for`, which runs each method's request
check first and looks the item up only once one passed, then defers to
`allowed_methods` with it. A requester refused every method gets `Allow:
OPTIONS` for a missing, a hidden and an existing key alike, as on the DRF
route. The ninja type routes answer OPTIONS with their own operation over it
(`scrud_django.ninja.routers`, not scrudful's automatic OPTIONS), after
resolving the user as every other method does.

`tests/test_core_resources.py::test_allowed_methods_for_matches_the_drf_route`
pins it against the DRF route (an admin, a reader in "a", a user granted
nothing and the anonymous user; the list, a readable item, a hidden one, a
missing key and one that is no key), and
`::test_allowed_methods_for_looks_nothing_up_for_a_requester_refused_everything`
that nothing is looked up for such a requester;
`tests/test_front_end_parity.py::test_options_missing_and_existing_answer_alike`
and the `options-*` corpus cases compare both front ends.

<a id="core-options-copy-entry"></a>
## core-options-copy-entry: the ninja front end must refuse scrudful's OPTIONS copy at entry

Decision: every ninja operation passes `scrud_django.ninja.entry.entry`
first, which refuses a request rewritten from an OPTIONS
(`access.refuse_rewritten_options`: 403, 401 for the anonymous) before any
core function, as DRF's `initial()` does; scoped-rbac's URL wrapping refuses
it too. A real OPTIONS reaches only scrud's OPTIONS operations, whose
body-less copies the core builds itself (`_options_copy`).

`tests/test_ninja_api.py::test_rewritten_options_is_refused_before_the_core`
(a probe route) and `::test_rewritten_options_is_refused_on_the_real_detail_route`
(the ninja API's partner-profiles detail route, an existing item) send an
OPTIONS rewritten to a PUT by middleware and assert a 403 with no call of
`core.resources.check_request`, which the same PUT, not rewritten, reaches;
`::test_entry_itself_refuses_a_rewritten_options` asks `entry` past the URL
wrapping.

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

<a id="core-type-check-ci"></a>
## core-type-check-ci: mypy is not run in CI

`scrud_django/core/` type-checks clean (since the `TypeSpec` rework), but
nothing keeps it so: mypy is not a dependency and no CI job runs it.
Django ships no stubs here (django-stubs is not installed), so the check
ignores unresolved imports and does not follow into other packages.

Test: this command reports `Success: no issues found`, and a `lint`-stage
job should run it:

```bash
uv run --with mypy mypy scrud_django/core --ignore-missing-imports --follow-imports=silent
```

<a id="ninja-3a-rulings"></a>
## ninja-3a-rulings: R1 to R5 of the additive ninja front end (0.16.0)

Decided in `docs/superpowers/plans/2026-11-12-ninja-phase3a-front-end.md`
(the owner may overrule; each says its cost if wrong there):

- **R1. The list route is not `scrudful_list`.** `scrudful_list` 400s a bad
  `page` or `page_size`; scrud's paginator clamps `page_size` silently (below
  1 or not an integer gives 100, above 1000 gives 1000) and answers 404
  "Invalid page.". The ninja list is a described GET that pages with
  `core.lists` and validates with the core's list ETag, Last-Modified and
  precondition functions, giving DRF's answers. So the spec's list-envelope
  change (`first` and `last`, page 1's `previous` as `?page=1`) and the
  scrudful `list_etag_func(request, queryset, page)` proposal are dropped.
- **R2. Conditional create is evaluated by the core**, not by
  `scrudful_create(list_load=...)`: the ninja create holds
  `core.writes.conditional_create_lock` and evaluates
  `core.lists.evaluate_list_preconditions` as DRF's `create` does
  (`tests/test_ninja_api.py::test_conditional_create_holds_the_type_lock`).
- **R3. Not served on ninja in 0.16.0:** the UI types (`ui_resource_type_urls()`),
  the studio's computed types (`studio_computed_type_urls()`) and the
  `ui-css-files` and `ui-js-files` views. `api()` does not mount them; a later release
  moves them onto core hooks.
- **R4. OPTIONS is scrud's own operation**, not scrudful's automatic one with
  an `allowed=` hook (see `core-allowed-methods-existence-leak`).
- **R5. Links follow the serving API's namespace.** The ninja routes keep
  DRF's route names under the `NinjaAPI`'s `urls_namespace`, and
  `core.urls.absolute_reverse` reverses `<request namespace>:<name>` first when
  the request was resolved under a namespace. This is how `href`, `Location`,
  `Link`s and `services/` point at the ninja routes.

R6 (no system check for `NinjaAPI(auth=...)`: the factory refuses `auth=`) and
R7 (the slug registry waits for the release that makes ninja the default) are in the plan too.

<a id="ninja-options-and-collections"></a>
## ninja-options-and-collections: OPTIONS on type routes, and where the collection documents live

OPTIONS on a ninja type route answers DRF's 204 with `Allow` from
`core.resources.allowed_methods_for` (and the HTTP Schema Resource link), not
scrudful's automatic OPTIONS. The collection documents
(`collections-json-schema/`, `collections-json-ld/`) moved into
`scrud_django/core/collections.py` and both front ends serve them
(`tests/test_core_collections.py`, and the `options-*` cases of
`tests/test_front_end_parity.py`).
