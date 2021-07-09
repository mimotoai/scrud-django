from django.dispatch import Signal

from .conf import workflow_actions

scrud_post_save = Signal()
scrud_post_delete = Signal()


class ScrudSignalProcessor:
    workflow_actions = {}
    post_save_literal = 'on_save'
    post_delete_literal = 'on_delete'
    resource_type_uri_literal = 'resource_type_uri'

    def __init__(self) -> None:
        scrud_post_save.connect(self.post_save_trigger)
        scrud_post_delete.connect(self.post_delete_trigger)
        self.workflow_actions = workflow_actions()

    def post_save_trigger(self, sender, **kwargs):
        actions = self.workflow_actions.get(kwargs.get(self.resource_type_uri_literal))
        if self.post_save_literal in actions:
            actions[self.post_save_literal](sender, **kwargs)

    def post_delete_trigger(self, sender, **kwargs):
        actions = self.workflow_actions.get(kwargs.get(self.resource_type_uri_literal))
        if self.post_save_literal in actions:
            actions[self.post_save_literal](sender, **kwargs)
