import math

from django.db import connections
from django.db.models import QuerySet
from rest_framework.pagination import PageNumberPagination

from scrud_django.core.lists import (
    DEFAULT_PAGE_SIZE,
    LAST_PAGE_STRINGS,
    MAX_PAGE_SIZE,
    PAGE_PARAMETER,
    PAGE_SIZE_PARAMETER,
    page_number_of,
    page_size_of,
    page_url,
    rows_by_keys,
)


class StandardResultsSetPagination(PageNumberPagination):
    """DRF's page-number pagination, with the paging rules of
    `scrud_django.core.lists` (which the ninja front end pages with too):
    the page size (`page_size_of`), the page number (`page_number_of`) and
    the next and previous pages' URLs (`page_url`), each given this
    paginator's attributes, so that a subclass setting its own (a
    ``page_size``, a ``page_query_param`` ...) is paged by them, as DRF's
    own paginator would."""

    page_size = DEFAULT_PAGE_SIZE
    page_query_param = PAGE_PARAMETER
    page_size_query_param = PAGE_SIZE_PARAMETER
    max_page_size = MAX_PAGE_SIZE
    last_page_strings = LAST_PAGE_STRINGS

    def get_page_size(self, request):
        return page_size_of(
            request.query_params,
            param=self.page_size_query_param,
            default=self.page_size,
            maximum=self.max_page_size,
        )

    def get_page_number(self, request, paginator):
        return page_number_of(
            request.query_params,
            paginator,
            param=self.page_query_param,
            last=self.last_page_strings,
        )

    def paginate_queryset(self, queryset, request, view=None):
        """DRF's page. On MySQL it is found among the keys alone, then its
        rows are read by key: MySQL sorts a page with every column it
        selects, so a page holding a row whose JSON content is over its
        ``sort_buffer_size`` would fail (error 1038, `scrud_django.rows`);
        sorting the keys alone does not. The page keeps the queryset's
        order (`scrud_django.core.lists.rows_by_keys`, as the core's
        ``page_of`` reads a page)."""
        if (
            not isinstance(queryset, QuerySet)
            or connections[queryset.db].vendor != "mysql"
        ):
            # Only MySQL needs it: PostgreSQL spills a large sort to disk,
            # and SQLite has no sort buffer; one query is enough there.
            return super().paginate_queryset(queryset, request, view)
        pks = super().paginate_queryset(
            queryset.values_list("pk", flat=True), request, view
        )
        if pks is None:
            return None
        return rows_by_keys(queryset, pks)

    def get_next_link(self):
        if not self.page.has_next():
            return None
        return page_url(
            self.request, self.page.next_page_number(), param=self.page_query_param
        )

    def get_previous_link(self):
        if not self.page.has_previous():
            return None
        return page_url(
            self.request,
            self.page.previous_page_number(),
            param=self.page_query_param,
        )

    def get_paginated_response(self, data, *args, **kwargs):

        return {
            'count': self.page.paginator.count,
            # The page size this request used (?page_size, capped at
            # max_page_size), not the default; 0 pages for an empty list.
            'page_count': math.ceil(
                self.page.paginator.count / self.page.paginator.per_page
            ),
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'content': data,
        }
