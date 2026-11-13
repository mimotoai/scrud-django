"""`scrud_django.paginations.StandardResultsSetPagination` pages with the
core's rules (`scrud_django.core.lists`), given its own attributes: a
subclass configuring them is paged as DRF's own ``PageNumberPagination``
pages with the same configuration."""

import pytest
from django.test import RequestFactory
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request

from scrud_django.models import Resource
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.registration import ResourceRegistration

pytestmark = pytest.mark.django_db

CONFIGURATION = {
    "page_size": 2,
    "max_page_size": 3,
    "page_size_query_param": "size",
    "page_query_param": "p",
    "last_page_strings": ("end",),
}


class Configured(StandardResultsSetPagination):
    page_size = 2
    max_page_size = 3
    page_size_query_param = "size"
    page_query_param = "p"
    last_page_strings = ("end",)


class Stock(PageNumberPagination):
    page_size = 2
    max_page_size = 3
    page_size_query_param = "size"
    page_query_param = "p"
    last_page_strings = ("end",)


class NoSizeParameter(StandardResultsSetPagination):
    page_size_query_param = None
    page_size = 4


class StockNoSizeParameter(PageNumberPagination):
    page_size_query_param = None
    page_size = 4


def answer(paginator, queryset, query):
    request = Request(RequestFactory().get("/notes/?" + query))
    try:
        page = paginator.paginate_queryset(queryset, request)
    except NotFound as exc:
        return ("not found", str(exc.detail))
    return (
        [row.pk for row in page],
        paginator.page.paginator.per_page,
        paginator.get_next_link(),
        paginator.get_previous_link(),
    )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "p=2",
        "p=end",
        "p=last",
        "p=0",
        "p=x",
        "page=2",
        "size=1&p=3",
        "size=99",
        "size=0",
        "size=x",
        "page_size=1",
        "size=1&p=2&a=b",
    ],
)
def test_a_subclass_configuration_is_paged_as_drfs(query):
    for n in range(7):
        ResourceRegistration.register(
            {"name": f"p-{n}", "rbac_context": "a"}, "partner-profiles"
        )
    queryset = Resource.objects.filter(resource_type__slug="partner-profiles").order_by(
        "pk"
    )

    assert answer(Configured(), queryset, query) == answer(Stock(), queryset, query)
    assert answer(NoSizeParameter(), queryset, query) == answer(
        StockNoSizeParameter(), queryset, query
    )


def test_the_configuration_is_the_cores():
    from scrud_django.core import lists

    paginator = StandardResultsSetPagination()
    assert (
        paginator.page_size,
        paginator.max_page_size,
        paginator.page_size_query_param,
        paginator.page_query_param,
        paginator.last_page_strings,
    ) == (
        lists.DEFAULT_PAGE_SIZE,
        lists.MAX_PAGE_SIZE,
        lists.PAGE_SIZE_PARAMETER,
        lists.PAGE_PARAMETER,
        lists.LAST_PAGE_STRINGS,
    )
    assert set(CONFIGURATION) <= set(vars(Configured))
