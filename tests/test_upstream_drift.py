"""scrud-django re-implements three upstream functions: DRF's
``GenericAPIView.get_object`` (as ``ResourceViewMixin.get_object``) and
scoped-rbac's private ``access._filter_per_object`` (as
``filters.filter_readable_resources_per_object``), to give object checks a
resource's content (a ``dict``), and DRF's ``GenericAPIView.filter_queryset``
(as ``ListValidatorsMixin.filter_queryset``, which narrows a list by the
object check right after the RBAC filter). It also relies on upstream
internals: the ``error`` / ``error_body_func`` attributes of scrudful's
``ScrudfulViewFunc`` (``gate_uniform_errors``). These tests fail when upstream drifts from what
was re-implemented or relied on: the names the upstream code uses are
pinned, and the re-implementations must give upstream's results where
upstream's behaviour is the one wanted.

When one fails after an upgrade, read the upstream change, port it into the
re-implementation if it applies, and update the pin."""

import types
from uuid import uuid4

import pytest
from django.conf import settings
from django.http import Http404
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.generics import GenericAPIView
from rest_framework.test import APIRequestFactory, force_authenticate
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.permissions import policy_for
from scoped_rbac.policy import Permission

from scrud_django.filters import filter_readable_resources_per_object
from scrud_django.mixins.view_mixins import ResourceViewMixin
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, get_resource_type_for
from scrud_django.views import ResourceViewSet

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES


def code_names(function):
    """The names the code of `function` (and the functions nested in it)
    uses, as a sorted tuple without repeats: the order the compiler records
    them in differs between Python versions."""
    names = []

    def walk(code):
        names.extend(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(function.__code__)
    return tuple(sorted(set(names)))


def test_drfs_get_object_is_what_resource_view_mixin_re_implements():
    assert code_names(GenericAPIView.get_object) == (
        "__class__",
        "__name__",
        "check_object_permissions",
        "filter_queryset",
        "get_object_or_404",
        "get_queryset",
        "kwargs",
        "lookup_field",
        "lookup_url_kwarg",
        "request",
    )


def test_drfs_filter_queryset_is_what_list_validators_mixin_re_implements():
    # ListValidatorsMixin.filter_queryset runs each filter backend itself, to
    # narrow a list by the object check right after the RBAC filter.
    assert code_names(GenericAPIView.filter_queryset) == (
        "filter_backends",
        "filter_queryset",
        "list",
        "request",
    )


def test_scoped_rbacs_per_object_filter_is_what_scrud_re_implements():
    assert code_names(access._filter_per_object) == (
        "AnyOf",
        "ConditionalListTooLarge",
        "_CANDIDATE_CHUNK_SIZE",
        "all",
        "any",
        "append",
        "cel_resource",
        "conditional",
        "conditional_anywhere",
        "conditional_list_max_rows",
        "enumerate",
        "filter",
        "isinstance",
        "iterator",
        "list",
        "map",
        "options",
        "outright",
        "pk",
        "should_allow",
        "tuple",
        "unrestricted",
        "usable_contexts",
    )


def test_scoped_rbacs_custom_contexts_for_reads_the_permissions_class():
    # scrud_django.permissions.reading_content gives a permission that keeps
    # scoped-rbac's contexts_for an instance-level one: lists stay on their
    # SQL path only while custom_contexts_for judges a permission by its
    # class, not by the bound method.
    from scoped_rbac.permissions import IsAuthorized, custom_contexts_for

    from scrud_django.permissions import reading_content

    assert code_names(custom_contexts_for) == (
        "IsAuthorized",
        "access",
        "contexts_for",
        "getattr",
        "object_contexts",
        "type",
    )
    (permission,) = reading_content([IsAuthorized()])
    assert "contexts_for" in vars(permission)
    assert custom_contexts_for(permission) is None


@pytest.mark.django_db
def test_a_scrud_view_reading_the_content_lists_by_its_default_contexts():
    from scrud_django.permissions import checks_read_content

    reverse("partner-profiles-list")
    view = ResourceViewSet(resource_type_name="partner-profiles")
    view.action_map = {"get": "list"}
    request = APIRequestFactory().get("/")
    force_authenticate(request, user=user_with())
    view.request = view.initialize_request(request)
    view.args, view.kwargs, view.format_kwarg = (), {}, None
    with checks_read_content(view):
        assert view.rbac_list_contexts_for() is None
    assert "get_permissions" not in vars(view)


def user_with(*grants, **flags):
    user = UserFactory(**flags)
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=timezone.now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=timezone.now(),
        ).save()
    return user


def request_of(user):
    request = APIRequestFactory().get("/")
    request.user = user
    return request


def team_of(obj):
    """A ``contexts_for`` that reads a resource's ``team``: scoped-rbac gives
    it the `Resource`, scrud-django's object checks its content (a
    ``dict``)."""
    content = obj if isinstance(obj, dict) else obj.content
    return [content.get("team", "")]


