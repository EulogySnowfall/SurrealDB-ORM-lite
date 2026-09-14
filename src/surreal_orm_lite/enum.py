from enum import StrEnum


class OrderBy(StrEnum):
    ASC = "ASC"
    DESC = "DESC"


class Operator(StrEnum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"


class LiveAction(StrEnum):
    """Action carried by a live-query notification.

    A ``StrEnum`` so a raw envelope stays a plain ``dict`` and still compares cleanly::

        if notif["action"] == LiveAction.CREATE: ...

    ``KILLED`` is sent by SurrealDB 3.x after ``kill()`` and has no 2.6.x counterpart; the ORM
    consumes it as an end-of-stream marker and never yields it to the caller.
    """

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    KILLED = "KILLED"
