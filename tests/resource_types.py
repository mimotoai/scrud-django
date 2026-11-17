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
    # As DRF takes them: an empty message, one that is not a string, and a
    # PermissionDenied's code.
    "denied-empty": lambda: PermissionDenied(""),
    "missing-empty": lambda: Http404(""),
    "denied-dict": lambda: PermissionDenied({"key": "x"}),
    "missing-dict": lambda: Http404({"key": "x", "nested": [1, {"deep": None}]}),
    "missing-list": lambda: Http404(["x", 2]),
    "missing-number": lambda: Http404(42),
    "denied-coded": lambda: PermissionDenied("nope", "custom_code"),
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


# The context `tests.urls.ContextDenyingIsAuthorized` refuses everything in
# (its ``DENIED``), which the types of `drf_only_types` and
# `hand_routed_types` refuse: by that ``IsAuthorized`` subclass alone, or by
# their hooks too (``hooked-denied-notes``).
DENIED = "denied"

DENIED_NOTE_TYPE_URI = "tests://DeniedNote"
CORE_DENIED_NOTE_TYPE_URI = "tests://CoreDeniedNote"
HOOKED_DENIED_NOTE_TYPE_URI = "tests://HookedDeniedNote"
CHECKED_DENIED_NOTE_TYPE_URI = "tests://CheckedDeniedNote"
OBJECT_CHECKED_DENIED_NOTE_TYPE_URI = "tests://ObjectCheckedDeniedNote"
HAND_ROUTED_NOTE_TYPE_URI = "tests://HandRoutedNote"
CORE_HAND_ROUTED_NOTE_TYPE_URI = "tests://CoreHandRoutedNote"


def not_in_denied(request, resource_type, body, context):
    """The check of ``hooked-denied-notes``: nothing is written in the
    context "denied" (`ContextDenyingIsAuthorized`'s rule)."""
    return context != DENIED


def readable_outside_denied(request, resource_type, obj):
    """The object check of ``hooked-denied-notes``: a note stored in the
    context "denied" is hidden."""
    return DENIED not in rbac_contexts_of(obj)


def note_type(type_uri, slug, title):
    """The registration arguments of a note type `slug` (`type_uri`)."""
    name = type_uri.removeprefix("tests://")
    return {
        "resource_type_uri": type_uri,
        "revision": "1",
        "slug": slug,
        "schema_func": lambda: dict(
            HOOKED_NOTE_SCHEMA, **{"$id": type_uri, "title": title}
        ),
        "context_func": lambda: dict(
            HOOKED_NOTE_CONTEXT, **{"@type": f"http://example.org/hooked#{name}"}
        ),
    }


def drf_only_types():
    """Register the types of ``tests.drf_only_urls`` served by the custom
    viewset ``tests.urls.ContextDenyingResourceViewSet`` (their rows, hooks
    and routes), returning their routes: ``denied-notes`` (DRF-only
    authorization), ``core-denied-notes`` (``ui_authorization="core"``),
    ``hooked-denied-notes`` (``checks=`` and ``object_checks=`` declaring
    the viewset's rule), and ``checked-denied-notes`` and
    ``object-checked-denied-notes``, which declare only one of the two."""
    from .urls import ContextDenyingIsAuthorized, ContextDenyingResourceViewSet

    assert ContextDenyingIsAuthorized.DENIED == DENIED
    viewset = {"viewset_class": ContextDenyingResourceViewSet}
    return registration.resource_types(
        registration.json_resource_type(
            **note_type(DENIED_NOTE_TYPE_URI, "denied-notes", "Denied note"),
            **viewset,
        ),
        registration.json_resource_type(
            **note_type(CORE_DENIED_NOTE_TYPE_URI, "core-denied-notes", "Core note"),
            **viewset,
            ui_authorization="core",
        ),
        registration.json_resource_type(
            **note_type(
                HOOKED_DENIED_NOTE_TYPE_URI, "hooked-denied-notes", "Hooked note"
            ),
            **viewset,
            checks=(not_in_denied,),
            object_checks=(readable_outside_denied,),
        ),
        registration.json_resource_type(
            **note_type(
                CHECKED_DENIED_NOTE_TYPE_URI, "checked-denied-notes", "Checked note"
            ),
            **viewset,
            checks=(not_in_denied,),
        ),
        registration.json_resource_type(
            **note_type(
                OBJECT_CHECKED_DENIED_NOTE_TYPE_URI,
                "object-checked-denied-notes",
                "Object-checked note",
            ),
            **viewset,
            object_checks=(readable_outside_denied,),
        ),
    )


def hand_routed_types():
    """Register the types of ``tests.drf_only_urls`` whose routes are built
    by hand, not by registration (``ResourceTypeRegistration(build_urls=
    False)``, then ``ContextDenyingResourceViewSet.as_view(...)`` named as
    registration names them), returning those routes: ``hand-routed-notes``,
    and ``core-hand-routed-notes``, whose options declare
    ``ui_authorization="core"`` (`register_type_options`)."""
    from django.urls import path

    from scrud_django.core.types import TypeOptions, register_type_options

    from .urls import ContextDenyingResourceViewSet

    routes = []
    for type_uri, slug, title in (
        (HAND_ROUTED_NOTE_TYPE_URI, "hand-routed-notes", "Hand-routed note"),
        (CORE_HAND_ROUTED_NOTE_TYPE_URI, "core-hand-routed-notes", "Core hand"),
    ):
        arguments = note_type(type_uri, slug, title)
        if slug.startswith("core-"):
            register_type_options(
                type_uri, TypeOptions(ui_authorization="core"), slug=slug
            )
        registration.ResourceTypeRegistration(**arguments, build_urls=False)
        routes += [
            path(
                f"{slug}/",
                ContextDenyingResourceViewSet.as_view(
                    {"get": "list", "head": "list", "post": "create"},
                    resource_type_name=slug,
                ),
                name=f"{slug}-list",
            ),
            path(
                f"{slug}/<str:slug>/",
                ContextDenyingResourceViewSet.as_view(
                    {"get": "retrieve", "put": "update", "delete": "destroy"},
                    resource_type_name=slug,
                ),
                name=f"{slug}-detail",
            ),
        ]
    return routes


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
