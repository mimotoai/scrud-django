"""The ninja front end's API factory."""

from collections.abc import Sequence

import ninja
from scrudful.ninja import ScrudfulRouter

from scrud_django.ninja import errors, routers
from scrud_django.registration import TypeSpec, built_in_type_specs


def api(
    *,
    types: Sequence[TypeSpec] = (),
    urls_namespace: str = "scrud",
    built_ins: bool = True,
    **ninja_kwargs,
) -> ninja.NinjaAPI:
    """A ``NinjaAPI`` serving scrud's resource types: the built-in ones (JSON
    Schema and JSON-LD documents, indexing and workflow policies) unless
    `built_ins` is false, and each of `types`, every one registered first
    (`TypeSpec.register`). Its URL names are under `urls_namespace`; the
    rest of `ninja_kwargs` go to ``NinjaAPI``, but ``auth`` (``TypeError``):
    scrud authenticates every request itself
    (`scrud_django.ninja.entry.entry`), as scoped-rbac decides, so that an
    anonymous request gets the anonymous user's grants and a hidden item is
    a 404 rather than ninja's 401.

    Its errors are answered as on DRF (`errors.install`), as uniform errors
    are for each request (`errors.answer_natively_when_off`), and its type
    routes vary on ``Accept`` as DRF's (`routers.vary_on_accept`). More
    routers may be added before its ``urls`` are read."""
    if "auth" in ninja_kwargs:
        raise TypeError(
            "scrud_django.ninja.api() takes no auth: scrud resolves the user of "
            "each request itself (scoped_rbac.ninja.resolve_user)."
        )
    scrud_api = ninja.NinjaAPI(
        urls_namespace=urls_namespace, default_router=ScrudfulRouter(), **ninja_kwargs
    )
    specs = [*(built_in_type_specs() if built_ins else ()), *types]
    url_names = []
    for spec in specs:
        spec.register()
        # A computed type's route is not served yet. Each router is built
        # afresh for each API: ninja mounts a router on one API only.
        if spec.kind == "json":
            scrud_api.add_router(
                f"{spec.slug}/",
                routers.type_router(spec, urls_namespace=urls_namespace),
            )
            url_names.append(f"{spec.slug}-detail")
    errors.install(scrud_api)
    errors.answer_natively_when_off(scrud_api)
    routers.vary_on_accept(scrud_api, url_names)
    return scrud_api
