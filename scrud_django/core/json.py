"""JSON written as DRF's ``JSONRenderer`` writes it with its default
settings, without DRF: compact separators (DRF's indented ones when
indented), non-ASCII characters as UTF-8 rather than ``\\u`` escapes,
U+2028 and U+2029 escaped (so that the JSON is a strict JavaScript subset),
``NaN`` and the infinities refused (``ValueError``). The studio's answers,
the UI's file views and the ninja front end's indented JSON write through
it.

A value that is not JSON's own is written by the encoder given, by default
Django's ``DjangoJSONEncoder``, as the studio has always written it; it
writes dates, times, UUIDs and datetimes without microseconds as DRF's
encoder does, but a datetime's microseconds to milliseconds, a ``Decimal``
as a string and a ``timedelta`` in ISO 8601, where DRF writes all the
microseconds, a number and the seconds. What scrud answers is stored JSON,
which has none of these."""

import json

from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse

JSON = "application/json"

# DRF's separators (``rest_framework.compat``): compact, then indented.
SHORT_SEPARATORS = (",", ":")
INDENT_SEPARATORS = (",", ": ")


def drf_json_text(data, *, indent: int | None = None, encoder=DjangoJSONEncoder) -> str:
    """`data` as the text DRF's ``JSONRenderer`` writes for it, indented by
    `indent` (``None``: compact), each value the JSON encoder class
    `encoder` cannot write itself encoded by it. ``NaN`` and the infinities
    are refused (``ValueError``). ``None`` is written ``null`` (DRF's
    renderer writes no body for it: a caller answering no body says so
    itself)."""
    text = json.dumps(
        data,
        cls=encoder,
        indent=indent,
        ensure_ascii=False,
        allow_nan=False,
        separators=SHORT_SEPARATORS if indent is None else INDENT_SEPARATORS,
    )
    # Fully escaped, so that the JSON is a strict JavaScript subset.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def dumps_like_drf(
    data, *, indent: int | None = None, encoder=DjangoJSONEncoder
) -> bytes:
    """The bytes DRF's ``JSONRenderer`` writes for `data` (`drf_json_text`,
    as UTF-8), for JSON scrud stores: with the default `encoder`, a value
    that is not JSON's own may be written otherwise than DRF writes it (see
    this module's docstring). Only the tests call it, to compare with
    DRF's bytes."""
    return drf_json_text(data, indent=indent, encoder=encoder).encode("utf-8")


def json_response(data, *, status=200, headers=None, content_type=JSON) -> HttpResponse:
    """`data` as a JSON response of `content_type`, written as DRF's
    ``JSONRenderer`` writes it (`drf_json_text`: a ``ValueError`` for
    ``NaN`` and the infinities), with `headers` (a ``Content-Type`` among
    them replaces `content_type`)."""
    # Text, encoded in the response's charset (as the studio wrote it).
    response = HttpResponse(
        drf_json_text(data), status=status, content_type=content_type
    )
    for name, value in (headers or {}).items():
        response[name] = value
    return response
