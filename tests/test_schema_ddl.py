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
refused before any request and comments read back (spec §5.3, §5.4; AC-5, AC-6, AC-7, AC-13, AC-15).
"""

import asyncio
import contextlib
import dataclasses
import os
import pickle
from collections.abc import AsyncIterator
from typing import Any

import pytest
from surrealdb import InternalError, NotAllowedError, ValidationError

import surreal_orm_lite
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    DatabaseIntrospector,
    SurrealConfigDict,
    SurrealDbAuthenticationError,
    SurrealDbConflictError,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealDbNotFoundError,
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
    function_ddl,
    remove_event_statement,
    remove_function_statement,
)
from surreal_orm_lite.utils import apply_ddl_statements

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
    """Spec §6.2 (D18): the free text of an unknown generic argument (``file<…>``) refuses a comment marker and a quote
    character; every other kind reads as before."""

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
        for kind in ["int>", "", "  ", "int; x", "file<x -- y>", "file<x # y>", "file<'z'>", "file<x//y>"]
    ],
    *[
        pytest.param(("x", "RETURN 1"), {"returns": kind}, ValueError, _kind_error("returns="), id=f"F7-returns-{kind!r}")
        for kind in ["array<", 'file<"q">', "file<a/*b>", ""]
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
            assert str(raised.value).startswith("Can't apply event removal: REMOVE EVENT `ev_audit` ON `ev_person`; -> ")
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
            assert str(raised.value).startswith("Can't apply event removal: REMOVE EVENT `ev_stock` ON `ev_product`; -> ")
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
        assert str(raised.value).startswith(
            "Can't apply event definition: DEFINE EVENT OVERWRITE `ev_broken` ON `ev_person` THEN { CREATE }; -> "
        )
        expected = RuntimeError if kind == "mem" else ValidationError if major >= 3 else InternalError
        assert type(raised.value.__cause__) is expected

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
