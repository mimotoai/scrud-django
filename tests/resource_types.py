import datetime
import decimal
import importlib.resources

from django.core.exceptions import PermissionDenied
from django.http import Http404

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


def static_text(package, name):
    """A registration's document function: the text of `name` in `package`."""
    return lambda: importlib.resources.files(package).joinpath(name).read_text()


# The arguments of each test type that both front ends serve, shared by the
# DRF registrations (`json_resource_types`, `hooked_resource_types`) and
# `ninja_type_specs`, so that both register the same types with the same
# hook objects.
PARTNER_PROFILES_TYPE = {
    "resource_type_uri": "tests://PartnerProfiles",
    "revision": "2",
    "slug": "partner-profiles",
    "schema_func": static_text("tests.static.partner_profile", "schema.json"),
    "context_func": static_text("tests.static.partner_profile", "ld.json"),
    "indexing_policy_func": static_text(
        "tests.static.partner_profile", "indexing-policy.json"
    ),
}

TODOS_TYPE = {
    "resource_type_uri": "tests://ToDo",
    "revision": "1",
    "slug": "todos",
    "schema_func": static_text("tests.static.todo_flow", "schema.json"),
    "context_func": static_text("tests.static.todo_flow", "ld.json"),
}

TODO_LISTS_TYPE = {
    "resource_type_uri": TODO_LIST_TYPE_URI,
    "revision": "1",
    "slug": "todo-lists",
    "schema_func": static_text("tests.static.todo_list", "schema.json"),
    "context_func": static_text("tests.static.todo_list", "ld.json"),
}


def json_resource_types():
    """This is implemented as a function in the tests so that the json resource types
    may be recreated in `conftest.py`, otherwise they're wiped on each clean of the
    database!.
    """
    return [
        registration.json_resource_type(**PARTNER_PROFILES_TYPE),
        registration.json_resource_type(**TODOS_TYPE),
        registration.json_resource_type(**TODO_LISTS_TYPE),
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


HOOKED_NOTES_TYPE = {
    "resource_type_uri": HOOKED_NOTE_TYPE_URI,
    "revision": "1",
    "slug": "hooked-notes",
    "schema_func": lambda: HOOKED_NOTE_SCHEMA,
    "context_func": lambda: HOOKED_NOTE_CONTEXT,
}

# The hooks ``hooked-notes`` is registered with (`hooked_resource_types`).
HOOKED_NOTES_HOOKS = {
    "checks": (deny_context_b,),
    "object_checks": (hide_secret,),
    "content_filter": True,
}


def hooked_note_type(**hooks):
    """The routes of ``hooked-notes``, registered with `hooks` (the
    registration's ``checks``, ``object_checks``, ``content_filter``)."""
    return registration.json_resource_type(**HOOKED_NOTES_TYPE, **hooks)


CHECKED_NOTE_TYPE_URI = "tests://CheckedNote"

CHECKED_NOTE_SCHEMA = dict(
    HOOKED_NOTE_SCHEMA,
    **{"$id": CHECKED_NOTE_TYPE_URI, "title": "Checked note"},
)

CHECKED_NOTE_CONTEXT = dict(
    HOOKED_NOTE_CONTEXT, **{"@type": "http://example.org/hooked#CheckedNote"}
)

CHECKED_NOTES_TYPE = {
    "resource_type_uri": CHECKED_NOTE_TYPE_URI,
    "revision": "1",
    "slug": "checked-notes",
    "schema_func": lambda: CHECKED_NOTE_SCHEMA,
    "context_func": lambda: CHECKED_NOTE_CONTEXT,
    "checks": (deny_context_b,),
}

SUMMARIES_TYPE = {
    "resource_type_uri": SUMMARY_TYPE_URI,
    "slug": "summaries",
    "schema_func": lambda: SUMMARY_SCHEMA,
    "context_func": lambda: SUMMARY_CONTEXT,
    "compute": summarize,
}


ENCODED_SUMMARY_TYPE_URI = "tests://EncodedSummary"


def encode_summary(request, key):
    """A computed type's ``compute`` whose content is not all JSON: a
    datetime with microseconds, a decimal, a duration and a set."""
    return {
        "key": key,
        "at": datetime.datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=datetime.UTC),
        "amount": decimal.Decimal("1.50"),
        "took": datetime.timedelta(seconds=1.5),
        "tags": {3},
    }


