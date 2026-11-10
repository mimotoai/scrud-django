from django.urls import path

from . import views

app_name = "scrud-studio"

urlpatterns = [
    path("", views.types, name="types"),
    path("types/<slug:slug>/", views.type_page, name="type"),
    path("types/<slug:slug>/sample", views.sample, name="sample"),
    path("types/<slug:slug>/preview", views.preview, name="preview"),
    path("types/<slug:slug>/explain", views.explain, name="explain"),
    path("save-item", views.save_item, name="save-item"),
    path("revert-item", views.revert_item, name="revert-item"),
    path("pull", views.pull, name="pull"),
    path("push", views.push, name="push"),
]
