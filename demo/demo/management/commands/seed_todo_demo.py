"""Seed the to-do demo: users and roles, a to-do list, the UI components and
stylesheets that render it, and three UI branches: ``admin`` published at
``/manage/`` (not ``/admin/resources/``: Django's admin claims ``/admin/``),
``customer`` published at ``/todo/``, and ``testing-admin``, a working branch
over ``admin``.

Idempotent: a second run creates nothing new. Users, role assignments,
components and stylesheets are brought back to the definitions below; the
"Groceries" list is left as it is unless ``--reset`` is given.

Resources are written with `ResourceRegistration`, which saves them with
`Resource.save()` and so keeps their `ResourceContext` rows in step.
"""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.urls import reverse
from django.utils.timezone import now
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    write_db,
)
from scrud_django.ui.branches import (
    MAIN,
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_CSS_SLUG,
    UI_CSS_TYPE_URI,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.branches.store import branches, ensure_main
from scrud_django.ui.branches.versions import current_version
from scrud_django.ui.engines import compile_template
from scrud_django.ui.types import (
    UI_COMPONENT_SLUG,
    UI_COMPONENT_TYPE_URI,
    UI_STYLESHEET_SLUG,
    UI_STYLESHEET_TYPE_URI,
)

TODO_LIST_TYPE_URI = "http://example.org/todo#TodoList"
TODO_LIST_SLUG = "todo-lists"
PROJECT = "project-a"
PREFIXES = {"ex": "http://example.org/todo#"}

UI_TYPES = [
    uri
    for type_uri in (UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI)
    for uri in (type_uri, collection_type_uri_for(type_uri))
]
TODO_LISTS = collection_type_uri_for(TODO_LIST_TYPE_URI)
# What a UI author writes on a branch: components, stylesheets, CSS, the
# branches themselves, their versions (read) and pushes.
BRANCH_TYPES = [
    uri
    for type_uri in (
        UI_COMPONENT_TYPE_URI,
        UI_STYLESHEET_TYPE_URI,
        UI_CSS_TYPE_URI,
        UI_BRANCH_TYPE_URI,
        UI_BRANCH_VERSION_TYPE_URI,
        UI_PUSH_TYPE_URI,
    )
    for uri in (type_uri, collection_type_uri_for(type_uri))
]
WRITES = ("GET", "POST", "PUT", "DELETE")

# The branches' RBAC contexts.
ADMIN_CONTEXT = "ui-admin"
CUSTOMER_CONTEXT = "ui-customer"
TESTING_CONTEXT = "ui-testing"

# {username: {rbac_context: role definition}}; the password is the username.
USERS = {
    "owner": {
        PROJECT: {
            "http.GET": [TODO_LIST_TYPE_URI, TODO_LISTS, *UI_TYPES],
            "http.PUT": [TODO_LIST_TYPE_URI],
            "http.DELETE": [TODO_LIST_TYPE_URI],
            "http.POST": [TODO_LIST_TYPE_URI, TODO_LISTS],
        },
        DEFAULT_CONTEXT: {"http.GET": UI_TYPES},
        # May open the customer branch's prefix too.
        CUSTOMER_CONTEXT: {"http.GET": [UI_BRANCH_TYPE_URI]},
    },
    # May open /todo/ (GET on the customer branch) and not /manage/.
    "reader": {
        PROJECT: {"http.GET": [TODO_LIST_TYPE_URI, TODO_LISTS]},
        CUSTOMER_CONTEXT: {"http.GET": [UI_BRANCH_TYPE_URI]},
    },
    # Writes the UI on main and in all three branches' contexts, and reads
    # the lists the pages show.
    "author": {
        DEFAULT_CONTEXT: {f"http.{method}": BRANCH_TYPES for method in WRITES},
        PROJECT: {"http.GET": [TODO_LIST_TYPE_URI, TODO_LISTS]},
        ADMIN_CONTEXT: {f"http.{method}": BRANCH_TYPES for method in WRITES},
        CUSTOMER_CONTEXT: {f"http.{method}": BRANCH_TYPES for method in WRITES},
        TESTING_CONTEXT: {f"http.{method}": BRANCH_TYPES for method in WRITES},
    },
}

GROCERIES = {
    "title": "Groceries",
    "rbac_context": PROJECT,
    "items": [
        {"text": "Buy milk", "done": False},
        {"text": "Buy bread", "done": False},
    ],
}

# The page: the built-in page's head (CSRF token, htmx, scrud-ui.js), a
# sidebar that lazy-loads the lists in the "sidebar" scope, and the body.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{{ csrf_token }}">
<title>{{ title }}</title>
<script src="{{ static.htmx }}" defer></script>
<script src="{{ static.scrud_ui }}" defer></script>
{% for href in css %}<link rel="stylesheet" href="{{ href }}">{% endfor %}
<style>
body { font-family: system-ui, sans-serif; margin: 0; color: #222; }
.todo-layout { display: flex; gap: 2rem; padding: 1rem 2rem; }
.todo-layout aside { flex: 0 0 16rem; border-right: 1px solid #ddd; padding-right: 1rem; }
.todo-layout main { flex: 1; }
.todo-readonly { color: #888; font-size: .85rem }
.todo-list ul { list-style: none; padding: 0; }
.todo-row { display: flex; gap: .5rem; align-items: center; margin: .25rem 0; }
.todo-row input[type=text] { width: 20rem; }
.todo-row-view.done { text-decoration: line-through; color: #888; }
.scrud-append { margin-top: .75rem; }
.scrud-notice-error { background: #fde8e8; border: 1px solid #e0a0a0; padding: .5rem; margin: .5rem 0; }
</style>
</head>
<body>
<div class="todo-layout">
<aside class="todo-sidebar" hx-get="/api/todo-lists/" hx-trigger="load" hx-headers='{"Scrud-UI-Scope": "sidebar", "Accept": "text/html"}'>Loading lists&hellip;</aside>
<main>{{ body }}</main>
</div>
</body>
</html>
"""

COMPONENTS = [
    {
        # The detail of a list: its title, and its items in the "row" scope.
        "name": "todo-list-detail",
        "engine": "django",
        "mode": "edit",
        "label": "To-do list",
        "scopes": {"items": "row"},
        "template": (
            '<section class="todo-list"><h1>{{ value.title }}</h1>'
            "{% for prop, html in child_items %}"
            "{% if prop == 'items' %}{{ html }}{% endif %}{% endfor %}"
            "</section>"
        ),
    },
    {
        # The same list for a reader: styled and read-only.
        "name": "todo-list-view",
        "engine": "django",
        "mode": "view",
        "label": "To-do list (read-only)",
        "scopes": {"items": "row"},
        "template": (
            '<section class="todo-list"><h1>{{ value.title }}</h1>'
            '<p class="todo-readonly">Read-only view</p>'
            "{% for prop, html in child_items %}"
            "{% if prop == 'items' %}{{ html }}{% endif %}{% endfor %}"
            "</section>"
        ),
    },
    {
        # An item the user may edit: a checkbox and a text input, named by
        # their JSON Pointers.
        "name": "todo-row-edit",
        "engine": "django",
        "mode": "edit",
        "label": "To-do item",
        "template": (
            '<div class="todo-row">'
            '<input type="checkbox" id="{{ input_id }}-done" name="{{ pointer }}/done"'
            '{% if value.done %} checked{% endif %} aria-label="Done">'
            '<input type="text" id="{{ input_id }}-text" name="{{ pointer }}/text"'
            ' value="{{ value.text }}" aria-label="Text">'
            "</div>"
        ),
    },
    {
        # An item, read-only.
        "name": "todo-row",
        "engine": "django",
        "mode": "view",
        "label": "To-do item",
        "template": (
            '<span class="todo-row todo-row-view{% if value.done %} done{% endif %}">'
            '{% if value.done %}&#9745;{% else %}&#9744;{% endif %} {{ value.text }}'
            "</span>"
        ),
    },
    {
        # A list in the sidebar: a link to it and its item count. In edit
        # mode, since its owner may write it (a resource the user may write
        # renders wholly in edit mode: ui-one-mode-per-resource in
        # docs/follow-ups.md); it renders no inputs.
        "name": "todo-list-compact",
        "engine": "django",
        "mode": "edit",
        "label": "To-do list (compact)",
        "template": (
            '<a class="todo-list-compact" href="{{ resource.href }}">'
            "{{ value.title }}</a> ({{ value.items|length }})"
        ),
    },
    {
        "name": "todo-page",
        "engine": "django",
        "mode": "view",
        "label": "To-do page",
        "template": PAGE,
    },
]


def rules(*pairs):
    return [{"select": select, "component": c} for select, c in pairs]


STYLESHEETS = [
    {
        "name": "todo",
        "rbac_context": DEFAULT_CONTEXT,
        "prefixes": PREFIXES,
        "rules": rules(
            ("ex:TodoList", "todo-list-view"),
            ("ex:TodoList", "todo-list-detail"),
            ("sidebar ex:TodoList", "todo-list-compact"),
            ("row ex:TodoItem", "todo-row"),
            ("row ex:TodoItem", "todo-row-edit"),
            ("scrud:page", "todo-page"),
        ),
    },
    {
        # Restyles the sidebar's rows in project-a only.
        "name": "project-a-sidebar",
        "rbac_context": PROJECT,
        "prefixes": PREFIXES,
        "rules": rules(("sidebar ex:TodoItem", "todo-row")),
    },
]


# The branches, created in this order (a parent first). Published branches
# are live over main: they hold only what they change.
BRANCHES = [
    {
        "name": "admin",
        "rbac_context": ADMIN_CONTEXT,
        "parent": MAIN,
        "prefix": "/manage/",
        "label": "Admin",
        "description": "The internal staff's presentation, at /manage/.",
    },
    {
        "name": "customer",
        "rbac_context": CUSTOMER_CONTEXT,
        "parent": MAIN,
        "prefix": "/todo/",
        "label": "Customer",
        "description": "The customers' presentation, at /todo/.",
    },
    {
        "name": "testing-admin",
        "rbac_context": TESTING_CONTEXT,
        "parent": "admin",
        "prefix": None,
        "label": "Testing (admin)",
        "description": "Testers browse changes to the admin branch here.",
    },
]

ADMIN_LIST_VIEW = {
    # Admin's list page: the read-only list, with its URL and ETag.
    "name": "todo-list-admin",
    "engine": "django",
    "mode": "view",
    "label": "To-do list (admin)",
    "rbac_context": ADMIN_CONTEXT,
    "branch": "admin",
    "base": None,
    "scopes": {"items": "row"},
    "template": (
        '<section class="todo-list todo-list-admin"><h1>{{ value.title }}</h1>'
        '<p class="todo-readonly">Admin view of {{ resource.href }}'
        " (ETag {{ resource.etag }})</p>"
        "{% for prop, html in child_items %}"
        "{% if prop == 'items' %}{{ html }}{% endif %}{% endfor %}"
        "</section>"
    ),
}

BRANCH_ITEMS = [
    (
        "ui-components",
        ADMIN_LIST_VIEW,
    ),
    (
        "ui-stylesheets",
        {
            # Order 10 wins over main's "todo" stylesheet (order 0).
            "name": "admin-todo",
            "rbac_context": ADMIN_CONTEXT,
            "applies_to": DEFAULT_CONTEXT,
            "branch": "admin",
            "base": None,
            "prefixes": PREFIXES,
            "order": 10,
            "rules": rules(("ex:TodoList", "todo-list-admin")),
        },
    ),
    (
        UI_CSS_SLUG,
        {
            "name": "admin-theme",
            "rbac_context": ADMIN_CONTEXT,
            "branch": "admin",
            "base": None,
            "order": 0,
            "css": (
                "body { background: #f3f0ff; }\n"
                ".todo-list-admin h1 { color: #4b2e83; }\n"
                ".todo-list-admin .todo-readonly { font-family: monospace; }\n"
            ),
        },
    ),
    (
        UI_CSS_SLUG,
        {
            "name": "customer-theme",
            "rbac_context": CUSTOMER_CONTEXT,
            "branch": "customer",
            "base": None,
            "order": 0,
            "css": (
                "body { background: #fffaf0; }\n"
                ".todo-list h1 { color: #b45309; font-family: Georgia, serif; }\n"
            ),
        },
    ),
]


class Command(BaseCommand):
    help = "Seed the to-do demo (users, roles, a list, UI components and stylesheets)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help='Rewrite the "Groceries" list to its seeded items.',
        )

    def handle(self, *args, reset=False, **options):
        # Importing the URLconf registers the resource types.
        for name in (TODO_LIST_SLUG, UI_COMPONENT_SLUG, UI_STYLESHEET_SLUG):
            reverse(f"{name}-list")
        with transaction.atomic():
            for username, roles in USERS.items():
                self.seed_user(username, roles)
            for content in COMPONENTS:
                compile_template(content["engine"], content["template"])
                self.seed_resource(
                    UI_COMPONENT_SLUG,
                    {**content, "rbac_context": DEFAULT_CONTEXT},
                    name=content["name"],
                )
            for content in STYLESHEETS:
                self.seed_resource(
                    UI_STYLESHEET_SLUG,
                    content,
                    name=content["name"],
                    rbac_context=content["rbac_context"],
                )
            groceries = self.seed_list(reset)
            self.seed_branches()
            for slug, content in BRANCH_ITEMS:
                self.seed_resource(
                    slug,
                    content,
                    name=content["name"],
                    rbac_context=content["rbac_context"],
                    branch=content["branch"],
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded: log in as owner/owner, reader/reader or author/author;"
                f" the list is /api/{TODO_LIST_SLUG}/{groceries.pk}/"
            )
        )
        for content in BRANCHES:
            where = content["prefix"] or f"under {content['parent']}"
            self.stdout.write(f"branch {content['name']}: {where}")
        self.stdout.write(
            f"admin: /manage/{TODO_LIST_SLUG}/{groceries.pk}/ (author)"
            f"; customer: /todo/{TODO_LIST_SLUG}/{groceries.pk}/"
            " (author, reader, owner)"
        )

    def seed_user(self, username, roles):
        user, created = get_user_model().objects.get_or_create(username=username)
        if created or not user.check_password(username):
            user.set_password(username)
            user.save()
        for rbac_context, definition in roles.items():
            assignment = (
                RoleAssignment.objects.filter(user=user, rbac_context=rbac_context)
                .select_related("role")
                .first()
            )
            if assignment is None:
                role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
                role.save()
                RoleAssignment(
                    user=user,
                    role=role,
                    rbac_context=rbac_context,
                    etag=uuid4().hex,
                    modified_at=now(),
                ).save()
                self.stdout.write(f"user {username}: role in {rbac_context!r} created")
            elif assignment.role.definition != definition:
                role = assignment.role
                role.definition = definition
                role.etag = uuid4().hex
                role.modified_at = now()
                role.save()
                self.stdout.write(f"user {username}: role in {rbac_context!r} updated")

    def seed_branches(self):
        """Create the branches that are missing (main is stored first); a
        branch that exists is left as it is, branching point included."""
        using = write_db()
        ensure_main(using)
        for definition in BRANCHES:
            if definition["name"] in branches(using):
                continue
            all_branches = branches(using)
            chain = []
            parent = definition["parent"]
            while parent is not None:
                chain.append(parent)
                parent = all_branches[parent].parent
            content = {
                **definition,
                "base": "live",
                "branching_point": {
                    name: current_version(name, using) for name in chain
                },
            }
            ResourceRegistration.register(content, UI_BRANCH_SLUG)
            self.stdout.write(f"{UI_BRANCH_SLUG} {definition['name']!r}: created")

    def seed_resource(
        self, slug, content, *, name, rbac_context=DEFAULT_CONTEXT, **fields
    ):
        """Create the resource of type `slug` called `name` (in
        `rbac_context`, and with `fields`), or bring it back to `content`."""
        existing = self.find(slug, rbac_context=rbac_context, name=name, **fields)
        if existing is None:
            ResourceRegistration.register(content, slug)
            self.stdout.write(f"{slug} {name!r}: created")
        elif existing.content != content:
            ResourceRegistration.update(content, slug, existing.pk)
            self.stdout.write(f"{slug} {name!r}: updated")

    def seed_list(self, reset):
        existing = self.find(TODO_LIST_SLUG, title=GROCERIES["title"])
        if existing is None:
            self.stdout.write("Groceries: created")
            return ResourceRegistration.register(GROCERIES, TODO_LIST_SLUG)
        if reset and existing.content != GROCERIES:
            self.stdout.write("Groceries: reset")
            return ResourceRegistration.update(GROCERIES, TODO_LIST_SLUG, existing.pk)
        return existing

    def find(self, slug, *, rbac_context=None, **fields):
        """The first resource of type `slug` whose content has `fields` (and
        whose ``rbac_context`` is `rbac_context`, when given), or ``None``."""
        resource_type = get_resource_type_for(slug)
        candidates = Resource.objects.filter(resource_type=resource_type).order_by("pk")
        for resource in candidates:
            content = resource.content
            if not isinstance(content, dict):
                continue
            if any(content.get(k) != v for k, v in fields.items()):
                continue
            if (
                rbac_context is not None
                and content.get("rbac_context", DEFAULT_CONTEXT) != rbac_context
            ):
                continue
            return resource
        return None
