"""Tests for the record store."""

from __future__ import annotations

import numpy as np
import pytest

from autocomplete.data import AutoCompleteData, tie_break_key
from autocomplete.normalize import ALPHABET, normalize
from autocomplete.records import RECORD_SEPARATOR, RecordStore, RecordStoreError
from autocomplete.reference import load_records


def build_tree(root, files: dict[str, bytes]):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture(scope="module")
def store(mini_corpus_path) -> RecordStore:
    return RecordStore.build(mini_corpus_path)


@pytest.fixture(scope="module")
def mini_corpus_path(pytestconfig):
    return pytestconfig.rootpath / "tests" / "fixtures" / "mini_corpus"


class TestBuild:
    def test_indexes_every_usable_line(self, store):
        # 5 demo lines + 3 Shakespeare + 6 usable lines in notes.txt.
        assert len(store) == 14

    def test_lists_files_in_order(self, store):
        assert store.paths == (
            "example.txt",
            "nested/deep/notes.txt",
            "shakespeare.txt",
        )

    def test_skips_lines_that_normalize_to_nothing(self, store):
        sentences = [store.sentence(i) for i in range(len(store))]
        assert "" not in sentences
        assert "   " not in sentences

    def test_offsets_are_real_line_numbers(self, store):
        digits = next(
            i for i in range(len(store)) if store.sentence(i).startswith("Digits")
        )
        # Two blank lines precede it, so it is line 8 rather than line 6.
        assert store.offset(digits) == 8

    def test_original_text_is_preserved(self, store):
        assert "To be or not to be, that is the question." in [
            store.sentence(i) for i in range(len(store))
        ]

    def test_normalized_text_matches_the_normalizer(self, store):
        for i in range(len(store)):
            assert store.normalized(i) == normalize(store.sentence(i))

    def test_empty_corpus_builds(self, tmp_path):
        empty = RecordStore.build(tmp_path)
        assert len(empty) == 0
        assert empty.max_record_length == 0
        assert bytes(empty.norm_blob) == b""

    def test_corpus_of_only_blank_lines_builds(self, tmp_path):
        build_tree(tmp_path, {"a.txt": b"\n\n   \n!!!\n"})
        assert len(RecordStore.build(tmp_path)) == 0

    def test_is_deterministic(self, mini_corpus_path):
        first = RecordStore.build(mini_corpus_path)
        second = RecordStore.build(mini_corpus_path)
        assert bytes(first.norm_blob) == bytes(second.norm_blob)
        assert bytes(first.orig_blob) == bytes(second.orig_blob)
        assert np.array_equal(first.starts, second.starts)
        assert np.array_equal(first.file_id, second.file_id)
        assert np.array_equal(first.line_no, second.line_no)
        assert first.paths == second.paths


class TestOrdering:
    def test_records_are_sorted_by_the_tie_break_key(self, store):
        keys = [
            tie_break_key(store.sentence(i), store.source_text(i), store.offset(i))
            for i in range(len(store))
        ]
        assert keys == sorted(keys)

    def test_duplicate_text_is_ordered_by_path(self, store):
        duplicates = [
            i
            for i in range(len(store))
            if store.sentence(i) == "Alpha: this is a demo."
        ]
        assert len(duplicates) == 2
        assert store.source_text(duplicates[0]) == "example.txt"
        assert store.source_text(duplicates[1]) == "nested/deep/notes.txt"

    def test_record_number_is_the_tie_break_rank(self, tmp_path):
        """Position in the store is exactly the order equal scores resolve to,
        which is what lets the search pick winners by record number."""
        build_tree(tmp_path, {"b.txt": b"same\nzeta\n", "a.txt": b"same\nalpha\n"})
        built = RecordStore.build(tmp_path)
        assert [
            (built.sentence(i), built.source_text(i)) for i in range(len(built))
        ] == [
            ("alpha", "a.txt"),
            ("same", "a.txt"),
            ("same", "b.txt"),
            ("zeta", "b.txt"),
        ]


