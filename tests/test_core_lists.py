"""A list without DRF (`scrud_django.core.lists`): the resources a requester
may list, filtered on query parameters, and the list's validators and
preconditions, asked with a plain Django request and a neutral requirement,
as the DRF routes answer them."""

import datetime
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.paginator import EmptyPage
from django.db import OperationalError, connection, transaction
from django.http import QueryDict
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import http_date, quote_etag
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for
from scrud_django.core.requirements import GrantsLeaf
from scrud_django.core.types import requirement_for
from scrud_django.exceptions import ScrudException
from scrud_django.filters import ui_parameters
from scrud_django.models import Resource, ResourceRemoval
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES
from .resource_types import HOOKED_NOTE_TYPE_URI, hooked_resource_types
from .steps import *  # noqa: F403, F401

PP = TYPE_URI_PARTNER_PROFILES
PP_READER = {"http.GET": [PP, TYPE_URI_LIST_PARTNER_PROFILES]}
AN_HOUR_AGO = datetime.timedelta(hours=1)


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
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


def profile(name, context):
    """A partner profile in `context`, stored an hour ago."""
    reverse("partner-profiles-list")
    resource = ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )
    Resource.objects.filter(pk=resource.pk).update(
        modified_at=timezone.now() - AN_HOUR_AGO
    )
    resource.refresh_from_db()
    return resource


def get_request(user, path="/"):
    request = RequestFactory().get(path)
    request.user = user
    return request


class RefusingLeaf(GrantsLeaf):
    """A leaf whose object check refuses the resource named `name`."""

    overrides_object_checks = True

    def __init__(self, type_iri, name):
        super().__init__(type_iri)
        self.name = name

    def has_object_permission(self, request, obj):
        return obj.get("name") != self.name


@pytest.mark.django_db
@test_context
def test_list_etag_matches_drf():
    from scrud_django.core.lists import list_etag

    for n in range(3):
        profile(f"seen-{n}", "a")
    for n in range(2):
        profile(f"hidden-{n}", "b")
    client = APIClient()
    client.force_authenticate(user_with((PP_READER, "a")))
    response = client.get(reverse("partner-profiles-list") + "?page=2&page_size=1")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] == 3
    (entry,) = body["content"]
    item = Resource.objects.get(content__name=entry["content"]["name"])
    rtype = get_resource_type_for("partner-profiles")
    expected = list_etag(
        rtype, items=[item], count=3, query=[("page", ["2"]), ("page_size", ["1"])]
    )
    assert response["ETag"] == quote_etag(expected)


@pytest.mark.django_db
@test_context
def test_readable_queryset_object_checks():
    from scrud_django.core.lists import readable_queryset

    refused, shown = profile("refused", "a"), profile("shown", "a")
    profile("elsewhere", "b")
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    # The grants alone: what the RBAC filter lets through.
    plain = readable_queryset(request, rtype, [GrantsLeaf(PP)])
    assert sorted(plain.values_list("pk", flat=True)) == sorted([refused.pk, shown.pk])
    requirement = [GrantsLeaf(PP), RefusingLeaf(PP, "refused")]
    listed = readable_queryset(request, rtype, requirement)
    assert list(listed.values_list("pk", flat=True)) == [shown.pk]
    # The refused resource was examined: it counts against the cap.
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=1)
    with override_settings(SCOPED_RBAC=capped):
        with pytest.raises(access.ConditionalListTooLarge):
            readable_queryset(request, rtype, requirement)
        # Without object checks the outright grants list in SQL, uncapped.
        assert readable_queryset(request, rtype, [GrantsLeaf(PP)]).count() == 2


