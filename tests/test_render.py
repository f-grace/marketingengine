"""Slide text wrapping.

Regression guard for a bug the first real render produced: a salary figure
split across lines mid-number, which ships a broken slide with no error.
"""

from __future__ import annotations

from engine.render import wrap_text


class TestNumbersStayIntact:
    def test_salary_range_never_splits(self):
        lines = wrap_text("$150,000-$200,000", width=12)
        assert "$150,000-$200,000" in lines
        for line in lines:
            assert not line.endswith("$20")
            assert not line.startswith("0,000")

    def test_large_figure_survives_a_narrow_box(self):
        lines = wrap_text("$1,000,000-$10,000,000+", width=8)
        assert any("$1,000,000-$10,000,000+" == l for l in lines)

    def test_hyphenated_word_is_not_broken(self):
        lines = wrap_text("baseline-relative scoring", width=10)
        assert any("baseline-relative" in l for l in lines)


class TestExplicitBreaks:
    def test_pipe_splits_lines(self):
        lines = wrap_text("5. Investment Banking Analyst | $150,000-$200,000", width=40)
        assert lines[0] == "5. Investment Banking Analyst"
        assert lines[-1] == "$150,000-$200,000"

    def test_pipe_with_wrapping_on_each_side(self):
        lines = wrap_text("A very long role title indeed | $1-$2", width=12)
        assert lines[-1] == "$1-$2"
        assert len(lines) > 2

    def test_empty_segments_dropped(self):
        assert "" not in wrap_text("Role || $100", width=40)


class TestOrdinaryText:
    def test_wraps_normally(self):
        lines = wrap_text("Top 5 highest paying jobs in finance", width=14)
        assert len(lines) > 1
        assert all(len(l) <= 20 for l in lines)

    def test_short_text_is_one_line(self):
        assert wrap_text("Hello", width=40) == ["Hello"]

    def test_never_returns_empty(self):
        assert wrap_text("x", width=1) != []
