import copy
import logging
from collections.abc import Mapping

import fastjsonschema
from django.core.exceptions import ImproperlyConfigured
from scoped_rbac import access
from scoped_rbac.permissions import drf_error

from scrud_django.core import workflow as core_workflow
from scrud_django.core.workflow import (  # noqa: F401  (moved; re-exported)
    check_policy_is_unique,
    plain_body,
)
from scrud_django.exceptions import (
    JsonSchemaInvalidError,
    JsonValidationError,
    WorkflowTransitionError,
)
from scrud_django.models import ResourceType
from scrud_django.permissions import (
    _not_checking_write,
    checks_read_content,
    content_of,
    may_read_in_context,
    reading_content,
)
from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI

logger = logging.getLogger(__name__)


class ValidatorEntry:
    def __init__(self, resource_type: ResourceType, validate: callable):
        self.type_uri = resource_type.type_uri
        self.revision = resource_type.revision
        self.etag = resource_type.schema.etag
        self.modified_at = resource_type.schema.modified_at
        self.validate = validate

    def matches(self, resource_type: ResourceType):
        return (
            self.type_uri == resource_type.type_uri
            and self.revision == resource_type.revision
            and self.etag == resource_type.schema.etag
            and self.modified_at == resource_type.schema.modified_at
        )


JSON_SCHEMA_TYPE_URIS = (
    "http://json-schema.org/draft-04/schema",
    "http://json-schema.org/draft-06/schema",
    "http://json-schema.org/draft-07/schema",
)


class RefusedRemoteRef(fastjsonschema.JsonSchemaDefinitionException):
    pass


def refuse_remote_ref(uri):
    raise RefusedRemoteRef(
        f"$ref {uri!r} is not allowed: a $ref must point into the schema itself "
        "(start with '#')."
    )


class RefuseEveryScheme(dict):
    """fastjsonschema's ``handlers``: it fetches a ``$ref`` outside the schema
    with the handler for its URI scheme, else with ``urlopen``. This mapping has
    a handler for every scheme (``http``, ``file``, none...), which refuses."""

    def __contains__(self, scheme):
        return True

    def __getitem__(self, scheme):
        return refuse_remote_ref

    def get(self, scheme, default=None):
        return refuse_remote_ref


def compile_json_schema(content):
    """Compile JSON Schema `content` with fastjsonschema, resolving ``$ref`` only
    within it. Raises ``JsonSchemaInvalidError`` if it does not compile, a
    ``$ref`` to anything else (a URL, a file, another document) included. The
    content is not changed. The validator applies no ``default``
    (``use_default=False``): it validates the data as it is, and leaves it as
    it is."""
    content = copy.deepcopy(content)
    if isinstance(content, dict):
        content.pop("$id", None)
    try:
        return fastjsonschema.compile(
            content, handlers=RefuseEveryScheme(), use_default=False
        )
    except Exception as e:
        raise JsonSchemaInvalidError(f"The JSON Schema does not compile: {e}") from e


class JsonValidator:
    """Validates a body against its type's JSON Schema. A body written to a
    JSON Schema type is itself a schema, and must compile
    (`compile_json_schema`). A stored schema that does not compile fails
    closed: every write of its type is refused with ``JsonSchemaInvalidError``
    (400).

    JSON Schema ``default`` values are not applied: the validator is compiled
    with ``use_default=False`` (`compile_json_schema`), so it validates `data`,
    the body that is stored, exactly as it was sent (a default can neither
    make it valid, for example through ``if``/``then``, nor be stored), and
    leaves it unchanged."""

    requires_context = True

    def __init__(self):
        self.validators = {}

    def __call__(self, data, context):
        if context.resource_type.type_uri in JSON_SCHEMA_TYPE_URIS:
            compile_json_schema(data)
        validate = self.validator_for(context.resource_type)
        validate(data)
        return data

    def is_json_schema(self, schema):
        return (
            schema is not None
            and schema.resource_type.type_uri in JSON_SCHEMA_TYPE_URIS
        )

    def validator_for(self, resource_type):
        schema = resource_type.schema
        if not self.is_json_schema(schema):
            return lambda r: r
        validator = self.validators.get(resource_type.type_uri, None)
        if validator is None or not validator.matches(resource_type):
            try:
                validation_func = compile_json_schema(schema.content)
            except JsonSchemaInvalidError as error:
                # The writer of a resource may not be the schema's author: the
                # reason is logged, not sent.
                logger.error(
                    "The JSON Schema of %s does not compile, so its writes are "
                    "refused: %s",
                    resource_type.type_uri,
                    error.detail,
                )

                def validate(content):
                    raise JsonSchemaInvalidError(
                        "The JSON Schema of this resource type does not compile, "
                        "so it cannot be validated."
                    )

            else:

                def validate(content):
                    try:
                        return validation_func(content)
                    except fastjsonschema.JsonSchemaException as e:
                        raise JsonValidationError(e) from e

            validator = ValidatorEntry(resource_type, validate)
            self.validators[resource_type.type_uri] = validator
        return validator.validate