@pytest.mark.django_db
@test_context
def test_content_filtered_ignores_paging_and_ui():
    from scrud_django.core.lists import content_filtered

    reverse("json-ld-list")
    a = ResourceRegistration.register({"text": "a", "rbac_context": "a"}, "json-ld")
    ResourceRegistration.register({"text": "b", "rbac_context": "a"}, "json-ld")
    queryset = Resource.objects.filter(resource_type=a.resource_type)
    ignored = frozenset({"page", "page_size", "format"}) | ui_parameters()
    assert "ui-mode" in ignored
    params = QueryDict("page=1&ui-mode=edit&text=a")
    filtered = content_filtered(queryset, params, ignored=ignored)
    assert list(filtered.values_list("pk", flat=True)) == [a.pk]
    # An integer key is a 400, as the DRF filter answers it.
    with pytest.raises(ScrudException) as raised:
        content_filtered(queryset, QueryDict("1=x"), ignored=ignored)
    assert raised.value.status_code == 400
    assert raised.value.errors == {
        "1": ["An integer is not a content key a list can be filtered on."]
    }


@pytest.mark.django_db
@test_context
def test_list_last_modified_includes_removal():
    from scrud_django.core.lists import list_last_modified, readable_queryset

    older, gone = profile("older", "a"), profile("gone", "a")
    Resource.objects.filter(pk=older.pk).update(
        modified_at=timezone.now() - 2 * AN_HOUR_AGO
    )
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    requirement = [GrantsLeaf(PP)]

    def last_modified():
        queryset = readable_queryset(request, rtype, requirement)
        return list_last_modified(request, rtype, queryset, requirement)

    before = last_modified()
    gone.refresh_from_db()
    assert before == gone.modified_at
    ResourceRegistration.delete("partner-profiles", gone.pk)
    after = last_modified()
    removal = ResourceRemoval.objects.filter(resource_type=rtype).get()
    assert after == removal.removed_at > before
    # A removal in a context the requester does not read does not move it.
    other = get_request(user_with((PP_READER, "b")))
    assert (
        list_last_modified(
            other, rtype, readable_queryset(other, rtype, requirement), requirement
        )
        is None
    )


@pytest.mark.django_db
@test_context
def test_evaluate_list_preconditions_reads_validators_only_when_needed():
    from scrud_django.core.lists import evaluate_list_preconditions

    calls = []

    def validators():
        calls.append(1)
        return "abc", None

    # No list precondition: nothing to evaluate.
    assert evaluate_list_preconditions("POST", {}, validators) is None
    # "*" is decided by the list existing, without its validators.
    assert evaluate_list_preconditions("POST", {"if-match": "*"}, validators) is None
    failed = evaluate_list_preconditions("POST", {"if-none-match": "*"}, validators)
    assert failed.status == 412
    assert calls == []
    # An entity-tag needs them.
    assert (
        evaluate_list_preconditions("POST", {"if-match": '"abc"'}, validators) is None
    )
    stale = evaluate_list_preconditions("POST", {"if-match": '"old"'}, validators)
    assert stale.status == 412
    assert calls == [1, 1]


JSON_LD = "http://www.w3.org/ns/json-ld#context"
JSON_LD_LIST = collection_type_uri_for(JSON_LD)


def json_ld_in(*contexts):
    """A JSON-LD resource in each of `contexts`, and the type."""
    reverse("json-ld-list")  # importing tests.urls registers the types
    stored = [
        ResourceRegistration.register({"rbac_context": context}, "json-ld")
        for context in contexts
    ]
    return get_resource_type_for("json-ld"), stored


def request_by(method, user):
    request = getattr(RequestFactory(), method.lower())("/")
    request.user = user
    return request


def drf_list_status(user):
    client = APIClient()
    if user.is_authenticated:
        client.force_authenticate(user)
    return client.get(reverse("json-ld-list"), HTTP_ACCEPT="application/json")


@pytest.mark.django_db
@test_context
def test_readable_queryset_asks_the_list_grant():
    """A user granted ``http.GET`` on JSON_LD in "a" but not on its list
    type: the DRF list is a 403, and `readable_queryset` of a GET or HEAD
    refuses (``access.AccessDenied``) rather than list the resource in "a".
    The anonymous user is refused too (DRF: 401)."""
    from scrud_django.core.lists import readable_queryset

    rtype, _ = json_ld_in("a")
    reader = user_with(({"http.GET": [JSON_LD]}, "a"))
    assert drf_list_status(reader).status_code == 403
    assert drf_list_status(AnonymousUser()).status_code == 401
    for user in (reader, AnonymousUser()):
        for method in ("GET", "HEAD"):
            with pytest.raises(access.AccessDenied):
                readable_queryset(
                    request_by(method, user), rtype, [GrantsLeaf(JSON_LD)]
                )


