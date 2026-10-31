from django.test import TestCase

from scrud_django.conf import workflow_actions
from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import (
    ScrudSignalProcessor,
    scrud_post_delete,
    scrud_post_save,
)
from tests.factories import fake_resource
from tests.workflow_actions import actions


class ScrudSignalProcessorMock(ScrudSignalProcessor):
    def post_save_trigger(self, sender, **kwargs):
        super().post_save_trigger(sender, **kwargs)
        return self.determine_action(
            self.post_save_literal, kwargs.get(self.resource_type_uri_literal)
        )

    def post_delete_trigger(self, sender, **kwargs):
        super().post_delete_trigger(sender, **kwargs)
        return self.determine_action(
            self.post_delete_literal, kwargs.get(self.resource_type_uri_literal)
        )


class TestCustomDjangoSignals(TestCase):
    signal_was_called = False
    type_uri = None
    sender = None
    signal_processor = None
    post_save_trigger = False
    post_delete_trigger = False

    def setUp(self):
        # Connected for this test only: pytest builds the TestCase instances
        # at collection, so receivers connected in __init__ would fire (and
        # run the ToDo actions again) in every module collected with this one.
        scrud_post_delete.connect(self.handler)
        self.addCleanup(scrud_post_delete.disconnect, self.handler)
        scrud_post_save.connect(self.handler)
        self.addCleanup(scrud_post_save.disconnect, self.handler)
        self.signal_processor = ScrudSignalProcessorMock()
        self.addCleanup(
            scrud_post_delete.disconnect, self.signal_processor.post_delete_trigger
        )
        self.addCleanup(
            scrud_post_save.disconnect, self.signal_processor.post_save_trigger
        )

    def handler(self, sender, **kwargs):
        self.type_uri = kwargs.get('resource_type_uri')
        self.proposed_state = (
            kwargs.get('transition_data').proposed_state
            if kwargs.get('transition_data')
            else None
        )
        self.prior_state = (
            kwargs.get('transition_data').prior_state
            if kwargs.get('transition_data')
            else None
        )
        if self.type_uri == "tests://ToDo":
            self.sender = sender
            self.signal_was_called = True
            self.post_save_trigger = self.signal_processor.post_save_trigger(
                sender, **kwargs
            )
            self.post_delete_trigger = self.signal_processor.post_delete_trigger(
                sender, **kwargs
            )

    def test_should_send_signal_when_resource_created_updated_or_deleted(self):
        initial_content = {"status": "not started"}
        content_for_update = {
            "status": "in progress",
            "priority": "medium",
            "assignee": "http://somewhere.example/someone",
        }
        # For Create
        self.signal_was_called = False
        new_resource = fake_resource()
        new_resource.content = initial_content
        new_resource.resource_type.type_uri = "tests://ToDo"
        new_resource.resource_type.save()
        new_resource = ResourceRegistration.register(
            new_resource.content, new_resource.resource_type.slug
        )
        self.assertTrue(self.signal_was_called)
        self.assertEqual(new_resource.resource_type.type_uri, self.type_uri)
        self.assertEqual(new_resource.__class__, self.sender)
        # Assert Transition data is sent with the signal: a create has no prior
        # state, and its proposed state is its initial state.
        self.assertIsNone(self.prior_state)
        self.assertEqual(self.proposed_state, "not started")

        # For Update
        self.signal_was_called = False
        new_resource.content = content_for_update
        new_resource.save()
        self.assertTrue(self.signal_was_called)
        self.assertEqual(new_resource.resource_type.type_uri, self.type_uri)
        self.assertEqual(new_resource.__class__, self.sender)
        # Assert Transition data is sent with the signal
        self.assertEqual(self.proposed_state, "in progress")

        # For Delete
        self.signal_was_called = False
        new_resource.delete()
        self.assertTrue(self.signal_was_called)
        self.assertEqual(new_resource.resource_type.type_uri, self.type_uri)
        self.assertEqual(new_resource.__class__, self.sender)

    def test_workflow_actions_fetcher(self):

        work_flow_actions = workflow_actions()
        self.assertEqual(work_flow_actions, actions)

    def test_scrud_signal_processor_triggers_correct_actions(self):
        initial_content = {"status": "not started"}

        new_resource = fake_resource()
        new_resource.content = initial_content
        new_resource.resource_type.type_uri = "tests://ToDo"
        new_resource.resource_type.save()
        new_resource = ResourceRegistration.register(
            new_resource.content, new_resource.resource_type.slug
        )

        self.assertEqual(actions['tests://ToDo']['on_save'], self.post_save_trigger)

        new_resource.delete()

        self.assertEqual(actions['tests://ToDo']['on_delete'], self.post_delete_trigger)
