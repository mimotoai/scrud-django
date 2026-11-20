"""The workflow of the demo's own ``articles.Article``: a draft, published once."""

ARTICLE = "demo://Article"


def policies():
    """``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]`` for the demo: policy
    content (mappings), which scrud-django compiles and checks per type."""
    return {
        ARTICLE: {
            "resource_type": ARTICLE,
            "initial_states": {"draft": 'state == "draft"'},
            "states": {"published": 'state == "published"'},
            "transitions": {"publish": {"from": "draft", "to": "published"}},
        }
    }