@pytest.mark.django_db
@test_context
def test_list_queryset_lists_for_a_get_or_head_only():
    """A user holding only the item grant is refused the list's GET, and a
    POST or OPTIONS is a ``ValueError`` (the list grant is asked for a GET or
    HEAD alone, so it would be skipped)."""
    from scrud_django.core.lists import list_queryset

    rtype, _ = json_ld_in("a")
    reader = user_with(({"http.GET": [JSON_LD]}, "a"))
    for method in ("GET", "HEAD"):
        with pytest.raises(access.AccessDenied):
            list_queryset(
                request_by(method, reader),
                rtype,
                QueryDict(),
                requirement=[GrantsLeaf(JSON_LD)],
            )
    for method in ("POST", "OPTIONS"):
        with pytest.raises(ValueError, match=method):
            list_queryset(
                request_by(method, reader),
                rtype,
                QueryDict(),
                requirement=[GrantsLeaf(JSON_LD)],
            )


@pytest.mark.django_db
@test_context
def test_readable_queryset_lists_for_the_lister():
    """ "lister-a" (``http.GET`` on JSON_LD and its list type in "a") gets
    the resource in "a" only, as the DRF list."""
    from scrud_django.core.lists import readable_queryset

    rtype, (in_a, _, _) = json_ld_in("a", "b", ["b", "c"])
    lister = user_with(({"http.GET": [JSON_LD, JSON_LD_LIST]}, "a"))
    response = drf_list_status(lister)
    assert response.status_code == 200, response.content
    assert [entry["content"] for entry in response.json()["content"]] == [in_a.content]
    for method in ("GET", "HEAD"):
        listed = readable_queryset(
            request_by(method, lister), rtype, [GrantsLeaf(JSON_LD)]
        )
        assert list(listed.values_list("pk", flat=True)) == [in_a.pk]


@pytest.mark.django_db
@test_context
def test_readable_queryset_asks_the_requirements_request_checks():
    """Beside the list grant, each entry of the requirement must allow the
    list's GET (``has_permission``): a leaf refusing it refuses the list,
    whatever the grants allow, a superuser's included."""
    from scrud_django.core.lists import readable_queryset

    rtype, (in_a,) = json_ld_in("a")
    superuser = UserFactory(is_staff=True, is_superuser=True)
    request = request_by("GET", superuser)
    assert (
        readable_queryset(request, rtype, [GrantsLeaf(JSON_LD)])
        .filter(pk=in_a.pk)
        .exists()
    )
    with pytest.raises(access.AccessDenied):
        readable_queryset(request, rtype, [GrantsLeaf(JSON_LD), RefusingGrantsLeaf()])


@pytest.mark.django_db
@test_context
def test_readable_queryset_of_a_write_asks_no_list_grant():
    """A write's list validators (a conditional create's): `readable_queryset`
    given a POST asks no list grant, the write's own request check
    (`validate_write`'s) governing it, as DRF evaluates them without it. A
    creator in "a" without the list grant gets the resources of its GET
    copy, those in "a"; a requirement asking no grants is still refused."""
    from django.core.exceptions import ImproperlyConfigured

    from scrud_django.core.lists import readable_queryset

    rtype, (in_a, _) = json_ld_in("a", "b")
    creator = user_with(({"http.GET": [JSON_LD], "http.POST": [JSON_LD]}, "a"))
    request = request_by("POST", creator)
    listed = readable_queryset(request, rtype, [GrantsLeaf(JSON_LD)])
    assert list(listed.values_list("pk", flat=True)) == [in_a.pk]
    with pytest.raises(access.AccessDenied):
        readable_queryset(request_by("GET", creator), rtype, [GrantsLeaf(JSON_LD)])
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        readable_queryset(request, rtype, [])


