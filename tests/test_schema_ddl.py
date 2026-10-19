"""Server-side events and DDL helpers (v0.31.0): the ``Event`` value object, the structural check of a hand-written
SurrealQL fragment (``define_parser.scan_fragment()``), the kind reader's free-text generic argument and the pure
renderers (``Event.ddl()``, ``function_ddl()``, the removal statements).

Unit tests (no server): ``scan_fragment()`` (spec §6.2), ``Event``'s construction and the exact texts of E1–E11
(spec §5.1, §5.6; AC-2), the ``_KindReader.raw_argument()`` hardening (spec §6.2), and the golden statements of spec
§5.2 with the exact texts of E12–E13 and F1–F9 (AC-3). The check catches mistakes and is not an injection defence:
every refused input below is a plain malformed name, fragment or kind.
"""

import dataclasses
import pickle
from typing import Any

import pytest

import surreal_orm_lite
from surreal_orm_lite import SurrealDBConnectionManager, schema
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
from surreal_orm_lite.schema import Event, function_ddl, remove_event_statement, remove_function_statement

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
