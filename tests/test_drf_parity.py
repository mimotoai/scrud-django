"""The DRF routes answer as they did before the framework-neutral core
(df04105) where an adopter configures DRF or overrides a view: a viewset's
own ``get_rbac_permissions`` still decides whether the grants are asked in
each context of the object check, and an adopter's DRF versioning scheme
(``DEFAULT_VERSIONING_CLASS``) still shapes every URL scrud builds (the Link
header, a list envelope's ``link``, ``Location``, the ``services/`` catalog
and `scrud_django.reverse`), as DRF's ``reverse`` builds them."""

import inspect
import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.timezone import now
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.reverse import reverse as drf_reverse
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.versioning import QueryParameterVersioning
from rest_framework.views import APIView
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.permissions import IsAuthorized

import scrud_django
from scrud_django.registration import ResourceRegistration
from scrud_django.views import ResourceViewSet, get_service_list

from .factories import UserFactory
from .fixtures import *  # noqa
from .fixtures import RESOURCE_ENDPOINT_DETAIL_NAME, RESOURCE_ENDPOINT_LIST_NAME
from .steps import *  # noqa: F403, F401

JSON_LD = "http://www.w3.org/ns/json-ld#context"


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


class AllowingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` whose ``has_context_permission`` allows
    every context: its own code, which does not read each context."""

    def has_context_permission(self, request, view, obj, context):
        return True


def view_of(view_class, user):
    """A `view_class` (on the type ``json-ld``) and its DRF request: a GET of
    its detail route by `user`."""
    drf = Request(APIRequestFactory().get("/"))
    drf.user = user
    view = view_class(resource_type_name="json-ld")
    view.request, view.action, view.format_kwarg = drf, "retrieve", None
    view.kwargs = {}
    return view, drf


@pytest.mark.django_db
@test_context
def test_get_rbac_permissions_decides_whether_each_context_is_asked():
    """A viewset whose ``get_rbac_permissions`` adds a leaf with its own
    ``has_context_permission`` has the grants asked in each context of the
    object check, whatever ``get_permissions`` holds; one whose
    ``get_rbac_permissions`` reads each context is not asked them, even when
    ``get_permissions`` holds such a leaf."""

    class AddingViewSet(ResourceViewSet):
        permission_classes = [IsAuthorized]

        def get_rbac_permissions(self):
            return [*super().get_rbac_permissions(), AllowingIsAuthorized()]

    class ReadingViewSet(ResourceViewSet):
        permission_classes = [AllowingIsAuthorized]

        def get_rbac_permissions(self):
            return [IsAuthorized()]

    reverse("json-ld-list")  # importing tests.urls registers the types
    content = ResourceRegistration.register({"rbac_context": "a"}, "json-ld").content
    reader_a = user_with(({"http.GET": [JSON_LD]}, "a"))
    reader_b = user_with(({"http.GET": [JSON_LD]}, "b"))

    view, drf = view_of(AddingViewSet, reader_a)
    view.check_grants_in_each_context(drf, content)
    view, drf = view_of(AddingViewSet, reader_b)
    with pytest.raises(PermissionDenied):
        view.check_grants_in_each_context(drf, content)

    for user in (reader_a, reader_b):
        view, drf = view_of(ReadingViewSet, user)
        view.check_grants_in_each_context(drf, content)


@pytest.mark.django_db
@test_context
def test_get_rbac_permissions_reading_each_context_keeps_the_detail_answer():
    """The detail route of a viewset whose permission class allows every
    context while its ``get_rbac_permissions`` reads each context: a user
    with no grants where the resource is reads it (200), as before."""

    class ReadingViewSet(ResourceViewSet):
        permission_classes = [AllowingIsAuthorized]

        def get_rbac_permissions(self):
            return [IsAuthorized()]

    reverse("json-ld-list")
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    reader_b = user_with(({"http.GET": [JSON_LD]}, "b"))
    request = APIRequestFactory().get("/", HTTP_ACCEPT="application/json")
    force_authenticate(request, reader_b)
    detail = ReadingViewSet.as_view({"get": "retrieve"}, resource_type_name="json-ld")
    response = detail(request, slug=stored.pk)
    assert response.status_code == 200, response.data


class V1Versioning(QueryParameterVersioning):
    """An adopter's DRF versioning scheme: ``?version=``, ``v1`` by
    default."""

    default_version = "v1"


@pytest.fixture
def versioned(monkeypatch):
    """DRF's views versioned by `V1Versioning`, as an adopter's
    ``REST_FRAMEWORK["DEFAULT_VERSIONING_CLASS"]`` versions them (DRF reads
    it once, into ``APIView.versioning_class``, and DRF's ``api_view``
    copies it into each function view's class when it decorates it, as for
    ``services/``)."""
    monkeypatch.setattr(APIView, "versioning_class", V1Versioning)
    monkeypatch.setattr(get_service_list.cls, "versioning_class", V1Versioning)


def versioned_url(url):
    """`url` as `V1Versioning` reverses it: with ``?version=v1``."""
    return f"{url}?version=v1"


def _detail_url_of(resource):
    return "http://testserver" + reverse(
        resource.resource_type.route_name_detail(), args=[resource.id]
    )


@pytest.mark.django_db
@test_context
def test_drf_versioning_shapes_links_location_and_services(
    versioned, partner_profiles, admin_login, partner_profile_post_data
):
    resource_type = partner_profiles.resource_type
    schema_url = versioned_url(_detail_url_of(resource_type.schema))
    context_url = versioned_url(_detail_url_of(resource_type.context))
    links = (
        f'<{schema_url}>; rel="describedby"; type="application/schema+json", '
        f'<{context_url}>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json"'
    )

    Given.http.force_authenticate(admin_login)
    When.http.get(
        reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    )
    Then.http.status_code_is(200)
    assert Http.response["Link"].startswith(links + ", "), Http.response["Link"]

    When.http.get(reverse(RESOURCE_ENDPOINT_LIST_NAME))
    Then.http.status_code_is(200)
    (envelope,) = [
        entry
        for entry in json.loads(Http.response.content)["content"]
        if entry["href"] == versioned_url(_detail_url_of(partner_profiles))
    ]
    assert envelope["link"] == links

    When.http.post(
        reverse(RESOURCE_ENDPOINT_LIST_NAME),
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
    )
    Then.http.status_code_is(201)
    location = Http.response["Location"]
    assert parse_qs(urlsplit(location).query) == {"version": ["v1"]}, location
    assert Http.response["Link"].startswith(links + ", "), Http.response["Link"]

    When.http.get(reverse("services"))
    Then.http.status_code_is(200)
    catalog = json.loads(Http.response.content)
    assert catalog, catalog
    for url in catalog.values():
        assert parse_qs(urlsplit(url).query) == {"version": ["v1"]}, url


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query", ["", "?format=json", "?version=v2", "?format=json&version=v2"]
)
@test_context
def test_scrud_reverse_is_drfs(query):
    """`scrud_django.reverse` gives what DRF's ``reverse`` gives, with or
    without a versioning scheme on the request, the ``?format=`` override
    kept; it takes DRF's ``format`` and extra keywords."""
    assert inspect.signature(scrud_django.reverse) == inspect.signature(drf_reverse)
    reverse("json-ld-list")
    name, args = "json-ld-detail", ["x"]
    plain = Request(APIRequestFactory().get("/" + query))
    versioned_request = Request(APIRequestFactory().get("/" + query))
    scheme = V1Versioning()
    versioned_request.version = scheme.determine_version(versioned_request)
    versioned_request.versioning_scheme = scheme
    for request in (None, plain, versioned_request):
        assert scrud_django.reverse(name, args, request=request) == drf_reverse(
            name, args, request=request
        )
        assert scrud_django.reverse(
            name, args=args, request=request, urlconf=None
        ) == drf_reverse(name, args=args, request=request, urlconf=None)


# The scrud-defined names each module had at df04105 that moved: each still
# imports from there, and is the moved object itself.
OLD_HOMES = {
    "scrud_django.validators": {
        "HiddenWorkflowTransitionError": "scrud_django.exceptions",
        "WorkflowPolicyInvalidError": "scrud_django.exceptions",
        "WorkflowPolicy": "scrud_django.workflow",
        "default_workflow_policy_for": "scrud_django.workflow",
        "stored_policies_in": "scrud_django.workflow",
        "workflow_policies_for": "scrud_django.workflow",
        "condition_resource": "scrud_django.core.authz",
        "rbac_contexts_of": "scrud_django.mixins.resource_mixin",
        "_checking_transition": "scrud_django.core.authz",
        "_checking_write": "scrud_django.core.authz",
        "_mark_workflow_authorized": "scrud_django.core.authz",
        "_reading_every_context": "scrud_django.core.authz",
    },
    "scrud_django.permissions": {
        "logger": "scrud_django.core.authz",
        "rbac_contexts_of": "scrud_django.mixins.resource_mixin",
    },
    "scrud_django.mixins.view_mixins": {
        "_checking_write": "scrud_django.core.authz",
        "_workflow_authorized": "scrud_django.core.authz",
    },
    "scrud_django.filters": {"ResourceRemoval": "scrud_django.models"},
    "scrud_django.exceptions": {"DOCUMENT_RENDERERS": "scrud_django.renderers"},
}


@pytest.mark.parametrize("module", sorted(OLD_HOMES))
def test_moved_names_import_from_their_old_modules(module):
    import importlib

    old = importlib.import_module(module)
    for name, home in OLD_HOMES[module].items():
        assert getattr(old, name) is getattr(importlib.import_module(home), name), name
    if module == "scrud_django.permissions":
        from scrud_django.permissions import logger

        assert logger.name == "scrud_django.permissions"
    if module == "scrud_django.exceptions":
        from scrud_django.exceptions import DOCUMENT_RENDERERS  # noqa: F401

        with pytest.raises(AttributeError):
            old.no_such_name  # noqa: B018
