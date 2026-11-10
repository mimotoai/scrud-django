"""The live preview (E1): what a working branch would show with unsaved drafts
of its items, or what a branch will show after a push, with the matches of a
rule marked, as a document for a sandboxed frame with an opaque origin.

The document runs the branch's scripts (``sandbox allow-scripts``, without
``allow-same-origin``), so they can reach neither the studio's page nor the
user's cookies or session. A request from an opaque origin is cross-site, so
the branch's CSS and JS files (gated by GET on the branch, privately cached)
would load without the author's login: they are inlined instead, under a
nonce new with each response (`live_preview_csp`), which is the only inline
code the document runs. ``preview-bridge.js``, loaded by URL, reports clicks
and the drafts' errors to the frame's parent with ``postMessage``.

Drafts are layered over the branch for the one render (`using_drafts`) and
never stored; each is shown only if its save would be accepted
(`drafts.validate_draft`), and its errors are listed otherwise."""

import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from django.shortcuts import render
from django.templatetags.static import static
from django.utils.safestring import mark_safe
from rest_framework.exceptions import PermissionDenied

from scrud_django.exceptions import (
    UIStudioBranchReadOnlyError,
    UIStudioPreviewInvalidError,
)
from scrud_django.ui.branches.css import css_files
from scrud_django.ui.branches.effective import (
    Draft,
    effective_rows,
    using_drafts,
    using_selection,
)
from scrud_django.ui.branches.js import js_files
from scrud_django.ui.branches.model import branch_of, item_key
from scrud_django.ui.branches.selection import branch_content, may_get_stored
from scrud_django.ui.branches.store import branches
from scrud_django.ui.cascade import (
    RuleSource,
    _applies_to_contexts,
    _default_stylesheet,
    _name_of,
    rules_memo,
)
from scrud_django.ui.render import _JSON_SCRIPT_ESCAPES, Mark, using_mark
from scrud_django.ui.renderer import STATIC_HTMX, STATIC_SCRUD_UI
from scrud_django.ui.studio.drafts import (
    after_push_drafts,
    draft_item,
    validate_draft,
)
from scrud_django.ui.studio.preview import render_preview
from scrud_django.ui.types import UI_STYLESHEET_TYPE_URI

logger = logging.getLogger(__name__)

MODES = ("view", "edit")

# The most drafts one live preview request may send.
MAX_DRAFTS = 50


def new_nonce() -> str:
    """A CSP nonce for one response."""
    return secrets.token_urlsafe(18)


def live_preview_csp(nonce: str) -> str:
    """The ``Content-Security-Policy`` of a live preview response: a sandbox
    with scripts and an opaque origin (no ``allow-same-origin``), scripts
    and styles from the server's own URLs or carrying `nonce`, nothing
    else."""
    return (
        "sandbox allow-scripts; default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; connect-src 'self'"
    )


@dataclass(frozen=True)
class LiveRequest:
    """A live preview's ``request``, its shape checked (`parse_request`)."""

    slug: str
    branch: str
    mode: str
    scope: tuple[str, ...]
    context: str
    instance: str | None
    drafts: tuple[Draft, ...]
    mark: dict | None
    after_push: str | None

    def params(self) -> dict:
        """The bootstrap preview's parameters (``views._shown``)."""
        params = {
            "branch": self.branch,
            "mode": self.mode,
            "scope": " ".join(self.scope),
            "context": self.context,
        }
        if self.instance is not None:
            params["instance"] = self.instance
        return params


