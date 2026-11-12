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
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.policy import Permission

from scrud_django.core import resources, writes
from scrud_django.core.authz import current_transition
from scrud_django.core.requirements import (
    GrantsLeaf,
    asks_grants,
    asks_grants_itself,
    grants_allow,
    is_hook,
    register_grant_asking_leaf,
    require_grants,
)
from scrud_django.core.types import HookLeaf, TypeOptions, requirement_for
from scrud_django.exceptions import WorkflowTransitionError
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    workflow_policy_resource_type,
)
from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI

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
    return {
        "empty": [],
        "hooks-only": hooks_only(resource_type),
        # An option of hooks alone would allow what the grants refuse.
        "anyof-hook-option": [
            access.AnyOf([[GrantsLeaf(JSON_LD)], hooks_only(resource_type)])
        ],
    }


@pytest.mark.django_db
@test_context
def test_asks_grants():
    resource_type = json_ld()
    assert asks_grants([GrantsLeaf(JSON_LD)])
    assert asks_grants([GrantsLeaf(JSON_LD), *hooks_only(resource_type)])
    # An AnyOf asks the grants when every option does.
    grants = [GrantsLeaf(JSON_LD)]
    assert asks_grants([access.AnyOf([grants, [GrantsLeaf("other")]])])
    assert asks_grants([access.AnyOf([GrantsLeaf(JSON_LD), grants])])
    assert asks_grants([*grants, access.AnyOf([grants, hooks_only(resource_type)])])
    assert not asks_grants([access.AnyOf([])])
    for requirement in no_grants(resource_type).values():
        assert not asks_grants(requirement)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["empty", "hooks-only", "anyof-hook-option"])
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
        "check_request (item)",
        "check_request (list)",
        "readable_queryset",
    ],
)
@pytest.mark.parametrize("requester", ["anonymous", "superuser"])
@test_context
def test_reads_refuse_a_requirement_asking_no_grants(call, kind, requester):
    # A superuser, whom the grants allow everything, is refused too: the
    # guard raises, not a refusal by the grants.
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    requirement = no_grants(resource_type)[kind]
    if requester == "superuser":
        user = user_with(superuser=True)
    else:
        user = AnonymousUser()
    calls = {
        "load_for_read": lambda: resources.load_for_read(
            request_of("GET", user),
            resource_type,
            stored.pk,
            requirement=requirement,
            method="GET",
        ),
        "load_for_read (missing)": lambda: resources.load_for_read(
            request_of("PUT", user),
            resource_type,
            stored.pk + 1000,
            requirement=requirement,
            method="PUT",
        ),
        "allowed_methods (item)": lambda: resources.allowed_methods(
            request_of("OPTIONS", user), resource_type, stored, requirement
        ),
        "allowed_methods (list)": lambda: resources.allowed_methods(
            request_of("OPTIONS", user), resource_type, None, requirement
        ),
        "allowed_methods (list requirement)": lambda: resources.allowed_methods(
            request_of("OPTIONS", user),
            resource_type,
            None,
            [GrantsLeaf(JSON_LD)],
            list_requirement=requirement,
        ),
        "readable_content": lambda: resources.readable_content(
            request_of("GET", user), stored.content, requirement
        ),
        "object_readable": lambda: resources.object_readable(
            request_of("GET", user), resource_type, stored, requirement
        ),
        "check_object": lambda: resources.check_object(
            request_of("GET", user),
            resource_type,
            stored,
            requirement,
            covered=False,
        ),
        "check_request (item)": lambda: resources.check_request(
            request_of("GET", user), resource_type, requirement, list_route=False
        ),
        "check_request (list)": lambda: resources.check_request(
            request_of("GET", user), resource_type, requirement, list_route=True
        ),
        "readable_queryset": lambda: resources.readable_queryset(
            request_of("GET", user), resource_type, requirement
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
            writes.destroy(
                request_of("DELETE", superuser),
                resource_type,
                Resource.objects.get(pk=stored.pk),
                requirement=requirement,
                using="default",
            )
        for stored_resource, method in ((None, "POST"), (stored, "PUT")):
            with pytest.raises(ImproperlyConfigured, match="asks no grants"):
                writes.validate_write(
                    request_of(method, superuser),
                    resource_type,
                    {"rbac_context": "a"},
                    stored=stored_resource,
                    requirement=requirement,
                    readable=lambda: True,
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


@pytest.mark.django_db
@test_context
def test_destroy_refuses_a_policy_in_a_context_not_granted():
    """`destroy` of a ``WorkflowPolicy`` asks its requirement in every
    context the policy applies to: a DELETE granted in "a" only does not
    delete one stored in ["a", "b"]."""
    json_ld()
    policy_type = workflow_policy_resource_type()
    policy = ResourceRegistration.register(
        {
            "resource_type": JSON_LD,
            "rbac_context": ["a", "b"],
            "initial_states": {"any": "true"},
            "states": {},
            "transitions": {},
        },
        "workflow-policies",
    )
    requirement = [GrantsLeaf(WORKFLOW_POLICY_TYPE_URI)]
    grant = {"http.DELETE": [WORKFLOW_POLICY_TYPE_URI]}
    in_a = user_with((grant, "a"))
    with pytest.raises(access.AccessDenied):
        writes.destroy(
            request_of("DELETE", in_a),
            policy_type,
            Resource.objects.get(pk=policy.pk),
            requirement=requirement,
            using="default",
        )
    assert Resource.objects.filter(pk=policy.pk).exists()
    in_both = user_with((grant, "a"), (grant, "b"))
    writes.destroy(
        request_of("DELETE", in_both),
        policy_type,
        Resource.objects.get(pk=policy.pk),
        requirement=requirement,
        using="default",
    )
    assert not Resource.objects.filter(pk=policy.pk).exists()


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
        # The request check (`check_request`, run first) asks the hooks too:
        # this one refuses only a transition, so that the narrow is what
        # refuses here.
        if current_transition() is None:
            return True
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
    assert len(asked) == 1
    # The requirement is required: without it no hook could be asked.
    with pytest.raises(TypeError, match="requirement"):
        validate()
    assert len(asked) == 1
    # A hook refusing the body's context refuses it at the request check,
    # before the workflow check (DRF's 403).
    refuses_a = HookLeaf(resource_type, TypeOptions(checks=(lambda *args: False,)))
    with pytest.raises(access.AccessDenied):
        validate(requirement=[GrantsLeaf(JSON_LD), refuses_a], narrow=())


@pytest.mark.django_db
@test_context
def test_validate_write_asks_a_registered_types_checks():
    """`validate_write` given the type's requirement (`requirement_for`)
    asks its registered checks: ``hooked-notes``'s `deny_context_b` refuses
    a write in "b", which its grants alone allow, at the request check
    (``access.AccessDenied``, DRF's 403), which asks them of the body's
    contexts before the workflow check asks them of each transition."""
    from .resource_types import HOOKED_NOTE_TYPE_URI, hooked_resource_types

    reverse("hooked-notes-list")
    hooked_resource_types()
    hooked = get_resource_type_for("hooked-notes")
    workflow_policy_resource_type()
    for context in ("a", "b"):
        ResourceRegistration.register(
            {
                "resource_type": HOOKED_NOTE_TYPE_URI,
                "rbac_context": context,
                "initial_states": {"any": "true"},
                "states": {},
                "transitions": {},
            },
            "workflow-policies",
        )
    grant = {"http.POST": [HOOKED_NOTE_TYPE_URI]}
    writer = user_with((grant, "a"), (grant, "b"))

    def validate(body, requirement):
        return writes.validate_write(
            request_of("POST", writer),
            hooked,
            body,
            stored=None,
            readable=None,
            requirement=requirement,
        )

    requirement = requirement_for(hooked, "hooked-notes")
    ((context, _, _),) = validate({"text": "x", "rbac_context": "a"}, requirement)
    assert context == "a"
    with pytest.raises(access.AccessDenied):
        validate({"text": "x", "rbac_context": "b"}, requirement)
    # The grants alone allow it: the type's check refused it.
    validate({"text": "x", "rbac_context": "b"}, [GrantsLeaf(HOOKED_NOTE_TYPE_URI)])


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


class SelfTrusting:
    """A leaf of a class the core does not know, which says it asks the
    grants in each context (``reads_each_context``) but allows everything."""

    type_iri = JSON_LD
    reads_each_context = True
    overrides_object_checks = False

    def contexts_for(self, obj):
        return rbac_contexts_of(obj)

    def has_permission(self, request, body):
        return True

    def has_object_permission(self, request, obj):
        return True

    def has_context_permission(self, request, obj, context):
        return True


class ClaimsToGrantNothing(SelfTrusting):
    """`SelfTrusting` claiming to be a hook (``grants_nothing``)."""

    grants_nothing = True


OTHER_TYPE = "tests://Other"
DELETER = {"http.GET": [JSON_LD], "http.DELETE": [JSON_LD]}


@pytest.mark.django_db
@test_context
def test_a_leaf_of_an_unknown_class_does_not_count_as_asking_grants():
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        require_grants([SelfTrusting()], "x")
    require_grants([SelfTrusting(), GrantsLeaf(JSON_LD)], "x")
    # An AnyOf option of an unknown leaf alone asks none.
    assert not asks_grants([access.AnyOf([[GrantsLeaf(JSON_LD)], [SelfTrusting()]])])
    assert asks_grants(
        [access.AnyOf([[GrantsLeaf(JSON_LD)], [SelfTrusting(), GrantsLeaf(JSON_LD)]])]
    )
    # The leaves the core knows: GrantsLeaf (subclasses:
    # test_a_subclass_overriding_the_grants_check_is_not_trusted) and HookLeaf.
    hook = hooks_only(json_ld())[0]
    assert asks_grants_itself(GrantsLeaf(JSON_LD))
    assert not asks_grants_itself(SelfTrusting())
    assert not asks_grants_itself(hook)
    assert is_hook(hook)
    assert not is_hook(ClaimsToGrantNothing())


@pytest.mark.django_db
@pytest.mark.parametrize("leaf", [SelfTrusting, ClaimsToGrantNothing])
@test_context
def test_the_grants_are_asked_beside_a_leaf_of_an_unknown_class(leaf):
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    requirement = [GrantsLeaf(JSON_LD), leaf()]

    def load(user):
        return resources.load_for_read(
            request_of("DELETE", user),
            resource_type,
            stored.pk,
            requirement=requirement,
            method="DELETE",
        )

    reader_b = user_with(({"http.GET": [JSON_LD]}, "b"))
    for user in (AnonymousUser(), reader_b):
        with pytest.raises((Http404, access.AccessDenied)):
            load(user)
    writer = user_with((DELETER, "a"))
    assert load(writer).pk == stored.pk

    # A GrantsLeaf of another type trusts that type's grants alone; beside
    # an unknown leaf, the grants of the resource's type are asked too.
    other = user_with(
        ({"http.GET": [JSON_LD, OTHER_TYPE], "http.DELETE": [OTHER_TYPE]}, "a")
    )
    assert (
        resources.load_for_read(
            request_of("DELETE", other),
            resource_type,
            stored.pk,
            requirement=[GrantsLeaf(OTHER_TYPE)],
            method="DELETE",
        ).pk
        == stored.pk
    )
    with pytest.raises(access.AccessDenied):
        resources.load_for_read(
            request_of("DELETE", other),
            resource_type,
            stored.pk,
            requirement=[GrantsLeaf(OTHER_TYPE), leaf()],
            method="DELETE",
        )

    # The core functions themselves, with the unknown leaf alone.
    permission = Permission("http.DELETE", JSON_LD)
    anonymous = request_of("DELETE", AnonymousUser())
    with pytest.raises(access.AccessDenied):
        resources.check_grants_in_each_context(
            anonymous, stored.content, [leaf()], permission
        )
    resources.check_grants_in_each_context(
        request_of("DELETE", writer), stored.content, [leaf()], permission
    )
    with pytest.raises(access.AccessDenied):
        writes.check_post_save(
            request_of("PUT", AnonymousUser()), resource_type, stored, [leaf()]
        )
    put_writer = user_with(({"http.PUT": [JSON_LD]}, "a"))
    writes.check_post_save(
        request_of("PUT", put_writer), resource_type, stored, [leaf()]
    )


@pytest.mark.django_db
@test_context
def test_a_grants_nothing_flag_on_an_unknown_class_is_ignored():
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        require_grants([ClaimsToGrantNothing()], "x")
    require_grants([ClaimsToGrantNothing(), GrantsLeaf(JSON_LD)], "x")
    permission = Permission("http.DELETE", JSON_LD)
    anonymous = request_of("DELETE", AnonymousUser())
    # Its grants are asked as any leaf's: it is not a hook.
    assert not grants_allow(
        [ClaimsToGrantNothing()], anonymous, stored.content, permission
    )
    assert grants_allow(
        [ClaimsToGrantNothing()],
        request_of("DELETE", user_with((DELETER, "a"))),
        stored.content,
        permission,
    )
    with pytest.raises(access.AccessDenied):
        writes.check_post_save(
            request_of("PUT", AnonymousUser()),
            resource_type,
            stored,
            [ClaimsToGrantNothing()],
        )
    # A type's hooks ask no grants, beside them or of their own.
    assert grants_allow(
        hooks_only(resource_type), anonymous, stored.content, permission
    )


class NarrowingGrantsLeaf(GrantsLeaf):
    """A `GrantsLeaf` overriding only its object check: still trusted."""

    def has_object_permission(self, request, obj):
        return super().has_object_permission(request, obj)


class AllowingGrantsLeaf(GrantsLeaf):
    """A `GrantsLeaf` whose ``has_context_permission`` asks no grants."""

    def has_context_permission(self, request, obj, context):
        return True


class ClaimingGrantsLeaf(GrantsLeaf):
    """A `GrantsLeaf` overriding ``reads_each_context`` (to what it was)."""

    @property
    def reads_each_context(self):
        return True


@register_grant_asking_leaf
class RegisteredAllowingGrantsLeaf(AllowingGrantsLeaf):
    """`AllowingGrantsLeaf`, registered: trusted, as its author asks."""


@pytest.mark.django_db
@test_context
def test_a_subclass_overriding_the_grants_check_is_not_trusted():
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    # Overriding only the object check keeps the trust.
    narrowing = NarrowingGrantsLeaf(JSON_LD)
    assert asks_grants_itself(narrowing)
    require_grants([narrowing], "x")

    permission = Permission("http.DELETE", JSON_LD)
    writer = user_with((DELETER, "a"), ({"http.PUT": [JSON_LD]}, "a"))
    for leaf in (AllowingGrantsLeaf(JSON_LD), ClaimingGrantsLeaf(JSON_LD)):
        assert not asks_grants_itself(leaf)
        with pytest.raises(ImproperlyConfigured, match="asks no grants"):
            require_grants([leaf], "x")
        # The grants are asked beside it.
        with pytest.raises(access.AccessDenied):
            resources.check_grants_in_each_context(
                request_of("DELETE", AnonymousUser()),
                stored.content,
                [leaf],
                permission,
            )
        resources.check_grants_in_each_context(
            request_of("DELETE", writer), stored.content, [leaf], permission
        )
        with pytest.raises(access.AccessDenied):
            writes.check_post_save(
                request_of("PUT", AnonymousUser()), resource_type, stored, [leaf]
            )
        writes.check_post_save(request_of("PUT", writer), resource_type, stored, [leaf])

    # An instance's own has_context_permission is an override too.
    patched = GrantsLeaf(JSON_LD)
    patched.has_context_permission = lambda request, obj, context: True
    assert not asks_grants_itself(patched)
    assert not asks_grants([patched])

    # Registered itself, the overriding subclass is trusted.
    registered = RegisteredAllowingGrantsLeaf(JSON_LD)
    assert asks_grants_itself(registered)
    require_grants([registered], "x")
    resources.check_grants_in_each_context(
        request_of("DELETE", AnonymousUser()), stored.content, [registered], permission
    )


class NarrowingHookLeaf(HookLeaf):
    """A `HookLeaf` subclass overriding nothing the core reads: still a
    type's hooks."""

    def __repr__(self):
        return f"NarrowingHookLeaf({self.type_iri!r})"


def hook_subclass_overriding(name):
    """A `HookLeaf` subclass overriding `name` with an allowing method."""

    def allowing_method(self, *args):
        return True

    def contexts_for(self, obj):
        return ["a"]

    method = contexts_for if name == "contexts_for" else allowing_method
    return type(f"Overriding_{name}", (HookLeaf,), {name: method})


HOOK_METHODS = (
    "contexts_for",
    "has_permission",
    "has_object_permission",
    "has_context_permission",
)


@pytest.mark.django_db
@pytest.mark.parametrize("name", HOOK_METHODS)
@pytest.mark.parametrize("where", ["class", "instance"])
@test_context
def test_a_hook_subclass_overriding_a_check_is_no_hook(name, where):
    """`is_hook` follows the rule `_trusted` follows for grants leaves: a
    `HookLeaf` subclass (or instance) overriding ``contexts_for`` or a
    ``has_*`` check may allow what a type's hooks would not, so it is not
    exempt from the grants: they are asked beside it, as beside any leaf of
    an unknown class."""
    resource_type = json_ld()
    stored = ResourceRegistration.register({"rbac_context": "a"}, "json-ld")
    options = TypeOptions(checks=(allowing,), object_checks=(allowing,))
    assert is_hook(NarrowingHookLeaf(resource_type, options))
    if where == "class":
        leaf = hook_subclass_overriding(name)(resource_type, options)
    else:
        leaf = HookLeaf(resource_type, options)
        allowing_method = getattr(hook_subclass_overriding(name), name)
        setattr(leaf, name, allowing_method.__get__(leaf))
    assert not is_hook(leaf)
    permission = Permission("http.DELETE", JSON_LD)
    anonymous = request_of("DELETE", AnonymousUser())
    # The grants are asked beside it.
    assert not grants_allow([leaf], anonymous, stored.content, permission)
    with pytest.raises(access.AccessDenied):
        resources.check_grants_in_each_context(
            anonymous, stored.content, [leaf], permission
        )
    with pytest.raises(access.AccessDenied):
        writes.check_post_save(
            request_of("PUT", AnonymousUser()), resource_type, stored, [leaf]
        )
    writer = user_with((DELETER, "a"), ({"http.PUT": [JSON_LD]}, "a"))
    assert grants_allow(
        [leaf], request_of("DELETE", writer), stored.content, permission
    )
    writes.check_post_save(request_of("PUT", writer), resource_type, stored, [leaf])
    # Still no leaf asking the grants: alone, it is refused.
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        require_grants([leaf], "x")
