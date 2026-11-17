"""The UI resource types' write behaviour, as the hooks of their
`TypeOptions` (`UI_TYPE_OPTIONS`, registered by
`scrud_django.ui.types.ui_resource_type_urls`), which
`scrud_django.core.writes` runs for every front end; the DRF viewsets
(`scrud_django.ui.views`, `scrud_django.ui.branches.views`) call the same
functions.

A write's ``prepare_write`` checks and completes its body before the
type's JSON Schema validates it, and notes what the hooks after it need
(`UIWrite`, on the request: `write_state`): the item's branch, whether it
keeps its override (``rebase``), pulls (a branch's ``pull``) or applies (a
push moved into ``applied``). ``around_save`` stores the virtual ``main``
first, in the save's transaction (`ensure_main`); ``after_write`` resolves
a rebased item's conflict, records a pulled branch's version, and applies
a push (whose stored and answered content then carries its ``result``).
``before_destroy`` refuses a delete (or discards a branch), and
``after_destroy`` resolves a dropped override's conflict.

Refusals are ``ScrudException``s, and ``scoped_rbac.access.AccessDenied``
(403) for a grant missing: each front end answers them as it answers its
own."""

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass

from fastjsonschema import JsonSchemaValueException
from scoped_rbac import access
from scoped_rbac.policy import Permission

from scrud_django.core.authz import content_of, may_act_in_context
from scrud_django.core.types import TypeOptions
from scrud_django.exceptions import (
    JsonValidationError,
    UIBranchContextError,
    UIBranchInUseError,
    UIBranchInvalidError,
    UIComponentNameTakenError,
    UICssInvalidError,
    UICssNameTakenError,
    UIJsInvalidError,
    UIJsNameTakenError,
    UILayerNameReservedError,
    UIPushAppliedError,
    UISelectorInvalidError,
    UIStylesheetNameTakenError,
    UITemplateInvalidError,
)
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, type_db
from scrud_django.rows import unsorted
from scrud_django.ui.branches import (
    ITEM_KINDS,
    MAIN,
    PREFIX,
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_SLUG,
    UI_CSS_SLUG,
    UI_JS_SLUG,
    UI_PUSH_SLUG,
)
from scrud_django.ui.branches.checks import prefix_problems
from scrud_django.ui.branches.effective import parent_state
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    Branch,
    applies_to,
    authorizing_context,
    branch_of,
    item_key,
    kind_of,
)
from scrud_django.ui.branches.selection import branch_content, may_get_stored
from scrud_django.ui.branches.store import (
    BranchChainError,
    _read_branches,
    ancestors,
    branch_from,
    branches,
    ensure_main,
)
from scrud_django.ui.branches.sync import (
    apply_push,
    apply_restore,
    pull,
    refuse_push_between,
    resolve_conflict,
)
from scrud_django.ui.branches.versions import (
    current_version,
    discard_branch,
    record_version,
)
from scrud_django.ui.engines import TemplateCompileError, compile_template
from scrud_django.ui.layers import layer_item, may_use_name
from scrud_django.ui.selectors import SelectorError, parse_selector
from scrud_django.ui.types import UI_COMPONENT_SLUG, UI_STYLESHEET_SLUG

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BASES = ("live", "pinned")
# The field a refused prefix names (`UIBranchInvalidError.name`).
PREFIX_FIELD = "prefix"
# The methods a UI branch version's routes serve: versions are recorded by
# the server (`scrud_django.ui.branches.versions`) and never changed.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class UIWrite:
    """What the ``prepare_write`` of a UI write found, for the hooks that
    follow it on the same request (`write_state`)."""

    # The branch of the item being written (`validate_branch`), or None.
    item_branch: Branch | None = None
    # Whether the item being written keeps its override (`rebase`).
    rebased: bool = False
    # Whether the branch being written is pulled (`pull`).
    pulled: bool = False
    # Whether the write moves the push into ``applied``.
    applying: bool = False


# The request attribute holding the `UIWrite` of its write.
STATE_ATTR = "_scrud_ui_write"


