"""The viewsets of the UI branch resource type, with its write-time checks,
of the read-only UI branch versions, and of UI pushes."""

import dataclasses
import hashlib
import re
from collections.abc import Mapping

from django.db import transaction
from rest_framework.exceptions import PermissionDenied
from scoped_rbac.policy import Permission
from scrudful.rest_framework import scrudful_viewset

from scrud_django.exceptions import (
    UIBranchContextError,
    UIBranchInUseError,
    UIBranchInvalidError,
    UIPushAppliedError,
)
from scrud_django.models import Resource
from scrud_django.permissions import (
    may_act_in_context,
    may_read_in_context,
    resource_contexts,
)
from scrud_django.registration import ResourceRegistration, type_db
from scrud_django.rows import unsorted
from scrud_django.ui.branches import ITEM_KINDS, MAIN, PREFIX, UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.checks import prefix_problems
from scrud_django.ui.branches.model import MAIN_BRANCH, Branch, applies_to
from scrud_django.ui.branches.selection import branch_content, may_get_stored
from scrud_django.ui.branches.store import (
    BranchChainError,
    _read_branches,
    ancestors,
    branch_from,
    branches,
)
from scrud_django.ui.branches.sync import (
    apply_push,
    apply_restore,
    pull,
    refuse_push_between,
)
from scrud_django.ui.branches.versions import (
    current_version,
    discard_branch,
    record_version,
)
from scrud_django.ui.views import UIContentValidationMixin
from scrud_django.views import ResourceViewSet

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BASES = ("live", "pinned")
# The field a refused prefix names (`UIBranchInvalidError.name`).
PREFIX_FIELD = "prefix"


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
        self.check_parent(name, content, parent, prefix, base, all_branches, instance)
        self.check_prefix(name, content, prefix, all_branches, instance)
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
            content["branching_point"] = (
                {} if parent is None else self.branching_point(name, all_branches)
            )
            return
        content["branching_point"] = instance.content.get("branching_point", {})
        if "conflicts" in instance.content:
            content["conflicts"] = instance.content["conflicts"]
        if pulling:
            try:
                content.update(pull(content, self.write_database(), all_branches))
            except BranchChainError as error:
                raise UIBranchInvalidError(str(error)) from error
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
                for item in unsorted(Resource.objects.using(self.write_database()))
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
        if content.get("prefix") is not None:
            # Deleting a published branch takes its URLs away, as removing
            # its prefix does: the same site-level grant.
            self.check_publisher(content)
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

    def check_parent(
        self, name, content, parent, prefix, base, all_branches, instance=None
    ):
        """The parent exists, and the chain of parents does not loop. On a
        create, and on an update that changes the parent, the writer must
        also be allowed to GET the parent as stored (ruling R36): a branch
        shows its parent's items. A parent they may not GET is refused
        exactly as an unknown one is, so the answer tells nothing about
        it."""
        if parent is None:
            return
        changed = instance is None or instance.content.get("parent") != parent
        if parent not in all_branches or (
            changed
            and not may_get_stored(
                self.request,
                branch_content(all_branches[parent], self.write_database()),
            )
        ):
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

    def check_prefix(self, name, content, prefix, all_branches, instance=None):
        """A prefix is ``""`` or ``/segment/.../``, and no other branch's
        (the virtual ``main``'s ``""`` included). Setting one, or changing
        it (to another, to none or from none), publishes URLs of the site,
        and needs PUT on branches in the context ``""`` (`check_publisher`,
        403; ruling R37); the new prefix must not lie under the API's URLs,
        hide URLs the project's URLconf serves, or overlap another branch's
        (`checks.prefix_problems`, 400)."""
        if prefix is not None and prefix and not PREFIX.fullmatch(prefix):
            raise UIBranchInvalidError(
                f'A branch prefix is "" or matches {PREFIX.pattern}; {prefix!r} '
                "does not.",
                name=PREFIX_FIELD,
            )
        stored = (
            None if instance is None else branch_from(instance.pk, instance.content)
        )
        changed = prefix != (None if stored is None else stored.prefix)
        if changed:
            self.check_publisher(content)
        if prefix is None:
            return
        for other in all_branches.values():
            if other.name != name and other.prefix == prefix:
                raise UIBranchInvalidError(
                    f"Branch {other.name!r} already has the prefix {prefix!r}.",
                    name=PREFIX_FIELD,
                )
        if changed and prefix:
            others = [b for b in all_branches.values() if b.name != name]
            problems = prefix_problems(prefix, others, type_db())
            if problems:
                raise UIBranchInvalidError(" ".join(problems), name=PREFIX_FIELD)

    def check_publisher(self, content):
        """Refuse (``PermissionDenied``, 403) unless the writer may PUT
        branches in the context ``""``: a prefix claims URLs of the whole
        site, which no grant in the branch's own context covers. Setting,
        changing or removing a prefix needs it, and so does deleting a
        published branch."""
        permission = Permission("http.PUT", UI_BRANCH_TYPE_URI)
        if not may_act_in_context(self.request, permission, content, [""]):
            raise PermissionDenied(
                "Setting, changing or removing a branch's prefix (or deleting "
                "a published branch) changes the site's URLs: it needs PUT on "
                "UI branches in the context \"\"."
            )


