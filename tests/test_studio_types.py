"""The studio app: the type browser and the page shell."""

import re
from unittest import mock

import pytest
from django.urls import reverse
from safetydance import test_context  # noqa: F401
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.studio.apps import CHECK_ID_UI_MISSING, check_ui_installed

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_http import html


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def slugs(page):
    return re.findall(r"<strong>([^<]+)</strong>", page)


@pytest.mark.django_db
@test_context
def test_studio_needs_login():
    When.http.get(reverse("scrud-studio:types"))
    Then.http.status_code_is(401)
    When.http.get(reverse("scrud-studio:type", args=["todo-lists"]))
    Then.http.status_code_is(401)


@pytest.mark.django_db
@test_context
def test_types_lists_every_registered_type_and_searches(login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    When.http.get(reverse("scrud-studio:types"))
    Then.http.status_code_is(200)
    found = slugs(html())
    assert "todo-lists" in found and "ui-components" in found
    assert found == sorted(found)

    When.http.get(reverse("scrud-studio:types"), {"q": "TODO-L"})
    Then.http.status_code_is(200)
    assert slugs(html()) == ["todo-lists"]

    When.http.get(reverse("scrud-studio:types"), {"q": "todo"})
    assert {"todo-lists", "todos"} <= set(slugs(html()))

    When.http.get(reverse("scrud-studio:types"), {"q": "zzz-nothing"})
    assert slugs(html()) == []


@pytest.mark.django_db
@test_context
def test_type_page_shell(login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    When.http.get(reverse("scrud-studio:type", args=["todo-lists"]))
    Then.http.status_code_is(200)
    assert "todo-lists" in html()
    When.http.get(reverse("scrud-studio:type", args=["no-such-type"]))
    Then.http.status_code_is(404)


@pytest.mark.django_db
@test_context
def test_studio_pages_have_no_inline_scripts(login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    for url in (
        reverse("scrud-studio:types"),
        reverse("scrud-studio:type", args=["todo-lists"]),
    ):
        When.http.get(url)
        page = html()
        assert not re.search(r"<script(?![^>]*\ssrc=)", page)
        assert not re.search(r"<[^>]+\son[a-z]+\s*=", page, re.I)
        assert "scrud_ui/htmx.min.js" in page
        assert "scrud_studio/studio.js" in page
        assert "scrud_studio/studio.css" in page
        assert '<meta name="csrf-token"' in page


def test_studio_requires_ui_app():
    assert check_ui_installed() == []
    with mock.patch("django.apps.apps.is_installed", return_value=False):
        errors = check_ui_installed()
    assert [e.id for e in errors] == [CHECK_ID_UI_MISSING]
    assert CHECK_ID_UI_MISSING == "scrud_django_ui_studio.E001"
