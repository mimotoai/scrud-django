import importlib.resources
import json


def workflow_policies():
    """The test settings' ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``: policy
    content (mappings), which scrud-django compiles and checks per type."""
    return {
        "tests://ToDo": read_workflow_policy("tests.static.todo_flow", "workflow.json")
    }


def read_workflow_policy(module, filename):
    text = importlib.resources.files(module).joinpath(filename).read_text()
    return json.loads(text)
