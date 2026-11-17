"""The system checks of the types the UI app does not serve, when the project
includes it: those it refuses for their DRF-only authorization
(`check_drf_only_authorization`, ``scrud_django_ui.W003``), registered types
with no API route it can serve (`check_unrouted_types`,
``scrud_django_ui.W004``), and type slugs its own routes shadow
(`check_reserved_slugs`, ``scrud_django_ui.W005``)."""

from django.core import checks
from django.db import DatabaseError
from django.http import Http404
from django.urls import NoReverseMatch, get_resolver, reverse

DRF_ONLY_ID = "scrud_django_ui.W003"
UNROUTED_ID = "scrud_django_ui.W004"
RESERVED_SLUG_ID = "scrud_django_ui.W005"

# The suffixes of the names of a type's API routes (``<slug>-list``,
# ``<slug>-detail``), as registration names them.
ROUTE_SUFFIXES = ("-list", "-detail")


def _ui_included() -> bool:
    """Whether the project includes the UI app, the hooks its URLconf
    registers loaded first (importing it notes its registrations)."""
    from scrud_django.core.types import ensure_hooks_loaded
    from scrud_django.ui.branches.selection import ui_base_info

    ensure_hooks_loaded()
    try:
        ui_base_info()
    except NoReverseMatch:
        return False
    return True


def check_drf_only_authorization(app_configs, **kwargs):
    """A Warning (``scrud_django_ui.W003``) naming each type whose API routes
    are in the URLconf (`pages.routed`) but which the UI app refuses (a 404,
    as for a slug it does not serve) for the authorization of their views
    (`pages.refusal`): a custom ``viewset_class``'s (its ``IsAuthorized``
    subclasses and their compositions), or views not built by registration,
    which the core does not ask; with how to opt in. The types asked about
    are those the UI would be asked to serve (`asked_slugs`): registered in
    this process, or stored and routed in the URLconf. Nothing when the
    project does not include the UI app."""
    from scrud_django.ui import pages

    if not _ui_included():
        return []
    found = []
    for slug in asked_slugs():
        authorization = pages.refusal(slug)
        if authorization is None:
            continue
        found.append(
            checks.Warning(
                f"The UI app does not serve the type {slug!r} (its pages, "
                f"embeds and prefixes answer 404): {authorization}, and the "
                "UI reads through the core, which does not ask them.",
                hint=(
                    "Declare the type's rules to the core as both checks= and "
                    "object_checks= on its registration, or pass "
                    'ui_authorization="core" to its registration if the '
                    "core's checks (its grants and hooks) are complete for it "
                    "(for a type routed by hand, register those options with "
                    "scrud_django.core.types.register_type_options)."
                ),
                obj=slug,
                id=DRF_ONLY_ID,
            )
        )
    return found


def asked_slugs() -> list[str]:
    """The slugs of the types the UI may be asked to serve, sorted: those
    registered in this process (`scrud_django.core.types.type_slugs`), and
    those routed in the root URLconf (`routed_slugs`) whose type is stored
    (``registration.get_resource_type_for`` finds it, as the UI asks before
    serving one): a type routed by hand needs no registration in this
    process. A route named so whose slug names no stored type (another
    app's ``<name>-list``) is none; nor, when the database cannot be read,
    is any slug not registered in this process."""
    from scrud_django.core.types import type_slugs

    registered = set(type_slugs())
    return sorted(registered | stored_slugs(routed_slugs() - registered))


def stored_slugs(slugs) -> set[str]:
    """Those of `slugs` whose type is stored
    (``registration.get_resource_type_for`` finds it, as the UI asks before
    serving one); none when the database cannot be read."""
    from scrud_django.registration import get_resource_type_for

    found = set()
    for slug in sorted(slugs):
        try:
            get_resource_type_for(slug)
        except (Http404, DatabaseError):
            continue
        found.add(slug)
    return found


def routed_slugs() -> set[str]:
    """The slugs of the routes of the root URLconf outside any URL namespace
    named as a type's API routes are (``<slug>-list``, ``<slug>-detail``):
    the routes the UI reads a type's authorization from."""
    found = set()
    for name in get_resolver().reverse_dict:
        if not isinstance(name, str):
            continue
        for suffix in ROUTE_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                found.add(name[: -len(suffix)])
    return found


def check_unrouted_types(app_configs, **kwargs):
    """A Warning (``scrud_django_ui.W004``) naming each type registered in
    this process (`scrud_django.core.types.type_slugs`) and stored
    (`stored_slugs`: the UI would be asked to serve it) that has no API
    route the UI app can serve: no route named ``<slug>-list`` nor
    ``<slug>-detail`` outside a URL namespace, as registration names them.
    Its pages answer 404, as for a slug the UI does not serve. scrud's ninja
    ``api()`` routes a type only in its URL namespace (``urls_namespace``),
    which the UI does not read yet (follow-up ``ui-ninja-routes``). Nothing
    when the project does not include the UI app."""
    from scrud_django.core.types import type_slugs

    if not _ui_included():
        return []
    unrouted = {
        slug
        for slug in type_slugs()
        if not _reverses(f"{slug}-list") and not _reverses(f"{slug}-detail", "key")
    }
    found = []
    for slug in sorted(stored_slugs(unrouted)):
        found.append(
            checks.Warning(
                f"The UI app does not serve the type {slug!r} (its pages, "
                "embeds and prefixes answer 404): it has no API route named "
                f"{slug + '-list'!r} or {slug + '-detail'!r} outside a URL "
                "namespace, which is where the UI looks for a type's routes.",
                hint=(
                    "Route the type with registration (resource_type_urls, "
                    "json_resource_type, computed_resource_type) without a "
                    "namespace. The routes of scrud's ninja api() are in its "
                    "urls_namespace: the UI does not serve ninja-routed types "
                    "yet."
                ),
                obj=slug,
                id=UNROUTED_ID,
            )
        )
    return found


def _reverses(name, *args) -> bool:
    try:
        reverse(name, args=args)
    except NoReverseMatch:
        return False
    return True


def check_reserved_slugs(app_configs, **kwargs):
    """A Warning (``scrud_django_ui.W005``) naming each type registered in
    this process (`scrud_django.core.types.type_slugs`) whose slug is the
    first path segment of one of the UI app's own routes
    (`scrud_django.ui.reserved_slugs`: ``ui-css-files``, ``ui-js-files``,
    and ``studio`` with the studio installed), which the UI app routes
    before its pages: the type's page URLs (``<slug>/``, ``<slug>/<pk>/``)
    are in theirs. Nothing when the project does not include the UI app."""
    from scrud_django.core.types import type_slugs
    from scrud_django.ui import reserved_slugs

    if not _ui_included():
        return []
    reserved = reserved_slugs()
    return [
        checks.Warning(
            f"The type {slug!r} has a slug the UI app routes itself "
            f"({slug + '/'!r}): its pages' URLs are among the UI app's own "
            "routes, which answer before them.",
            hint=(
                f"Register the type under another slug (reserved by the UI: "
                f"{', '.join(sorted(reserved))})."
            ),
            obj=slug,
            id=RESERVED_SLUG_ID,
        )
        for slug in type_slugs()
        if slug in reserved
    ]