class TestBlobLayout:
    def test_records_are_separated_in_the_blob(self, store):
        blob = bytes(store.norm_blob)
        for i in range(len(store)):
            assert blob[int(store.starts[i + 1]) - 1 : int(store.starts[i + 1])] == (
                RECORD_SEPARATOR
            )

    def test_offsets_span_the_blob(self, store):
        assert int(store.starts[0]) == 0
        assert int(store.starts[len(store)]) == len(store.norm_blob)

    def test_blob_holds_only_alphabet_and_separators(self, store):
        assert not bytes(store.norm_blob).translate(None, ALPHABET + RECORD_SEPARATOR)

    def test_max_record_length_is_the_longest_record(self, store):
        assert store.max_record_length == max(
            len(store.normalized(i)) for i in range(len(store))
        )

    def test_separator_cannot_appear_inside_a_record(self, store):
        for i in range(len(store)):
            assert RECORD_SEPARATOR not in store.normalized(i)


class TestPositionLookup:
    def test_maps_positions_to_the_owning_record(self, store):
        for i in range(len(store)):
            start = int(store.starts[i])
            last = int(store.starts[i + 1]) - 2
            assert store.record_at(start) == i
            assert store.record_at(last) == i

    def test_vectorized_lookup_agrees(self, store):
        positions = np.arange(0, len(store.norm_blob), 3, dtype=np.int64)
        expected = [store.record_at(int(p)) for p in positions]
        assert list(store.records_at(positions)) == expected

    def test_completion_carries_every_field(self, store):
        result = store.completion(1, 14)
        assert result == AutoCompleteData(
            "Alpha: this is a demo.", "example.txt", 1, 14
        )


class TestAgreesWithTheReference:
    """The store must hold exactly the records the reference engine reads.

    The reference walks files directly with no index, so this checks the whole
    build, traversal, decoding, normalization, skipping and ordering, against an
    implementation that shares none of it.
    """

    def test_same_records(self, store, mini_corpus_path):
        from_store = sorted(
            (store.sentence(i), store.source_text(i), store.offset(i), store.normalized(i))
            for i in range(len(store))
        )
        from_reference = sorted(
            (r.completed_sentence, r.source_text, r.offset, r.normalized)
            for r in load_records(mini_corpus_path)
        )
        assert from_store == from_reference


class TestRoundTrip:
    @pytest.mark.parametrize("use_mmap", [True, False])
    def test_write_then_read(self, store, tmp_path, use_mmap):
        store.write_to(tmp_path)
        restored = RecordStore.read_from(tmp_path, use_mmap=use_mmap)

        assert len(restored) == len(store)
        assert restored.paths == store.paths
        assert bytes(restored.norm_blob) == bytes(store.norm_blob)
        assert bytes(restored.orig_blob) == bytes(store.orig_blob)
        for i in range(len(store)):
            assert restored.sentence(i) == store.sentence(i)
            assert restored.normalized(i) == store.normalized(i)
            assert restored.source_text(i) == store.source_text(i)
            assert restored.offset(i) == store.offset(i)

    @pytest.mark.parametrize("use_mmap", [True, False])
    def test_empty_store_round_trips(self, tmp_path, use_mmap):
        source = tmp_path / "corpus"
        source.mkdir()
        target = tmp_path / "index"
        target.mkdir()
        RecordStore.build(source).write_to(target)
        assert len(RecordStore.read_from(target, use_mmap=use_mmap)) == 0

    def test_array_types_survive(self, store, tmp_path):
        store.write_to(tmp_path)
        restored = RecordStore.read_from(tmp_path, use_mmap=False)
        assert restored.starts.dtype == np.int32
        assert restored.orig_starts.dtype == np.int64
        assert restored.file_id.dtype == np.uint32
        assert restored.line_no.dtype == np.int32


class TestInvariantChecks:
    def test_a_healthy_store_passes(self, store):
        store.check_invariants()

    def test_catches_offsets_that_do_not_span_the_blob(self, store):
        from dataclasses import replace

        broken = replace(store, starts=store.starts.copy())
        broken.starts[-1] += 1
        with pytest.raises(RecordStoreError, match="do not span"):
            broken.check_structure()

    def test_catches_a_stray_byte_in_the_blob(self, store):
        from dataclasses import replace

        broken = replace(store, norm_blob=bytes(store.norm_blob).replace(b"a", b"A", 1))
        with pytest.raises(RecordStoreError, match="outside the alphabet"):
            broken.check_alphabet()

    def test_catches_a_record_pointing_at_an_unknown_file(self, store):
        from dataclasses import replace

        broken = replace(store, paths=store.paths[:1])
        with pytest.raises(RecordStoreError, match="not listed"):
            broken.check_structure()

    def test_catches_mismatched_array_lengths(self, store):
        from dataclasses import replace

        broken = replace(store, line_no=store.line_no[:-1])
        with pytest.raises(RecordStoreError):
            broken.check_structure()