class RefusingRequests:
    """A leaf (of an untrusted class) whose request check refuses every
    request and whose object check allows every object."""

    type_iri = JSON_LD
    reads_each_context = False
    overrides_object_checks = False

    def contexts_for(self, obj):
        return [obj.get("rbac_context", "")]

    def has_permission(self, request, body):
        return False

    def has_object_permission(self, request, obj):
        return True

    def has_context_permission(self, request, obj, context):
        return True


class RefusingGrantsLeaf(GrantsLeaf):
    """A trusted leaf (a `GrantsLeaf` subclass overriding neither
    ``has_context_permission`` nor ``contexts_for``) whose request check
    refuses every request."""

    def __init__(self, type_iri=JSON_LD):
        super().__init__(type_iri)

    def has_permission(self, request, body):
        return False


class OwnContexts(GrantsLeaf):
    """A `GrantsLeaf` reading contexts of its own, not a resource's stored
    ones."""

    def contexts_for(self, obj):
        return ["a"]


@pytest.mark.django_db
@test_context
def test_readable_queryset_refuses_a_leaf_with_its_own_contexts():
    """The SQL filter and the removal stamps read a resource's stored
    contexts (its ``ResourceContext`` rows): `readable_queryset` and
    `list_last_modified` refuse (``ImproperlyConfigured``), whatever the
    method or the requester, a requirement with a leaf that may read others,
    an ``AnyOf`` option's included: a `GrantsLeaf` subclass overriding
    ``contexts_for``, a leaf given one of its own, a leaf of a class that is
    neither a `GrantsLeaf`, a type's hooks (`HookLeaf`) nor a DRF view's
    (`ViewLeaf`), and a `ViewLeaf` whose permission overrides
    ``contexts_for``. A subclass keeping its base's ``contexts_for`` lists."""
    from django.core.exceptions import ImproperlyConfigured

    from scrud_django.core.lists import list_last_modified, readable_queryset
    from scrud_django.core.types import HookLeaf, TypeOptions
    from scrud_django.drf_requirements import requirement_of
    from scrud_django.views import ResourceViewSet

    from .urls import TeamIsAuthorized

    in_a = profile("in-a", "a")
    rtype = get_resource_type_for("partner-profiles")
    given = GrantsLeaf(PP)
    given.contexts_for = lambda obj: ["a"]
    team_view = ResourceViewSet(resource_type_name="partner-profiles")
    team_view.permission_classes = [TeamIsAuthorized]
    team_view.action = "list"
    plain_view = ResourceViewSet(resource_type_name="partner-profiles")
    plain_view.action = "list"
    custom = {
        "subclass": [OwnContexts(PP)],
        "instance": [given],
        "other class": [GrantsLeaf(PP), RefusingRequests()],
        "AnyOf option": [access.AnyOf([[GrantsLeaf(PP)], [OwnContexts(PP)]])],
        "view leaf": requirement_of(team_view),
    }
    queryset = Resource.objects.filter(resource_type=rtype)
    superuser = UserFactory(is_staff=True, is_superuser=True)
    for user in (superuser, user_with((PP_READER, "a")), AnonymousUser()):
        for method in ("GET", "HEAD", "POST"):
            request = request_by(method, user)
            plain_view.request = team_view.request = request
            for requirement in custom.values():
                with pytest.raises(ImproperlyConfigured, match="contexts"):
                    readable_queryset(request, rtype, requirement)
                with pytest.raises(ImproperlyConfigured, match="contexts"):
                    list_last_modified(request, rtype, queryset, requirement)
    request = request_by("GET", user_with((PP_READER, "a")))
    plain_view.request = request
    hooks = HookLeaf(rtype, TypeOptions())
    for requirement in (
        [RefusingLeaf(PP, "elsewhere"), hooks],
        [access.AnyOf([[GrantsLeaf(PP)], [RefusingLeaf(PP, "x")]])],
        requirement_of(plain_view),
    ):
        listed = readable_queryset(request, rtype, requirement)
        assert list(listed.values_list("pk", flat=True)) == [in_a.pk]
        assert list_last_modified(request, rtype, listed, requirement) == (
            in_a.modified_at
        )