@pytest.mark.django_db
def test_the_per_object_list_filter_agrees_with_scoped_rbacs():
    # Upstream also lists a resource stored in a reserved context, whose
    # detail route is a 404; scrud's does not. Those are left out here.
    reverse("partner-profiles-list")
    for n, team in enumerate(["red", "red", "blue", "blue", "green", "", "*"]):
        name = "ok" if n % 2 else "no"
        ResourceRegistration.register({"name": name, "team": team}, "partner-profiles")
    queryset = Resource.objects.filter(
        resource_type=get_resource_type_for("partner-profiles")
    )
    reader = {"http.GET": [PP, PP_LIST]}
    cel = {"http.GET": {PP: {"condition": 'name == "ok"'}}}
    users = {
        "superuser": UserFactory(is_superuser=True, is_staff=True),
        "red": user_with((reader, "red")),
        "red+blue-cel": user_with((reader, "red"), (cel, "blue")),
        "cel-anywhere": user_with((cel, "*")),
        "default": user_with((reader, "")),
        "none": user_with(),
    }
    anywhere = dict(
        settings.SCOPED_RBAC,
        POLICY_FOR_AUTHENTICATED="tests.rbac_defaults."
        "policy_read_partner_profiles_anywhere",
    )
    permission = Permission("http.GET", PP)
    for label, user, setting in [
        *((label, user, settings.SCOPED_RBAC) for label, user in users.items()),
        ("reader-anywhere", user_with(), anywhere),
    ]:
        with override_settings(SCOPED_RBAC=setting):
            request = request_of(user)
            ours = set(
                filter_readable_resources_per_object(
                    queryset, request, permission, team_of
                ).values_list("pk", flat=True)
            )
            theirs = set(
                access.filter_authorized(
                    queryset,
                    policy_for(request),
                    permission,
                    request=request,
                    contexts_for=team_of,
                ).values_list("pk", flat=True)
            )
        assert ours == theirs, label


def outcome(get_object, view):
    try:
        return get_object(view).pk
    except Http404:
        return "404"


@pytest.mark.django_db
def test_resource_view_mixins_lookup_agrees_with_drfs():
    reverse("partner-profiles-list")
    readable = ResourceRegistration.register(
        {"name": "a", "rbac_context": "a"}, "partner-profiles"
    )
    hidden = ResourceRegistration.register(
        {"name": "b", "rbac_context": "b"}, "partner-profiles"
    )
    users = [
        UserFactory(is_superuser=True, is_staff=True),
        user_with(({"http.GET": [PP, PP_LIST]}, "a")),
        user_with(),
    ]
    seen = {}
    for user in users:
        for slug in [readable.pk, hidden.pk, 999999, "not-a-number"]:
            results = []
            for get_object in (ResourceViewMixin.get_object, GenericAPIView.get_object):
                view = ResourceViewSet(resource_type_name="partner-profiles")
                view.action_map = {"get": "retrieve"}
                request = APIRequestFactory().get("/")
                force_authenticate(request, user=user)
                view.request = view.initialize_request(request)
                view.args, view.kwargs = (), {"slug": str(slug)}
                view.format_kwarg = None
                try:
                    results.append(outcome(get_object, view))
                except Exception as exc:  # noqa: BLE001 (any other outcome)
                    results.append(type(exc).__name__)
            assert results[0] == results[1], (user, slug, results)
            seen[(users.index(user), str(slug))] = results[0]
    superuser, reader, nobody = 0, 1, 2
    assert seen[(nobody, str(readable.pk))] == "404"
    assert seen[(superuser, str(hidden.pk))] == hidden.pk
    assert seen[(reader, str(readable.pk))] == readable.pk
    assert seen[(reader, str(hidden.pk))] == "404"
    assert seen[(superuser, "999999")] == "404"
    assert seen[(superuser, "not-a-number")] == "404"


def test_scrudfuls_view_func_keeps_error_and_error_body_func_in_its_dict():
    # view_mixins.gate_uniform_errors turns the ScrudfulViewFunc instances
    # scrudful_viewset installed into UniformErrorsViewFunc (reassigning
    # __class__), reading their "error" and "error_body_func" from the
    # instance __dict__ into the properties that gate them per request.
    from scrudful.rest_framework import ScrudfulViewFunc

    assert code_names(ScrudfulViewFunc.__init__) == (
        "TypeError",
        "_check_error",
        "_check_sync",
        "_from_viewset",
        "bool",
        "callable",
        "check_error_body_func",
        "check_validators",
        "context_link_or_func",
        "context_rel_or_func",
        "context_type_or_func",
        "creates",
        "error",
        "error_body_func",
        "etag_func",
        "functools",
        "last_modified_func",
        "location_func",
        "object_validators",
        "schema_link_or_func",
        "schema_rel_or_func",
        "schema_type_or_func",
        "update_wrapper",
        "view_method",
    )
    assert "__slots__" not in vars(ScrudfulViewFunc)
    for name in ("error", "error_body_func"):
        assert name not in vars(ScrudfulViewFunc), name

    def view(self, request):
        return None

    from scoped_rbac import problems

    handler = ScrudfulViewFunc(
        view,
        error=problems.problem_entity(),
        error_body_func=problems.conditional_problem,
    )
    assert handler.__dict__["error"] is handler.error
    assert handler.__dict__["error_body_func"] is handler.error_body_func
