"""The UI component and UI stylesheet resource types."""

import importlib

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import NoReverseMatch, clear_url_caches, reverse
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

COMPONENT_TYPE = "http://api.openteams.com/json-ld/UIComponent"
STYLESHEET_TYPE = "http://api.openteams.com/json-ld/UIStylesheet"


def component(**overrides):
    return {
        "name": "todo-row",
        "engine": "django",
        "mode": "view",
        "template": "<li>{{ value.text }}</li>",
        **overrides,
    }


def stylesheet(**overrides):
    return {
        "name": "theme",
        "rules": [{"select": "array", "component": "todo-row"}],
        **overrides,
    }


@pytest.mark.django_db
@test_context
def test_ui_types_registered_and_listed_in_services(admin_login):
    reverse("ui-components-list")  # the URLconf registers the types
    Given.http.force_authenticate(admin_login)
    When.http.get(reverse("services"))
    Then.http.status_code_is(200)
    listed = str(Http.response.json())
    assert COMPONENT_TYPE in listed
    assert STYLESHEET_TYPE in listed


@pytest.mark.django_db
@test_context
def test_component_crud_as_superuser(admin_login):
    Given.http.force_authenticate(admin_login)
    When.http.post(reverse("ui-components-list"), data=component(), format="json")
    Then.http.status_code_is(201)
    location = Http.response["Location"]
    When.http.get(location)
    Then.http.status_code_is(200)
    assert Http.response.json()["name"] == "todo-row"


@pytest.mark.django_db
@test_context
def test_component_outside_default_context_is_400(admin_login):
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("ui-components-list"),
        data=component(rbac_context="project-a"),
        format="json",
    )
    Then.http.status_code_is(400)


@pytest.mark.django_db
@test_context
def test_component_reserved_name_is_400(admin_login):
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("ui-components-list"),
        data=component(name="scrud:object"),
        format="json",
    )
    Then.http.status_code_is(400)


@pytest.mark.django_db
@test_context
def test_component_duplicate_name_is_400(admin_login):
    Given.http.force_authenticate(admin_login)
    When.http.post(reverse("ui-components-list"), data=component(), format="json")
    Then.http.status_code_is(201)
    location = Http.response["Location"]
    etag = Http.response["ETag"]
    When.http.post(
        reverse("ui-components-list"),
        data=component(template="<b>x</b>"),
        format="json",
    )
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("ui-component-name-taken")
    # An update that keeps its own name is not a duplicate.
    When.http.put(
        location,
        data=component(template="<b>y</b>"),
        format="json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(200)
    # Renaming onto another component's name is.
    When.http.post(
        reverse("ui-components-list"), data=component(name="other"), format="json"
    )
    Then.http.status_code_is(201)
    other_etag = Http.response["ETag"]
    other_location = Http.response["Location"]
    When.http.put(
        other_location,
        data=component(),
        format="json",
        HTTP_IF_MATCH=other_etag,
    )
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("ui-component-name-taken")


MALFORMED = [
    ("ui-stylesheets", stylesheet(rules=5)),
    ("ui-stylesheets", stylesheet(rules="array")),
    ("ui-stylesheets", stylesheet(rules={"select": "array"})),
    ("ui-stylesheets", stylesheet(rules=[5])),
    ("ui-stylesheets", stylesheet(rules=[{"select": 5, "component": "x"}])),
    ("ui-stylesheets", stylesheet(prefixes="x")),
    ("ui-stylesheets", stylesheet(prefixes={"ex": 5})),
    (
        "ui-stylesheets",
        stylesheet(prefixes={"ex": 5}, rules=[{"select": "ex:A", "component": "x"}]),
    ),
    ("ui-stylesheets", stylesheet(name=5)),
    ("ui-stylesheets", stylesheet(name=["theme"])),
    ("ui-stylesheets", stylesheet(rbac_context=5)),
    ("ui-stylesheets", [stylesheet()]),
    ("ui-components", component(template=5)),
    ("ui-components", component(engine=5)),
    ("ui-components", component(engine="other")),
    ("ui-components", component(name=5)),
    ("ui-components", component(name=["todo-row"])),
    ("ui-components", [component()]),
]


@pytest.mark.django_db
@pytest.mark.parametrize("write", ["POST", "PUT"])
@pytest.mark.parametrize("slug,body", MALFORMED)
@test_context
def test_malformed_ui_content_is_400(admin_login, slug, body, write):
    """A body of the wrong shape is refused by the type's schema (400), not
    by an error in the write-time checks (500)."""
    good = stylesheet() if slug == "ui-stylesheets" else component()
    Given.http.force_authenticate(admin_login)
    if write == "POST":
        When.http.post(reverse(f"{slug}-list"), data=body, format="json")
    else:
        When.http.post(reverse(f"{slug}-list"), data=good, format="json")
        Then.http.status_code_is(201)
        location, etag = Http.response["Location"], Http.response["ETag"]
        When.http.put(location, data=body, format="json", HTTP_IF_MATCH=etag)
    Then.http.status_code_is(400)


@pytest.mark.django_db
@test_context
def test_stylesheet_same_name_other_context_is_201(admin_login):
    url = reverse("ui-stylesheets-list")
    Given.http.force_authenticate(admin_login)
    When.http.post(url, data=stylesheet(rbac_context=""), format="json")
    Then.http.status_code_is(201)
    When.http.post(url, data=stylesheet(rbac_context="project-a"), format="json")
    Then.http.status_code_is(201)
    When.http.post(url, data=stylesheet(rbac_context="project-a"), format="json")
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("ui-stylesheet-name-taken")


@pytest.mark.django_db
def test_without_ui_app_no_ui_routes():
    import scrud_django.urls

    # scrud_django.urls is resolved by name: tests.urls includes it once at
    # import, so its routes are looked up on that module's own resolver.
    urlconf = "scrud_django.urls"
    installed = [app for app in settings.INSTALLED_APPS if app != "scrud_django.ui"]
    try:
        with override_settings(INSTALLED_APPS=installed):
            clear_url_caches()
            importlib.reload(scrud_django.urls)
            with pytest.raises(NoReverseMatch):
                reverse("ui-components-list", urlconf=urlconf)
            with pytest.raises(NoReverseMatch):
                reverse("ui-stylesheets-list", urlconf=urlconf)
            assert reverse("services", urlconf=urlconf) == "/services/"
    finally:
        clear_url_caches()
        importlib.reload(scrud_django.urls)
    assert reverse("ui-components-list", urlconf=urlconf) == "/ui-components/"
