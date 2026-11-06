"""Middleware the tests install with ``override_settings(MIDDLEWARE=...)``."""


class MethodOverrideMiddleware:
    """Rewrites ``request.method`` from ``X-HTTP-Method-Override``, as some
    projects' middleware does: an OPTIONS rewritten so looks like scrudful's
    body-less copy of an OPTIONS (``scoped_rbac.access.checks_allow``)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        override = request.headers.get("X-HTTP-Method-Override")
        if override:
            request.method = override.upper()
        return self.get_response(request)