def rbac_permissions_of(view):
    """The ``IsAuthorized`` (or subclass) permissions of `view` (nested ones
    included: ``ResourceViewMixin.get_rbac_permissions``), or ``[]`` for no
    view or one without them."""
    get_rbac_permissions = getattr(view, "get_rbac_permissions", None)
    return list(get_rbac_permissions()) if callable(get_rbac_permissions) else []


def narrow_of(view):
    """`scrud_django.core.workflow.workflow_check`'s ``narrow`` for `view`
    (``None``: no view): one check asking each ``IsAuthorized`` permission of
    the view (`rbac_permissions_of`, read again each time it is asked, as
    `reading_content` makes it read the content)
    ``has_context_permission(request, view, body, rbac_context)``. All must
    allow. An ``IsAuthorized`` subclass can therefore refuse a transition,
    never grant one the policy refuses: its ``obj`` here is the request body
    as the writer sent it (a check written for stored fields, such as an
    owner, would read the writer's claim)."""

    def narrow(request, body, rbac_context):
        return all(
            leaf.has_context_permission(request, view, body, rbac_context)
            for leaf in reading_content(rbac_permissions_of(view))
        )

    return (narrow,)


def view_of(context):
    """The view of the serializer `context`, or ``None``."""
    return getattr(context, "context", {}).get("view")


