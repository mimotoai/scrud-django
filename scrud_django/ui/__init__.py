"""Server-rendered UI components, stored as resources. Enabled by adding
``scrud_django.ui`` to ``INSTALLED_APPS``."""

from django.apps import apps as django_apps

UI_APP_NAME = "scrud_django.ui"

#: The URL namespace of the UI app's routes (`scrud_django.ui.urls`).
UI_NAMESPACE = "scrud_ui"

#: The UI app's install name of the studio, routed at `STUDIO_SEGMENT`.
STUDIO_APP_NAME = "scrud_django.ui.studio"

#: The first path segments of the UI app's own routes beside its pages
#: (`scrud_django.ui.urls`): the CSS and JS files', and the studio's (when
#: its app is installed). A type with one of them as its slug has its pages'
#: URLs in theirs (`reserved_slugs`; the check ``scrud_django_ui.W005``).
CSS_FILES_SEGMENT = "ui-css-files"
JS_FILES_SEGMENT = "ui-js-files"
STUDIO_SEGMENT = "studio"


def is_enabled() -> bool:
    """Whether the project installed ``scrud_django.ui``."""
    return django_apps.is_installed(UI_APP_NAME)


def reserved_slugs() -> frozenset[str]:
    """The type slugs the UI app's own routes shadow: their first path
    segments (`CSS_FILES_SEGMENT`, `JS_FILES_SEGMENT`, and `STUDIO_SEGMENT`
    when the studio is installed)."""
    found = {CSS_FILES_SEGMENT, JS_FILES_SEGMENT}
    if django_apps.is_installed(STUDIO_APP_NAME):
        found.add(STUDIO_SEGMENT)
    return frozenset(found)
