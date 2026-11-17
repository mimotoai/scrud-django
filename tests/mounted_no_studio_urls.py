"""`tests.mounted_urls` without the studio's URLs: the API under ``/api/`` and
the UI app's pages and files at ``/``, as a project that installs the studio
app but does not include its URLs (``scrud_django.ui.urls`` includes them
when the app is installed, so the patterns are the UI's own)."""

from django.urls import include, path

from scrud_django.ui import UI_NAMESPACE
from scrud_django.ui.urls import ui_urlpatterns

from . import urls

urlpatterns = [
    path("api/", include(urls.urlpatterns)),
    path("", include((ui_urlpatterns, UI_NAMESPACE))),
]
