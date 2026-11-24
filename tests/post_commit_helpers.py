"""Helpers of the post-commit tests that need no DRF
(tests/test_post_commit.py, tests/test_transition_actions.py)."""

from contextlib import contextmanager
from uuid import uuid4

from django.utils.timezone import now
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.scrud_signals import scrud_post_save

from .factories import UserFactory

TODO = "tests://ToDo"


def default_context_todo_writer_user():
    """A user who may read and write ToDos in the default context only."""
    user = UserFactory()
    definition = {
        m: [TODO] for m in ("http.GET", "http.POST", "http.PUT", "http.DELETE")
    }
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context=DEFAULT_CONTEXT,
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    return user


@contextmanager
def todos_moved_after_save(context="other"):
    """A ``scrud_post_save`` receiver that moves every saved ToDo to
    `context`: by default "other", where the writer may not write, so that
    the post-save check refuses the write with 403 and rolls it back (a
    reserved context is a 400, whoever writes)."""

    def move(sender, instance, resource_type_uri=None, **kwargs):
        if resource_type_uri == TODO:
            content = {**instance.content, "rbac_context": context}
            type(instance).objects.filter(pk=instance.pk).update(content=content)

    scrud_post_save.connect(move, weak=False, dispatch_uid="test-post-commit-move")
    try:
        yield
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-post-commit-move")
