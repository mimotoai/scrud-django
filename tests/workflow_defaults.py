import importlib.resources
import json

from scrud_django.workflow import WorkflowPolicy


def workflow_policies():
    return {
        "tests://ToDo": read_workflow_policy("tests.static.todo_flow", "workflow.json")
    }


def read_workflow_policy(module, filename):
    text = (
        importlib.resources.files("tests.static.todo_flow")
        .joinpath("workflow.json")
        .read_text()
    )
    policy_json = json.loads(text)
    return WorkflowPolicy(policy_json)
