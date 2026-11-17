"""The test types' ninja API (`tests.resource_types.ninja_type_specs`), as
`tests.parity_urls` mounts it. Its URL patterns are read once (`URLS`): ninja
registers an API's URL namespace when they are read, and refuses a second
read for the same one."""

import scrud_django.ninja

from .resource_types import ninja_type_specs

API = scrud_django.ninja.api(types=ninja_type_specs(), urls_namespace="scrud-ninja")
URLS = API.urls

#: `URLS`' patterns outside the API's namespace, as ``tests.urls`` names the
#: DRF routes: the suite without DRF mounts them so (``tests.no_drf_urls``,
#: ``tests.mounted_urls``), so that a test's ``reverse("todos-list")``
#: names the same type's route on either front end.
PATTERNS = URLS[0]
