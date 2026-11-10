"""The viewsets of the UI branch resource type, with its write-time checks,
and of the read-only UI branch versions."""

import dataclasses
import hashlib
import re
from collections.abc import Mapping

from scrudful.rest_framework import scrudful_viewset

from scrud_django.exceptions import UIBranchInUseError, UIBranchInvalidError
from scrud_django.models import Resource
from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.ui.branches import ITEM_KINDS, MAIN
from scrud_django.ui.branches.model import MAIN_BRANCH, Branch, applies_to
from scrud_django.ui.branches.store import (
    BranchChainError,
    _read_branches,
    ancestors,
    branch_from,
    branches,
)
from scrud_django.ui.branches.sync import pull
from scrud_django.ui.branches.versions import (
    current_version,
    discard_branch,
    record_version,
)
from scrud_django.ui.views import UIContentValidationMixin
from scrud_django.views import ResourceViewSet

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PREFIX = re.compile(r"^/(?:[a-z0-9][a-z0-9_-]*/)*$")
BASES = ("live", "pinned")


@scrudful_viewset
class UIBranchViewSet(UIContentValidationMixin, ResourceViewSet):
    def validate_content(self, content, instance=None):
        """Refuse (`UIBranchInvalidError`, 400) a bad, taken or changed name,
        a reserved ``main`` that is not as reserved, a missing parent, a
        cycle of parents, a bad or taken prefix, ``pinned`` without a
        parent, and on an update a changed ``rbac_context`` or a switch
        between published and working that would change the authorizing
        context of its items (`check_unchanged_authorization`). Set ``base``
        (default ``live``) and the server-managed ``branching_point`` (on
        create, the current version of every ancestor, `current_version`;
        kept on update) in `content`, the request body that is saved. The
        write-only ``pull`` is removed; ``true`` on an update pulls
        (`pull`): the branching point moves to the ancestors' current
        versions and ``conflicts`` is recomputed. ``conflicts`` is
        server-managed: the stored one is kept, whatever the body says."""
        self.pulled = False
        super().validate_content(content, instance=instance)
        if not isinstance(content, dict):
            return
        name = content.get("name")
        parent = content.get("parent")
        prefix = content.get("prefix")
        base = content.get("base", "live")
        if not (
            isinstance(name, str)
            and isinstance(parent, (str, type(None)))
            and isinstance(prefix, (str, type(None)))
            and base in BASES
        ):
            return  # the schema refuses it
        if not NAME.fullmatch(name):
            raise UIBranchInvalidError(
                f"A branch name must match {NAME.pattern}; {name!r} does not."
            )
        if instance is not None and instance.content.get("name") != name:
            raise UIBranchInvalidError(
                "A branch cannot be renamed: its items and child branches name it."
            )
        if self.other_resources(instance).filter(content__name=name).exists():
            raise UIBranchInvalidError(f"A UI branch named {name!r} already exists.")
        if name == MAIN:
            self.check_main(content)
        all_branches = dict(branches(self.write_database()))
        self.check_parent(name, content, parent, prefix, base, all_branches)
        self.check_prefix(name, prefix, all_branches)
        if base == "pinned" and parent is None:
            raise UIBranchInvalidError(
                "A pinned branch needs a parent: it is pinned to its parent's "
                "branching point."
            )
        if instance is not None:
            self.check_unchanged_authorization(instance, content, prefix)
        content["base"] = base
        pulling = content.pop("pull", False) is True and instance is not None
        content.pop("conflicts", None)
        if instance is None:
            content["branching_point"] = self.branching_point(name, all_branches)
            return
        content["branching_point"] = instance.content.get("branching_point", {})
        if "conflicts" in instance.content:
            content["conflicts"] = instance.content["conflicts"]
        if pulling:
            content.update(pull(content, self.write_database(), all_branches))
            self.pulled = True

    def update(self, request, slug: str):
        """Update the branch; a pull also records the branch's next version,
        in the request's transaction, once the branch is saved."""
        response = super().update(request, slug)
        if self.pulled:
            record_version(request.data["name"], self.write_database())
        return response

    def branching_point(self, name, all_branches):
        """The current version of every ancestor of the new branch `name`
        (in `all_branches`, which `check_parent` gave it), by name: each
        ancestor's first is recorded now if it has none."""
        using = self.write_database()
        return {
            ancestor.name: current_version(ancestor.name, using)
            for ancestor in ancestors(name, all_branches)
        }

    def check_unchanged_authorization(self, instance, content, prefix):
        """Refuse an update that changes the branch's ``rbac_context``, or
        switches it between published (a prefix) and working (none) while
        it has its own stylesheets that apply to a context: either would
        change the context the stored items are authorized in
        (`scrud_django.ui.branches.model.authorizing_context`). A published
        branch may change its prefix."""
        stored = branch_from(instance.pk, instance.content)
        context = content.get("rbac_context", "")
        if isinstance(context, str) and context != stored.context:
            raise UIBranchInvalidError(
                f"A branch's rbac_context cannot change (it is {stored.context!r}): "
                "its items are authorized in it."
            )
        if (prefix is not None) == stored.published:
            return
        switched = dataclasses.replace(stored, prefix=prefix)
        targeted = sorted(
            {
                item.get("name")
                for item in Resource.objects.using(self.write_database())
                .filter(
                    resource_type__type_uri=ITEM_KINDS["stylesheet"],
                    content__branch=stored.name,
                )
                .values_list("content", flat=True)
                if applies_to(item, stored) or applies_to(item, switched)
            },
            key=str,
        )
        if targeted:
            raise UIBranchInvalidError(
                "A branch cannot switch between published and working while "
                "its own stylesheets apply to a context (their authorizing "
                f"context would change): {', '.join(map(repr, targeted))}."
            )

    def destroy(self, request, slug: str):
        """Delete a branch, its own items and its versions, in the request's
        transaction (`discard_branch`): a branch created again under its
        name starts afresh. ``main`` is refused (`UIBranchInvalidError`,
        400), and so is a branch that other branches name as their parent
        (`UIBranchInUseError`, 409)."""
        instance = self.get_instance(request, slug)
        content = instance.content if isinstance(instance.content, Mapping) else {}
        name = content.get("name")
        if name == MAIN:
            raise UIBranchInvalidError(
                f"The branch {MAIN!r} cannot be deleted: it is the published "
                "branch at the API URLs."
            )
        using = self.write_database()
        if isinstance(name, str):
            children = sorted(
                branch.name
                for branch in _read_branches(using).values()
                if branch.parent == name and branch.name != name
            )
            if children:
                raise UIBranchInUseError(name, children)
            discard_branch(name, using)
        return super().destroy(request, slug)

    def check_main(self, content):
        """``main`` is the published branch at the API URLs (ruling R1)."""
        if (
            content.get("rbac_context", "") != MAIN_BRANCH.context
            or content.get("prefix") != MAIN_BRANCH.prefix
            or content.get("parent") is not None
        ):
            raise UIBranchInvalidError(
                f'The branch {MAIN!r} is reserved: its rbac_context must be "", '
                'its prefix "", and it has no parent.'
            )

    def check_parent(self, name, content, parent, prefix, base, all_branches):
        """The parent exists, and the chain of parents does not loop."""
        if parent is None:
            return
        if parent not in all_branches:
            raise UIBranchInvalidError(
                f"There is no UI branch named {parent!r} to be the parent."
            )
        all_branches[name] = Branch(
            name=name,
            context=content.get("rbac_context", ""),
            parent=parent,
            base=base,
            branching_point={},
            prefix=prefix,
            pk=None,
        )
        try:
            ancestors(name, all_branches)
        except BranchChainError as error:
            raise UIBranchInvalidError(str(error)) from error

    def check_prefix(self, name, prefix, all_branches):
        """A prefix is ``""`` or ``/segment/.../``, and no other branch's
        (the virtual ``main``'s ``""`` included)."""
        if prefix is None:
            return
        if prefix and not PREFIX.fullmatch(prefix):
            raise UIBranchInvalidError(
                f'A branch prefix is "" or matches {PREFIX.pattern}; {prefix!r} '
                "does not."
            )
        for other in all_branches.values():
            if other.name != name and other.prefix == prefix:
                raise UIBranchInvalidError(
                    f"Branch {other.name!r} already has the prefix {prefix!r}."
                )


