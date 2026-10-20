"""Server-side events and DDL helpers (v0.31.0): the ``Event`` value object, the structural check of a hand-written
SurrealQL fragment (``define_parser.scan_fragment()``), the kind reader's free-text generic argument and the pure
renderers (``Event.ddl()``, ``function_ddl()``, the removal statements).

Unit tests (no server): ``scan_fragment()`` (spec §6.2), ``Event``'s construction and the exact texts of E1–E11
(spec §5.1, §5.6; AC-2), the ``_KindReader.raw_argument()`` hardening (spec §6.2), the golden statements of spec
§5.2 with the exact texts of E12–E13 and F1–F9 (AC-3), and the events of a model — ``SurrealConfigDict(events=)``,
``get_events()``, ``event_ddl()`` and the exact texts of E14 (spec §5.3; AC-4), and applying DDL with fakes — the
refusals ``apply_ddl_statements(classify=True)`` classifies (S1–S3), ``check_call_transaction(operation=)`` and the
``ASYNC`` check C1 (spec §5.6, §6.4, §6.5; AC-13). The check catches mistakes and is not an injection defence: every
refused input below is a plain malformed name, fragment or kind.

E2E tests (classes suffixed ``E2E``) on the line under test over WebSocket and HTTP, and on ``mem://``: the named
connection ``"evt"`` on the database ``evt_db``, reset before each test (spec §9.3) — events applied and removed from a
model and from the manager, fired by ORM and raw writes, ``ASYNC`` events on 3.x and refused before any DDL on 2.x and
the embedded engine, the per-line facts (``$input``, an unchanged ``upsert()``), server refusals classified, names
refused before any request and comments read back (spec §5.3, §5.4; AC-5, AC-6, AC-7, AC-13, AC-15); events applied
with ``tx=`` on both transaction strategies and a write a guard event refuses inside ``transaction()``, the model's and
the manager's connection with a transaction of another one refused before any request, a record user refused (S1),
and the rows events write seen by ``watch()`` and by the query cache once invalidated (spec §7; AC-8 a–b, AC-9, AC-12,
AC-16); stored functions defined, called, redefined and removed through the manager, their cached signature dropped
at once and again after a transaction commits, with ``tx=``, another connection's transaction and a record user
refused, and every definition read back by the schema introspection on each line (spec §5.4; AC-8d, AC-9, AC-10,
AC-11, AC-12, AC-15).
"""

import ast
import asyncio
import contextlib
import contextvars
import dataclasses
import inspect
import os
import pickle
import re
import traceback
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest
from surrealdb import InternalError, NotAllowedError, ValidationError

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    DatabaseIntrospector,
    LiveAction,
    QueryCache,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDbConflictError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
    Transaction,
    schema,
)
from surreal_orm_lite._sdk import ServerError
from surreal_orm_lite.define_parser import (
    DefineParseError,
    KLiteral,
    KName,
    KObject,
    KUnion,
    parse_kind,
    render_kind,
    scan_fragment,
)
from surreal_orm_lite.routing import check_call_transaction
from surreal_orm_lite.schema import (
    Event,
    ensure_async_events_supported,
    event_subject,
    function_ddl,
    remove_event_statement,
    remove_function_statement,
)
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import apply_ddl_statements
from tests._readme_tables import (
    assert_embedded_rows,
    assert_server_header_sentence,
    assert_server_rows,
    assert_server_table,
)
from tests.conftest import default_connection_class
from tests.test_embedded import (
    _EMBEDDED_BEHAVIOUR,
    _cells,
    _normalised,
    _readme,
    _readme_part,
    _subsection,
    _table_rows,
)
from tests.test_embedded import _tables as _md_tables
from tests.test_query_cache import _bullets
from tests.test_time_travel import _SERVER_BEHAVIOUR

# The fixed DefineParseError reasons, written out: the tests pin the exact texts.
_R_BRACKETS = "its brackets are not balanced"
_R_STRING = "a string literal is not terminated"
_R_IDENT = "an identifier is not terminated"
_R_NUL = "it holds a NUL character (\\x00)"
_R_COMMENT = "it holds a SurrealQL comment marker (-- , #, // or /* */)"
_R_KIND = "its kind is not understood"

_IDENTIFIER_EXPECTED = "an identifier is expected (letters, digits, underscores; not starting with a digit)."


def _reason(error: pytest.ExceptionInfo[DefineParseError]) -> str:
    assert len(error.value.args) == 1
    reason = error.value.args[0]
    assert str(error.value) == reason
    return str(reason)


def _embed_error(arg: str, reason: str) -> str:
    """E6's text for the event ``'e'``."""
    return f"Event 'e': {arg}= is not SurrealQL the ORM can embed: {reason}."


_E10 = "Event 'e': retry= and max_depth= apply to ASYNC events only; set async_mode=True (SurrealDB 3.x)."

#: ``(args, kwargs, exception class, exact text)`` — one case per refusal of spec §5.6 E1–E11.
_EVENT_ERRORS = [
    # E1
    pytest.param((1,), {"then": "x"}, TypeError, "Event name must be a str, got int.", id="E1-int"),
    pytest.param((None,), {"then": "x"}, TypeError, "Event name must be a str, got NoneType.", id="E1-none"),
    # E2
    *[
        pytest.param(
            (name,), {"then": "x"}, ValueError, f"Invalid event name {name!r}: {_IDENTIFIER_EXPECTED}", id=f"E2-{name!r}"
        )
        for name in ["", "a b", "a\n", "a`b", "1a", "a;b", "a-b", "é"]
    ],
    # E3
    pytest.param(("e",), {"then": 1}, TypeError, "Event 'e': then= must be a str, got int.", id="E3-then"),
    pytest.param(("e",), {"then": "x", "when": 1}, TypeError, "Event 'e': when= must be a str, got int.", id="E3-when"),
    pytest.param(
        ("e",), {"then": "x", "comment": 1}, TypeError, "Event 'e': comment= must be a str, got int.", id="E3-comment"
    ),
    # E4
    *[
        pytest.param(
            ("e",),
            {"then": then},
            ValueError,
            "Event 'e': then= is empty; it is the SurrealQL the event runs.",
            id=f"E4-{then!r}",
        )
        for then in ["", "  ", "\n\t"]
    ],
    # E5
    *[
        pytest.param(
            ("e",),
            {"then": "x", "when": when},
            ValueError,
            "Event 'e': when= is empty; leave it out to run the event on every write.",
            id=f"E5-{when!r}",
        )
        for when in ["", "  "]
    ],
    # E6 — brackets, strings, comment markers and NUL, in then= and in when=
    pytest.param(("e",), {"then": "(1"}, ValueError, _embed_error("then", _R_BRACKETS), id="E6-then-brackets"),
    pytest.param(
        ("e",), {"then": "CREATE log SET x = 1 }"}, ValueError, _embed_error("then", _R_BRACKETS), id="E6-then-closer"
    ),
    # Every / is an operator, so the misplaced ) and ( are seen.
    pytest.param(("e",), {"then": "$a/2) + ($b/3"}, ValueError, _embed_error("then", _R_BRACKETS), id="E6-then-slashes"),
    pytest.param(("e",), {"then": "'x"}, ValueError, _embed_error("then", _R_STRING), id="E6-then-string"),
    pytest.param(("e",), {"then": "x", "when": "'x"}, ValueError, _embed_error("when", _R_STRING), id="E6-when-string"),
    pytest.param(("e",), {"then": "x", "when": "(true"}, ValueError, _embed_error("when", _R_BRACKETS), id="E6-when-brackets"),
    pytest.param(("e",), {"then": "CREATE `log"}, ValueError, _embed_error("then", _R_IDENT), id="E6-then-ident"),
    *[
        pytest.param(("e",), {"then": then}, ValueError, _embed_error("then", _R_COMMENT), id=f"E6-then-comment-{then!r}")
        for then in ["NONE -- x", "NONE # x", "NONE // x", "NONE /* x */", "NONE --"]
    ],
    pytest.param(
        ("e",),
        {"then": "x", "when": "$event = 'DELETE' -- only deletes"},
        ValueError,
        _embed_error("when", _R_COMMENT),
        id="E6-when-comment",
    ),
    pytest.param(("e",), {"then": "NONE\x00"}, ValueError, _embed_error("then", _R_NUL), id="E6-then-nul"),
    pytest.param(("e",), {"then": "x", "when": "true\x00"}, ValueError, _embed_error("when", _R_NUL), id="E6-when-nul"),
    # E7
    pytest.param(
        ("e",),
        {"then": "x", "when": "true; false"},
        ValueError,
        "Event 'e': when= is one expression; it holds a ';' outside brackets.",
        id="E7",
    ),
    # E8
    pytest.param(
        ("e",), {"then": "x", "async_mode": 1}, TypeError, "Event 'e': async_mode= must be a bool, got int.", id="E8-async"
    ),
    pytest.param(
        ("e",),
        {"then": "x", "async_mode": None},
        TypeError,
        "Event 'e': async_mode= must be a bool, got NoneType.",
        id="E8-async-none",
    ),
    pytest.param(
        ("e",),
        {"then": "x", "async_mode": True, "retry": True},
        TypeError,
        "Event 'e': retry= must be an int or None, got bool.",
        id="E8-retry-bool",
    ),
    pytest.param(
        ("e",),
        {"then": "x", "async_mode": True, "retry": 1.0},
        TypeError,
        "Event 'e': retry= must be an int or None, got float.",
        id="E8-retry-float",
    ),
    pytest.param(
        ("e",),
        {"then": "x", "async_mode": True, "max_depth": "3"},
        TypeError,
        "Event 'e': max_depth= must be an int or None, got str.",
        id="E8-max-depth-str",
    ),
    # E9
    *[
        pytest.param(
            ("e",),
            {"then": "x", "async_mode": True, arg: value},
            ValueError,
            f"Event 'e': {arg}={value} is outside 0..65535, the range SurrealDB accepts.",
            id=f"E9-{arg}-{value}",
        )
        for arg in ["retry", "max_depth"]
        for value in [-1, 65536]
    ],
    # E10
    pytest.param(("e",), {"then": "x", "retry": 3}, ValueError, _E10, id="E10-retry"),
    pytest.param(("e",), {"then": "x", "retry": 0}, ValueError, _E10, id="E10-retry-zero"),
    pytest.param(("e",), {"then": "x", "max_depth": 3}, ValueError, _E10, id="E10-max-depth"),
    pytest.param(("e",), {"then": "x", "async_mode": False, "retry": 3, "max_depth": 3}, ValueError, _E10, id="E10-both"),
    # E11
    pytest.param(
        ("e",),
        {"then": "x", "comment": "a\x00b"},
        ValueError,
        "Event 'e': comment= holds a NUL character (\\x00), which SurrealQL cannot carry.",
        id="E11",
    ),
]


class TestEvent:
    """AC-2: ``scan_fragment()`` and the ``Event`` value object (spec §5.1, §5.6 E1–E11, §6.2)."""

    # --- scan_fragment(): the structural check of a hand-written fragment ---------------------------------------

    @pytest.mark.parametrize(
        ("fragment", "top_level_semicolon"),
        [
            ("math::floor($after.total/100)", False),  # every / is an operator
            ("$a/2 + $b/3", False),
            ("CREATE log SET x = 1", False),
            ("$before.name != $after.name", False),
            ("CREATE x SET s = 'a; b }'", False),  # a ; or a } inside a string is data
            ('CREATE x SET s = "a; b )"', False),
            ("CREATE x SET s = 'it\\'s; ok'", False),  # an escaped quote does not end the literal
            ("CREATE x SET s = 'a -- b'", False),  # a comment marker inside a literal is data
            ('CREATE x SET s = "a # b // c /* d */"', False),
            ("CREATE `a -- b` SET x = 1", False),  # inside a quoted identifier too
            ("CREATE ⟨a#b⟩ SET x = 1", False),
            ("IF $a { CREATE x } ELSE { CREATE y }", False),
            ("[1, (2), { a: 3 }]", False),
            ("$a - -1", False),  # a single - is an operator
            ("LET $x = 1; CREATE log SET x = $x", True),  # a real top-level ;
            ("{ LET $x = 1; CREATE log SET x = $x }", False),  # inside a block, the ; is not top level
            ("CREATE log; ", True),
        ],
    )
    def test_scan_fragment_reports_a_top_level_semicolon(self, fragment: str, top_level_semicolon: bool) -> None:
        assert scan_fragment(fragment) is top_level_semicolon

    @pytest.mark.parametrize(
        ("fragment", "reason"),
        [
            ("CREATE log SET x = 1 }", _R_BRACKETS),
            ("CREATE log SET x = (1", _R_BRACKETS),
            ("$a/2) + ($b/3", _R_BRACKETS),  # every / is an operator, so the misplaced ) and ( are seen
            ("(1]", _R_BRACKETS),
            ("[1)", _R_BRACKETS),
            ("'x", _R_STRING),
            ('"x', _R_STRING),
            ("'x\\'", _R_STRING),
            ("`x", _R_IDENT),
            ("⟨x", _R_IDENT),
            ("NONE\x00", _R_NUL),
            ("CREATE x SET s = 'a\x00b'", _R_NUL),  # a raw NUL anywhere, inside a literal too
            ("NONE -- x", _R_COMMENT),
            ("NONE # x", _R_COMMENT),
            ("NONE // x", _R_COMMENT),
            ("NONE /* x */", _R_COMMENT),
            ("NONE#", _R_COMMENT),
            ("$a--1", _R_COMMENT),
        ],
    )
    def test_scan_fragment_refuses_a_malformed_fragment(self, fragment: str, reason: str) -> None:
        with pytest.raises(DefineParseError) as error:
            scan_fragment(fragment)
        assert _reason(error) == reason

    @pytest.mark.parametrize(
        ("fragment", "reason"),
        [("$after.name = /a(/", _R_BRACKETS), ("$after.name = /it's/", _R_STRING), ("$after.name = /a#b/", _R_COMMENT)],
    )
    def test_a_raw_regex_literal_is_read_as_plain_characters(self, fragment: str, reason: str) -> None:
        # Every / is an operator: a raw regex literal holding a bracket, a quote or a comment marker of its own is
        # refused, and a ; inside one counts as top level. Keep such a pattern in a string (a string:: call) or, for
        # the ;, inside a { … } block.
        with pytest.raises(DefineParseError) as error:
            scan_fragment(fragment)
        assert _reason(error) == reason

    def test_a_semicolon_in_a_raw_regex_literal_counts_as_top_level(self) -> None:
        assert scan_fragment("$after.code = /x;y/") is True
        assert scan_fragment("{ $after.code = /x;y/ }") is False

    def test_scan_fragment_is_exported_by_the_parser(self) -> None:
        from surreal_orm_lite import define_parser

        assert "scan_fragment" in define_parser.__all__

    # --- construction --------------------------------------------------------------------------------------------

    def test_valid_event(self) -> None:
        event = Event("email_audit", when="$before.email != $after.email", then="CREATE log")
        assert event.name == "email_audit"
        assert event.when == "$before.email != $after.email"
        assert event.then == "CREATE log"

    def test_defaults(self) -> None:
        event = Event("email_audit", then="CREATE log")
        assert event.when is None
        assert event.async_mode is False
        assert event.retry is None
        assert event.max_depth is None
        assert event.comment is None

    def test_then_is_keyword_only(self) -> None:
        constructor: Any = Event
        with pytest.raises(TypeError):
            constructor("e", "x")

    def test_then_and_when_are_stored_stripped(self) -> None:
        event = Event("e", when="  $event = 'CREATE'\n", then="\n  CREATE log SET x = 1  ")
        assert event.then == "CREATE log SET x = 1"
        assert event.when == "$event = 'CREATE'"
        assert event == Event("e", when="$event = 'CREATE'", then="CREATE log SET x = 1")

    def test_then_may_hold_several_statements(self) -> None:
        event = Event("e", then="LET $n = 1; CREATE log SET n = $n")
        assert event.then == "LET $n = 1; CREATE log SET n = $n"

    def test_a_comment_marker_inside_a_literal_constructs(self) -> None:
        assert Event("e", then="CREATE x SET s = 'a -- b'").then == "CREATE x SET s = 'a -- b'"
        assert Event("e", when="$after.s = 'a # b'", then="NONE").when == "$after.s = 'a # b'"

    def test_bounds_are_accepted(self) -> None:
        low = Event("e", then="NONE", async_mode=True, retry=0, max_depth=65535)
        high = Event("e", then="NONE", async_mode=True, retry=65535, max_depth=0)
        assert (low.retry, low.max_depth) == (0, 65535)
        assert (high.retry, high.max_depth) == (65535, 0)
        assert Event("e", then="NONE", async_mode=True).async_mode is True

    def test_a_comment_is_kept_as_written(self) -> None:
        comment = 'it\'s "q" \\ \nnext'
        assert Event("e", then="NONE", comment=comment).comment == comment
        assert Event("e", then="NONE", comment="").comment == ""

    def test_frozen(self) -> None:
        event = Event("e", then="NONE")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.name = "other"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.then = "other"  # type: ignore[misc]

    def test_equal_by_value_and_hashable(self) -> None:
        first = Event("e", when="true", then="NONE", async_mode=True, retry=2, max_depth=3, comment="c")
        second = Event("e", when="true", then="NONE", async_mode=True, retry=2, max_depth=3, comment="c")
        assert first == second
        assert first is not second
        assert hash(first) == hash(second)
        assert len({first, second}) == 1
        assert first != Event("e", when="false", then="NONE", async_mode=True, retry=2, max_depth=3, comment="c")
        assert first != Event("e", when="true", then="NONE", async_mode=True, retry=1, max_depth=3, comment="c")

    def test_pickle_round_trip(self) -> None:
        event = Event("e", when="true", then="NONE", async_mode=True, retry=0, max_depth=65535, comment="it's")
        # Spec §5.1: an Event is picklable. The bytes are the ones this test just produced.
        copy = pickle.loads(pickle.dumps(event))
        assert copy == event
        assert copy.then == "NONE"

    def test_repr_leaves_out_when_and_then(self) -> None:
        event = Event("email_audit", when="$before.email != $after.email", then="CREATE log")
        text = repr(event)
        assert "name='email_audit'" in text
        assert "CREATE log" not in text
        assert "$before.email" not in text
        assert text == "Event(name='email_audit', async_mode=False, retry=None, max_depth=None, comment=None)"

    # --- refusals ------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(("args", "kwargs", "error", "text"), _EVENT_ERRORS)
    def test_refusals(self, args: tuple[Any, ...], kwargs: dict[str, Any], error: type[Exception], text: str) -> None:
        constructor: Any = Event
        with pytest.raises(error) as raised:
            constructor(*args, **kwargs)
        assert type(raised.value) is error
        assert str(raised.value) == text

    @pytest.mark.parametrize(
        ("kwargs", "text"),
        [
            # The name first, so every later message can name the event.
            ({"name": 1, "then": 1}, "Event name must be a str, got int."),
            # then= before when=.
            ({"name": "e", "then": "(1", "when": 1}, _embed_error("then", _R_BRACKETS)),
            # when= before async_mode=.
            (
                {"name": "e", "then": "x", "when": "", "async_mode": 1},
                "Event 'e': when= is empty; leave it out to run the event on every write.",
            ),
            # async_mode= before retry=.
            ({"name": "e", "then": "x", "async_mode": 1, "retry": True}, "Event 'e': async_mode= must be a bool, got int."),
            # The types and ranges of retry= and max_depth= before E10.
            ({"name": "e", "then": "x", "retry": True}, "Event 'e': retry= must be an int or None, got bool."),
            (
                {"name": "e", "then": "x", "max_depth": 65536},
                "Event 'e': max_depth=65536 is outside 0..65535, the range SurrealDB accepts.",
            ),
            # E10 before comment=.
            ({"name": "e", "then": "x", "retry": 1, "comment": 1}, _E10),
        ],
    )
    def test_the_checks_run_in_field_order(self, kwargs: dict[str, Any], text: str) -> None:
        constructor: Any = Event
        with pytest.raises((TypeError, ValueError)) as raised:
            constructor(**kwargs)
        assert str(raised.value) == text

    # --- export and docstrings -----------------------------------------------------------------------------------

    def test_event_is_exported(self) -> None:
        from surreal_orm_lite import Event as Exported

        assert Exported is schema.Event
        assert "Event" in surreal_orm_lite.__all__
        assert "Event" in schema.__all__

    @pytest.mark.parametrize("documented", [schema, Event, scan_fragment], ids=["module", "Event", "scan_fragment"])
    def test_the_docstrings_say_what_the_check_is(self, documented: object) -> None:
        text = " ".join((documented.__doc__ or "").split())
        assert "catches mistakes" in text
        assert "not an injection defence" in text
        assert "cannot inject" not in text
        assert "cannot close" not in text

    def test_the_module_and_event_say_expressions_are_trusted_code(self) -> None:
        for documented in (schema, Event):
            text = " ".join((documented.__doc__ or "").split())
            assert "expressions are trusted application code, never user input" in text


