"""The ninja front end's API factory."""

from collections.abc import Sequence

import ninja
from scoped_rbac.ninja.api import router as scoped_rbac_router
from scrudful.ninja import ScrudfulRouter

from scrud_django import registration
from scrud_django.ninja import computed, entry, errors, routers, services
from scrud_django.registration import TypeSpec, built_in_type_specs

#: The prefix of the URL names of scoped-rbac's routes
#: (``scoped_rbac.ninja.api.router``).
SCOPED_RBAC_ROUTES = "scoped-rbac-"


def api(
    *,
    types: Sequence[TypeSpec] = (),
    urls_namespace: str = "scrud",
    **ninja_kwargs,
) -> ninja.NinjaAPI:
    """A ``NinjaAPI`` serving scrud's resource types: the built-in ones (JSON
    Schema and JSON-LD documents, indexing and workflow policies), always
    (every stored type's Links name its schema and context resources, which
    the built-in routes serve), and each of `types`, every one registered first
    (`TypeSpec.register`): a stored type's list and item routes
    (`routers.type_router`), a computed type's item route
    (`computed.computed_router`). Also the service catalog (``services/``,
    `services.services_router`), scoped-rbac's own routes (``roles/``,
    ``role-assignments/``, ``user-rbac-policy/``, with the roles in the
    catalog, ``registration.register_role_services``), and the collection
    documents the lists link to (`routers.collections_router`). Its URL names
    are under `urls_namespace`; the rest of `ninja_kwargs` go to
    ``NinjaAPI`` (whose ``docs_url`` and ``openapi_url`` default to ``None``
    here: no interactive docs or OpenAPI document, which the DRF routes do
    not serve either, unless passed; each operation has an id of its own,
    ``<slug>_<operation>``, so the document is valid), but ``auth``
    (``TypeError``): scrud authenticates every
    request itself (`scrud_django.ninja.entry.entry`), as scoped-rbac
    decides, so that an anonymous request gets the anonymous user's grants
    and a hidden item is a 404 rather than ninja's 401.

    The ``ScrudfulRouter``s are mounted first, before any path that could
    shadow their HTTP Schema Resource routes (``/_hsr/``).

    Every route of these resolves its requester first, every method alike
    (`entry.authenticate_first`), as DRF authenticates every request. Its
    errors are answered as on DRF (`errors.install`), as uniform errors
    are for each request (`errors.answer_natively_when_off`); the routes of
    DRF views negotiating among several renderers vary on ``Accept`` as
    theirs (`routers.vary_on_accept`); the computed routes refuse the
    anonymous before their 405 (`computed.refuse_the_anonymous`); the
    plain routers' 405 is DRF's whatever uniform errors are
    (`routers.method_not_allowed_as_drf`); and the
    collection documents', the computed routes' and the catalog's answers
    give DRF's ``Allow`` (`routers.as_drf_api_views`,
    `services.as_drf_api_view_function`). More routers may be added before
    its ``urls`` are read."""
    if "auth" in ninja_kwargs:
        raise TypeError(
            "scrud_django.ninja.api() takes no auth: scrud resolves the user of "
            "each request itself (scoped_rbac.ninja.resolve_user)."
        )
    # No interactive docs or OpenAPI document unless asked for (the DRF
    # routes serve none); each operation's id is unique, so one turned on
    # is valid.
    ninja_kwargs.setdefault("docs_url", None)
    ninja_kwargs.setdefault("openapi_url", None)
    scrud_api = ninja.NinjaAPI(
        urls_namespace=urls_namespace, default_router=ScrudfulRouter(), **ninja_kwargs
    )
    specs = [*built_in_type_specs(), *types]
    type_routes = []
    computed_routes = []
    for spec in specs:
        spec.register()
    # Each router is built afresh for each API: ninja mounts a router on one
    # API only. The ScrudfulRouters first (scrudful: a path mounted before
    # one of their HSR routes must not shadow it).
    for spec in specs:
        if spec.kind == "json":
            scrud_api.add_router(
                f"{spec.slug}/",
                routers.type_router(spec, urls_namespace=urls_namespace),
            )
            type_routes.extend((f"{spec.slug}-list", f"{spec.slug}-detail"))
    scrud_api.add_router("services/", services.services_router())
    scrud_api.add_router("", scoped_rbac_router())
    registration.register_role_services()
    for spec in specs:
        if spec.kind == "computed":
            scrud_api.add_router(
                f"{spec.slug}/",
                computed.computed_router(spec, urls_namespace=urls_namespace),
            )
            computed_routes.append(f"{spec.slug}-detail")
    # The documents the lists' Links name.
    scrud_api.add_router("", routers.collections_router())
    scrud_routes = frozenset(
        [
            *type_routes,
            *computed_routes,
            *routers.COLLECTION_ROUTES,
            services.SERVICES_ROUTE,
        ]
    )
    entry.authenticate_first(
        scrud_api,
        lambda name: name in scrud_routes or name.startswith(SCOPED_RBAC_ROUTES),
    )
    errors.install(scrud_api)
    computed.refuse_the_anonymous(scrud_api, computed_routes)
    routers.method_not_allowed_as_drf(
        scrud_api, [*routers.COLLECTION_ROUTES, *computed_routes]
    )
    errors.answer_natively_when_off(scrud_api)
    routers.vary_on_accept(scrud_api, scrud_routes)
    routers.as_drf_api_views(scrud_api, [*routers.COLLECTION_ROUTES, *computed_routes])
    services.as_drf_api_view_function(
        scrud_api, [services.SERVICES_ROUTE], services.SERVICES_METHODS
    )
    return scrud_api