def parse_request(text) -> LiveRequest:
    """The `LiveRequest` the form field ``request`` (`text`) holds:

    ``{"type": slug, "branch": selection value (default "main"), "mode":
    "view" | "edit" (default "view"), "scope": [name], "context": str,
    "instance": pk | null, "drafts": [{"kind", "content"}], "mark":
    {"stylesheet": name, "applies_to": str, "index": int} | null,
    "after_push": {"source": branch name} | null}``.

    `UIStudioPreviewInvalidError` for anything else, and for an
    ``after_push`` with drafts. A draft's content is checked when it is
    validated, so that its errors are listed, not refused."""
    if not isinstance(text, str):
        raise UIStudioPreviewInvalidError("the form field 'request' is required.")
    try:
        body = json.loads(text)
    except (ValueError, RecursionError) as error:
        # RecursionError: nested too deep for the parser.
        raise UIStudioPreviewInvalidError("it is not JSON the server can read.") from (
            error
        )
    if not isinstance(body, dict):
        raise UIStudioPreviewInvalidError("it must be a JSON object.")
    slug = body.get("type")
    if not isinstance(slug, str) or not slug:
        raise UIStudioPreviewInvalidError("'type' must be a resource type slug.")
    branch = _optional(body, "branch", str, "main")
    mode = _optional(body, "mode", str, "view")
    if mode not in MODES:
        raise UIStudioPreviewInvalidError("'mode' must be 'view' or 'edit'.")
    scope = _optional(body, "scope", list, [])
    if not all(isinstance(name, str) and name.split() == [name] for name in scope):
        raise UIStudioPreviewInvalidError("'scope' must be a list of scope names.")
    context = _optional(body, "context", str, "")
    instance = body.get("instance")
    if isinstance(instance, bool) or not isinstance(instance, (int, str, type(None))):
        raise UIStudioPreviewInvalidError("'instance' must be a pk or null.")
    drafts = _optional(body, "drafts", list, [])
    if len(drafts) > MAX_DRAFTS:
        raise UIStudioPreviewInvalidError(f"send at most {MAX_DRAFTS} drafts.")
    if not all(isinstance(d, dict) and isinstance(d.get("kind"), str) for d in drafts):
        raise UIStudioPreviewInvalidError(
            "'drafts' must be a list of objects, each with a 'kind'."
        )
    mark = body.get("mark")
    if mark is not None and not (
        isinstance(mark, dict)
        and isinstance(mark.get("stylesheet"), str)
        and isinstance(mark.get("applies_to"), str)
        and type(mark.get("index")) is int
    ):
        raise UIStudioPreviewInvalidError(
            "'mark' must be {stylesheet, applies_to, index} or null."
        )
    after_push = body.get("after_push")
    if after_push is not None:
        source = after_push.get("source") if isinstance(after_push, dict) else None
        if not isinstance(source, str) or not source:
            raise UIStudioPreviewInvalidError(
                "'after_push' must be {source: branch name} or null."
            )
        if drafts:
            raise UIStudioPreviewInvalidError(
                "'after_push' previews the source's stored overrides alone: "
                "send no drafts with it."
            )
        after_push = source
    return LiveRequest(
        slug=slug,
        branch=branch,
        mode=mode,
        scope=tuple(scope),
        context=context,
        instance=None if instance is None else str(instance),
        drafts=tuple(Draft(d["kind"], d.get("content")) for d in drafts),
        mark=mark,
        after_push=after_push,
    )


def _optional(body, key, kind, default):
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, kind):
        raise UIStudioPreviewInvalidError(f"{key!r} has the wrong type.")
    return value


