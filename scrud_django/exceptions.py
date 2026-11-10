from django.urls import NoReverseMatch, reverse
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler
from scoped_rbac.rest import exception_handler as scoped_rbac_exception_handler

from scrud_django.renderers import DOCUMENT_RENDERERS


def scrud_exception_handler(exc, context):
    """Scrud errors keep their own body; everything else gets scoped-rbac's
    RFC 9457 problem details."""
    if not isinstance(exc, ScrudException):
        return scoped_rbac_exception_handler(exc, context)
    response = drf_exception_handler(exc, context)
    response.data = exc.response_data(context)
    response.data["status"] = response.status_code
    try:
        request = context["request"]
        renderer = request.accepted_renderer
        # The "json" format, or a schema or JSON-LD document's renderer
        # (formats schema-json and ld-json). Another renderer (e.g. an
        # adopter's application/vnd.api+json) keeps its media type.
        if renderer.format == "json" or isinstance(renderer, DOCUMENT_RENDERERS):
            response.content_type = "application/problem+json"
            response["Content-Type"] = "application/problem+json"
    except AttributeError:
        pass
    return response


class ScrudException(APIException):
    """A scrud error. Its ``detail`` is always an ``ErrorDetail`` (a message
    with the class's ``default_code``), as DRF's ``APIException`` makes it, so
    any exception handler (DRF's, scoped-rbac's, scrud's) can render it and read
    its code. The RFC 7807 body `scrud_exception_handler` sends is
    `response_data`, which may say more."""

    def __init__(self, message=None):
        super().__init__(detail=message)

    def response_data(self, context):
        """Return RFC 7807 response data"""
        data = {}
        for property_name in ["type", "title", "detail", "instance"]:
            if hasattr(self, property_name):
                value = getattr(self, property_name)
                data[property_name] = str(value) if isinstance(value, str) else value
        return data


class JsonValidationError(ScrudException):
    status_code = 400
    default_detail = "Bad Request"
    default_code = "json_invalid"

    def __init__(self, json_schema_exc):
        super().__init__(json_schema_exc.message)
        self.type = "https://api.openteams.com/http-problem/json-invalid"
        self.title = "JSON Validation Error"
        self.exc = json_schema_exc

    def response_data(self, context):
        data = super().response_data(context)
        data["name"] = self.exc.name
        data["value"] = self.exc.value
        data["definition"] = self.exc.definition
        data["rule"] = self.exc.rule
        data["rule_definition"] = self.exc.rule_definition
        return data


class JsonSchemaInvalidError(ScrudException):
    """A JSON Schema that does not compile: refused when it is written, and
    refusing writes of its type when it is stored (validation fails closed)."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "json_schema_invalid"

    def __init__(self, message):
        super().__init__(message)
        self.type = "https://api.openteams.com/http-problem/json-schema-invalid"
        self.title = "Invalid JSON Schema"


class RbacContextInvalidError(ScrudException):
    """A request body whose ``rbac_context`` cannot be stored: an empty list
    (which would put the resource in no context), or a context longer than
    ``scrud_django.models.resource.MAX_CONTEXT_BYTES`` bytes of UTF-8."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "rbac_context_invalid"

    def __init__(self, message):
        super().__init__(message)
        self.type = "https://api.openteams.com/http-problem/rbac-context-invalid"
        self.title = "Invalid rbac_context"


class WorkflowPolicyInvalidError(ScrudException):
    """A ``WorkflowPolicy`` write that is refused, with the reason as
    ``detail``: the policy fails ``WorkflowPolicy``'s checks (for example a
    state condition that does not compile; the ``ImproperlyConfigured``
    message), names a context that is not a string, or would be a second
    stored policy for its type in its context. The reason is about the
    policy in the request body, which its writer sent."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "workflow_policy_invalid"

    def __init__(self, message):
        super().__init__(message)
        self.type = "https://api.openteams.com/http-problem/workflow-policy-invalid"
        self.title = "Invalid workflow policy"


class UIComponentNameTakenError(ScrudException):
    """A UI component whose ``name`` another component already has: names are
    unique site-wide."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_component_name_taken"

    def __init__(self, name):
        super().__init__(f"A UI component named {name!r} already exists.")
        self.type = "https://api.openteams.com/http-problem/ui-component-name-taken"
        self.title = "UI component name taken"


