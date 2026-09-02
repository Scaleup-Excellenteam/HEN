"""Tests for the interactive completion loop."""

from __future__ import annotations

import io

import pytest

from autocomplete import cli
from autocomplete.cli import READY_MESSAGE, format_stats, format_suggestions
from autocomplete.data import AutoCompleteData
from autocomplete.index import SearchIndex

DEMO_LINES = (
    b"Alpha: this is a demo.\n"
    b"Beta: this is a demo.\n"
    b"Delta: this is a demo.\n"
    b"Gamma: this is a demo.\n"
    b"Omega: this is a demo.\n"
)


def write_corpus(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def session(
    index, typed: str, limit: int | None = None, stats: bool = False
) -> str:
    """Run the loop over scripted input and return everything it printed."""
    output = io.StringIO()
    cli.run(index, io.StringIO(typed), output, limit, stats=stats)
    return output.getvalue()


@pytest.fixture(scope="module")
def demo_index(tmp_path_factory) -> SearchIndex:
    root = write_corpus(tmp_path_factory.mktemp("demo"), {"example.txt": DEMO_LINES})
    return SearchIndex.build(root, summary_width=5)


@pytest.fixture(scope="module")
def varied_index(tmp_path_factory) -> SearchIndex:
    root = write_corpus(
        tmp_path_factory.mktemp("varied"),
        {
            "a.txt": b"alpha bravo charlie\ndelta echo foxtrot\n",
            "b.txt": b"one of a kind\n",
        },
    )
    return SearchIndex.build(root, summary_width=5)


class TestWorkedExample:
    def test_reproduces_the_transcript_from_the_assignment(self, demo_index):
        """Byte for byte, the session printed in the assignment.

        The entry appears because scripted input is echoed, standing in for what
        a terminal would print as it is typed.
        """
        assert session(demo_index, "this is\n") == (
            "The system is ready. Enter your text:\n"
            "this is\n"
            "Here are 5 suggestions:\n"
            "1. Alpha: this is a demo. (example.txt:1, score=14)\n"
            "2. Beta: this is a demo. (example.txt:2, score=14)\n"
            "3. Delta: this is a demo. (example.txt:3, score=14)\n"
            "4. Gamma: this is a demo. (example.txt:4, score=14)\n"
            "5. Omega: this is a demo. (example.txt:5, score=14)\n"
            "this is\n"
        )


class TestFormatting:
    def test_numbers_results_from_one(self):
        results = [
            AutoCompleteData("first", "a.txt", 1, 10),
            AutoCompleteData("second", "a.txt", 2, 8),
        ]
        assert format_suggestions(results) == (
            "Here are 2 suggestions:\n"
            "1. first (a.txt:1, score=10)\n"
            "2. second (a.txt:2, score=8)\n"
        )

    def test_a_single_result_reads_naturally(self):
        results = [AutoCompleteData("only", "a.txt", 1, 4)]
        assert format_suggestions(results).startswith("Here is 1 suggestion:\n")

    def test_no_results_says_so(self):
        assert format_suggestions([]) == "No suggestions found.\n"

    def test_each_line_carries_every_field(self):
        rendered = format_suggestions([AutoCompleteData("text", "deep/b.txt", 42, 7)])
        assert "1. text (deep/b.txt:42, score=7)" in rendered


class TestSession:
    def test_greets_once_at_the_start(self, demo_index):
        assert session(demo_index, "this is\ndemo\n").count(READY_MESSAGE) == 1

    def test_ends_cleanly_when_the_input_stops(self, demo_index):
        assert session(demo_index, "") == f"{READY_MESSAGE}\n\n"

    def test_shows_suggestions_for_each_entry(self, varied_index):
        printed = session(varied_index, "alpha\nzzzz\n")
        assert printed.count("suggestion") == 2

    def test_fewer_results_than_the_limit(self, varied_index):
        printed = session(varied_index, "one of\n")
        assert "Here is 1 suggestion:" in printed
        assert "1. one of a kind (b.txt:1, score=12)" in printed

    def test_no_matches(self, varied_index):
        assert "No suggestions found." in session(varied_index, "zzqqxx\n")

    def test_limit_is_honoured(self, demo_index):
        printed = session(demo_index, "this is\n", limit=2)
        assert "Here are 2 suggestions:" in printed
        assert "3. " not in printed


class TestCumulativeTyping:
    def test_the_sentence_grows_across_turns(self, varied_index):
        """Each turn searches everything typed so far, not just the new text."""
        printed = session(varied_index, "alpha\n bravo\n charlie\n")
        assert printed.count("Here is 1 suggestion:") == 3
        assert printed.rstrip().endswith("alpha bravo charlie")

    def test_what_has_been_typed_is_echoed_as_the_prompt(self, varied_index):
        printed = session(varied_index, "alpha\n bravo\n")
        assert "\nalpha" in printed
        assert printed.rstrip().endswith("alpha bravo")

    def test_text_is_appended_exactly_as_entered(self, varied_index):
        """Nothing is inserted between turns, so a word split across two entries
        is joined back together."""
        printed = session(varied_index, "alp\nha bravo\n")
        assert printed.rstrip().endswith("alpha bravo")
        assert "1. alpha bravo charlie (a.txt:1, score=22)" in printed

    def test_an_empty_entry_leaves_the_sentence_alone(self, varied_index):
        with_blank = session(varied_index, "alpha\n\n")
        assert with_blank.count("Here is 1 suggestion:") == 2

    def test_a_blank_first_entry_searches_nothing(self, varied_index):
        printed = session(varied_index, "\n\n")
        assert "suggestion" not in printed
        assert printed.startswith(f"{READY_MESSAGE}\n")


class TestReset:
    def test_the_reset_character_starts_a_new_sentence(self, varied_index):
        printed = session(varied_index, "alpha\n#\n")
        assert printed.count(READY_MESSAGE) == 2
        assert printed.rstrip().endswith(READY_MESSAGE)

    def test_typing_continues_from_nothing_after_a_reset(self, varied_index):
        printed = session(varied_index, "alpha\n#\ndelta\n")
        # The final prompt shows only what was typed after the reset.
        assert printed.rstrip().endswith("delta")
        assert "1. delta echo foxtrot (a.txt:2, score=10)" in printed

    def test_a_reset_discards_the_rest_of_its_line(self, varied_index):
        """Current policy: the line is a signal to start over, not text."""
        printed = session(varied_index, "alpha\nbravo#charlie\ndelta\n")
        assert printed.rstrip().endswith("delta")
        assert "charlie" not in printed.split(READY_MESSAGE)[-1]

    def test_a_reset_on_the_very_first_entry_is_harmless(self, varied_index):
        printed = session(varied_index, "#\nalpha\n")
        assert printed.count(READY_MESSAGE) == 2
        assert "Here is 1 suggestion:" in printed

    def test_repeated_resets(self, varied_index):
        assert session(varied_index, "#\n#\n#\n").count(READY_MESSAGE) == 4


class TestQueryHandling:
    def test_case_punctuation_and_spacing_do_not_have_to_match(self, demo_index):
        for entry in ["THIS IS\n", "this  is\n", "This, is!\n"]:
            assert "Here are 5 suggestions:" in session(demo_index, entry)

    def test_a_typo_still_finds_the_sentence(self, varied_index):
        printed = session(varied_index, "alpha bravi\n")
        assert "alpha bravo charlie" in printed

    def test_carriage_returns_from_windows_input_are_stripped(self, varied_index):
        printed = session(varied_index, "alpha\r\n")
        assert "Here is 1 suggestion:" in printed

    def test_punctuation_only_input_finds_nothing(self, varied_index):
        assert "No suggestions found." in session(varied_index, "!!!\n")


class TestInputEcho:
    """Scripted input is echoed so a piped run reads like a typed session; a
    real terminal already prints what is typed, so it is not echoed twice."""

    def test_scripted_input_is_echoed(self, varied_index):
        assert "\nalpha\n" in session(varied_index, "alpha\n")

    def test_terminal_input_is_not_echoed(self, varied_index):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        output = io.StringIO()
        cli.run(varied_index, Terminal("alpha\n"), output)
        printed = output.getvalue()

        # The scripted run stands in for the terminal's echo, so it carries the
        # entry on its own line before the results; the terminal run does not.
        assert "alpha\nHere is 1 suggestion:" in session(varied_index, "alpha\n")
        assert "alpha\nHere is 1 suggestion:" not in printed
        assert "Here is 1 suggestion:" in printed

    def test_a_stream_without_isatty_is_treated_as_scripted(self, varied_index):
        class Bare:
            def __init__(self, text):
                self._lines = iter(text.splitlines(keepends=True))

            def readline(self):
                return next(self._lines, "")

        output = io.StringIO()
        cli.run(varied_index, Bare("alpha\n"), output)
        assert "\nalpha\n" in output.getvalue()


class TestStats:
    """``--stats`` adds a timing and memory line under each result set.

    It is off by default because the session without it is the assignment's
    transcript, checked byte for byte above.
    """

    def test_nothing_is_added_unless_asked_for(self, demo_index):
        assert "found in" not in session(demo_index, "this is\n")

    def test_a_timing_line_follows_the_suggestions(self, demo_index):
        printed = session(demo_index, "this is\n", stats=True)
        assert "Here are 5 suggestions:" in printed
        assert "found in" in printed
        assert printed.index("5. Omega") < printed.index("found in")

    def test_every_search_is_timed(self, varied_index):
        printed = session(varied_index, "alpha\nbravo\n", stats=True)
        assert printed.count("found in") == 2

    def test_a_search_that_found_nothing_is_still_timed(self, varied_index):
        printed = session(varied_index, "zzqqxx\n", stats=True)
        assert "No suggestions found." in printed
        assert "found in" in printed

    def test_a_reset_is_not_a_search(self, varied_index):
        assert "found in" not in session(varied_index, "#\n", stats=True)

    def test_the_line_reports_milliseconds(self):
        assert format_stats(0.0031).startswith("   found in 3.1 ms")

    def test_the_line_reports_memory_when_it_can_be_read(self, monkeypatch):
        monkeypatch.setattr(cli.memory, "resident_bytes", lambda: 1_810_000_000)
        assert format_stats(0.0031) == "   found in 3.1 ms | memory 1.81 GB\n"

    def test_memory_is_left_out_rather_than_guessed(self, monkeypatch):
        monkeypatch.setattr(cli.memory, "resident_bytes", lambda: None)
        assert format_stats(0.0031) == "   found in 3.1 ms\n"
