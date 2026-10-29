from django.urls import NoReverseMatch, reverse
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler
from scoped_rbac.rest import exception_handler as scoped_rbac_exception_handler


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
        if request.accepted_renderer.format == "json":
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


class WorkflowTransitionError(ScrudException):
    status_code = 400
    default_detail = "Bad Request"
    default_code = "workflow_transition_invalid"
    title = "Invalid workflow transition"

    def __init__(self, policy, prior_state, proposed_state, message=None):
        """`policy` is the policy's content (a policy from settings) or a
        `StoredPolicyReference` (a stored policy), which the body gives as its
        URL. ``detail`` is `message` (else the title); the body's ``detail`` is
        the policy when there is no `message`, as it has always been."""
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
