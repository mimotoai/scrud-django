"""The studio's writes: save or revert an item on a working branch, pull the
branch, push it. Each is a request to a resource view made in process
(`call_view`) for the studio user, so every check of the API applies (scoped
RBAC, ruling R36, R14's ``base``, versions) and the studio adds no rights.
The view's answer is passed through as it is, its errors included.

The studio refuses, before dispatching anything, a branch that is unknown or
that the user may not select (`selectable`: GET on it and its working
ancestors), alike (`UIBranchUnavailableError`), then a published one
(`UIStudioBranchReadOnlyError`): changes reach a published branch by a
push."""

from collections.abc import Mapping

from django.http import HttpResponse
from django.utils.http import quote_etag
from rest_framework.exceptions import NotFound, ValidationError

from scrud_django.exceptions import (
    UIBranchUnavailableError,
    UIStudioBranchReadOnlyError,
)
from scrud_django.models import Resource
from scrud_django.registration import write_db
from scrud_django.rows import by_pk, unsorted
from scrud_django.ui.branches import (
    ITEM_KINDS,
    MAIN,
    UI_BRANCH_SLUG,
    UI_CSS_SLUG,
    UI_PUSH_SLUG,
)
from scrud_django.ui.branches.effective import Selection
from scrud_django.ui.branches.model import (
    Branch,
    authorizing_context,
    branch_of,
    item_key,
)
from scrud_django.ui.branches.selection import (
    branch_content,
    may_get_stored,
    selectable,
)
from scrud_django.ui.branches.store import branches
from scrud_django.ui.studio.dispatch import call_view
from scrud_django.ui.types import UI_COMPONENT_SLUG, UI_STYLESHEET_SLUG

# The htmx event a successful write triggers (``HX-Trigger``).
SAVED_EVENT = "scrud-studio-saved"

# The resource type slug of each item kind.
KIND_SLUGS = {
    "component": UI_COMPONENT_SLUG,
    "stylesheet": UI_STYLESHEET_SLUG,
    "css": UI_CSS_SLUG,
}

# The headers of the resource view's answer the studio's answer keeps.
_PASSED_HEADERS = ("ETag", "Location")


def own_item(
    kind: str, branch: Branch, name: str, applies_to: str = "", using=None
) -> Resource | None:
    """`branch`'s own row (an override, an addition or a deletion marker) of
    the item of `kind` named `name` (a stylesheet: that applies to
    `applies_to`), the lowest pk's if it is stored twice; ``None`` when the
    branch shows an inherited item, or none. Read unsorted
    (`scrud_django.rows`) on `using` (default: the write database, so that
    an item just written is seen)."""
    using = using or write_db()
    rows = Resource.objects.using(using).filter(
        resource_type__type_uri=ITEM_KINDS[kind]
    )
    if branch.name != MAIN:
        rows = rows.filter(content__branch=branch.name)
    key = (name, applies_to) if kind == "stylesheet" else (name,)
    found = [
        row
        for row in unsorted(rows)
        if isinstance(row.content, Mapping)
        and branch_of(row.content) == branch.name
        and item_key(kind, row.content, branch) == key
    ]
    return by_pk(found, pk=lambda row: row.pk)[0] if found else None


def save_item(request, body) -> HttpResponse:
    """Save ``content`` as the item ``{kind, name, applies_to?}`` of the
    working ``branch``: create the branch's override (POST; the server sets
    its ``base``) when the branch inherits the item or has none, else
    update its own row (PUT, with the request's ``If-Match``). The body is
    ``content`` with the item's ``name`` and ``branch``, a stylesheet's
    ``applies_to``, and its authorizing context as ``rbac_context``."""
    kind, name, target = _item(body)
    content = body.get("content")
    if not isinstance(content, dict):
        raise ValidationError({"content": ["An object is required."]})
    branch = _working_branch(request, body.get("branch"))
    item = {**content, "name": name, "branch": branch.name}
    if kind == "stylesheet":
        item["applies_to"] = target
    item["rbac_context"] = authorizing_context(kind, item, branch)
    own = own_item(kind, branch, name, target)
    if own is None:
        return _passed(call_view(request, KIND_SLUGS[kind], "POST", data=item))
    return _passed(
        call_view(
            request,
            KIND_SLUGS[kind],
            "PUT",
            pk=own.pk,
            data=item,
            if_match=request.headers.get("If-Match"),
        )
    )


