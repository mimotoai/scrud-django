"""The bootstrap studio: an authoring UI on top of `scrud_django.ui`, and the
packaged UI layer (``studio:``, `scrud_django.ui.studio.layer`) of the studio
the UI hosts itself."""


def studio_branch() -> str | None:
    """The name of the site's published studio root, the branch whose prefix
    serves the self-hosted studio: the setting ``SCRUD_UI_STUDIO_BRANCH``
    (default ``None``). A value that is not a non-empty string is ``None``."""
    from django.conf import settings

    name = getattr(settings, "SCRUD_UI_STUDIO_BRANCH", None)
    return name if isinstance(name, str) and name else None


def studio_layer_enabled() -> bool:
    """Whether the studio's packaged UI layer (``studio:``) is installed with
    the app: the setting ``SCRUD_UI_STUDIO_LAYER`` (default ``True``), any
    false value turning it off."""
    from django.conf import settings

    return bool(getattr(settings, "SCRUD_UI_STUDIO_LAYER", True))
