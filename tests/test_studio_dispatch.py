"""`answers.write`: the studio's reads and writes of resources, made through
the core with the studio user's own rights."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.studio.answers import as_response, write

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
    response = write(
        studio_request(todo_reader_login), "todo-lists", "GET", pk=todo_list.pk
    )
    assert response.status == 200
    assert response.content == todo_list.content
    # Answered as JSON.
    rendered = as_response(response, saved_trigger=False)
    assert rendered["Content-Type"] == "application/json"
    assert rendered.content.startswith(b"{")
    assert rendered["ETag"] == f'"{todo_list.etag}"'
    assert "HX-Trigger" not in rendered


@pytest.mark.django_db
@test_context
def test_get_as_an_unauthorized_user_answers_as_the_api(todo_list, login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    for pk in (todo_list.pk, 999999):
        api = api_get(reverse("todo-lists-detail", args=[pk]))
        response = write(studio_request(login_no_roles), "todo-lists", "GET", pk=pk)
        assert response.status == api.status_code
        assert response.status in (403, 404)
        assert response.body == api.json()


@pytest.mark.django_db
@test_context
def test_post_is_authorized_as_the_studio_user(todo_owner_login, login_no_roles):
    reverse("todo-lists-list")
    body = {"title": "New", "rbac_context": "project-a", "items": []}
    before = Resource.objects.count()
    refused = write(studio_request(login_no_roles), "todo-lists", "POST", body=body)
    assert refused.status == 403
    assert Resource.objects.count() == before
    created = write(studio_request(todo_owner_login), "todo-lists", "POST", body=body)
    assert created.status == 201
    assert Resource.objects.count() == before + 1
    assert created.content["title"] == "New"
    assert Resource.objects.filter(content__title="New").count() == 1


@pytest.mark.django_db
@test_context
def test_put_sends_if_match(todo_list, todo_owner_login):
    user = todo_owner_login
    body = {**todo_list.content, "title": "Renamed"}
    stale = write(
        studio_request(user, "put"),
        "todo-lists",
        "PUT",
        pk=todo_list.pk,
        body=body,
        if_match='"not-the-etag"',
    )
    assert stale.status == 412
    saved = write(
        studio_request(user, "put"),
        "todo-lists",
        "PUT",
        pk=todo_list.pk,
        body=body,
        if_match=f'"{todo_list.etag}"',
    )
    assert saved.status == 200
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Renamed"


@pytest.mark.django_db
@test_context
def test_unsafe_method_from_a_safe_request_is_refused(todo_owner_login, todo_list):
    reverse("todo-lists-list")
    body = {"title": "New", "rbac_context": "project-a", "items": []}
    before = Resource.objects.count()
    for outer in ("get", "head", "options"):
        for method in ("POST", "put", "DELETE", "PATCH"):
            with pytest.raises(ImproperlyConfigured):
                write(
                    studio_request(todo_owner_login, outer),
                    "todo-lists",
                    method,
                    pk=None if method == "POST" else 1,
                    body=body,
                )
    assert Resource.objects.count() == before
    # A safe method from a safe request is fine.
    response = write(
        studio_request(todo_owner_login, "get"), "todo-lists", "GET", pk=todo_list.pk
    )
    assert response.status == 200


@pytest.mark.django_db
@test_context
def test_anonymous_user_answers_as_the_api(todo_list):
    for method, pk in (("GET", todo_list.pk),):
        url = (
            reverse("todo-lists-detail", args=[pk])
            if pk
            else reverse("todo-lists-list")
        )
        api = api_get(url)
        assert api.status_code in (401, 403)
        response = write(
            studio_request(AnonymousUser(), "get"), "todo-lists", method, pk=pk
        )
        assert response.status == api.status_code
        assert response.body == api.json()


@pytest.mark.django_db
@test_context
def test_pk_without_a_detail_url_is_the_apis_404(todo_reader_login):
    reverse("todo-lists-list")
    Given.http.force_authenticate(todo_reader_login)
    api = api_get("/api/todo-lists/a/b/")
    assert api.status_code == 404
    for pk in ("", "a/b"):
        response = write(
            studio_request(todo_reader_login, "get"), "todo-lists", "GET", pk=pk
        )
        assert response.status == 404
        assert as_response(response, saved_trigger=False)["Content-Type"] == (
            "application/problem+json"
        )
        assert response.body["status"] == 404


@pytest.mark.django_db
@test_context
def test_failed_preconditions_answer_as_the_api(todo_list, todo_owner_login):
    body = {**todo_list.content, "title": "Renamed"}
    Given.http.force_authenticate(todo_owner_login)
    for headers, if_match in (({}, None), ({"HTTP_IF_MATCH": '"stale"'}, '"stale"')):
        When.http.put(
            reverse("todo-lists-detail", args=[todo_list.pk]),
            data=body,
            format="json",
            **headers,
        )
        api = Http.response
        answer = write(
            studio_request(todo_owner_login, "put"),
            "todo-lists",
            "PUT",
            pk=todo_list.pk,
            body=body,
            if_match=if_match,
        )
        assert api.status_code in (412, 428)
        assert answer.status == api.status_code
        assert answer.body == api.json()
        assert (
            as_response(answer, saved_trigger=True)["Content-Type"]
            == (api["Content-Type"])
        )
