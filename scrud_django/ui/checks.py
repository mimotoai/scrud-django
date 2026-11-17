"""The system check of the types the UI refuses for their DRF-only
authorization (`check_drf_only_authorization`)."""

from django.core import checks
from django.urls import NoReverseMatch

DRF_ONLY_ID = "scrud_django_ui.W003"


def check_drf_only_authorization(app_configs, **kwargs):
    """A Warning (``scrud_django_ui.W003``) naming each type registered in
    this process (`scrud_django.core.types.type_slugs`) whose API routes are
    in the URLconf (`pages.routed`) but which the UI app refuses (a 404, as
    for a slug it does not serve) for the authorization of their views
    (`pages.refusal`): a custom ``viewset_class``'s (its ``IsAuthorized``
    subclasses and their compositions), or views not built by registration,
    which the core does not ask; with how to opt in. Nothing when the
    project does not include the UI app. The URLconf is imported first, so
    that its registrations are noted."""
    from scrud_django.core.types import ensure_hooks_loaded, type_slugs
    from scrud_django.ui import pages
    from scrud_django.ui.branches.selection import ui_base_info

    ensure_hooks_loaded()
    try:
        ui_base_info()
    except NoReverseMatch:  # the project does not include the UI app
        return []
    found = []
    for slug in type_slugs():
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
