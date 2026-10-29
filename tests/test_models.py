import pytest
from safetydance_django.test import *  # noqa: F403, F401

from .factories import ResourceFactory, ResourceTypeFactory
from .steps import *  # noqa: F403, F401


@pytest.mark.django_db
@test_context
def test_resource():
    """Resource test."""
    step_name = 'resource'
    Given.an_instance_named(step_name, ResourceFactory())
    Then.is_valid_resource(step_name)


@pytest.mark.django_db
@test_context
def test_resource_type():
    """Resource type test."""
    step_name = 'resource_type'
    Given.an_instance_named(step_name, ResourceTypeFactory())
    Then.is_valid_resource_type(step_name)
