"""The test URLconf with the API under ``/api/`` (``tests.urls`` serves it at
``/``, where no prefix applies; without DRF, ``SCRUD_TEST_NO_DRF``, the test
types' ninja routes, `tests.ninja_api.PATTERNS`), so that branches can be published at other
prefixes (``tests/test_ui_branch_prefixes.py``), and the UI app at ``/``
(`scrud_django.ui.urls`, the studio at ``/studio/`` with it), last, as a
project includes it (``tests/test_ui_app.py``)."""

from django.http import HttpResponse
from django.urls import include, path

from .settings import NO_DRF

if NO_DRF:
    from . import ninja_api

    # And scoped-rbac's semantic documents, which tests.urls has
    # (scrud_django.drf.urls).
    api_patterns = [
        path("", include(ninja_api.PATTERNS)),
        path("semantic/", include("scoped_rbac.semantic_urls")),
    ]
else:
    from . import urls

    api_patterns = urls.urlpatterns


def shop_todo_lists(request):
    """A project page at ``/shop/todo-lists/``, which a branch published at
    ``/shop/`` would shadow."""
    return HttpResponse("The shop's to-do lists")


urlpatterns = [
    path("api/", include(api_patterns)),
    path("shop/todo-lists/", shop_todo_lists, name="shop-todo-lists"),
    path("", include("scrud_django.ui.urls")),
]
