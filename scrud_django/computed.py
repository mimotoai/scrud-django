"""Deprecated, for one release: the old path of the computed types' names.
Each name read from here is the moved object itself, with a
``DeprecationWarning`` (once) naming its new home (`__getattr__`): the
neutral helpers (`MOVED_TO_CORE`) are `scrud_django.core.computed`'s, and
resolve without DRF; the DRF view (`MOVED_TO_DRF`) is
`scrud_django.drf.computed`'s, which imports DRF."""

from scrud_django._aliases import moved_name

#: The neutral names, which moved to `scrud_django.core.computed`.
MOVED_TO_CORE = frozenset(
    {"computed_answer", "computed_etag", "computed_link", "not_modified"}
)

#: The DRF names, which moved to `scrud_django.drf.computed`.
MOVED_TO_DRF = frozenset({"ComputedResourceViewSet"})


def __getattr__(name):
    """A name in `MOVED_TO_CORE` or `MOVED_TO_DRF`: the object of its new
    module, with a ``DeprecationWarning`` (once)."""
    if name in MOVED_TO_CORE:
        return moved_name(globals(), name, "scrud_django.core.computed")
    if name in MOVED_TO_DRF:
        return moved_name(globals(), name, "scrud_django.drf.computed")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
