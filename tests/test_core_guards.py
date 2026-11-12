"""The core's public entry points fail closed on a requirement that asks no
grants (empty, or a type's hooks only): ``ImproperlyConfigured``, before
anything is read or written, rather than allow every requester. The DRF
adapters keep their answers: they pass the view's own checks (`recheck`) or
use the unguarded helpers, as a DRF view with no ``IsAuthorized`` has no
grants to ask. `validate_write` derives its ``narrow`` from a requirement's
hooks; the DRF workflow check is always told whether the stored resource is
readable on an update."""

from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core import resources, writes
from scrud_django.core.requirements import GrantsLeaf, asks_grants
from scrud_django.core.types import HookLeaf, TypeOptions
from scrud_django.exceptions import WorkflowTransitionError
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    workflow_policy_resource_type,
)

from .factories import UserFactory
from .steps import *  # noqa: F403, F401

JSON_LD = "http://www.w3.org/ns/json-ld#context"


def user_with(*grants, superuser=False):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory(is_staff=superuser, is_superuser=superuser)
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


def request_of(method, user):
    request = getattr(RequestFactory(), method.lower())("/")
    request.user = user
    return request


def json_ld():
    reverse("json-ld-list")  # importing tests.urls registers the types
    return get_resource_type_for("json-ld")


def allowing(*args):
    return True


def hooks_only(resource_type):
    """A requirement of a type's hooks alone: they only refuse."""
    return [
        HookLeaf(
            resource_type, TypeOptions(checks=(allowing,), object_checks=(allowing,))
        )
    ]


def no_grants(resource_type):
    return {"empty": [], "hooks-only": hooks_only(resource_type)}


@pytest.mark.django_db
@test_context
def test_asks_grants():
    resource_type = json_ld()
    assert asks_grants([GrantsLeaf(JSON_LD)])
    assert asks_grants([GrantsLeaf(JSON_LD), *hooks_only(resource_type)])
    for requirement in no_grants(resource_type).values():
        assert not asks_grants(requirement)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["empty", "hooks-only"])
@pytest.mark.parametrize(
    "call",
    [
        "load_for_read",
        "load_for_read (missing)",
        "allowed_methods (item)",
        "allowed_methods (list)",
        "allowed_methods (list requirement)",
        "readable_content",
        "object_readable",
        "check_object",
    ],
)
@test_context
def test_reads_refuse_a_requirement_asking_no_grants(call, kind):
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    requirement = no_grants(resource_type)[kind]
    anonymous = AnonymousUser()
    calls = {
        "load_for_read": lambda: resources.load_for_read(
            request_of("GET", anonymous),
            resource_type,
            stored.pk,
            requirement=requirement,
            method="GET",
        ),
        "load_for_read (missing)": lambda: resources.load_for_read(
            request_of("PUT", anonymous),
            resource_type,
            stored.pk + 1000,
            requirement=requirement,
            method="PUT",
        ),
        "allowed_methods (item)": lambda: resources.allowed_methods(
            request_of("OPTIONS", anonymous), resource_type, stored, requirement
        ),
        "allowed_methods (list)": lambda: resources.allowed_methods(
            request_of("OPTIONS", anonymous), resource_type, None, requirement
        ),
        "allowed_methods (list requirement)": lambda: resources.allowed_methods(
            request_of("OPTIONS", anonymous),
            resource_type,
            None,
            [GrantsLeaf(JSON_LD)],
            list_requirement=requirement,
        ),
        "readable_content": lambda: resources.readable_content(
            request_of("GET", anonymous), stored.content, requirement
        ),
        "object_readable": lambda: resources.object_readable(
            request_of("GET", anonymous), resource_type, stored, requirement
        ),
        "check_object": lambda: resources.check_object(
            request_of("GET", anonymous),
            resource_type,
            stored,
            requirement,
            covered=False,
        ),
    }
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        calls[call]()


@pytest.mark.django_db
@test_context
def test_writes_refuse_a_requirement_asking_no_grants():
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    superuser = user_with(superuser=True)
    before = list(Resource.objects.values_list("pk", "etag"))
    for requirement in no_grants(resource_type).values():
        with pytest.raises(ImproperlyConfigured, match="asks no grants"):
            writes.create(
                request_of("POST", superuser),
                resource_type,
                {"rbac_context": "a"},
                requirement=requirement,
                using="default",
            )
        with pytest.raises(ImproperlyConfigured, match="asks no grants"):
            writes.update(
                request_of("PUT", superuser),
                resource_type,
                Resource.objects.get(pk=stored.pk),
                {"rbac_context": "b"},
                requirement=requirement,
                using="default",
            )
        with pytest.raises(ImproperlyConfigured, match="asks no grants"):
            writes.save_checked(
                request_of("POST", superuser),
                resource_type,
                lambda: pytest.fail("saved"),
                instance=None,
                requirement=requirement,
                using="default",
                covered=True,
            )
    assert list(Resource.objects.values_list("pk", "etag")) == before


