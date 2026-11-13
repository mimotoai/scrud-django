from scrudful.utils import join_links, link_content

from scrud_django.core.urls import absolute_reverse


def get_schema_uri_for(resource_type, request, namespace=None):
    """The URL of `resource_type`'s JSON Schema: its stored document's
    detail route (`absolute_reverse`, within `namespace` first when there is
    no request resolved under one), else the URI it names, else ``None``."""
    uri = None
    # prefer a local version to a remote version
    if resource_type.schema:
        schema = resource_type.schema
        uri = absolute_reverse(
            schema.resource_type.route_name_detail(),
            args=[schema.id],
            request=request,
            namespace=namespace,
        )
    elif resource_type.schema_uri:
        uri = resource_type.schema_uri
    return uri


def get_context_uri_for(resource_type, request, namespace=None):
    """The URL of `resource_type`'s JSON-LD context: its stored document's
    detail route (`absolute_reverse`, within `namespace` first when there is
    no request resolved under one), else the URI it names, else ``None``."""
    uri = None
    # prefer a local version to a remote version
    if resource_type.context:
        context = resource_type.context
        uri = absolute_reverse(
            context.resource_type.route_name_detail(),
            args=[context.id],
            request=request,
            namespace=namespace,
        )
    elif resource_type.context_uri:
        uri = resource_type.context_uri
    return uri


def get_link_header_for(resource_type, request):
    """The Link field value of a resource of `resource_type`, as scrudful sends
    it on the resource's own responses: the schema (``rel="describedby"``,
    ``type="application/schema+json"``) and the JSON-LD context. A list
    envelope's ``link`` is this value."""
    return join_links(*get_link_values_for(resource_type, request)) or ""


def get_link_values_for(resource_type, request):
    """The link-values of `get_link_header_for`, each its own: the schema's,
    then the JSON-LD context's, either left out when the type has none."""
    schema_uri = get_schema_uri_for(resource_type, request)
    context_uri = get_context_uri_for(resource_type, request)
    schema_link = (
        link_content(schema_uri, "describedby", "application/schema+json")
        if schema_uri
        else None
    )
    context_link = (
        link_content(
            context_uri,
            "http://www.w3.org/ns/json-ld#context",
            "application/ld+json",
        )
        if context_uri
        else None
    )
    return [link for link in (schema_link, context_link) if link]