def _begin(request) -> UIWrite:
    """A new `UIWrite` for the write `request` makes, replacing any earlier
    one's."""
    state = UIWrite()
    setattr(request, STATE_ATTR, state)
    return state


def write_state(request) -> UIWrite:
    """The `UIWrite` the last ``prepare_write`` on `request` left; an empty
    one when none ran."""
    state = getattr(request, STATE_ATTR, None)
    return state if isinstance(state, UIWrite) else UIWrite()


def other_resources(resource_type, using, instance=None):
    """The stored resources of `resource_type` on `using`, but `instance`."""
    # Unsorted: callers test membership, and MySQL can't sort rows with
    # a large content (`scrud_django.rows`).
    resources = unsorted(
        Resource.objects.using(using).filter(resource_type=resource_type)
    )
    if instance is not None:
        resources = resources.exclude(pk=instance.pk)
    return resources


# Items (``ITEM_KINDS``): components, stylesheets, CSS and JS.


def prepare_item(request, resource_type, body, *, instance, using):
    """Refuse (a ``ScrudException``) `body`, the body of a write of a UI
    item (a type in ``ITEM_KINDS``; `instance` is the resource an update
    replaces), when its branch fields are refused, and complete them;
    return it. It runs before the type's JSON Schema validates the body,
    so it must accept a body of any shape: what it cannot check (a value
    of the wrong type) it leaves to the schema, which refuses it (400).

    It first checks and completes the item's branch fields
    (`validate_branch`). `body` is changed in place: what this sets in it
    is validated and stored.

    A name with a prefix (``abc:``) must be that of an installed layer's
    item of the kind (and ``applies_to``) that the item overrides
    (`layers.may_use_name`); else the write is refused
    (`UILayerNameReservedError`, 400). So ``scrud:`` always is. An update
    that keeps the item's name (and ``applies_to``) is not refused: an
    override whose layer item an upgrade removed (an orphan) stays
    editable. ``rebase: true`` keeps the override (`validate_rebase`):
    its ``base`` becomes the parent's current item's (`base_of`)."""
    state = _begin(request)
    kind = kind_of(resource_type.type_uri)
    if kind is not None and isinstance(body, dict):
        state.item_branch = validate_branch(
            request, resource_type, kind, body, instance, using
        )
        if not may_use_name(kind, body) and not keeps_key(
            kind, body, instance, state.item_branch
        ):
            raise UILayerNameReservedError(body["name"])
        state.rebased = validate_rebase(
            resource_type, body, instance, state.item_branch
        )
        if state.rebased:
            body["base"] = base_of(resource_type, kind, body, state.item_branch, using)
    return body