class TestKindReaderHardening:
    """Spec §6.2 (D18): the free text of an unknown generic argument (``file<…>``) refuses a comment marker, a quote
    character and a ``;`` (one expression, like the other one-expression contexts); every other kind reads as
    before."""

    @pytest.mark.parametrize(
        "text",
        [
            "file<x -- y>",
            "file<x # y>",
            "file<x//y>",
            "file<a/*b>",
            "file<'z'>",
            'file<"q">',
            "file<x -- 'y>",  # an unterminated quote: refused before too
            "frob<a<b>, 'c>'>",
            "option<file<x # y>>",
            "file<`a--b`>",  # the whole argument text is checked
        ],
    )
    def test_a_comment_marker_or_a_quote_in_a_free_text_argument_is_refused(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_kind(text)
        assert _reason(error) == _R_KIND

    @pytest.mark.parametrize(
        "text",
        [
            "file<x; y>",
            "file<x;y>",
            "file<;>",
            "file<x;>",
            "option<file<x; y>>",
            "frob<a<b>; c>",
            "frob<a (b; c)>",  # inside brackets too
            "frob<a { b; c }>",
            "file<`a;b`>",  # the whole argument text is checked
            "int | file<x; y>",
        ],
    )
    def test_a_semicolon_in_a_free_text_argument_is_refused(self, text: str) -> None:
        with pytest.raises(DefineParseError) as error:
            parse_kind(text)
        assert _reason(error) == _R_KIND

    @pytest.mark.parametrize(
        ("text", "tree"),
        [
            ("file<bucket>", KName("file", ("bucket",))),
            ("file< bucket >", KName("file", ("bucket",))),
            ("file<a/b>", KName("file", ("a/b",))),  # one / is not a comment marker
            ("file<a - b>", KName("file", ("a - b",))),
            ("frob<a<b>, c>", KName("frob", ("a<b>, c",))),
            ("frob<a<b>, `c>`>", KName("frob", ("a<b>, `c>`",))),  # a quoted identifier is still skipped
            ("frob<⟨c>⟩>", KName("frob", ("⟨c>⟩",))),
            # The other kinds are untouched: quoted literal kinds and object keys still read.
            ("'a' | \"it's\"", KUnion((KLiteral("'a'"), KLiteral('"it\'s"')))),
            ("{ 'c d': string }", KObject((("'c d'", KName("string")),))),
            ("record<`weird-name` | b>", KName("record", ("`weird-name`", "b"))),
        ],
    )
    def test_every_other_argument_reads_as_before(self, text: str, tree: object) -> None:
        assert parse_kind(text) == tree

    def test_a_free_text_argument_renders_back(self) -> None:
        assert render_kind(parse_kind("file<bucket>")) == "file<bucket>"
        assert render_kind(parse_kind("option<file<bucket>>")) == "option<file<bucket>>"


# --- Task 2: the pure renderers (spec §5.1, §5.2, §5.6 E12–E13, F1–F9; AC-3) --------------------------------------

_AUDIT_THEN = "CREATE ev_log SET kind = $event, rid = $value.id"

#: ``(event, ddl() keyword arguments, table, exact statement)`` — the event rows of spec §5.2, then a few more.
_EVENT_GOLDENS = [
    pytest.param(
        Event("ev_audit", then=_AUDIT_THEN),
        {},
        "ev_person",
        "DEFINE EVENT OVERWRITE `ev_audit` ON `ev_person` THEN { CREATE ev_log SET kind = $event, rid = $value.id };",
        id="ev_audit",
    ),
    pytest.param(
        Event(
            "ev_rename",
            when="$event = 'UPDATE' AND $before.name != $after.name",
            then="CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name",
            comment="it's renamed",
        ),
        {},
        "ev_person",
        "DEFINE EVENT OVERWRITE `ev_rename` ON `ev_person` WHEN $event = 'UPDATE' AND $before.name != $after.name "
        "THEN { CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name } COMMENT 'it\\'s renamed';",
        id="ev_rename",
    ),
    pytest.param(
        Event("ev_guard", when="$after.name = 'forbidden'", then="THROW 'name refused: ' + $after.name"),
        {},
        "ev_person",
        "DEFINE EVENT OVERWRITE `ev_guard` ON `ev_person` WHEN $after.name = 'forbidden' "
        "THEN { THROW 'name refused: ' + $after.name };",
        id="ev_guard",
    ),
    pytest.param(
        Event(
            "ev_upper",
            when="$event = 'CREATE'",
            then="LET $n = string::uppercase($after.name); CREATE ev_log SET kind = 'upper', name = $n",
        ),
        {},
        "ev_person",
        "DEFINE EVENT OVERWRITE `ev_upper` ON `ev_person` WHEN $event = 'CREATE' "
        "THEN { LET $n = string::uppercase($after.name); CREATE ev_log SET kind = 'upper', name = $n };",
        id="ev_upper",
    ),
    pytest.param(
        Event(
            "ev_async",
            when="$event = 'CREATE'",
            then="CREATE ev_log SET kind = 'async', rid = $value.id",
            async_mode=True,
            retry=2,
        ),
        {},
        "ev_person",
        "DEFINE EVENT OVERWRITE `ev_async` ON `ev_person` ASYNC RETRY 2 WHEN $event = 'CREATE' "
        "THEN { CREATE ev_log SET kind = 'async', rid = $value.id };",
        id="ev_async",
    ),
    pytest.param(
        Event("e", then="NONE", async_mode=True), {}, "t", "DEFINE EVENT OVERWRITE `e` ON `t` ASYNC THEN { NONE };", id="async"
    ),
    pytest.param(
        Event("e", then="NONE", async_mode=True, retry=0, max_depth=65535),
        {},
        "t",
        "DEFINE EVENT OVERWRITE `e` ON `t` ASYNC RETRY 0 MAXDEPTH 65535 THEN { NONE };",
        id="async-bounds",
    ),
    pytest.param(
        Event("ev_audit", then=_AUDIT_THEN),
        {"overwrite": False},
        "ev_person",
        "DEFINE EVENT IF NOT EXISTS `ev_audit` ON `ev_person` THEN { CREATE ev_log SET kind = $event, rid = $value.id };",
        id="if-not-exists",
    ),
    pytest.param(
        Event("update", then="NONE"), {}, "select", "DEFINE EVENT OVERWRITE `update` ON `select` THEN { NONE };", id="keywords"
    ),
    pytest.param(
        # One backslash and a real newline in the Python literal: two backslashes in the statement, the double quotes
        # unescaped, the newline kept raw.
        Event("e", then="NONE", comment='a\\b "c"\nd'),
        {},
        "t",
        "DEFINE EVENT OVERWRITE `e` ON `t` THEN { NONE } COMMENT 'a\\\\b \"c\"\nd';",
        id="comment",
    ),
    # Beyond §5.2: MAXDEPTH alone, every clause at once, a then= that already is a block, stripped fragments.
    pytest.param(
        Event("e", then="NONE", async_mode=True, max_depth=3),
        {},
        "t",
        "DEFINE EVENT OVERWRITE `e` ON `t` ASYNC MAXDEPTH 3 THEN { NONE };",
        id="async-max-depth",
    ),
    pytest.param(
        Event("e", when="true", then="NONE", async_mode=True, retry=1, max_depth=3, comment="c"),
        {"overwrite": True},
        "t",
        "DEFINE EVENT OVERWRITE `e` ON `t` ASYNC RETRY 1 MAXDEPTH 3 WHEN true THEN { NONE } COMMENT 'c';",
        id="every-clause",
    ),
    pytest.param(
        Event("e", then="{ CREATE log }"), {}, "t", "DEFINE EVENT OVERWRITE `e` ON `t` THEN { { CREATE log } };", id="block"
    ),
    pytest.param(
        Event("e", when="\n $event = 'DELETE' ", then="  CREATE log  \n"),
        {},
        "t",
        "DEFINE EVENT OVERWRITE `e` ON `t` WHEN $event = 'DELETE' THEN { CREATE log };",
        id="stripped",
    ),
    pytest.param(
        Event("e", then="NONE", comment=""),
        {},
        "t",
        "DEFINE EVENT OVERWRITE `e` ON `t` THEN { NONE } COMMENT '';",
        id="empty-comment",
    ),
]

_GREET = "fn::ev_greet"

#: ``(args, kwargs, exact statement)`` — the function rows of spec §5.2, then a few more.
_FUNCTION_GOLDENS = [
    pytest.param(
        (_GREET, "RETURN string::concat('Hello ', $name);"),
        {"parameters": {"name": "string", "times": "option<int>"}, "returns": "string", "comment": "Greets"},
        "DEFINE FUNCTION OVERWRITE fn::ev_greet($name: string, $times: option<int>) -> string "
        "{ RETURN string::concat('Hello ', $name); } COMMENT 'Greets';",
        id="ev_greet",
    ),
    pytest.param(
        ("ev_guarded", "RETURN 1;"),
        {"permissions": "where $auth.admin = true"},
        "DEFINE FUNCTION OVERWRITE fn::ev_guarded() { RETURN 1; } PERMISSIONS WHERE $auth.admin = true;",
        id="ev_guarded",
    ),
    pytest.param(
        ("fn::ev_obj", "RETURN $a;"),
        {"parameters": {"a": "{ x: int }"}, "returns": "{ x: int }", "permissions": "none", "overwrite": False},
        "DEFINE FUNCTION IF NOT EXISTS fn::ev_obj($a: { x: int }) -> { x: int } { RETURN $a; } PERMISSIONS NONE;",
        id="ev_obj",
    ),
    # Beyond §5.2.
    pytest.param(("greet", "RETURN 1"), {}, "DEFINE FUNCTION OVERWRITE fn::greet() { RETURN 1 };", id="no-parameters"),
    pytest.param(
        ("fn::x", "RETURN 1"), {"parameters": {}}, "DEFINE FUNCTION OVERWRITE fn::x() { RETURN 1 };", id="empty-mapping"
    ),
    pytest.param(
        ("fn::billing::total", "\n  RETURN $a + $b;  \n"),
        {"parameters": {"a": "int", "b": "int"}, "returns": "int"},
        "DEFINE FUNCTION OVERWRITE fn::billing::total($a: int, $b: int) -> int { RETURN $a + $b; };",
        id="path-and-stripped-body",
    ),
    pytest.param(
        ("fn::x", "LET $y = $a; RETURN $y;"),
        {"parameters": {"a": "int"}},
        "DEFINE FUNCTION OVERWRITE fn::x($a: int) { LET $y = $a; RETURN $y; };",
        id="several-statements",
    ),
    pytest.param(
        ("fn::x", "RETURN 1"),
        {"permissions": "full", "comment": "it's"},
        "DEFINE FUNCTION OVERWRITE fn::x() { RETURN 1 } COMMENT 'it\\'s' PERMISSIONS FULL;",
        id="comment-then-permissions",
    ),
    pytest.param(
        ("fn::x", "RETURN 1"),
        {"permissions": " NONE "},
        "DEFINE FUNCTION OVERWRITE fn::x() { RETURN 1 } PERMISSIONS NONE;",
        id="none",
    ),
    pytest.param(
        ("fn::x", "RETURN 1"),
        {"permissions": "  WhErE\n  $auth.id = $a  "},
        "DEFINE FUNCTION OVERWRITE fn::x() { RETURN 1 } PERMISSIONS WHERE $auth.id = $a;",
        id="where-any-case",
    ),
]


def _fn_error(reason: str, what: str = "body=") -> str:
    """F3's text for ``fn::x``."""
    return f"define_function('fn::x'): {what} is not SurrealQL the ORM can embed: {reason}."


def _kind_error(what: str) -> str:
    """F7's text for ``fn::x``."""
    return f"define_function('fn::x'): {what} is not a SurrealQL kind the ORM can read: {_R_KIND}."


_F5 = "define_function('fn::x'): invalid parameter name {!r}: an identifier is expected, without the '$'."
_F6 = (
    "define_function('fn::x'): the parameter name {!r} is one SurrealDB defines itself "
    "($access, $auth, $parent, $session, $this, $token); choose another."
)
_F8 = "define_function('fn::x'): permissions= must be 'FULL', 'NONE' or 'WHERE <condition>', got {!r}."
_F9 = "define_function('fn::x'): the permissions condition is one expression; it holds a ';' outside brackets."
_INVALID_FUNCTION = (
    "Invalid stored function name: {!r}. Expected 'fn::name' or 'fn::namespace::name', where each segment is an "
    "identifier ([A-Za-z_][A-Za-z0-9_]*)."
)

#: ``(args, kwargs, exception class, exact text)`` — one case per refusal of spec §5.6 F1–F9 (and E11, E13).
_FUNCTION_ERRORS = [
    # F1 — normalize_function_name()'s texts, unchanged
    pytest.param(("fn::x;y", "1"), {}, ValueError, _INVALID_FUNCTION.format("fn::x;y"), id="F1-semicolon"),
    pytest.param(("a b", "1"), {}, ValueError, _INVALID_FUNCTION.format("a b"), id="F1-space"),
    pytest.param(("fn::", "1"), {}, ValueError, _INVALID_FUNCTION.format("fn::"), id="F1-prefix-only"),
    pytest.param(("", "1"), {}, ValueError, "Function name cannot be empty.", id="F1-empty"),
    pytest.param((1, "1"), {}, TypeError, "Function name must be a string, got 'int'.", id="F1-int"),
    # F2
    pytest.param(("x", 1), {}, TypeError, "define_function('fn::x'): body= must be a str, got int.", id="F2-type"),
    *[
        pytest.param(
            ("x", body),
            {},
            ValueError,
            "define_function('fn::x'): body= is empty; it is the SurrealQL the function runs.",
            id=f"F2-empty-{body!r}",
        )
        for body in ["", "  ", "\n"]
    ],
    # F3 — body= and the permissions condition: brackets, strings, comment markers, NUL
    pytest.param(("x", "RETURN (1"), {}, ValueError, _fn_error(_R_BRACKETS), id="F3-body-brackets"),
    pytest.param(("x", "RETURN 'a"), {}, ValueError, _fn_error(_R_STRING), id="F3-body-string"),
    pytest.param(("x", "RETURN 1 -- x"), {}, ValueError, _fn_error(_R_COMMENT), id="F3-body-comment"),
    pytest.param(("x", "RETURN 1 /* x */"), {}, ValueError, _fn_error(_R_COMMENT), id="F3-body-block-comment"),
    pytest.param(("x", "RETURN\x001"), {}, ValueError, _fn_error(_R_NUL), id="F3-body-nul"),
    pytest.param(
        ("x", "RETURN 1"),
        {"permissions": "WHERE (a"},
        ValueError,
        _fn_error(_R_BRACKETS, "the permissions condition"),
        id="F3-permissions-brackets",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"permissions": "WHERE $a # y"},
        ValueError,
        _fn_error(_R_COMMENT, "the permissions condition"),
        id="F3-permissions-comment",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"permissions": "WHERE $a // y"},
        ValueError,
        _fn_error(_R_COMMENT, "the permissions condition"),
        id="F3-permissions-line-comment",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"permissions": "WHERE $a = 1\x00"},
        ValueError,
        _fn_error(_R_NUL, "the permissions condition"),
        id="F3-permissions-nul",
    ),
    # F4
    pytest.param(
        ("x", "RETURN 1"),
        {"parameters": ["a"]},
        TypeError,
        "define_function('fn::x'): parameters= must be a mapping of parameter name to SurrealQL kind, got list.",
        id="F4-parameters",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"parameters": "a"},
        TypeError,
        "define_function('fn::x'): parameters= must be a mapping of parameter name to SurrealQL kind, got str.",
        id="F4-parameters-str",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"parameters": {"a": 1}},
        TypeError,
        "define_function('fn::x'): the kind of parameter 'a' must be a str, got int.",
        id="F4-kind",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"parameters": {"a": None}},
        TypeError,
        "define_function('fn::x'): the kind of parameter 'a' must be a str, got NoneType.",
        id="F4-kind-none",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"returns": 1},
        TypeError,
        "define_function('fn::x'): returns= must be a str or None, got int.",
        id="F4-returns",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"permissions": 1},
        TypeError,
        "define_function('fn::x'): permissions= must be a str or None, got int.",
        id="F4-permissions",
    ),
    # F5
    *[
        pytest.param(("x", "RETURN 1"), {"parameters": {name: "int"}}, ValueError, _F5.format(name), id=f"F5-{name!r}")
        for name in ["$a", "a b", "a;b", "1a", "a\n", "", "a-b"]
    ],
    pytest.param(("x", "RETURN 1"), {"parameters": {1: "int"}}, ValueError, _F5.format(1), id="F5-int"),
    # F6
    *[
        pytest.param(("x", "RETURN 1"), {"parameters": {name: "int"}}, ValueError, _F6.format(name), id=f"F6-{name}")
        for name in ["access", "auth", "session", "token", "this", "parent"]
    ],
    # F7 — unreadable kinds, the hardened free-text argument included
    *[
        pytest.param(
            ("x", "RETURN 1"),
            {"parameters": {"a": kind}},
            ValueError,
            _kind_error("the kind of parameter 'a'"),
            id=f"F7-parameter-{kind!r}",
        )
        for kind in [
            "int>",
            "",
            "  ",
            "int; x",
            "file<x -- y>",
            "file<x # y>",
            "file<'z'>",
            "file<x//y>",
            "file<x; y>",
            "option<file<a;b>>",
        ]
    ],
    *[
        pytest.param(("x", "RETURN 1"), {"returns": kind}, ValueError, _kind_error("returns="), id=f"F7-returns-{kind!r}")
        for kind in ["array<", 'file<"q">', "file<a/*b>", "", "file<x; y>"]
    ],
    # F8
    *[
        pytest.param(("x", "RETURN 1"), {"permissions": value}, ValueError, _F8.format(value), id=f"F8-{value!r}")
        for value in ["ALL", "WHERE", "where  ", "WHEREx", "FULL NONE", "", "  "]
    ],
    # F9
    pytest.param(("x", "RETURN 1"), {"permissions": "WHERE a; b"}, ValueError, _F9, id="F9"),
    pytest.param(("x", "RETURN 1"), {"permissions": "WHERE $a; $b"}, ValueError, _F9, id="F9-variables"),
    # E11 and the comment's type, with define_function's subject
    pytest.param(
        ("x", "RETURN 1"),
        {"comment": "\x00"},
        ValueError,
        "define_function('fn::x'): comment= holds a NUL character (\\x00), which SurrealQL cannot carry.",
        id="E11",
    ),
    pytest.param(
        ("x", "RETURN 1"),
        {"comment": 1},
        TypeError,
        "define_function('fn::x'): comment= must be a str, got int.",
        id="comment-type",
    ),
    # E13
    pytest.param(("x", "RETURN 1"), {"overwrite": 1}, TypeError, "overwrite= must be a bool, got int.", id="E13"),
    pytest.param(("x", "RETURN 1"), {"overwrite": None}, TypeError, "overwrite= must be a bool, got NoneType.", id="E13-none"),
]


class TestEventDdl:
    """AC-3: ``Event.ddl()`` and ``remove_event_statement()`` (spec §5.1, §5.2, §5.6 E12–E13)."""

    @pytest.mark.parametrize(("event", "kwargs", "table", "statement"), _EVENT_GOLDENS)
    def test_golden_statements(self, event: Event, kwargs: dict[str, Any], table: str, statement: str) -> None:
        assert event.ddl(table, **kwargs) == statement

    def test_deterministic(self) -> None:
        event = Event("e", when="true", then="NONE", async_mode=True, retry=1, max_depth=3, comment="c")
        assert event.ddl("t") == event.ddl("t")
        assert event.ddl("t", overwrite=False) == event.ddl("t", overwrite=False)
        assert Event("e", then="NONE").ddl("t") == Event("e", then="NONE").ddl("t")

    def test_overwrite_is_keyword_only(self) -> None:
        ddl: Any = Event("e", then="NONE").ddl
        with pytest.raises(TypeError):
            ddl("t", False)

    @pytest.mark.parametrize(
        ("table", "kwargs", "error", "text"),
        [
            # E12
            (1, {}, TypeError, "Table name must be a str, got int."),
            (None, {}, TypeError, "Table name must be a str, got NoneType."),
            *[
                (table, {}, ValueError, f"Invalid table name {table!r}: {_IDENTIFIER_EXPECTED}")
                for table in ["a b", "select;", "", "a\n", "a`b", "1a", "a-b"]
            ],
            # E13
            ("t", {"overwrite": 1}, TypeError, "overwrite= must be a bool, got int."),
            ("t", {"overwrite": "yes"}, TypeError, "overwrite= must be a bool, got str."),
            ("t", {"overwrite": None}, TypeError, "overwrite= must be a bool, got NoneType."),
            # The table first.
            ("a b", {"overwrite": 1}, ValueError, f"Invalid table name 'a b': {_IDENTIFIER_EXPECTED}"),
        ],
    )
    def test_refusals(self, table: Any, kwargs: dict[str, Any], error: type[Exception], text: str) -> None:
        ddl: Any = Event("e", then="NONE").ddl
        with pytest.raises(error) as raised:
            ddl(table, **kwargs)
        assert type(raised.value) is error
        assert str(raised.value) == text

    # --- removal ------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("missing_ok", "statement"),
        [(True, "REMOVE EVENT IF EXISTS `ev_audit` ON `ev_person`;"), (False, "REMOVE EVENT `ev_audit` ON `ev_person`;")],
    )
    def test_removal_statement(self, missing_ok: bool, statement: str) -> None:
        assert remove_event_statement("ev_audit", "ev_person", missing_ok) == statement

    def test_removal_statement_quotes_keyword_names(self) -> None:
        assert remove_event_statement("update", "select", True) == "REMOVE EVENT IF EXISTS `update` ON `select`;"

    @pytest.mark.parametrize(
        ("args", "error", "text"),
        [
            # E1, E2
            ((1, "t", True), TypeError, "Event name must be a str, got int."),
            *[
                ((name, "t", True), ValueError, f"Invalid event name {name!r}: {_IDENTIFIER_EXPECTED}")
                for name in ["", "a b", "a;b", "a\n", "a`b"]
            ],
            # E12
            (("e", 1, True), TypeError, "Table name must be a str, got int."),
            *[
                (("e", table, True), ValueError, f"Invalid table name {table!r}: {_IDENTIFIER_EXPECTED}")
                for table in ["", "a b", "a;b", "a\n", "a`b"]
            ],
            # E13
            (("e", "t", "yes"), TypeError, "missing_ok= must be a bool, got str."),
            (("e", "t", 1), TypeError, "missing_ok= must be a bool, got int."),
            (("e", "t", None), TypeError, "missing_ok= must be a bool, got NoneType."),
        ],
    )
    def test_removal_refusals(self, args: tuple[Any, ...], error: type[Exception], text: str) -> None:
        render: Any = remove_event_statement
        with pytest.raises(error) as raised:
            render(*args)
        assert type(raised.value) is error
        assert str(raised.value) == text

    async def test_works_with_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert (
            Event("ev_audit", then=_AUDIT_THEN)
            .ddl("ev_person", overwrite=False)
            .startswith("DEFINE EVENT IF NOT EXISTS `ev_audit` ON `ev_person` THEN {")
        )
        assert remove_event_statement("ev_audit", "ev_person", False) == "REMOVE EVENT `ev_audit` ON `ev_person`;"


