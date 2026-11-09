"""What the viewing user may do with a resource, for the render tree to offer,
and why a write was refused, for the HTML representation to show.

Both ask the same policy the API's checks ask (`scrud_django.permissions`), so
what a page offers is what a request would be allowed.
"""

from scoped_rbac import access
from scoped_rbac.policy import Decision, Permission

from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.permissions import (
    _checking_write,
    condition_resource,
    may_act_in_context,
    may_read_in_context,
)

ALLOWED = "allowed"
POSSIBLY = "possibly"
DENIED = "denied"

METHODS = ("GET", "PUT", "DELETE")


def decisions_for(request, type_uri: str, content: dict) -> dict[str, str]:
    """``{"GET", "PUT", "DELETE": "allowed" | "possibly" | "denied"}`` for the
    resource of `type_uri` whose content is `content`, read in each of its
    contexts as the object checks read it.

    A PUT the policy refuses outside a write's checks, but allows inside them,
    is ``"possibly"``: a ``scrud_workflow`` condition allows there, as the
    workflow check decides on submit whether the transition is authorized."""
    contexts = rbac_contexts_of(content)
    decisions = {}
    for method in METHODS:
        permission = Permission(f"http.{method}", type_uri)
        if may_act_in_context(request, permission, content, contexts):
            decisions[method] = ALLOWED
            continue
        decisions[method] = DENIED
        if method == "PUT":
            with _checking_write():
                if may_act_in_context(request, permission, content, contexts):
                    decisions[method] = POSSIBLY
    return decisions


def refusal_messages(request, type_uri: str, body: dict, stored: dict) -> list[str]:
    """The messages of the policy conditions that refuse a PUT of `body` over
    the resource whose content is `stored`, in order and each once; ``[]``
    unless the user may read `stored` (whoever may not must not learn what
    the conditions ask).

    The conditions are evaluated as a write's checks evaluate them (a
    ``scrud_workflow`` condition allows: the workflow check reports its own
    refusals), in each context of `body` whose decision is not ``ALLOWED``."""
    if not may_read_in_context(request, type_uri, stored, rbac_contexts_of(stored)):
        return []
    policy = access.policy_for(request)
    permission = Permission("http.PUT", type_uri)
    messages = []
    with _checking_write():
        for context in rbac_contexts_of(body):
            explanation = policy.explain(
                permission, context, condition_resource(body, context)
            )
            if explanation.decision is Decision.ALLOWED:
                continue
            for condition in explanation.conditions:
                message = condition.message
                if (
                    condition.decision is not Decision.ALLOWED
                    and message
                    and message not in messages
                ):
                    messages.append(message)
    return messages
