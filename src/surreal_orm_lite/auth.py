"""Pure helpers for connection-level authentication (v0.16.0).

Everything here is server-free: a token container, the credentials → SDK-payload translation
(which is also where every argument is validated, so a malformed call fails before any round
trip), and the error normalisation. ``connection_manager.py`` holds the async methods that use
them.

Keeping this module I/O-free mirrors how ``functions.py`` backs ``call_function()``: it
imports only :mod:`.exceptions`, so it stays cycle-free — and it is where v0.17.0's
``AuthenticatedUserMixin`` will land.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .exceptions import SurrealDbAuthenticationError

__all__ = ["AuthTokens", "build_auth_payload", "wrap_auth_error"]


@dataclass(frozen=True, slots=True)
class AuthTokens:
    """The tokens returned by :meth:`SurrealDBConnectionManager.signin` / ``signup``.

    ``access`` is the JWT the server issues for the session — hand it to a web client, and
    give it back later through ``authenticate()``.

    ``refresh`` is **SurrealDB 3.x only**, and only when the access method declares
    ``WITH REFRESH``: 2.6.x cannot parse that clause at all, so the field is always ``None``
    there. It buys a renewal without re-asking for the password, through
    ``signin(access=…, refresh=…)``.

    The ORM defines this rather than re-exporting the SDK's ``Tokens`` for two reasons:
    ``access`` is typed non-optional here (a signin that returned nothing raises instead of
    handing back ``None``), and the repr redacts. A token that lands in a log line, a
    traceback or a pytest assertion diff is a leaked credential, so there is deliberately
    **no** ``__str__`` returning the JWT either — read ``tokens.access`` explicitly.

    Redaction covers every rendering path (``repr``, ``str``, f-strings, nesting in a list or
    dict). It cannot cover ``dataclasses.asdict()`` / ``astuple()``, which read the fields
    directly — so do not hand this object to a generic serialiser.
    """

    access: str
    refresh: str | None = None

    def __repr__(self) -> str:
        """Redact both tokens, while still saying whether a refresh token is present.

        That last bit is the reason anyone prints this object — "did 3.x give me a refresh
        token?" — and it leaks nothing.
        """
        refresh = "<redacted>" if self.refresh is not None else "None"
        return f"AuthTokens(access=<redacted>, refresh={refresh})"


def build_auth_payload(
    *,
    username: str | None = None,
    password: str | None = None,
    access: str | None = None,
    variables: Mapping[str, Any] | None = None,
    refresh: str | None = None,
    namespace: str | None = None,
    database: str | None = None,
    default_namespace: str | None = None,
    default_database: str | None = None,
) -> dict[str, Any]:
    """Translate ORM keyword credentials into the payload dict the SDK expects.

    Exactly one of three shapes must be supplied:

    - **record access** — ``access`` + ``variables``;
    - **system user** — ``username`` + ``password``;
    - **refresh exchange** — ``access`` + ``refresh`` (SurrealDB 3.x only).

    Namespace and database are treated differently per shape, and that asymmetry is the whole
    point of this function. Record access *requires* them, so they default to the configured
    connection's. A system user must **not** get them by default: a root user is defined at
    neither level, and sending them would silently turn a root signin into a database-user
    signin against a user the server does not have.

    :raises ValueError: for any invalid combination — raised here, before the caller issues a
        request, so a mistake never costs a round trip or a half-changed session.
    """
    has_system = username is not None or password is not None
    has_record = access is not None or variables is not None or refresh is not None

    if has_system and has_record:
        raise ValueError(
            "Pass either system-user credentials (username=, password=) or record access "
            "(access= with variables= or refresh=), not both — they are two different "
            "identities."
        )
    if not has_system and not has_record:
        raise ValueError(
            "No credentials given. Pass username= and password= for a system user, "
            "access= and variables= for a record user, or access= and refresh= to renew "
            "an existing record session."
        )
    if database is not None and namespace is None:
        raise ValueError("database= needs namespace=: SurrealDB scopes a database inside a namespace.")

    if has_system:
        if username is None or password is None:
            raise ValueError("A system user needs both username= and password=.")
        payload: dict[str, Any] = {"username": username, "password": password}
        # Only what the caller asked for: the level a system user lives at is what
        # distinguishes a root user from a namespace or database one.
        if namespace is not None:
            payload["namespace"] = namespace
        if database is not None:
            payload["database"] = database
        return payload

    if access is None:
        raise ValueError("access= is required to name the DEFINE ACCESS method that variables= or refresh= applies to.")
    if variables is not None and refresh is not None:
        raise ValueError(
            "Pass either variables= or refresh=, not both — variables= proves an identity "
            "from scratch, refresh= renews one that already exists."
        )
    if variables is None and refresh is None:
        raise ValueError("access= needs variables= (to sign in) or refresh= (to renew a session).")

    if (namespace is None) != (database is None):
        # Half-explicit is a trap: pairing an explicit namespace with the *configured*
        # database (or vice versa) points the signin at a target the caller never named.
        raise ValueError(
            "Pass namespace= and database= together, or neither — pairing one explicit value "
            "with the configured default silently targets a database you did not name."
        )
    resolved_namespace = namespace if namespace is not None else default_namespace
    resolved_database = database if database is not None else default_database
    if resolved_namespace is None or resolved_database is None:
        raise ValueError(
            "Record access needs a namespace and a database. Set them on the connection "
            "(set_connection) or pass namespace= and database= explicitly."
        )

    payload = {"namespace": resolved_namespace, "database": resolved_database, "access": access}
    if refresh is not None:
        # Top level, never nested under "variables" — the server answers a nested one with
        # NotAllowedError (measured on 3.2.4).
        payload["refresh"] = refresh
    else:
        payload["variables"] = dict(variables) if variables is not None else {}
    return payload


def wrap_auth_error(exc: Exception, operation: str) -> SurrealDbAuthenticationError:
    """Normalise any failure of an auth call into :class:`SurrealDbAuthenticationError`.

    The mapping is unconditional rather than message-matched. These operations do nothing but
    authenticate, so any failure they raise *is* an authentication failure — and matching on
    wording would be actively fragile: the same wrong password reads ``NotFoundError: No
    record was returned`` on SurrealDB 3.x and ``InternalError: There was a problem with the
    database: No record was returned`` on 2.6.x, while a malformed token is rejected by the
    SDK's own CBOR schema check as a bare ``ValueError`` that never reaches a server.

    The original is preserved in the message and should also be chained by the caller with
    ``raise … from exc``.
    """
    return SurrealDbAuthenticationError(f"Authentication failed during {operation}: {exc}")
