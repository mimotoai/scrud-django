"""The toolkit for an app's own Django models served through ninja: scrud's
workflow check, signals and workflow actions, composed with scoped-rbac's
ninja blocks.

`workflow_check` governs a model by the workflow policies of its type IRI
(stored ``WorkflowPolicy`` resources whose ``resource_type`` is the IRI, else
the settings default, as for scrud's own types) and returns a
`ModelWorkflow`, whose `ModelWorkflow.save_checked` a hand-written router's
create and update call in place of ``scoped_rbac.ninja.save_checked``, and
whose `ModelWorkflow.delete_checked` its delete calls. The router's request
and object checks run inside `ModelWorkflow.covering` (scoped-rbac's hooks
wrapped with `ModelWorkflow.covered`), so that a grant conditioned on the
``scrud_workflow`` operator allows the writes the workflow check covers, as
on scrud's resource routes.
"""

import copy
import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import ninja
import pydantic
from django.db import router, transaction
from django.db.models import Model, QuerySet
from scoped_rbac import access
from scoped_rbac.ninja import save_checked as rbac_save_checked

from scrud_django.core.authz import _consume_workflow_mark, request_actor
from scrud_django.core.decisions import same_transitions
from scrud_django.core.resources import COVERED_ITEM_METHOD, COVERED_LIST_METHOD
from scrud_django.core.signals import (
    acting_as,
    acting_user,
    scrud_post_delete,
    scrud_post_save,
)
from scrud_django.core.transitions import (
    checked_save,
    govern,
    log_overrides,
    log_skipped_check,
    transitions_between,
)
from scrud_django.core.workflow import Create, Update, authorizing_write
from scrud_django.core.workflow import workflow_check as check_write

__all__ = ["ModelWorkflow", "workflow_check"]


