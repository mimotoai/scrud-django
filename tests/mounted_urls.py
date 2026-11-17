"""The test URLconf with the API under ``/api/`` (``tests.urls`` serves it at
``/``, where no prefix applies), so that branches can be published at other
prefixes (``tests/test_ui_branch_prefixes.py``), and the UI app at ``/``
(`scrud_django.ui.urls`, the studio at ``/studio/`` with it), last, as a
project includes it (``tests/test_ui_app.py``)."""

from django.http import HttpResponse
from django.urls import include, path

from . import urls


def shop_todo_lists(request):
    """A project page at ``/shop/todo-lists/``, which a branch published at
    ``/shop/`` would shadow."""
    return HttpResponse("The shop's to-do lists")


urlpatterns = [
    path("api/", include(urls.urlpatterns)),
    path("shop/todo-lists/", shop_todo_lists, name="shop-todo-lists"),
    path("", include("scrud_django.ui.urls")),
]