ON_MYSQL = settings.DATABASES["default"]["ENGINE"] == "django.db.backends.mysql"
HOOKED_READER = {
    "http.GET": [HOOKED_NOTE_TYPE_URI, collection_type_uri_for(HOOKED_NOTE_TYPE_URI)]
}


def hooked_note(text, context):
    """A ``hooked-notes`` resource (a type with ``content_filter``)."""
    reverse("hooked-notes-list")
    hooked_resource_types()
    return ResourceRegistration.register(
        {"text": text, "rbac_context": context}, "hooked-notes"
    )


@pytest.mark.django_db
@test_context
def test_list_queryset_orders_and_filters():
    from scrud_django.core.lists import list_queryset

    first, _, third = (hooked_note(text, "a") for text in ("x", "y", "x"))
    hooked_note("x", "b")  # not readable by the requester
    rtype = get_resource_type_for("hooked-notes")
    request = get_request(user_with((HOOKED_READER, "a")))
    requirement = requirement_for(rtype, "hooked-notes")
    params = QueryDict("text=x&page=2")
    listed = list_queryset(
        request, rtype, params, requirement=requirement, ignored=frozenset({"page"})
    )
    assert listed.query.order_by == tuple(Resource._meta.ordering)
    assert list(listed.values_list("pk", flat=True)) == [first.pk, third.pk]
    # Without the type's content filter, the parameters filter nothing.
    one, two = profile("one", "a"), profile("two", "a")
    profiles = get_resource_type_for("partner-profiles")
    listed = list_queryset(
        get_request(user_with((PP_READER, "a"))),
        profiles,
        QueryDict("name=one"),
        requirement=[GrantsLeaf(PP)],
    )
    assert list(listed.values_list("pk", flat=True)) == [one.pk, two.pk]


@pytest.mark.django_db
@test_context
def test_page_of_returns_key_order():
    from scrud_django.core.lists import list_queryset, page_of

    stored = [profile(f"p-{n}", "a") for n in range(5)]
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    queryset = list_queryset(request, rtype, QueryDict(), requirement=[GrantsLeaf(PP)])
    rows, count = page_of(queryset, number=1, size=2)
    assert [row.pk for row in rows] == [stored[0].pk, stored[1].pk]
    assert count == 5
    rows, count = page_of(queryset, number=3, size=2)
    assert [row.pk for row in rows] == [stored[4].pk]
    assert count == 5
    with pytest.raises(EmptyPage):
        page_of(queryset, number=4, size=2)
    with pytest.raises(EmptyPage):
        page_of(queryset, number=0, size=2)
    with pytest.raises(ValueError):
        page_of(queryset, number=1, size=0)
    # An empty list has one page, empty.
    assert page_of(queryset.none(), number=1, size=2) == ([], 0)


@pytest.mark.django_db
@test_context
def test_rows_by_keys_keeps_the_keys_order():
    from scrud_django.core.lists import rows_by_keys

    a, b, c = (profile(name, "a") for name in "abc")
    queryset = Resource.objects.filter(resource_type=a.resource_type)
    rows = rows_by_keys(queryset, [c.pk, a.pk, b.pk])
    assert [row.pk for row in rows] == [c.pk, a.pk, b.pk]
    # A key the queryset does not hold is left out.
    assert rows_by_keys(queryset.exclude(pk=a.pk), [c.pk, a.pk]) == [c]
    assert rows_by_keys(queryset, []) == []


@pytest.mark.skipif(not ON_MYSQL, reason="MySQL's sort_buffer_size")
@pytest.mark.django_db
@test_context
def test_page_of_a_large_row_on_mysql():
    """A row whose content is over MySQL's ``sort_buffer_size`` (256 KB by
    default), as `tests.test_ui_branch_large_rows` builds one: a page sorting
    whole rows fails with error 1038; `page_of` sorts the keys alone. The
    resources the requester does not read (in "b") outnumber those it does,
    so that MySQL reads the context rows first and sorts the page (``Using
    filesort``) rather than reading the type's index in key order."""
    from scrud_django.core.lists import list_queryset, page_of

    large = "a" * 262144
    small = profile("small", "a")
    big = profile(large, "a")
    after = profile("after", "a")
    for n in range(20):
        profile(f"unread-{n}", "b")
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    queryset = list_queryset(request, rtype, QueryDict(), requirement=[GrantsLeaf(PP)])
    if not connection.mysql_is_mariadb:
        # The page sorting whole rows fails on MySQL: this test reaches the
        # sort it guards against.
        with pytest.raises(OperationalError) as raised, transaction.atomic():
            list(queryset[:2])
        assert raised.value.args[0] == 1038
    rows, count = page_of(queryset, number=1, size=2)
    assert [row.pk for row in rows] == [small.pk, big.pk]
    assert rows[1].content["name"] == large
    assert count == 3
    rows, _ = page_of(queryset, number=2, size=2)
    assert [row.pk for row in rows] == [after.pk]


