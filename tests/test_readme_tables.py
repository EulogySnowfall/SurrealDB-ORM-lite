"""Unit: the README-structure helpers of ``tests/_readme_tables.py``, on made-up headers.

The documentation tests rely on these helpers to fail when a release's text changes by one character, including the
punctuation that ends it and the word that starts it (issue #272, PR review).
"""

from __future__ import annotations

import pytest

from tests._readme_tables import assert_header_sentence

_V1 = "rows added in v0.1.0 on 2.7.0 and 3.3.4, identical on 3.1.5"
_V2 = "the rows added in v0.2.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs"
_V3 = "rows added in v0.3.0 on 2.7.0 and 3.3.4 in one process, identical on 3.1.5"
_V4 = "The row added in v0.4.0 was measured on 2.7.0 and 3.3.4, identical on 3.1.5."
_V5 = "Rows added in v0.5.0 were measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5."
_HEADER = f"### Table Intro. Rows added in v0.0.9 were measured on 2.7.0; {_V1}; {_V2}; {_V3}. {_V4} {_V5}"


class TestAssertHeaderSentence:
    @pytest.mark.parametrize("sentence", [_V1, _V2, _V3, _V4, _V5])
    def test_a_bounded_sentence_or_clause_passes(self, sentence: str) -> None:
        assert_header_sentence(_HEADER, sentence)

    def test_the_last_sentence_may_end_the_header(self) -> None:
        assert_header_sentence(_HEADER.removesuffix(f" {_V5}"), _V4)
        assert_header_sentence(_HEADER.removesuffix(f" {_V4} {_V5}"), _V3)

    @pytest.mark.parametrize(
        ("old", "new", "sentence"),
        [
            # A wrong measured version right after a clause or a sentence.
            pytest.param(f"{_V1};", f"{_V1}0;", _V1, id="3.1.50-after-a-clause"),
            pytest.param(_V5, f"{_V5[:-1]}0.", _V5, id="3.1.50-in-a-sentence"),
            # A clause no longer separated by "; " or ". ": its end, then the start of the next one.
            pytest.param(f"{_V1}; {_V2}", f"{_V1}, {_V2}", _V1, id="comma-ends-a-clause"),
            pytest.param(f"{_V1}; {_V2}", f"{_V1}, {_V2}", _V2, id="comma-starts-a-clause"),
            pytest.param(f"{_V3}. {_V4}", f"{_V3}, {_V4}", _V3, id="comma-ends-the-last-clause"),
            pytest.param(f"{_V3}. {_V4}", f"{_V3}, {_V4}", _V4, id="comma-starts-a-sentence"),
            # A word glued to the start of a clause or a sentence.
            pytest.param(f"; {_V2}", f"; a{_V2}", _V2, id="arrows"),
            pytest.param(f" {_V5}", f" A{_V5}", _V5, id="ARows"),
            # A sentence's full stop followed by anything but a space or the end of the header.
            pytest.param(f"{_V4} {_V5}", f"{_V4}x{_V5}", _V4, id="no-space-after-a-sentence"),
            # A clause going on past where it ends.
            pytest.param(f"{_V3}. {_V4}", f"{_V3} and more. {_V4}", _V3, id="a-clause-goes-on"),
        ],
    )
    def test_a_change_at_a_boundary_fails(self, old: str, new: str, sentence: str) -> None:
        assert _HEADER.count(old) == 1
        with pytest.raises(AssertionError):
            assert_header_sentence(_HEADER.replace(old, new), sentence)

    def test_a_changed_character_inside_fails(self) -> None:
        with pytest.raises(AssertionError):
            assert_header_sentence(_HEADER.replace("in one process", "in one proces"), _V3)

    def test_a_second_copy_fails(self) -> None:
        with pytest.raises(AssertionError):
            assert_header_sentence(f"{_HEADER} {_V5}", _V5)

    def test_releases_out_of_order_fail(self) -> None:
        swapped = _HEADER.replace(f"{_V1}; {_V2}", f"{_V2}; {_V1}")
        with pytest.raises(AssertionError, match="out of version order"):
            assert_header_sentence(swapped, _V1)

    @pytest.mark.parametrize("sentence", ["identical on 3.1.5", f"{_V1} and {_V2}"])
    def test_a_sentence_names_one_release(self, sentence: str) -> None:
        with pytest.raises(AssertionError, match="names one release"):
            assert_header_sentence(_HEADER, sentence)
