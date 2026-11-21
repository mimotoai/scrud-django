"""`store_event`, the receiver of ``scrud_post_save`` and
``scrud_post_delete`` that ``OutboxConfig.ready()`` connects."""

import logging
from collections.abc import Mapping

from scrud_django.core.decisions import NULL_CONTENT
from scrud_django.core.signals import write_alias
from scrud_django.outbox.conf import recorded_type
from scrud_django.outbox.models import OutboxEvent

logger = logging.getLogger("scrud_django.workflow_actions")


def store_event(sender, **kwargs):
    """Store one `OutboxEvent` of the write the signal reports, when its type
    is recorded (`scrud_django.outbox.conf.recorded_type`; otherwise it
    returns before any query). It is written on the write's database (the
    signal's ``using``, `scrud_django.core.signals.write_alias`), after the
    write and in whatever transaction is open there. When the write runs in
    a transaction (every scrud front end, ``ResourceRegistration`` and
    ``ModelWorkflow`` writes do), the event commits with it, and a write
    rolled back stores none. A bare ``save()`` or ``delete()`` in autocommit
    has committed by the time the signal is sent: server code saving that
    way must wrap the save or delete in ``transaction.atomic(using=...)``,
    or an event that fails to insert leaves the write committed without it
    (follow-up ``outbox-autocommit-saves``).

    An event whose primary key is unknown (``None``) is not stored: a
    warning on the ``scrud_django.workflow_actions`` logger names it.

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
    pk = kwargs.get("pk", getattr(instance, "pk", None))
    if pk is None:
        logger.warning(
            "The outbox stores no %s event of %r: it has no primary key.",
            action,
            type_uri,
        )
        return
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
        resource_pk=str(pk),
        action=action,
        checked=checked,
        transitions=transitions,
        user_id=user_id(kwargs.get("user")),
        content=content,
        prior_content=prior_content,
    )


def user_id(user):
    """`user`'s primary key as text, or ``None`` for no user, one not
    authenticated (``AnonymousUser``) or one without a primary key."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    pk = getattr(user, "pk", None)
    return None if pk is None else str(pk)


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