@pytest.mark.parametrize(
    "query",
    [
        "",
        "page_size=1",
        "page_size=1000",
        "page_size=1001",
        "page_size=5000",
        "page_size=0",
        "page_size=-3",
        "page_size=x",
        "page_size=",
        "page_size=2.0",
        "page_size=+7",
        "page_size=%207",
        "page_size=1_0",
        "page_size=4&page_size=9",
    ],
)
def test_page_size_of_is_drfs(query):
    """The page size, as DRF's ``PageNumberPagination`` (stock, with the
    list's settings) reads it."""
    from rest_framework.pagination import PageNumberPagination
    from rest_framework.request import Request

    from scrud_django.core.lists import page_size_of

    class Stock(PageNumberPagination):
        page_size = 100
        page_size_query_param = "page_size"
        max_page_size = 1000

    params = QueryDict(query)
    expected = Stock().get_page_size(Request(RequestFactory().get("/?" + query)))
    assert page_size_of(params) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("", 1),
        ("page=", 1),
        ("page=2", "2"),
        ("page=last", 3),
        ("page=x", "x"),
        ("page=1&page=last", 3),
    ],
)
def test_page_number_of_is_drfs(query, expected):
    from django.core.paginator import Paginator
    from rest_framework.pagination import PageNumberPagination
    from rest_framework.request import Request

    from scrud_django.core.lists import page_number_of

    paginator = Paginator(list(range(5)), 2)
    request = Request(RequestFactory().get("/?" + query))
    assert page_number_of(QueryDict(query), paginator) == expected
    assert PageNumberPagination().get_page_number(request, paginator) == expected


@pytest.mark.parametrize(
    "query", ["", "page=2", "page_size=1&page=2&b=1&a=2", "x=%C3%BC&page=3&y="]
)
@pytest.mark.parametrize("number", [1, 2, 3])
def test_page_url_is_drfs(query, number):
    from rest_framework.utils.urls import remove_query_param, replace_query_param

    from scrud_django.core.lists import page_url

    request = RequestFactory().get("/notes/?" + query)
    url = request.build_absolute_uri()
    if number == 1:
        expected = remove_query_param(url, "page")
    else:
        expected = replace_query_param(url, "page", number)
    assert page_url(request, number) == expected


def test_page_url_under_a_published_branch_is_the_apis():
    from scrud_django.core.lists import page_url

    class Mount:
        def canonical(self, url):
            return url.replace("/branch/", "/api/")

    request = RequestFactory().get("/branch/notes/?page=1")
    request.scrud_ui_mount = Mount()
    assert page_url(request, 2) == "http://testserver/api/notes/?page=2"


@pytest.mark.django_db
@test_context
def test_requested_page_pages_as_drf():
    from django.core.paginator import InvalidPage

    from scrud_django.core.lists import ListPage, list_queryset, requested_page

    stored = [profile(f"p-{n}", "a") for n in range(5)]
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    queryset = list_queryset(request, rtype, QueryDict(), requirement=[GrantsLeaf(PP)])

    page = requested_page(queryset, QueryDict("page=2&page_size=2"))
    assert page == ListPage(items=stored[2:4], count=5, number=2, size=2)
    assert (page.page_count, page.has_next, page.has_previous) == (3, True, True)
    last = requested_page(queryset, QueryDict("page=last&page_size=2"))
    assert (last.items, last.number, last.has_next) == (tuple(stored[4:]), 3, False)
    assert requested_page(queryset, QueryDict("page_size=5000")).size == 1000
    assert requested_page(queryset, QueryDict("page_size=0")).size == 100
    for query in ("page=0", "page=x", "page=4&page_size=2", "page=1.0"):
        with pytest.raises(InvalidPage):
            requested_page(queryset, QueryDict(query))
    empty = requested_page(queryset.none(), QueryDict())
    assert (empty.count, empty.page_count, empty.has_next) == (0, 0, False)
    with pytest.raises(InvalidPage):
        requested_page(queryset.none(), QueryDict("page=2"))