class WorkflowValidator:
    """Checks a create or update of a resource against the workflow policy of
    every context concerned: the DRF adapter of
    `scrud_django.core.workflow.workflow_check`, which describes the check.
    It gives the check the body as sent (`body_of`), the stored content of
    the serializer's ``instance`` (``None`` for a create), the view's
    ``IsAuthorized`` permissions as its ``narrow`` (`narrow_of`; without a
    view, a serializer used outside the views, the policy alone decides),
    and whether the stored resource is readable (`stored_resource_readable`).
    A refusal in a context without a policy is DRF's 403
    (``scoped_rbac.permissions.drf_error``).

    A ``WorkflowPolicy`` resource is itself checked when it is written
    (`validate_policy`): a policy that fails those checks, names a context that
    is not a string, or would be a second policy for its type in its context
    is a 400 and is not stored. Policies stored before those checks existed
    (or server-side) are not rechecked on write; the database system check
    reports those that fail them (``scrud_django.E005``).
    """

    requires_context = True

    def __call__(self, data, context):
        resource_type = context.resource_type
        instance = context.instance
        data = self.body_of(data, context)
        if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
            self.validate_policy(data, instance)
        request = context.context.get("request")
        readable = None
        if instance is not None:

            def readable():
                return self.stored_resource_readable(request, instance, context)

        try:
            core_workflow.workflow_check(
                request,
                type_iri=resource_type.type_uri,
                prior=instance.content if instance is not None else None,
                proposed=data,
                narrow=narrow_of(view_of(context)),
                readable=readable,
            )
        except core_workflow.WorkflowRequestRequired:
            self.request_of(context)
            raise
        except core_workflow.NoWorkflowPolicyDenied as exc:
            raise drf_error(exc, request) from exc
        return data

    @staticmethod
    def body_of(data, context):
        """What the check reads: the body as sent (the serializer's
        ``initial_data``, which is what is stored), not `data`, the copy DRF
        passes to validators, which another validator may have changed (a
        JSON Schema default filled in, for example). `data` when the
        serializer has no mapping ``initial_data``."""
        initial_data = getattr(context, "initial_data", None)
        return initial_data if isinstance(initial_data, Mapping) else data

    def request_of(self, context):
        """The request of the serializer `context`; the workflow check
        authorizes it. Raises ``ImproperlyConfigured`` without one."""
        request = getattr(context, "context", {}).get("request")
        if request is None:
            raise ImproperlyConfigured(
                "WorkflowValidator needs the request in the serializer context "
                "(context={'request': request}) to authorize workflow transitions."
            )
        return request

    def errors_hidden_unless_readable(
        self, workflow_policy, request, instance=None, context=None
    ):
        """`scrud_django.core.workflow.errors_hidden_unless_readable` for the
        stored resource `instance` (``None`` for a create), as
        `stored_resource_readable` reads it."""
        return core_workflow.errors_hidden_unless_readable(
            workflow_policy,
            request,
            instance.content if instance is not None else None,
            lambda: self.stored_resource_readable(request, instance, context),
        )

    @staticmethod
    def stored_resource_readable(request, instance, context=None):
        """Whether the requester may read the stored resource `instance` (a
        ``Resource``), as the detail route decides it tells a 403 from a 404:
        the view's ``object_readable`` (``http.GET`` in the resource's
        contexts, as each of its ``IsAuthorized`` permissions reads them from
        the content, and the object checks its list asks for a read), else
        its ``is_readable``, else ``http.GET`` on the resource's type in one
        of its contexts. Asked outside any write's checks, so a grant
        conditioned on ``scrud_workflow`` does not make it readable; no
        request is no requester to hide anything from."""
        if request is None:
            return True
        view = getattr(context, "context", {}).get("view")
        content = content_of(instance)
        with _not_checking_write():
            for name in ("object_readable", "is_readable"):
                readable = getattr(view, name, None)
                if callable(readable):
                    with checks_read_content(view):
                        return readable(request, content)
            return may_read_in_context(
                request,
                instance.resource_type.type_uri,
                content,
                instance.rbac_contexts,
            )

    def validate_policy(self, data, instance):
        """`scrud_django.core.workflow.validate_policy_write` of the
        ``WorkflowPolicy`` write `data` replacing `instance` (``None`` for a
        create)."""
        core_workflow.validate_policy_write(
            data, instance.pk if instance is not None else None
        )

    def transition_for(self, workflow_policy, data, instance, entering=False):
        """`scrud_django.core.workflow.transition_for` from the stored
        `instance` (``None`` for a create)."""
        prior = instance.content if instance is not None else None
        return core_workflow.transition_for(workflow_policy, data, prior, entering)

    def authorize(
        self, workflow_policy, transition_data, rbac_context, context, data=None
    ):
        """Raise unless the user may make `transition_data`'s transition in
        `rbac_context` (``None``: the resource is in no context)."""
        if not self.allowed(context, rbac_context, transition_data.transition, data):
            raise WorkflowTransitionError(
                workflow_policy.reference,
                transition_data.prior_state,
                transition_data.proposed_state,
                message="The user isn't authorized to perform this state transition on this resource.",
            )

    def authorize_without_policy(self, rbac_context, context, data):
        """`scrud_django.core.workflow.authorize_without_policy`, its refusal
        DRF's 403 (``PermissionDenied``)."""
        if self.allowed(context, rbac_context, None, data):
            return
        if not self.allowed(context, rbac_context, data=data, transition_check=False):
            return
        raise drf_error(access.AccessDenied(), self.request_of(context))

    def allowed(
        self, context, rbac_context, transition=None, data=None, transition_check=True
    ):
        """`scrud_django.core.workflow.allowed` for the request and type of
        the serializer `context`, narrowed by its view's ``IsAuthorized``
        permissions (`narrow_of`)."""
        return core_workflow.allowed(
            self.request_of(context),
            context.resource_type.type_uri,
            rbac_context,
            transition,
            data,
            narrow_of(view_of(context)),
            transition_check,
        )

    @staticmethod
    def allowed_on(request, permission, rbac_context, body, context):
        """`scrud_django.core.workflow.allowed_on`, narrowed by each
        ``IsAuthorized`` of the serializer `context`'s view."""
        return core_workflow.allowed_on(
            request, permission, rbac_context, body, narrow_of(view_of(context))
        )