class UISelectorInvalidError(ScrudException):
    """A UI stylesheet rule whose selector does not parse."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_selector_invalid"

    def __init__(self, reason):
        super().__init__(f"The stylesheet selector is invalid: {reason}")
        self.type = "https://api.openteams.com/http-problem/ui-selector-invalid"
        self.title = "UI selector invalid"


class UITemplateInvalidError(ScrudException):
    """A UI component whose template does not compile."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_template_invalid"

    def __init__(self, reason):
        super().__init__(f"The component template is invalid: {reason}")
        self.type = "https://api.openteams.com/http-problem/ui-template-invalid"
        self.title = "UI template invalid"


class UIStylesheetNameTakenError(ScrudException):
    """A UI stylesheet whose ``name`` another stylesheet in the same context
    already has."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_stylesheet_name_taken"

    def __init__(self, name, context):
        super().__init__(
            f"A UI stylesheet named {name!r} already exists in context {context!r}."
        )
        self.type = "https://api.openteams.com/http-problem/ui-stylesheet-name-taken"
        self.title = "UI stylesheet name taken"


class UICssNameTakenError(ScrudException):
    """A UI CSS item whose ``name`` another CSS item on the same branch
    already has."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_css_name_taken"

    def __init__(self, name):
        super().__init__(f"A UI CSS item named {name!r} already exists.")
        self.type = "https://api.openteams.com/http-problem/ui-css-name-taken"
        self.title = "UI CSS name taken"


class UICssInvalidError(ScrudException):
    """A UI CSS item whose text could end the page's ``<style>`` (it contains
    ``</style``)."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_css_invalid"

    def __init__(self):
        super().__init__("The CSS may not contain '</style'.")
        self.type = "https://api.openteams.com/http-problem/ui-css-invalid"
        self.title = "UI CSS invalid"


class UIJsNameTakenError(ScrudException):
    """A UI JS item whose ``name`` another JS item on the same branch already
    has."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_js_name_taken"

    def __init__(self, name):
        super().__init__(f"A UI JS item named {name!r} already exists.")
        self.type = "https://api.openteams.com/http-problem/ui-js-name-taken"
        self.title = "UI JS name taken"


class UIJsInvalidError(ScrudException):
    """A UI JS item whose text could end the page's ``<script>`` (it contains
    ``</script``)."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_js_invalid"

    def __init__(self):
        super().__init__("The JavaScript may not contain '</script'.")
        self.type = "https://api.openteams.com/http-problem/ui-js-invalid"
        self.title = "UI JS invalid"


class UIBranchInvalidError(ScrudException):
    """A UI branch write that is refused (a bad or taken name or prefix, a
    missing parent, a cycle of parents, a reserved ``main`` that is not as
    reserved), or a UI item naming a branch that does not exist, with the
    reason as ``detail``."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_branch_invalid"

    def __init__(self, reason):
        super().__init__(reason)
        self.type = "https://api.openteams.com/http-problem/ui-branch-invalid"
        self.title = "UI branch invalid"


class UIBranchContextError(ScrudException):
    """A UI item whose ``rbac_context`` is not its authorizing context on its
    branch (`scrud_django.ui.branches.model.authorizing_context`): the one
    context its write is authorized in."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_branch_context"

    def __init__(self, expected):
        super().__init__(
            f"This item's rbac_context must be {expected!r}: on its branch, "
            "its write is authorized in that context."
        )
        self.type = "https://api.openteams.com/http-problem/ui-branch-context"
        self.title = "UI item in the wrong context for its branch"


class UIBranchUnavailableError(ScrudException):
    """A JSON list of UI items whose request selects a branch (the
    ``Scrud-UI-Branch`` header or the ``ui-branch`` parameter) that is
    refused: unknown, not readable by the requester, out of the tree of the
    URL's published branch, or pinned to a missing version. One answer for
    all, so that it tells nothing about a branch the requester may not
    see."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_branch_unavailable"

    def __init__(self, branch):
        super().__init__(
            f"The UI branch {branch!r} is not available here: it does not "
            "exist, you may not read it, or it is not in this URL's tree."
        )
        self.type = "https://api.openteams.com/http-problem/ui-branch-unavailable"
        self.title = "UI branch unavailable"


