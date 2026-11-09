import copy
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product

from cel_expr_python import cel
from django.core.exceptions import ImproperlyConfigured
from scoped_rbac.cel import (
    InvalidExpression,
    compile_expression,
    new_environment,
)

from scrud_django.exceptions import (
    DefaultPolicyReference,
    StoredPolicyReference,
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)

CEL_ENVIRONMENT = new_environment({})

# State conditions that fail to evaluate (DEBUG), and saves that skipped the
# workflow check (``ResourceMixin.save``, WARNING).
logger = logging.getLogger("scrud_django.workflow")

WORKFLOW_POLICY_TYPE_URI = "http://api.openteams.com/json-ld/WorkflowPolicy"

# The transitions scrud-django names itself. A policy may not define a
# transition with their prefix, `RESERVED_TRANSITION_PREFIX`.
# A create, and a move into a context: no prior state.
INITIALIZE = "__INITIALIZE__"
# An update that stays in its state (unless a transition of the policy leads
# from that state to itself).
SAME_STATE = "__SAME_STATE__"
# What ``scrud_post_save``'s ``transitions`` reports for a context a write
# moves a resource out of: no proposed state, the prior state the one it
# left. No grant is asked for it (the ``WorkflowValidator`` checks a move
# out of a context as a transition from the stored content there).
LEAVE = "__LEAVE__"
RESERVED_TRANSITIONS = (INITIALIZE, SAME_STATE, LEAVE)
RESERVED_TRANSITION_PREFIX = "__"


@dataclass(frozen=True)
class TransitionData:
    """A write's transition under one policy: from `prior_state` (``None``
    for a create or a move into the context) to `proposed_state` (``None``
    for a move out of it, `LEAVE`), by `transition`, the policy's
    transition name or a reserved one (`INITIALIZE`, `SAME_STATE`,
    `LEAVE`). Immutable: the same instance may be reported to several
    receivers."""

    proposed_state: str | None
    prior_state: str | None
    transition: str