def readable_version(request, version):
    """`version` (a version `Resource`, read for `request`) as `request` may
    see it, in memory only: its ``items`` without those the requester may
    not GET live (``http.GET`` on the item's type in the item's contexts,
    as the list filter checks a stored item; ruling R9). When any is left
    out, its ``etag`` becomes one for what is left (the stored ETag and the
    positions left out), so that two requesters seeing different items get
    different ETags. Done once per instance."""
    if getattr(version, "_scrud_readable", False):
        return version
    version._scrud_readable = True
    content = version.content
    items = content.get("items") if isinstance(content, Mapping) else None
    if not isinstance(items, Mapping):
        return version
    shown, hidden = {}, []
    for kind, listed in items.items():
        type_uri = ITEM_KINDS.get(kind)
        shown[kind] = []
        for index, item in enumerate(listed if isinstance(listed, list) else []):
            if (
                type_uri is not None
                and isinstance(item, Mapping)
                and may_read_in_context(
                    request, type_uri, item, resource_contexts(item)
                )
            ):
                shown[kind].append(item)
            else:
                hidden.append(f"{kind}:{index}")
    if hidden:
        digest = hashlib.sha256("\n".join(hidden).encode("utf-8")).hexdigest()
        version.content = {**content, "items": shown}
        version.etag = f"{version.etag}-{digest[:16]}"
    return version


@scrudful_viewset
class UIBranchVersionViewSet(ResourceViewSet):
    """Branch versions are recorded by the server
    (`scrud_django.ui.branches.versions`) and never changed: a POST, PUT or
    DELETE is 405. A version is read in its branch's context, and shows only
    the items its reader may read (`readable_version`): its ETag, and the
    list's, follow what the reader gets."""

    http_method_names = ["get", "head", "options"]

    def get_object(self):
        return readable_version(self.request, super().get_object())

    def listed(self):
        """Every version the list shows (its page, else all), readable."""
        page = self.list_page()
        for version in page if page is not None else self.list_queryset():
            readable_version(self.request, version)

    def list_etag(self, request):
        self.listed()
        return super().list_etag(request)

    def list(self, request):
        self.listed()
        return super().list(request)
