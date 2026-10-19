"""Server-side events and DDL helpers (v0.31.0): the ``Event`` value object, the structural check of a hand-written
SurrealQL fragment (``define_parser.scan_fragment()``) and the kind reader's free-text generic argument.

Unit tests (no server): ``scan_fragment()`` (spec §6.2), ``Event``'s construction and the exact texts of E1–E11
(spec §5.1, §5.6; AC-2), and the ``_KindReader.raw_argument()`` hardening (spec §6.2). The check catches mistakes and
is not an injection defence: every refused input below is a plain malformed name or fragment.
"""

import dataclasses
import pickle
from typing import Any

import pytest

import surreal_orm_lite
from surreal_orm_lite import schema
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
from surreal_orm_lite.schema import Event

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
