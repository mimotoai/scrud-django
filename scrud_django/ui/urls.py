"""The UI app's URLconf: the pages of the types the API serves, their CSS
and JS files, and the studio. Include it last, at the root, after the API
and the project's own URLs, which it never claims (a slug it does not serve
does not match: the URLconf's later patterns, or Django's 404, answer)::

    urlpatterns = [
        path("api/", include("scrud_django.urls")),
        ...,
        path("", include("scrud_django.ui.urls")),
    ]

Its routes are in the URL namespace ``scrud_ui``
(``scrud_django.ui.UI_NAMESPACE``):

- ``ui-css-files/<name>/<etag>.css`` (``ui-css-file``) and
  ``ui-js-files/<name>/<etag>.js`` (``ui-js-file``), the CSS and JS items'
  files (`file_views`);
- ``<slug>/`` (``list``), the list of a type's resources, and
  ``<slug>/<pk>/`` (``detail``), one of them, or a computed type's content
  for a key (`pages.list_page`, `pages.item_page`), for a `slug` the UI
  serves (`UISlugConverter`).

The studio (``scrud_django.ui.studio.urls``), when its app is installed, is
at ``studio/``, in its own namespace, ``scrud-studio``: it is included
beside the ``scrud_ui`` routes, not in their namespace (an app namespace
would nest it, as ``scrud_ui:scrud-studio``)."""

from django.apps import apps
from django.urls import include, path, register_converter

from scrud_django.ui import UI_NAMESPACE, pages
from scrud_django.ui.branches.file_urls import CSS_FILE_URL_NAME, JS_FILE_URL_NAME
from scrud_django.ui.branches.file_views import css_file, js_file

STUDIO_APP = "scrud_django.ui.studio"


class UISlugConverter:
    """The slug of a type the UI serves, with its type (`pages.served`, a
    `pages.ServedSlug`): any other path segment is no match
    (``ValueError``), so the URLconf's later patterns still match it."""

    regex = "[-a-zA-Z0-9_]+"

    def to_python(self, value):
        found = pages.served(value)
        if found is None:
            raise ValueError(f"the UI serves no type {value!r}")
        return found

    def to_url(self, value):
        return str(value)


register_converter(UISlugConverter, "ui_slug")

ui_urlpatterns = [
    path("ui-css-files/<str:name>/<str:etag>.css", css_file, name=CSS_FILE_URL_NAME),
    path("ui-js-files/<str:name>/<str:etag>.js", js_file, name=JS_FILE_URL_NAME),
    path("<ui_slug:slug>/", pages.list_page, name="list"),
    path("<ui_slug:slug>/<str:pk>/", pages.item_page, name="detail"),
]

urlpatterns = []
if apps.is_installed(STUDIO_APP):
    urlpatterns.append(path("studio/", include("scrud_django.ui.studio.urls")))
urlpatterns.append(path("", include((ui_urlpatterns, UI_NAMESPACE))))
