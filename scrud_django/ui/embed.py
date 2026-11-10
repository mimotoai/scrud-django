"""Server-side embeds: ``embed(path, scope, mode)`` in a stored Jinja
component's context (node and page) renders the resource at `path` as the
fragment its own URL answers, in process, as the same user
(`dispatch.dispatch_path`), so that its view's checks (RBAC, workflow,
masking, ``check_mount``) apply unchanged.

The embedded request is a GET with ``HX-Request: true`` (a fragment), the
scope chain (`SCOPE_HEADER`), ``Scrud-UI-Mode: view`` when `mode` is
``"view"``, and the embedding render's branch (`BRANCH_HEADER`, the active
selection's value). A response of status 400 or more is embedded as it is:
the notice the resource's own URL answers with.

What the embed carries over in process, by context variable rather than by
header: safe mode (`branches.effective.using_safe_mode`, which the
renderer keeps), preview (`render.using_preview`, which `render_resource`
keeps), the embed stack and the response's embed count (`_state`).

Refused, with the notice `REFUSED_MESSAGE` (and a warning logged): a path
that is not a string, not absolute or has a scheme or host; one that does
not resolve to a resource view (`ResourceViewMixin`: stored and computed
types), or whose response is not the UI's HTML (``?format=json``); a path and scope chain already on the embed stack (a cycle; the
top-level request is its bottom); an embed deeper than `MAX_EMBED_DEPTH`;
and any embed past the first `MAX_EMBEDS` of a top-level response. A view
that raises renders `FAILED_MESSAGE` (logged)."""

import contextvars
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from markupsafe import Markup

from scrud_django.ui.branches.effective import BRANCH_HEADER, active_selection
from scrud_django.ui.branches.selection import selection_value
from scrud_django.ui.cascade import builtin_component, rules_memo
from scrud_django.ui.dispatch import dispatch_path, resolve_path

logger = logging.getLogger(__name__)

# How deep embeds nest below the top-level response (an embed of an embed of
# an embed), and how many one top-level response makes, nested ones
# included.
MAX_EMBED_DEPTH = 3
MAX_EMBEDS = 20

REFUSED_MESSAGE = "This embed was refused."
FAILED_MESSAGE = "This embed failed."

# On the top-level request: its `_EmbedState`.
_REQUEST_ATTR = "_scrud_ui_embeds"


@dataclass
class _Count:
    embeds: int = 0


@dataclass(frozen=True)
class _EmbedState:
    """The embed `stack` (``(API path, scope chain)`` of the top-level
    request, then of each embed it is inside, outermost first), and the
    top-level response's embed `count`, shared by every embed in it."""

    stack: tuple[tuple[str, tuple[str, ...]], ...]
    count: _Count = field(default_factory=_Count)


# The state of the embed whose request is in progress (``None`` outside
# one: the top-level request's is on it, `_REQUEST_ATTR`).
_state: contextvars.ContextVar[_EmbedState | None] = contextvars.ContextVar(
    "scrud_ui_embed_state", default=None
)


def embed_for(request):
    """The ``embed`` callable of a component rendered for `request`:
    ``embed(path, scope=(), mode=None)`` returns the HTML (`Markup`) of the
    resource at `path` in the scope chain `scope` (a string of names, or a
    list of them), in view mode when `mode` is ``"view"``, or a notice."""

    def embed(path, scope=(), mode=None) -> Markup:
        return Markup(_embed(request, path, scope, mode))

    return embed


def _embed(request, path, scope, mode) -> str:
    from scrud_django.ui import renderer

    state = _state.get() or _top_state(request)
    state.count.embeds += 1
    if state.count.embeds > MAX_EMBEDS:
        return _refused(path, f"over the count limit ({MAX_EMBEDS} per response)")
    if not isinstance(path, str):
        return _refused(path, "not a URL path")
    try:
        parts = urlsplit(path)
    except ValueError:
        return _refused(path, "not a URL path")
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return _refused(path, "not an absolute path on this site")
    if len(state.stack) > MAX_EMBED_DEPTH:
        return _refused(path, f"over the depth limit ({MAX_EMBED_DEPTH})")
    resolved = resolve_path(parts.path)
    if resolved is None or not _is_resource_view(resolved.match.func):
        return _refused(path, "not a resource view")
    chain = renderer.parse_scope_chain(_scope_value(scope))
    key = (resolved.path_info, chain)
    if key in state.stack:
        return _refused(path, f"a cycle (in scope {' '.join(chain)!r})")
    headers = {
        renderer.HX_REQUEST_HEADER: "true",
        renderer.SCOPE_HEADER: " ".join(chain),
        BRANCH_HEADER: selection_value(active_selection()),
    }
    if mode == renderer.VIEW_MODE:
        headers[renderer.MODE_HEADER] = renderer.VIEW_MODE
    token = _state.set(_EmbedState(state.stack + (key,), state.count))
    try:
        # A memo of its own: the embedded request chooses its branch again
        # (it may be refused), and the memo is keyed without the branch.
        with rules_memo():
            response = dispatch_path(request, path, headers=headers, resolved=resolved)
    except Exception:
        logger.warning("scrud-ui: embed of %r failed", path, exc_info=True)
        return _notice(FAILED_MESSAGE)
    finally:
        _state.reset(token)
    # Only the UI's own HTML: never JSON (``?format=json``) or the browsable
    # API's page (``?format=api``), which are not fragments, nor escaped.
    if not isinstance(
        getattr(response, "accepted_renderer", None), renderer.ScrudHTMLRenderer
    ):
        return _refused(path, f"answered {response.get('Content-Type')!r}, not UI HTML")
    return response.content.decode(response.charset or "utf-8")


def _top_state(request) -> _EmbedState:
    """The embed state of the top-level request `request`, kept on it."""
    from scrud_django.ui import renderer

    http = getattr(request, "_request", request)
    state = getattr(http, _REQUEST_ATTR, None)
    if state is None:
        top = (getattr(request, "path_info", ""), renderer.scope_chain_of(request))
        state = _EmbedState(stack=(top,))
        setattr(http, _REQUEST_ATTR, state)
    return state


def _scope_value(scope) -> str:
    """`scope` (a string of names, or a list or tuple of names) as a scope
    header's value; anything else as no scope."""
    if isinstance(scope, str):
        return scope
    if isinstance(scope, (list, tuple)) and all(isinstance(s, str) for s in scope):
        return " ".join(scope)
    return ""


def _is_resource_view(view) -> bool:
    """Whether `view` is a scrud resource view (stored or computed)."""
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    cls = getattr(view, "cls", None)
    return isinstance(cls, type) and issubclass(cls, ResourceViewMixin)


def _refused(path, reason) -> str:
    logger.warning("scrud-ui: embed of %r refused: %s", path, reason)
    return _notice(REFUSED_MESSAGE)


def _notice(message) -> str:
    return str(
        builtin_component("scrud:notice")
        .compiled()
        .render({"message": message, "level": "error"})
    )
