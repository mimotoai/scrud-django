import pytest
from rest_framework.exceptions import NotFound
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from scrud_django.exceptions import WorkflowInvalidStateError, scrud_exception_handler


@pytest.fixture
def api_context():
    view = APIView()
    request = Request(APIRequestFactory().get("/"))
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = "application/json"
    view.request = request
    return {"request": request, "view": view}


def test_scrud_errors_keep_their_body(api_context):
    exc = WorkflowInvalidStateError(policy="todo")
    response = scrud_exception_handler(exc, api_context)
    assert response.status_code == 400
    assert (
        response.data["type"]
        == "http://api.openteams.com/http-problem/workflow-transition-invalid"
    )
    assert response.data["message"] == "The request does not match a valid state"
    assert response["Content-Type"].startswith("application/problem+json")


def test_other_errors_use_scoped_rbac_problem_details(api_context):
    response = scrud_exception_handler(NotFound(), api_context)
    assert response.status_code == 404
    assert set(response.data) >= {"type", "title", "status"}
