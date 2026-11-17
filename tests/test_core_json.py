"""JSON written without DRF (`scrud_django.core.json`): the bytes DRF's
``JSONRenderer`` writes, compact or indented, NaN and the infinities
refused; and its writers in the studio, the UI's file views and the ninja
front end are that one."""

import datetime
import uuid

import pytest
from rest_framework.renderers import JSONRenderer

from scrud_django.core import json as core_json
from scrud_django.core.json import drf_json_text, dumps_like_drf, json_response

# U+2028 and U+2029, non-ASCII text, numbers, nesting, the empty containers.
CONTENT = {
    "text": "line\u2028para\u2029 é ü ☃ \U0001f600",
    "n": [1, 2.5, -0.0, 10**20, None, True, False],
    "k": {},
    "l": [],
    "nested": {"a": [{"b": "c\u2028"}]},
}

# Values that are not JSON's own which Django's encoder (the default) and
# DRF's write alike. (They differ for a datetime's microseconds, a Decimal
# and a timedelta: see `scrud_django.core.json`.)
DJANGO_VALUES = {
    "when": datetime.datetime(2026, 11, 17, 12, 30, 15),
    "aware": datetime.datetime(2026, 11, 17, 12, 30, 15, tzinfo=datetime.UTC),
    "day": datetime.date(2026, 11, 17),
    "time": datetime.time(1, 2, 3),
    "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
}


def drf(data, indent=None):
    if indent is None:
        return JSONRenderer().render(data)
    return JSONRenderer().render(
        data, f"application/json; indent={indent}", {"indent": indent}
    )


@pytest.mark.parametrize("data", [CONTENT, DJANGO_VALUES, "\u2028", 1.5, [], {}])
def test_dumps_like_drf_writes_drfs_bytes(data):
    assert dumps_like_drf(data) == drf(data)


@pytest.mark.parametrize("indent", [1, 4, 8])
def test_dumps_like_drf_indents_as_drf(indent):
    assert dumps_like_drf(CONTENT, indent=indent) == drf(CONTENT, indent)


def test_line_and_paragraph_separators_are_escaped():
    written = dumps_like_drf(CONTENT)
    assert b"\\u2028" in written and b"\\u2029" in written
    assert "\u2028".encode() not in written and "\u2029".encode() not in written
    # The rest is UTF-8, not escaped.
    assert "é ü ☃ \U0001f600".encode() in written


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_the_infinities_are_refused_as_drf_refuses_them(value):
    with pytest.raises(ValueError):
        drf({"x": value})
    with pytest.raises(ValueError):
        dumps_like_drf({"x": value})
    with pytest.raises(ValueError):
        json_response({"x": value})


def test_drf_json_text_is_the_text_of_those_bytes():
    assert drf_json_text(CONTENT).encode("utf-8") == drf(CONTENT)
    assert drf_json_text(CONTENT, indent=2).encode("utf-8") == drf(CONTENT, 2)


def test_json_response_writes_drfs_bytes_with_its_headers():
    response = json_response(
        CONTENT,
        status=412,
        headers={"Content-Type": "application/problem+json", "ETag": '"e"'},
    )
    assert response.status_code == 412
    assert response.content == drf(CONTENT)
    assert response["Content-Type"] == "application/problem+json"
    assert response["ETag"] == '"e"'
    assert json_response(CONTENT)["Content-Type"] == "application/json"


def test_the_front_ends_write_through_the_core():
    """One writer: the studio's and the file views' `json_response`, and the
    ninja front end's indented JSON, are the core's."""
    from scrud_django.ninja import negotiation
    from scrud_django.ui.branches import file_views
    from scrud_django.ui.studio import answers

    assert answers.json_response is core_json.json_response
    assert file_views.json_response is core_json.json_response
    assert negotiation.drf_json_text is core_json.drf_json_text