class UIStudioContextUnavailableError(ScrudException):
    """A studio sample or preview in a data context whose stylesheets the
    requester may not read (GET on UI stylesheets there). One answer for
    every refused context, so that it tells nothing of any."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_studio_context_unavailable"

    def __init__(self):
        super().__init__(
            "This data context is not available: you may not read its UI stylesheets."
        )
        self.type = (
            "https://api.openteams.com/http-problem/ui-studio-context-unavailable"
        )
        self.title = "Studio data context unavailable"


class UIStudioBranchReadOnlyError(ScrudException):
    """A studio write naming a published branch (``main``, or one with a
    ``prefix``): the studio edits working branches only, so that changes
    reach a published branch by a push. Refused before anything is
    written or dispatched."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_studio_branch_read_only"

    def __init__(self, branch):
        super().__init__(
            f"UI branch {branch!r} is published: the studio does not edit it. "
            "Edit a working branch, then push it."
        )
        self.type = "https://api.openteams.com/http-problem/ui-studio-branch-read-only"
        self.title = "Studio branch read-only"


class UIBranchInUseError(ScrudException):
    """A DELETE of a UI branch that other branches name as their parent:
    409 Conflict, naming them."""

    status_code = 409
    default_detail = "Conflict"
    default_code = "ui_branch_in_use"

    def __init__(self, branch, children):
        names = ", ".join(map(repr, children))
        super().__init__(
            f"UI branch {branch!r} is the parent of {names}: delete or re-parent "
            "them first."
        )
        self.type = "https://api.openteams.com/http-problem/ui-branch-in-use"
        self.title = "UI branch in use"


class UIBranchVersionMissingError(ScrudException):
    """A version of a UI branch that is not stored
    (`scrud_django.ui.branches.versions.snapshot`)."""

    status_code = 404
    default_detail = "Not Found"
    default_code = "ui_branch_version_missing"

    def __init__(self, branch, number):
        super().__init__(f"UI branch {branch!r} has no version {number!r}.")
        self.type = "https://api.openteams.com/http-problem/ui-branch-version-missing"
        self.title = "UI branch version missing"


class UIPushConflictError(ScrudException):
    """A UI push whose source overrides items the target changed since
    (`scrud_django.ui.branches.sync.conflicts`): 409 Conflict, naming in
    ``detail`` and listing as ``conflicts`` (``{"kind", "key", "base",
    "current"}`` each) those on items the pusher may read in the target, and
    only counting the `unreadable` others (``unreadable_conflicts``).
    Nothing is written."""

    status_code = 409
    default_detail = "Conflict"
    default_code = "ui_push_conflict"

    def __init__(self, conflicts, unreadable=0):
        self.conflicts = list(conflicts)
        self.unreadable = unreadable
        names = [f"{entry['kind']} {tuple(entry['key'])!r}" for entry in self.conflicts]
        if unreadable:
            names.append(f"and {unreadable} more you may not read")
        super().__init__(
            f"The target changed these items since the source overrode them: "
            f"{', '.join(names)}. Pull the source and resolve them, then push "
            "again."
        )
        self.type = "https://api.openteams.com/http-problem/ui-push-conflict"
        self.title = "UI push conflict"

    def response_data(self, context):
        data = super().response_data(context)
        data["conflicts"] = self.conflicts
        data["unreadable_conflicts"] = self.unreadable
        return data


class UIPushForbiddenError(ScrudException):
    """A UI push or restore that would write an item into the target that
    the pusher may not write there directly (the grant on the item's type in
    its authorizing context on the target): 403 Forbidden. Nothing is
    written."""

    status_code = 403
    default_detail = "Forbidden"
    default_code = "ui_push_forbidden"

    def __init__(self, kind, key):
        super().__init__(
            f"You may not write the {kind} {tuple(key)!r} into the target "
            "branch: a push needs, for each item, the grant a direct write of "
            "it there needs."
        )
        self.type = "https://api.openteams.com/http-problem/ui-push-forbidden"
        self.title = "UI push forbidden"


class UIPushAppliedError(ScrudException):
    """A DELETE of a UI push that is applied: an applied push is the record
    of a change to its target, and is kept (409 Conflict)."""

    status_code = 409
    default_detail = "Conflict"
    default_code = "ui_push_applied"

    def __init__(self):
        super().__init__(
            "An applied UI push is a record of what it changed and cannot be deleted."
        )
        self.type = "https://api.openteams.com/http-problem/ui-push-applied"
        self.title = "UI push applied"


