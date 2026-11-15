"""The page chrome of a working branch: the banner a page on it shows, and the
page that asks before a shared link switches branch.

Both are package templates (``templates/``) rendered on the built-in engine,
never resolved through stylesheets, so that no branch's components can
change or hide them. Every value is escaped, and every URL is the request's
path (URI-escaped) with a query string encoded here. The links that switch
branch carry the viewer's confirmation token for their selection
(`selection.confirm_token`), which no link planted in content can."""

import importlib.resources
from functools import cache

from django.utils.encoding import escape_uri_path
from django.utils.safestring import SafeString, mark_safe

from scrud_django.ui.branches.effective import BRANCH_PARAMETER, Selection
from scrud_django.ui.branches.selection import (
    CONFIRM_PARAMETER,
    Chosen,
    confirm_token,
    is_published,
    published_branch,
    selectable_branches,
    selection_value,
)
from scrud_django.ui.branches.store import branches
from scrud_django.ui.engines import cached_template

# Server-rendered, constant: it names no branch and holds no input.
SAFE_NOTICE = mark_safe(
    '<div class="scrud-safe-mode" role="status">'
    "Safe mode: this page ignores stored UI items.</div>"
)


def banner(chosen: Chosen, request) -> SafeString:
    """The banner of a page that `chosen` did not render on the published
    branch, or that names a branch it refused: the branch and its mode, a
    switcher (a link to each branch the viewer may GET in the tree), a
    live/pinned toggle, a link to share and the way back."""
    published = published_branch(request)
    selection = chosen.selection
    working = not is_published(chosen, request)
    branch = branches().get(selection.branch) if working else None
    context = {
        "refused": chosen.refused,
        "published": published,
        "branch": selection.branch if working else None,
        "mode": _mode(selection, branch),
        "switch": [
            (name, _href(request, name, confirm=True))
            for name in selectable_branches(request)
        ],
        "back_href": _href(request, "", confirm=True),
        "copy_href": _href(request, selection_value(selection)) if working else None,
        "toggle_href": None,
        "toggle_label": None,
    }
    if branch is not None and branch.base == "live":
        flipped = Selection(branch=selection.branch, pinned=not selection.pinned)
        if not flipped.pinned or _pinned_number(branch) is not None:
            context["toggle_href"] = _href(
                request, selection_value(flipped), confirm=True
            )
            context["toggle_label"] = (
                "View live" if selection.pinned else "View at its branching point"
            )
    return _template("banner.html").render(context)


def confirm_page(chosen: Chosen, request) -> SafeString:
    """A page asking before the request's link switches to `chosen`'s
    branch: Continue (the same URL, confirmed) or Cancel (without the
    selection)."""
    selection = chosen.selection
    query = request.GET.copy()
    query[CONFIRM_PARAMETER] = confirm_token(request, query.get(BRANCH_PARAMETER, ""))
    return _template("confirm.html").render(
        {
            "branch": selection.branch,
            "mode": _mode(selection, branches().get(selection.branch)),
            "continue_href": _with_query(request, query),
            "cancel_href": _with_query(request, _base_query(request)),
        }
    )


def _mode(selection, branch) -> str:
    """``"live"``, or ``"pinned at <parent> v<n>"``."""
    if branch is None or not (selection.pinned or branch.base == "pinned"):
        return "live"
    number = _pinned_number(branch)
    if number is None:
        return "pinned"
    return f"pinned at {branch.parent} v{number}"


def _pinned_number(branch):
    number = branch.branching_point.get(branch.parent) if branch.parent else None
    return number if isinstance(number, int) else None


def _base_query(request):
    """The request's query without the branch selection."""
    query = request.GET.copy()
    query.pop(BRANCH_PARAMETER, None)
    query.pop(CONFIRM_PARAMETER, None)
    return query


def _href(request, value, confirm=False) -> str:
    """The request's URL selecting `value` (confirmed with `confirm`)."""
    query = _base_query(request)
    query[BRANCH_PARAMETER] = value
    if confirm:
        query[CONFIRM_PARAMETER] = confirm_token(request, value)
    return _with_query(request, query)


def _with_query(request, query) -> str:
    encoded = query.urlencode()
    return escape_uri_path(request.path) + ("?" + encoded if encoded else "")


@cache
def _source(name: str) -> str:
    return (
        importlib.resources.files("scrud_django.ui.branches")
        .joinpath("templates", name)
        .read_text(encoding="utf-8")
    )


def _template(name: str):
    return cached_template(("branch-chrome", name), "django", _source(name))
