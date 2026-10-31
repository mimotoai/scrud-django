"""``--reverse`` (``tests/conftest.py``) reverses the order of the tests
within each of pytest-django's groups, and keeps the groups in pytest-django's
order: database tests, then transactional ones (which flush the database,
and so the resource types ``django_db_setup`` registers once per session),
then the others. A transactional test must not run first in the reverse
run."""

import pytest
from pytest_django.plugin import (
    pytest_collection_modifyitems as pytest_django_ordering,
)

from tests import conftest


class Item:
    """What pytest-django's ordering reads of a test item."""

    def __init__(self, name, marker=None, fixtures=()):
        self.name = name
        self.cls = None
        self.fixturenames = list(fixtures)
        self.marker = marker

    def get_closest_marker(self, name):
        return self.marker if name == "django_db" else None

    def __repr__(self):
        return self.name


class Config:
    def __init__(self, reverse):
        self.reverse = reverse

    def getoption(self, name):
        assert name == "--reverse"
        return self.reverse


DB = pytest.mark.django_db.mark
TRANSACTIONAL = pytest.mark.django_db(transaction=True).mark


def collected():
    """Items in file order: database, transactional (by marker and by
    fixture) and plain tests, interleaved."""
    return [
        Item("db1", DB),
        Item("tx1", TRANSACTIONAL),
        Item("plain1"),
        Item("db2", DB),
        Item("tx2", fixtures=["transactional_db"]),
        Item("plain2"),
        Item("db3", fixtures=["db"]),
    ]


def ordered(reverse):
    """The items as pytest orders them: the conftest hook wraps the others,
    pytest-django's among them."""
    items = collected()
    hook = conftest.pytest_collection_modifyitems(Config(reverse), items)
    next(hook)
    pytest_django_ordering(items)
    with pytest.raises(StopIteration):
        hook.send(None)
    return [item.name for item in items]


def test_the_forward_order_is_pytest_djangos():
    assert ordered(reverse=False) == [
        "db1",
        "db2",
        "db3",
        "tx1",
        "tx2",
        "plain1",
        "plain2",
    ]


def test_the_reverse_order_reverses_each_group_and_keeps_the_groups():
    assert ordered(reverse=True) == [
        "db3",
        "db2",
        "db1",
        "tx2",
        "tx1",
        "plain2",
        "plain1",
    ]