class WorkflowPolicy:
    """One workflow policy is in effect per resource type per access control
    context (see `workflow_policy_for`). A write is checked under the policy of
    every context the resource is in and every context it is moved to: the new
    state must be valid, and the transition authorized, in each of them.

    Constructing a policy checks it and raises ``ImproperlyConfigured`` for a
    policy that is not an object, ``initial_states``, ``states`` or
    ``transitions`` that are present but not objects, a state condition that is
    not a string or does not compile, a state named in both ``initial_states``
    and ``states``, a transition that is not an object
    with ``from`` and ``to`` naming defined states or whose name starts with
    ``__`` (reserved), two transitions that lead from one state to another
    (a pair of states has at most one transition), or a ``restrict_update``
    that is not a list of defined state names. These are the checks a
    ``WorkflowPolicy`` write must pass; they are not a proof that every write
    under the policy succeeds.
    """

    def __init__(self, policy, resource=None, default_for=None):
        """`policy` is the policy's content; `resource` the stored
        ``WorkflowPolicy`` resource it comes from, or ``None`` for a policy from
        settings, whose type URI is `default_for` (``None``: the content's
        ``resource_type``)."""
        if not isinstance(policy, Mapping):
            raise ImproperlyConfigured(
                f"Invalid workflow policy: it must be an object, not {policy!r}."
            )
        self.policy = policy
        self.resource = resource
        self.default_for = default_for
        self.initial_state_expressions = {}
        self.state_expressions = {}
        # from state -> to state -> transition name
        self.allowed_transitions = {}
        # state -> the transition whose "from" and "to" each name that state
        # alone: the one way to update a restrict_update state in place.
        self.in_place_transitions = {}
        self.compile_state_expressions()
        self.compile_allowed_transitions()
        self.restrict_update = self.compile_restrict_update()

    @property
    def reference(self):
        """What a workflow error names as its policy, never its content: a
        stored policy by its URL (`StoredPolicyReference`, which RBAC
        protects; the writer may not be allowed to read it), a policy from
        settings by the type it is the default for (`DefaultPolicyReference`).
        """
        if self.resource is not None:
            return StoredPolicyReference(self.resource)
        type_uri = self.default_for
        if type_uri is None and isinstance(self.policy, Mapping):
            type_uri = self.policy.get("resource_type")
        return DefaultPolicyReference(type_uri if isinstance(type_uri, str) else None)

    def validate(self, proposed_data, prior_data=None):
        if prior_data is None:
            self.validate_initial_state(proposed_data)
        else:
            self.validate_transition(proposed_data, prior_data)
        return proposed_data

    def validate_initial_state(self, data):
        """The initial state `data` is in (`initial_state`); raises
        ``WorkflowInvalidInitialStateError`` if it is in none, and
        ``WorkflowAmbiguousStateError`` if it meets the conditions of more
        than one state."""
        proposed_state = self.initial_state(data)
        if proposed_state is None:
            raise WorkflowInvalidInitialStateError(self.reference, proposed_state)
        return proposed_state

    def initial_transition(self, data):
        """The ``__INITIALIZE__`` (`INITIALIZE`) `TransitionData` of a
        create of `data`: no prior state, its initial state as the proposed
        one."""
        return TransitionData(self.validate_initial_state(data), None, INITIALIZE)

    def state(self, data):
        """The state content `data` is stored in: the one state, among the
        initial states and ``states`` (`state_expressions`), whose condition
        it meets, or ``None``. Raises ``WorkflowAmbiguousStateError`` when it
        meets the conditions of more than one (`determine_state`). A write is
        authorized into this state and the next one leaves from it."""
        return self.determine_state(data, self.state_expressions)

    def initial_state(self, data):
        """The initial state a create of `data` makes, or ``None``: the state
        `data` is stored in (`state`, which raises
        ``WorkflowAmbiguousStateError`` for content that meets the
        conditions of more than one state, initial or not) when it is one of
        the initial states."""
        state = self.state(data)
        return state if state in self.initial_state_expressions else None

    def validate_transition(self, proposed_data, prior_data):
        """Given a proposed state and a previous state determine if the proposed
        transition is allowed by this policy.
        """
        self.determine_transition(proposed_data, prior_data)

    def determine_transition(self, proposed_data, prior_data):
        """The `TransitionData` of replacing `prior_data` with `proposed_data`:
        from the state the stored content is in to the state the new content
        will be stored in (`state` of each), and the transition between them.
        Raises ``WorkflowAmbiguousStateError`` when either content meets the
        conditions of more than one state (the stored content's error says
        it is the stored resource's: such content, stored before this check
        or after a policy edit, can no longer be updated until the policy's
        conditions exclude each other), ``WorkflowInvalidStateError`` when
        `proposed_data` is in no state (and `prior_data` is in one), and
        ``WorkflowTransitionError`` when no transition leads from the prior
        state to the proposed one.
        Content that stays in its state is the ``__SAME_STATE__`` transition
        (unless a transition of the policy leads from that state to itself).
        For a state in ``restrict_update`` it is refused, unless a transition
        names that state alone as its ``from`` and its ``to``
        (`in_place_transitions`): a transition whose lists lead from the state
        to itself among other states does not update it in place.
        """
        # TODO add prior state to Resource instead
        try:
            prior_state = self.state(prior_data)
        except WorkflowAmbiguousStateError as e:
            raise WorkflowAmbiguousStateError(
                self.reference, e.matching_states, stored=True
            ) from e
        proposed_state = self.state(proposed_data)
        if prior_state is not None:
            if proposed_state is None:
                raise WorkflowInvalidStateError(self.reference)
            transition = self.allowed_transitions.get(prior_state, {}).get(
                proposed_state
            )
            if prior_state == proposed_state and proposed_state in self.restrict_update:
                transition = self.in_place_transitions.get(proposed_state)
                if transition is None:
                    raise WorkflowTransitionError(
                        self.reference,
                        prior_state,
                        proposed_state,
                        message='Update not allowed on this state',
                    )
            if transition is not None:
                return TransitionData(proposed_state, prior_state, transition)
            if prior_state == proposed_state:
                return TransitionData(proposed_state, prior_state, SAME_STATE)
        raise WorkflowTransitionError(self.reference, prior_state, proposed_state)

    def determine_state(self, data, state_expressions):
        """The one state of `state_expressions` whose condition `data`
        meets, or ``None``. Every condition is evaluated: content that meets
        more than one is ambiguous (``WorkflowAmbiguousStateError``, naming
        them), never resolved by the conditions' order, which a policy stored
        in a JSON column does not keep (PostgreSQL's ``jsonb`` and MySQL's
        JSON reorder an object's keys). Data that is not a mapping (content
        stored before 0.12.0 could be a list) is in no state: ``None``.
        """
        matches = self.matching_states(data, state_expressions)
        if len(matches) > 1:
            raise WorkflowAmbiguousStateError(self.reference, matches)
        return matches[0] if matches else None

    def matching_states(self, data, state_expressions):
        """The names of the `state_expressions` whose condition `data` (a
        mapping, else none) meets. An evaluation error, such as a property
        missing from `data`, or a result that is not ``true``, is no match
        (as scoped-rbac's ``is_true`` decides). A condition whose evaluation
        fails, or gives no boolean, is logged at DEBUG on the
        ``scrud_django.workflow`` logger, naming the policy (`reference`)
        and the state: a misspelled property fails on every content. A
        condition that is ``false`` is not logged."""
        if not isinstance(data, Mapping):
            return []
        matches = []
        for name, condition in state_expressions.items():
            result = condition.eval(data=data)
            if result.type() == cel.Type.BOOL:
                if result.value() is True:
                    matches.append(name)
                continue
            if result.type() == cel.Type.ERROR:
                outcome = f"failed to evaluate: {result.value()}"
            else:
                outcome = f"is not a boolean ({result.type()})"
            logger.debug(
                "The condition of workflow state %r of policy %r %s; it is no match.",
                name,
                self.reference,
                outcome,
            )
        return matches

    def mapping_of(self, key, what):
        """The policy's `key` (``{}`` when absent), which must be an object (of
        `what`)."""
        value = self.policy.get(key, {})
        if not isinstance(value, Mapping):
            raise ImproperlyConfigured(
                f"Invalid workflow {key}: it must be an object of {what}, not "
                f"{value!r}."
            )
        return value

    def compile_state_expressions(self):
        what = "state names to CEL conditions"
        initial_states = self.mapping_of("initial_states", what)
        for name, condition in initial_states.items():
            self.initial_state_expressions[name] = self.compile_expression(
                condition, name
            )
        self.state_expressions = dict(self.initial_state_expressions)
        states = self.mapping_of("states", what)
        for name, condition in states.items():
            if name in self.initial_state_expressions:
                # Creates would read one condition, updates the other.
                raise ImproperlyConfigured(
                    f"Invalid workflow state {name!r}: it is named in both "
                    "initial_states and states; a state has one condition."
                )
            self.state_expressions[name] = self.compile_expression(condition, name)

    def compile_expression(self, state_condition, name=None):
        if not isinstance(state_condition, str):
            raise ImproperlyConfigured(
                f"Invalid workflow state condition for {name!r}: it must be a CEL "
                f"expression (a string), not {state_condition!r}."
            )
        try:
            return compile_expression(state_condition, CEL_ENVIRONMENT)
        except InvalidExpression as e:
            raise ImproperlyConfigured(f"Invalid workflow state condition: {e}") from e

    def compile_allowed_transitions(self):
        """`allowed_transitions` (and `in_place_transitions`) from the
        policy's ``transitions``: each leads from every state of its ``from``
        to every state of its ``to``. Two transitions that lead from one
        state to another are an ``ImproperlyConfigured``: which one a write
        is (and so which grant it needs) would depend on their order."""
        transitions = self.mapping_of("transitions", "transition names to transitions")

        def as_list(x):
            return [x] if not isinstance(x, list) else x  # noqa: E731

        for transition_name, transition_policy in transitions.items():
            if not isinstance(transition_name, str) or transition_name.startswith(
                RESERVED_TRANSITION_PREFIX
            ):
                raise ImproperlyConfigured(
                    f"Invalid workflow transition {transition_name!r}: a name must "
                    f"be a string not starting with {RESERVED_TRANSITION_PREFIX!r}, "
                    f"which is reserved ({', '.join(RESERVED_TRANSITIONS)})."
                )
            if not (
                isinstance(transition_policy, Mapping)
                and "from" in transition_policy
                and "to" in transition_policy
            ):
                raise ImproperlyConfigured(
                    f"Invalid workflow transition {transition_name!r}: it must be "
                    "an object with 'from' and 'to'."
                )
            transition_from = as_list(transition_policy["from"])
            transition_to = as_list(transition_policy["to"])
            for state in transition_from + transition_to:
                if not isinstance(state, str) or state not in self.state_expressions:
                    raise ImproperlyConfigured(
                        f"Invalid workflow transition {transition_name!r}: "
                        f"{state!r} is not a defined state."
                    )
            # A state a list names twice is one pair, not a duplicate.
            sources = list(dict.fromkeys(transition_from))
            targets = list(dict.fromkeys(transition_to))
            for source, target in product(sources, targets):
                source_transitions = self.allowed_transitions.setdefault(source, {})
                other = source_transitions.get(target)
                if other is not None:
                    raise ImproperlyConfigured(
                        f"Invalid workflow transitions {other!r} and "
                        f"{transition_name!r}: both lead from {source!r} to "
                        f"{target!r}; a pair of states may have one transition."
                    )
                source_transitions[target] = transition_name
            if len(sources) == 1 and sources == targets:
                self.in_place_transitions[sources[0]] = transition_name

    def compile_restrict_update(self):
        """The states whose content may not change in place (but through
        their `in_place_transitions`): ``restrict_update``, absent or a list
        of defined state names."""
        restrict_update = self.policy.get("restrict_update", [])
        if not isinstance(restrict_update, list) or not all(
            isinstance(state, str) and state in self.state_expressions
            for state in restrict_update
        ):
            raise ImproperlyConfigured(
                "Invalid workflow restrict_update: it must be a list of defined "
                f"state names, not {restrict_update!r}."
            )
        return frozenset(restrict_update)