def readable_version(request, version):
    """`version` (a version `Resource`, read for `request`) as `request` may
    see it, in memory only: its ``items`` without those the requester may
    not GET live (``http.GET`` on the item's type in the item's contexts,
    as the list filter checks a stored item; ruling R9), and ``etags``
    without theirs. When any is left out, its ``etag`` becomes one for what
    is left (the stored ETag and the positions left out), so that two
    requesters seeing different items get different ETags. Done once per
    instance."""
    if getattr(version, "_scrud_readable", False):
        return version
    version._scrud_readable = True
    content = version.content
    items = content.get("items") if isinstance(content, Mapping) else None
    if not isinstance(items, Mapping):
        return version
    etags = content.get("etags")
    etags = etags if isinstance(etags, Mapping) else None
    shown, shown_etags, hidden = {}, {}, []
    for kind, listed in items.items():
        type_uri = ITEM_KINDS.get(kind)
        tags = etags.get(kind) if etags is not None else None
        tags = tags if isinstance(tags, list) else []
        shown[kind], shown_etags[kind] = [], []
        for index, item in enumerate(listed if isinstance(listed, list) else []):
            if (
                type_uri is not None
                and isinstance(item, Mapping)
                and may_read_in_context(
                    request, type_uri, item, resource_contexts(item)
                )
            ):
                shown[kind].append(item)
                shown_etags[kind].append(tags[index] if index < len(tags) else None)
            else:
                hidden.append(f"{kind}:{index}")
    if hidden:
        digest = hashlib.sha256("\n".join(hidden).encode("utf-8")).hexdigest()
        version.content = {**content, "items": shown}
        if etags is not None:
            version.content["etags"] = shown_etags
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


def applied_content(content) -> bool:
    """Whether push `content` is applied."""
    return isinstance(content, Mapping) and content.get("state") == "applied"


def applied(push) -> bool:
    """Whether the stored push `push` (a `Resource`) is applied."""
    return applied_content(push.content)


