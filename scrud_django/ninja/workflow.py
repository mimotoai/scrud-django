"""The toolkit for an app's own Django models served through ninja: scrud's
workflow check, signals and workflow actions, composed with scoped-rbac's
ninja blocks.

`workflow_check` governs a model by the workflow policies of its type IRI
(stored ``WorkflowPolicy`` resources whose ``resource_type`` is the IRI, else
the settings default, as for scrud's own types) and returns a
`ModelWorkflow`, whose `ModelWorkflow.save_checked` a hand-written router's
create and update call in place of ``scoped_rbac.ninja.save_checked``, and
whose `ModelWorkflow.delete_checked` its delete calls.
"""

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import ninja
from django.db import router, transaction
from django.db.models import Model, QuerySet
from scoped_rbac import access
from scoped_rbac.ninja import save_checked as rbac_save_checked

from scrud_django.core.authz import _consume_workflow_mark
from scrud_django.core.signals import scrud_post_delete, scrud_post_save
from scrud_django.core.transitions import checked_save, govern, transitions_between
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
        transaction on the model's write database:

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
        3. ``scrud_post_save``, with the transitions from the stored state to
           ``state(saved)`` (``core.transitions.transitions_between``), whose
           workflow actions run once the write commits.

        Any failure rolls the write back and sends nothing. The request's
        workflow mark is removed however it ends. Returns what
        ``scoped_rbac.ninja.save_checked`` returns."""
        model = self.model
        db = router.db_for_write(model, instance=instance)
        try:
            with transaction.atomic(using=db):
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
                check_write(request, type_iri=self.iri, write=write)
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
                found = transitions_between(
                    self.iri,
                    prior=prior,
                    proposed=self.state(obj),
                    contexts_before=contexts_before,
                    contexts_after=self.contexts_for(obj),
                )
                scrud_post_save.send(
                    sender=model,
                    instance=obj,
                    update_fields=None,
                    using=db,
                    resource_type_uri=self.iri,
                    transition_data=found.transition_data,
                    transition_error=found.transition_error,
                    transitions=found.transitions,
                    action="CREATE" if instance is None else "UPDATE",
                )
                return obj
        finally:
            _consume_workflow_mark(request)

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
        commits. A delete runs no workflow check (follow-up
        ``delete-no-workflow-check``), as a resource's does not; the route
        authorizes it (``scoped_rbac.ninja.rbac_load``)."""
        db = router.db_for_write(self.model, instance=instance)
        pk = instance.pk
        with transaction.atomic(using=db):
            (instance.delete if delete is None else delete)()
            scrud_post_delete.send(
                sender=self.model,
                instance=instance,
                pk=pk,
                using=db,
                resource_type_uri=self.iri,
                action="DELETE",
            )


def _dumped(schema_out: type[ninja.Schema]) -> Callable[[Model], Mapping]:
    """The default ``state_of``: the JSON `schema_out` gives of an instance,
    what the API answers with."""

    def state_of(obj: Model) -> Mapping:
        return schema_out.from_orm(obj).model_dump(mode="json")

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
