"""The branch a request browses: chosen from its `BRANCH_HEADER`, its
`BRANCH_PARAMETER` query parameter or its `BRANCH_COOKIE`, in that order, else
the published branch of the request's prefix (``main`` at the API URLs).

A value is ``<name>`` or ``<name>@pinned`` (`VALUE`, at most
`MAX_VALUE_BYTES`); any other is ignored, as if absent. An empty header or
parameter names the published branch. A branch that is unknown, whose chain
is broken, that the viewer may not GET, that is not in the prefix's tree, or
whose pinned snapshot is missing is refused: the published branch is used,
and `Chosen.refused` names it (the banner says so). A request with no
selection, or one naming the published branch, reads no branch row (ruling
R1)."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from django.urls import reverse

from scrud_django.models import Resource
from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.ui.branches import MAIN, UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.effective import (
    BRANCH_HEADER,
    BRANCH_PARAMETER,
    PINNED_SUFFIX,
    Selection,
    _missing_snapshot,
    chain_of,
)
from scrud_django.ui.branches.model import Branch
from scrud_django.ui.branches.store import BranchChainError, branches, published_root
from scrud_django.ui.cascade import _memo
from scrud_django.ui.types import UI_COMPONENT_SLUG

logger = logging.getLogger(__name__)

BRANCH_COOKIE = "scrud_ui_branch"
CONFIRM_PARAMETER = "ui-branch-confirm"
VALUE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}(?:@pinned)?")
MAX_VALUE_BYTES = 80

# Where a selection came from.
HEADER, PARAM, COOKIE, NONE = "header", "param", "cookie", "none"

# On the request: {use the cookie: Chosen}.
_CHOSEN_ATTR = "_scrud_ui_chosen"
# The key the stored branch contents are memoised under in a `rules_memo`.
_CONTENTS = "branch-contents"


@dataclass(frozen=True)
class Chosen:
    """The `selection` a request renders, the `source` of the value that
    chose it (``"header"``, ``"param"``, ``"cookie"`` or ``"none"``), the
    branch name it `refused` (``None`` when it was used), and whether the
    parameter came `confirmed` (``ui-branch-confirm=1``, not cross-site)."""

    selection: Selection
    source: str
    refused: str | None
    confirmed: bool


def api_base() -> str:
    """The path the API's type URLs start with (``/api/``): the path of the
    UI components' list without its slug."""
    path = reverse("ui-components-list")
    suffix = UI_COMPONENT_SLUG + "/"
    return path[: -len(suffix)] if path.endswith(suffix) else path


def published_branch(request) -> str:
    """The name of the published branch of `request`'s prefix: ``main``
    (prefixes come later)."""
    return MAIN


def cookie_path(request) -> str:
    """The path `BRANCH_COOKIE` is scoped to: the prefix's."""
    return api_base()


def selection_value(selection: Selection) -> str:
    """`selection` as a header, parameter or cookie value."""
    return selection.branch + (PINNED_SUFFIX if selection.pinned else "")


def parse_value(value) -> Selection | None:
    """The selection `value` names, or ``None`` when it is not one
    (`VALUE`, at most `MAX_VALUE_BYTES`)."""
    if not isinstance(value, str) or not VALUE.fullmatch(value):
        return None
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return None
    pinned = value.endswith(PINNED_SUFFIX)
    name = value[: -len(PINNED_SUFFIX)] if pinned else value
    return Selection(branch=name, pinned=pinned)


def is_published(chosen: Chosen, request) -> bool:
    return chosen.selection == _published_selection(published_branch(request))


def frame_branch(request) -> str | None:
    """What a frame's ``data-scrud-branch`` says, so that the requests made
    from it (`BRANCH_HEADER`) stay on the page's branch: the selection's
    value off the published branch; ``""`` on the published branch when the
    request named something (so that a cookie does not move its fragments);
    else ``None``, no attribute."""
    if request is None:
        return None
    chosen = choose(request)
    if not is_published(chosen, request):
        return selection_value(chosen.selection)
    return "" if chosen.source != NONE else None


def cookie_branch(request) -> str | None:
    """The branch name `BRANCH_COOKIE` holds, when it is a selection value."""
    cookies = getattr(request, "COOKIES", None)
    selection = parse_value(cookies.get(BRANCH_COOKIE) if cookies else None)
    return selection.branch if selection is not None else None


