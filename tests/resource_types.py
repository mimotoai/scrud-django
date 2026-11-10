import importlib.resources

from scrud_django import registration


def json_resource_types():
    """This is implemented as a function in the tests so that the json resource types
    may be recreated in `conftest.py`, otherwise they're wiped on each clean of the
    database!.
    """
    return [
        registration.json_resource_type(
            resource_type_uri="tests://PartnerProfiles",
            revision="2",
            slug="partner-profiles",
            schema_func=lambda: (
                importlib.resources.files("tests.static.partner_profile")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.partner_profile")
                .joinpath("ld.json")
                .read_text()
            ),
            indexing_policy_func=lambda: (
                importlib.resources.files("tests.static.partner_profile")
                .joinpath("indexing-policy.json")
                .read_text()
            ),
        ),
        registration.json_resource_type(
            resource_type_uri="tests://ToDo",
            revision="1",
            slug="todos",
            schema_func=lambda: (
                importlib.resources.files("tests.static.todo_flow")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.todo_flow")
                .joinpath("ld.json")
                .read_text()
            ),
        ),
        registration.json_resource_type(
            resource_type_uri="tests://TodoList",
            revision="1",
            slug="todo-lists",
            schema_func=lambda: (
                importlib.resources.files("tests.static.todo_list")
                .joinpath("schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("tests.static.todo_list")
                .joinpath("ld.json")
                .read_text()
            ),
        ),
    ]
