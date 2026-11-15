"""`call_view`: the studio's in-process requests to the resource views, made
with the studio user's own rights."""

from unittest import mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory
from django.urls import reverse
from rest_framework.request import ForcedAuthentication
from rest_framework.views import APIView
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.studio.dispatch import call_view

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def studio_request(user, method="post"):
    """A studio request (as the studio's views get it) made by `user`."""
    request = getattr(RequestFactory(), method)("/studio/")
    request.user = user
    return request


def api_get(url):
    When.http.get(url, HTTP_ACCEPT="application/json")
    return Http.response


@pytest.mark.django_db
@test_context
def test_get_as_an_authorized_user(todo_list, todo_reader_login):
    response = call_view(
        studio_request(todo_reader_login), "todo-lists", "GET", pk=todo_list.pk
    )
    assert response.status_code == 200
    assert response.data == todo_list.content
    # Rendered, as JSON.
    assert response["Content-Type"] == "application/json"
    assert response.content.startswith(b"{")


@pytest.mark.django_db
@test_context
def test_get_as_an_unauthorized_user_answers_as_the_api(todo_list, login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    for pk in (todo_list.pk, 999999):
        api = api_get(reverse("todo-lists-detail", args=[pk]))
        response = call_view(studio_request(login_no_roles), "todo-lists", "GET", pk=pk)
        assert response.status_code == api.status_code
        assert response.status_code in (403, 404)
        assert response.data == api.json()


@pytest.mark.django_db
@test_context
def test_post_is_authorized_as_the_studio_user(todo_owner_login, login_no_roles):
    reverse("todo-lists-list")
    body = {"title": "New", "rbac_context": "project-a", "items": []}
    before = Resource.objects.count()
    refused = call_view(studio_request(login_no_roles), "todo-lists", "POST", data=body)
    assert refused.status_code == 403
    assert Resource.objects.count() == before
    created = call_view(
        studio_request(todo_owner_login), "todo-lists", "POST", data=body
    )
    assert created.status_code == 201
    assert Resource.objects.count() == before + 1
    assert created.data["title"] == "New"
    assert Resource.objects.filter(content__title="New").count() == 1


@pytest.mark.django_db
@test_context
def test_put_sends_if_match(todo_list, todo_owner_login):
    user = todo_owner_login
    body = {**todo_list.content, "title": "Renamed"}
    stale = call_view(
        studio_request(user, "put"),
        "todo-lists",
        "PUT",
        pk=todo_list.pk,
        data=body,
        if_match='"not-the-etag"',
    )
    assert stale.status_code == 412
    saved = call_view(
        studio_request(user, "put"),
        "todo-lists",
        "PUT",
        pk=todo_list.pk,
        data=body,
        if_match=f'"{todo_list.etag}"',
    )
    assert saved.status_code == 200
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Renamed"


@pytest.mark.django_db
@test_context
def test_internal_request_is_forced_json_and_csrf_exempt(todo_list, todo_reader_login):
    seen = []
    original = APIView.initial

    def spy(view, request, *args, **kwargs):
        original(view, request, *args, **kwargs)
        seen.append(
            (
                type(request.successful_authenticator),
                request.user,
                request._request._dont_enforce_csrf_checks,
                request.META["HTTP_ACCEPT"],
                request.META["HTTP_X_EXTRA"],
            )
        )

    with mock.patch.object(APIView, "initial", spy):
        call_view(
            studio_request(todo_reader_login),
            "todo-lists",
            "GET",
            pk=todo_list.pk,
            headers={"X-Extra": "yes"},
        )
    assert seen == [
        (ForcedAuthentication, todo_reader_login, True, "application/json", "yes")
    ]


@pytest.mark.django_db
@test_context
def test_unsafe_method_from_a_safe_request_is_refused(todo_owner_login):
    reverse("todo-lists-list")
    body = {"title": "New", "rbac_context": "project-a", "items": []}
    before = Resource.objects.count()
    for outer in ("get", "head", "options"):
        for method in ("POST", "put", "DELETE", "PATCH"):
            with pytest.raises(ImproperlyConfigured):
                call_view(
                    studio_request(todo_owner_login, outer),
                    "todo-lists",
                    method,
                    pk=None if method == "POST" else 1,
                    data=body,
                )
    assert Resource.objects.count() == before
    # A safe method from a safe request is fine.
    response = call_view(studio_request(todo_owner_login, "get"), "todo-lists", "GET")
    assert response.status_code == 200


@pytest.mark.django_db
@test_context
def test_anonymous_user_answers_as_the_api(todo_list):
    for method, pk in (("GET", todo_list.pk), ("GET", None)):
        url = (
            reverse("todo-lists-detail", args=[pk])
            if pk
            else reverse("todo-lists-list")
        )
        api = api_get(url)
        assert api.status_code in (401, 403)
        response = call_view(
            studio_request(AnonymousUser(), "get"), "todo-lists", method, pk=pk
        )
        assert response.status_code == api.status_code
        assert response.data == api.json()


@pytest.mark.django_db
@test_context
def test_pk_without_a_detail_url_is_the_apis_404(todo_reader_login):
    reverse("todo-lists-list")
    Given.http.force_authenticate(todo_reader_login)
    api = api_get("/api/todo-lists/a/b/")
    assert api.status_code == 404
    for pk in ("", "a/b"):
        response = call_view(
            studio_request(todo_reader_login, "get"), "todo-lists", "GET", pk=pk
        )
        assert response.status_code == 404
        assert response["Content-Type"] == "application/problem+json"
        assert response.data["status"] == 404