@dataclass(frozen=True)
class ModelWorkflow:
    """The workflow of `model`, governed by the policies of the type `iri`:
    the content they read of an instance is ``state_of(instance)`` (`state`),
    its contexts ``contexts_for(instance)``. Built by `workflow_check`."""

    iri: str
    model: type[Model]
    state_of: Callable[[Model], Mapping]
    contexts_for: Callable[[Model], Sequence[str]]

    def state(self, obj: Model) -> Mapping:
        """``state_of(obj)``: the content the workflow policies read of `obj`.
        ``TypeError``, naming the model, when it is not a ``Mapping``."""
        found = self.state_of(obj)
        if not isinstance(found, Mapping):
            raise TypeError(
                f"The workflow state_of of {self.model._meta.label} ({self.iri}) "
                f"must return a Mapping, not {type(found).__name__}."
            )
        return found

    def covering(self, request, *, item: bool):
        """The block in which a request or object check of `request` (on an
        item route with `item`, else on the list route) runs: with the
        ``scrud_workflow`` operator allowing for the writes the workflow
        check covers, a POST to the list or a PUT to an item, and refusing
        for any other method (GET, HEAD, PATCH, DELETE), as scrud's resource
        routes decide it (``core.resources``' ``COVERED_LIST_METHOD`` and
        ``COVERED_ITEM_METHOD``, which ``check_request``, ``check_object``
        and ``allowed_methods`` use). For a PUT or POST, `save_checked` then
        authorizes the transition itself. Wrap the create's
        ``authorize_write`` in it; scoped-rbac's hooks take `covered`.

        A covered check is allowed on the promise that the workflow check
        follows: wrap only the checks of a POST to the list or a PUT to an
        item whose write goes through `save_checked`. A covered route that
        saves any other way lets those ``scrud_workflow`` grants write
        unchecked."""
        covered = COVERED_ITEM_METHOD if item else COVERED_LIST_METHOD
        return authorizing_write(request, covered=request.method == covered)

    def covered(self, hook: Callable[..., Any], *, item: bool) -> Callable[..., Any]:
        """`hook` (``(request, **params)``: scoped-rbac's ``rbac_load``, or
        the ``before`` of ``item_check`` or ``list_check``) run inside
        `covering` (on an item route with `item`). The hook's attributes
        (``rbac_load``'s ``contexts_from`` and ``post_save_checked``, which
        ``item_check(load=...)`` reads) are kept. scrudful's OPTIONS checks
        each method on a copy of the request with that method, so a PUT a
        ``scrud_workflow`` grant allows is listed in ``Allow``, as on the
        resource routes. As for `covering`: wrap only the hooks of routes
        whose POST or PUT writes through `save_checked`."""

        @functools.wraps(hook)
        def run(request, *args, **kwargs):
            with self.covering(request, item=item):
                return hook(request, *args, **kwargs)

        return run

    def save_checked(
        self,
        request,
        *,
        instance: Model | None,
        values: Mapping[str, Any],
        save: Callable[[], Model],
        queryset: QuerySet | None = None,
    ) -> Model:
        """Write `values` (the validated body) by calling `save`, a create
        (`instance` ``None``) or an update of `instance` (the stored row, as
        the route loaded it), checked as a resource write is, in one
        transaction on the model's write database.

        For an update the row is first read again on that database with
        ``select_for_update()`` and its stored values set on `instance`, so
        that the check and the save start from the current row, held until
        the write commits, not from the one the route loaded before the
        transaction (a write committed meanwhile is not reverted);
        ``scoped_rbac.access.ObjectNotFound`` (404) when it was deleted
        meanwhile. Then:

        1. the workflow check (``core.workflow.workflow_check``) of a
           `Create`, or an `Update` from ``state(instance)``, of
           ``state(<instance, or a new model instance, with values set>)``:
           a ``WorkflowTransitionError`` (or a subclass) when it is invalid
           or not authorized, hidden (``HiddenWorkflowTransitionError``) for a
           requester who may not read the stored row (``http.GET`` on the
           IRI in its contexts, ``scoped_rbac.access.may_read``, the object
           check a GET of it gets);
        2. ``scoped_rbac.ninja.save_checked`` of `save` (and `queryset`, what
           the saved object is read back through), whose post-save check
           requires the request to be marked workflow-authorized for this
           write (``authorizing_write``); the save is not logged as skipping
           the workflow check (``checked_save``);
        3. ``scrud_post_save``, with the check's decision (``decision``) when
           the save wrote what it checked, else the decision recomputed from
           the stored state to ``state(saved)`` (``core.transitions.transitions_between``; in the
           settings default's context ``None`` for an object in no context,
           as the workflow check applied it), whose workflow actions run once
           the write commits. `save` must return one instance of the model
           (``TypeError`` otherwise: a list of many is scoped-rbac's, not a
           workflow write). Should it have written other than the checked
           values (a ``Model.save`` override, say), each context where the
           saved state is invalid is logged as a WARNING on the
           ``scrud_django.workflow`` logger (``log_skipped_check``), as an
           unchecked save is. A context the check overrode (a
           superuser's write) is logged as an override instead
           (``log_overrides``).

        Any failure rolls the write back and sends nothing. The request's
        workflow mark is removed however it ends. Returns what
        ``scoped_rbac.ninja.save_checked`` returns."""
        model = self.model
        db = router.db_for_write(model, instance=instance)
        try:
            # The request's user acts for the save and for what the
            # receivers of scrud_post_save save.
            with acting_as(request_actor(request)), transaction.atomic(using=db):
                if instance is not None:
                    self._lock_current(instance, db)
                applied = model() if instance is None else copy.copy(instance)
                for name, value in values.items():
                    setattr(applied, name, value)
                proposed = self.state(applied)
                contexts_after = list(self.contexts_for(applied))
                if instance is None:
                    prior = None
                    contexts_before = []
                    write = Create(proposed, contexts_after=contexts_after)
                else:
                    prior = self.state(instance)
                    contexts_before = list(self.contexts_for(instance))
                    write = Update(
                        prior,
                        proposed,
                        readable=lambda: access.may_read(
                            request, self.iri, instance, contexts_before
                        ),
                        stored_pk=instance.pk,
                        contexts_before=contexts_before,
                        contexts_after=contexts_after,
                    )
                # What the check decided; the recompute below, sent as
                # before, describes the same write (its resource and actor).
                decision = check_write(request, type_iri=self.iri, write=write)
                stored_pk = None if instance is None else instance.pk
                with (
                    authorizing_write(
                        request,
                        covered=True,
                        authorized=True,
                        type_iri=self.iri,
                        stored_pk=stored_pk,
                    ),
                    checked_save(model),
                ):
                    obj = rbac_save_checked(
                        request,
                        self.iri,
                        instance=instance,
                        save=save,
                        model=model,
                        contexts_for=self.contexts_for,
                        queryset=queryset,
                    )
                if not isinstance(obj, model):
                    # scoped-rbac takes a list for a create of many objects;
                    # a workflow write is of one.
                    raise TypeError(
                        f"save must return one {model._meta.label}, not "
                        f"{type(obj).__name__} (save_checked writes one object)."
                    )
                recomputed = transitions_between(
                    self.iri,
                    prior=prior,
                    proposed=self.state(obj),
                    contexts_before=contexts_before,
                    # In no context before or after: the settings default
                    # under None, as the workflow check applied it.
                    contexts_after=list(self.contexts_for(obj)),
                    stored_pk=decision.stored_pk,
                    actor=decision.actor,
                )
                # What the check decided when the save wrote what it
                # checked (same transitions), else what the save wrote.
                found = (
                    decision if same_transitions(decision, recomputed) else recomputed
                )
                # The checked values were valid; a `save` (or a Model.save
                # override) that wrote another state is logged, as an
                # unchecked save is (checked_save kept the receiver quiet).
                subject = f"{model._meta.label} pk {obj.pk}"
                # A context the sent decision overrode errors in the
                # recompute (it carries no override): logged as an
                # override, not as a skipped check.
                log_skipped_check(
                    {
                        context: error
                        for context, error in recomputed.errors.items()
                        if context not in found.overrides
                    },
                    subject=subject,
                    type_iri=self.iri,
                )
                log_overrides(found, subject=subject)
                scrud_post_save.send(
                    sender=model,
                    instance=obj,
                    update_fields=None,
                    using=db,
                    resource_type_uri=self.iri,
                    transition_data=found.transition_data,
                    transition_error=found.transition_error,
                    transitions=found.transitions,
                    decision=found,
                    user=found.actor,
                    prior_content=found.prior,
                    action="CREATE" if instance is None else "UPDATE",
                )
                return obj
        finally:
            _consume_workflow_mark(request)

    def _lock_current(self, instance: Model, db: str) -> None:
        """Read `instance`'s row again on `db`, locked until the transaction
        ends (``select_for_update``), and set its stored values on
        `instance`; ``access.ObjectNotFound`` when it is gone."""
        model = self.model
        try:
            current = (
                model._base_manager.using(db).select_for_update().get(pk=instance.pk)
            )
        except model.DoesNotExist:
            raise access.ObjectNotFound() from None
        for field in model._meta.concrete_fields:
            setattr(instance, field.attname, getattr(current, field.attname))

    def delete_checked(
        self,
        request,
        *,
        instance: Model,
        delete: Callable[[], object] | None = None,
    ) -> None:
        """Delete `instance` with `delete` (default ``instance.delete``), then
        send ``scrud_post_delete`` (``pk`` the one it had), in one transaction
        on the model's write database, whose workflow actions run once it
        commits. The row is first read again on that database with
        ``select_for_update()`` (its stored values set on `instance`), held
        until the delete commits, as a resource delete locks it;
        ``scoped_rbac.access.ObjectNotFound`` (404) when another request
        deleted it meanwhile, and nothing is sent. A delete runs no workflow
        check (follow-up ``delete-no-workflow-check``), as a resource's does
        not; the route authorizes it (``scoped_rbac.ninja.rbac_load``)."""
        db = router.db_for_write(self.model, instance=instance)
        pk = instance.pk
        with acting_as(request_actor(request)), transaction.atomic(using=db):
            self._lock_current(instance, db)
            (instance.delete if delete is None else delete)()
            scrud_post_delete.send(
                sender=self.model,
                instance=instance,
                pk=pk,
                using=db,
                resource_type_uri=self.iri,
                action="DELETE",
                user=acting_user(),
            )