# How CEL's type checker names an identifier it cannot resolve.
UNDECLARED_REFERENCE = re.compile(r"undeclared reference to '([^']+)'")
CEL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _undeclared_references(condition, variables):
    """The names CEL's type checker cannot resolve in `condition`, given
    `variables` (declared as ``dyn``): top-level names, as their first
    segment, and functions. ``set()`` if it compiles."""
    from cel_expr_python import cel

    environment = cel.NewEnv(
        variables={
            name: cel.Type.DYN for name in variables if CEL_IDENTIFIER.match(name)
        }
    )
    try:
        environment.compile(condition, disable_check=False)
    except RuntimeError as e:
        return {name.split(".")[0] for name in UNDECLARED_REFERENCE.findall(str(e))}
    return set()


def undeclared_properties(condition, properties):
    """The top-level properties the CEL state `condition` reads that are not
    among `properties`, sorted. A state condition is compiled without type
    checking (`compile_expression`), so one that reads a misspelled
    property compiles, and that property is missing from every resource.
    The cel package exposes no expression's references; its type checker
    names the identifiers it cannot resolve, given the declared variables.
    A name that still does not resolve once declared as a variable is a
    function (``foo(x)``, ``x.foo()``), not a property. Other type errors
    are ignored, and a condition that does not compile reads none. A
    failure of the type checker itself reports none either, and is logged
    at DEBUG on the ``scrud_django.workflow`` logger, with its traceback."""
    try:
        missing = _undeclared_references(condition, properties)
        if not missing:
            return []
        functions = _undeclared_references(condition, set(properties) | missing)
    except Exception:  # noqa: BLE001
        # Never fail a system check on one condition; say why it reported
        # nothing.
        logger.debug(
            "The properties workflow state condition %r reads could not be "
            "told; none is reported.",
            condition,
            exc_info=True,
        )
        return []
    return sorted(missing - functions)