def revert_item(request, body) -> HttpResponse:
    """Delete the working ``branch``'s own row of the item ``{kind, name,
    applies_to?}`` (with the request's ``If-Match``, else the row's
    current ETag), so that the branch shows its parent's again: the undo,
    and "take theirs" for a conflict. 404 when the branch has none."""
    kind, name, target = _item(body)
    branch = _working_branch(request, body.get("branch"))
    own = own_item(kind, branch, name, target)
    if own is None:
        raise NotFound(f"Branch {branch.name!r} has no row of its own for this item.")
    if_match = request.headers.get("If-Match") or quote_etag(own.etag)
    return _passed(
        call_view(request, KIND_SLUGS[kind], "DELETE", pk=own.pk, if_match=if_match)
    )


def pull(request, body) -> HttpResponse:
    """Pull the working ``branch``: PUT it as the user reads it, with
    ``pull: true`` and its ETag then."""
    branch = _working_branch(request, body.get("branch"))
    read = call_view(request, UI_BRANCH_SLUG, "GET", pk=branch.pk)
    if read.status_code != 200:
        return _passed(read)
    return _passed(
        call_view(
            request,
            UI_BRANCH_SLUG,
            "PUT",
            pk=branch.pk,
            data={**read.data, "pull": True},
            if_match=read["ETag"],
        )
    )


def push(request, body) -> HttpResponse:
    """Push the working ``branch`` into ``target`` (default: its parent):
    create a UI push in ``applied``, in the target's context. A named
    target must be the parent, ``main`` or a branch the user may GET
    (else `UIBranchUnavailableError`, as for one that does not exist), so
    that the context the push names tells nothing of a branch the user
    may not see."""
    target = body.get("target")
    if target is not None and not isinstance(target, str):
        raise ValidationError({"target": ["A branch name is required."]})
    branch = _working_branch(request, body.get("branch"))
    target = target or branch.parent
    if target is None:
        raise ValidationError({"target": [f"Branch {branch.name!r} has no parent."]})
    found = branches(write_db()).get(target)
    if found is None or (
        target not in (branch.parent, MAIN)
        and not may_get_stored(request, branch_content(found, write_db()))
    ):
        raise UIBranchUnavailableError(target)
    return _passed(
        call_view(
            request,
            UI_PUSH_SLUG,
            "POST",
            data={
                "source": branch.name,
                "target": target,
                "restore": None,
                "state": "applied",
                "rbac_context": found.context,
            },
        )
    )


def _item(body) -> tuple[str, str, str]:
    """The ``kind``, ``name`` and ``applies_to`` (default ``""``; a
    stylesheet's only) an item write names."""
    kind = body.get("kind")
    if kind not in KIND_SLUGS:
        raise ValidationError({"kind": [f"Must be one of {', '.join(KIND_SLUGS)}."]})
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise ValidationError({"name": ["A name is required."]})
    target = body.get("applies_to")
    if target is None or kind != "stylesheet":
        target = ""
    if not isinstance(target, str):
        raise ValidationError({"applies_to": ["Must be a context."]})
    return kind, name, target


def _working_branch(request, name) -> Branch:
    """The branch named `name`, which the studio may write: refused alike
    when it is unknown or the user may not select it (`selectable`), and
    as read-only when it is published."""
    if not isinstance(name, str) or not name:
        raise ValidationError({"branch": ["A branch name is required."]})
    all_branches = branches(write_db())
    branch = all_branches.get(name)
    if branch is None or not selectable(
        request, Selection(branch=name), all_branches=all_branches
    ):
        raise UIBranchUnavailableError(name)
    if branch.published:
        raise UIStudioBranchReadOnlyError(name)
    return branch


def _passed(response) -> HttpResponse:
    """The resource view's rendered `response` as the studio's answer: its
    status, body, media type, ETag and Location, and on success the
    `SAVED_EVENT` trigger."""
    passed = HttpResponse(response.content, status=response.status_code)
    if "Content-Type" in response:
        passed["Content-Type"] = response["Content-Type"]
    else:
        del passed["Content-Type"]
    for header in _PASSED_HEADERS:
        if header in response:
            passed[header] = response[header]
    if 200 <= response.status_code < 300:
        passed["HX-Trigger"] = SAVED_EVENT
    return passed
