from django.urls import path

from . import views

app_name = "scrud-studio"

urlpatterns = [
    path("", views.types, name="types"),
    path("types/<slug:slug>/", views.type_page, name="type"),
    path("types/<slug:slug>/sample", views.sample, name="sample"),
    path("types/<slug:slug>/preview", views.preview, name="preview"),
]
