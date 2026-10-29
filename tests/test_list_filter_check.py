"""``scrud_django.E006``: a ``ResourceViewMixin`` or ``GenericViewMixin``
view routed for ``list`` without an ``RbacFilter`` among its
``filter_backends`` is reported by ``manage.py check`` (its list would answer
every request with ``ImproperlyConfigured``, a 500, rather than list every
object)."""

import pytest
from django.core import checks
from django.urls import include, path

from scrud_django import checks as scrud_checks


def unfiltered_patterns():
    from .urls import NoteViewSet

    class UnfilteredNotes(NoteViewSet):
        filter_backends = []

    return [
        path(
            "api/",
            include(
                [
                    path("notes/", UnfilteredNotes.as_view({"get": "list"})),
                    # A detail route lists nothing: not reported.
                    path("notes/<slug>/", UnfilteredNotes.as_view({"get": "retrieve"})),
                    path("ok/", NoteViewSet.as_view({"get": "list"})),
                ]
            ),
        )
    ]


@pytest.mark.django_db
def test_a_list_view_without_an_rbac_filter_is_an_error():
    found = scrud_checks.list_filter_errors(unfiltered_patterns())
    assert [(m.id, m.level) for m in found] == [("scrud_django.E006", checks.ERROR)]
    assert "UnfilteredNotes" in found[0].msg
    assert "api/notes/" in found[0].msg


@pytest.mark.django_db
def test_the_configured_views_pass_the_check():
    assert scrud_checks.check_list_filters(None) == []
    assert not [m for m in checks.run_checks() if m.id == "scrud_django.E006"]
