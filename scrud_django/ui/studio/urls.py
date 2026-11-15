from django.urls import path

from . import views

app_name = "scrud-studio"

urlpatterns = [
    path("", views.types, name="types"),
    path("types/<slug:slug>/", views.type_page, name="type"),
    path("types/<slug:slug>/strip", views.strip, name="strip"),
    path("types/<slug:slug>/sample", views.sample, name="sample"),
    path("types/<slug:slug>/preview", views.preview, name="preview"),
    path("types/<slug:slug>/explain", views.explain, name="explain"),
    path(
        "types/<slug:slug>/new-component/options",
        views.new_component_options,
        name="new-component-options",
    ),
    path("live-preview", views.live_preview, name="live-preview"),
    path("new-component", views.new_component, name="new-component"),
    path("item", views.item, name="item"),
    path("save-item", views.save_item, name="save-item"),
    path("revert-item", views.revert_item, name="revert-item"),
    path("pull", views.pull, name="pull"),
    path("push", views.push, name="push"),
]
