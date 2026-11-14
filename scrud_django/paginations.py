import math

from django.db import connections
from django.db.models import QuerySet
from rest_framework.pagination import PageNumberPagination

from scrud_django.core.lists import rows_by_keys


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000

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
        return _canonical(self.request, super().get_next_link())

    def get_previous_link(self):
        return _canonical(self.request, super().get_previous_link())

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


def _canonical(request, url):
    """`url`, built from the request's URL, as the API's URL: under a
    published branch's prefix (``scrud_django.ui``'s ``Mount``), JSON keeps
    the API's links."""
    mount = getattr(request, "scrud_ui_mount", None)
    return mount.canonical(url) if mount is not None and url else url
