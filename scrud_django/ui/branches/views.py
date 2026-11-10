"""The viewset of the UI branch resource type, with its write-time checks."""

import re

from scrudful.rest_framework import scrudful_viewset

from scrud_django.exceptions import UIBranchInvalidError
from scrud_django.ui.branches import MAIN
from scrud_django.ui.branches.model import MAIN_BRANCH, Branch
from scrud_django.ui.branches.store import BranchChainError, ancestors, branches
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
        cycle of parents, a bad or taken prefix, and ``pinned`` without a
        parent. Set ``base`` (default ``live``) and the server-managed
        ``branching_point`` (``{}`` on create; kept on update) in
        `content`, the request body that is saved."""
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
        content["base"] = base
        content["branching_point"] = (
            {} if instance is None else instance.content.get("branching_point", {})
        )

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
