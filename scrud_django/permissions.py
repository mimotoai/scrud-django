from collections.abc import Mapping


class TransitionCheck(Mapping):
    """The workflow transition a write makes in one context, as the
    `WorkflowValidator` authorizes it.

    `transition` is its name, or ``None`` where no workflow policy applies
    (there is no transition to authorize). As a mapping it is the request body
    (read-only), so an operator other than ``scrud_workflow`` reads it as it
    reads the body in scoped-rbac's request check; ``scrud_workflow`` recognises
    it by its type."""

    __slots__ = ("_transition", "_body")

    def __init__(self, transition, body=None):
        object.__setattr__(self, "_transition", transition)
        object.__setattr__(
            self, "_body", dict(body) if isinstance(body, Mapping) else {}
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
        """What a CEL condition reads: the body, as a dict."""
        return dict(self._body)

    def __repr__(self):
        return f"TransitionCheck({self._transition!r})"


class ResourceCheck(Mapping):
    """A stored resource as an object check authorizes an action on it: the
    detail route's (DRF's object check, and whether a denied resource is
    readable), the post-save check's and a list's per-object check's.

    As a mapping it is the resource's content (read-only; ``{}`` for content
    that is not a JSON object), so an adopter's operator reads it as it reads
    the body in scoped-rbac's request check, and a CEL condition reads its
    fields (`to_cel`). Attribute reads are the resource's (``obj.content``,
    ``obj.rbac_contexts``, ``obj.pk``...), so a permission written against the
    `Resource` keeps working; `resource` is the instance itself. Attributes
    cannot be set."""

    __slots__ = ("_resource", "_content")

    def __init__(self, resource):
        content = getattr(resource, "content", None)
        object.__setattr__(self, "_resource", resource)
        object.__setattr__(
            self, "_content", dict(content) if isinstance(content, Mapping) else {}
        )

    @property
    def resource(self):
        """The `Resource` checked."""
        return self._resource

    def to_cel(self):
        """What a CEL condition reads: the content, as a dict."""
        return dict(self._content)

    def __getattr__(self, name):
        # Only called for names the class does not define: the resource's.
        if name in ResourceCheck.__slots__:
            raise AttributeError(name)
        return getattr(self._resource, name)

    def __setattr__(self, name, value):
        raise AttributeError(f"ResourceCheck is read-only; cannot set {name!r}")

    def __delattr__(self, name):
        raise AttributeError(f"ResourceCheck is read-only; cannot delete {name!r}")

    def __getitem__(self, key):
        return self._content[key]

    def __iter__(self):
        return iter(self._content)

    def __len__(self):
        return len(self._content)

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