def _type_uri(resource_type):
    return getattr(resource_type, "type_uri", resource_type)


def default_workflow_policy_for(resource_type):
    """The settings default policy for `resource_type` (a `ResourceType` or its
    type URI), from ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``, or ``None``.

    The defaults are built and checked once (`scrud_django.conf`). An invalid
    default raises ``ImproperlyConfigured`` here, for its own type only; the
    ``scrud_django.E001`` system check reports it at ``manage.py check``."""
    from scrud_django.conf import default_workflow_policy_set

    return default_workflow_policy_set().policy_for(_type_uri(resource_type))


def stored_policies_for(resource_type, context):
    """The stored ``WorkflowPolicy`` resources for `resource_type` (a
    `ResourceType` or its type URI) whose own contexts (read with
    `rbac_contexts_of`, so a policy that names no ``rbac_context`` is in the
    default context ``""``) include `context`, oldest first. There is at most
    one, unless policies were stored past the API's check (or before it).
    `stored_policies_in` for one context."""
    return stored_policies_in(resource_type, [context])[context]


def stored_policies_in(resource_type, contexts):
    """`stored_policies_for` each of `contexts`, in one query: a dict from
    each context to its list of policies (oldest first).

    The query selects the type's policies in SQL by their `ResourceContext`
    rows (``ContextField``: compared exactly, as bytes on MySQL and MariaDB),
    so a write reads the policies of its own contexts, not every tenant's.
    Those rows are an index of the content: ``Resource.save()`` keeps them
    in step, ``QuerySet.update()``, ``bulk_create()`` and ``bulk_update()``
    do not. A policy left out by stale rows would let the writes in its
    contexts skip it, so the query also selects, by content, a policy with
    no row at all (``scrud_django.W004``), one whose ``rbac_context`` is
    one of `contexts` or a list (``scrud_django.W001``) and, when `contexts`
    has the default context, one whose ``rbac_context`` is absent or
    ``null``. What it can still miss takes a ``QuerySet.update()`` of a
    policy's ``rbac_context`` to another JSON type (a number, an object:
    the default context) while its rows name another context; ``manage.py
    scrud_sync_contexts`` repairs those rows.

    The type and contexts are compared again in Python, exactly: a list or a
    stale row selects more than its contexts, and MariaDB compares content
    values in SQL as text that pads with spaces."""
    from django.db import router
    from django.db.models import Exists, OuterRef, Q
    from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

    from scrud_django.mixins.resource_mixin import rbac_contexts_of
    from scrud_django.models import Resource, ResourceContext

    type_uri = _type_uri(resource_type)
    contexts = list(dict.fromkeys(contexts))
    if not contexts:
        return {}
    rows = ResourceContext.objects.filter(resource=OuterRef("pk"))
    in_contexts = Exists(rows.filter(rbac_context__in=contexts)) | ~Exists(rows)
    # A list (as JSON text; a string starting with "[" too). Not a
    # ``__0__isnull`` lookup: MySQL reads ``$[0]`` of a string as the string.
    in_contexts |= Q(content__rbac_context__startswith="[")
    for context in contexts:
        in_contexts |= Q(content__rbac_context=context)
    if DEFAULT_CONTEXT in contexts:
        in_contexts |= Q(content__rbac_context__isnull=True)
        in_contexts |= Q(content__rbac_context=None)
    # The write database: a write's uniqueness re-check (and the policy a
    # write is checked under) must not read a stale replica. The policy's
    # type comes with it (a refused transition links the policy by it,
    # exceptions.StoredPolicyReference), not loaded later through the read
    # router.
    stored = [
        policy
        for policy in Resource.objects.using(router.db_for_write(Resource))
        .select_related("resource_type")
        .filter(
            in_contexts,
            resource_type__type_uri=WORKFLOW_POLICY_TYPE_URI,
            content__resource_type=type_uri,
        )
        .order_by("pk")
        if isinstance(policy.content, dict)
        and policy.content.get("resource_type") == type_uri
    ]
    found = {}
    for context in contexts:
        found[context] = [
            policy for policy in stored if context in rbac_contexts_of(policy.content)
        ]
    return found


