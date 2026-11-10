import importlib.resources

from scrud_django import registration
from scrud_django.computed import ComputedResourceViewSet
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.permissions import may_read_in_context

TODO_SUMMARY_TYPE_URI = "tests://TodoSummary"
TODO_LIST_TYPE_URI = "tests://TodoList"


class TodoSummaryViewSet(ComputedResourceViewSet):
    """The titles of the to-do lists in the context `key` that the requesting
    user may read: computed, never stored."""

    def compute(self, request, key):
        titles = []
        lists = Resource.objects.filter(resource_type__type_uri=TODO_LIST_TYPE_URI)
        for stored in lists:
            content = stored.content
            if content.get("rbac_context", "") != key:
                continue
            if may_read_in_context(
                request, TODO_LIST_TYPE_URI, content, rbac_contexts_of(content)
            ):
                titles.append(content["title"])
        return {"context": key, "titles": sorted(titles)}


def json_resource_types():
    """This is implemented as a function in the tests so that the json resource types
    may be recreated in `conftest.py`, otherwise they're wiped on each clean of the
    database!.
    """
    return [
        registration.json_resource_type(
            resource_type_uri="tests://PartnerProfiles",
            revision="2",
            slug="partner-profiles",
            schema_func=lambda: (
                importlib.resources.files("tests.static.partner_profile")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.partner_profile")
                .joinpath("ld.json")
                .read_text()
            ),
            indexing_policy_func=lambda: (
                importlib.resources.files("tests.static.partner_profile")
                .joinpath("indexing-policy.json")
                .read_text()
            ),
        ),
        registration.json_resource_type(
            resource_type_uri="tests://ToDo",
            revision="1",
            slug="todos",
            schema_func=lambda: (
                importlib.resources.files("tests.static.todo_flow")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.todo_flow")
                .joinpath("ld.json")
                .read_text()
            ),
        ),
        registration.json_resource_type(
            resource_type_uri="tests://TodoList",
            revision="1",
            slug="todo-lists",
            schema_func=lambda: (
                importlib.resources.files("tests.static.todo_list")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.todo_list")
                .joinpath("ld.json")
                .read_text()
            ),
        ),
        registration.computed_resource_type(
            resource_type_uri=TODO_SUMMARY_TYPE_URI,
            slug="todo-summaries",
            schema_func=lambda: (
                importlib.resources.files("tests.static.todo_summary")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.todo_summary")
                .joinpath("ld.json")
                .read_text()
            ),
            viewset_class=TodoSummaryViewSet,
        ),
    ]