def validate_branch(request, resource_type, kind, content, instance, using):
    """The `Branch` the item `content` is written to, its ``branch`` (on
    an update, the stored item's when the body names none; else
    ``main``). Refuse (400) an unknown branch (and, alike, one the
    writer may not see, `may_name_branch`), a move to another branch,
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
    branch = MAIN_BRANCH if name == MAIN else branches(using).get(name)
    if branch is None or not may_name_branch(request, kind, content, branch, using):
        raise UIBranchInvalidError(f"There is no UI branch named {name!r}.")
    if kind == "stylesheet":
        content["applies_to"] = applies_to(content, branch)
    expected = authorizing_context(kind, content, branch)
    if context != expected:
        raise UIBranchContextError(expected)
    content["branch"] = branch.name
    if instance is None:
        content["base"] = base_of(resource_type, kind, content, branch, using)
    else:  # server-managed: only `rebase` moves it (`validate_rebase`)
        content["base"] = instance.content.get("base")
    return branch


def may_name_branch(request, kind, content, branch, using) -> bool:
    """Whether the writer may write the item `content` naming `branch`,
    as far as seeing the branch goes (ruling R36): they may GET it.
    Else the write is answered as for an unknown branch, which names
    nothing of it (its context included). Two writes need no GET:
    those on ``main``, which always exists, is reserved (ruling R1)
    and is written as before branches; and a stylesheet on a published
    branch that applies to a context other than the branch's, which is
    authorized in that context alone (spec, "Authorization on
    branches": whoever may style ``acme`` styles it on every published
    branch)."""
    if branch.name == MAIN:
        return True
    if kind == "stylesheet" and branch.published:
        target = applies_to(content, branch)
        if target and target != branch.context:
            return True
    return may_get_stored(request, branch_content(branch, using))


def validate_rebase(resource_type, content, instance, branch):
    """Remove the write-only ``rebase`` from `content` and say whether
    the write keeps the override as it is (``rebase: true``), which
    resolves its conflict. Refuse (400) it anywhere but on an update of
    an own item of a branch with a parent, or of a root branch's item
    that overrides an installed layer item (`layers.layer_item`): there
    it resolves its layer conflict. `branch` is the item's
    (`validate_branch`)."""
    rebase = content.pop("rebase", False)
    if rebase is False:
        return False
    if (
        rebase is not True
        or instance is None
        or branch is None
        or (
            branch.parent is None
            and not overrides_layer_item(resource_type, content, branch)
        )
    ):
        raise UIBranchInvalidError(
            "rebase: true keeps an override of the parent's item (or, on a "
            "branch without a parent, of a packaged UI layer's item): it is "
            "valid only on an update of an item of a branch with a parent, "
            "or of one that overrides an installed layer item."
        )
    return True


def overrides_layer_item(resource_type, content, branch) -> bool:
    """Whether the item `content` on `branch` has the key of an installed
    layer's item."""
    kind = kind_of(resource_type.type_uri)
    if not isinstance(content.get("name"), str):
        return False
    key = item_key(kind, content, branch)
    return layer_item(kind, key) is not None


def base_of(resource_type, kind, content, branch, using):
    """The ETag of the parent's item for the key of `content` on
    `branch`, as `branch` shows it (`parent_state`): the current one on
    a live branch, the one its pinned version recorded on a pinned branch
    (ruling R38), the installed layer item's where no ancestor has one;
    else ``None``. On a root branch (no parent), the layer item's ETag
    (`layers.layer_item`), else ``None``."""
    if branch.parent is None:
        if not isinstance(content.get("name"), str):
            return None
        found = layer_item(kind, item_key(kind, content, branch))
        return None if found is None else found[0]
    all_branches = branches(using)
    try:
        rows = parent_state(kind, resource_type.type_uri, branch, all_branches, using)
    except BranchChainError as error:
        raise UIBranchInvalidError(str(error)) from error
    parent = all_branches[branch.parent]
    key = item_key(kind, content, parent)
    for _pk, etag, stored in rows:
        if item_key(kind, stored, parent) == key:
            return etag
    return None


def keeps_key(kind, content, instance, branch) -> bool:
    """Whether `content`, the body of an update of `instance` on `branch`
    (``None``: a body the schema refuses), keeps the stored item's
    `item_key`: its name and, for a stylesheet, its ``applies_to``."""
    if instance is None or branch is None or not isinstance(instance.content, dict):
        return False
    if not isinstance(content.get("name"), str):
        return False
    return item_key(kind, content, branch) == item_key(kind, instance.content, branch)


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


def _same_named(request, resource_type, kind, body, instance, using):
    """Another stored item of `kind` that is the item `body` on its branch
    (`same_item`), or ``None``; ``None`` too for a body without a name or
    branch."""
    name = body.get("name")
    branch = write_state(request).item_branch
    if name is None or branch is None:
        return None
    return same_item(
        other_resources(resource_type, using, instance).filter(content__name=name),
        kind,
        body,
        branch,
    )