# How many compiled stored policies `workflow_policy_for` keeps
# (`STORED_POLICY_CACHE`), least recently used first out.
STORED_POLICY_CACHE_SIZE = 256


class CompiledPolicyCache:
    """Compiled stored ``WorkflowPolicy`` resources, so that a policy's CEL
    conditions are compiled once per version rather than for every context of
    every write: a bounded LRU cache of at most `maxsize` policies, keyed by
    the stored resource's pk, ETag and ``modified_at`` (every write through
    ``ResourceRegistration`` or the API gives it a new ETag). A hit whose
    content differs from the resource's (a save that kept the ETag) is
    compiled again. Safe to share between threads: the entries are read and
    changed under a lock, and a compiled policy is not changed after it is
    built (settings defaults are shared the same way). A policy that does not
    compile is not cached: each lookup raises ``ImproperlyConfigured``."""

    def __init__(self, maxsize=STORED_POLICY_CACHE_SIZE):
        self.maxsize = maxsize
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()

    def get(self, resource):
        """The `WorkflowPolicy` of the stored ``WorkflowPolicy`` `resource`:
        a copy of the cached compiled policy that names `resource` (its
        ``resource`` and ``policy`` are this lookup's)."""
        key = (resource.pk, resource.etag, resource.modified_at)
        with self._lock:
            compiled = self._entries.get(key)
            if compiled is not None and compiled.policy == resource.content:
                self._entries.move_to_end(key)
            else:
                compiled = None
        if compiled is None:
            content = copy.deepcopy(resource.content)
            try:
                compiled = WorkflowPolicy(content)
            except ImproperlyConfigured as e:
                raise ImproperlyConfigured(
                    f"Stored WorkflowPolicy pk {resource.pk} is invalid "
                    f"(scrud_django.E005): {e}"
                ) from e
            with self._lock:
                self._entries[key] = compiled
                self._entries.move_to_end(key)
                while len(self._entries) > self.maxsize:
                    self._entries.popitem(last=False)
        policy = copy.copy(compiled)
        policy.policy = resource.content
        policy.resource = resource
        return policy