@pytest.mark.django_db
@test_context
def test_list_body_is_drfs():
    """The list body (`list_body`, `resource_envelope`) the DRF list route
    answers, byte for byte once rendered as DRF renders it."""
    from rest_framework.renderers import JSONRenderer

    from scrud_django.core.lists import (
        ignored_list_parameters,
        list_body,
        list_queryset,
        requested_page,
    )

    for n in range(3):
        profile(f"p-{n}", "a")
    user = user_with((PP_READER, "a"))
    path = reverse("partner-profiles-list") + "?page_size=1&page=2&z=1"
    client = APIClient()
    client.force_authenticate(user)
    response = client.get(path)
    assert response.status_code == 200, response.content

    request = get_request(user, path)
    rtype = get_resource_type_for("partner-profiles")
    queryset = list_queryset(
        request,
        rtype,
        QueryDict("page_size=1&page=2"),
        requirement=[GrantsLeaf(PP)],
        ignored=ignored_list_parameters(),
    )
    body = list_body(request, rtype, requested_page(queryset, request.GET))
    assert JSONRenderer().render(body) == response.content


@pytest.mark.django_db
@test_context
def test_list_validators_and_read_preconditions_are_drfs():
    from scrudful.conditional import http_timestamp

    from scrud_django.core.lists import (
        evaluate_list_read_preconditions,
        list_queryset,
        list_validators,
        requested_page,
    )

    profile("p", "a")
    user = user_with((PP_READER, "a"))
    client = APIClient()
    client.force_authenticate(user)
    response = client.get(reverse("partner-profiles-list"))
    request = get_request(user, reverse("partner-profiles-list"))
    rtype = get_resource_type_for("partner-profiles")
    requirement = [GrantsLeaf(PP)]
    queryset = list_queryset(request, rtype, request.GET, requirement=requirement)
    etag, last_modified = list_validators(
        request, rtype, queryset, requested_page(queryset, request.GET), requirement
    )
    assert response["ETag"] == quote_etag(etag)
    assert response["Last-Modified"] == http_date(last_modified.timestamp())

    def evaluate(**headers):
        return evaluate_list_read_preconditions(
            "GET", headers, etag=etag, last_modified=last_modified
        )

    assert evaluate() is None
    assert evaluate(**{"if-none-match": quote_etag(etag)}).status == 304
    assert evaluate(**{"if-none-match": '"other"'}) is None
    since = http_date(http_timestamp(last_modified))
    assert evaluate(**{"if-modified-since": since}).status == 304
    assert evaluate(**{"if-match": '"stale"'}).status == 412
    assert evaluate(**{"if-match": "stale"}).status == 400
    for name, value in (
        ("If-None-Match", quote_etag(etag)),
        ("If-Modified-Since", since),
    ):
        assert (
            client.get(response.request["PATH_INFO"], headers={name: value}).status_code
            == 304
        )


@pytest.mark.django_db
@test_context
def test_create_list_validators_are_the_lists_without_the_list_grant():
    """A create's list validators (`create_list_validators`) are those of
    the list its GET copy gets, filtered by the item grants alone: a writer
    without the list grant still gets them."""
    from scrud_django.core.lists import (
        create_list_validators,
        list_queryset,
        list_validators,
        requested_page,
    )

    profile("seen", "a")
    profile("hidden", "b")
    rtype = get_resource_type_for("partner-profiles")
    requirement = [GrantsLeaf(PP)]
    writer = user_with(({"http.GET": [PP], "http.POST": [PP]}, "a"))
    post = RequestFactory().post("/?page_size=1")
    post.user = writer
    validators = create_list_validators(post, rtype, requirement=requirement)
    reader = get_request(user_with((PP_READER, "a")), "/?page_size=1")
    queryset = list_queryset(reader, rtype, reader.GET, requirement=requirement)
    expected = list_validators(
        reader, rtype, queryset, requested_page(queryset, reader.GET), requirement
    )
    assert validators == expected


