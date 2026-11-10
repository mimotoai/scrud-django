"""The studio's previews: a type's sample, or an instance, rendered by the
renderer on a selected branch in preview mode (no write attributes, each node
marked with its JSON Pointer), and the CSS links of that branch."""

from urllib.parse import urlencode

from django.urls import get_script_prefix, reverse
from django.utils.safestring import SafeString

from scrud_django.ui.branches.css import CSS_FILE_URL_NAME, css_files
from scrud_django.ui.branches.effective import (
    BRANCH_PARAMETER,
    Selection,
    using_selection,
)
from scrud_django.ui.branches.selection import Mount, api_base, selection_value
from scrud_django.ui.branches.store import BranchChainError, branches, published_root
from scrud_django.ui.render import render_resource
from scrud_django.ui.studio.sample import sample_for

# What a sample may do: it is shown as editable, never deletable.
SAMPLE_DECISIONS = {"GET": "allowed", "PUT": "allowed", "DELETE": "denied"}


def sample_of(resource_type, context: str = "") -> object:
    """The sample instance of `resource_type` (`sample_for` its schema), in
    `context`: its ``rbac_context`` when it is an object."""
    schema = getattr(resource_type.schema, "content", None)
    sample = sample_for(schema if isinstance(schema, dict) else {})
    if isinstance(sample, dict):
        sample["rbac_context"] = context
    return sample


def render_preview(
    request,
    resource_type,
    content,
    *,
    selection: Selection,
    mode: str | None,
    scope_chain,
    contexts,
    decisions=None,
    href: str,
) -> SafeString:
    """`content` (of `resource_type`, at `href`) rendered for the user of
    `request` as on `selection`'s branch (which must be `selectable`), in
    preview mode: `render_resource` with ``preview=True``. `contexts` are
    those whose stylesheets apply; `decisions` default to the user's on
    `content`.

    The rules memo (`cascade.rules_memo`) is keyed without the selection:
    never render two branches' previews inside one ``rules_memo()``, or the
    second gets the first's rules."""
    with using_selection(selection):
        return render_resource(
            content,
            resource_type=resource_type,
            href=href,
            etag=None,
            request=request,
            scope_chain=tuple(scope_chain),
            mode=mode,
            decisions=decisions,
            contexts=list(contexts) if contexts is not None else None,
            preview=True,
        )


def preview_css_urls(selection: Selection) -> list[str]:
    """The URLs of the CSS files effective on `selection`'s branch (which
    must be `selectable`), each selecting it (`BRANCH_PARAMETER`), so that
    the file is served from that branch whatever a cookie browses: under the
    prefix of its published root, where the CSS view serves its tree; at the
    API's URLs for ``main``'s."""
    urls = [
        reverse(CSS_FILE_URL_NAME, args=[name, etag])
        for name, etag, _text in css_files(selection)
    ]
    try:
        root = published_root(selection.branch, branches())
    except BranchChainError:  # defensive: a selectable branch has a root
        root = None
    if root is not None and root.prefix:
        mount = Mount(root.prefix, root.name, api_base(), get_script_prefix())
        urls = [mount.href(url) for url in urls]
    query = urlencode({BRANCH_PARAMETER: selection_value(selection)})
    return [f"{url}?{query}" for url in urls]
