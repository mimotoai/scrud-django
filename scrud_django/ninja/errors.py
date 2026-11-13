"""The ninja front end's error responses: scrud's own errors as the DRF front
end answers them, the rest as scoped-rbac's ninja handlers do, each following
``SCOPED_RBAC["UNIFORM_ERRORS"]`` per request, as the DRF views read it."""

import functools
import json

from django.conf import settings
from django.utils.translation import gettext
from scoped_rbac import problems
from scoped_rbac.ninja import install_error_handlers

from scrud_django.exceptions import ScrudException

PROBLEM_JSON = "application/problem+json"

#: The members of a problem a native error body does not have.
PROBLEM_MEMBERS = frozenset({"type", "title", "status", "code"})

#: The problem-details schema and context, the targets of
#: ``problems.PROBLEM_LINKS``.
PROBLEM_LINK_TARGETS = frozenset({problems.PROBLEM_SCHEMA, problems.PROBLEM_CONTEXT})

#: The attribute marking a response scrud's own error handler answered, which
#: `answer_natively_when_off` leaves as it is.
OWN_ERROR_ATTR = "_scrud_own_error"

#: The methods in the order DRF's views list them in a 405's ``Allow``.
DRF_METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def uniform_errors() -> bool:
    """Whether the ninja front end answers errors as problem details now
    (``SCOPED_RBAC["UNIFORM_ERRORS"]`` for ``"ninja"``, read per request, as
    the DRF views read theirs)."""
    return problems.uniform_errors("ninja")


def install(api) -> None:
    """Answer `api`'s `ScrudException`s as
    `scrud_django.exceptions.scrud_exception_handler` does on DRF: the
    error's status, its `ScrudException.response_data` with that ``status``,
    written as DRF's JSON (scoped-rbac's renderer, which
    `install_error_handlers` sets), as ``application/problem+json``, without
    a ``Link`` of its own. One standing for an error DRF answers through
    scoped-rbac's handler (`ScrudException.problem_links`) is answered as
    that one: with scoped-rbac's problem-details Links while uniform errors
    are on, else as DRF's native body (``application/json``). Then
    scoped-rbac's handlers for everything else (`install_error_handlers`),
    which must come after this one and before ``api.urls`` is read."""

    def scrud_error(request, exc):
        native = exc.problem_links and not uniform_errors()
        if native:
            data = exc.native_response_data()
        else:
            data = exc.response_data({"request": request})
            data["status"] = exc.status_code
        response = api.create_response(request, data, status=exc.status_code)
        if not native:
            response["Content-Type"] = PROBLEM_JSON
            if exc.problem_links:
                response["Link"] = problems.PROBLEM_LINKS
        setattr(response, OWN_ERROR_ATTR, True)
        return response

    api.add_exception_handler(ScrudException, scrud_error)
    install_error_handlers(api)


def answer_natively_when_off(api) -> None:
    """Have every URL pattern of `api` (its operations, its 405s, its HTTP
    Schema Resources) answer, while uniform errors are off for the request,
    an error answered as problem details as DRF answers it then
    (`_native`): scrudful's own 400, 412 and 428, which scrud's routers
    always describe as problem details, and scoped-rbac's errors, when its
    handlers were installed with uniform errors on. Call it after `install`,
    before ``api.urls`` is read."""
    get_urls = api._get_urls

    @functools.wraps(get_urls)
    def _get_urls():
        patterns = get_urls()
        for pattern in patterns:
            pattern.callback = _answering_natively(pattern.callback, api)
        return patterns

    api._get_urls = _get_urls


def _answering_natively(view, api):
    @functools.wraps(view)
    def answered(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if response.status_code >= 400 and not uniform_errors():
            return _native(request, response, api)
        return response

    return answered


def _parse_error_detail(request):
    """DRF's ``JSONParser`` message for `request`'s body, which is not JSON:
    the error of decoding it with its charset (else ``DEFAULT_CHARSET``) and
    parsing it strictly (no ``NaN`` or infinities)."""
    try:
        text = request.body.decode(request.encoding or settings.DEFAULT_CHARSET)
        json.loads(text, parse_constant=_strict_constant)
    except ValueError as exc:
        return f"JSON parse error - {exc}"
    return gettext("Malformed request.")


def _strict_constant(value):
    raise ValueError(f"Out of range float values are not JSON compliant: {value!r}")


#: The ``detail`` DRF's own exception gives, with uniform errors off, where a
#: problem (by its ``code``) words it otherwise: the reason phrase of a 401
#: and a 405, a parse error's.
NATIVE_DETAILS = {
    "not_authenticated": lambda request: gettext(
        "Authentication credentials were not provided."
    ),
    "method_not_allowed": lambda request: gettext(
        'Method "{method}" not allowed.'
    ).format(method=request.method),
    "parse_error": _parse_error_detail,
}


def _native(request, response, api):
    """`response`, a problem details error, as DRF answers the error with
    uniform errors off: its body without the problem's own members
    (`PROBLEM_MEMBERS`: ``{"detail": ...}`` and any extension, such as a
    428's ``accepted-precondition-headers``), with the ``detail`` DRF's own
    exception gives where the problem words it otherwise (`NATIVE_DETAILS`),
    as ``application/json``, without the problem-details Links; a 405's
    ``Allow`` in DRF's order. scrud's own errors (`install`) and validation
    problems (whose native field errors their problem does not keep) are
    left as they are."""
    if getattr(response, OWN_ERROR_ATTR, False):
        return response
    if response.get("Content-Type", "").split(";")[0].strip() != PROBLEM_JSON:
        return response
    try:
        body = json.loads(response.content)
    except ValueError:
        return response
    if not isinstance(body, dict) or "errors" in body:
        return response
    native = {
        name: value for name, value in body.items() if name not in PROBLEM_MEMBERS
    }
    detail = NATIVE_DETAILS.get(body.get("code"))
    if detail is not None and "detail" in native:
        native["detail"] = detail(request)
    response.content = api.renderer.render(
        request, native, response_status=response.status_code
    )
    response["Content-Type"] = api.get_content_type()
    links = [
        link
        for link in _split_links(response.get("Link", ""))
        if link[1 : link.index(">")] not in PROBLEM_LINK_TARGETS
    ]
    if links:
        response["Link"] = ", ".join(links)
    elif response.has_header("Link"):
        del response["Link"]
    if response.status_code == 405 and response.has_header("Allow"):
        allowed = {method.strip() for method in response["Allow"].split(",")}
        response["Allow"] = ", ".join(m for m in DRF_METHOD_ORDER if m in allowed)
    return response


def _split_links(value):
    """The link-values of a Link field value (no URL here holds ``", <"``)."""
    if not value:
        return []
    return [part if part.startswith("<") else "<" + part for part in value.split(", <")]
