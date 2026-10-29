"""JSON Schema ``$ref`` is resolved only within the schema itself, and a schema
that does not compile fails closed: it cannot be written through the API, and
a stored one refuses writes of its type."""

import http.server
import json
import threading
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory

TODO = "tests://ToDo"
SCHEMA = "http://json-schema.org/draft-04/schema"
JSON_INVALID = "https://api.openteams.com/http-problem/json-invalid"
SCHEMA_INVALID = "https://api.openteams.com/http-problem/json-schema-invalid"
SECRET = "SECRET_API_KEY=sk-live-12345"


class Hits(http.server.BaseHTTPRequestHandler):
    """A loopback server that records each request it gets."""

    hits = []

    def do_GET(self):
        Hits.hits.append(self.path)
        body = json.dumps({"type": "object", "required": [SECRET]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def loopback():
    Hits.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Hits)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "secret.json"
    path.write_text(json.dumps({"type": "object", "required": [SECRET]}))
    return path


def user_with(definition, context=""):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def todo_schema():
    reverse("todos-list")
    return ResourceType.objects.get(type_uri=TODO).schema


def schema_with_ref(ref):
    return {
        "$schema": SCHEMA,
        "type": "object",
        "properties": {"title": {"type": "string"}, "probe": {"$ref": ref}},
    }


def admin():
    return UserFactory(is_staff=True, is_superuser=True)


def post_todo(user, content):
    return client_for(user).post(reverse("todos-list"), content, format="json")


def remote_refs(loopback, secret_file):
    return {
        "http": f"{loopback}/internal-only/metadata.json",
        "file": f"file://{secret_file}",
        "relative": "other.json#/definitions/x",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("scheme", ["http", "file", "relative"])
def test_schema_with_a_remote_ref_is_refused(scheme, loopback, secret_file):
    schema = todo_schema()
    editor = user_with({"http.GET": [SCHEMA], "http.PUT": [SCHEMA]})
    ref = remote_refs(loopback, secret_file)[scheme]
    response = client_for(editor).put(
        reverse("json-schema-detail", args=[schema.pk]),
        schema_with_ref(ref),
        format="json",
        HTTP_IF_MATCH=quote_etag(schema.etag),
    )
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["type"] == SCHEMA_INVALID, body
    assert body["detail"].startswith("The JSON Schema does not compile: $ref "), body
    assert SECRET not in response.content.decode()
    assert Hits.hits == []
    stored = Resource.objects.get(pk=schema.pk)
    assert stored.content == schema.content
    assert stored.etag == schema.etag


@pytest.mark.django_db
def test_schema_with_a_remote_ref_cannot_be_created(loopback):
    reverse("todos-list")
    before = Resource.objects.count()
    response = client_for(admin()).post(
        reverse("json-schema-list"),
        schema_with_ref(f"{loopback}/x.json"),
        format="json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["type"] == SCHEMA_INVALID
    assert Hits.hits == []
    assert Resource.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad_schema",
    [
        pytest.param("remote", id="remote-ref"),
        pytest.param(
            schema_with_ref("#/definitions/missing"), id="unresolvable-local-ref"
        ),
        pytest.param({"$schema": SCHEMA, "type": 5}, id="invalid-type"),
    ],
)
def test_stored_uncompilable_schema_fails_closed(bad_schema, loopback):
    if bad_schema == "remote":
        bad_schema = schema_with_ref(f"{loopback}/x.json")
    schema = todo_schema()
    # Stored server-side, past the API's checks.
    ResourceRegistration.update(
        content=bad_schema, register_type="json-schema", slug=str(schema.pk)
    )
    before = Resource.objects.count()
    response = post_todo(admin(), {"title": "Buy milk", "status": "not started"})
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["type"] == SCHEMA_INVALID, body
    # The reason is logged, not sent to a writer who did not write the schema.
    assert body["detail"] == (
        "The JSON Schema of this resource type does not compile, so it cannot be "
        "validated."
    )
    assert loopback not in response.content.decode()
    assert Resource.objects.count() == before
    assert Hits.hits == []


@pytest.mark.django_db
def test_local_refs_still_work():
    todo_schema()
    blocked = {
        "title": "Buy milk",
        "status": "blocked",
        "priority": "high",
        "assignee": "http://somewhere.example/someone",
    }
    # BlockerNote (``#/definitions/BlockerNote``) requires an explanation.
    response = post_todo(admin(), dict(blocked, blockers=[{"blocked_by": "x:y"}]))
    assert response.status_code == 400, response.content
    assert response.json()["type"] == JSON_INVALID

    response = post_todo(admin(), dict(blocked, blockers=[{"explanation": "rain"}]))
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_schema_with_local_refs_may_be_written():
    schema = todo_schema()
    content = dict(
        schema.content,
        properties=dict(
            schema.content["properties"],
            more_blockers={"$ref": "#/definitions/BlockerNote"},
        ),
    )
    response = client_for(admin()).put(
        reverse("json-schema-detail", args=[schema.pk]),
        content,
        format="json",
        HTTP_IF_MATCH=quote_etag(schema.etag),
    )
    assert response.status_code == 200, response.content
    response = post_todo(admin(), {"title": "Buy milk", "more_blockers": {}})
    assert response.status_code == 400, response.content
    assert response.json()["type"] == JSON_INVALID


@pytest.mark.django_db
def test_validating_does_not_change_the_stored_schema():
    schema = todo_schema()
    before = json.loads(json.dumps(schema.content))
    response = post_todo(admin(), {"title": "Buy milk", "status": "not started"})
    assert response.status_code == 201, response.content
    assert Resource.objects.get(pk=schema.pk).content == before