class TestFunctionDdl:
    """AC-3: ``function_ddl()`` and ``remove_function_statement()`` (spec §5.2, §5.6 F1–F9, E11, E13)."""

    @pytest.mark.parametrize(("args", "kwargs", "statement"), _FUNCTION_GOLDENS)
    def test_golden_statements(self, args: tuple[Any, ...], kwargs: dict[str, Any], statement: str) -> None:
        assert function_ddl(*args, **kwargs) == statement

    def test_deterministic(self) -> None:
        kwargs: dict[str, Any] = {
            "parameters": {"name": "string", "times": "option<int>"},
            "returns": "string",
            "permissions": "WHERE $auth.admin = true",
            "comment": "Greets",
        }
        assert function_ddl(_GREET, "RETURN $name;", **kwargs) == function_ddl(_GREET, "RETURN $name;", **kwargs)

    def test_the_name_is_normalised(self) -> None:
        assert function_ddl("greet", "RETURN 1") == function_ddl("fn::greet", "RETURN 1")
        assert function_ddl(" fn::greet ", "RETURN 1") == "DEFINE FUNCTION OVERWRITE fn::greet() { RETURN 1 };"

    def test_parameters_keep_their_order(self) -> None:
        assert (
            function_ddl("fn::x", "RETURN 1", parameters={"times": "option<int>", "name": "string"})
            == "DEFINE FUNCTION OVERWRITE fn::x($times: option<int>, $name: string) { RETURN 1 };"
        )

    def test_only_body_and_function_are_positional(self) -> None:
        render: Any = function_ddl
        with pytest.raises(TypeError):
            render("fn::x", "RETURN 1", {"a": "int"})

    # --- kinds: canonical form and the P1 round trip --------------------------------------------------------------

    @pytest.mark.parametrize("kind", ["none | int", "int | none", "option<int>", " option< int > ", "none|int"])
    def test_a_kind_is_written_in_canonical_form(self, kind: str) -> None:
        assert (
            function_ddl("fn::x", "RETURN $a", parameters={"a": kind}, returns=kind)
            == "DEFINE FUNCTION OVERWRITE fn::x($a: option<int>) -> option<int> { RETURN $a };"
        )

    @pytest.mark.parametrize(
        ("kind", "canonical"),
        [("{x:int}", "{ x: int }"), ("record<a|b>", "record<a | b>"), ("array<string,10>", "array<string, 10>")],
    )
    def test_other_spellings_are_normalised(self, kind: str, canonical: str) -> None:
        assert function_ddl("fn::x", "RETURN $a", parameters={"a": kind}) == (
            f"DEFINE FUNCTION OVERWRITE fn::x($a: {canonical}) {{ RETURN $a }};"
        )

    @pytest.mark.parametrize("kind", ["string", "int", "option<int>", "{ x: int }"])
    def test_the_golden_kinds_round_trip_unchanged(self, kind: str) -> None:
        # P1: every kind of the spec goldens is written exactly as given, as a parameter kind and as returns=.
        assert function_ddl("fn::x", "RETURN $a", parameters={"a": kind}, returns=kind) == (
            f"DEFINE FUNCTION OVERWRITE fn::x($a: {kind}) -> {kind} {{ RETURN $a }};"
        )

    # --- refusals -----------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(("args", "kwargs", "error", "text"), _FUNCTION_ERRORS)
    def test_refusals(self, args: tuple[Any, ...], kwargs: dict[str, Any], error: type[Exception], text: str) -> None:
        render: Any = function_ddl
        with pytest.raises(error) as raised:
            render(*args, **kwargs)
        assert type(raised.value) is error
        assert str(raised.value) == text

    def test_messages_name_the_normalised_function(self) -> None:
        with pytest.raises(ValueError) as raised:
            function_ddl("billing::total", "  ")
        assert (
            str(raised.value) == "define_function('fn::billing::total'): body= is empty; it is the SurrealQL the function runs."
        )

    def test_a_comment_marker_inside_a_literal_is_data(self) -> None:
        assert (
            function_ddl("fn::x", "RETURN '-- # //'", permissions="WHERE $a = '/* x */'")
            == "DEFINE FUNCTION OVERWRITE fn::x() { RETURN '-- # //' } PERMISSIONS WHERE $a = '/* x */';"
        )

    # --- removal ------------------------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("function", "missing_ok", "statement"),
        [
            (_GREET, True, "REMOVE FUNCTION IF EXISTS fn::ev_greet;"),
            (_GREET, False, "REMOVE FUNCTION fn::ev_greet;"),
            ("greet", True, "REMOVE FUNCTION IF EXISTS fn::greet;"),
            ("billing::total", False, "REMOVE FUNCTION fn::billing::total;"),
        ],
    )
    def test_removal_statement(self, function: str, missing_ok: bool, statement: str) -> None:
        assert remove_function_statement(function, missing_ok) == statement

    @pytest.mark.parametrize(
        ("args", "error", "text"),
        [
            (("fn::x;y", True), ValueError, _INVALID_FUNCTION.format("fn::x;y")),
            ((1, True), TypeError, "Function name must be a string, got 'int'."),
            (("fn::x", "yes"), TypeError, "missing_ok= must be a bool, got str."),
            (("fn::x", 0), TypeError, "missing_ok= must be a bool, got int."),
        ],
    )
    def test_removal_refusals(self, args: tuple[Any, ...], error: type[Exception], text: str) -> None:
        render: Any = remove_function_statement
        with pytest.raises(error) as raised:
            render(*args)
        assert type(raised.value) is error
        assert str(raised.value) == text

    async def test_works_with_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert (
            function_ddl(_GREET, "RETURN $name;", parameters={"name": "string"}, returns="string")
            == "DEFINE FUNCTION OVERWRITE fn::ev_greet($name: string) -> string { RETURN $name; };"
        )
        assert remove_function_statement(_GREET, True) == "REMOVE FUNCTION IF EXISTS fn::ev_greet;"

    # --- export and docstring -----------------------------------------------------------------------------------

    def test_function_ddl_is_exported(self) -> None:
        from surreal_orm_lite import function_ddl as exported

        assert exported is schema.function_ddl
        assert "function_ddl" in surreal_orm_lite.__all__
        assert schema.__all__ == ["Event", "function_ddl"]

    def test_the_docstring_says_what_the_check_is_and_how_kinds_are_written(self) -> None:
        text = " ".join((function_ddl.__doc__ or "").split())
        assert "catches mistakes" in text
        assert "not an injection defence" in text
        assert "expressions are trusted application code, never user input" in text
        assert "canonical form" in text
        assert "cannot inject" not in text
        assert "cannot close" not in text


# --- AC-4: events on the model (spec §5.3, §5.6 E14) ---------------------------------------------------------------

_MD_RENAME = Event(
    "ev_rename",
    when="$event = 'UPDATE' AND $before.name != $after.name",
    then="CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name",
)
_MD_AUDIT = Event("ev_audit", then=_AUDIT_THEN)


class EvMdPerson(BaseSurrealModel):
    """Two events, declared rename first: ``get_events()`` keeps that order (not sorted by name)."""

    model_config = SurrealConfigDict(table_name="ev_md_person", events=[_MD_RENAME, _MD_AUDIT])

    name: str


class EvMdArchived(EvMdPerson):
    """Another table, no ``events`` of its own: it inherits the parent's (D16)."""

    model_config = SurrealConfigDict(table_name="ev_md_archived")


class EvMdQuiet(EvMdPerson):
    """``events=[]`` overrides the inherited events: none."""

    model_config = SurrealConfigDict(table_name="ev_md_quiet", events=[])


