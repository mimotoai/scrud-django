"""The ninja front end's error responses: scrud's own errors as the DRF front
end answers them, the rest as scoped-rbac's ninja handlers do."""

from scoped_rbac.ninja import install_error_handlers

from scrud_django.exceptions import ScrudException

PROBLEM_JSON = "application/problem+json"


def install(api) -> None:
    """Answer `api`'s `ScrudException`s as
    `scrud_django.exceptions.scrud_exception_handler` does on DRF: the
    error's status, its `ScrudException.response_data` with that ``status``,
    written as DRF's JSON (scoped-rbac's renderer, which
    `install_error_handlers` sets), as ``application/problem+json``, without
    a ``Link`` of its own. Then scoped-rbac's handlers for everything else
    (`install_error_handlers`), which must come after this one and before
    ``api.urls`` is read."""

    def scrud_error(request, exc):
        data = exc.response_data({"request": request})
        data["status"] = exc.status_code
        response = api.create_response(request, data, status=exc.status_code)
        response["Content-Type"] = PROBLEM_JSON
        return response

    api.add_exception_handler(ScrudException, scrud_error)
    install_error_handlers(api)