ENCODED_SUMMARIES_TYPE = {
    "resource_type_uri": ENCODED_SUMMARY_TYPE_URI,
    "slug": "encoded-summaries",
    "schema_func": lambda: dict(
        SUMMARY_SCHEMA, **{"$id": ENCODED_SUMMARY_TYPE_URI, "title": "Encoded"}
    ),
    "context_func": lambda: dict(
        SUMMARY_CONTEXT, **{"@type": "http://example.org/summary#Encoded"}
    ),
    "compute": encode_summary,
}


REFUSING_SUMMARY_TYPE_URI = "tests://RefusingSummary"

#: What `refuse_summary` raises, by key.
REFUSALS = {
    "denied": lambda: PermissionDenied("nope"),
    "denied-bare": PermissionDenied,
    "missing": lambda: Http404("nope"),
    "missing-bare": Http404,
}


def refuse_summary(request, key):
    """A computed type's ``compute`` raising what its contract allows
    (``PermissionDenied``, ``Http404``), with a message or without, by key
    (`REFUSALS`); any other key is summarized."""
    if key in REFUSALS:
        raise REFUSALS[key]()
    return summarize(request, key)


REFUSING_SUMMARIES_TYPE = {
    "resource_type_uri": REFUSING_SUMMARY_TYPE_URI,
    "slug": "refusing-summaries",
    "schema_func": lambda: dict(
        SUMMARY_SCHEMA, **{"$id": REFUSING_SUMMARY_TYPE_URI, "title": "Refusing"}
    ),
    "context_func": lambda: dict(
        SUMMARY_CONTEXT, **{"@type": "http://example.org/summary#Refusing"}
    ),
    "compute": refuse_summary,
}


WORKFLOW_NOTE_TYPE_URI = "tests://Note"

WORKFLOW_NOTE_SCHEMA = {
    "$id": WORKFLOW_NOTE_TYPE_URI,
    "$schema": "http://json-schema.org/draft-04/schema",
    "title": "Workflow note",
    "description": "A note under a workflow policy (tests.test_core_parity).",
    "type": "object",
    "properties": {
        "state": {"type": "string"},
        "rbac_context": {"type": "string"},
    },
}

WORKFLOW_NOTE_CONTEXT = {
    "ex": "http://example.org/workflow#",
    "openteams": "http://api.openteams.com/json-ld/",
    "@type": "http://example.org/workflow#Note",
    "rbac_context": {"@id": "openteams:RBACContext"},
    "state": {"@id": "ex:state"},
}

WORKFLOW_NOTES_TYPE = {
    "resource_type_uri": WORKFLOW_NOTE_TYPE_URI,
    "revision": "1",
    "slug": "workflow-notes",
    "schema_func": lambda: WORKFLOW_NOTE_SCHEMA,
    "context_func": lambda: WORKFLOW_NOTE_CONTEXT,
}


def hooked_resource_types():
    """The types registered with per-type hooks (`tests.test_type_hooks`):
    ``hooked-notes`` (a check refusing the context "b", an object check
    hiding secret notes, and the content filter), the computed
    ``summaries`` (a ``compute`` hook, no viewset class) and
    ``encoded-summaries`` (content not all JSON) and ``refusing-summaries``
    (a ``compute`` raising ``PermissionDenied`` or ``Http404``);
    ``checked-notes`` has the check only."""
    return [
        registration.json_resource_type(**CHECKED_NOTES_TYPE),
        hooked_note_type(**HOOKED_NOTES_HOOKS),
        registration.computed_resource_type(**SUMMARIES_TYPE),
        registration.computed_resource_type(**ENCODED_SUMMARIES_TYPE),
        registration.computed_resource_type(**REFUSING_SUMMARIES_TYPE),
        # ``tests://Note``, the type of ``tests.test_core_workflow``, as a
        # stored type (`tests.test_core_parity`).
        registration.json_resource_type(**WORKFLOW_NOTES_TYPE),
    ]


def ninja_type_specs():
    """The `TypeSpec`s of the test types that register no viewset class
    (all of `json_resource_types` and `hooked_resource_types` but
    ``todo-summaries``), from the same arguments and hooks."""
    return [
        registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE),
        registration.json_resource_type_spec(**TODOS_TYPE),
        registration.json_resource_type_spec(**TODO_LISTS_TYPE),
        registration.json_resource_type_spec(**CHECKED_NOTES_TYPE),
        registration.json_resource_type_spec(**HOOKED_NOTES_TYPE, **HOOKED_NOTES_HOOKS),
        registration.computed_resource_type_spec(**SUMMARIES_TYPE),
        registration.computed_resource_type_spec(**ENCODED_SUMMARIES_TYPE),
        registration.computed_resource_type_spec(**REFUSING_SUMMARIES_TYPE),
        registration.json_resource_type_spec(**WORKFLOW_NOTES_TYPE),
    ]
