from django.urls import path

from . import views

app_name = "scrud-studio"

urlpatterns = [
    path("", views.types, name="types"),
    path("types/<slug:slug>/", views.type_page, name="type"),
]
