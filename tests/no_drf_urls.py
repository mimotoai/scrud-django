"""The URLconf of the suite without DRF (``SCRUD_TEST_NO_DRF=1``): as
``tests.urls`` serves the test types' DRF routes at the root, outside any
namespace, this serves their ninja routes (`tests.ninja_api.PATTERNS`), with
the admin and the default ``scrud_django.urls`` (ninja's API in the namespace
``scrud``, and scoped-rbac's semantic documents) at ``api/``. It governs
``tests.models.Article`` as ``tests.urls`` does (the settings'
``SCRUD_WORKFLOW_ACTIONS`` has actions for it)."""

from django.contrib import admin
from django.urls import include, path

from scrud_django.core.transitions import govern

from . import ninja_api
from .models import Article
from .workflow_defaults import ARTICLE, article_contexts, article_state

govern(Article, ARTICLE, state_of=article_state, contexts_for=article_contexts)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("scrud_django.urls")),
    path("", include(ninja_api.PATTERNS)),
]