def prepare_component(request, resource_type, body, *, instance, using):
    """A UI component's `prepare_item`; then its ``engine``, ``mode`` and
    ``template`` are required (but on a deletion marker), its name is
    unique on its branch (`UIComponentNameTakenError`), and its template
    compiles (`UITemplateInvalidError`)."""
    body = prepare_item(request, resource_type, body, instance=instance, using=using)
    if not isinstance(body, dict):
        return body
    require_unless_deleted(body, ("engine", "mode", "template"))
    if _same_named(request, resource_type, "component", body, instance, using):
        raise UIComponentNameTakenError(body["name"])
    if "template" in body:
        try:
            compile_template(body.get("engine"), body["template"])
        except TemplateCompileError as error:
            raise UITemplateInvalidError(error) from error
    return body


def prepare_stylesheet(request, resource_type, body, *, instance, using):
    """A UI stylesheet's `prepare_item`; then its ``rules`` are required
    (but on a deletion marker), each rule's selector parses
    (`validate_selectors`), and its name is unique on its branch for its
    ``applies_to`` (`UIStylesheetNameTakenError`)."""
    body = prepare_item(request, resource_type, body, instance=instance, using=using)
    if not isinstance(body, dict):
        return body
    require_unless_deleted(body, ("rules",))
    validate_selectors(body)
    if _same_named(request, resource_type, "stylesheet", body, instance, using):
        raise UIStylesheetNameTakenError(body["name"], body["applies_to"])
    return body


def validate_selectors(content):
    """Parse every rule's selector with the stylesheet's own prefixes."""
    prefixes = content.get("prefixes")
    if not isinstance(prefixes, dict):
        prefixes = {}
    rules = content.get("rules")
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        select = rule.get("select") if isinstance(rule, dict) else None
        if not isinstance(select, str):
            continue
        try:
            parse_selector(select, prefixes)
        except SelectorError as error:
            raise UISelectorInvalidError(error, rule=index) from error


def _prepare_text_item(
    request,
    resource_type,
    body,
    *,
    instance,
    using,
    text_field,
    closing_tag,
    invalid_error,
    name_taken_error,
):
    """For the CSS and JS items: `prepare_item`; then the text
    (`text_field`) is required unless the item is a deletion marker, may
    not contain `closing_tag` (any case; `invalid_error`), and the name is
    unique on the branch (`name_taken_error`)."""
    body = prepare_item(request, resource_type, body, instance=instance, using=using)
    if not isinstance(body, dict):
        return body
    require_unless_deleted(body, (text_field,))
    text = body.get(text_field)
    if isinstance(text, str) and closing_tag in text.lower():
        raise invalid_error()
    if _same_named(request, resource_type, text_field, body, instance, using):
        raise name_taken_error(body["name"])
    return body


def prepare_css(request, resource_type, body, *, instance, using):
    """A UI CSS item's checks (`_prepare_text_item`): no ``</style``."""
    return _prepare_text_item(
        request,
        resource_type,
        body,
        instance=instance,
        using=using,
        text_field="css",
        closing_tag="</style",
        invalid_error=UICssInvalidError,
        name_taken_error=UICssNameTakenError,
    )


def prepare_js(request, resource_type, body, *, instance, using):
    """A UI JS item's checks (`_prepare_text_item`): no ``</script``."""
    return _prepare_text_item(
        request,
        resource_type,
        body,
        instance=instance,
        using=using,
        text_field="js",
        closing_tag="</script",
        invalid_error=UIJsInvalidError,
        name_taken_error=UIJsNameTakenError,
    )


def around_item_save(request, resource_type, save, *, instance, using):
    """`save`; an item written to ``main`` while it is virtual stores
    ``main`` first, in the same transaction (`ensure_main`)."""
    branch = write_state(request).item_branch
    if branch is not None and branch.pk is None:
        ensure_main(using)
    return save()


def resolve(resource_type, content, using):
    """Remove the conflict of the item `content` from its branch."""
    kind = kind_of(resource_type.type_uri)
    branch = branches(using).get(branch_of(content))
    if kind is not None and branch is not None and branch.pk is not None:
        key = item_key(kind, content, branch)
        resolve_conflict(branch.name, kind, key, using)


