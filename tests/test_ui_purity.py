"""A stored component renders from plain JSON values only: no request, user,
settings or engine internals, and nothing it does to its values reaches
another node or the stored data."""

import re

import pytest
from django.conf import settings
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_http import make_component, make_stylesheet

HTML = "text/html"

# Names a template might try to reach the request, the user or the settings
# through; each is empty, or the component fails and the page falls back.
DJANGO_PROBES = [
    "{{ request }}",
    "{{ request.user }}",
    "{{ user }}",
    "{{ user.username }}",
    "{{ perms }}",
    "{{ settings }}",
    "{{ settings.SECRET_KEY }}",
    "{{ config }}",
    "{{ self }}",
    "{{ csrf_token }}",
    "{{ resource.user }}",
    "{{ resource.request }}",
    "{{ view }}",
]
JINJA_PROBES = [p for p in DJANGO_PROBES if p != "{{ self }}"] + [
    "{{ self._TemplateReference__context }}",
    "{{ request.META }}",
    "{{ ''.__class__.__mro__ }}",
    "{{ cycler.__init__.__globals__ }}",
    "{{ lipsum.__globals__ }}",
]


def render_with(template, engine, todo_list, user):
    Given.http.force_authenticate(user)
    When.http.get(
        reverse("todo-lists-detail", args=[todo_list.pk]),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    return Http.response.content.decode()


def assert_nothing_leaks(output, user):
    assert user.username not in output
    for secret in (settings.SECRET_KEY, "WSGIRequest", "tests.settings", "HTTP_"):
        assert secret not in output, secret


@pytest.mark.django_db
@pytest.mark.parametrize(
    "engine,probes", [("django", DJANGO_PROBES), ("jinja2", JINJA_PROBES)]
)
@test_context
def test_stored_components_cannot_reach_the_request_or_settings(
    todo_list, todo_reader_login, engine, probes
):
    made = make_component("probe", "x")
    make_stylesheet("probing", [("ex:TodoList", "probe")])
    made.content = {**made.content, "engine": engine}
    for probe in probes:
        made.content = {
            **made.content,
            "template": f'<p class="probe">[{probe}]</p>',
        }
        made.save()
        output = render_with(probe, engine, todo_list, todo_reader_login)
        assert_nothing_leaks(output, todo_reader_login)
        # rendered empty, or failed and fell back to the built-in
        if 'class="probe"' in output:
            assert '<p class="probe">[]</p>' in output, probe
        else:
            assert "scrud-object" in output and "Groceries" in output, probe


@pytest.mark.django_db
@test_context
def test_a_jinja_component_mutating_its_values_changes_nothing_else(
    todo_list, todo_reader_login
):
    """Its values are copies: appending to ``value`` or rewriting
    ``resource`` in one node leaves its siblings, its parent, the offered
    controls and the stored data as they were."""
    mutate_array = (
        "{% set _ = value.append({'text': 'INJECTED'}) %}"
        '<span class="mut">{{ value|length }}</span>'
        "{% for child in children %}{{ child }}{% endfor %}"
    )
    mutate_string = (
        '<em class="str">{{ resource.decisions.PUT }}|{{ resource.etag != "X" }}</em>'
        "{% set _ = resource.decisions.update({'PUT': 'allowed', 'DELETE': 'allowed'}) %}"
        "{% set _ = resource.update({'etag': 'X'}) %}"
    )
    for name, template in (("mut-array", mutate_array), ("mut-string", mutate_string)):
        made = make_component(name, template)
        made.content = {**made.content, "engine": "jinja2"}
        made.save()
    make_component(
        "root",
        '<p class="root">{{ value.items|length }}</p>{{ children.title }}{{ children.items }}',
    )
    make_stylesheet(
        "mutating",
        [
            ("ex:TodoList", "root"),
            ("array", "mut-array"),
            ("string", "mut-string"),
        ],
    )
    before = Resource.objects.get(pk=todo_list.pk)
    output = render_with("", "jinja2", todo_list, todo_reader_login)
    # the array component ran on its own copy, the root saw the stored items
    assert '<span class="mut">3</span>' in output
    assert '<p class="root">2</p>' in output
    # every string node sees the decisions as authorized (denied) and the
    # real ETag, whatever the nodes before it did to their own copies
    seen = re.findall(r'<em class="str">([^<]*)</em>', output)
    assert len(seen) >= 3 and set(seen) == {"denied|True"}, seen
    assert "hx-put" not in output and "hx-delete" not in output
    after = Resource.objects.get(pk=todo_list.pk)
    assert after.content == before.content and after.etag == before.etag
    assert "INJECTED" not in output


@pytest.mark.django_db
@test_context
def test_jinja_self_is_the_engines_own_reference_only(todo_list, todo_reader_login):
    made = make_component("probe", '<p class="probe">[{{ self }}]</p>')
    made.content = {**made.content, "engine": "jinja2"}
    made.save()
    make_stylesheet("probing", [("ex:TodoList", "probe")])
    output = render_with("", "jinja2", todo_list, todo_reader_login)
    assert '<p class="probe">[&lt;TemplateReference None&gt;]</p>' in output
    assert_nothing_leaks(output, todo_reader_login)