class UIHeaderInvalidError(ScrudException):
    """An HTML request whose fragment header (``Scrud-UI-Pointer``) is
    invalid."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_header_invalid"

    def __init__(self, header, reason):
        super().__init__(f"The {header} header is invalid: {reason}")
        self.type = "https://api.openteams.com/http-problem/ui-header-invalid"
        self.title = "UI header invalid"


class UIPointerNotFoundError(ScrudException):
    """A JSON Pointer (the studio's ``?pointer=``) that names no node a
    render of the resource has: a key or index the content or its schema
    lacks, or not a pointer."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "ui_pointer_not_found"

    def __init__(self, pointer):
        super().__init__(f"No node of this resource is at {pointer!r}.")
        self.type = "https://api.openteams.com/http-problem/ui-pointer-not-found"
        self.title = "UI pointer not found"


class RequestBodyMissingError(ScrudException):
    """A POST or PUT (or PATCH) with an empty body, or a body without a
    ``Content-Length`` (chunked), which DRF does not read. DRF reads either
    as ``{}``, which would create an empty resource or wipe the stored
    one."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "body_missing"

    def __init__(self, message=None):
        super().__init__(
            message
            if message is not None
            else "A POST or PUT needs a JSON object as its body, sent with a "
            "Content-Length header; this request has an empty body or no "
            "Content-Length."
        )
        self.type = "https://api.openteams.com/http-problem/body-missing"
        self.title = "Missing request body"


class RequestBodyTooDeepError(ScrudException):
    """A POST or PUT (or PATCH) body nested deeper than
    ``scrud_django.mixins.view_mixins.MAX_BODY_DEPTH`` levels (objects and
    arrays; ``MARIADB_MAX_BODY_DEPTH`` on MariaDB): code that walks a body
    recursively (reversion's serializer, an operator) would fail on it with
    ``RecursionError``, and MariaDB would not store it, a 500."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "body_too_deep"

    def __init__(self, max_depth):
        super().__init__(
            f"A request body may be nested at most {max_depth} levels deep "
            "(objects and arrays)."
        )
        self.type = "https://api.openteams.com/http-problem/body-too-deep"
        self.title = "Request body too deeply nested"


class RequestBodyNulError(ScrudException):
    """A POST or PUT (or PATCH) body with a NUL character (``\\u0000``) in a
    string, key or value: PostgreSQL's ``jsonb`` cannot store one (a 500
    there, stored on SQLite), so it is refused on every database."""

    status_code = 400
    default_detail = "Bad Request"
    default_code = "body_nul_character"

    def __init__(self):
        super().__init__(
            "A request body may not contain a NUL character (\\u0000) in any "
            "string, key or value."
        )
        self.type = "https://api.openteams.com/http-problem/body-nul-character"
        self.title = "NUL character in request body"


class ResourceInUseError(ScrudException):
    """A DELETE of a resource that a resource type uses as its JSON Schema,
    JSON-LD context or indexing policy (``on_delete=PROTECT``): 409 Conflict,
    naming each type and what it uses the resource as (``used_by``). Type URIs
    are public metadata (the service catalog lists them)."""

    status_code = 409
    default_detail = "Conflict"
    default_code = "resource_in_use"

    # ResourceType's protecting foreign keys, as ``used_by`` names them.
    USES = {
        "schema_id": "schema",
        "context_id": "context",
        "indexing_policy_id": "indexing_policy",
    }

    def __init__(self, resource, resource_types):
        self.used_by = sorted(
            (
                {"resource_type": resource_type.type_uri, "as": use}
                for resource_type in resource_types
                for attname, use in self.USES.items()
                if getattr(resource_type, attname, None) == resource.pk
            ),
            key=lambda use: (use["resource_type"], use["as"]),
        )
        names = ", ".join(
            f"the {use['as'].replace('_', ' ')} of {use['resource_type']}"
            for use in self.used_by
        )
        super().__init__(
            f"This resource cannot be deleted while a resource type uses it: it "
            f"is {names or 'used by a resource type'}."
        )
        self.type = "https://api.openteams.com/http-problem/resource-in-use"
        self.title = "Resource in use"

    def response_data(self, context):
        data = super().response_data(context)
        data["used_by"] = self.used_by
        return data


class StoredPolicyReference:
    """A stored ``WorkflowPolicy`` resource, as a workflow error names it: by its
    detail URL, never its content, which a writer refused because of it may not
    be allowed to read."""

    def __init__(self, resource):
        self.resource = resource

    def url(self, request=None):
        try:
            path = reverse(
                self.resource.resource_type.route_name_detail(),
                args=[self.resource.pk],
            )
        except NoReverseMatch:
            return None
        return request.build_absolute_uri(path) if request is not None else path

    def __repr__(self):
        return f"StoredPolicyReference(pk={self.resource.pk!r})"


class DefaultPolicyReference:
    """A settings default workflow policy
    (``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``), as a workflow error
    names it: by the type URI it is the default for, marked as the default,
    never by its content (its CEL conditions and transitions, some of which
    may be other roles'). The body gives it as `data`:
    ``{"resource_type": "<type URI>", "default": true}``."""

    def __init__(self, type_uri):
        self.type_uri = type_uri

    def data(self):
        return {"resource_type": self.type_uri, "default": True}

    def __eq__(self, other):
        return (
            isinstance(other, DefaultPolicyReference)
            and other.type_uri == self.type_uri
        )

    def __hash__(self):
        return hash((DefaultPolicyReference, self.type_uri))

    def __repr__(self):
        return f"DefaultPolicyReference({self.type_uri!r})"


class WorkflowTransitionError(ScrudException):
    status_code = 400
    default_detail = "Bad Request"
    default_code = "workflow_transition_invalid"
    title = "Invalid workflow transition"

    def __init__(self, policy, prior_state, proposed_state, message=None):
        """`policy` names the policy: a `StoredPolicyReference` (a stored
        policy), which the body gives as its URL, or a `DefaultPolicyReference`
        (a settings default), which the body gives as
        ``{"resource_type": ..., "default": true}``; ``None`` in a generic
        error. Any other value is given as it is. No policy the library raises
        an error for is embedded. ``detail`` is `message` (else the title);
        the body's ``detail`` is the policy reference when there is no
        `message`, as it has always been."""
        super().__init__(message if message is not None else self.title)
        self.type = "http://api.openteams.com/http-problem/workflow-transition-invalid"
        self.detail_is_policy = message is None
        self.policy = policy
        self.prior_state = prior_state
        self.proposed_state = proposed_state
        self.message = message if message is not None else self.title

    def policy_data(self, context):
        if isinstance(self.policy, StoredPolicyReference):
            request = context.get("request") if isinstance(context, dict) else None
            return self.policy.url(request)
        if isinstance(self.policy, DefaultPolicyReference):
            return self.policy.data()
        return self.policy

    def response_data(self, context):
        data = super().response_data(context)
        if self.detail_is_policy:
            data["detail"] = self.policy_data(context)
        data["policy"] = self.policy_data(context)
        data["prior_state"] = self.prior_state
        data["proposed_state"] = self.proposed_state
        data["message"] = self.message
        return data


class HiddenWorkflowTransitionError(WorkflowTransitionError):
    """A workflow error under a stored policy the requester may not read: the
    same status, ``type`` and ``title``, a generic message, and no policy or
    states."""

    MESSAGE = (
        "The request is not allowed by a workflow policy that applies to this resource."
    )

    def __init__(self):
        super().__init__(None, None, None, message=self.MESSAGE)


class WorkflowInvalidInitialStateError(WorkflowTransitionError):
    def __init__(self, policy, proposed_state):
        super().__init__(
            policy,
            None,
            proposed_state,
            message="The request is not a valid initial state.",
        )


class WorkflowInvalidStateError(WorkflowTransitionError):
    def __init__(self, policy):
        super().__init__(
            policy, None, None, message="The request does not match a valid state"
        )


class WorkflowAmbiguousStateError(WorkflowInvalidStateError):
    """Content that meets the conditions of more than one state of a policy
    (`matching_states`, sorted): its state would depend on the order of the
    conditions, which a JSON column does not keep (PostgreSQL's ``jsonb`` and
    MySQL's JSON reorder an object's keys). The request's content (or, when
    `stored`, the stored content a write replaces) is refused. Its ``type``
    is the other workflow errors' (``workflow-transition-invalid``); the
    body adds ``matching_states``. Like them it becomes a
    `HiddenWorkflowTransitionError`, which names no state, for a requester
    who may not read the policy or the stored resource."""

    def __init__(self, policy, matching_states, stored=False):
        self.matching_states = sorted(matching_states)
        names = ", ".join(repr(state) for state in self.matching_states)
        content = "The stored resource" if stored else "The request"
        WorkflowTransitionError.__init__(
            self,
            policy,
            None,
            None,
            message=(
                f"{content} meets the conditions of more than one workflow "
                f"state ({names}); a state's condition must exclude the others'."
            ),
        )
        self.stored = stored

    def response_data(self, context):
        data = super().response_data(context)
        data["matching_states"] = list(self.matching_states)
        return data
