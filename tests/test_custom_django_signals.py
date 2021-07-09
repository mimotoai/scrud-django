from django.test import TestCase

from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import scrud_post_delete, scrud_post_save
from tests.factories import fake_resource


class TestCustomDjangoSignals(TestCase):
    signal_was_called = False
    type_uri = None
    sender = None

    def __init__(self, methodName: str) -> None:
        super().__init__(methodName=methodName)
        scrud_post_delete.connect(self.handler)
        scrud_post_save.connect(self.handler)

    def handler(self, sender, **kwargs):
        self.type_uri = kwargs.get('resource_type_uri')
        self.sender = sender
        self.signal_was_called = True

    def test_should_send_signal_when_resource_created_updated_or_deleted(self):

        # For Create
        self.signal_was_called = False
        new_resource = fake_resource()
        new_resource = ResourceRegistration.register(
            new_resource.content, new_resource.resource_type.slug
        )
        self.assertTrue(self.signal_was_called)
        self.assertEqual(new_resource.resource_type.type_uri, self.type_uri)
        self.assertEqual(new_resource.__class__, self.sender)

        # For Update
        self.signal_was_called = False
        new_resource.content = {}
        new_resource.save()
        self.assertTrue(self.signal_was_called)
        self.assertEqual(new_resource.resource_type.type_uri, self.type_uri)
        self.assertEqual(new_resource.__class__, self.sender)

        # For Delete
        self.signal_was_called = False
        new_resource.delete()
        self.assertTrue(self.signal_was_called)
        self.assertEqual(new_resource.resource_type.type_uri, self.type_uri)
        self.assertEqual(new_resource.__class__, self.sender)
