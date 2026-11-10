"""Viewsets of the UI resource types, with their write-time checks."""

from fastjsonschema import JsonSchemaValueException
from scrudful.rest_framework import scrudful_viewset

from scrud_django.exceptions import (
    JsonValidationError,
    UIBranchContextError,
    UIBranchInvalidError,
    UIComponentNameTakenError,
    UICssInvalidError,
    UICssNameTakenError,
    UISelectorInvalidError,
    UIStylesheetNameTakenError,
    UITemplateInvalidError,
)
from scrud_django.models import Resource
from scrud_django.registration import write_db
from scrud_django.ui.branches import MAIN
from scrud_django.ui.branches.effective import (
    chain_of,
    effective_items,
    effective_rows,
)
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    applies_to,
    authorizing_context,
    branch_of,
    item_key,
    kind_of,
)
from scrud_django.ui.branches.selection import choose
from scrud_django.ui.branches.store import BranchChainError, branches, ensure_main
from scrud_django.ui.branches.sync import resolve_conflict
from scrud_django.ui.engines import TemplateCompileError, compile_template
from scrud_django.ui.renderer import negotiated_html
from scrud_django.ui.selectors import SelectorError, parse_selector
from scrud_django.views import ResourceViewSet


class UIContentValidationMixin:
    """Checks the content of a UI resource before it is saved: `validate_content`
    runs ahead of `create_resource` and `update` saving."""

    # The branch of the item being written (`validate_branch`), or None.
    item_branch = None
    # Whether the item being written keeps its override (`rebase`).
    rebased = False

    def filter_queryset(self, queryset):
        """A list shows the effective items of the selected branch (main's
        without a selection), deletion markers included; the RBAC list
        filter then applies to each row. A type that is no item kind (a
        branch, a push) lists every resource. The browsing cookie selects
        only for a list asked for as HTML: JSON is not affected by
        browsing."""
        queryset = super().filter_queryset(queryset)
        type_uri = self.get_resource_type().type_uri
        if getattr(self, "action", None) != "list" or kind_of(type_uri) is None:
            return queryset
        html = negotiated_html(self.request) is not None
        rows = effective_rows(
            type_uri,
            choose(self.request, cookie=html).selection,
            include_deleted=True,
        )
        return queryset.filter(pk__in=[pk for pk, _etag, _content in rows])

    def validate_content(self, content, instance=None):
        """Raise a ``ScrudException`` when `content` (the request body) is
        refused. `instance` is the resource an update replaces. It runs
        before the type's JSON Schema validates the body, so it must accept
        a body of any shape: what it cannot check (a value of the wrong
        type) it leaves to the schema, which refuses it (400).

        For an item (a type in ``ITEM_KINDS``) it first checks and completes
        the item's branch fields (`validate_branch`). `content` is the
        request body itself (``request.data``, the ``dict`` the JSON parser
        made), which `create_resource` and `update` then save: what this
        sets in it is stored."""
        kind = kind_of(self.get_resource_type().type_uri)
        self.rebased = False
        if kind is not None and isinstance(content, dict):
            self.item_branch = self.validate_branch(kind, content, instance)
            self.rebased = self.validate_rebase(content, instance)
            if self.rebased:
                content["base"] = self.base_of(kind, content, self.item_branch)

    def validate_branch(self, kind, content, instance=None):
        """The `Branch` the item `content` is written to, its ``branch`` (on
        an update, the stored item's when the body names none; else
        ``main``). Refuse (400) an unknown branch, a move to another branch,
        and an ``rbac_context`` other than the item's `authorizing_context`
        there. Set ``branch``, a stylesheet's ``applies_to`` (`applies_to`)
        and ``base`` in `content`: on a create, `base_of`; on an update, the
        stored one (a client's is ignored). ``None`` for a body the schema
        refuses (a ``branch``, ``rbac_context`` or ``applies_to`` that is not
        a string)."""
        stored = None if instance is None else branch_of(instance.content)
        name = content.get("branch", stored or MAIN)
        context = content.get("rbac_context", "")
        target = content.get("applies_to", "")
        if not all(isinstance(value, str) for value in (name, context, target)):
            return None
        if stored is not None and name != stored:
            raise UIBranchInvalidError(
                f"This item is on branch {stored!r}; it cannot move to {name!r}."
            )
        branch = (
            MAIN_BRANCH if name == MAIN else branches(self.write_database()).get(name)
        )
        if branch is None:
            raise UIBranchInvalidError(f"There is no UI branch named {name!r}.")
        if kind == "stylesheet":
            content["applies_to"] = applies_to(content, branch)
        expected = authorizing_context(kind, content, branch)
        if context != expected:
            raise UIBranchContextError(expected)
        content["branch"] = branch.name
        if instance is None:
            content["base"] = self.base_of(kind, content, branch)
        else:  # server-managed: only `rebase` moves it (`validate_rebase`)
            content["base"] = instance.content.get("base")
        return branch

    def validate_rebase(self, content, instance):
        """Remove the write-only ``rebase`` from `content` and say whether
        the write keeps the override as it is (``rebase: true``), which
        resolves its conflict. Refuse (400) it anywhere but on an update of
        an own item of a branch with a parent."""
        rebase = content.pop("rebase", False)
        if rebase is False:
            return False
        branch = self.item_branch
        if (
            rebase is not True
            or instance is None
            or branch is None
            or (branch.parent is None)
        ):
            raise UIBranchInvalidError(
                "rebase: true keeps an override of the parent's item: it is "
                "valid only on an update of an item of a branch with a parent."
            )
        return True

    def base_of(self, kind, content, branch):
        """The ETag of the parent's current effective item for the key of
        `content` on `branch`, else ``None`` (no parent, or none there)."""
        if branch.parent is None:
            return None
        write_db = self.write_database()
        try:
            chain = chain_of(branch.parent, branches(write_db))
        except BranchChainError as error:
            raise UIBranchInvalidError(str(error)) from error
        rows = (
            Resource.objects.using(write_db)
            .filter(resource_type=self.get_resource_type())
            .order_by("pk")
            .values_list("pk", "etag", "content")
        )
        key = item_key(kind, content, chain[0])
        for _pk, etag, stored in effective_items(kind, rows, chain):
            if item_key(kind, stored, chain[0]) == key:
                return etag
        return None

    def save_checked(self, save, instance=None):
        """Save as `ResourceViewMixin.save_checked` does; an item written to
        ``main`` while it is virtual stores ``main`` first, in the same
        transaction (`ensure_main`)."""
        if self.item_branch is None or self.item_branch.pk is not None:
            return super().save_checked(save, instance=instance)

        def save_on_main():
            ensure_main(self.write_database())
            return save()

        return super().save_checked(save_on_main, instance=instance)

    def write_database(self):
        """The database this request writes: its locked write's, else the
        write alias."""
        return self.locked_write_db or write_db()

    def create_resource(self, request):
        self.validate_content(request.data)
        return super().create_resource(request)

    def update(self, request, slug: str):
        instance = self.get_instance(request, slug)
        self.validate_content(request.data, instance=instance)
        response = super().update(request, slug)
        if self.rebased:
            self.resolve(request.data)
        return response

    def resolve(self, content):
        """Remove the conflict of the item `content` from its branch."""
        kind = kind_of(self.get_resource_type().type_uri)
        branch = branches(self.write_database()).get(branch_of(content))
        if kind is not None and branch is not None and branch.pk is not None:
            key = item_key(kind, content, branch)
            resolve_conflict(branch.name, kind, key, self.write_database())

    def destroy(self, request, slug: str):
        """Delete the item; dropping an override (its own row, not a deletion
        marker) of a branch with a parent resolves its conflict, in the same
        transaction."""
        content = self.get_instance(request, slug).content
        response = super().destroy(request, slug)
        if isinstance(content, dict) and content.get("deleted") is not True:
            self.resolve(content)
        return response

    def other_resources(self, instance=None):
        """The stored resources of this view's type, but `instance`."""
        resources = Resource.objects.using(self.write_database()).filter(
            resource_type=self.get_resource_type()
        )
        if instance is not None:
            resources = resources.exclude(pk=instance.pk)
        return resources


