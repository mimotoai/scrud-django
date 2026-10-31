from django.utils import timezone
from django.utils.http import http_date


def link_content(url, rel, content_type):
    return f"<{url}>; rel=\"{rel}\"; type=\"{content_type}\""


def http_date_for(value):
    """The HTTP date (``Last-Modified``) of the datetime `value`. A naive one
    (stored with ``USE_TZ = False``) is in the default ``TIME_ZONE``, as Django
    and scrudful (``Last-Modified`` headers) read it, not UTC."""
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_default_timezone())
    return http_date(value.timestamp())
