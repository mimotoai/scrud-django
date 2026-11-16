import importlib.resources
import json

ARTICLE = "tests://Article"


def workflow_policies():
    """The test settings' ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``: policy
    content (mappings), which scrud-django compiles and checks per type."""
    return {
        "tests://ToDo": read_workflow_policy("tests.static.todo_flow", "workflow.json"),
        ARTICLE: read_workflow_policy("tests.static.article_flow", "workflow.json"),
    }


def article_state(article):
    """The content ``tests://Article``'s policies read of a ``tests.Article``."""
    return {
        "title": article.title,
        "state": article.state,
        "rbac_context": article.rbac_context,
    }


def article_contexts(article):
    """The contexts a ``tests.Article`` is in."""
    return [article.rbac_context]


def read_workflow_policy(module, filename):
    text = importlib.resources.files(module).joinpath(filename).read_text()
    return json.loads(text)