def require_unless_deleted(content, fields):
    """Refuse (400, as the JSON Schema's ``required`` would) an item that is
    not a deletion marker (``deleted: true``) and lacks one of `fields`."""
    if content.get("deleted") is True:
        return
    missing = [name for name in fields if name not in content]
    if missing:
        raise JsonValidationError(
            JsonSchemaValueException(
                f"data must contain {missing} properties",
                value=content,
                name="data",
                definition={"required": list(fields)},
                rule="required",
            )
        )


def same_item(others, kind, content, branch):
    """The first of `others` (stored items of `kind`) that is the item
    `content` on `branch`: on that branch, with its `item_key`."""
    key = item_key(kind, content, branch)
    for other in others:
        if (
            branch_of(other.content) == branch.name
            and item_key(kind, other.content, branch) == key
        ):
            return other
    return None


@scrudful_viewset
class UIComponentViewSet(UIContentValidationMixin, ResourceViewSet):
    def validate_content(self, content, instance=None):
        super().validate_content(content, instance=instance)
        if not isinstance(content, dict):
            return
        require_unless_deleted(content, ("engine", "mode", "template"))
        name = content.get("name")
        branch = self.item_branch
        if (
            name is not None
            and branch is not None
            and same_item(
                self.other_resources(instance).filter(content__name=name),
                "component",
                content,
                branch,
            )
        ):
            raise UIComponentNameTakenError(name)
        if "template" in content:
            try:
                compile_template(content.get("engine"), content["template"])
            except TemplateCompileError as error:
                raise UITemplateInvalidError(error) from error