def test_ui_parameters_name_the_uis():
    from scrud_django.core.lists import UI_PARAMETERS, ignored_list_parameters
    from scrud_django.core.lists import ui_parameters as core_ui_parameters
    from scrud_django.ui.branches.effective import BRANCH_PARAMETER, SAFE_PARAMETER
    from scrud_django.ui.branches.selection import CONFIRM_PARAMETER
    from scrud_django.ui.renderer import MODE_PARAMETER, SCOPE_PARAMETER

    assert UI_PARAMETERS == {
        BRANCH_PARAMETER,
        CONFIRM_PARAMETER,
        SCOPE_PARAMETER,
        MODE_PARAMETER,
        SAFE_PARAMETER,
    }
    assert core_ui_parameters() == ui_parameters() == UI_PARAMETERS
    assert ignored_list_parameters() == {"page", "page_size", "format"} | UI_PARAMETERS


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False], ids=["uniform", "native"])
def test_content_filter_errors_answer_as_on_drf(uniform):
    """A content filter's 400 (`ContentFilterError`) on the DRF list route:
    DRF's ``ValidationError`` as scoped-rbac answers it, which the ninja
    front end answers from the error itself."""
    from scrud_django.exceptions import ContentFilterError

    client = APIClient()
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    message = "An integer is not a content key a list can be filtered on."
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    ):
        response = client.get(reverse("filtered-partner-profiles-list") + "?1=x")
    assert response.status_code == 400
    error = ContentFilterError({"1": [message]})
    if uniform:
        assert response["Content-Type"] == "application/problem+json"
        assert response.json() == {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": "Invalid input.",
            "code": "invalid",
            "errors": {
                "1": [{"code": "invalid", "message": "This value is not valid."}]
            },
        }
        assert response.json() == error.response_data({})
    else:
        assert response["Content-Type"] == "application/json"
        assert response.json() == {"1": [message]}
        assert response.json() == error.native_response_data()


def test_list_page_checks_its_invariants():
    """A `ListPage` holds at least one row per page, a page from 1 and no
    negative count, and keeps its items as a tuple (`types.md` S6)."""
    from scrud_django.core.lists import ListPage

    for bad in ({"size": 0}, {"number": 0}, {"count": -1}):
        with pytest.raises(ValueError):
            ListPage(**{"items": [], "count": 0, "number": 1, "size": 1, **bad})
    page = ListPage(items=[], count=0, number=1, size=1)
    assert page.items == ()
    assert ListPage(items=iter([1, 2]), count=2, number=1, size=2).items == (1, 2)


@pytest.mark.django_db
@test_context
def test_lists_ignore_the_paging_parameters_by_default():
    """Without `ignored`, `list_queryset` and `create_list_validators` ignore
    what the list routes ignore (`ignored_list_parameters`): a content-
    filtered type is not filtered on ``page_size`` (`types.md` S7)."""
    from scrud_django.core.lists import (
        create_list_validators,
        ignored_list_parameters,
        list_queryset,
    )

    note = hooked_note("x", "a")
    rtype = get_resource_type_for("hooked-notes")
    requirement = requirement_for(rtype, "hooked-notes")
    request = get_request(user_with((HOOKED_READER, "a")), "/?page_size=1")
    listed = list_queryset(request, rtype, request.GET, requirement=requirement)
    assert list(listed.values_list("pk", flat=True)) == [note.pk]
    post = RequestFactory().post("/?page_size=1")
    post.user = user_with(({"http.GET": [HOOKED_NOTE_TYPE_URI]}, "a"))
    assert create_list_validators(
        post, rtype, requirement=requirement
    ) == create_list_validators(
        post, rtype, requirement=requirement, ignored=ignored_list_parameters()
    )
