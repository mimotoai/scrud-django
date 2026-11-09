"""Compiling stored Django and Jinja2 templates."""

import pytest
from django.urls import reverse
from django.utils.safestring import SafeString
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.engines import (
    TemplateCompileError,
    cached_template,
    compile_template,
)

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

ENGINES = ["django", "jinja2"]


@pytest.mark.parametrize("engine", ENGINES)
def test_autoescapes_values(engine):
    template = compile_template(engine, "{{ value }}")
    assert template.render({"value": "<b>"}) == "&lt;b&gt;"


@pytest.mark.parametrize("engine", ENGINES)
def test_safe_children_unescaped(engine):
    template = compile_template(engine, "{{ children.a }}")
    html = template.render({"children": {"a": SafeString("<i>x</i>")}})
    assert "<i>x</i>" in html
    assert isinstance(html, SafeString)


@pytest.mark.parametrize("source", ['{% load static %}', '{% include "x.html" %}'])
def test_django_include_and_load_unavailable(source):
    with pytest.raises(TemplateCompileError):
        compile_template("django", source)


@pytest.mark.parametrize("engine,source", [("django", "{% if %}"), ("jinja2", "{{ ")])
def test_bad_syntax_raises(engine, source):
    with pytest.raises(TemplateCompileError):
        compile_template(engine, source)


def test_unknown_engine_raises():
    with pytest.raises(TemplateCompileError):
        compile_template("mustache", "x")


def test_cached_template_reuses_compiled_template():
    first = cached_template(("builtin", "t"), "django", "{{ value }}")
    assert cached_template(("builtin", "t"), "django", "{{ value }}") is first
    other = cached_template(("builtin", "t"), "django", "changed")
    assert other is not first


@pytest.mark.django_db
@test_context
def test_component_with_bad_template_is_400(admin_login):
    Given.http.force_authenticate(admin_login)
    body = {
        "name": "bad",
        "engine": "django",
        "mode": "view",
        "template": "{% if %}",
    }
    When.http.post(reverse("ui-components-list"), data=body, format="json")
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("ui-template-invalid")


@pytest.mark.django_db
@test_context
def test_update_with_bad_template_is_400(admin_login):
    Given.http.force_authenticate(admin_login)
    body = {
        "name": "good",
        "engine": "jinja2",
        "mode": "view",
        "template": "<li>{{ value }}</li>",
    }
    When.http.post(reverse("ui-components-list"), data=body, format="json")
    Then.http.status_code_is(201)
    location, etag = Http.response["Location"], Http.response["ETag"]
    When.http.put(
        location,
        data={**body, "template": "{{ "},
        format="json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("ui-template-invalid")


def _jinja_output(source):
    """Render a Jinja source; a SecurityError counts as blocked (None)."""
    from jinja2.exceptions import SecurityError

    try:
        return str(compile_template("jinja2", source).render({}))
    except (TemplateCompileError, SecurityError):
        return None


def test_jinja_sandbox_blocks_dunder_access():
    output = _jinja_output("{{ ''.__class__.__mro__ }}")
    assert output is None or "<class" not in output
    output = _jinja_output("{{ ''.__class__.__mro__[1].__subclasses__()|length }}")
    assert output is None or not output.strip().isdigit()


def test_jinja_sandbox_blocks_globals_escape():
    output = _jinja_output("{{ cycler.__init__.__globals__ }}")
    assert output is None or "os" not in output and "{" not in output
    output = _jinja_output("{{ cycler.__init__.__globals__.os.getcwd() }}")
    assert output is None or "/" not in output
