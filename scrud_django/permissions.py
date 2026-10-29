import copy
from collections.abc import Mapping, Sequence


class TransitionCheck(Mapping):
    """The workflow transition a write makes in one context, as the
    `WorkflowValidator` authorizes it.

    `transition` is its name, or ``None`` where no workflow policy applies
    (there is no transition to authorize). As a mapping it is the request body
    (read-only at every level, as `ResourceCheck`: nested objects and arrays
    are read-only views, so an operator cannot change what is stored), so an
    operator other than ``scrud_workflow`` reads it as it reads the body in
    scoped-rbac's request check (`BodyCheck`); ``scrud_workflow`` recognises
    it by its type.

    The permission classes' ``has_context_permission`` gets it in the
    workflow check too, where it may refuse, never grant, a transition the
    policy refuses: its mapping is the request body, which the writer chose,
    not the stored resource."""

    __slots__ = ("_transition", "_body")

    def __init__(self, transition, body=None):
        object.__setattr__(self, "_transition", transition)
        object.__setattr__(
            self, "_body", read_only(body)._items if isinstance(body, Mapping) else {}
        )

    @property
    def transition(self):
        return self._transition

    def __setattr__(self, name, value):
        raise AttributeError(f"TransitionCheck is read-only; cannot set {name!r}")

    def __getitem__(self, key):
        return self._body[key]

    def __iter__(self):
        return iter(self._body)

    def __len__(self):
        return len(self._body)

    def to_cel(self):
        """What a CEL condition reads: the body, as a plain ``dict`` (a
        copy)."""
        return writable(self._body)

    def __repr__(self):
        return f"TransitionCheck({self._transition!r})"


class ReadOnlyMapping(Mapping):
    """A read-only view of a JSON object's members (their values read-only
    too, `read_only`). It compares equal to a ``dict`` with the same items,
    and ``copy.copy``/``copy.deepcopy`` give it back (it never changes)."""

    __slots__ = ("_items",)

    def __init__(self, items):
        object.__setattr__(self, "_items", items)

    def __getitem__(self, key):
        return self._items[key]

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __repr__(self):
        return f"{type(self).__name__}({dict(self._items)!r})"


class ReadOnlyList(Sequence):
    """A read-only view of a JSON array's items (read-only too,
    `read_only`). It compares equal to a ``list`` or ``tuple`` with the same
    items, and ``copy.copy``/``copy.deepcopy`` give it back."""

    __slots__ = ("_items",)

    def __init__(self, items):
        object.__setattr__(self, "_items", tuple(items))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return ReadOnlyList(self._items[index])
        return self._items[index]

    def __len__(self):
        return len(self._items)

    def __eq__(self, other):
        if isinstance(other, (list, tuple, ReadOnlyList)):
            return list(self._items) == list(other)
        return NotImplemented

    __hash__ = None

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __repr__(self):
        return f"{type(self).__name__}({list(self._items)!r})"


def read_only(value):
    """`value` (JSON content) as read-only views at every level: a mapping
    as a `ReadOnlyMapping`, a list or tuple as a `ReadOnlyList`, anything else
    (a string, number, boolean or ``None``) as it is."""
    if isinstance(value, Mapping):
        return ReadOnlyMapping({key: read_only(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return ReadOnlyList(read_only(item) for item in value)
    return value


def writable(value):
    """`value` with `read_only`'s views (and any mapping or sequence) back
    as plain ``dict``s and ``list``s: a fresh copy."""
    if isinstance(value, Mapping):
        return {key: writable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, ReadOnlyList)):
        return [writable(item) for item in value]
    return value


class BodyCheck(ReadOnlyMapping):
    """A request body as a check reads it: read-only at every level
    (`read_only`), so that an operator or a permission class cannot change
    what is stored, with `to_cel` a plain copy for a CEL condition.
    `ResourceViewMixin.check_permissions` gives it to scoped-rbac's request
    check as the body, and the workflow check where no transition is
    asked."""

    __slots__ = ()

    def __init__(self, body):
        super().__init__(read_only(body)._items if isinstance(body, Mapping) else {})

    def to_cel(self):
        """What a CEL condition reads: the body, as a plain ``dict`` (a
        copy)."""
        return writable(self._items)


class ResourceCheck(ReadOnlyMapping):
    """A stored resource as an object check authorizes an action on it: the
    detail route's (DRF's object check, and whether a denied resource is
    readable), the post-save check's and a list's per-object check's.

    As a mapping it is the resource's content, read-only at every level
    (`read_only`: nested objects and arrays are read-only views; ``{}`` for
    content that is not a JSON object), so an adopter's operator reads it as
    it reads the body in scoped-rbac's request check, and cannot change the
    resource through it. A CEL condition reads its fields (`to_cel`, a plain
    copy). ``content`` is a copy of the resource's content (changing it
    changes nothing); other attribute reads are the resource's
    (``obj.rbac_contexts``, ``obj.pk``...), so a permission written against
    the `Resource` keeps working, and `resource` is the instance itself.
    Attributes cannot be set. ``copy.copy``/``copy.deepcopy`` give it back;
    like a ``dict`` it is not hashable."""

    __slots__ = ("_resource",)

    def __init__(self, resource):
        content = getattr(resource, "content", None)
        object.__setattr__(self, "_resource", resource)
        super().__init__(
            read_only(content)._items if isinstance(content, Mapping) else {}
        )

    @property
    def resource(self):
        """The `Resource` checked."""
        return self._resource

    @property
    def content(self):
        """A copy of the resource's content."""
        return copy.deepcopy(self._resource.content)

    def to_cel(self):
        """What a CEL condition reads: the content, as a plain ``dict`` (a
        copy)."""
        return writable(self._items)

    def __getattr__(self, name):
        # Only called for names the class does not define: the resource's.
        if name in ("_resource", "_items"):
            raise AttributeError(name)
        return getattr(self._resource, name)

    def __setattr__(self, name, value):
        raise AttributeError(f"ResourceCheck is read-only; cannot set {name!r}")

    def __delattr__(self, name):
        raise AttributeError(f"ResourceCheck is read-only; cannot delete {name!r}")

    def __repr__(self):
        return f"ResourceCheck(pk={getattr(self._resource, 'pk', None)!r})"


def resource_check(obj):
    """`obj` as object checks get it: a `ResourceCheck` over a resource (an
    object with ``content``), `obj` itself if it already is one."""
    if isinstance(obj, ResourceCheck):
        return obj
    return ResourceCheck(obj)


def authorized_transitions(resource, configuration):
    """The ``scrud_workflow`` operator: allows a `TransitionCheck` whose
    transition is in the configuration's ``authorized_transitions``, and
    denies every other one, including the ``None`` transition the
    `WorkflowValidator` checks where no workflow policy applies, so a grant
    conditioned on it allows no write there.

    Anything else (the request body of scoped-rbac's request check, the
    stored resource of its object check) is allowed: the transition is only
    known to the `WorkflowValidator`, which authorizes it in every context of
    the write; otherwise the workflow validation would run twice.

    To use this `scoped_rbac.policy.Operator`, define an operator condition that
    specifies the `authorized_transitions` property. Here's an example: ```json {
    "condition": {"operator": "scrud_workflow", "authorized_transitions": ["start"]} }
    ```

    The name for the initial transition is `__INITIALIZE__`, and for an update
    that stays in the same state `__SAME_STATE__`.
    """
    if isinstance(resource, TransitionCheck):
        return resource.transition is not None and resource.transition in (
            configuration.get("authorized_transitions") or []
        )
    return True
