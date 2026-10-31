"""Model fields of scrud_django's models."""

from django.db import models


class ContextField(models.CharField):
    """An RBAC context (a string), compared exactly, code point for code
    point, on every database, as scoped-rbac compares contexts in Python: a
    list filtered by it in SQL (`scrud_django.filters`) agrees with the
    detail route.

    On SQLite and PostgreSQL it is a ``CharField``. MySQL and MariaDB
    compare text under a collation, and none of theirs is exact: their
    defaults (``utf8mb4_0900_ai_ci``, ``utf8mb4_uca1400_ai_ci``) ignore case
    and accents, and ``utf8mb4_bin`` pads with spaces (``"a" = "a "``). There
    the column is ``VARBINARY(max_length)``, holding the context's UTF-8
    bytes, compared byte for byte: `max_length` is then a length in bytes
    (``MAX_CONTEXT_BYTES``, which every stored context fits), and an index of
    it fits InnoDB's 3072 bytes, which a ``VARCHAR(2048)`` in utf8mb4 (8192
    bytes) does not."""

    def db_type(self, connection):
        if connection.vendor == "mysql":
            return f"varbinary({self.max_length})"
        return super().db_type(connection)

    def get_db_prep_value(self, value, connection, prepared=False):
        value = super().get_db_prep_value(value, connection, prepared)
        if connection.vendor == "mysql" and isinstance(value, str):
            return value.encode("utf-8", "surrogatepass")
        return value

    def from_db_value(self, value, expression, connection):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", "surrogatepass")
        return value