class EvMdTuple(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ev_md_tuple", events=(_MD_AUDIT,))


class EvMdPlain(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ev_md_plain")


class EvMdNone(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ev_md_none", events=None)


class EvMdFollows(BaseSurrealEdge):
    """An edge model: its events fire on the edge table."""

    model_config = SurrealConfigDict(table_name="ev_md_follows", events=[_MD_AUDIT])


class EvMdUnrouted(BaseSurrealModel):
    """No events, on a connection that is never registered: reaching it would raise."""

    model_config = SurrealConfigDict(table_name="ev_md_unrouted", connection="ev_md_unregistered")


class TestModelEvents:
    """AC-4: ``SurrealConfigDict(events=)``, ``get_events()``, ``event_ddl()`` and E14 (spec §5.3, §5.6)."""

    @pytest.mark.parametrize("model", [EvMdUnrouted, EvMdPlain, EvMdQuiet])
    async def test_define_events_without_events_sends_nothing(self, model: type[BaseSurrealModel]) -> None:
        # No statement, no request: the connection is not even resolved (EvMdUnrouted's is not registered, and no
        # default connection is configured).
        await SurrealDBConnectionManager.unset_connection()
        assert await model.define_events() == []
        assert await model.define_events(overwrite=False) == []

    def test_get_events_is_a_tuple_in_declaration_order(self) -> None:
        events = EvMdPerson.get_events()
        assert type(events) is tuple
        assert events == (_MD_RENAME, _MD_AUDIT)
        assert events[0] is _MD_RENAME
        assert events[1] is _MD_AUDIT

    def test_event_ddl_renders_every_event_on_the_model_table(self) -> None:
        assert EvMdPerson.event_ddl() == [event.ddl("ev_md_person") for event in (_MD_RENAME, _MD_AUDIT)]
        assert EvMdPerson.event_ddl() == [
            "DEFINE EVENT OVERWRITE `ev_rename` ON `ev_md_person` WHEN $event = 'UPDATE' AND $before.name != "
            "$after.name THEN { CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name };",
            "DEFINE EVENT OVERWRITE `ev_audit` ON `ev_md_person` THEN { CREATE ev_log SET kind = $event, rid = $value.id };",
        ]

    def test_event_ddl_without_overwrite_keeps_what_exists(self) -> None:
        statements = EvMdPerson.event_ddl(overwrite=False)
        assert statements == [event.ddl("ev_md_person", overwrite=False) for event in (_MD_RENAME, _MD_AUDIT)]
        assert all(statement.startswith("DEFINE EVENT IF NOT EXISTS `") for statement in statements)

    def test_overwrite_is_keyword_only(self) -> None:
        event_ddl: Any = EvMdPerson.event_ddl
        with pytest.raises(TypeError):
            event_ddl(False)

    def test_event_ddl_is_deterministic(self) -> None:
        assert EvMdPerson.event_ddl() == EvMdPerson.event_ddl()

    @pytest.mark.parametrize("model", [EvMdPlain, EvMdNone], ids=["no-key", "None"])
    def test_a_model_without_events(self, model: type[BaseSurrealModel]) -> None:
        assert model.get_events() == ()
        assert type(model.get_events()) is tuple
        assert model.event_ddl() == []
        assert model.event_ddl(overwrite=False) == []

    def test_a_tuple_is_accepted(self) -> None:
        assert EvMdTuple.get_events() == (_MD_AUDIT,)
        assert EvMdTuple.event_ddl() == [_MD_AUDIT.ddl("ev_md_tuple")]

    def test_a_subclass_inherits_the_events_on_its_own_table(self) -> None:
        assert EvMdArchived.get_events() == EvMdPerson.get_events()
        assert EvMdArchived.event_ddl() == [event.ddl("ev_md_archived") for event in (_MD_RENAME, _MD_AUDIT)]
        # The parent still renders on its own table.
        assert EvMdPerson.event_ddl() == [event.ddl("ev_md_person") for event in (_MD_RENAME, _MD_AUDIT)]

    def test_a_subclass_can_drop_the_inherited_events(self) -> None:
        assert EvMdQuiet.get_events() == ()
        assert EvMdQuiet.event_ddl() == []
        assert EvMdPerson.get_events() == (_MD_RENAME, _MD_AUDIT)

    def test_an_edge_model_renders_on_its_edge_table(self) -> None:
        assert EvMdFollows.get_events() == (_MD_AUDIT,)
        assert EvMdFollows.event_ddl() == [
            "DEFINE EVENT OVERWRITE `ev_audit` ON `ev_md_follows` THEN { CREATE ev_log SET kind = $event, rid = $value.id };"
        ]

    async def test_works_with_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert EvMdPerson.get_events() == (_MD_RENAME, _MD_AUDIT)
        assert EvMdPerson.event_ddl() == [event.ddl("ev_md_person") for event in (_MD_RENAME, _MD_AUDIT)]

    # --- E14, at the class statement --------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("events", "error", "text"),
        [
            ("x", TypeError, "EvMdBad: model_config['events'] must be a list or tuple of Event, got str."),
            (_MD_AUDIT, TypeError, "EvMdBad: model_config['events'] must be a list or tuple of Event, got Event."),
            ({_MD_AUDIT}, TypeError, "EvMdBad: model_config['events'] must be a list or tuple of Event, got set."),
            ([1], TypeError, "EvMdBad: model_config['events'] holds a int, not an Event."),
            ((_MD_AUDIT, "e"), TypeError, "EvMdBad: model_config['events'] holds a str, not an Event."),
            (
                [Event("a", then="NONE"), Event("a", when="true", then="NONE")],
                ValueError,
                "EvMdBad: model_config['events'] declares the event 'a' more than once.",
            ),
            (
                (_MD_AUDIT, _MD_RENAME, _MD_AUDIT),
                ValueError,
                "EvMdBad: model_config['events'] declares the event 'ev_audit' more than once.",
            ),
        ],
        ids=["str", "one-event", "set", "int-item", "str-item", "duplicate-name", "duplicate-event"],
    )
    def test_e14_at_the_class_statement(self, events: Any, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as raised:

            class EvMdBad(BaseSurrealModel):
                model_config = SurrealConfigDict(table_name="ev_md_bad", events=events)

        assert type(raised.value) is error
        assert str(raised.value) == text

    def test_e14_names_the_subclass(self) -> None:
        with pytest.raises(TypeError) as raised:

            class EvMdBadChild(EvMdPerson):
                model_config = SurrealConfigDict(events=[_MD_AUDIT, 1])  # type: ignore[list-item]

        assert str(raised.value) == "EvMdBadChild: model_config['events'] holds a int, not an Event."

    def test_e14_on_an_edge_model(self) -> None:
        with pytest.raises(ValueError) as raised:

            class EvMdBadEdge(BaseSurrealEdge):
                model_config = SurrealConfigDict(table_name="ev_md_bad_edge", events=[_MD_AUDIT, _MD_AUDIT])

        assert str(raised.value) == "EvMdBadEdge: model_config['events'] declares the event 'ev_audit' more than once."

    def test_a_bad_event_fails_at_the_class_statement(self) -> None:
        with pytest.raises(ValueError) as raised:

            class EvMdBadEvent(BaseSurrealModel):
                model_config = SurrealConfigDict(events=[Event("e", then="(1")])

        assert str(raised.value) == _embed_error("then", _R_BRACKETS)


# --- AC-13 (unit): applying DDL — refusals classified, the transaction check, the ASYNC check (spec §5.6, §6.4, §6.5) -

_DEFINE = "DEFINE EVENT OVERWRITE `e` ON `t` THEN { CREATE log };"
_REMOVE = "REMOVE EVENT `e` ON `t`;"
_PERMISSIONS = "IAM error: Not enough permissions to perform this action"
_MISSING = "The event 'e' does not exist"
_ACCESS = "DEFINE ACCESS OVERWRITE a ON DATABASE TYPE RECORD;"


class _FakeClient:
    """A client whose ``query()`` records each call and raises *error* when one is given, and whose ``version()``
    answers *version* and counts its calls."""

    def __init__(self, error: BaseException | None = None, version: object = "surrealdb-3.3.4") -> None:
        self.error = error
        self.answer = version
        self.queries: list[tuple[str, object]] = []
        self.version_calls = 0

    async def query(self, statement: str, variables: object) -> list[object]:
        self.queries.append((statement, variables))
        if self.error is not None:
            raise self.error
        return []

    async def version(self) -> object:
        self.version_calls += 1
        return self.answer


class _FakeTransaction:
    """A transaction whose ``add()`` records each statement and raises *error* when one is given, as an interactive
    transaction's ``add()`` raises the ORM's own error once it has read the response."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.added: list[tuple[str, object]] = []

    async def add(self, statement: str, variables: object) -> None:
        self.added.append((statement, variables))
        if self.error is not None:
            raise self.error


class _TaggedTransaction:
    """A transaction opened on the connection *name*, as the manager tags one (``_connection_name``)."""

    def __init__(self, name: str) -> None:
        self._connection_name = name


async def _apply(
    statements: list[str], error: BaseException, *, through_tx: bool = False, **options: Any
) -> pytest.ExceptionInfo[BaseException]:
    """Apply *statements* with a client (or a transaction) that raises *error*; the raised exception."""
    client = _FakeClient(None if through_tx else error)
    tx = _FakeTransaction(error) if through_tx else None
    with pytest.raises(BaseException) as raised:
        await apply_ddl_statements(statements, client=client, tx=tx, **options)
    return raised


class TestApplyClassification:
    """Spec §6.4, §5.6 S1–S3: ``apply_ddl_statements(kind=, classify=)`` — the message names the statement, and with
    ``classify=True`` the class says why the server refused it; without the new keywords nothing changes."""

    @pytest.mark.parametrize(
        "error",
        [
            NotAllowedError("NotAllowed", _PERMISSIONS),  # SurrealDB 3.x
            InternalError("Internal", _PERMISSIONS),  # SurrealDB 2.x
            InternalError("Internal", "IAM ERROR: NOT ENOUGH PERMISSIONS TO PERFORM THIS ACTION"),
        ],
        ids=["3.x", "2.x", "upper-case"],
    )
    @pytest.mark.parametrize("statement", [_DEFINE, _REMOVE], ids=["define", "remove"])
    async def test_s1_a_permissions_refusal(self, error: Exception, statement: str) -> None:
        kind = "removal" if statement.startswith("REMOVE") else "definition"
        raised = await _apply([statement], error, what="event", kind=kind, classify=True)
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply event {kind}: {statement} -> {error}"
        assert raised.value.__cause__ is error

    async def test_s1_inside_an_interactive_transaction(self) -> None:
        error = SurrealDbError(f"Transaction failed and rolled back: {_PERMISSIONS}")
        raised = await _apply([_DEFINE], error, through_tx=True, what="event", classify=True)
        assert type(raised.value) is SurrealDbAuthenticationError
        assert str(raised.value) == f"Can't apply event definition: {_DEFINE} -> {error}"

    @pytest.mark.parametrize(
        "error",
        [InternalError("Internal", _MISSING), InternalError("Internal", "The function 'fn::greet' does not exist")],
        ids=["event", "function"],
    )
    async def test_s2_a_removal_of_a_missing_definition(self, error: Exception) -> None:
        raised = await _apply([_REMOVE], error, what="event", kind="removal", classify=True)
        assert type(raised.value) is SurrealDbNotFoundError
        assert str(raised.value) == f"Can't apply event removal: {_REMOVE} -> {error}"
        assert raised.value.__cause__ is error

    async def test_s2_is_for_a_removal_only(self) -> None:
        error = InternalError("Internal", _MISSING)
        raised = await _apply([_DEFINE], error, what="event", classify=True)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply event definition: {_DEFINE} -> {error}"

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("Parse error: Unexpected token `an identifier`, expected Eof"),  # the embedded engine
            ValidationError("Validation", "Parse error: Unexpected end of file"),  # SurrealDB 3.x
            InternalError("Internal", "The event 'e' already exists"),
            SurrealDbError("Transaction failed and rolled back: Parse error"),  # an interactive transaction
        ],
        ids=["runtime", "validation", "already-exists", "interactive"],
    )
    async def test_s3_any_other_refusal(self, error: Exception) -> None:
        raised = await _apply([_DEFINE], error, what="function", classify=True)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply function definition: {_DEFINE} -> {error}"
        assert raised.value.__cause__ is error

    @pytest.mark.parametrize("through_tx", [True, False], ids=["tx", "client"])
    async def test_a_conflict_propagates_unchanged(self, through_tx: bool) -> None:
        error = SurrealDbConflictError("Transaction conflict: This transaction can be retried")
        raised = await _apply([_DEFINE], error, through_tx=through_tx, what="event", classify=True)
        assert raised.value is error

    @pytest.mark.parametrize(
        ("statement", "error"),
        [
            (_ACCESS, NotAllowedError("NotAllowed", _PERMISSIONS)),
            (_REMOVE, InternalError("Internal", _MISSING)),
            (_ACCESS, SurrealDbConflictError("Transaction conflict: This transaction can be retried")),
            (_ACCESS, RuntimeError("Parse error")),
        ],
        ids=["permissions", "missing", "conflict", "runtime"],
    )
    async def test_without_the_new_keywords_nothing_changes(self, statement: str, error: Exception) -> None:
        raised = await _apply([statement], error, what="access")
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply access definition: {statement} -> {error}"
        assert raised.value.__cause__ is error

    async def test_the_hint_ends_the_message(self) -> None:
        error = InternalError("Internal", _MISSING)
        raised = await _apply([_REMOVE], error, what="event", kind="removal", classify=True, hint=" (a hint)")
        assert type(raised.value) is SurrealDbNotFoundError
        assert str(raised.value) == f"Can't apply event removal: {_REMOVE} -> {error} (a hint)"

    async def test_statements_are_sent_one_by_one_and_returned(self) -> None:
        client = _FakeClient()
        statements = [_DEFINE, _REMOVE]
        applied = await apply_ddl_statements(statements, client=client, what="event", kind="removal", classify=True)
        assert applied == statements
        assert client.queries == [(_DEFINE, {}), (_REMOVE, {})]

    async def test_a_transaction_queues_without_variables(self) -> None:
        client = _FakeClient()
        tx = _FakeTransaction()
        assert await apply_ddl_statements([_DEFINE], client=client, tx=tx, what="event", classify=True) == [_DEFINE]
        assert tx.added == [(_DEFINE, None)]
        assert client.queries == []

    async def test_the_first_refusal_stops_the_rest(self) -> None:
        client = _FakeClient(InternalError("Internal", "The event 'e' already exists"))
        with pytest.raises(SurrealDbError):
            await apply_ddl_statements([_DEFINE, _REMOVE], client=client, what="event", classify=True)
        assert client.queries == [(_DEFINE, {})]


#: A secret a definition holds, which no refusal may quote (review of PR #273).
_SECRET = "s3cr3t_k3y"
_HOOK = f"DEFINE EVENT OVERWRITE `hook` ON `t` THEN {{ http::post('https://hooks.example/{_SECRET}', {{}}) CREATE }};"
_HOOK_SUBJECT = "'hook' on table 't'"


def _excerpt_3x(position: str, line: str) -> str:
    """A SurrealDB 3.x parse error quoting *line* of the statement at *position* (as measured on 3.3.4)."""
    return f"Parse error: Unexpected token `}}`, expected an expression\n --> [{position}]\n  |\n1 | {line}\n  |  ^\n"


#: SurrealDB 2.x and the embedded engine: a parse error quotes the statement twice, from its start the second time.
_EXCERPT_2X = (
    "There was a problem with the database: Parse error: Unexpected token `CREATE` expected delimiter `}`\n"
    " --> [1:102]\n  |\n1 | ...k3y', {}) CREATE };\n  |              ^^^^^^ \n --> [1:42]\n  |\n"
    f"1 | DEFINE EVENT OVERWRITE `hook` ON `t` THEN {{ http::post('https://hooks.example/{_SECRET}...\n"
    "  |                                          ^ expected this delimiter to close\n"
)


def _leaks(exc: BaseException, text: str) -> list[str]:
    """Where *exc* shows *text*: its ``str()``, its ``repr()`` and its printed traceback (the chained error
    included)."""
    shown = {"str": str(exc), "repr": repr(exc), "traceback": "".join(traceback.format_exception(exc))}
    return [where for where, value in shown.items() if text in value]


class TestApplyNamesTheDefinition:
    """Review of PR #273: with ``subjects=`` (every v0.31.0 applier) a refusal names the definition — the event and
    its table, or the function — never its statement, whose ``when``, ``then`` or body may hold a secret. The
    server's reason is kept whole when it does not quote the statement; a parse error's excerpt of the statement
    is left out (its position kept), and a reason that still quotes it is dropped. Only a reason kept whole is
    chained as ``__cause__``: otherwise the server's error stays reachable as ``__context__`` but is not printed."""

    @pytest.mark.parametrize(
        ("statement", "subject", "error"),
        [
            (_HOOK, _HOOK_SUBJECT, NotAllowedError("NotAllowed", _PERMISSIONS)),
            (_HOOK, _HOOK_SUBJECT, SurrealDbError(f"Transaction failed and rolled back: {_PERMISSIONS}")),
            (
                "DEFINE EVENT `a_rather_long_event_name` ON `a_rather_long_table_name` THEN { NONE };",
                "'a_rather_long_event_name' on table 'a_rather_long_table_name'",
                InternalError("Internal", "The event 'a_rather_long_event_name' already exists"),
            ),
            (
                "REMOVE FUNCTION fn::a_rather_long::function_name;",
                "'fn::a_rather_long::function_name'",
                InternalError("Internal", "The function 'fn::a_rather_long::function_name' does not exist"),
            ),
            (_HOOK, _HOOK_SUBJECT, RuntimeError("Parse error: Unexpected token `an identifier`, expected Eof")),
            (_HOOK, _HOOK_SUBJECT, InternalError("Internal", "Unexpected token `CREATE`")),  # a short overlap
        ],
        ids=["permissions", "permissions-interactive", "already-exists", "missing", "no-excerpt", "a-keyword"],
    )
    async def test_a_reason_that_does_not_quote_the_statement_is_kept_whole(
        self, statement: str, subject: str, error: Exception
    ) -> None:
        kind = "removal" if statement.startswith("REMOVE") else "definition"
        what = "function" if "FUNCTION" in statement else "event"
        raised = await _apply([statement], error, what=what, kind=kind, classify=True, subjects=[subject])
        assert str(raised.value) == f"Can't apply {what} {kind}: {subject} -> {error}"
        assert raised.value.__cause__ is error
        assert statement not in str(raised.value)

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (
                ValidationError("Validation", _excerpt_3x("1:109", f"...{_SECRET}', {{}}) CREATE }};")),
                "Parse error: Unexpected token `}`, expected an expression (at line 1, column 109 of the statement)",
            ),
            (
                InternalError("Internal", _EXCERPT_2X),
                "There was a problem with the database: Parse error: Unexpected token `CREATE` expected delimiter `}` "
                "(at line 1, column 102 of the statement)",
            ),
            (
                RuntimeError(_EXCERPT_2X),  # the embedded engine
                "There was a problem with the database: Parse error: Unexpected token `CREATE` expected delimiter `}` "
                "(at line 1, column 102 of the statement)",
            ),
        ],
        ids=["3.x", "2.x", "embedded"],
    )
    async def test_a_parse_error_loses_its_excerpt_of_the_statement(self, error: Exception, reason: str) -> None:
        raised = await _apply([_HOOK], error, what="event", classify=True, subjects=[_HOOK_SUBJECT])
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == f"Can't apply event definition: {_HOOK_SUBJECT} -> {reason}"
        assert _SECRET in str(error)
        assert _leaks(raised.value, _SECRET) == []
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is error
        assert raised.value.__suppress_context__

    async def test_an_interactive_transactions_parse_error(self) -> None:
        # A 3.x interactive transaction's add() reads a statement the server cannot parse from the response's error
        # envelope: since #278 its error gives the server's reason and the position, not the excerpt.
        from surreal_orm_lite.transaction import Transaction

        raw = {"error": {"code": -32000, "message": _excerpt_3x("1:109", f"...{_SECRET}', {{}}) CREATE }};")}}
        with pytest.raises(SurrealDbError) as built:
            Transaction.raise_for_status(raw)
        error = built.value
        raised = await _apply([_HOOK], error, through_tx=True, what="event", classify=True, subjects=[_HOOK_SUBJECT])
        assert type(raised.value) is SurrealDbError
        message = str(raised.value)
        assert message == f"Can't apply event definition: {_HOOK_SUBJECT} -> {error}"
        assert message.endswith("(at line 1, column 109 of the statement)")
        assert _leaks(raised.value, _SECRET) == []
        assert raised.value.__cause__ is error

    @pytest.mark.parametrize(
        "error",
        [
            InternalError("Internal", f"Cannot call http::post('https://hooks.example/{_SECRET}', {{}})"),
            InternalError("Internal", f"Bad URL: https://hooks.example/{_SECRET}"),
        ],
        ids=["call", "url"],
    )
    async def test_a_reason_that_quotes_the_statement_is_dropped(self, error: Exception) -> None:
        raised = await _apply([_HOOK], error, what="event", classify=True, subjects=[_HOOK_SUBJECT])
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == (
            f"Can't apply event definition: {_HOOK_SUBJECT} -> the server's message quotes the statement, so it is left out"
        )
        assert _leaks(raised.value, _SECRET) == []
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is error

    async def test_the_class_still_follows_the_servers_text(self) -> None:
        missing = InternalError("Internal", _MISSING)
        raised = await _apply([_REMOVE], missing, what="event", kind="removal", classify=True, subjects=["'e' on table 't'"])
        assert type(raised.value) is SurrealDbNotFoundError
        assert str(raised.value) == f"Can't apply event removal: 'e' on table 't' -> {missing}"

    async def test_the_hint_still_ends_the_message(self) -> None:
        error = InternalError("Internal", _MISSING)
        raised = await _apply([_REMOVE], error, what="event", kind="removal", subjects=["'e' on table 't'"], hint=" (h)")
        assert str(raised.value) == f"Can't apply event removal: 'e' on table 't' -> {error} (h)"

    @pytest.mark.parametrize("through_tx", [True, False], ids=["tx", "client"])
    async def test_a_conflict_still_propagates_unchanged(self, through_tx: bool) -> None:
        error = SurrealDbConflictError("Transaction conflict: This transaction can be retried")
        raised = await _apply([_HOOK], error, through_tx=through_tx, what="event", classify=True, subjects=[_HOOK_SUBJECT])
        assert raised.value is error

    async def test_each_statement_has_its_subject(self) -> None:
        client = _FakeClient()
        statements = [_DEFINE, _HOOK]
        subjects = ["'e' on table 't'", _HOOK_SUBJECT]
        assert await apply_ddl_statements(statements, client=client, what="event", subjects=subjects) == statements
        assert client.queries == [(_DEFINE, {}), (_HOOK, {})]

    async def test_one_subject_per_statement_or_nothing_is_sent(self) -> None:
        client = _FakeClient()
        with pytest.raises(ValueError) as raised:
            await apply_ddl_statements([_DEFINE, _HOOK], client=client, what="event", subjects=["'e' on table 't'"])
        assert str(raised.value) == "apply_ddl_statements(): 2 statements and 1 subjects; one subject per statement."
        assert client.queries == []

    def test_event_subject(self) -> None:
        assert event_subject("email_audit", "account") == "'email_audit' on table 'account'"


class TestCallTransactionOperation:
    """Spec §6.5: ``check_call_transaction(operation=)`` names the manager method in X1m; the default is unchanged."""

    @staticmethod
    def _x1m(operation: str) -> str:
        return (
            f"{operation} cannot run in this transaction: the transaction belongs to connection 'other' and the call "
            "runs on connection 'default'. A transaction is one connection's (one database): call it inside "
            "using('other'), or open the transaction on 'default'."
        )

    @pytest.mark.parametrize("operation", ["define_event()", "remove_event()", "define_function()", "remove_function()"])
    def test_x1m_names_the_operation(self, operation: str) -> None:
        with pytest.raises(SurrealDbError) as raised:
            check_call_transaction("default", _TaggedTransaction("other"), operation=operation)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == self._x1m(operation)

    def test_the_default_text_is_unchanged(self) -> None:
        with pytest.raises(SurrealDbError) as raised:
            check_call_transaction("default", _TaggedTransaction("other"))
        assert str(raised.value) == self._x1m("call_function()")

    @pytest.mark.parametrize("tx", [None, _TaggedTransaction("default"), object()], ids=["none", "same", "untagged"])
    def test_a_transaction_of_the_same_connection_passes(self, tx: object) -> None:
        assert check_call_transaction("default", tx, operation="define_event()") is None


class TestForgetFunctionSignature:
    """What ``define_function()`` and ``remove_function()`` drop from the ``params=`` signature cache: the function's
    entry on every connection of the process to the same namespace and database, whatever URL it was cached under —
    the SDK reaches one server as ``ws://…/rpc``, ``ws://…``, ``http://…``, ``localhost`` or ``127.0.0.1`` alike.
    White box and no request: the caches are filled by hand, nothing is ever connected."""

    async def test_every_url_goes_other_names_stay(self) -> None:
        for name, url in (("evt", "ws://localhost:8000/rpc"), ("evt_admin", "http://127.0.0.1:8000")):
            SurrealDBConnectionManager.add_connection(
                name, url=url, user="root", password="root", namespace="ns", database="evt_db"
            )
        app, admin = (
            SurrealDBConnectionManager._manager_for(name)._SurrealDBConnectionManager__function_signatures  # type: ignore[attr-defined]
            for name in ("evt", "evt_admin")
        )
        kept = {
            ("ws://localhost:8000/rpc", "ns", "evt_db", "fn::other"): ("a",),
            ("ws://localhost:8000/rpc", "ns", "db", "fn::pair"): ("a", "b"),
            ("ws://localhost:8000/rpc", "other_ns", "evt_db", "fn::pair"): ("a", "b"),
        }
        app.update(kept)
        app[("ws://localhost:8000", "ns", "evt_db", "fn::pair")] = ("a", "b")
        app[("ws://localhost:8000/rpc", "ns", "evt_db", "fn::pair")] = ("a", "b")
        admin[("http://127.0.0.1:8000", "ns", "evt_db", "fn::pair")] = ("a", "b")

        async with SurrealDBConnectionManager.using("evt_admin"):
            SurrealDBConnectionManager._forget_function_signature("fn::pair")
        assert app == kept
        assert admin == {}

    @pytest.mark.parametrize("drop", ["forget", "clear"])
    @pytest.mark.parametrize(
        ("drops", "reads", "signature", "cached"),
        [(0, 1, ("a", "b"), [("a", "b")]), (1, 2, ("b", "a"), [("b", "a")]), (2, 2, ("b", "a"), [])],
    )
    async def test_a_read_overtaken_by_a_drop_is_read_again_never_stored(
        self, drop: str, drops: int, reads: int, signature: tuple[str, ...], cached: list[tuple[str, ...]]
    ) -> None:
        # The first read answers the order before a reorder; a drop while it is on its way back makes the second
        # read (the new order) the one stored, and a drop during that one too leaves nothing in the cache.
        SurrealDBConnectionManager.add_connection(
            "evt", url="ws://localhost:8000/rpc", user="root", password="root", namespace="ns", database="evt_db"
        )
        async with SurrealDBConnectionManager.using("evt"):
            client = _OvertakenReads(
                drops,
                SurrealDBConnectionManager.clear_function_signature_cache
                if drop == "clear"
                else lambda: SurrealDBConnectionManager._forget_function_signature("fn::pair"),
            )
            assert await SurrealDBConnectionManager._function_signature(client, "fn::pair") == signature
        assert client.reads == reads
        assert list(_evt_signatures().values()) == cached


class _OvertakenReads:
    """A client whose ``INFO FOR DB`` answers ``fn::pair($a, $b)``, then ``fn::pair($b, $a)`` on every later read;
    the first *drops* reads run *drop* while their answer is on its way back."""

    def __init__(self, drops: int, drop: Callable[[], None]) -> None:
        self.drops = drops
        self.drop = drop
        self.reads = 0

    async def query(self, statement: str, variables: Mapping[str, Any]) -> Any:
        assert (statement, variables) == ("INFO FOR DB;", {})
        self.reads += 1
        if self.reads <= self.drops:
            self.drop()
        order = "$a: string, $b: string" if self.reads == 1 else "$b: string, $a: string"
        return {"functions": {"pair": f"DEFINE FUNCTION fn::pair({order}) {{ RETURN 1; }} PERMISSIONS FULL"}}


def _c1(engine: str, names: str) -> str:
    """C1's exact text (spec §5.6)."""
    return (
        f"ASYNC events need SurrealDB 3.x, and this connection runs {engine}: {names} cannot be defined with "
        "async_mode=True. Drop async_mode, retry and max_depth, or connect to a SurrealDB 3.x server. Nothing was "
        "defined."
    )


_SYNC = Event("sync_one", then="NONE")
_NOTIFY = Event("notify", when="$event = 'CREATE'", then="CREATE outbox SET id = $after.id", async_mode=True)
_RETRYING = Event("retrying", then="NONE", async_mode=True, retry=2, max_depth=3)


class TestAsyncCheck:
    """Spec §4, §5.6 C1, D5: ``ensure_async_events_supported()`` refuses ``ASYNC`` events on a connection before
    SurrealDB 3.x, before any DDL, and asks the version only when an event is asynchronous."""

    @pytest.mark.parametrize("events", [[], (), [_SYNC], (_SYNC, Event("other", then="NONE"))])
    @pytest.mark.parametrize("embedded", [False, True])
    async def test_no_async_event_never_asks_the_version(self, events: Any, embedded: bool) -> None:
        client = _FakeClient(version="surrealdb-2.7.0")
        assert await ensure_async_events_supported(client, events, embedded=embedded) is None
        assert client.version_calls == 0
        assert client.queries == []

    async def test_c1_on_a_2x_server(self) -> None:
        client = _FakeClient(version="surrealdb-2.7.0")
        with pytest.raises(SurrealDbError) as raised:
            await ensure_async_events_supported(client, [_SYNC, _NOTIFY], embedded=False)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _c1("SurrealDB 2.x", "'notify'")
        assert client.version_calls == 1
        assert client.queries == []

    async def test_c1_on_the_embedded_engine(self) -> None:
        client = _FakeClient(version="surrealdb-2.0.0")
        with pytest.raises(SurrealDbError) as raised:
            await ensure_async_events_supported(client, (_NOTIFY,), embedded=True)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _c1("the embedded engine (a SurrealDB 2.x core)", "'notify'")
        assert client.queries == []

    async def test_c1_names_every_async_event_in_order(self) -> None:
        client = _FakeClient(version="surrealdb-2.7.0")
        with pytest.raises(SurrealDbError) as raised:
            await ensure_async_events_supported(client, [_RETRYING, _SYNC, _NOTIFY], embedded=False)
        assert str(raised.value) == _c1("SurrealDB 2.x", "'retrying', 'notify'")
        assert client.version_calls == 1

    async def test_c1_names_the_major(self) -> None:
        client = _FakeClient(version="surrealdb-1.5.4")
        with pytest.raises(SurrealDbError) as raised:
            await ensure_async_events_supported(client, [_NOTIFY], embedded=False)
        assert str(raised.value) == _c1("SurrealDB 1.x", "'notify'")

    @pytest.mark.parametrize("version", ["surrealdb-3.3.4", "surrealdb-3.1.5", "surrealdb-4.0.0", "dev", "", None])
    @pytest.mark.parametrize("embedded", [False, True])
    async def test_3x_or_an_unreadable_version_passes(self, version: object, embedded: bool) -> None:
        client = _FakeClient(version=version)
        assert await ensure_async_events_supported(client, [_NOTIFY, _RETRYING], embedded=embedded) is None
        assert client.version_calls == 1
        assert client.queries == []


# --- AC-1: the public API — exports, coroutines and signatures (spec §5.1–§5.4, §5.7) ------------------------------


def _parameter(
    name: str, annotation: Any, default: Any = inspect.Parameter.empty, *, keyword: bool = False
) -> inspect.Parameter:
    """One expected ``inspect.Parameter``: positional-or-keyword, or keyword-only with *keyword*."""
    kind = inspect.Parameter.KEYWORD_ONLY if keyword else inspect.Parameter.POSITIONAL_OR_KEYWORD
    return inspect.Parameter(name, kind, default=default, annotation=annotation)


_OVERWRITE = _parameter("overwrite", bool, True, keyword=True)
_MISSING_OK = _parameter("missing_ok", bool, True, keyword=True)
_TX = _parameter("tx", Transaction | None, None, keyword=True)
_FUNCTION_OPTIONS = [
    _parameter("parameters", Mapping[str, str] | None, None, keyword=True),
    _parameter("returns", str | None, None, keyword=True),
    _parameter("permissions", str | None, None, keyword=True),
    _parameter("comment", str | None, None, keyword=True),
    _OVERWRITE,
]

#: What ``inspect.signature()`` shows for each public v0.31.0 callable, ``cls`` bound (spec §5.1–§5.4): names, kinds,
#: defaults, annotations and the return annotation.
_SIGNATURES: dict[str, inspect.Signature] = {
    "Event": inspect.Signature(
        [
            _parameter("name", str),
            _parameter("then", str, keyword=True),
            _parameter("when", str | None, None, keyword=True),
            _parameter("async_mode", bool, False, keyword=True),
            _parameter("retry", int | None, None, keyword=True),
            _parameter("max_depth", int | None, None, keyword=True),
            _parameter("comment", str | None, None, keyword=True),
        ],
        return_annotation=None,
    ),
    "Event.ddl": inspect.Signature(
        [_parameter("self", inspect.Parameter.empty), _parameter("table", str), _OVERWRITE], return_annotation=str
    ),
    "function_ddl": inspect.Signature(
        [_parameter("function", str), _parameter("body", str), *_FUNCTION_OPTIONS], return_annotation=str
    ),
    "BaseSurrealModel.get_events": inspect.Signature([], return_annotation=tuple[Event, ...]),
    "BaseSurrealModel.event_ddl": inspect.Signature([_OVERWRITE], return_annotation=list[str]),
    "BaseSurrealModel.define_events": inspect.Signature([_OVERWRITE, _TX], return_annotation=list[str]),
    "BaseSurrealModel.remove_event": inspect.Signature([_parameter("event", str), _MISSING_OK, _TX], return_annotation=str),
    "SurrealDBConnectionManager.define_event": inspect.Signature(
        [_parameter("event", Event), _parameter("table", str), _OVERWRITE, _TX], return_annotation=str
    ),
    "SurrealDBConnectionManager.remove_event": inspect.Signature(
        [_parameter("event", str), _parameter("table", str), _MISSING_OK, _TX], return_annotation=str
    ),
    "SurrealDBConnectionManager.define_function": inspect.Signature(
        [_parameter("function", str), _parameter("body", str), *_FUNCTION_OPTIONS, _TX], return_annotation=str
    ),
    "SurrealDBConnectionManager.remove_function": inspect.Signature(
        [_parameter("function", str), _MISSING_OK, _TX], return_annotation=str
    ),
}

#: The six coroutines that apply DDL (spec §5.3, §5.4).
_APPLIERS = [
    "BaseSurrealModel.define_events",
    "BaseSurrealModel.remove_event",
    "SurrealDBConnectionManager.define_event",
    "SurrealDBConnectionManager.remove_event",
    "SurrealDBConnectionManager.define_function",
    "SurrealDBConnectionManager.remove_function",
]

#: The pure ones: they build SurrealQL without a connection, so they are plain functions.
_PURE = ["BaseSurrealModel.get_events", "BaseSurrealModel.event_ddl", "Event.ddl", "function_ddl"]

#: The four manager methods, each ``@classmethod @_routed("current")`` (spec §5.4).
_MANAGER_METHODS = ["define_event", "remove_event", "define_function", "remove_function"]


def _public(path: str) -> Any:
    """The callable *path* names (``"Owner.method"`` or a bare name), as a user reaches it."""
    owners: dict[str, Any] = {
        "Event": Event,
        "function_ddl": function_ddl,
        "BaseSurrealModel": BaseSurrealModel,
        "SurrealDBConnectionManager": SurrealDBConnectionManager,
    }
    owner, _, attribute = path.partition(".")
    return getattr(owners[owner], attribute) if attribute else owners[owner]


class TestPublicApi:
    """AC-1 (spec §5.1–§5.4, §5.7): the exports, which callables are coroutines, and every signature, pinned — the
    manager's ``remove_event`` takes ``event``, never ``name`` (D17)."""

    def test_exports(self) -> None:
        names = surreal_orm_lite.__all__
        assert schema.__all__ == ["Event", "function_ddl"]
        for name in schema.__all__:
            assert names.count(name) == 1, name
            assert getattr(surreal_orm_lite, name) is getattr(schema, name), name
        # The "Server-side events and DDL (v0.31.0)" group follows the schema introspection group.
        assert names.index("function_ddl") == names.index("Event") + 1
        assert names.index("Event") > names.index("generate_models_from_db")

    @pytest.mark.parametrize("path", _APPLIERS)
    def test_appliers_are_coroutine_functions(self, path: str) -> None:
        assert inspect.iscoroutinefunction(_public(path)) is True

    @pytest.mark.parametrize("path", _PURE)
    def test_renderers_are_plain_functions(self, path: str) -> None:
        assert inspect.iscoroutinefunction(_public(path)) is False

    @pytest.mark.parametrize("path", sorted(_SIGNATURES))
    def test_signature(self, path: str) -> None:
        assert inspect.signature(_public(path)) == _SIGNATURES[path]

    def test_a_model_subclass_shows_the_same_signatures(self) -> None:
        for path in _SIGNATURES:
            if path.startswith("BaseSurrealModel."):
                method = path.partition(".")[2]
                assert inspect.signature(getattr(EvMdPerson, method)) == _SIGNATURES[path], method

    @pytest.mark.parametrize("method", _MANAGER_METHODS)
    def test_manager_methods_are_routed_with_nothing_hidden(self, method: str) -> None:
        """The current connection's policy, the same coroutine and signature on the router and on a connection class,
        and the declared signature is the visible one plus ``cls``: ``_routed`` hid no ``name`` (D17)."""
        function = vars(SurrealDBConnectionManager)[method].__func__
        assert function.__surreal_routed__ == "current"
        expected = _SIGNATURES[f"SurrealDBConnectionManager.{method}"]
        default = SurrealDBConnectionManager._manager_for("default")
        assert inspect.iscoroutinefunction(getattr(default, method)) is True
        assert inspect.signature(getattr(default, method)) == expected
        declared = inspect.signature(inspect.unwrap(function))
        assert list(declared.parameters)[0] == "cls"
        assert declared.replace(parameters=list(declared.parameters.values())[1:]) == expected
        assert "name" not in declared.parameters

    def test_remove_event_takes_event_never_name(self) -> None:
        """D17: a first ``name`` parameter would be read by ``_routed`` as a connection name and hidden."""
        visible = inspect.signature(SurrealDBConnectionManager.remove_event).parameters
        assert list(visible) == ["event", "table", "missing_ok", "tx"]
        assert "name" not in visible
        assert [visible[name].kind for name in ("missing_ok", "tx")] == [inspect.Parameter.KEYWORD_ONLY] * 2
        declared = inspect.signature(inspect.unwrap(vars(SurrealDBConnectionManager)["remove_event"].__func__))
        assert list(declared.parameters) == ["cls", "event", "table", "missing_ok", "tx"]
        assert list(inspect.signature(BaseSurrealModel.remove_event).parameters) == ["event", "missing_ok", "tx"]

    def test_events_is_a_config_key(self) -> None:
        assert "events" in SurrealConfigDict.__annotations__
        assert SurrealConfigDict.__annotations__["events"] == Sequence[Event] | None
        assert "events" in SurrealConfigDict.__optional_keys__


# === E2E (spec §9.3): the connection "evt" on the database evt_db, over WebSocket and HTTP, and mem:// ================

#: The engines every E2E class runs on: the line under test over WebSocket and HTTP, and the embedded engine.
_KINDS = ["ws", "http", "mem"]

#: The reset of spec §9.3, in one query: ``ev_log`` and ``ev_person`` are defined up front, so a raw ``SELECT`` of
#: them reads ``[]`` before any event has written (3.x refuses a raw ``SELECT`` of a table that does not exist).
_EVT_RESET = (
    "REMOVE DATABASE IF EXISTS evt_db; DEFINE DATABASE evt_db; "
    "DEFINE TABLE ev_log TYPE ANY SCHEMALESS; DEFINE TABLE ev_person TYPE ANY SCHEMALESS;"
)

#: The events of spec §5.2, applied through the ORM.
_EV_AUDIT = Event("ev_audit", then=_AUDIT_THEN)
_EV_RENAME = Event(
    "ev_rename",
    when="$event = 'UPDATE' AND $before.name != $after.name",
    then="CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name",
    comment="it's renamed",
)
_EV_GUARD = Event("ev_guard", when="$after.name = 'forbidden'", then="THROW 'name refused: ' + $after.name")
_EV_UPPER = Event(
    "ev_upper",
    when="$event = 'CREATE'",
    then="LET $n = string::uppercase($after.name); CREATE ev_log SET kind = 'upper', name = $n",
)
_EV_ASYNC = Event(
    "ev_async",
    when="$event = 'CREATE'",
    then="CREATE ev_log SET kind = 'async', rid = $value.id",
    async_mode=True,
    retry=2,
)

#: ``ev_async`` as SurrealDB 3.x reads it back (spec §3): ``ASYNC`` always with both values.
_EV_ASYNC_INFO_3X = (
    "DEFINE EVENT ev_async ON ev_person ASYNC RETRY 2 MAXDEPTH 3 WHEN $event = 'CREATE' "
    "THEN { CREATE ev_log SET kind = 'async', rid = $value.id }"
)

#: ``fn::ev_greet`` of spec §5.2 — ``define_function(*_GREET_ARGS, **_GREET_KWARGS)`` —, parameters ``(name, times)``.
_GREET_ARGS = (_GREET, "RETURN string::concat('Hello ', $name);")
_GREET_KWARGS: dict[str, Any] = {
    "parameters": {"name": "string", "times": "option<int>"},
    "returns": "string",
    "comment": "Greets",
}
#: The same function, its parameters declared in the other order: ``(times, name)``.
_GREET_REORDERED: dict[str, Any] = {**_GREET_KWARGS, "parameters": {"times": "option<int>", "name": "string"}}


class EvPerson(BaseSurrealModel):
    """The four synchronous events of spec §5.2 on ``ev_person``, in the database of the connection ``"evt"``."""

    model_config = SurrealConfigDict(
        table_name="ev_person", connection="evt", events=[_EV_AUDIT, _EV_RENAME, _EV_GUARD, _EV_UPPER]
    )

    id: str | None = None
    name: str
    age: int | None = None


class EvLog(BaseSurrealModel):
    """The rows the events write; any column besides ``kind`` is kept as an extra."""

    model_config = SurrealConfigDict(table_name="ev_log", connection="evt", extra="allow")

    id: str | None = None
    kind: str | None = None


#: An event the server cannot parse, whose excerpt of the statement quotes the secret (checked on every engine).
_EV_HOOK = Event("ev_hook", then=f"RETURN <{_SECRET}> 1")


class EvHookPerson(BaseSurrealModel):
    """``ev_hook`` on ``ev_person``, on the connection ``"evt"``: its definition holds a secret."""

    model_config = SurrealConfigDict(table_name="ev_person", connection="evt", events=[_EV_HOOK])

    id: str | None = None
    name: str


class EvAsyncPerson(BaseSurrealModel):
    """``ev_async`` on ``ev_person`` — on the connection ``"evt"`` too, so nothing lands in the default database."""

    model_config = SurrealConfigDict(table_name="ev_person", connection="evt", events=[_EV_ASYNC])

    id: str | None = None
    name: str
    age: int | None = None


def _url(kind: str) -> str:
    """The URL of *kind*: ``ws`` or ``http`` on the line under test (``SURREALDB_PORT``), or ``mem``."""
    if kind == "mem":
        return "mem://"
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _register_evt(kind: str) -> None:
    if kind == "mem":
        SurrealDBConnectionManager.add_connection("evt", url="mem://", namespace="ns", database="evt_db")
    else:
        SurrealDBConnectionManager.add_connection(
            "evt", url=_url(kind), user="root", password="root", namespace="ns", database="evt_db"
        )


async def _apply_ddl(client: Any, ddl: str) -> None:
    """Run *ddl* and check every statement: ``query()`` checks only the first one."""
    response = await client.query_raw(ddl, {})
    assert "error" not in response, response["error"]
    refused = [result for result in response["result"] if result["status"] != "OK"]
    assert response["result"] and not refused, refused


@contextlib.asynccontextmanager
async def _evt(kind: str) -> AsyncIterator[Any]:
    """The connection ``"evt"`` on *kind*, its database ``evt_db`` reset in one query; removed, then unregistered,
    on exit."""
    _register_evt(kind)
    client = await SurrealDBConnectionManager.get_client("evt")
    await _apply_ddl(client, _EVT_RESET)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client("evt")
            await cleanup.query("REMOVE DATABASE IF EXISTS evt_db;", {})
        await SurrealDBConnectionManager.remove_connection("evt")


async def _major(client: Any) -> int:
    """The major version of the engine *client* talks to: ``"surrealdb-3.3.4"`` → 3; ``mem://`` → 2."""
    return int((await client.version()).rsplit("-", 1)[-1].split(".", 1)[0])


async def _require_line(client: Any, line: int, item: str) -> None:
    """A *line*-only *item*: the test runs on that line, where a refusal fails it, and is skipped on the other."""
    if await _major(client) != line:
        pytest.skip(f"SurrealDB {line}.x only: {item}")


def _spy(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[tuple[str, tuple[object, ...]]]:
    """Record every ``query``, ``query_raw`` and ``version`` call *client* receives (and let it through)."""
    calls: list[tuple[str, tuple[object, ...]]] = []

    def recording(method: str) -> Any:
        real = getattr(client, method)

        async def call(*args: object, **kwargs: object) -> object:
            calls.append((method, args))
            return await real(*args, **kwargs)

        return call

    for method in ("query", "query_raw", "version"):
        monkeypatch.setattr(client, method, recording(method))
    return calls


async def _wait_rows(queryset: Any, n: int, timeout: float = 5.0) -> list[Any]:
    """The rows of *queryset* once there are at least *n* — polled through the ORM every 20 ms, for an ``ASYNC``
    event's rows; fails after *timeout* seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        rows: list[Any] = await queryset.exec()
        if len(rows) >= n:
            return rows
        if loop.time() >= deadline:
            pytest.fail(f"{n} row(s) expected within {timeout} s, {len(rows)} read")
        await asyncio.sleep(0.02)


async def _events_info(client: Any, table: str) -> dict[str, str]:
    """The ``events`` of ``INFO FOR TABLE`` *table* (a constant name of these tests): name → definition."""
    info = await client.query(f"INFO FOR TABLE {table};", {})
    return dict(info["events"])


async def _log(client: Any) -> list[tuple[Any, ...]]:
    """Every ``ev_log`` row as ``(kind, rid, old, new, name)`` — ``rid`` as text, an absent column ``None`` —,
    sorted (row ids are random)."""
    rows = await client.query("SELECT * FROM ev_log;", {})
    return _sorted(
        (
            row.get("kind"),
            None if row.get("rid") is None else str(row["rid"]),
            row.get("old"),
            row.get("new"),
            row.get("name"),
        )
        for row in rows
    )


def _sorted(rows: Any) -> list[tuple[Any, ...]]:
    return sorted(rows, key=repr)


#: The names AC-15 (i) refuses — a ``;``, a backtick, a space, a trailing newline, nothing.
_BAD_NAMES = ["a;b", "a`b", "a b", "a\n", ""]


class TestEventsE2E:
    """AC-5, AC-7, AC-13 (S3) and AC-15 (i, iii): synchronous events defined, fired and removed — from a model and
    from the manager — on every engine."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_events_fire_on_orm_writes(self, kind: str) -> None:
        async with _evt(kind) as client:
            applied = await EvPerson.define_events()
            assert applied == EvPerson.event_ddl()
            assert sorted(await _events_info(client, "ev_person")) == ["ev_audit", "ev_guard", "ev_rename", "ev_upper"]

            person = EvPerson(id="ada", name="Ada", age=30)
            await person.save()
            await person.merge(name="Bea")
            await person.merge(age=31)
            await person.delete()
            assert await _log(client) == _sorted(
                [
                    ("CREATE", "ev_person:ada", None, None, None),
                    ("upper", None, None, None, "ADA"),
                    ("UPDATE", "ev_person:ada", None, None, None),
                    ("rename", None, "Ada", "Bea", None),
                    ("UPDATE", "ev_person:ada", None, None, None),
                    ("DELETE", "ev_person:ada", None, None, None),
                ]
            )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_guard_event_refuses_the_write(self, kind: str) -> None:
        async with _evt(kind):
            await EvPerson.define_events()
            with pytest.raises(ServerError) as raised:
                await EvPerson(id="bad", name="forbidden").save()
            assert "name refused: forbidden" in str(raised.value)
            assert await EvPerson.objects().filter(name="forbidden").exec() == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_events_is_idempotent(self, kind: str) -> None:
        async with _evt(kind) as client:
            first = await EvPerson.define_events()
            before = await _events_info(client, "ev_person")
            assert await EvPerson.define_events() == first
            assert await _events_info(client, "ev_person") == before

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_keeps_an_existing_event(self, kind: str) -> None:
        async with _evt(kind) as client:
            await _apply_ddl(client, "DEFINE EVENT ev_audit ON ev_person WHEN false THEN { CREATE ev_log SET kind = 'kept' };")
            applied = await EvPerson.define_events(overwrite=False)
            assert applied == EvPerson.event_ddl(overwrite=False)
            assert all(statement.startswith("DEFINE EVENT IF NOT EXISTS ") for statement in applied)
            events = await _events_info(client, "ev_person")
            assert sorted(events) == ["ev_audit", "ev_guard", "ev_rename", "ev_upper"]
            assert "WHEN false" in events["ev_audit"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_model_remove_event(self, kind: str) -> None:
        async with _evt(kind) as client:
            await EvPerson.define_events()
            assert await EvPerson.remove_event("ev_audit") == "REMOVE EVENT IF EXISTS `ev_audit` ON `ev_person`;"
            assert sorted(await _events_info(client, "ev_person")) == ["ev_guard", "ev_rename", "ev_upper"]
            await EvPerson(id="cy", name="Cy").save()
            assert await _log(client) == [("upper", None, None, None, "CY")]

            # Removal is by name: a second call passes (IF EXISTS), and so does an event the model never declared.
            assert await EvPerson.remove_event("ev_audit") == "REMOVE EVENT IF EXISTS `ev_audit` ON `ev_person`;"
            await _apply_ddl(client, "DEFINE EVENT ev_extra ON ev_person THEN { NONE };")
            await EvPerson.remove_event("ev_extra")
            assert sorted(await _events_info(client, "ev_person")) == ["ev_guard", "ev_rename", "ev_upper"]

            with pytest.raises(SurrealDbNotFoundError) as raised:
                await EvPerson.remove_event("ev_audit", missing_ok=False)
            assert str(raised.value).startswith("Can't apply event removal: 'ev_audit' on table 'ev_person' -> ")
            assert "does not exist" in str(raised.value)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_manager_define_and_remove_event_on_a_table_without_a_model(self, kind: str) -> None:
        stock_guard = Event("ev_stock", when="$after.stock < 0", then="THROW 'stock refused'")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            statement = await SurrealDBConnectionManager.define_event(stock_guard, "ev_product")
            assert statement == stock_guard.ddl("ev_product")
            assert sorted(await _events_info(client, "ev_product")) == ["ev_stock"]
            with pytest.raises(ServerError, match="stock refused"):
                await client.query("CREATE ev_product:p1 SET stock = -1;", {})

            removal = "REMOVE EVENT IF EXISTS `ev_stock` ON `ev_product`;"
            assert await SurrealDBConnectionManager.remove_event("ev_stock", "ev_product") == removal
            assert await _events_info(client, "ev_product") == {}
            await client.query("CREATE ev_product:p1 SET stock = -1;", {})
            assert await SurrealDBConnectionManager.remove_event("ev_stock", "ev_product") == removal

            with pytest.raises(SurrealDbNotFoundError) as raised:
                await SurrealDBConnectionManager.remove_event("ev_stock", "ev_product", missing_ok=False)
            assert str(raised.value).startswith("Can't apply event removal: 'ev_stock' on table 'ev_product' -> ")
            assert "does not exist" in str(raised.value)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_through_the_manager(self, kind: str) -> None:
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await _apply_ddl(client, "DEFINE EVENT ev_audit ON ev_person WHEN false THEN { NONE };")
            statement = await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_person", overwrite=False)
            assert statement == _EV_AUDIT.ddl("ev_person", overwrite=False)
            assert "WHEN false" in (await _events_info(client, "ev_person"))["ev_audit"]
            await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_person")
            assert "WHEN false" not in (await _events_info(client, "ev_person"))["ev_audit"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_server_refusal_is_s3(self, kind: str) -> None:
        # AC-13: well formed for the ORM, refused by the server's parser.
        broken = Event("ev_broken", then="CREATE")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            with pytest.raises(SurrealDbError) as raised:
                await SurrealDBConnectionManager.define_event(broken, "ev_person")
            major = await _major(client)
        assert type(raised.value) is SurrealDbError
        assert str(raised.value).startswith("Can't apply event definition: 'ev_broken' on table 'ev_person' -> ")
        assert str(raised.value).endswith(" of the statement)")  # the server's excerpt is left out, not its position
        expected = RuntimeError if kind == "mem" else ValidationError if major >= 3 else InternalError
        assert raised.value.__cause__ is None
        assert type(raised.value.__context__) is expected

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_refusal_never_quotes_the_definition(self, kind: str) -> None:
        # Review of PR #273: the server's excerpt quotes the secret; the ORM's error, its repr() and its traceback
        # do not, from the model and from the manager.
        async with _evt(kind), SurrealDBConnectionManager.using("evt"):
            with pytest.raises(SurrealDbError) as from_model:
                await EvHookPerson.define_events()
            with pytest.raises(SurrealDbError) as from_manager:
                await SurrealDBConnectionManager.define_event(_EV_HOOK, "ev_person")
        for raised in (from_model, from_manager):
            assert type(raised.value) is SurrealDbError
            assert str(raised.value).startswith("Can't apply event definition: 'ev_hook' on table 'ev_person' -> ")
            assert _SECRET in str(raised.value.__context__)
            assert _leaks(raised.value, _SECRET) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_failing_then_refuses_the_write_by_line(self, kind: str) -> None:
        # The 2.x/3.x table's row "An event whose THEN fails": 3.x names the event; 2.x gives "An error occurred: …"
        # for a THROW and the bare cause for any other error.
        thrower = Event("ev_throw", when="$after.k = 1", then="THROW 'nope'")
        duplicate = Event("ev_dup", when="$after.k = 2", then="CREATE ev_log:dup")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_event(thrower, "ev_person")
            await SurrealDBConnectionManager.define_event(duplicate, "ev_person")
            await client.query("CREATE ev_log:dup;", {})
            with pytest.raises(ServerError) as thrown:
                await client.query("CREATE ev_person:a SET k = 1;", {})
            with pytest.raises(ServerError) as failed:
                await client.query("CREATE ev_person:b SET k = 2;", {})
            assert await client.query("SELECT * FROM ev_person;", {}) == []
            major = await _major(client)
        assert type(thrown.value).__name__ == type(failed.value).__name__ == "InternalError"
        cause = "Database record `ev_log:dup` already exists"
        if major >= 3:
            assert str(thrown.value) == "Error while processing event ev_throw: An error occurred: nope"
            assert str(failed.value) == f"Error while processing event ev_dup: {cause}"
        else:
            assert str(thrown.value) == "An error occurred: nope"
            assert str(failed.value) == cause

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_input_differs_by_line(self, kind: str) -> None:
        # AC-7 (M5): $input is the write's input on 3.x, NONE on 2.x and the embedded engine.
        recorder = Event("ev_input", then="CREATE ev_log SET kind = 'input', inp = $input")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_event(recorder, "ev_person")
            await client.query("CREATE ev_person:i SET name = 'x';", {})
            rows = await client.query("SELECT * FROM ev_log WHERE kind = 'input';", {})
            expected = {"name": "x"} if await _major(client) >= 3 else None
        assert [row.get("inp") for row in rows] == [expected]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_unchanged_writes_differ_by_line(self, kind: str) -> None:
        # AC-7 (M6): an unchanged merge() fires no event anywhere; an unchanged upsert() one UPDATE event on 3.x only.
        async with _evt(kind) as client:
            await EvPerson.define_events()
            person = EvPerson(id="ada", name="Ada", age=30)
            await person.save()
            await client.query("DELETE ev_log;", {})
            await person.merge(name="Ada")
            assert await _log(client) == []
            await person.upsert()
            expected = [("UPDATE", "ev_person:ada", None, None, None)] if await _major(client) >= 3 else []
            assert await _log(client) == expected

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_names_are_refused_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # AC-15 (i): each name refused by the ORM, before anything reaches the server.
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            calls = _spy(monkeypatch, client)
            for bad in _BAD_NAMES:
                event_error = f"Invalid event name {bad!r}: {_IDENTIFIER_EXPECTED}"
                table_error = f"Invalid table name {bad!r}: {_IDENTIFIER_EXPECTED}"
                with pytest.raises(ValueError) as raised:
                    await SurrealDBConnectionManager.define_event(Event(bad, then="NONE"), "ev_person")
                assert str(raised.value) == event_error
                with pytest.raises(ValueError) as raised:
                    await SurrealDBConnectionManager.remove_event(bad, "ev_person")
                assert str(raised.value) == event_error
                with pytest.raises(ValueError) as raised:
                    await EvPerson.remove_event(bad)
                assert str(raised.value) == event_error
                with pytest.raises(ValueError) as raised:
                    await SurrealDBConnectionManager.define_event(_EV_AUDIT, bad)
                assert str(raised.value) == table_error
                with pytest.raises(ValueError) as raised:
                    await SurrealDBConnectionManager.remove_event("ev_audit", bad)
                assert str(raised.value) == table_error
            assert calls == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_arguments_are_refused_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # E15, E13 and E1, each before anything reaches the server.
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            calls = _spy(monkeypatch, client)
            with pytest.raises(TypeError) as raised:
                await SurrealDBConnectionManager.define_event("ev_audit", "ev_person")  # type: ignore[arg-type]
            assert str(raised.value) == "define_event() takes an Event, got str."
            with pytest.raises(TypeError) as raised:
                await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_person", overwrite=1)  # type: ignore[arg-type]
            assert str(raised.value) == "overwrite= must be a bool, got int."
            with pytest.raises(TypeError) as raised:
                await SurrealDBConnectionManager.remove_event("ev_audit", "ev_person", missing_ok="yes")  # type: ignore[arg-type]
            assert str(raised.value) == "missing_ok= must be a bool, got str."
            with pytest.raises(TypeError) as raised:
                await EvPerson.remove_event("ev_audit", missing_ok=1)  # type: ignore[arg-type]
            assert str(raised.value) == "missing_ok= must be a bool, got int."
            with pytest.raises(TypeError) as raised:
                await EvPerson.remove_event(_EV_AUDIT)  # type: ignore[arg-type]
            assert str(raised.value) == "Event name must be a str, got Event."
            with pytest.raises(TypeError) as raised:
                await SurrealDBConnectionManager.define_event(_EV_AUDIT, 1)  # type: ignore[arg-type]
            assert str(raised.value) == "Table name must be a str, got int."
            with pytest.raises(TypeError) as raised:
                await EvPerson.define_events(overwrite=1)  # type: ignore[arg-type]
            assert str(raised.value) == "overwrite= must be a bool, got int."
            assert calls == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_model_without_events_sends_nothing(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # AC-4: no statement to apply, so no request at all — not even version().
        async with _evt(kind) as client:
            calls = _spy(monkeypatch, client)
            assert await EvLog.define_events() == []
            assert calls == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_comments_round_trip(self, kind: str) -> None:
        # AC-15 (iii): a quote, double quotes, a backslash and a newline, escaped by the ORM and read back as written.
        comments = {"ev_c1": "it's", "ev_c2": 'say "hi"', "ev_c3": "back\\slash", "ev_c4": "two\nlines"}
        async with _evt(kind), SurrealDBConnectionManager.using("evt"):
            for name, comment in comments.items():
                await SurrealDBConnectionManager.define_event(Event(name, then="NONE", comment=comment), "ev_person")
            schema = await DatabaseIntrospector("evt", tables=["ev_person"]).introspect()
        assert schema.issues == ()
        assert {name: event.comment for name, event in schema.tables["ev_person"].events.items()} == comments


class TestAsyncEventsE2E:
    """AC-6: ``ASYNC`` events defined and fired on SurrealDB 3.x; refused before any DDL (C1) on 2.x and the
    embedded engine."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_model_defines_an_async_event_on_3x(self, kind: str) -> None:
        async with _evt(kind) as client:
            await _require_line(client, 3, "ASYNC events")
            assert await EvAsyncPerson.define_events() == [_EV_ASYNC.ddl("ev_person")]
            assert await _events_info(client, "ev_person") == {"ev_async": _EV_ASYNC_INFO_3X}
            await EvAsyncPerson(id="ann", name="Ann").save()
            rows = await _wait_rows(EvLog.objects().filter(kind="async"), 1)
        assert [str(row.rid) for row in rows] == ["ev_person:ann"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_manager_defines_an_async_event_on_3x(self, kind: str) -> None:
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await _require_line(client, 3, "ASYNC events")
            assert await SurrealDBConnectionManager.define_event(_EV_ASYNC, "ev_person") == _EV_ASYNC.ddl("ev_person")
            assert await _events_info(client, "ev_person") == {"ev_async": _EV_ASYNC_INFO_3X}
            await client.query("CREATE ev_person:bo SET name = 'Bo';", {})
            rows = await _wait_rows(EvLog.objects().filter(kind="async"), 1)
        assert [str(row.rid) for row in rows] == ["ev_person:bo"]

    async def test_an_async_event_runs_after_its_transaction_commits(self) -> None:
        # Interactive transactions: WebSocket on 3.x. The event runs once the transaction commits, so it has not run
        # while the transaction is open.
        async with _evt("ws") as client:
            await _require_line(client, 3, "ASYNC events")
            await EvAsyncPerson.define_events()
            async with SurrealDBConnectionManager.transaction("evt") as tx:
                assert tx.is_interactive
                await EvAsyncPerson(id="tx", name="Tx").save(tx=tx)
                assert await EvLog.objects(tx=tx).filter(kind="async").exec() == []
            rows = await _wait_rows(EvLog.objects().filter(kind="async"), 1)
        assert [str(row.rid) for row in rows] == ["ev_person:tx"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_c1_before_any_ddl_on_2x_and_the_embedded_engine(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _evt(kind) as client:
            if await _major(client) >= 3:
                pytest.skip("SurrealDB 2.x and the embedded engine only: ASYNC events refused before any DDL (C1)")
            engine = "the embedded engine (a SurrealDB 2.x core)" if kind == "mem" else "SurrealDB 2.x"
            expected = _c1(engine, "'ev_async'")
            calls = _spy(monkeypatch, client)
            with pytest.raises(SurrealDbError) as from_model:
                await EvAsyncPerson.define_events()
            async with SurrealDBConnectionManager.using("evt"):
                with pytest.raises(SurrealDbError) as from_manager:
                    await SurrealDBConnectionManager.define_event(_EV_ASYNC, "ev_person")
                async with SurrealDBConnectionManager.transaction() as tx:
                    assert not tx.is_interactive
                    with pytest.raises(SurrealDbError) as in_model_tx:
                        await EvAsyncPerson.define_events(tx=tx)
                    with pytest.raises(SurrealDbError) as in_manager_tx:
                        await SurrealDBConnectionManager.define_event(_EV_ASYNC, "ev_person", tx=tx)
                    assert tx.statements == []
            assert [method for method, _ in calls] == ["version"] * 4
            assert "ev_async" not in await _events_info(client, "ev_person")
        for raised in (from_model, from_manager, in_model_tx, in_manager_tx):
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == expected


class _Abort(Exception):
    """Raised by a test to leave a ``transaction()`` block."""


class TestEventTransactionsE2E:
    """AC-8 (a, b): events applied with ``tx=`` on every engine and both strategies — interactive (WebSocket on 3.x)
    and buffered (HTTP, WebSocket on 2.x, the embedded engine) — and a write a guard event refuses inside a
    ``transaction()`` block."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_events_defined_in_the_block_fire_for_its_writes(self, kind: str) -> None:
        async with _evt(kind) as client:
            interactive = kind == "ws" and await _major(client) >= 3
            async with SurrealDBConnectionManager.transaction("evt") as tx:
                assert tx.is_interactive is interactive
                assert await EvPerson.define_events(tx=tx) == EvPerson.event_ddl()
                await EvPerson(id="ada", name="Ada").save(tx=tx)
            assert sorted(await _events_info(client, "ev_person")) == ["ev_audit", "ev_guard", "ev_rename", "ev_upper"]
            assert await _log(client) == _sorted(
                [("CREATE", "ev_person:ada", None, None, None), ("upper", None, None, None, "ADA")]
            )

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_removal_and_the_manager_apply_with_the_block(self, kind: str) -> None:
        stock_guard = Event("ev_stock", when="$after.stock < 0", then="THROW 'stock refused'")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await EvPerson.define_events()
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await EvPerson.remove_event("ev_audit", tx=tx) == "REMOVE EVENT IF EXISTS `ev_audit` ON `ev_person`;"
                assert await SurrealDBConnectionManager.define_event(stock_guard, "ev_product", tx=tx) == stock_guard.ddl(
                    "ev_product"
                )
                assert await SurrealDBConnectionManager.remove_event("ev_upper", "ev_person", tx=tx) == (
                    "REMOVE EVENT IF EXISTS `ev_upper` ON `ev_person`;"
                )
            assert sorted(await _events_info(client, "ev_person")) == ["ev_guard", "ev_rename"]
            assert sorted(await _events_info(client, "ev_product")) == ["ev_stock"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_buffered_refusal_comes_back_at_commit(self, kind: str) -> None:
        # Review of PR #273: a buffered transaction only queues the statement, so the refusal is the transaction's
        # SurrealDbError when the block commits, never the classified SurrealDbNotFoundError.
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            if kind == "ws" and await _major(client) >= 3:
                pytest.skip("buffered transactions only: HTTP, SurrealDB 2.x and the embedded engine")
            for remove in (
                lambda tx: EvPerson.remove_event("ev_audit", missing_ok=False, tx=tx),
                lambda tx: SurrealDBConnectionManager.remove_event("ev_audit", "ev_person", missing_ok=False, tx=tx),
                lambda tx: SurrealDBConnectionManager.remove_function(_GREET, missing_ok=False, tx=tx),
            ):
                with pytest.raises(SurrealDbError) as raised:
                    async with SurrealDBConnectionManager.transaction() as tx:
                        assert not tx.is_interactive
                        await remove(tx)
                assert type(raised.value) is SurrealDbError
                assert str(raised.value).startswith("Transaction failed and rolled back: ")
                assert "does not exist" in str(raised.value)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_block_that_raises_defines_no_event(self, kind: str) -> None:
        async with _evt(kind) as client:
            with pytest.raises(_Abort):
                async with SurrealDBConnectionManager.transaction("evt") as tx:
                    await EvPerson.define_events(tx=tx)
                    async with SurrealDBConnectionManager.using("evt"):
                        await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_product", tx=tx)
                    raise _Abort
            assert await _events_info(client, "ev_person") == {}
            assert "ev_product" not in (await client.query("INFO FOR DB;", {}))["tables"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_write_the_guard_refuses_cancels_the_block(self, kind: str) -> None:
        # M24: the refusal propagates out of transaction(), which cancels the block: nothing is written.
        async with _evt(kind) as client:
            await EvPerson.define_events()
            with pytest.raises(SurrealDbError) as raised:
                async with SurrealDBConnectionManager.transaction("evt") as tx:
                    await EvPerson(id="ok", name="Ok").save(tx=tx)
                    await EvPerson(id="bad", name="forbidden").save(tx=tx)
            assert "name refused: forbidden" in str(raised.value)
            assert await EvPerson.objects().exec() == []
            assert await _log(client) == []


def _x1(operation: str, model: str = "EvPerson") -> str:
    """X1: ``routing.check_transaction()``'s text for *operation* of *model* (on ``"evt"``) with a default tx."""
    return (
        f"{operation} cannot run in this transaction: the transaction belongs to connection 'default' and {model} "
        "runs on connection 'evt'. A transaction is one connection's (one database): open it on 'evt' — "
        "SurrealDBConnectionManager.transaction('evt') — or run the block inside using('default')."
    )


def _x1m(operation: str) -> str:
    """X1m: ``routing.check_call_transaction()``'s text for *operation* on ``"evt"`` with a default tx."""
    return (
        f"{operation} cannot run in this transaction: the transaction belongs to connection 'default' and the call "
        "runs on connection 'evt'. A transaction is one connection's (one database): call it inside "
        "using('default'), or open the transaction on 'evt'."
    )


@contextlib.asynccontextmanager
async def _default(kind: str) -> AsyncIterator[Any]:
    """The default connection on the line under test (``ns``/``db``) — over *kind*, WebSocket for ``mem`` — and its
    client; its previous settings put back on exit."""
    saved_url, *saved = default_connection_class()._settings()
    url = _url("ws" if kind == "mem" else kind)
    SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="ns", database="db")
    try:
        yield await SurrealDBConnectionManager.get_client()
    finally:
        await SurrealDBConnectionManager.unset_connection()
        if saved_url is not None:
            SurrealDBConnectionManager.set_connection(saved_url, *saved)


async def _tables(client: Any) -> dict[str, Any]:
    """The tables of *client*'s database."""
    return dict((await client.query("INFO FOR DB;", {}))["tables"])


class TestEventConnectionsE2E:
    """AC-9 (events): the model's connection, the manager's current connection, and a transaction of another
    connection refused before any request (X1, X1m)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_model_defines_on_its_own_connection(self, kind: str) -> None:
        async with _default(kind) as default, _evt(kind) as client:
            await EvPerson.define_events()
            assert sorted(await _events_info(client, "ev_person")) == ["ev_audit", "ev_guard", "ev_rename", "ev_upper"]
            # A DEFINE EVENT creates its table (M16): the default database has none, so nothing landed there.
            assert "ev_person" not in await _tables(default)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_manager_defines_on_the_current_connection(self, kind: str) -> None:
        async with _default(kind) as default, _evt(kind) as client:
            async with SurrealDBConnectionManager.using("evt"):
                await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_product")
            assert sorted(await _events_info(client, "ev_product")) == ["ev_audit"]
            assert "ev_product" not in await _tables(default)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_x1_a_transaction_of_another_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _default(kind) as default, _evt(kind) as client:
            async with SurrealDBConnectionManager.transaction() as tx:
                evt_calls = _spy(monkeypatch, client)
                default_calls = _spy(monkeypatch, default)
                with pytest.raises(SurrealDbError) as define_events:
                    await EvPerson.define_events(tx=tx)
                with pytest.raises(SurrealDbError) as remove_event:
                    await EvPerson.remove_event("ev_audit", tx=tx)
                async with SurrealDBConnectionManager.using("evt"):
                    with pytest.raises(SurrealDbError) as define_event:
                        await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_person", tx=tx)
                    with pytest.raises(SurrealDbError) as manager_remove_event:
                        await SurrealDBConnectionManager.remove_event("ev_audit", "ev_person", tx=tx)
                assert evt_calls == []
                assert default_calls == []
                assert getattr(tx, "statements", []) == []
            assert await _events_info(client, "ev_person") == {}
        assert str(define_events.value) == _x1("EvPerson.define_events()")
        assert str(remove_event.value) == _x1("EvPerson.remove_event()")
        assert str(define_event.value) == _x1m("define_event()")
        assert str(manager_remove_event.value) == _x1m("remove_event()")


#: A record access on ``evt_db``: a user signed up through it is a record user, who may not define (M22).
_EVT_ACCESS = (
    "DEFINE ACCESS evt_acc ON DATABASE TYPE RECORD SIGNUP (CREATE evt_user SET name = $name) "
    "SIGNIN (SELECT * FROM evt_user WHERE name = $name);"
)


class TestDdlPermissionsE2E:
    """AC-12: a session that may not define — a record user — gets S1 (``SurrealDbAuthenticationError``) for events
    and stored functions on both server lines; the embedded engine's session is its owner, which may always define."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_s1_a_record_user(self, kind: str) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always define")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await _apply_ddl(client, _EVT_ACCESS)
            await SurrealDBConnectionManager.signup(access="evt_acc", variables={"name": "evt-user"})
            try:
                with pytest.raises(SurrealDbError) as define_events:
                    await EvPerson.define_events()
                with pytest.raises(SurrealDbError) as remove_event:
                    await EvPerson.remove_event("ev_audit")
                with pytest.raises(SurrealDbError) as define_event:
                    await SurrealDBConnectionManager.define_event(_EV_AUDIT, "ev_person")
                with pytest.raises(SurrealDbError) as manager_remove_event:
                    await SurrealDBConnectionManager.remove_event("ev_audit", "ev_person")
            finally:
                await SurrealDBConnectionManager.invalidate()
            assert await _events_info(client, "ev_person") == {}
        removal = "REMOVE EVENT IF EXISTS `ev_audit` ON `ev_person`;"
        first = EvPerson.get_events()[0].name
        refusals = [
            (define_events, f"Can't apply event definition: '{first}' on table 'ev_person' -> ", EvPerson.event_ddl()[0]),
            (remove_event, "Can't apply event removal: 'ev_audit' on table 'ev_person' -> ", removal),
            (define_event, "Can't apply event definition: 'ev_audit' on table 'ev_person' -> ", _EV_AUDIT.ddl("ev_person")),
            (manager_remove_event, "Can't apply event removal: 'ev_audit' on table 'ev_person' -> ", removal),
        ]
        for raised, prefix, statement in refusals:
            assert type(raised.value) is SurrealDbAuthenticationError
            assert str(raised.value).startswith(prefix)
            assert "not enough permissions" in str(raised.value).lower()
            assert statement not in str(raised.value)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_s1_a_record_user_on_functions(self, kind: str) -> None:
        if kind == "mem":
            pytest.skip("the embedded engine's session is its owner: it may always define")
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await _apply_ddl(client, _EVT_ACCESS)
            await SurrealDBConnectionManager.signup(access="evt_acc", variables={"name": "evt-user"})
            try:
                with pytest.raises(SurrealDbError) as define_function:
                    await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
                with pytest.raises(SurrealDbError) as remove_function:
                    await SurrealDBConnectionManager.remove_function(_GREET)
            finally:
                await SurrealDBConnectionManager.invalidate()
            assert "ev_greet" not in (await client.query("INFO FOR DB;", {}))["functions"]
        refusals = [
            (
                define_function,
                "Can't apply function definition: 'fn::ev_greet' -> ",
                function_ddl(*_GREET_ARGS, **_GREET_KWARGS),
            ),
            (remove_function, "Can't apply function removal: 'fn::ev_greet' -> ", _GREET_REMOVAL),
        ]
        for raised, prefix, statement in refusals:
            assert type(raised.value) is SurrealDbAuthenticationError
            assert str(raised.value).startswith(prefix)
            assert "not enough permissions" in str(raised.value).lower()
            assert statement not in str(raised.value)


async def _notifications(stream: Any, n: int) -> list[Any]:
    """The next *n* notifications of *stream*, within ten seconds."""
    received: list[Any] = []
    async with asyncio.timeout(10):
        while len(received) < n:
            received.append(await anext(stream))
    return received


@pytest.fixture
def _fresh_query_cache() -> Iterator[None]:
    """The query cache's default settings and an empty store around the test: the cache is process-wide."""
    QueryCache.configure()
    QueryCache.clear()
    yield
    QueryCache.configure()
    QueryCache.clear()


class TestEventLiveAndCacheE2E:
    """AC-16: a row an event writes reaches a live query on its table (M23), and is not seen by the query cache's
    invalidation, which covers the tables the ORM itself writes — ``QueryCache.invalidate()`` makes it visible."""

    async def test_watch_sees_the_rows_an_event_writes(self) -> None:
        async with _evt("ws"):
            await EvPerson.define_events()
            async with EvLog.objects().watch() as stream:
                await EvPerson(id="ada", name="Ada").save()
                received = await _notifications(stream, 2)
        assert {notification["action"] for notification in received} == {LiveAction.CREATE}
        assert sorted(notification["result"]["kind"] for notification in received) == ["CREATE", "upper"]

    @pytest.mark.usefixtures("_fresh_query_cache")
    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_cached_read_stays_stale_until_invalidated(self, kind: str) -> None:
        async with _evt(kind):
            await EvPerson.define_events()
            assert await EvLog.objects().cache().exec() == []
            await EvPerson(id="ada", name="Ada").save()
            hits = QueryCache.stats()["hits"]
            assert await EvLog.objects().cache().exec() == []
            assert QueryCache.stats()["hits"] == hits + 1
            assert QueryCache.invalidate(EvLog) == 1
            rows = await EvLog.objects().cache().exec()
        assert sorted(row.kind for row in rows) == ["CREATE", "upper"]


#: What ``remove_function()`` applies for ``fn::ev_greet`` by default (``missing_ok=True``).
_GREET_REMOVAL = "REMOVE FUNCTION IF EXISTS fn::ev_greet;"

#: ``fn::ev_pair`` with its two string parameters, as ``(function, body)``: bound in the wrong order, it answers
#: ``"B/A"`` instead of ``"A/B"`` without any error.
_PAIR = ("fn::ev_pair", "RETURN string::concat($a, '/', $b);")


@contextlib.asynccontextmanager
async def _evt_admin(kind: str) -> AsyncIterator[Any]:
    """A second connection ``"evt_admin"`` to ``evt_db`` over the other transport than *kind* (``ws`` or ``http``),
    as an application that applies DDL over HTTP and runs live queries over WebSocket would; its client, and the
    connection unregistered on exit."""
    SurrealDBConnectionManager.add_connection(
        "evt_admin",
        url=_url("http" if kind == "ws" else "ws"),
        user="root",
        password="root",
        namespace="ns",
        database="evt_db",
    )
    try:
        yield await SurrealDBConnectionManager.get_client("evt_admin")
    finally:
        await SurrealDBConnectionManager.remove_connection("evt_admin")


def _hold_signature_read(monkeypatch: pytest.MonkeyPatch, client: Any) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold back the answer to *client*'s next ``INFO FOR DB``: the first event is set once the server has answered,
    and the answer reaches its caller once the second one is set. Every later read goes straight through."""
    real = client.query
    answered, release = asyncio.Event(), asyncio.Event()

    async def query(statement: str, *args: Any, **kwargs: Any) -> Any:
        result = await real(statement, *args, **kwargs)
        if statement.startswith("INFO FOR DB") and not release.is_set():
            answered.set()
            await release.wait()
        return result

    monkeypatch.setattr(client, "query", query)
    return answered, release


def _evt_signatures() -> dict[tuple[str | None, str | None, str | None, str], tuple[str, ...]]:
    """The ``params=`` signature cache of the connection ``"evt"`` (white box)."""
    manager = SurrealDBConnectionManager._manager_for("evt")
    return manager._SurrealDBConnectionManager__function_signatures  # type: ignore[attr-defined,no-any-return]


class TestFunctionsE2E:
    """AC-10, AC-8d, AC-9 (functions) and AC-15 (i, functions): stored functions defined, called, redefined and
    removed through the manager on every engine — the cached signature dropped by ``define_function()`` and
    ``remove_function()``, and again after a transaction commits."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_define_then_call(self, kind: str) -> None:
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            statement = await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
            assert statement == function_ddl(*_GREET_ARGS, **_GREET_KWARGS)
            assert sorted((await client.query("INFO FOR DB;", {}))["functions"]) == ["ev_greet"]
            # A trailing option<…> parameter may be left out (M20).
            assert await SurrealDBConnectionManager.call_function(_GREET, ["Ada"]) == "Hello Ada"
            assert await SurrealDBConnectionManager.call_function("ev_greet", ["Bea", 2]) == "Hello Bea"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_reorder_is_picked_up_at_once(self, kind: str) -> None:
        # Both keys in every params= call: the key sets match, so a stale cached order would not be noticed and the
        # call would bind Ada to $times. Only the signature define_function() drops makes the new order read.
        both = {"name": "Ada", "times": 1}
        async with _evt(kind), SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
            assert await SurrealDBConnectionManager.call_function(_GREET, params=both) == "Hello Ada"
            assert SurrealDBConnectionManager.function_signature_cache_size() == 1

            await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_REORDERED)
            assert SurrealDBConnectionManager.function_signature_cache_size() == 0
            assert await SurrealDBConnectionManager.call_function(_GREET, params=both) == "Hello Ada"
            assert await SurrealDBConnectionManager.call_function(_GREET, [1, "Bea"]) == "Hello Bea"

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_reorder_reaches_every_connection_to_the_database(self, kind: str) -> None:
        # The DDL runs on a second connection to evt_db (an admin one, say), set up with another URL for the same
        # server — the other transport, as an application that applies DDL over HTTP and runs live queries over
        # WebSocket would —; "evt" had cached the old order. Two string parameters: a stale order binds without any
        # error, "B/A" instead of "A/B". The default connection, on another database, keeps its own cached signature
        # of a function with the same name. Not on mem://, where each connection is an engine of its own.
        pair = ("fn::ev_pair", "RETURN string::concat($a, '/', $b);")
        both = {"a": "A", "b": "B"}
        async with _default(kind), _evt(kind):
            SurrealDBConnectionManager.add_connection(
                "evt_admin",
                url=_url("http" if kind == "ws" else "ws"),
                user="root",
                password="root",
                namespace="ns",
                database="evt_db",
            )
            try:
                await SurrealDBConnectionManager.define_function(*pair, parameters={"a": "string", "b": "string"})
                assert await SurrealDBConnectionManager.call_function(pair[0], params=both) == "A/B"
                async with SurrealDBConnectionManager.using("evt_admin"):
                    await SurrealDBConnectionManager.define_function(*pair, parameters={"a": "string", "b": "string"})
                async with SurrealDBConnectionManager.using("evt"):
                    assert await SurrealDBConnectionManager.call_function(pair[0], params=both) == "A/B"
                    assert SurrealDBConnectionManager.function_signature_cache_size() == 1

                async with SurrealDBConnectionManager.using("evt_admin"):
                    await SurrealDBConnectionManager.define_function(*pair, parameters={"b": "string", "a": "string"})
                async with SurrealDBConnectionManager.using("evt"):
                    assert SurrealDBConnectionManager.function_signature_cache_size() == 0
                    assert await SurrealDBConnectionManager.call_function(pair[0], params=both) == "A/B"
                    assert await SurrealDBConnectionManager.call_function(pair[0], ["B", "A"]) == "A/B"
                assert SurrealDBConnectionManager.function_signature_cache_size() == 1

                async with SurrealDBConnectionManager.using("evt_admin"):
                    await SurrealDBConnectionManager.remove_function(pair[0])
                async with SurrealDBConnectionManager.using("evt"):
                    assert SurrealDBConnectionManager.function_signature_cache_size() == 0
                    with pytest.raises(SurrealDbNotFoundError):
                        await SurrealDBConnectionManager.call_function(pair[0], params=both)
                assert SurrealDBConnectionManager.function_signature_cache_size() == 1
            finally:
                await SurrealDBConnectionManager.remove_function(pair[0])
                await SurrealDBConnectionManager.remove_connection("evt_admin")

    # A params= call whose INFO FOR DB answer (the old order) is still on its way back while the function is reordered
    # and its signature dropped: the read is overtaken, so it is read again — the call in flight binds in the new
    # order, and the old one never lands in the cache, where every later call would have bound in it ("B/A"). The
    # answer is held back by wrapping the application client's query(), never by sleeping.

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_reorder_by_another_connection_overtakes_a_signature_read(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        both = {"a": "A", "b": "B"}
        async with _evt(kind) as client, _evt_admin(kind), SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_PAIR, parameters={"a": "string", "b": "string"})
            answered, release = _hold_signature_read(monkeypatch, client)
            in_flight = asyncio.create_task(SurrealDBConnectionManager.call_function(_PAIR[0], params=both))
            async with asyncio.timeout(10):
                await answered.wait()
            async with SurrealDBConnectionManager.using("evt_admin"):
                await SurrealDBConnectionManager.define_function(*_PAIR, parameters={"b": "string", "a": "string"})
            release.set()
            assert await in_flight == "A/B"
            assert await SurrealDBConnectionManager.call_function(_PAIR[0], params=both) == "A/B"
            assert list(_evt_signatures().values()) == [("b", "a")]

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_reorder_committed_by_a_transaction_overtakes_a_signature_read(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The read starts inside the admin's transaction, so it answers the committed schema — the old order — and
        # only the signature dropped once the transaction commits can overtake it. The call runs in the context
        # copied before using("evt_admin"): created inside the block, it would run on the admin connection.
        both = {"a": "A", "b": "B"}
        async with _evt(kind) as client, _evt_admin(kind), SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_PAIR, parameters={"a": "string", "b": "string"})
            answered, release = _hold_signature_read(monkeypatch, client)
            application = contextvars.copy_context()
            async with SurrealDBConnectionManager.using("evt_admin"), SurrealDBConnectionManager.transaction() as tx:
                await SurrealDBConnectionManager.define_function(*_PAIR, parameters={"b": "string", "a": "string"}, tx=tx)
                in_flight = application.run(
                    lambda: asyncio.create_task(SurrealDBConnectionManager.call_function(_PAIR[0], params=both))
                )
                async with asyncio.timeout(10):
                    await answered.wait()
            release.set()
            assert await in_flight == "A/B"
            assert await SurrealDBConnectionManager.call_function(_PAIR[0], params=both) == "A/B"
            assert list(_evt_signatures().values()) == [("b", "a")]

    @pytest.mark.parametrize("kind", ["ws", "http"])
    async def test_a_cache_clear_after_raw_ddl_overtakes_a_signature_read(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        both = {"a": "A", "b": "B"}
        async with _evt(kind) as client, _evt_admin(kind) as admin, SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_PAIR, parameters={"a": "string", "b": "string"})
            answered, release = _hold_signature_read(monkeypatch, client)
            in_flight = asyncio.create_task(SurrealDBConnectionManager.call_function(_PAIR[0], params=both))
            async with asyncio.timeout(10):
                await answered.wait()
            await _apply_ddl(admin, f"DEFINE FUNCTION OVERWRITE {_PAIR[0]}($b: string, $a: string) {{ {_PAIR[1]} }};")
            SurrealDBConnectionManager.clear_function_signature_cache()
            release.set()
            assert await in_flight == "A/B"
            assert await SurrealDBConnectionManager.call_function(_PAIR[0], params=both) == "A/B"
            assert list(_evt_signatures().values()) == [("b", "a")]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_remove_function(self, kind: str) -> None:
        both = {"name": "Ada", "times": 1}
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
            assert await SurrealDBConnectionManager.call_function(_GREET, params=both) == "Hello Ada"
            assert SurrealDBConnectionManager.function_signature_cache_size() == 1

            assert await SurrealDBConnectionManager.remove_function(_GREET) == _GREET_REMOVAL
            assert SurrealDBConnectionManager.function_signature_cache_size() == 0
            assert (await client.query("INFO FOR DB;", {}))["functions"] == {}
            with pytest.raises(SurrealDbNotFoundError):
                await SurrealDBConnectionManager.call_function(_GREET, ["Ada"])
            with pytest.raises(SurrealDbNotFoundError):
                await SurrealDBConnectionManager.call_function(_GREET, params=both)

            # IF EXISTS: a second removal passes; without it, a missing function is S2.
            assert await SurrealDBConnectionManager.remove_function("ev_greet") == _GREET_REMOVAL
            with pytest.raises(SurrealDbNotFoundError) as raised:
                await SurrealDBConnectionManager.remove_function(_GREET, missing_ok=False)
        assert str(raised.value).startswith("Can't apply function removal: 'fn::ev_greet' -> ")
        assert "does not exist" in str(raised.value)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_overwrite_false_keeps_the_old_body(self, kind: str) -> None:
        async with _evt(kind), SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(_GREET, "RETURN 'old';")
            statement = await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS, overwrite=False)
            assert statement == function_ddl(*_GREET_ARGS, **_GREET_KWARGS, overwrite=False)
            assert statement.startswith("DEFINE FUNCTION IF NOT EXISTS fn::ev_greet(")
            assert await SurrealDBConnectionManager.call_function(_GREET) == "old"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_server_refusal_is_s3(self, kind: str) -> None:
        # Well formed for the ORM, refused by the server's parser.
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            with pytest.raises(SurrealDbError) as raised:
                await SurrealDBConnectionManager.define_function("fn::ev_broken", "RETURN")
            assert (await client.query("INFO FOR DB;", {}))["functions"] == {}
        assert type(raised.value) is SurrealDbError
        assert str(raised.value).startswith("Can't apply function definition: 'fn::ev_broken' -> ")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_refusal_never_quotes_the_definition(self, kind: str) -> None:
        # Review of PR #273: the server's excerpt quotes the secret the body holds; the ORM's error does not.
        async with _evt(kind), SurrealDBConnectionManager.using("evt"):
            with pytest.raises(SurrealDbError) as raised:
                await SurrealDBConnectionManager.define_function("fn::ev_hook", f"RETURN <{_SECRET}> 1")
        assert type(raised.value) is SurrealDbError
        assert str(raised.value).startswith("Can't apply function definition: 'fn::ev_hook' -> ")
        assert _SECRET in str(raised.value.__context__)
        assert _leaks(raised.value, _SECRET) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_redefinition_in_a_transaction_is_read_after_the_commit(self, kind: str) -> None:
        # D11: params= resolves against the committed schema, so a call inside the block reads and caches the old
        # order again; the commit drops it once more, and the next call reads the new order.
        async with _evt(kind), SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
            assert await SurrealDBConnectionManager.call_function(_GREET, params={"name": "Ada", "times": 1}) == "Hello Ada"
            async with SurrealDBConnectionManager.transaction() as tx:
                statement = await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_REORDERED, tx=tx)
                assert statement == function_ddl(*_GREET_ARGS, **_GREET_REORDERED)
                assert SurrealDBConnectionManager.function_signature_cache_size() == 0
                both = {"name": "Bea", "times": 1}
                assert await SurrealDBConnectionManager.call_function(_GREET, params=both) == "Hello Bea"
                assert SurrealDBConnectionManager.function_signature_cache_size() == 1
            assert SurrealDBConnectionManager.function_signature_cache_size() == 0
            assert await SurrealDBConnectionManager.call_function(_GREET, params={"name": "Cy", "times": 1}) == "Hello Cy"
            assert await SurrealDBConnectionManager.call_function(_GREET, [1, "Dee"]) == "Hello Dee"

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_removal_in_a_transaction_drops_the_signature_after_the_commit(self, kind: str) -> None:
        both = {"name": "Ada", "times": 1}
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
            async with SurrealDBConnectionManager.transaction() as tx:
                assert await SurrealDBConnectionManager.remove_function(_GREET, tx=tx) == _GREET_REMOVAL
                # Still defined outside the block until it commits: params= reads and caches its signature.
                assert await SurrealDBConnectionManager.call_function(_GREET, params=both) == "Hello Ada"
                assert SurrealDBConnectionManager.function_signature_cache_size() == 1
            assert SurrealDBConnectionManager.function_signature_cache_size() == 0
            assert (await client.query("INFO FOR DB;", {}))["functions"] == {}
            with pytest.raises(SurrealDbNotFoundError):
                await SurrealDBConnectionManager.call_function(_GREET, params=both)

    async def test_a_function_defined_in_an_interactive_transaction(self) -> None:
        # AC-8d. Interactive transactions: WebSocket on 3.x. The function is callable inside the block with
        # positional args; after the commit, params= reads its new signature.
        async with _evt("ws") as client, SurrealDBConnectionManager.using("evt"):
            await _require_line(client, 3, "interactive transactions")
            await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS)
            assert await SurrealDBConnectionManager.call_function(_GREET, params={"name": "Ada", "times": 1}) == "Hello Ada"
            async with SurrealDBConnectionManager.transaction() as tx:
                assert tx.is_interactive
                await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_REORDERED, tx=tx)
                assert await SurrealDBConnectionManager.call_function(_GREET, [1, "Bea"], tx=tx) == "Hello Bea"
                await SurrealDBConnectionManager.define_function("fn::ev_new", "RETURN 7;", tx=tx)
                assert await SurrealDBConnectionManager.call_function("fn::ev_new", tx=tx) == 7
                # Invisible outside the transaction until it commits (M14).
                with pytest.raises(SurrealDbNotFoundError):
                    await SurrealDBConnectionManager.call_function("fn::ev_new")
            assert await SurrealDBConnectionManager.call_function(_GREET, params={"name": "Cy", "times": 1}) == "Hello Cy"
            assert await SurrealDBConnectionManager.call_function("fn::ev_new", params={}) == 7

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_cancelled_block_defines_nothing(self, kind: str) -> None:
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            with pytest.raises(_Abort):
                async with SurrealDBConnectionManager.transaction() as tx:
                    await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS, tx=tx)
                    raise _Abort
            assert (await client.query("INFO FOR DB;", {}))["functions"] == {}

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_x1m_a_transaction_of_another_connection(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        async with _default(kind) as default, _evt(kind) as client:
            async with SurrealDBConnectionManager.transaction() as tx:
                evt_calls = _spy(monkeypatch, client)
                default_calls = _spy(monkeypatch, default)
                async with SurrealDBConnectionManager.using("evt"):
                    with pytest.raises(SurrealDbError) as define_function:
                        await SurrealDBConnectionManager.define_function(*_GREET_ARGS, **_GREET_KWARGS, tx=tx)
                    with pytest.raises(SurrealDbError) as remove_function:
                        await SurrealDBConnectionManager.remove_function(_GREET, tx=tx)
                assert evt_calls == []
                assert default_calls == []
                assert getattr(tx, "statements", []) == []
            assert (await client.query("INFO FOR DB;", {}))["functions"] == {}
        assert str(define_function.value) == _x1m("define_function()")
        assert str(remove_function.value) == _x1m("remove_function()")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_arguments_are_refused_before_any_request(self, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        # AC-15 (i): parameter names (F5), a function name (F1) and E13, each before anything reaches the server.
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            calls = _spy(monkeypatch, client)
            for bad in ["a;b", "$a", "a b"]:
                with pytest.raises(ValueError) as raised:
                    await SurrealDBConnectionManager.define_function("fn::x", "RETURN 1;", parameters={bad: "int"})
                assert str(raised.value) == _F5.format(bad)
            with pytest.raises(ValueError) as raised:
                await SurrealDBConnectionManager.define_function("fn::x;y", "RETURN 1;")
            assert str(raised.value) == _INVALID_FUNCTION.format("fn::x;y")
            with pytest.raises(ValueError) as raised:
                await SurrealDBConnectionManager.define_function("fn::x", "RETURN 1;", parameters={"a": "file<x; y>"})
            assert str(raised.value) == _kind_error("the kind of parameter 'a'")
            with pytest.raises(ValueError) as raised:
                await SurrealDBConnectionManager.define_function("fn::x", "RETURN 1;", returns="file<x; y>")
            assert str(raised.value) == _kind_error("returns=")
            with pytest.raises(ValueError) as raised:
                await SurrealDBConnectionManager.remove_function("fn::x;y")
            assert str(raised.value) == _INVALID_FUNCTION.format("fn::x;y")
            with pytest.raises(TypeError) as raised:
                await SurrealDBConnectionManager.define_function(*_GREET_ARGS, overwrite=1)  # type: ignore[arg-type]
            assert str(raised.value) == "overwrite= must be a bool, got int."
            with pytest.raises(TypeError) as raised:
                await SurrealDBConnectionManager.remove_function(_GREET, missing_ok="yes")  # type: ignore[arg-type]
            assert str(raised.value) == "missing_ok= must be a bool, got str."
            assert calls == []


#: The stored functions of spec §5.2, as ``(args, kwargs)`` of ``define_function()``.
_FUNCTIONS = [
    (_GREET_ARGS, _GREET_KWARGS),
    (("ev_guarded", "RETURN 1;"), {"permissions": "where $auth.admin = true"}),
    (
        ("fn::ev_obj", "RETURN $a;"),
        {"parameters": {"a": "{ x: int }"}, "returns": "{ x: int }", "permissions": "none", "overwrite": False},
    ),
]

#: ``INFO FOR DB``'s ``functions`` once ``_FUNCTIONS`` are applied (spec §3): SurrealDB 3.x drops the ``;`` of a
#: single-statement body and renders ``option<int>`` as ``none | int``.
_FUNCTIONS_INFO_3X = {
    "ev_greet": "DEFINE FUNCTION fn::ev_greet($name: string, $times: none | int) -> string "
    "{ RETURN string::concat('Hello ', $name) } COMMENT 'Greets' PERMISSIONS FULL",
    "ev_guarded": "DEFINE FUNCTION fn::ev_guarded() { RETURN 1 } PERMISSIONS WHERE $auth.admin = true",
    "ev_obj": "DEFINE FUNCTION fn::ev_obj($a: { x: int }) -> { x: int } { RETURN $a } PERMISSIONS NONE",
}
#: The same on SurrealDB 2.x and the embedded engine.
_FUNCTIONS_INFO_2X = {
    "ev_greet": "DEFINE FUNCTION fn::ev_greet($name: string, $times: option<int>) -> string "
    "{ RETURN string::concat('Hello ', $name); } COMMENT 'Greets' PERMISSIONS FULL",
    "ev_guarded": "DEFINE FUNCTION fn::ev_guarded() { RETURN 1; } PERMISSIONS WHERE $auth.admin = true",
    "ev_obj": "DEFINE FUNCTION fn::ev_obj($a: { x: int }) -> { x: int } { RETURN $a; } PERMISSIONS NONE",
}

#: ``then`` of each ``EvPerson`` event as SurrealDB 3.x reads it back (spec §3): one line, a single statement
#: without its ``;``, several each followed by one.
_THEN_3X = {
    "ev_audit": "{ CREATE ev_log SET kind = $event, rid = $value.id }",
    "ev_rename": "{ CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name }",
    "ev_guard": "{ THROW 'name refused: ' + $after.name }",
    "ev_upper": "{ LET $n = string::uppercase($after.name); CREATE ev_log SET kind = 'upper', name = $n; }",
}
#: The same on SurrealDB 2.x and the embedded engine: every ``;`` kept, several statements on separate lines.
_THEN_2X = {
    "ev_audit": "{ CREATE ev_log SET kind = $event, rid = $value.id; }",
    "ev_rename": "{ CREATE ev_log SET kind = 'rename', old = $before.name, new = $after.name; }",
    "ev_guard": "{ THROW 'name refused: ' + $after.name; }",
    "ev_upper": "{\nLET $n = string::uppercase($after.name);\nCREATE ev_log SET kind = 'upper', name = $n;\n}",
}


def _event_fields(event: Any) -> tuple[Any, ...]:
    """What AC-11 compares of an ``EventState``."""
    return (
        event.name,
        event.table,
        event.when,
        event.then,
        event.comment,
        event.async_mode,
        event.retry,
        event.max_depth,
    )


class TestRoundTripE2E:
    """AC-11 and AC-14: the events and stored functions of spec §5.2, applied through the ORM, read back by the
    schema introspection (v0.30.0) as they were defined — each line with its own rendering of a body (spec §3)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_events_and_functions_read_back(self, kind: str) -> None:
        async with _evt(kind) as client, SurrealDBConnectionManager.using("evt"):
            major = await _major(client)
            await EvPerson.define_events()
            if major >= 3:
                await EvAsyncPerson.define_events()
            for args, kwargs in _FUNCTIONS:
                await SurrealDBConnectionManager.define_function(*args, **kwargs)
            info = await client.query("INFO FOR DB;", {})
            schema = await DatabaseIntrospector("evt", tables=["ev_person"]).introspect()
        assert schema.issues == ()
        then = _THEN_3X if major >= 3 else _THEN_2X
        expected = {
            "ev_audit": ("ev_audit", "ev_person", "true", then["ev_audit"], None, False, None, None),
            "ev_rename": (
                "ev_rename",
                "ev_person",
                "$event = 'UPDATE' AND $before.name != $after.name",
                then["ev_rename"],
                "it's renamed",
                False,
                None,
                None,
            ),
            "ev_guard": ("ev_guard", "ev_person", "$after.name = 'forbidden'", then["ev_guard"], None, False, None, None),
            "ev_upper": ("ev_upper", "ev_person", "$event = 'CREATE'", then["ev_upper"], None, False, None, None),
        }
        if major >= 3:
            expected["ev_async"] = (
                "ev_async",
                "ev_person",
                "$event = 'CREATE'",
                "{ CREATE ev_log SET kind = 'async', rid = $value.id }",
                None,
                True,
                2,
                3,
            )
        events = schema.tables["ev_person"].events
        assert {name: _event_fields(event) for name, event in events.items()} == expected

        assert dict(info["functions"]) == (_FUNCTIONS_INFO_3X if major >= 3 else _FUNCTIONS_INFO_2X)
        functions = {
            name: (function.parameters, function.returns, function.permissions, function.comment)
            for name, function in schema.functions.items()
        }
        assert functions == {
            "fn::ev_greet": (("name", "times"), "string", "FULL", "Greets"),
            "fn::ev_guarded": ((), None, "WHERE $auth.admin = true", None),
            "fn::ev_obj": (("a",), "{ x: int }", "NONE", None),
        }

    async def test_async_event_is_read(self) -> None:
        # M13: v0.30.0 reported an ASYNC event as an issue; it is now read, RETRY and MAXDEPTH included.
        async with _evt("ws") as client:
            await _require_line(client, 3, "ASYNC events")
            await _apply_ddl(
                client,
                "DEFINE EVENT ev_async ON ev_person ASYNC RETRY 2 WHEN $event = 'CREATE' "
                "THEN { CREATE ev_log SET kind = 'async', rid = $value.id };",
            )
            schema = await DatabaseIntrospector("evt", tables=["ev_person"]).introspect()
        assert schema.issues == ()
        event = schema.tables["ev_person"].events["ev_async"]
        assert _event_fields(event) == (
            "ev_async",
            "ev_person",
            "$event = 'CREATE'",
            "{ CREATE ev_log SET kind = 'async', rid = $value.id }",
            None,
            True,
            2,
            3,
        )
        assert event.raw == _EV_ASYNC_INFO_3X


# --- README: section 32, section 17 and the tables (spec §11.1; AC-17) ----------------------------------------------

_SECTION_17 = "### 17. Stored functions: `call_function()`"
_SECTION_31 = "### 31. Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
_SECTION_32 = "### 32. Server-side events and stored functions (`Event`, `define_events()`, `define_function()`)"
#: Spec §11.1 item 1: section 32's subsections, in order, after an untitled intro paragraph.
_SUBSECTIONS_32 = [
    "Declaring events on a model",
    "Events without a model",
    "Asynchronous events (SurrealDB 3.x)",
    "Stored functions: `define_function()`",
    "Transactions, connections and the query cache",
    "SurrealDB 2.x and 3.x",
    "Security",
    "Coming from the full SurrealDB-ORM",
]
#: Spec §11.1 item 1, example A, exactly: it opens "Declaring events on a model".
_EXAMPLE_A = """\
from surrealdb import ServerError

from surreal_orm_lite import BaseSurrealModel, Event, SurrealConfigDict, SurrealDBConnectionManager


class Account(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="account",
        events=[
            Event(
                "email_audit",
                when="$event = 'UPDATE' AND $before.email != $after.email",
                then="CREATE audit_log SET account = $value.id, old = $before.email, new = $after.email",
                comment="Keeps every e-mail change",
            ),
            Event(
                "no_negative_balance",
                when="$event != 'DELETE' AND $after.balance < 0",
                then="THROW 'balance cannot be negative'",
            ),
        ],
    )

    id: str | None = None
    email: str
    balance: int = 0


for statement in Account.event_ddl():
    print(statement)
# DEFINE EVENT OVERWRITE `email_audit` ON `account` WHEN $event = 'UPDATE' AND $before.email != $after.email THEN { CREATE audit_log SET account = $value.id, old = $before.email, new = $after.email } COMMENT 'Keeps every e-mail change';
# DEFINE EVENT OVERWRITE `no_negative_balance` ON `account` WHEN $event != 'DELETE' AND $after.balance < 0 THEN { THROW 'balance cannot be negative' };

await Account.define_events()  # idempotent (OVERWRITE): run it at start-up

account = Account(id="ada", email="ada@old.example", balance=10)
await account.save()
await account.merge(email="ada@new.example")
client = await SurrealDBConnectionManager.get_client()
print(await client.query("SELECT old, new FROM audit_log;", {}))
# [{'new': 'ada@new.example', 'old': 'ada@old.example'}]

try:
    await account.merge(balance=-5)
except ServerError as exc:  # the event refused the write: nothing changed
    print(type(exc).__name__)
# InternalError
"""
#: Example B, exactly: it opens "Events without a model".
_EXAMPLE_B = """\
stock_guard = Event(
    "stock_guard", when="$event != 'DELETE' AND $after.stock < 0", then="THROW 'stock cannot go below zero'"
)
print(await SurrealDBConnectionManager.define_event(stock_guard, "product"))
# DEFINE EVENT OVERWRITE `stock_guard` ON `product` WHEN $event != 'DELETE' AND $after.stock < 0 THEN { THROW 'stock cannot go below zero' };

await SurrealDBConnectionManager.remove_event("stock_guard", "product")
await Account.remove_event("no_negative_balance")  # declared or not: removal is by name
"""
#: Example C, exactly: it opens "Asynchronous events (SurrealDB 3.x)"; on ``mem://`` it prints C1.
_EXAMPLE_C = """\
from surreal_orm_lite import SurrealDbError

notify = Event(
    "notify_shipping",
    when="$event = 'CREATE'",
    then="CREATE outbox SET shipment = $value.id, at = time::now()",
    async_mode=True,
    retry=3,
)
print(notify.ddl("shipment"))
# DEFINE EVENT OVERWRITE `notify_shipping` ON `shipment` ASYNC RETRY 3 WHEN $event = 'CREATE' THEN { CREATE outbox SET shipment = $value.id, at = time::now() };

try:
    await SurrealDBConnectionManager.define_event(notify, "shipment")
except SurrealDbError as exc:  # the embedded engine is a 2.x core, like a SurrealDB 2.x server
    print(exc)
# ASYNC events need SurrealDB 3.x, and this connection runs the embedded engine (a SurrealDB 2.x core): 'notify_shipping' cannot be defined with async_mode=True. Drop async_mode, retry and max_depth, or connect to a SurrealDB 3.x server. Nothing was defined.
"""
#: Example D, exactly: it opens "Stored functions: `define_function()`".
_EXAMPLE_D = """\
await SurrealDBConnectionManager.define_function(
    "fn::greet",
    "RETURN string::concat('Hello, ', $name, '!');",
    parameters={"name": "string"},
    returns="string",
    comment="Says hello",
)
print(await SurrealDBConnectionManager.call_function("fn::greet", ["Ada"]))
# Hello, Ada!

await SurrealDBConnectionManager.define_function(
    "fn::greet",
    "RETURN string::concat($greeting, ', ', $name, '!');",
    parameters={"greeting": "string", "name": "string"},
    returns="string",
)
print(await SurrealDBConnectionManager.call_function("fn::greet", params={"name": "Ada", "greeting": "Hi"}))
# Hi, Ada!

await SurrealDBConnectionManager.remove_function("fn::greet")
"""
_EXAMPLES_32 = [_EXAMPLE_A, _EXAMPLE_B, _EXAMPLE_C, _EXAMPLE_D]
#: Spec §8 and §11.1 item 1 (D18, binding): the sentence the "Security" subsection carries, whitespace normalised.
_D18_SENTENCE = (
    "The check catches mistakes — unbalanced brackets, a comment that would swallow the clauses the ORM appends, a "
    "second statement where one expression is expected — and is not an injection defence: expressions are trusted "
    "application code, never user input."
)
#: Spec §11.1 item 1: what the intro and each subsection must mention.
_INTRO_NEEDLES_32 = (
    "`SurrealConfigDict(events=[…])`",
    "`call_function()`",
    "section 31",
    "`$input`",
    "2.x",
    "3.x",
    "`mem://`",
)
_PART_NEEDLES_32 = {
    "Declaring events on a model": (
        "`{ … }`",
        "`LET`",
        "`THROW`",
        "`WHEN true`",
        "`$event`",
        "`$before`",
        "`$after`",
        "`$value`",
        "`$input`",
        "columns",
        "`Field(alias=…)`",
        "`update`",
        "`DEFINE EVENT OVERWRITE`",
        "`overwrite=False`",
        "`IF NOT EXISTS`",
        "`tx=`",
        "`event_ddl()`",
        '`Account.remove_event("email_audit")`',
        "`missing_ok=False`",
        "`SurrealDbNotFoundError`",
        "v0.37.0",
        "`BaseSurrealEdge`",
        "`SCHEMALESS`",
        "`InternalError`",
        "signal",
    ),
    "Events without a model": (
        "`SurrealDBConnectionManager.define_event(event, table)`",
        "`remove_event(event, table)`",
        "`using()`",
        '`"default"`',
        "`event.ddl(table)`",
        "`repr()`",
        "Without `tx=`, a refusal leaves them out too",
        "`Can't apply event definition: 'stock_guard' on table 'product' -> …`",
        "excerpt of the statement",
    ),
    "Asynchronous events (SurrealDB 3.x)": (
        "`ASYNC`",
        "`RETRY`",
        "`MAXDEPTH`",
        "commits",
        "cancelled",
        "`retry`",
        "`max_depth`",
        "`0` to `65535`",
        "`async_mode=True`",
        "`EventState`",
        "`SurrealDbError`",
        "before sending anything",
        "`event.ddl()`",
    ),
    "Stored functions: `define_function()`": (
        "`call_function()`",
        '`"fn::billing::total"`',
        "positional",
        "canonical form",
        "`option<int>`",
        "`none | int`",
        "never rewritten",
        "`returns`",
        '`"FULL"`',
        '`"NONE"`',
        '`"WHERE <condition>"`',
        "`remove_function()`",
        "`function_ddl()`",
        "`clear_function_signature_cache()`",
        "Without `tx=`, a body the server refuses",
        "`Can't apply function definition: 'fn::greet' -> …`",
        "never its body",
    ),
    "Transactions, connections and the query cache": (
        "`tx=`",
        "buffered",
        "interactive",
        "a block that raises leaves nothing defined",
        "positional `args`",
        "`connection`",
        "`using()`",
        "system user",
        "`SurrealDbAuthenticationError`",
        "only queues the statement",
        "the transaction's own error",
        "`missing_ok=False` included",
        "without the server's excerpt of the statement (v0.31.2, [#278](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/278))",
        "`watch()`",
        "`QueryCache.invalidate(Model)`",
        "signal",
    ),
    "SurrealDB 2.x and 3.x": (
        "`ASYNC`",
        "`$input`",
        "`NONE`",
        "`upsert()`",
        "`UPDATE`",
        "`none | int`",
        "`option<int>`",
    ),
    "Security": (
        "identifiers",
        "quoted",
        "escaped",
        "`--`",
        "`#`",
        "`//`",
        "`/* */`",
        "NUL",
        "every `/`",
        "`$after.email`",
        "`DEFINE PARAM`",
        "canonical form",
        "`repr()`",
        "a refusal raised without `tx=` names the event or the function, never its statement",
        "without the server's excerpt of the statement (v0.31.2, [#278](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/278))",
        "system user",
    ),
}
#: Spec §11.1 item 1: one key phrase per "Coming from the full SurrealDB-ORM" bullet, in order.
_COMING_FROM_PHRASES_32 = (
    "`DefineEvent(name, table, when, then, comment)`",
    "`Event(…, async_mode=True, retry=3)`",
    "`then` is a block",
    "comments escaped with a backslash",
    "`define_function()` has no full-ORM equivalent",
    "come with migrations (v0.37.0)",
)
#: Spec §11.1 items 3–5: the features row, the roadmap split and the comparison rows.
_FEATURES_ROW_32 = ["Server-side events & function DDL (`Event`, `define_function()`)", "✅"]
_ROADMAP_ROWS_32 = [
    ["v0.30.0", "Tier 3 — Schema introspection", "✅ Released"],
    ["v0.31.0", "Tier 3 — Server-side events and DDL helpers", "✅ Released"],
    ["v0.32.0 – v0.39.0", "Tier 3 — Advanced (search, DDL, migrations, CLI)", "📋 Planned"],
    ["v0.40.0", "Beta Phase (API freeze, hardening)", "📋 Planned"],
]
_COMPARISON_ROWS_32 = [
    ["Schema Introspection", "✅ v0.30.0 (`DatabaseIntrospector`, `generate_models_from_db()`)", "✅"],
    ["DEFINE EVENT", "✅ v0.31.0 (`Event`, `define_events()`, `ASYNC` on 3.x)", "✅ migrations (`DefineEvent`)"],
    ["`define_function()` (DDL)", "✅ v0.31.0", "❌"],
    ["Materialized views", "v0.32.0", "✅"],
]
#: Spec §11.1 item 6 (D14): exactly seven rows v0.31.0 adds to the 2.x/3.x table, cell by cell.
_EVENT_ROWS = [
    [
        "Server-side events (`define_events()`, `define_event()`, `remove_event()`)",
        "same on both lines: defined, fired on `CREATE`, `UPDATE` and `DELETE`, `WHEN` honoured; `$event`, `$before`, "
        "`$after`, `$value` alike",
        "the same; names are quoted, so `update` or `delete` work as event names (3.x refuses them bare)",
        "v0.31.0",
    ],
    [
        "Asynchronous events (`Event(async_mode=True, retry=…, max_depth=…)`)",
        "`SurrealDbError` before any DDL: 2.x cannot parse `ASYNC`",
        "run after the write's transaction commits, never for a cancelled one; read back with `async_mode`, `retry`, "
        "`max_depth`",
        "v0.31.0",
    ],
    ["`$input` in an event", "`NONE`", "the fields the write sent (`{ name: 'a' }` for `SET name = 'a'`)", "v0.31.0"],
    [
        "A write that changes nothing, on a table with an event",
        "fires no event",
        "fires no event, except an `UPSERT` (the ORM's `upsert()`): one `UPDATE` event",
        "v0.31.0",
    ],
    [
        "An event whose `THEN` fails (a `THROW`, an error)",
        "the write is refused; the SDK's `InternalError` (`An error occurred: …` for a `THROW`, the bare cause otherwise)",
        "the write is refused; the SDK's `InternalError` (`Error while processing event <name>: …`)",
        "v0.31.0",
    ],
    [
        "Event and function definitions read back (`EventState`, `FunctionState`)",
        "a block on several lines, each statement ending with `;`; `option<T>` parameters as written",
        "a block on one line, no `;` after a single statement; optional parameters rendered with `none` first",
        "v0.31.0",
    ],
    [
        "`define_function()`, `remove_function()` and `call_function()`",
        "same on both lines: the next `call_function()` sees a redefinition on every connection of this process to "
        "the same namespace and database, whatever its URL",
        "the same",
        "v0.31.0",
    ],
]
_HEADER_SENTENCE_31 = "Rows added in v0.31.0 were measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5."
#: Spec §11.1 item 7: the row v0.31.0 adds to the embedded-engine table, cell by cell.
_EMBEDDED_EVENTS_ROW = [
    "Server-side events and stored functions (v0.31.0)",
    "as on a 2.x server: `$input` is `NONE`; `Event(async_mode=True)` raises `SurrealDbError` before any DDL",
    "`ASYNC` events on 3.x",
    "v0.31.0",
]
#: Spec §11.1 item 8: section 17's signature-cache paragraph, whitespace normalised.
_SIGNATURE_CACHE_32 = (
    "Signature caching is transparent. `define_function()` and `remove_function()` "
    "([section 32](#32-server-side-events-and-stored-functions-event-define_events-define_function)) drop the "
    "function's cached signature themselves, on every connection of this process to the same namespace and "
    "database, whatever its URL (`ws://` or `http://`, with or without `/rpc`, `localhost` or `127.0.0.1`), so the "
    "next call sees a redefinition made through them at once, even one that only changes the order of the same "
    "parameter names, and even on another connection than the one that applied the DDL. For a function redefined "
    'out of band — with raw DDL (`client.query("DEFINE FUNCTION …")`) or from another process — two helpers are '
    "available, both on the current connection: `SurrealDBConnectionManager.clear_function_signature_cache()` and "
    "`function_signature_cache_size()`. The cache is keyed by URL, namespace and database, so `set_url()`, "
    "`set_namespace()` and `set_database()` cannot serve a signature read elsewhere; it is cleared outright by "
    "`set_connection()` and `unset_connection()`. After an out-of-band redefinition, a signature whose parameter "
    "_names_ changed self-heals on its own — but one with the **same names in a different order** cannot be "
    "detected, since `params=` is a mapping and carries no order to compare against: call "
    "`clear_function_signature_cache()` on each connection that calls the function, or `params=` keeps binding in "
    "the stale order."
)


def _section_32() -> str:
    return _readme_part(_SECTION_32)


def _part_32(heading: str) -> str:
    """One ``####`` subsection of section 32, as written."""
    return _subsection(f"#### {heading}", _section_32())


def _python_blocks(text: str) -> list[str]:
    """The ``python`` blocks of *text*, in order."""
    blocks: list[str] = re.findall(r"^```python\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    return blocks


def _printed(block: str) -> list[str]:
    """What a section 32 example prints: its comment lines that start a line (``# <printed line>``), in order."""
    return re.findall(r"^# (.*)$", block, re.MULTILINE)


def _anchor(heading: str) -> str:
    """GitHub's anchor of a README heading: lower case, punctuation but ``-`` and ``_`` dropped, spaces as ``-``."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


class TestReadmeSection32:
    """AC-17: README section 32 — its place, subsections, examples (run on ``mem://`` as written, each printed line
    equal to its comment), the Security wording (D18) and the canonical kinds (P1) — section 17's first example and
    signature-cache paragraph, the generated-models bullet, and the features, roadmap, comparison and both behaviour
    tables (spec §11.1)."""

    def test_section_32_follows_section_31(self) -> None:
        """Right after section 31 (after its "Coming from" bullets) and before the ``---`` of "Configuration Options"."""
        readme = _readme()
        start_31 = readme.index(f"\n{_SECTION_31}\n")
        start_32 = readme.index(f"\n{_SECTION_32}\n")
        configuration = readme.index("\n## Configuration Options\n")
        assert start_31 < start_32 < configuration
        assert re.findall(r"^#{2,3} .*$", readme[start_31 + 1 : configuration], re.MULTILINE) == [
            _SECTION_31,
            _SECTION_32,
        ]
        assert readme[:configuration].rstrip().endswith("\n---"), "section 32 ends before the --- of the next part"

    def test_subsections_in_order(self) -> None:
        assert re.findall(r"^#### (.*)$", _section_32(), re.MULTILINE) == _SUBSECTIONS_32

    def test_the_spec_examples_open_their_subsections(self) -> None:
        """Examples A–D exactly, each the first block of its subsection, and no other block; ``python`` fences only."""
        assert _python_blocks(_section_32()) == _EXAMPLES_32
        for heading, example in zip(_SUBSECTIONS_32, _EXAMPLES_32, strict=False):
            part = _part_32(heading)
            assert _python_blocks(part)[0] == example, heading
            assert part.split("\n\n", 1)[1].startswith(f"```python\n{example}```"), heading
        fences = re.findall(r"^```(\S*)$", _section_32(), re.MULTILINE)
        assert fences == ["python", ""] * len(_EXAMPLES_32), fences

    def test_intro_paragraph(self) -> None:
        intro = _normalised(_section_32().split("\n#### ", 1)[0])
        missing = [needle for needle in _INTRO_NEEDLES_32 if needle not in intro]
        assert not missing, missing

    @pytest.mark.parametrize("heading", list(_PART_NEEDLES_32))
    def test_what_each_subsection_says(self, heading: str) -> None:
        text = _normalised(_part_32(heading))
        missing = [needle for needle in _PART_NEEDLES_32[heading] if needle not in text]
        assert not missing, missing

    def test_security_wording(self) -> None:
        """D18: the binding sentence, and no stronger claim about fragments."""
        text = _normalised(_part_32("Security"))
        assert _D18_SENTENCE in text, text
        for claim in ("cannot inject", "cannot close", "boundary", "sound"):
            assert claim not in _normalised(_section_32()), claim

    def test_kinds_are_written_in_canonical_form(self) -> None:
        """P1: the functions prose says kinds are written in canonical form, with ``option<int>`` as the example."""
        text = _normalised(_part_32("Stored functions: `define_function()`"))
        assert "written in canonical form" in text, text
        assert text.index("canonical form") < text.index("`option<int>`"), text

    def test_coming_from_the_full_orm(self) -> None:
        """One key phrase per bullet, in order."""
        bullets = _bullets(_part_32("Coming from the full SurrealDB-ORM"))
        assert len(bullets) == len(_COMING_FROM_PHRASES_32), bullets
        for bullet, phrase in zip(bullets, _COMING_FROM_PHRASES_32, strict=True):
            assert phrase in bullet, (phrase, bullet)
        assert "`RemoveEvent(name, table)`" in bullets[0] and ".remove_event(event, table)`" in bullets[0]

    def test_links_resolve(self) -> None:
        """Every in-page link of section 32 and of section 17's new paragraph names a heading of the README."""
        anchors = {_anchor(heading) for heading in re.findall(r"^#{2,4} (.*)$", _readme(), re.MULTILINE)}
        links = re.findall(r"\]\(#([^)]+)\)", _section_32() + _readme_part(_SECTION_17))
        assert links, "section 32 links to sections 17 and 31"
        assert [link for link in links if link not in anchors] == []

    def test_features_row(self) -> None:
        features = [cells for _, cells in _table_rows(_readme_part("## Features"))]
        firsts = [cells[0] for cells in features]
        assert _FEATURES_ROW_32 in features, features
        introspection = "Schema introspection (`DatabaseIntrospector`, `generate_models_from_db()`)"
        assert firsts.index(_FEATURES_ROW_32[0]) == firsts.index(introspection) + 1

    def test_roadmap_rows(self) -> None:
        roadmap = [cells for _, cells in _table_rows(_readme_part("## Roadmap"))]
        start = [cells[0] for cells in roadmap].index("v0.30.0")
        assert roadmap[start : start + 4] == _ROADMAP_ROWS_32, roadmap

    def test_comparison_rows(self) -> None:
        """The DEFINE EVENT row filled in and a ``define_function()`` row after it, between the introspection row and
        the materialized views."""
        comparison = [cells for _, cells in _table_rows(_readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"))]
        start = [cells[0] for cells in comparison].index("Schema Introspection")
        assert comparison[start : start + 4] == _COMPARISON_ROWS_32, comparison

    def test_server_behaviour_rows(self) -> None:
        """Exactly seven rows, Since v0.31.0, together and in version order: earlier releases above them, later ones
        below them, then the table's last row (v0.19.1's re-run)."""
        assert_server_rows(_EVENT_ROWS)

    def test_server_behaviour_table_keeps_its_widths(self) -> None:
        """Every line 535 characters, the columns 136, 175, 181 and 38 wide: the new rows are padded to them."""
        assert_server_table()

    def test_server_behaviour_header_sentence(self) -> None:
        """The sentence v0.31.0 adds to the table's intro paragraph, once, among sentences naming their releases in
        increasing version order."""
        assert_server_header_sentence(_HEADER_SENTENCE_31)

    def test_embedded_table_row(self) -> None:
        """All four cells, in version order (earlier releases above, later ones below) before the catch-all
        "Everything else" row; widths kept (69, 343, 113, 9)."""
        assert_embedded_rows([_EMBEDDED_EVENTS_ROW])

    def test_tables_have_no_raw_pipe_in_a_cell(self) -> None:
        """Every table v0.31.0 touches keeps one cell count per line and no escaped pipe."""
        parts = [
            _readme_part("## Features"),
            _readme_part(_SERVER_BEHAVIOUR),
            _readme_part(_EMBEDDED_BEHAVIOUR),
            _readme_part("## Roadmap"),
            _readme_part("## SurrealDB-ORM-lite vs SurrealDB-ORM"),
        ]
        tables = [lines for part in parts for lines in _md_tables(part)]
        assert len(tables) == len(parts)
        assert _md_tables(_section_32()) == [], "section 32 has no table"
        for lines in tables:
            assert len({len(_cells(line)) for line in lines}) == 1 and "\\|" not in "".join(lines), lines

    def test_signature_cache_paragraph(self) -> None:
        """Spec §11.1 item 8: a redefinition through ``define_function()``/``remove_function()`` is followed at once,
        on every connection to the same database, whatever its URL; only an out-of-band one still needs
        ``clear_function_signature_cache()``."""
        paragraphs = [_normalised(paragraph) for paragraph in _readme_part(_SECTION_17).split("\n\n")]
        assert _SIGNATURE_CACHE_32 in paragraphs, [p for p in paragraphs if p.startswith("Signature caching")]

    async def test_section_17_declares_its_function_with_define_function(self) -> None:
        """Spec §11.1 item 2: the first example declares ``fn::acquire_lock`` with ``define_function()``; run on
        ``mem://`` up to its positional call, which returns the function's object."""
        section = _readme_part(_SECTION_17)
        block = _python_blocks(section)[0]
        assert 'await SurrealDBConnectionManager.define_function(\n    "fn::acquire_lock",' in block, block
        assert "client.query(" not in block and "lands in v0.31.0" not in section
        module = ast.parse(block)
        # The last statement calls a function the example does not define (``billing::total``).
        assert "billing::total" in ast.unparse(module.body[-1])
        module.body = module.body[:-1]
        compiled = compile(module, "<README section 17>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        namespace: dict[str, Any] = {"__name__": "readme_section_17"}
        try:
            async with embedded_database():
                # A doctest of this repository's own README (trusted text), top-level awaits included.
                outcome = eval(compiled, namespace)
                if inspect.iscoroutine(outcome):
                    await outcome
                assert namespace["lock"] == {"acquired": True, "holder": "pod-a"}
        finally:
            namespace.clear()

    async def test_section_32_examples(self, capsys: pytest.CaptureFixture[str]) -> None:
        """AC-17: every ``python`` block runs as written, in order and in one namespace, on ``mem://`` (inside
        ``embedded_database()``); what each block prints is, line by line, its ``# `` comment lines."""
        capsys.readouterr()
        namespace: dict[str, Any] = {"__name__": "readme_section_32"}
        try:
            async with embedded_database():
                for code in _python_blocks(_section_32()):
                    compiled = compile(code, "<README section 32>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                    assert capsys.readouterr().out.splitlines() == _printed(code), code
        finally:
            namespace.clear()

    async def test_section_32_guards_let_a_delete_through(self) -> None:
        """A guard on ``$after`` skips deletes: ``$after`` is ``NONE`` after a delete and ``NONE < 0`` is true on every
        line, so without ``$event != 'DELETE'`` example A's guard would refuse every ``delete()`` of an account and
        example B's every delete of a product. Examples A and B run as written on ``mem://``, then their guards are
        applied again (B removes both) and a record of each table is deleted."""
        namespace: dict[str, Any] = {"__name__": "readme_section_32_guards"}
        try:
            async with embedded_database():
                for code in _python_blocks(_section_32())[:2]:
                    compiled = compile(code, "<README section 32>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    # A doctest of this repository's own README (trusted text), top-level awaits included.
                    outcome = eval(compiled, namespace)
                    if inspect.iscoroutine(outcome):
                        await outcome
                await namespace["Account"].define_events()
                await namespace["account"].delete()
                await SurrealDBConnectionManager.define_event(namespace["stock_guard"], "product")
                client = await SurrealDBConnectionManager.get_client()
                await client.query("CREATE product:widget SET stock = 1;", {})
                await client.query("DELETE product:widget;", {})
                assert await client.query("SELECT * FROM account;", {}) == []
                assert await client.query("SELECT * FROM product;", {}) == []
        finally:
            namespace.clear()