@scrudful_viewset
class UIPushViewSet(UIContentValidationMixin, ResourceViewSet):
    """A push writes its source branch's overrides into its target branch
    (`apply_push`), or restores version ``restore`` of its target
    (`apply_restore`), in the transaction of the write that moves it into
    ``applied`` (a create in ``applied`` included); its ``result`` is then
    the target's new version. A refused push writes nothing, the push
    included. Workflow policies on the type gate the move as on any
    resource."""

    # Whether the write being served moves the push into ``applied``.
    applying = False
    # An applied push is a record: it accepts neither a PUT nor a DELETE, so
    # its HTML page is a view (no form, no Delete button).
    is_record = staticmethod(applied_content)
    # The push as saved by the write being served (`save_checked`), and, for
    # a create, the instance the serializer created (`perform_create`), which
    # the response describes.
    saved = None
    created = None

    def validate_content(self, content, instance=None):
        """Refuse (`UIBranchInvalidError`, 400) a push that names both or
        neither of ``source`` and ``restore``, an unknown branch, a source
        that is the target or one of its ancestors (a push would write a
        branch's overrides into its own base), and (`UIBranchContextError`,
        400) an ``rbac_context`` that is not the target's context. A push
        from a branch with no parent is refused: it would delete the items
        of a root (``main``: the live site's). An applied push is a record:
        any update of it is refused (400; pushing again takes a new push).
        The server-managed ``result`` is dropped (only an applied push has
        one). Note whether the write moves the push into ``applied``."""
        super().validate_content(content, instance=instance)
        self.applying = False
        if instance is not None and applied(instance):
            raise UIBranchInvalidError(
                "This UI push is applied: it is the record of what it changed "
                "and cannot be updated. Push again with a new UI push."
            )
        if not isinstance(content, dict):
            return
        source = content.get("source")
        target = content.get("target")
        restore = content.get("restore")
        if not (
            isinstance(source, (str, type(None)))
            and isinstance(target, str)
            and (restore is None or type(restore) is int)
        ):
            return  # the schema refuses it
        if (source is None) == (restore is None):
            raise UIBranchInvalidError(
                "A push names exactly one of a source branch (source) and a "
                "version of the target to restore (restore)."
            )
        all_branches = branches(self.write_database())
        for name in (target, source):
            if name is not None and name not in all_branches:
                raise UIBranchInvalidError(f"There is no UI branch named {name!r}.")
        if source is not None and not self.may_get(all_branches[source]):
            # Applying needs GET on the source anyway: one the pusher may not
            # see is answered as unknown (ruling R36).
            raise UIBranchInvalidError(f"There is no UI branch named {source!r}.")
        if source is not None:
            refuse_push_between(source, target, all_branches)
        expected = all_branches[target].context
        if content.get("rbac_context") != expected:
            if not self.may_get(all_branches[target]):
                # Naming the context would describe a branch the pusher may
                # not see (ruling R36).
                raise UIBranchInvalidError(f"There is no UI branch named {target!r}.")
            raise UIBranchContextError(expected)
        content.pop("result", None)
        self.applying = content.get("state") == "applied"

    def may_get(self, branch) -> bool:
        """Whether the pusher may GET `branch` (``main`` always exists)."""
        return branch.name == MAIN or may_get_stored(
            self.request, branch_content(branch, self.write_database())
        )

    def destroy(self, request, slug: str):
        """Delete a push that is not applied; an applied push is kept
        (`UIPushAppliedError`, 409)."""
        if applied(self.get_instance(request, slug)):
            raise UIPushAppliedError()
        return super().destroy(request, slug)

    def save_checked(self, save, instance=None):
        self.saved = super().save_checked(save, instance=instance)
        return self.saved

    def perform_create(self, serializer):
        result = super().perform_create(serializer)
        self.created = serializer.instance
        return result

    def create_resource(self, request):
        """Create the push and, created in ``applied``, apply it, in one
        transaction (a POST runs in none of its own)."""
        with transaction.atomic(using=self.write_database()):
            return self.applied(request, super().create_resource(request))

    def update(self, request, slug: str):
        """Update the push and, moved into ``applied``, apply it, in the
        request's transaction."""
        with transaction.atomic(using=self.write_database()):
            return self.applied(request, super().update(request, slug))

    def applied(self, request, response):
        """`response` to the write that saved the push; when the write moved
        it into ``applied``, the push is applied first and its ``result``
        stored and answered."""
        if not self.applying:
            return response
        using = self.write_database()
        pushed = self.created if self.created is not None else self.saved
        content = pushed.content
        try:
            if content.get("source") is not None:
                number = apply_push(request, content, using)
            else:
                number = apply_restore(request, content, using)
        except BranchChainError as error:
            # A chain broken, or a pinned snapshot gone, since validation.
            raise UIBranchInvalidError(str(error)) from error
        ResourceRegistration.update_locked(
            pushed,
            {**content, "result": {"version": number}},
            resource_type=self.get_resource_type(),
            using=using,
        )
        response.data = self.get_serializer(instance=pushed, many=False).data
        return response