def render_live_preview(
    request, resource_type, shown, live: LiveRequest, nonce: str, readable
):
    """The live preview document of `live` for `request`'s user: what
    `shown` (``views._Shown``, its branch selectable) shows on its branch T,
    with `live`'s drafts (only those `validate_draft` accepts) or, with
    ``after_push``, the source's stored overrides (`after_push_drafts`)
    layered over T; T's CSS and JS inlined under `nonce`; the drafts'
    errors listed; and `live`'s mark, unless `readable` (the inspector's
    read check of a rule's `RuleSource`, ``views._readable``) refuses its
    stylesheet. Drafts on a published T are refused
    (`UIStudioBranchReadOnlyError`); an ``after_push`` source the user may
    not GET, or that does not exist, is ``PermissionDenied`` (403), alike.
    Nothing is written."""
    selection = shown.selection
    all_branches = branches()
    target = all_branches[selection.branch]
    if live.drafts and target.published:
        raise UIStudioBranchReadOnlyError(target.name)
    errors = []
    if live.after_push is not None:
        source = all_branches.get(live.after_push)
        if source is None or not may_get_stored(request, branch_content(source)):
            raise PermissionDenied(
                f"You may not read branch {live.after_push!r}, or it does not exist."
            )
        drafts = after_push_drafts(source, target)
    else:
        drafts = _accepted(request, target, live.drafts, errors)
    with (
        using_selection(selection),
        using_drafts(target.name, drafts),
        rules_memo(),
        using_mark(_mark(live.mark, readable)),
    ):
        frame = render_preview(
            request,
            resource_type,
            shown.content,
            selection=selection,
            mode=shown.mode,
            scope_chain=shown.scope_chain,
            contexts=shown.contexts,
            decisions=shown.decisions,
            href=shown.href,
        )
        styles = _inlined(css_files(selection), "</style")
        scripts = _inlined(js_files(selection), "</script")
    return render(
        request._request,
        "scrud_studio/live_preview.html",
        {
            "title": _title(resource_type),
            "nonce": nonce,
            "styles": styles,
            "scripts": scripts,
            "frame": frame,
            "errors": mark_safe(
                json.dumps(errors, ensure_ascii=False).translate(_JSON_SCRIPT_ESCAPES)
            ),
            "static": {
                "htmx": static(STATIC_HTMX),
                "scrud_ui": static(STATIC_SCRUD_UI),
            },
        },
    )


def _accepted(request, target, drafts, errors) -> list[Draft]:
    """The drafts of `drafts` that may be saved on `target`, each as its
    save would send it (`drafts.draft_item`); each other one is added to
    `errors` as ``{kind, name, messages}``: what its save would answer, or,
    for a second draft of an item, that another comes first."""
    accepted = []
    seen = set()
    for draft in drafts:
        item, messages = draft_item(target, draft)
        key = None if item is None else (draft.kind, item_key(draft.kind, item, target))
        if key in seen:
            messages = ["Another draft of this item comes before it."]
        elif key is not None:
            messages = validate_draft(request, target, draft)
        if messages:
            content = draft.content if isinstance(draft.content, Mapping) else {}
            name = content.get("name")
            errors.append(
                {
                    "kind": draft.kind,
                    "name": name if isinstance(name, str) else None,
                    "messages": messages,
                }
            )
            continue
        seen.add(key)
        accepted.append(Draft(draft.kind, item))
    return accepted


def _mark(mark, readable) -> Mark | None:
    """The `Mark` of a request's ``mark``, the stylesheet's branch as the
    selected branch (drafts included) resolves it: the branch of the row it
    shows for the stylesheet's name in that context, ``""`` for the default
    stylesheet. ``None`` (nothing marked) when it shows none, and alike when
    `readable` refuses that row's source, as the inspector hides its rules:
    a mark tells nothing of a stylesheet its user may not read."""
    if mark is None:
        return None
    name, target = mark["stylesheet"], mark["applies_to"]
    for pk, _etag, content in effective_rows(UI_STYLESHEET_TYPE_URI):
        if _name_of(content) == name and target in _applies_to_contexts(content):
            branch = branch_of(content)
            if not readable(RuleSource(pk, name, branch, target, content)):
                return None
            return Mark(name, target, mark["index"], branch)
    if name == _name_of(_default_stylesheet()) and target == "":
        return Mark(name, target, mark["index"], "")
    return None


def _inlined(files, closing_tag) -> list[str]:
    """The texts of `files` (``(name, etag, text)``) that may be inlined in
    an element `closing_tag` closes: a text containing it (which no write
    through the API stores) is left out, and logged."""
    texts = []
    for name, _etag, text in files:
        if closing_tag in text.lower():
            logger.warning(
                "scrud-ui: not inlining %r in a live preview: it contains %r",
                name,
                closing_tag,
            )
            continue
        texts.append(text)
    return texts


def _title(resource_type) -> str:
    schema = getattr(resource_type.schema, "content", None)
    title = schema.get("title") if isinstance(schema, Mapping) else None
    return title if isinstance(title, str) and title else resource_type.slug