def _dumped(schema_out: type[ninja.Schema]) -> Callable[[Model], Mapping]:
    """The default ``state_of``: the JSON `schema_out` gives of an instance,
    what the API answers with. A create's proposed state is that of an
    unsaved instance, which has no primary key yet (nor any value the
    database sets): a schema that requires one cannot give it, a
    ``TypeError`` naming the model, the schema and pydantic's first error."""

    def state_of(obj: Model) -> Mapping:
        try:
            found = schema_out.from_orm(obj)
        except pydantic.ValidationError as error:
            if not obj._state.adding:
                raise
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise TypeError(
                f"{schema_out.__name__} cannot give the workflow state of an "
                f"unsaved {type(obj)._meta.label} (a create's proposed state): "
                f"{where}: {first['msg']}. Make those fields optional in "
                f"{schema_out.__name__} (e.g. id: int | None = None), or pass "
                "state_of= to workflow_check."
            ) from error
        return found.model_dump(mode="json")

    return state_of


def workflow_check(
    iri: str,
    *,
    model: type[Model],
    schema_out: type[ninja.Schema] | None = None,
    state_of: Callable[[Model], Mapping] | None = None,
    contexts_for: Callable[[Model], Sequence[str]] | None = None,
) -> ModelWorkflow:
    """The `ModelWorkflow` of `model` (an app's own model) under the workflow
    policies of the type `iri`, which governs it
    (``core.transitions.govern``: a save outside the toolkit is logged as
    skipping the workflow check). The content the policies read of an
    instance is ``state_of(instance)``, or, with `schema_out` instead, the
    JSON the API answers with (``schema_out.from_orm(instance)
    .model_dump(mode="json")``): pass exactly one (``TypeError`` otherwise).
    A create's state is that of an unsaved instance, without a primary key:
    `schema_out`'s fields the database fills in must be optional (``id: int
    | None = None``), else the create is a ``TypeError`` saying so.
    Its contexts are ``contexts_for(instance)``, by default
    ``scoped_rbac.access.object_contexts`` (its ``rbac_contexts``, else its
    ``rbac_context``), as for scoped-rbac's blocks: pass the same
    ``contexts_for`` to both. Call it once per model, where its router is
    built (a URLconf imported twice governs it again, alike)."""
    if (schema_out is None) == (state_of is None):
        raise TypeError(
            "workflow_check takes exactly one of schema_out (the state is its "
            "JSON) or state_of."
        )
    workflow = ModelWorkflow(
        iri=iri,
        model=model,
        state_of=_dumped(schema_out) if state_of is None else state_of,
        contexts_for=access.object_contexts if contexts_for is None else contexts_for,
    )
    govern(model, iri, state_of=workflow.state, contexts_for=workflow.contexts_for)
    return workflow
