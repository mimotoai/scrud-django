"""The system check of the types the UI refuses for their DRF-only
authorization (`check_drf_only_authorization`)."""

from django.core import checks
from django.urls import NoReverseMatch

DRF_ONLY_ID = "scrud_django_ui.W003"


def check_drf_only_authorization(app_configs, **kwargs):
    """A Warning (``scrud_django_ui.W003``) naming each type the UI app
    would serve pages of (its API routes are in the URLconf,
    `pages.routed`) but refuses (a 404, as for a slug it does not serve)
    because its DRF route holds authorization the core does not ask
    (`scrud_django.core.types.drf_only_authorization`: a custom
    ``viewset_class``, with its ``IsAuthorized`` subclasses and their
    compositions), with how to opt in. Nothing when the project does not
    include the UI app. The URLconf is imported first, so that its
    registrations are recorded."""
    from scrud_django.core.types import (
        drf_only_authorization,
        drf_only_slugs,
        ensure_hooks_loaded,
    )
    from scrud_django.ui import pages
    from scrud_django.ui.branches.selection import ui_base_info

    ensure_hooks_loaded()
    try:
        ui_base_info()
    except NoReverseMatch:  # the project does not include the UI app
        return []
    found = []
    for slug in drf_only_slugs():
        authorization = drf_only_authorization(slug)
        if authorization is None or not pages.routed(slug):
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
                    "core's checks (its grants and hooks) are complete for it."
                ),
                obj=slug,
                id=DRF_ONLY_ID,
            )
        )
    return found
