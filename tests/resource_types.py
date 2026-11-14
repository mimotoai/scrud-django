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


HOOKED_NOTE_TYPE_URI = "tests://HookedNote"
SUMMARY_TYPE_URI = "tests://Summary"

HOOKED_NOTE_SCHEMA = {
    "$id": HOOKED_NOTE_TYPE_URI,
    "$schema": "http://json-schema.org/draft-04/schema",
    "title": "Hooked note",
    "description": "A note whose type declares hooks at registration.",
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "secret": {"type": "boolean"},
        "rbac_context": {"type": "string"},
    },
}

HOOKED_NOTE_CONTEXT = {
    "ex": "http://example.org/hooked#",
    "openteams": "http://api.openteams.com/json-ld/",
    "@type": "http://example.org/hooked#HookedNote",
    "rbac_context": {"@id": "openteams:RBACContext"},
    "text": {"@id": "ex:text"},
    "secret": {"@id": "ex:secret"},
}

SUMMARY_SCHEMA = {
    "$id": SUMMARY_TYPE_URI,
    "$schema": "http://json-schema.org/draft-04/schema",
    "title": "Summary",
    "description": "Computed by the type's registered compute hook.",
    "type": "object",
    "properties": {"key": {"type": "string"}, "user": {"type": "string"}},
}

SUMMARY_CONTEXT = {
    "ex": "http://example.org/summary#",
    "@type": "http://example.org/summary#Summary",
    "key": {"@id": "ex:key"},
    "user": {"@id": "ex:user"},
}


def deny_context_b(request, resource_type, body, context):
    """A type's check: nothing is written in the context "b"."""
    return context != "b"


def hide_secret(request, resource_type, obj):
    """A type's object check: a note marked ``secret`` is refused."""
    return obj.get("secret") is not True


def summarize(request, key):
    """A computed type's ``compute``: the key and who asked."""
    return {"key": key, "user": request.user.get_username()}


def hooked_note_type(**hooks):
    """The routes of ``hooked-notes``, registered with `hooks` (the
    registration's ``checks``, ``object_checks``, ``content_filter``)."""
    return registration.json_resource_type(
        resource_type_uri=HOOKED_NOTE_TYPE_URI,
        revision="1",
        slug="hooked-notes",
        schema_func=lambda: HOOKED_NOTE_SCHEMA,
        context_func=lambda: HOOKED_NOTE_CONTEXT,
        **hooks,
    )


CHECKED_NOTE_TYPE_URI = "tests://CheckedNote"

CHECKED_NOTE_SCHEMA = dict(
    HOOKED_NOTE_SCHEMA,
    **{"$id": CHECKED_NOTE_TYPE_URI, "title": "Checked note"},
)

CHECKED_NOTE_CONTEXT = dict(
    HOOKED_NOTE_CONTEXT, **{"@type": "http://example.org/hooked#CheckedNote"}
)


def hooked_resource_types():
    """The types registered with per-type hooks (`tests.test_type_hooks`):
    ``hooked-notes`` (a check refusing the context "b", an object check
    hiding secret notes, and the content filter) and the computed
    ``summaries`` (a ``compute`` hook, no viewset class); ``checked-notes``
    has the check only."""
    return [
        registration.json_resource_type(
            resource_type_uri=CHECKED_NOTE_TYPE_URI,
            revision="1",
            slug="checked-notes",
            schema_func=lambda: CHECKED_NOTE_SCHEMA,
            context_func=lambda: CHECKED_NOTE_CONTEXT,
            checks=(deny_context_b,),
        ),
        hooked_note_type(
            checks=(deny_context_b,),
            object_checks=(hide_secret,),
            content_filter=True,
        ),
        registration.computed_resource_type(
            resource_type_uri=SUMMARY_TYPE_URI,
            slug="summaries",
            schema_func=lambda: SUMMARY_SCHEMA,
            context_func=lambda: SUMMARY_CONTEXT,
            compute=summarize,
        ),
    ]
