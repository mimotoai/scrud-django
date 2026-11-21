"""`store_event`, the receiver of ``scrud_post_save`` and
``scrud_post_delete`` that ``OutboxConfig.ready()`` connects."""

from collections.abc import Mapping

from scrud_django.core.decisions import NULL_CONTENT
from scrud_django.core.signals import write_alias
from scrud_django.outbox.conf import recorded_type
from scrud_django.outbox.models import OutboxEvent


def store_event(sender, **kwargs):
    """Store one `OutboxEvent` of the write the signal reports, when its type
    is recorded (`scrud_django.outbox.conf.recorded_type`; otherwise it
    returns before any query). It is written on the write's database (the
    signal's ``using``, `scrud_django.core.signals.write_alias`), inside the
    write's transaction: the signals are sent before the write commits, so a
    write rolled back stores no event.

    A save records its ``decision``: ``checked``, ``transitions`` (a list in
    the decision's order), the decision's proposed content as ``content``
    (an own model's instance has no ``content``) and ``prior_content``. A
    save sent without a decision (by third-party code) records the
    instance's ``content`` if it has one, ``checked=None`` and no
    transitions. A delete records ``checked=None``, no transitions, and the
    instance's ``content`` (a resource's; ``None`` for an own model)."""
    type_uri = kwargs.get("resource_type_uri")
    if not recorded_type(type_uri):
        return
    instance = kwargs.get("instance")
    action = kwargs.get("action")
    user = kwargs.get("user")
    decision = kwargs.get("decision")  # a delete has none
    if decision is not None:
        checked = decision.checked
        transitions = [transition_entry(entry) for entry in decision.contexts]
        content = json_content(decision.proposed)
        prior_content = json_content(decision.prior)
    else:
        checked = None
        transitions = []
        content = json_content(getattr(instance, "content", None))
        prior_content = None
    OutboxEvent.objects.using(write_alias(sender, kwargs)).create(
        type_uri=type_uri,
        resource_pk=str(kwargs.get("pk", getattr(instance, "pk", None))),
        action=action,
        checked=checked,
        transitions=transitions,
        user_id=None if user is None else str(user.pk),
        content=content,
        prior_content=prior_content,
    )


def transition_entry(entry):
    """The JSON of a ``ContextDecision``: its context, its transition's name
    and states (``None`` without one) and leaving flag, and its error's
    message (``None`` without one)."""
    transition = entry.transition
    error = entry.error
    return {
        "context": entry.context,
        "transition": None if transition is None else transition.transition,
        "prior_state": None if transition is None else transition.prior_state,
        "proposed_state": None if transition is None else transition.proposed_state,
        "leaving": entry.leaving,
        "error": None if error is None else str(getattr(error, "message", error)),
    }


def json_content(content):
    """`content` as stored: ``None`` for `NULL_CONTENT` (stored content that
    is JSON null or not an object), a plain ``dict`` for a read-only
    mapping."""
    if content is NULL_CONTENT:
        return None
    if isinstance(content, Mapping) and not isinstance(content, dict):
        return dict(content)
    return content
