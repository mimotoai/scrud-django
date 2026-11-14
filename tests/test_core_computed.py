"""`scrud_django.core.computed`: a computed type's answer without DRF (its
content and ETag, ``If-None-Match``, its Links, the authenticated requester
it asks), as ``ComputedResourceViewSet`` and the ninja computed route give
it; and the service catalog's routes (`scrud_django.ScrudServices`), within
the serving API's namespace."""

import datetime
import decimal
import hashlib
import ipaddress
import json
import uuid
from types import MappingProxyType, SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory
from django.urls import NoReverseMatch, resolve
from django.utils.functional import lazy
from rest_framework.renderers import JSONRenderer
from rest_framework.utils.encoders import JSONEncoder
from scoped_rbac import access

from scrud_django import ScrudServices, registration
from scrud_django.core import computed
from scrud_django.core.urls import first_absolute_reverse

from .resource_types import SUMMARIES_TYPE


def drf_etag(content):
    """The ETag ``ComputedResourceViewSet`` gave before the core did: DRF's
    ``JSONEncoder``."""
    canonical = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        cls=JSONEncoder,
    )
    return '"' + hashlib.sha256(canonical.encode()).hexdigest() + '"'


@pytest.mark.parametrize(
    "value",
    [
        "text é",
        datetime.datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=datetime.UTC),
        datetime.datetime(2026, 1, 2, 3, 4, 5),
        datetime.datetime(
            2026, 1, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
        ),
        datetime.date(2026, 1, 2),
        datetime.time(3, 4, 5, 6),
        datetime.timedelta(days=1, seconds=1.5),
        decimal.Decimal("1.50"),
        uuid.UUID(int=7),
        ipaddress.ip_address("10.0.0.1"),
        ipaddress.ip_network("10.0.0.0/8"),
        b"bytes",
        lazy(lambda: "lazy", str)(),
        (1, 2),
        {3, 4} - {4},
        MappingProxyType({"a": 5}),
        (i for i in (7, 8)),
    ],
    ids=repr,
)
def test_computed_etag_is_drfs(value):
    # Generators are consumed by encoding: one for each encoder.
    if hasattr(value, "__next__"):
        assert computed.computed_etag({"v": (i for i in (7, 8))}) == drf_etag(
            {"v": (i for i in (7, 8))}
        )
    else:
        assert computed.computed_etag({"v": value}) == drf_etag({"v": value})


def test_computed_etag_refuses_an_aware_time_as_drf_does():
    aware = datetime.time(3, tzinfo=datetime.UTC)
    with pytest.raises(ValueError):
        drf_etag({"t": aware})
    with pytest.raises(ValueError):
        computed.computed_etag({"t": aware})


def test_computed_etag_of_the_queryset_of_a_model(django_db_blocker):
    with django_db_blocker.unblock():
        rows = User.objects.none()
        assert computed.computed_etag({"q": rows}) == drf_etag({"q": rows})


@pytest.mark.django_db
def test_computed_answer_is_the_registered_compute_and_its_etag():
    spec = registration.computed_resource_type_spec(**SUMMARIES_TYPE)
    spec.register()
    rtype = registration.get_resource_type_for("summaries")
    request = SimpleNamespace(user=SimpleNamespace(get_username=lambda: "ann"))

    content, etag = computed.computed_answer(request, rtype, "k")

    assert content == {"key": "k", "user": "ann"}
    assert etag == computed.computed_etag(content)


def test_computed_answer_of_a_compute_given():
    def compute(request, key):
        return {"key": key}

    assert computed.computed_answer(None, None, "k", compute=compute) == (
        {"key": "k"},
        computed.computed_etag({"key": "k"}),
    )


@pytest.mark.parametrize("content", [["a list"], "text", None, 3])
def test_computed_answer_refuses_content_not_a_mapping(content):
    class Owner:
        def compute(self, request, key):
            return content

    def hook(request, key):
        return content

    with pytest.raises(TypeError, match=r"^Owner\.compute returned a "):
        computed.computed_answer(None, None, "k", compute=Owner().compute)
    with pytest.raises(TypeError, match=r"hook returned a .*, not a mapping\.$"):
        computed.computed_answer(None, None, "k", compute=hook)


@pytest.mark.django_db
def test_computed_answer_without_a_compute_is_not_implemented():
    rtype = SimpleNamespace(slug="no-such-computed-type")
    with pytest.raises(NotImplementedError):
        computed.computed_answer(None, rtype, "k")


