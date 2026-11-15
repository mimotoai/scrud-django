"""The studio links: server chrome on the pages of the UI's types and the
studio's computed types (`renderer._page` adds them when the studio app is
installed), so that a broken studio can always be reached in safe mode or in
the bootstrap studio."""

from django.urls import NoReverseMatch, get_script_prefix, reverse
from django.utils.encoding import escape_uri_path
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from scrud_django.ui.branches import (
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_CSS_TYPE_URI,
    UI_JS_TYPE_URI,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.branches.effective import SAFE_PARAMETER
from scrud_django.ui.branches.selection import Mount, api_base
from scrud_django.ui.branches.store import branches
from scrud_django.ui.renderer import MODE_PARAMETER, SCOPE_PARAMETER, VIEW_MODE
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

# The scope of the self-hosted studio's pages.
STUDIO_SCOPE = "studio"

UI_TYPE_URIS = frozenset(
    {
        UI_COMPONENT_TYPE_URI,
        UI_STYLESHEET_TYPE_URI,
        UI_BRANCH_TYPE_URI,
        UI_BRANCH_VERSION_TYPE_URI,
        UI_PUSH_TYPE_URI,
        UI_CSS_TYPE_URI,
        UI_JS_TYPE_URI,
    }
)


def has_studio_links(resource_type) -> bool:
    """Whether the pages of `resource_type` (a `ResourceType`, or ``None``)
    carry the studio links: one of the UI's seven types, or a studio
    computed type."""
    from scrud_django.ui.studio.computed import STUDIO_COMPUTED_TYPES, TYPE_URI_BASE

    type_uri = getattr(resource_type, "type_uri", None)
    return type_uri in UI_TYPE_URIS or type_uri in {
        TYPE_URI_BASE + name for name, _slug, _viewset in STUDIO_COMPUTED_TYPES
    }


def studio_links(request) -> SafeString:
    """``<nav class="scrud-studio-links">`` with, in order, a link to the
    self-hosted studio (`studio_href`) when there is one, to the current
    URL in safe mode, and to the bootstrap studio when its URLs are
    included."""
    links = []
    studio = studio_href()
    if studio is not None:
        links.append((studio, "Studio"))
    links.append((safe_href(request), "Safe mode"))
    try:
        links.append((reverse("scrud-studio:types"), "Bootstrap studio"))
    except NoReverseMatch:
        pass  # The project does not include the studio's URLs.
    return format_html(
        '<nav class="scrud-studio-links">{}</nav>',
        format_html_join("", '<a href="{}">{}</a>', links),
    )


def studio_href() -> str | None:
    """The workbench of the studio root (`studio.studio_branch`) on itself:
    its own ``ui-branches`` detail under its prefix, in the scope
    ``studio``, in view mode (the workbench is a view of its branch: a user
    who may PUT it would otherwise get the branch's edit form). ``None``
    when the setting is unset, names no stored branch, or that branch has
    no prefix."""
    from scrud_django.ui.studio import studio_branch

    name = studio_branch()
    branch = branches().get(name) if name is not None else None
    if branch is None or not branch.prefix or branch.pk is None:
        return None
    mount = Mount(branch.prefix, branch.name, api_base(), get_script_prefix())
    detail = reverse(f"{UI_BRANCH_SLUG}-detail", args=[branch.pk])
    return (
        f"{mount.href(detail)}?{SCOPE_PARAMETER}={STUDIO_SCOPE}"
        f"&{MODE_PARAMETER}={VIEW_MODE}"
    )


def safe_href(request) -> str:
    """The request's URL with ``ui-safe=1`` (its other parameters kept)."""
    query = request.GET.copy()
    query[SAFE_PARAMETER] = "1"
    return escape_uri_path(request.path) + "?" + query.urlencode()
