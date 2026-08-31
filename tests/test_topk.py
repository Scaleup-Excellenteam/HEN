"""Tests for the block-summary top-k structure.

Every answer is compared against reading the range entry by entry, which is the
definition the summaries have to reproduce exactly.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from autocomplete.topk import (
    DEFAULT_BLOCK_SIZE,
    SENTINEL,
    BlockSummaries,
    BlockSummaryError,
)


def make(record_of_suffix: list[int], *, width: int, block_size: int) -> BlockSummaries:
    """Build summaries for a made-up suffix array with the given record layout.

    Each "record" is given one blob position, so the record of suffix ``i`` is
    exactly ``record_of_suffix[i]``, which keeps the tests about the summaries
    rather than about position mapping.
    """
    count = max(record_of_suffix, default=-1) + 1
    starts = np.arange(count + 1, dtype=np.int64)
    positions = np.array(record_of_suffix, dtype=np.int32)
    return BlockSummaries.build(
        positions, starts, width=width, block_size=block_size
    )


def brute_force(
    record_of_suffix: list[int], low: int, high: int, need: int, exclude=()
) -> list[int]:
    excluded = set(exclude)
    distinct = sorted({r for r in record_of_suffix[low:high]} - excluded)
    return distinct[:need]


class TestBuild:
    def test_rows_hold_the_smallest_distinct_records_of_each_block(self):
        layout = [5, 3, 5, 9, 1, 0, 7, 7]
        blocks = make(layout, width=2, block_size=4)
        assert blocks.summaries.shape == (2, 2)
        assert list(blocks.summaries[0]) == [3, 5]
        assert list(blocks.summaries[1]) == [0, 1]

    def test_sparse_blocks_are_padded_with_the_sentinel(self):
        blocks = make([4, 4, 4, 4], width=3, block_size=4)
        assert list(blocks.summaries[0]) == [4, SENTINEL, SENTINEL]

    def test_a_final_partial_block_is_summarized(self):
        blocks = make([1, 2, 3, 4, 9], width=2, block_size=4)
        assert blocks.summaries.shape == (2, 2)
        assert list(blocks.summaries[1]) == [9, SENTINEL]

    def test_empty_suffix_array(self):
        blocks = make([], width=5, block_size=4)
        assert blocks.summaries.shape == (0, 5)
        assert blocks.smallest_record_ids(0, 0, need=1) == []

    def test_rejects_a_width_below_one(self):
        with pytest.raises(BlockSummaryError, match="width"):
            make([0, 1], width=0, block_size=4)

    def test_rejects_a_block_size_below_one(self):
        with pytest.raises(BlockSummaryError, match="block size"):
            make([0, 1], width=2, block_size=0)


class TestRangeSelection:
    LAYOUT = [7, 2, 9, 2, 4, 0, 1, 8, 3, 5, 6, 1, 0, 9, 4, 2]

    @pytest.fixture
    def blocks(self):
        return make(self.LAYOUT, width=5, block_size=4)

    def test_empty_range(self, blocks):
        assert blocks.smallest_record_ids(5, 5, need=3) == []

    def test_reversed_range_is_empty(self, blocks):
        assert blocks.smallest_record_ids(9, 4, need=3) == []

    def test_single_entry_range(self, blocks):
        assert blocks.smallest_record_ids(0, 1, need=3) == [7]

    def test_range_inside_one_block(self, blocks):
        assert blocks.smallest_record_ids(1, 4, need=5) == brute_force(
            self.LAYOUT, 1, 4, 5
        )

    def test_range_on_exact_block_boundaries(self, blocks):
        assert blocks.smallest_record_ids(4, 12, need=5) == brute_force(
            self.LAYOUT, 4, 12, 5
        )

    def test_range_spanning_two_partial_blocks(self, blocks):
        assert blocks.smallest_record_ids(2, 7, need=5) == brute_force(
            self.LAYOUT, 2, 7, 5
        )

    def test_range_with_whole_blocks_and_both_ends_partial(self, blocks):
        assert blocks.smallest_record_ids(1, 15, need=5) == brute_force(
            self.LAYOUT, 1, 15, 5
        )

    def test_whole_array(self, blocks):
        assert blocks.smallest_record_ids(0, 16, need=5) == brute_force(
            self.LAYOUT, 0, 16, 5
        )

    def test_final_partial_block(self):
        layout = [3, 1, 2, 8, 0, 6]
        blocks = make(layout, width=3, block_size=4)
        assert blocks.smallest_record_ids(0, 6, need=3) == brute_force(layout, 0, 6, 3)

    @pytest.mark.parametrize("need", [1, 2, 3, 4, 5])
    def test_every_need(self, blocks, need):
        assert blocks.smallest_record_ids(0, 16, need=need) == brute_force(
            self.LAYOUT, 0, 16, need
        )

    @pytest.mark.parametrize("excluded", [(), (0,), (0, 1), (0, 1, 2), (0, 1, 2, 3)])
    def test_exclusions_of_every_size_up_to_width_minus_one(self, blocks, excluded):
        need = 5 - len(excluded)
        assert blocks.smallest_record_ids(
            0, 16, need=need, exclude=excluded
        ) == brute_force(self.LAYOUT, 0, 16, need, excluded)

    def test_asking_beyond_the_summary_width_is_refused(self, blocks):
        with pytest.raises(BlockSummaryError, match="beyond the 5 kept"):
            blocks.smallest_record_ids(0, 16, need=3, exclude=(0, 1, 2))

    def test_fewer_records_than_asked_for(self):
        layout = [4, 4, 4, 4]
        blocks = make(layout, width=5, block_size=2)
        assert blocks.smallest_record_ids(0, 4, need=5) == [4]

    def test_exactly_as_many_records_as_asked_for(self):
        layout = [3, 1, 2, 0, 4]
        blocks = make(layout, width=5, block_size=2)
        assert blocks.smallest_record_ids(0, 5, need=5) == [0, 1, 2, 3, 4]

    def test_more_records_than_asked_for(self):
        layout = list(range(20))
        blocks = make(layout, width=5, block_size=4)
        assert blocks.smallest_record_ids(0, 20, need=5) == [0, 1, 2, 3, 4]

    def test_one_record_repeated_many_times(self):
        """A record occupying a whole range must be reported once."""
        layout = [11] * 5000
        blocks = make(layout, width=5, block_size=DEFAULT_BLOCK_SIZE)
        assert blocks.smallest_record_ids(0, 5000, need=5) == [11]

    def test_duplicate_heavy_range_with_one_rare_record(self):
        layout = [9] * 4095 + [1] + [9] * 4096
        blocks = make(layout, width=5, block_size=DEFAULT_BLOCK_SIZE)
        assert blocks.smallest_record_ids(0, len(layout), need=2) == [1, 9]

    def test_the_needed_record_sits_at_the_very_end_of_a_huge_range(self):
        layout = [9] * 9000 + [0]
        blocks = make(layout, width=5, block_size=DEFAULT_BLOCK_SIZE)
        assert blocks.smallest_record_ids(0, len(layout), need=2) == [0, 9]

    def test_sentinels_never_appear_in_a_result(self):
        layout = [2, 2, 2]
        blocks = make(layout, width=5, block_size=2)
        assert SENTINEL not in blocks.smallest_record_ids(0, 3, need=5)

    def test_zero_need_returns_nothing(self, blocks):
        assert blocks.smallest_record_ids(0, 16, need=0) == []


class TestConfigurableWidth:
    """The summaries are built for the number of results the caller wants, so a
    project configured for more than five still gets exact answers."""

    @pytest.mark.parametrize("width", [1, 2, 3, 5, 8, 13])
    def test_any_width_answers_exactly(self, width):
        layout = [(i * 7) % 30 for i in range(200)]
        blocks = make(layout, width=width, block_size=16)
        assert blocks.summaries.shape[1] == width
        assert blocks.smallest_record_ids(0, len(layout), need=width) == brute_force(
            layout, 0, len(layout), width
        )

    def test_a_wider_request_than_the_build_allows_is_refused(self):
        blocks = make([1, 2, 3], width=2, block_size=2)
        with pytest.raises(BlockSummaryError):
            blocks.smallest_record_ids(0, 3, need=3)


class TestBoundaryFragmentsAreExact:
    def test_a_record_only_present_in_a_boundary_fragment_is_found(self):
        """Boundary fragments are read entry by entry rather than summarized, so
        a record that a block summary would have dropped is still seen."""
        block_size = 4
        # Block 0 covers records 10..13, whose summary of width 1 keeps only 10.
        # Record 11 survives only because entry 1 sits in the left fragment.
        layout = [10, 11, 12, 13, 20, 21, 22, 23]
        blocks = make(layout, width=1, block_size=block_size)
        assert list(blocks.summaries[0]) == [10]
        assert blocks.smallest_record_ids(1, 8, need=1) == [11]


class TestAgainstBruteForce:
    @given(
        layout=st.lists(st.integers(min_value=0, max_value=25), min_size=1, max_size=120),
        width=st.integers(min_value=1, max_value=6),
        block_size=st.integers(min_value=1, max_value=16),
        bounds=st.tuples(
            st.integers(min_value=0, max_value=120),
            st.integers(min_value=0, max_value=120),
        ),
        excluded=st.sets(st.integers(min_value=0, max_value=25), max_size=5),
        need=st.integers(min_value=1, max_value=6),
    )
    @settings(
        max_examples=600,
        deadline=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_arbitrary_ranges_match_brute_force(
        self, layout, width, block_size, bounds, excluded, need
    ):
        assume(need + len(excluded) <= width)
        low, high = sorted(bounds)
        low = min(low, len(layout))
        high = min(high, len(layout))

        blocks = make(layout, width=width, block_size=block_size)
        assert blocks.smallest_record_ids(
            low, high, need=need, exclude=excluded
        ) == brute_force(layout, low, high, need, excluded)
