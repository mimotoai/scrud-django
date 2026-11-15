"""Reading rows without asking the database to sort their content.

MySQL sorts a result with every column it selects ("filesort" with the
columns as addon fields). A row whose JSON ``content`` is larger than the
server's ``sort_buffer_size`` (256 KB by default) fails the sort with error
1038, "Out of sort memory". ``Resource.Meta.ordering`` is ``["id"]``, so an
unordered queryset sorts too. So a query that reads ``content`` either asks
for no order (`unsorted`) and is sorted here (`by_pk`), or sorts the keys
alone and reads the rows by key (`first_by_pk`, `locked_by_pk`,
`scrud_django.core.lists.page_of`, `StandardResultsSetPagination`). Sorting keys alone needs no large buffer
on any database.
"""

from operator import itemgetter


def unsorted(queryset):
    """`queryset` without an ``ORDER BY``, not even the model's default."""
    return queryset.order_by()


def by_pk(rows, pk=itemgetter(0), reverse=False) -> list:
    """`rows` sorted by their key, lowest first (`reverse`: highest first);
    `pk` reads a row's key (default: its first value)."""
    return sorted(rows, key=pk, reverse=reverse)


def first_by_pk(queryset, *fields, last=False):
    """The row of `queryset` with the lowest key (`last`: the highest), as
    the values of `fields` (one field: its value) or, with no `fields`, the
    instance; ``None`` when there is none. Two queries: the keys sorted
    alone, then the row by its key."""
    pk = queryset.order_by("-pk" if last else "pk").values_list("pk", flat=True).first()
    if pk is None:
        return None
    rows = unsorted(queryset.filter(pk=pk))
    if fields:
        rows = rows.values_list(*fields, flat=len(fields) == 1)
    found = list(rows[:1])
    return found[0] if found else None


def locked_by_pk(queryset) -> list:
    """The instances of `queryset`, each locked (``select_for_update``) until
    the transaction ends, lowest key first. The rows are locked in key
    order (the keys sorted alone), then read by key, unsorted."""
    pks = list(queryset.select_for_update().order_by("pk").values_list("pk", flat=True))
    if not pks:
        return []
    return by_pk(unsorted(queryset.filter(pk__in=pks)), pk=lambda row: row.pk)