def choose(request, *, cookie: bool = True) -> Chosen:
    """The branch `request` browses (`Chosen`). Without `cookie`, the cookie
    is not read: JSON is not affected by browsing. Chosen once per request."""
    if request is None:
        return Chosen(Selection(), NONE, None, False)
    cache = getattr(request, _CHOSEN_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(request, _CHOSEN_ATTR, cache)
        except AttributeError:
            pass
    if cookie not in cache:
        cache[cookie] = _choose(request, cookie)
    return cache[cookie]


def selection_for_request(request) -> Selection:
    """`choose(request).selection`."""
    return choose(request).selection


def _choose(request, cookie):
    published = published_branch(request)
    for source, value in _values(request, cookie):
        if value == "" and source != COOKIE:
            selection = _published_selection(published)
        else:
            selection = parse_value(value)
            if selection is None:
                continue  # ignored
        confirmed = source == PARAM and _confirmed(request)
        if selection.branch == published:
            return Chosen(_published_selection(published), source, None, confirmed)
        if _selectable(request, selection, published):
            return Chosen(selection, source, None, confirmed)
        return Chosen(
            _published_selection(published), source, selection.branch, confirmed
        )
    return Chosen(_published_selection(published), NONE, None, False)


def _values(request, cookie):
    headers = getattr(request, "headers", None)
    params = getattr(request, "GET", None)
    yield HEADER, headers.get(BRANCH_HEADER) if headers is not None else None
    yield PARAM, params.get(BRANCH_PARAMETER) if params is not None else None
    if cookie:
        cookies = getattr(request, "COOKIES", None)
        yield COOKIE, cookies.get(BRANCH_COOKIE) if cookies else None


def _published_selection(published):
    return Selection(branch=published)


def _confirmed(request) -> bool:
    """Whether the request carries ``ui-branch-confirm=1`` from the site's own
    pages: a cross-site link (or one pasted in the address bar) that carries
    it is asked to confirm all the same."""
    params = getattr(request, "GET", None)
    if params is None or params.get(CONFIRM_PARAMETER) != "1":
        return False
    headers = getattr(request, "headers", None)
    site = headers.get("Sec-Fetch-Site") if headers is not None else None
    return site in (None, "same-origin")


def _selectable(request, selection, published) -> bool:
    all_branches = branches()
    name = selection.branch
    if name not in all_branches:
        return False
    try:
        chain = chain_of(name, all_branches)
        root = published_root(name, all_branches)
    except BranchChainError as error:
        logger.warning("scrud-ui: not rendering branch %r: %s", name, error)
        return False
    if root.name != published or not may_get(request, all_branches[name]):
        return False
    missing = _missing_snapshot(chain, selection.pinned)
    if missing is not None:
        logger.warning(
            "scrud-ui: not rendering branch %r: version %r of %r, which it is "
            "pinned to, is missing",
            name,
            missing[1],
            missing[0],
        )
        return False
    return True


def may_get(request, branch: Branch) -> bool:
    """Whether the user of `request` may GET `branch`, as stored."""
    content = _stored_contents().get(branch.pk)
    if not isinstance(content, Mapping):
        return False
    return may_read_in_context(
        request, UI_BRANCH_TYPE_URI, content, resource_contexts(content)
    )


def selectable_branches(request) -> list[str]:
    """The names of the working branches in the tree of `request`'s
    published branch that its user may GET, sorted."""
    published = published_branch(request)
    all_branches = branches()
    found = []
    for name, branch in all_branches.items():
        if branch.published:
            continue
        try:
            root = published_root(name, all_branches)
        except BranchChainError:
            continue
        if root.name == published and may_get(request, branch):
            found.append(name)
    return sorted(found)


def _stored_contents() -> dict:
    """``{pk: content}`` of every stored branch, read once per `rules_memo`."""
    memo = _memo.get()
    if memo is not None and _CONTENTS in memo:
        return memo[_CONTENTS]
    found = dict(
        Resource.objects.filter(resource_type__type_uri=UI_BRANCH_TYPE_URI).values_list(
            "pk", "content"
        )
    )
    if memo is not None:
        memo[_CONTENTS] = found
    return found
