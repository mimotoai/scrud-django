from django.urls import include, path
from rest_framework import routers

from .views import ResourceSearchViewSet

router = routers.DefaultRouter()
router.register("resources", ResourceSearchViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
