from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class OutboxEvent(models.Model):
    """One write of a type ``SCRUD_OUTBOX["TYPES"]`` lists, stored in the
    write's transaction (`scrud_django.outbox.receivers.store_event`) and
    delivered by `scrud_django.outbox.send_events`, which sets `sent_at`.

    `type_uri` is the resource type's URI (an own model's IRI), `resource_pk`
    the written object's primary key as text, `action` ``CREATE``,
    ``UPDATE`` or ``DELETE``. `checked` is the decision's (``True``: the
    workflow check ran; ``False``: the save skipped it), ``None`` for a
    delete or a save sent without a decision. `transitions` is a list, in
    the decision's order, of ``{"context", "transition", "prior_state",
    "proposed_state", "leaving", "error"}`` (``error`` the workflow error's
    message, ``transition`` and the states ``None`` with it). `user_id` is
    the actor's primary key as text, ``None`` when unknown; `content` the
    content written (``None`` for an own model's delete), `prior_content`
    the stored content before it (``None`` for a create or a delete).

    `content` and `prior_content` are written with ``DjangoJSONEncoder``, so
    that an own model's state with dates, times, decimals or UUIDs is
    recorded (as strings) rather than failing the write."""

    type_uri = models.TextField()
    resource_pk = models.CharField(max_length=255)
    action = models.CharField(max_length=6)
    checked = models.BooleanField(null=True)
    transitions = models.JSONField(default=list)
    user_id = models.CharField(max_length=255, null=True)
    content = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    prior_content = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["sent_at", "id"])]

    def __str__(self):
        return f"{self.action} {self.type_uri} {self.resource_pk}"
