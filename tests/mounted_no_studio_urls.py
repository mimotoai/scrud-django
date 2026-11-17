"""`tests.mounted_urls` without the studio's URLs: the API under ``/api/`` and
the UI app's pages and files at ``/``, as a project that installs the studio
app but does not include its URLs (``scrud_django.ui.urls`` includes them
when the app is installed, so the patterns are the UI's own)."""

from django.urls import include, path

from scrud_django.ui import UI_NAMESPACE
from scrud_django.ui.urls import ui_urlpatterns

from .mounted_urls import api_patterns

urlpatterns = [
    path("api/", include(api_patterns)),
    path("", include((ui_urlpatterns, UI_NAMESPACE))),
]