@pytest.mark.parametrize(
    "header, matches",
    [
        ("", False),
        ('"e"', True),
        ('W/"e"', True),
        ('"x", "e"', True),
        ('"x",W/"e"', True),
        ("*", True),
        ('"x"', False),
        ("e", False),
    ],
)
def test_not_modified_compares_if_none_match_weakly(header, matches):
    request = RequestFactory().get("/", headers={"If-None-Match": header})
    assert computed.not_modified(request, '"e"') is matches


def test_not_modified_without_if_none_match():
    assert computed.not_modified(RequestFactory().get("/"), '"e"') is False


def test_check_authenticated_refuses_the_anonymous():
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    with pytest.raises(access.AccessDenied):
        computed.check_authenticated(request)
    del request.user
    with pytest.raises(access.AccessDenied):
        computed.check_authenticated(request)
    request.user = User(username="u")
    computed.check_authenticated(request)


@pytest.mark.django_db
@pytest.mark.urls("tests.parity_urls")
def test_computed_link_names_the_types_documents_on_the_serving_mount():
    registration.computed_resource_type_spec(**SUMMARIES_TYPE).register()
    rtype = registration.get_resource_type_for("summaries")
    schema, context = rtype.schema.pk, rtype.context.pk
    for mount in ("drf", "ninja"):
        path = f"/{mount}/summaries/k/"
        request = RequestFactory().get(path)
        request.resolver_match = resolve(path)

        assert computed.computed_link(request, rtype) == (
            f'<http://testserver/{mount}/json-schema/{schema}/>; rel="describedby"; '
            'type="application/schema+json", '
            f"<http://testserver/{mount}/json-ld/{context}/>; "
            'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
        )
    # Without a request, within the namespace given.
    assert computed.computed_link(None, rtype, namespace="scrud-ninja").startswith(
        f"</ninja/json-schema/{schema}/>"
    )
    assert computed.computed_link(None, None) is None


@pytest.mark.django_db
@pytest.mark.urls("tests.namespaced_urls")
def test_first_absolute_reverse_prefers_the_serving_namespace():
    def resolved(path):
        request = RequestFactory().get(path)
        request.resolver_match = resolve(path)
        return request

    names = ("no-such-route", "partner-profiles-detail")
    namespaced = resolved("/ns/partner-profiles/a/")
    plain = resolved("/services/")

    assert first_absolute_reverse(names, args=["b"], request=namespaced) == (
        "http://testserver/ns/partner-profiles/b/"
    )
    assert first_absolute_reverse(names, args=["b"], request=plain) == (
        "http://testserver/partner-profiles/b/"
    )
    # The namespace first, for every name, then globally.
    assert first_absolute_reverse(
        ("services", "partner-profiles-detail"), args=(), request=namespaced
    ) == ("http://testserver/services/")
    with pytest.raises(NoReverseMatch):
        first_absolute_reverse(("no-such-route",), request=plain)
    with pytest.raises(NoReverseMatch):
        first_absolute_reverse((), request=plain)
    # Any iterable of names.
    assert first_absolute_reverse(iter(names), args=["b"], request=namespaced) == (
        "http://testserver/ns/partner-profiles/b/"
    )


@pytest.mark.django_db
@pytest.mark.urls("tests.parity_urls")
def test_a_catalog_lists_each_list_on_the_serving_mount():
    from .front_ends import serve_both

    serve_both()
    catalog = ScrudServices()
    catalog.add_service("tests://A", "partner-profiles")
    catalog.add_service("tests://R", ("role", "scoped-rbac-role"))
    catalog.add_service("tests://F", lambda request=None: "http://example.com/f/")
    for mount in ("drf", "ninja"):
        request = RequestFactory().get(f"/{mount}/services/")
        request.resolver_match = resolve(f"/{mount}/services/")

        served = catalog.catalog(request)

        assert served == {
            "tests://A": f"http://testserver/{mount}/partner-profiles/",
            "tests://R": f"http://testserver/{mount}/roles/",
            "tests://F": "http://example.com/f/",
        }
        assert catalog.etag_for(request) == catalog.etag_of(served)


def test_served_content_is_the_json_drf_writes():
    content = {
        "z": datetime.datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=datetime.UTC),
        "n": decimal.Decimal("1.50"),
        "d": datetime.timedelta(seconds=1.5),
        "s": {3},
        "é": "ü",
    }
    served = computed.served_content(content)

    assert served == {
        "z": "2026-01-02T03:04:05.678901Z",
        "n": 1.5,
        "d": "1.5",
        "s": [3],
        "é": "ü",
    }
    # In the order DRF writes it, and hashing as the content does.
    assert list(served) == list(content)
    drf = JSONRenderer().render(content)
    assert JSONRenderer().render(served) == drf
    assert computed.computed_etag(served) == computed.computed_etag(content)