@scrudful_viewset
class UIStylesheetViewSet(UIContentValidationMixin, ResourceViewSet):
    def validate_content(self, content, instance=None):
        super().validate_content(content, instance=instance)
        if not isinstance(content, dict):
            return
        require_unless_deleted(content, ("rules",))
        self.validate_selectors(content)
        name = content.get("name")
        branch = self.item_branch
        if name is None or branch is None:
            return
        if same_item(
            self.other_resources(instance).filter(content__name=name),
            "stylesheet",
            content,
            branch,
        ):
            raise UIStylesheetNameTakenError(name, content["applies_to"])

    def validate_selectors(self, content):
        """Parse every rule's selector with the stylesheet's own prefixes."""
        prefixes = content.get("prefixes")
        if not isinstance(prefixes, dict):
            prefixes = {}
        rules = content.get("rules")
        for rule in rules if isinstance(rules, list) else []:
            select = rule.get("select") if isinstance(rule, dict) else None
            if not isinstance(select, str):
                continue
            try:
                parse_selector(select, prefixes)
            except SelectorError as error:
                raise UISelectorInvalidError(error) from error


@scrudful_viewset
class UICssViewSet(UIContentValidationMixin, ResourceViewSet):
    def validate_content(self, content, instance=None):
        super().validate_content(content, instance=instance)
        if not isinstance(content, dict):
            return
        require_unless_deleted(content, ("css",))
        text = content.get("css")
        if isinstance(text, str) and "</style" in text.lower():
            raise UICssInvalidError()
        name = content.get("name")
        branch = self.item_branch
        if (
            name is not None
            and branch is not None
            and same_item(
                self.other_resources(instance).filter(content__name=name),
                "css",
                content,
                branch,
            )
        ):
            raise UICssNameTakenError(name)