@pytest.mark.django_db
@test_context
def test_the_drf_paths_stay_unguarded():
    """The DRF write gives its own post-save check (`recheck`) and no
    requirement; `check_post_save` itself takes an empty requirement (a
    DRF view with no ``IsAuthorized``)."""
    resource_type = json_ld()
    superuser = user_with(superuser=True)
    rechecked = []
    saved = writes.save_checked(
        request_of("POST", superuser),
        resource_type,
        lambda: ResourceRegistration.register_instance(
            Resource(content={"rbac_context": "a"}, resource_type=resource_type)
        ),
        instance=None,
        requirement=None,
        using="default",
        covered=True,
        recheck=lambda request, obj: rechecked.append(obj.pk),
    )
    assert rechecked == [saved.pk]
    writes.check_post_save(request_of("PUT", AnonymousUser()), resource_type, saved, [])


def may_initialize(method):
    """A role writing a JSON-LD resource only by ``__INITIALIZE__``."""
    return {
        f"http.{method}": {
            JSON_LD: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["__INITIALIZE__"],
                }
            }
        }
    }


@pytest.mark.django_db
@test_context
def test_validate_write_narrows_by_the_requirements_hooks():
    resource_type = json_ld()
    workflow_policy_resource_type()
    ResourceRegistration.register(
        {
            "resource_type": JSON_LD,
            "rbac_context": "a",
            "initial_states": {"any": "true"},
            "states": {},
            "transitions": {},
        },
        "workflow-policies",
    )
    asked = []

    def refusing(request, rtype, body, context):
        asked.append((rtype, body, context))
        return False

    hooks = HookLeaf(resource_type, TypeOptions(checks=(refusing,)))
    writer = user_with((may_initialize("POST"), "a"))
    body = {"rbac_context": "a"}

    def validate(**kwargs):
        return writes.validate_write(
            request_of("POST", writer),
            resource_type,
            body,
            stored=None,
            readable=None,
            **kwargs,
        )

    with pytest.raises(WorkflowTransitionError):
        validate(requirement=[GrantsLeaf(JSON_LD), hooks])
    assert asked == [(resource_type, body, "a")]
    # Without hooks, or with `narrow` given, the requirement's hooks are
    # not asked.
    ((context, _, transition),) = validate(requirement=[GrantsLeaf(JSON_LD)])
    assert (context, transition.transition) == ("a", "__INITIALIZE__")
    validate(requirement=[GrantsLeaf(JSON_LD), hooks], narrow=())
    validate()
    assert len(asked) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("with_view", [True, False], ids=["view", "no-view"])
@test_context
def test_drf_workflow_check_is_told_whether_an_update_is_readable(with_view):
    """The DRF adapter (``WorkflowValidator``) passes ``readable`` as a
    callable for every update, in the views and outside them (a
    serializer with no view: ``may_read_in_context`` of the stored
    resource), and ``None`` only for a create."""
    from scrud_django.core import workflow as core_workflow
    from scrud_django.validators import WorkflowValidator

    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    reader = user_with(({"http.GET": [JSON_LD]}, "a"))
    other = user_with(({"http.GET": [JSON_LD]}, "b"))
    seen = []

    def recording(request, **kwargs):
        seen.append(kwargs["readable"])
        return []

    for user, readable in ((reader, True), (other, False)):
        request = request_of("PUT", user)
        view = SimpleNamespace(
            object_readable=lambda request, content, readable=readable: readable,
            get_permissions=lambda: [],
        )
        context = SimpleNamespace(
            context={"request": request, "view": view if with_view else None},
            resource_type=resource_type,
            instance=Resource.objects.get(pk=stored.pk),
            initial_data={"rbac_context": "a"},
        )
        with mock.patch.object(core_workflow, "workflow_check", recording):
            WorkflowValidator()({"rbac_context": "a"}, context)
            assert callable(seen[-1])
            assert seen[-1]() is readable
            context.instance = None
            WorkflowValidator()({"rbac_context": "a"}, context)
            assert seen[-1] is None
