"""The neutral validators and workflow names, kept here as before: the JSON
Schema validation (`scrud_django.core.writes`), the workflow helpers
(`scrud_django.core.workflow`, `scrud_django.workflow`) and the errors. The
DRF validator, ``WorkflowValidator``, and its helpers moved to
`scrud_django.drf.validators`: their names here are deprecated aliases for
one release (`__getattr__`), which import DRF."""

import logging

from scoped_rbac import access  # noqa: F401  (re-exported)

from scrud_django._aliases import moved_name
from scrud_django.core.authz import (  # noqa: F401  (re-exported)
    _checking_transition,
    _checking_write,
    _mark_workflow_authorized,
    _not_checking_write,
    _reading_every_context,
    condition_resource,
    content_of,
    may_read_in_context,
)
from scrud_django.core.workflow import (  # noqa: F401  (re-exported)
    NULL_CONTENT,
    NULL_STORED_CONTENT,
    check_policy_is_unique,
    plain_body,
    prior_of,
)
from scrud_django.core.writes import (  # noqa: F401  (re-exported)
    JSON_SCHEMA_TYPE_URIS,
    JsonValidator,
    RefusedRemoteRef,
    RefuseEveryScheme,
    ValidatorEntry,
    compile_json_schema,
    refuse_remote_ref,
)
from scrud_django.exceptions import (  # noqa: F401  (re-exported)
    HiddenWorkflowTransitionError,
    JsonSchemaInvalidError,
    JsonValidationError,
    WorkflowPolicyInvalidError,
    WorkflowTransitionError,
)
from scrud_django.mixins.resource_mixin import (  # noqa: F401  (re-exported)
    rbac_contexts_of,
)
from scrud_django.models import ResourceType  # noqa: F401  (re-exported)
from scrud_django.workflow import (  # noqa: F401  (re-exported)
    WORKFLOW_POLICY_TYPE_URI,
    WorkflowPolicy,
    default_workflow_policy_for,
    stored_policies_in,
    workflow_policies_for,
)

# The logger's name is kept: operators filter on it.
logger = logging.getLogger(__name__)

#: The names that moved to `scrud_django.drf.validators`.
MOVED_TO_DRF = frozenset(
    {
        "WorkflowValidator",
        "checks_read_content",
        "drf_error",
        "narrow_of",
        "rbac_permissions_of",
        "reading_content",
        "type_hook_leaf",
        "view_of",
    }
)


def __getattr__(name):
    """A name in `MOVED_TO_DRF`: the object of `scrud_django.drf.validators`,
    with a ``DeprecationWarning`` (once)."""
    if name in MOVED_TO_DRF:
        return moved_name(globals(), name, "scrud_django.drf.validators")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