def after_item_write(request, resource_type, saved, *, created, using):
    """A write that keeps the override (``rebase``) resolves its
    conflict, in the write's transaction. The saved content is answered."""
    if write_state(request).rebased:
        resolve(resource_type, saved.content, using)
    return None


def after_item_destroy(request, resource_type, instance, *, using):
    """Dropping an override (its own row, not a deletion marker) of a
    branch with a parent resolves its conflict, in the same
    transaction."""
    content = instance.content
    if isinstance(content, dict) and content.get("deleted") is not True:
        resolve(resource_type, content, using)


# Branches.


def prepare_branch(request, resource_type, body, *, instance, using):
    """Refuse (`UIBranchInvalidError`, 400) a bad, taken or changed name,
    a reserved ``main`` that is not as reserved, a missing parent, a
    cycle of parents, a bad or taken prefix, ``pinned`` without a
    parent, and on an update a changed ``rbac_context`` or a switch
    between published and working that would change the authorizing
    context of its items (`check_unchanged_authorization`). Setting,
    changing or removing a prefix needs PUT on branches in the context
    ``""`` (`check_publisher`, 403). Set ``base`` (default ``live``) and
    the server-managed ``branching_point`` (on create, the current version
    of every ancestor, `current_version`; kept on update) in `body`, which
    is returned. The write-only ``pull`` is removed; ``true`` on an update
    pulls (`pull`): the branching point moves to the ancestors' current
    versions and ``conflicts`` is recomputed (the branch's next version is
    recorded once it is saved, `after_branch_write`). ``conflicts`` is
    server-managed: the stored one is kept, whatever the body says."""
    state = _begin(request)
    if not isinstance(body, dict):
        return body
    name = body.get("name")
    parent = body.get("parent")
    prefix = body.get("prefix")
    base = body.get("base", "live")
    if not (
        isinstance(name, str)
        and isinstance(parent, (str, type(None)))
        and isinstance(prefix, (str, type(None)))
        and base in BASES
    ):
        return body  # the schema refuses it
    if not NAME.fullmatch(name):
        raise UIBranchInvalidError(
            f"A branch name must match {NAME.pattern}; {name!r} does not."
        )
    if instance is not None and instance.content.get("name") != name:
        raise UIBranchInvalidError(
            "A branch cannot be renamed: its items and child branches name it."
        )
    if (
        other_resources(resource_type, using, instance)
        .filter(content__name=name)
        .exists()
    ):
        raise UIBranchInvalidError(f"A UI branch named {name!r} already exists.")
    if name == MAIN:
        check_main(body)
    all_branches = dict(branches(using))
    check_parent(
        request, name, body, parent, prefix, base, all_branches, instance, using
    )
    check_prefix(request, name, body, prefix, all_branches, instance)
    if base == "pinned" and parent is None:
        raise UIBranchInvalidError(
            "A pinned branch needs a parent: it is pinned to its parent's "
            "branching point."
        )
    if instance is not None:
        check_unchanged_authorization(instance, body, prefix, using)
    body["base"] = base
    pulling = body.pop("pull", False) is True and instance is not None
    body.pop("conflicts", None)
    if instance is None:
        body["branching_point"] = (
            {} if parent is None else branching_point(name, all_branches, using)
        )
        return body
    body["branching_point"] = instance.content.get("branching_point", {})
    if "conflicts" in instance.content:
        body["conflicts"] = instance.content["conflicts"]
    if pulling:
        try:
            body.update(pull(body, using, all_branches))
        except BranchChainError as error:
            raise UIBranchInvalidError(str(error)) from error
        state.pulled = True
    return body


def after_branch_write(request, resource_type, saved, *, created, using):
    """A pull records the branch's next version, in the write's
    transaction, once the branch is saved. The saved content is
    answered."""
    if write_state(request).pulled:
        record_version(saved.content["name"], using)
    return None


def branching_point(name, all_branches, using):
    """The current version of every ancestor of the new branch `name`
    (in `all_branches`, which `check_parent` gave it), by name: each
    ancestor's first is recorded now if it has none."""
    return {
        ancestor.name: current_version(ancestor.name, using)
        for ancestor in ancestors(name, all_branches)
    }