STORED_POLICY_CACHE = CompiledPolicyCache()


# The attribute of a connection that says its transaction holds
# `lock_workflow_policy_type` for a block (`registration.type_locked`): the
# lock is not asked for again there.
WORKFLOW_POLICY_TYPE_HELD = "_scrud_workflow_policy_type_held"


def lock_workflow_policy_type(using):
    """Lock the ``WorkflowPolicy`` resource type's rows (``select_for_update``)
    on database `using`, inside the caller's transaction: ``WorkflowPolicy``
    writes take it before they re-check uniqueness, so they run one at a
    time. Where the database has it (PostgreSQL), the lock is ``FOR NO KEY
    UPDATE``: it still excludes the next policy writer, but not the foreign
    key check (``FOR KEY SHARE``) of a ``ResourceRemoval`` row a concurrent
    delete of a policy inserts, which ``FOR UPDATE`` blocked into a
    deadlock. (SQLite ignores the lock: one writer holds the whole database.
    Give a file database ``OPTIONS["transaction_mode"] = "IMMEDIATE"`` so
    that a second writer waits instead of failing with "database is
    locked".)"""
    from django.db import connections

    from scrud_django.models import ResourceType

    connection = connections[using]
    if getattr(connection, WORKFLOW_POLICY_TYPE_HELD, False):
        # Taken already in this transaction (`registration.type_locked`).
        return
    no_key = connection.features.has_select_for_no_key_update
    list(
        ResourceType.objects.using(using)
        .select_for_update(no_key=no_key)
        .filter(type_uri=WORKFLOW_POLICY_TYPE_URI)
        .order_by("pk")
    )


def workflow_policy_for(resource_type, context):
    """The `WorkflowPolicy` in effect for `resource_type` (a `ResourceType` or its
    type URI) in `context`, or ``None`` when the type has no workflow there.

    It is the one stored ``WorkflowPolicy`` resource for the type in
    `context` (`stored_policies_for`); a ``WorkflowPolicy`` write that would
    make a second one is refused. Without one, it is the settings default for
    the type (`default_workflow_policy_for`). A policy stored in one context
    never applies in another. Several stored policies for the type in
    `context` (stored before policies were unique, or past the API) raise
    ``ImproperlyConfigured``: none of them is picked, so the writes they
    govern fail until all but one are deleted.

    The one lookup of a policy: the `WorkflowValidator` and
    ``ResourceMixin.get_workflow_policy`` both use it, through
    `workflow_policies_for`. A stored policy is compiled once per version
    (`STORED_POLICY_CACHE`); the stored resources are read on every call.
    """
    return next(workflow_policies_for(resource_type, [context]))[1]


def workflow_policies_for(resource_type, contexts):
    """``(context, policy)`` for each of `contexts` in turn, the policy as
    `workflow_policy_for` gives it, the stored policies of all of them read
    in one query (`stored_policies_in`) when the first is asked for. A
    context's ``ImproperlyConfigured`` is raised when its turn comes, so a
    caller that stops at an earlier context does not see it."""
    contexts = list(contexts)
    stored = stored_policies_in(resource_type, contexts)
    for context in contexts:
        policies = stored[context]
        if len(policies) > 1:
            raise ImproperlyConfigured(
                f"{len(policies)} WorkflowPolicy resources (pks "
                f"{[policy.pk for policy in policies]}) apply to "
                f"{_type_uri(resource_type)!r} in context {context!r}; there must "
                "be at most one."
            )
        if policies:
            yield context, STORED_POLICY_CACHE.get(policies[0])
        else:
            yield context, default_workflow_policy_for(resource_type)
