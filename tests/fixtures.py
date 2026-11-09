import json
import os
from http import server
from multiprocessing import Process
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django import collection_type_uri_for
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI
from scrud_django.utils import http_date_for

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_PARTNER_PROFILES

__all__ = [
    'ROOT_PATH',
    'DATA_PATH',
    'RESOURCE_ENDPOINT_PREFIX',
    'RESOURCE_ENDPOINT_DETAIL_NAME',
    'RESOURCE_ENDPOINT_LIST_NAME',
    'admin_login',
    'http_static_server',
    'last_modified_for',
    'login_no_roles',
    'login_other_context',
    'partner_profile_post_data',
    'partner_profiles',
    'regular_login',
    'role_allow_all',
    'serialize_page',
    'serialize_resource_envelope',
    'serialize_resource',
    'todo_list',
    'todo_owner_login',
    'todo_reader_login',
    'ui_author_login',
    'unapply_a_scrud_migration',
]

UI_TYPE_URIS = (UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI)

ROOT_PATH = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT_PATH, 'data')


# RESOURCE

RESOURCE_ENDPOINT_PREFIX = "partner-profiles"
RESOURCE_ENDPOINT_DETAIL_NAME = f"{RESOURCE_ENDPOINT_PREFIX}-detail"
RESOURCE_ENDPOINT_LIST_NAME = f"{RESOURCE_ENDPOINT_PREFIX}-list"


def unapply_a_scrud_migration(alias="default"):
    """Record scrud_django's latest migration as not applied on database
    `alias`, inside the test's transaction (rolled back after it): the
    database then counts as not migrated (``scrud_migrations_pending``),
    as before ``migrate`` of an upgrade, though its tables are there."""
    from django.db import connections
    from django.db.migrations.recorder import MigrationRecorder

    recorded = MigrationRecorder(connections[alias]).migration_qs.filter(
        app="scrud_django"
    )
    recorded.filter(pk=recorded.order_by("-name")[0].pk).delete()


def last_modified_for(resource):
    return http_date_for(resource.modified_at)


def serialize_page(resources):
    return {
        'count': 1,
        'page_count': 1,
        'next': None,
        'previous': None,
        'content': [serialize_resource_envelope(resource) for resource in resources],
    }


def serialize_resource_envelope(resource):
    return {
        'content': resource.content,
        'last_modified': last_modified_for(resource),
        'etag': quote_etag(resource.etag),
        'href': "http://testserver"
        + reverse(RESOURCE_ENDPOINT_DETAIL_NAME, args=[resource.id]),
    }


def serialize_resource(resource):
    return resource.content


@pytest.fixture
def role_allow_all(db):
    role = Role(
        definition=True,
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    return role


@pytest.fixture
def role_todo_creator(db):
    role = Role(
        definition={
            "http.GET": ["tests://ToDo"],
            "http.POST": ["tests://ToDo"],
            "http.PUT": {
                "tests://ToDo": {
                    "condition": {
                        "operator": "scrud_workflow",
                        "authorized_transitions": ["cancel"],
                    }
                }
            },
        },
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    return role


@pytest.fixture
def regular_login(db, role_allow_all):
    user = UserFactory()
    RoleAssignment(
        user=user,
        role=role_allow_all,
        rbac_context="",
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    return user


@pytest.fixture
def admin_login(db):
    return UserFactory(is_staff=True, is_superuser=True)


@pytest.fixture
def login_no_roles(db):
    return UserFactory()


@pytest.fixture
def login_other_context(db):
    """A user who may GET, PUT and DELETE partner profiles only in the context
    "other": past the view-level check, but unable to read a resource in ""."""
    role = Role(
        definition={
            method: [TYPE_URI_PARTNER_PROFILES]
            for method in ("http.GET", "http.PUT", "http.DELETE")
        },
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context="other",
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    return user


@pytest.fixture
def todo_creator_login(db, role_todo_creator):
    user = UserFactory()
    RoleAssignment(
        user=user,
        role=role_todo_creator,
        rbac_context=DEFAULT_CONTEXT,
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    return user


TYPE_URI_TODO_LIST = "tests://TodoList"


def assign_role(definition, *rbac_contexts):
    """A new user assigned a new role with `definition` in each of
    `rbac_contexts`."""
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    for rbac_context in rbac_contexts:
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=rbac_context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


@pytest.fixture
def todo_owner_login(db):
    """May read, update and delete to-do lists in "project-a", create them
    there, and read the UI components and stylesheets."""
    readable = [
        TYPE_URI_TODO_LIST,
        collection_type_uri_for(TYPE_URI_TODO_LIST),
        *UI_TYPE_URIS,
    ]
    return assign_role(
        {
            "http.GET": readable,
            "http.PUT": [TYPE_URI_TODO_LIST],
            "http.DELETE": [TYPE_URI_TODO_LIST],
            "http.POST": [
                TYPE_URI_TODO_LIST,
                collection_type_uri_for(TYPE_URI_TODO_LIST),
            ],
        },
        "project-a",
    )


@pytest.fixture
def todo_reader_login(db):
    """May only read to-do lists in "project-a"."""
    return assign_role(
        {
            "http.GET": [
                TYPE_URI_TODO_LIST,
                collection_type_uri_for(TYPE_URI_TODO_LIST),
            ]
        },
        "project-a",
    )


@pytest.fixture
def todo_list(db):
    """A to-do list in "project-a" with two items, the second one done."""
    reverse("todo-lists-list")  # the URLconf registers the types
    return ResourceRegistration.register(
        {
            "title": "Groceries",
            "rbac_context": "project-a",
            "items": [
                {"text": "Buy milk", "done": False},
                {"text": "Buy eggs", "done": True},
            ],
        },
        "todo-lists",
    )


@pytest.fixture
def ui_author_login(db):
    """May read and write UI components and stylesheets (in the default
    context, where components are stored)."""
    types = [uri for t in UI_TYPE_URIS for uri in (t, collection_type_uri_for(t))]
    return assign_role(
        {f"http.{m}": types for m in ("GET", "POST", "PUT", "DELETE")},
        DEFAULT_CONTEXT,
    )


def http_process_server():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    os.chdir(path)
    server_address = ('localhost', 8123)
    httpd = server.HTTPServer(server_address, server.SimpleHTTPRequestHandler)
    httpd.serve_forever()


@pytest.fixture
def http_static_server():
    p = Process(target=http_process_server, args=())
    p.start()

    yield

    p.join(timeout=1)
    p.kill()


@pytest.fixture
def partner_profile_post_data():
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'static',
        'post_partner_profile.json',
    )
    with open(path) as f:
        return json.loads(f.read())


@pytest.fixture
def partner_profiles(db, partner_profile_post_data):
    # force registration of resource types
    reverse(RESOURCE_ENDPOINT_LIST_NAME)
    return ResourceRegistration.register(
        content=partner_profile_post_data, register_type='partner-profiles'
    )