def check_unchanged_authorization(instance, content, prefix, using):
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
            for item in unsorted(Resource.objects.using(using))
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


def before_branch_destroy(request, resource_type, instance, *, using):
    """Before a branch is deleted: delete its own items and its versions,
    in the delete's transaction (`discard_branch`), so that a branch
    created again under its name starts afresh. ``main`` is refused
    (`UIBranchInvalidError`, 400), a published branch needs PUT on
    branches in the context ``""`` (`check_publisher`, 403), and a branch
    that other branches name as their parent is refused
    (`UIBranchInUseError`, 409)."""
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
        check_publisher(request, content)
    if isinstance(name, str):
        children = sorted(
            branch.name
            for branch in _read_branches(using).values()
            if branch.parent == name and branch.name != name
        )
        if children:
            raise UIBranchInUseError(name, children)
        discard_branch(name, using)


def check_main(content):
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
    request, name, content, parent, prefix, base, all_branches, instance, using
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
        and not may_get_stored(request, branch_content(all_branches[parent], using))
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


def check_prefix(request, name, content, prefix, all_branches, instance):
    """A prefix is ``""`` or ``/segment/.../``, and no other branch's
    (the virtual ``main``'s ``""`` included). Setting one, or changing
    it (to another, to none or from none), publishes URLs of the site,
    and needs PUT on branches in the context ``""`` (`check_publisher`,
    403; ruling R37); the new prefix must not lie under the API's URLs,
    hide URLs the project's URLconf serves, or overlap another branch's
    (`checks.prefix_problems`, 400)."""
    if prefix is not None and prefix and not PREFIX.fullmatch(prefix):
        raise UIBranchInvalidError(
            f'A branch prefix is "" or matches {PREFIX.pattern}; {prefix!r} does not.',
            name=PREFIX_FIELD,
        )
    stored = None if instance is None else branch_from(instance.pk, instance.content)
    changed = prefix != (None if stored is None else stored.prefix)
    if changed:
        check_publisher(request, content)
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


def check_publisher(request, content):
    """Refuse (``access.AccessDenied``, 403) unless the writer may PUT
    branches in the context ``""``: a prefix claims URLs of the whole
    site, which no grant in the branch's own context covers. Setting,
    changing or removing a prefix needs it, and so does deleting a
    published branch."""
    permission = Permission("http.PUT", UI_BRANCH_TYPE_URI)
    if not may_act_in_context(request, permission, content, [""]):
        raise access.AccessDenied(
            "Setting, changing or removing a branch's prefix (or deleting "
            "a published branch) changes the site's URLs: it needs PUT on "
            "UI branches in the context \"\"."
        )


# Pushes.


def applied_content(content) -> bool:
    """Whether push `content` is applied."""
    return isinstance(content, Mapping) and content.get("state") == "applied"


def applied(push) -> bool:
    """Whether the stored push `push` (a `Resource`) is applied."""
    return applied_content(push.content)


def may_get(request, branch, using) -> bool:
    """Whether the pusher may GET `branch` (``main`` always exists)."""
    return branch.name == MAIN or may_get_stored(request, branch_content(branch, using))


