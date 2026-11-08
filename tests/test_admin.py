"""The Django admin shows scrud-django's models to superusers only: a write
there bypasses scoped-rbac, the workflow and JSON Schema validation, so
staff with Django's model permissions must not see them. Its deletes still
run the workflow actions."""

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .workflow_actions import actions

MODELS = [("resource", Resource), ("resourcetype", ResourceType)]


def logged_in(**flags):
    user = UserFactory(is_staff=True, **flags)
    # Every model permission Django has for scrud_django's models.
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="scrud_django")
    )
    client = Client()
    client.force_login(user)
    return client


def admin_urls(resource):
    for name, model in MODELS:
        obj = resource if model is Resource else resource.resource_type
        yield f"admin:scrud_django_{name}_changelist", ()
        yield f"admin:scrud_django_{name}_add", ()
        yield f"admin:scrud_django_{name}_change", (obj.pk,)
        yield f"admin:scrud_django_{name}_delete", (obj.pk,)
        yield f"admin:scrud_django_{name}_history", (obj.pk,)


@pytest.mark.django_db
def test_staff_who_are_not_superusers_see_no_scrud_model():
    resource = ResourceRegistration.register({"name": "x"}, "partner-profiles")
    client = logged_in()
    index = client.get(reverse("admin:index"))
    assert index.status_code == 200
    assert "scrud_django" not in [app["app_label"] for app in index.context["app_list"]]
    assert (
        client.get(reverse("admin:app_list", args=["scrud_django"])).status_code == 404
    )
    for url_name, args in admin_urls(resource):
        response = client.get(reverse(url_name, args=args))
        assert response.status_code == 403, url_name
    content = dict(resource.content)
    response = client.post(
        reverse("admin:scrud_django_resource_delete", args=[resource.pk]),
        {"post": "yes"},
    )
    assert response.status_code == 403
    stored = Resource.objects.get(pk=resource.pk)
    assert stored.content == content


@pytest.mark.django_db
def test_superusers_see_and_may_change_scrud_models():
    resource = ResourceRegistration.register({"name": "x"}, "partner-profiles")
    client = logged_in(is_superuser=True)
    index = client.get(reverse("admin:index"))
    assert "scrud_django" in [app["app_label"] for app in index.context["app_list"]]
    for url_name, args in admin_urls(resource):
        response = client.get(reverse(url_name, args=args))
        assert response.status_code == 200, url_name
    response = client.post(
        reverse("admin:scrud_django_resource_delete", args=[resource.pk]),
        {"post": "yes"},
    )
    assert response.status_code == 302
    assert not Resource.objects.filter(pk=resource.pk).exists()


@pytest.mark.django_db
def test_the_admins_delete_selected_runs_on_delete_for_each_resource(
    monkeypatch, django_capture_on_commit_callbacks
):
    reverse("todos-list")  # Registers the test types.
    todos = [ResourceRegistration.register({"title": "t"}, "todos") for _ in range(2)]
    kept = ResourceRegistration.register({"title": "t"}, "todos")
    ran = []

    def on_delete(sender, instance, pk=None, **kwargs):
        ran.append((pk, kwargs["resource_type_uri"], kwargs["action"]))

    monkeypatch.setitem(actions["tests://ToDo"], "on_delete", on_delete)
    client = logged_in(is_superuser=True)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("admin:scrud_django_resource_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [t.pk for t in todos],
                "post": "yes",
            },
        )
    assert response.status_code == 302
    assert list(Resource.objects.filter(pk__in=[t.pk for t in todos])) == []
    assert Resource.objects.filter(pk=kept.pk).exists()
    assert sorted(ran) == sorted((t.pk, "tests://ToDo", "DELETE") for t in todos)