def prepare_push(request, resource_type, body, *, instance, using):
    """Refuse (`UIBranchInvalidError`, 400) a push that names both or
    neither of ``source`` and ``restore``, an unknown branch, a source
    that is the target or one of its ancestors (a push would write a
    branch's overrides into its own base), and (`UIBranchContextError`,
    400) an ``rbac_context`` that is not the target's context. A push
    from a branch with no parent is refused: it would delete the items
    of a root (``main``: the live site's). An applied push is a record:
    any update of it is refused (400; pushing again takes a new push).
    The server-managed ``result`` is dropped (only an applied push has
    one). Note whether the write moves the push into ``applied``
    (`after_push_write` applies it)."""
    state = _begin(request)
    if instance is not None and applied(instance):
        raise UIBranchInvalidError(
            "This UI push is applied: it is the record of what it changed "
            "and cannot be updated. Push again with a new UI push."
        )
    if not isinstance(body, dict):
        return body
    source = body.get("source")
    target = body.get("target")
    restore = body.get("restore")
    if not (
        isinstance(source, (str, type(None)))
        and isinstance(target, str)
        and (restore is None or type(restore) is int)
    ):
        return body  # the schema refuses it
    if (source is None) == (restore is None):
        raise UIBranchInvalidError(
            "A push names exactly one of a source branch (source) and a "
            "version of the target to restore (restore)."
        )
    all_branches = branches(using)
    for name in (target, source):
        if name is not None and name not in all_branches:
            raise UIBranchInvalidError(f"There is no UI branch named {name!r}.")
    if source is not None and not may_get(request, all_branches[source], using):
        # Applying needs GET on the source anyway: one the pusher may not
        # see is answered as unknown (ruling R36).
        raise UIBranchInvalidError(f"There is no UI branch named {source!r}.")
    if source is not None:
        refuse_push_between(source, target, all_branches)
    expected = all_branches[target].context
    if body.get("rbac_context") != expected:
        if not may_get(request, all_branches[target], using):
            # Naming the context would describe a branch the pusher may
            # not see (ruling R36).
            raise UIBranchInvalidError(f"There is no UI branch named {target!r}.")
        raise UIBranchContextError(expected)
    body.pop("result", None)
    state.applying = body.get("state") == "applied"
    return body


def after_push_write(request, resource_type, saved, *, created, using):
    """When the write moved the push `saved` into ``applied``, apply it in
    the write's transaction: write its source's overrides into its target
    (`apply_push`), or restore version ``restore`` of its target
    (`apply_restore`); then store its ``result``, the target's new
    version, and answer the push with it. A refused push writes nothing,
    the push included. ``None`` (the saved content answered) otherwise."""
    if not write_state(request).applying:
        return None
    content = saved.content
    try:
        if content.get("source") is not None:
            number = apply_push(request, content, using)
        else:
            number = apply_restore(request, content, using)
    except BranchChainError as error:
        # A chain broken, or a pinned snapshot gone, since validation.
        raise UIBranchInvalidError(str(error)) from error
    ResourceRegistration.update_locked(
        saved,
        {**content, "result": {"version": number}},
        resource_type=resource_type,
        using=using,
    )
    return content_of(saved)


def before_push_destroy(request, resource_type, instance, *, using):
    """An applied push is kept (`UIPushAppliedError`, 409)."""
    if applied(instance):
        raise UIPushAppliedError()


# The write behaviour of each UI type, by slug.
UI_TYPE_OPTIONS: dict[str, TypeOptions] = {
    UI_COMPONENT_SLUG: TypeOptions(
        prepare_write=prepare_component,
        around_save=around_item_save,
        after_write=after_item_write,
        after_destroy=after_item_destroy,
    ),
    UI_STYLESHEET_SLUG: TypeOptions(
        prepare_write=prepare_stylesheet,
        around_save=around_item_save,
        after_write=after_item_write,
        after_destroy=after_item_destroy,
    ),
    UI_CSS_SLUG: TypeOptions(
        prepare_write=prepare_css,
        around_save=around_item_save,
        after_write=after_item_write,
        after_destroy=after_item_destroy,
    ),
    UI_JS_SLUG: TypeOptions(
        prepare_write=prepare_js,
        around_save=around_item_save,
        after_write=after_item_write,
        after_destroy=after_item_destroy,
    ),
    UI_BRANCH_SLUG: TypeOptions(
        prepare_write=prepare_branch,
        after_write=after_branch_write,
        before_destroy=before_branch_destroy,
    ),
    UI_PUSH_SLUG: TypeOptions(
        prepare_write=prepare_push,
        after_write=after_push_write,
        before_destroy=before_push_destroy,
    ),
    UI_BRANCH_VERSION_SLUG: TypeOptions(methods=READ_ONLY_METHODS),
}
