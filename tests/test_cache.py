"""Tests for saving, validating and reusing a built index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocomplete import corpus
from autocomplete.cache import (
    FORMAT_VERSION,
    MANIFEST_FILE,
    POINTER_FILE,
    CacheMiss,
    build_or_load,
    current_generation_name,
    load,
    load_current,
    save,
)
from autocomplete.config import Config
from autocomplete.index import SearchIndex

WIDTH = 5


def build_index(root, width: int = WIDTH) -> SearchIndex:
    return SearchIndex.build(root, summary_width=width)


CORPUS_FILES = {
    "a.txt": b"Alpha line one.\nAlpha line two.\n",
    "deep/b.txt": b"Beta line.\n\n   \nGamma line!\n",
}


def build_tree(root, files: dict[str, bytes] = CORPUS_FILES):
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def corpus_root(tmp_path):
    return build_tree(tmp_path / "corpus")


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


@pytest.fixture
def saved(corpus_root, cache_dir):
    """A cache holding the corpus, plus its fingerprint."""
    digest = corpus.fingerprint(corpus_root)
    generation = save(build_index(corpus_root), cache_dir, digest)
    return generation, digest


def config_for(corpus_root, cache_dir, **overrides) -> Config:
    return Config(corpus_root=corpus_root, cache_dir=cache_dir, **overrides)


class TestSaveAndLoad:
    def test_round_trip(self, saved, cache_dir, corpus_root):
        _, digest = saved
        index = load(cache_dir, corpus_hash=digest, level="full", summary_width=WIDTH)
        original = build_index(corpus_root).records
        assert len(index) == len(original)
        assert [index.records.sentence(i) for i in range(len(index))] == [
            original.sentence(i) for i in range(len(original))
        ]

    def test_pointer_names_the_generation(self, saved, cache_dir):
        generation, _ = saved
        assert (cache_dir / POINTER_FILE).read_text().strip() == generation.name

    def test_generation_holds_a_manifest_and_the_artifacts(self, saved):
        generation, _ = saved
        names = {path.name for path in generation.iterdir()}
        assert MANIFEST_FILE in names
        assert "norm_blob.bin" in names and "starts.npy" in names

    def test_manifest_records_the_decisions_that_shaped_the_index(self, saved):
        generation, digest = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        assert manifest["format_version"] == FORMAT_VERSION
        assert manifest["corpus_hash"] == digest
        assert manifest["punctuation_policy"] == "delete"
        assert manifest["record_count"] == 4

    @pytest.mark.parametrize("use_mmap", [True, False])
    def test_loads_either_mapped_or_read(self, saved, cache_dir, use_mmap):
        _, digest = saved
        index = load(cache_dir, corpus_hash=digest, use_mmap=use_mmap, summary_width=WIDTH)
        assert index.records.sentence(0) == "Alpha line one."

    def test_empty_corpus_round_trips(self, tmp_path):
        source = build_tree(tmp_path / "corpus", {"a.txt": b"\n   \n"})
        cache = tmp_path / "cache"
        digest = corpus.fingerprint(source)
        save(build_index(source), cache, digest)
        assert len(load(cache, corpus_hash=digest, level="full", summary_width=WIDTH)) == 0


class TestSearchArtifacts:
    """A generation must describe one index, never a mixture of two."""

    def test_generation_holds_the_search_structures(self, saved):
        generation, _ = saved
        names = {path.name for path in generation.iterdir()}
        assert "suffix_array.npy" in names
        assert "block_summaries.npy" in names

    def test_manifest_records_the_search_settings(self, saved):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        assert manifest["summary_width"] == WIDTH
        assert manifest["block_size"] == 4096
        assert manifest["tie_break"] == "original-sentence-codepoint"
        assert manifest["arrays"]["suffix_array"]["dtype"] == "int32"
        assert manifest["arrays"]["block_summaries"]["shape"][1] == WIDTH

    def test_suffix_array_covers_the_normalized_blob(self, saved, cache_dir):
        _, digest = saved
        index = load(cache_dir, corpus_hash=digest, summary_width=WIDTH)
        assert len(index.suffix) == len(index.records.norm_blob)

    def test_a_cache_summarizing_fewer_results_is_rejected(self, corpus_root, cache_dir):
        save(build_index(corpus_root, width=3), cache_dir, corpus.fingerprint(corpus_root))
        with pytest.raises(CacheMiss, match="summary_width"):
            load(cache_dir, level="structural", summary_width=5)

    def test_a_cache_built_with_another_block_size_is_rejected(self, saved, cache_dir):
        with pytest.raises(CacheMiss, match="block_size"):
            load(cache_dir, level="structural", summary_width=WIDTH, block_size=1024)

    def test_a_cache_built_under_another_tie_break_is_rejected(self, saved, cache_dir):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        manifest["tie_break"] = "normalized-sentence"
        (generation / MANIFEST_FILE).write_text(json.dumps(manifest))
        with pytest.raises(CacheMiss, match="tie_break"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_missing_suffix_array(self, saved, cache_dir):
        generation, _ = saved
        (generation / "suffix_array.npy").unlink()
        with pytest.raises(CacheMiss, match="is missing"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_missing_block_summaries(self, saved, cache_dir):
        generation, _ = saved
        (generation / "block_summaries.npy").unlink()
        with pytest.raises(CacheMiss, match="is missing"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    @pytest.mark.parametrize("artifact", ["suffix_array.npy", "block_summaries.npy"])
    def test_truncated_search_artifact(self, saved, cache_dir, artifact):
        generation, _ = saved
        path = generation / artifact
        path.write_bytes(path.read_bytes()[:-8])
        with pytest.raises(CacheMiss, match="bytes, manifest says"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_suffix_array_of_the_wrong_shape_is_caught(self, saved, cache_dir):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        manifest["arrays"]["suffix_array"]["shape"] = [7]
        (generation / MANIFEST_FILE).write_text(json.dumps(manifest))
        with pytest.raises(CacheMiss, match="suffix_array"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_suffix_array_of_the_wrong_dtype_is_caught(self, saved, cache_dir):
        import numpy as np

        generation, _ = saved
        positions = np.load(generation / "suffix_array.npy")
        np.save(generation / "suffix_array.npy", positions.astype(np.int64))
        with pytest.raises(CacheMiss):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_a_suffix_array_from_another_corpus_cannot_be_grafted_in(
        self, saved, cache_dir, tmp_path
    ):
        """The suffix array is only meaningful for the blob it was built from,
        so one sized for different text must be refused."""
        import numpy as np

        generation, digest = saved
        other = build_tree(tmp_path / "other", {"a.txt": b"Totally different text.\n"})
        foreign = build_index(other)
        np.save(generation / "suffix_array.npy", foreign.suffix.positions)
        with pytest.raises(CacheMiss):
            load(cache_dir, corpus_hash=digest, summary_width=WIDTH)


class TestValidationLevels:
    def test_content_level_notices_an_edited_corpus(self, saved, cache_dir, corpus_root):
        (corpus_root / "a.txt").write_bytes(b"Alpha line ONE.\nAlpha line two.\n")
        with pytest.raises(CacheMiss, match="corpus has changed"):
            load(
                cache_dir,
                corpus_hash=corpus.fingerprint(corpus_root),
                level="content",
                summary_width=WIDTH,
            )

    def test_structural_level_does_not_look_at_the_corpus(self, saved, cache_dir):
        assert len(load(cache_dir, level="structural", summary_width=WIDTH)) == 4

    def test_content_level_requires_the_fingerprint(self, saved, cache_dir):
        with pytest.raises(ValueError, match="needs the corpus hash"):
            load(cache_dir, level="content", summary_width=WIDTH)

    def test_unknown_level_is_rejected(self, saved, cache_dir):
        with pytest.raises(ValueError, match="unknown validation level"):
            load(cache_dir, corpus_hash="x", level="paranoid", summary_width=WIDTH)

    def test_only_the_full_level_detects_silent_corruption(self, saved, cache_dir):
        """A flipped byte that keeps the file size is invisible to the cheaper
        levels; that is the documented trade-off for a fast start-up."""
        generation, digest = saved
        blob = generation / "orig_blob.bin"
        data = bytearray(blob.read_bytes())
        data[0] = data[0] ^ 0x20
        blob.write_bytes(bytes(data))

        assert load(cache_dir, corpus_hash=digest, level="content", summary_width=WIDTH)
        with pytest.raises(CacheMiss, match="does not match its checksum"):
            load(cache_dir, corpus_hash=digest, level="full", summary_width=WIDTH)


class TestRejectsUnusableCaches:
    def test_no_cache_at_all(self, cache_dir):
        with pytest.raises(CacheMiss, match="no usable cache"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_missing_pointer(self, saved, cache_dir):
        (cache_dir / POINTER_FILE).unlink()
        with pytest.raises(CacheMiss, match="no usable cache"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_pointer_naming_a_missing_generation(self, saved, cache_dir):
        (cache_dir / POINTER_FILE).write_text("gen-does-not-exist\n")
        with pytest.raises(CacheMiss, match="missing generation"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    @pytest.mark.parametrize("hostile", ["../elsewhere", "/etc", "gen-a/../../x", ""])
    def test_pointer_cannot_address_anything_outside_the_cache(
        self, saved, cache_dir, hostile
    ):
        (cache_dir / POINTER_FILE).write_text(f"{hostile}\n")
        with pytest.raises(CacheMiss):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_missing_manifest(self, saved, cache_dir):
        generation, _ = saved
        (generation / MANIFEST_FILE).unlink()
        with pytest.raises(CacheMiss, match="no manifest"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_unreadable_manifest(self, saved, cache_dir):
        generation, _ = saved
        (generation / MANIFEST_FILE).write_text("{not json")
        with pytest.raises(CacheMiss, match="not valid JSON"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_manifest_from_a_different_format_version(self, saved, cache_dir):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        manifest["format_version"] = FORMAT_VERSION + 1
        (generation / MANIFEST_FILE).write_text(json.dumps(manifest))
        with pytest.raises(CacheMiss, match="format version"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_index_built_under_a_different_punctuation_policy(self, saved, cache_dir):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        manifest["punctuation_policy"] = "space"
        (generation / MANIFEST_FILE).write_text(json.dumps(manifest))
        with pytest.raises(CacheMiss, match="punctuation_policy"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_truncated_artifact(self, saved, cache_dir):
        generation, _ = saved
        blob = generation / "norm_blob.bin"
        blob.write_bytes(blob.read_bytes()[:-3])
        with pytest.raises(CacheMiss, match="bytes, manifest says"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_deleted_artifact(self, saved, cache_dir):
        generation, _ = saved
        (generation / "starts.npy").unlink()
        with pytest.raises(CacheMiss, match="is missing"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_manifest_describing_the_wrong_files(self, saved, cache_dir):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        del manifest["artifacts"]["line_no.npy"]
        (generation / MANIFEST_FILE).write_text(json.dumps(manifest))
        with pytest.raises(CacheMiss, match="expected set of files"):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_array_of_the_wrong_shape(self, saved, cache_dir):
        generation, _ = saved
        manifest = json.loads((generation / MANIFEST_FILE).read_text())
        manifest["arrays"]["line_no"]["shape"] = [999]
        (generation / MANIFEST_FILE).write_text(json.dumps(manifest))
        with pytest.raises(CacheMiss, match="shape"):
            load(cache_dir, level="structural", summary_width=WIDTH)


class TestInterruptedBuilds:
    """A build that dies must never damage the cache that was already there."""

    def test_a_half_written_generation_is_ignored_until_it_is_published(
        self, saved, cache_dir
    ):
        generation, digest = saved
        abandoned = cache_dir / "gen-deadbeef-partial"
        abandoned.mkdir()
        (abandoned / "norm_blob.bin").write_bytes(b"truncated")

        # The pointer still names the good generation, so loading is unaffected.
        assert len(load(cache_dir, corpus_hash=digest, level="full", summary_width=WIDTH)) == 4
        assert (cache_dir / POINTER_FILE).read_text().strip() == generation.name

    def test_publishing_a_broken_generation_is_reported_not_silently_used(
        self, saved, cache_dir
    ):
        abandoned = cache_dir / "gen-deadbeef-partial"
        abandoned.mkdir()
        (cache_dir / POINTER_FILE).write_text(f"{abandoned.name}\n")
        with pytest.raises(CacheMiss):
            load(cache_dir, level="structural", summary_width=WIDTH)

    def test_an_abandoned_generation_is_cleaned_up_by_the_next_build(
        self, saved, cache_dir, corpus_root
    ):
        abandoned = cache_dir / "gen-deadbeef-partial"
        abandoned.mkdir()
        (abandoned / "junk.bin").write_bytes(b"x")

        save(build_index(corpus_root), cache_dir, corpus.fingerprint(corpus_root))
        assert not abandoned.exists()

    def test_a_leftover_pointer_temp_file_is_cleaned_up(
        self, saved, cache_dir, corpus_root
    ):
        leftover = cache_dir / f"{POINTER_FILE}.tmp-999-abcdef"
        leftover.write_text("gen-something\n")

        save(build_index(corpus_root), cache_dir, corpus.fingerprint(corpus_root))
        assert not leftover.exists()

    def test_rebuilding_replaces_the_generation_and_keeps_one(
        self, saved, cache_dir, corpus_root
    ):
        first, digest = saved
        second = save(build_index(corpus_root), cache_dir, digest)

        assert second != first
        assert not first.exists()
        generations = [p for p in cache_dir.iterdir() if p.is_dir()]
        assert generations == [second]
        assert len(load(cache_dir, corpus_hash=digest, level="full", summary_width=WIDTH)) == 4

    def test_two_builds_in_a_row_leave_a_loadable_cache(self, corpus_root, cache_dir):
        """Stands in for two builders racing: whichever publishes last wins, and
        the pointer always names a complete generation."""
        digest = corpus.fingerprint(corpus_root)
        save(build_index(corpus_root), cache_dir, digest)
        save(build_index(corpus_root), cache_dir, digest)
        assert len(load(cache_dir, corpus_hash=digest, level="full", summary_width=WIDTH)) == 4


class TestCurrentGenerationName:
    """A cheap way to notice a new build without repeating full validation.

    This is what a long-running reader polls: reading one small file, never
    the index artifacts themselves, so watching for a new generation costs
    nothing like a reload does.
    """

    def test_no_cache_at_all(self, cache_dir):
        assert current_generation_name(cache_dir) is None

    def test_names_the_generation_the_pointer_names(self, saved, cache_dir):
        generation, _ = saved
        assert current_generation_name(cache_dir) == generation.name

    def test_changes_after_a_second_build_is_published(self, saved, cache_dir, corpus_root):
        first = current_generation_name(cache_dir)
        second = save(build_index(corpus_root), cache_dir, corpus.fingerprint(corpus_root))
        assert current_generation_name(cache_dir) == second.name
        assert current_generation_name(cache_dir) != first

    def test_pointer_naming_a_missing_generation_is_reported_as_no_generation(
        self, saved, cache_dir
    ):
        (cache_dir / POINTER_FILE).write_text("gen-does-not-exist\n")
        assert current_generation_name(cache_dir) is None

    def test_a_hostile_pointer_is_reported_as_no_generation(self, saved, cache_dir):
        (cache_dir / POINTER_FILE).write_text("../elsewhere\n")
        assert current_generation_name(cache_dir) is None


class TestLoadCurrent:
    """Loading paired with the exact generation name that produced it.

    ``load`` alone reads the ``CURRENT`` pointer internally, so a caller that
    also wants a label for what it loaded and reads the pointer separately
    can be given a different generation's name if a new one is published in
    between the two reads. ``load_current`` reads the pointer once and
    returns the index and its generation name from that same read, so the
    two can never disagree about what was actually loaded.
    """

    def test_returns_the_index_and_the_generation_it_was_loaded_from(
        self, saved, cache_dir
    ):
        generation, digest = saved
        index, generation_name = load_current(
            cache_dir, corpus_hash=digest, summary_width=WIDTH
        )
        assert generation_name == generation.name
        assert index.records.sentence(0) == "Alpha line one."

    def test_names_the_newest_generation_after_a_second_build(
        self, saved, cache_dir, corpus_root
    ):
        digest = corpus.fingerprint(corpus_root)
        second = save(build_index(corpus_root), cache_dir, digest)
        _, generation_name = load_current(cache_dir, corpus_hash=digest, summary_width=WIDTH)
        assert generation_name == second.name

    def test_raises_cache_miss_the_same_way_load_does(self, cache_dir):
        with pytest.raises(CacheMiss):
            load_current(cache_dir, summary_width=WIDTH, level="structural")


class TestBuildOrLoad:
    def test_builds_when_there_is_no_cache(self, corpus_root, cache_dir):
        messages: list[str] = []
        store = build_or_load(
            config_for(corpus_root, cache_dir), log=messages.append
        )
        assert len(store) == 4
        assert any("building the index" in m for m in messages)
        assert (cache_dir / POINTER_FILE).exists()

    def test_reuses_the_cache_on_the_second_call(self, corpus_root, cache_dir):
        build_or_load(config_for(corpus_root, cache_dir))
        messages: list[str] = []
        store = build_or_load(config_for(corpus_root, cache_dir), log=messages.append)
        assert len(store) == 4
        assert any("from cache" in m for m in messages)

    def test_rebuilds_after_the_corpus_changes(self, corpus_root, cache_dir):
        build_or_load(config_for(corpus_root, cache_dir))
        (corpus_root / "c.txt").write_bytes(b"Added line.\n")

        messages: list[str] = []
        store = build_or_load(config_for(corpus_root, cache_dir), log=messages.append)
        assert len(store) == 5
        assert any("corpus has changed" in m for m in messages)

    def test_force_rebuild_ignores_a_valid_cache(self, corpus_root, cache_dir):
        build_or_load(config_for(corpus_root, cache_dir))
        messages: list[str] = []
        build_or_load(
            config_for(corpus_root, cache_dir),
            force_rebuild=True,
            log=messages.append,
        )
        assert any("rebuild requested" in m for m in messages)

    def test_structural_level_reuses_a_cache_of_a_changed_corpus(
        self, corpus_root, cache_dir
    ):
        """Documents what the cheapest level buys and costs: start-up does not
        read the corpus, so an edited corpus is not noticed."""
        build_or_load(config_for(corpus_root, cache_dir, validation_level="structural"))
        (corpus_root / "c.txt").write_bytes(b"Added line.\n")
        store = build_or_load(
            config_for(corpus_root, cache_dir, validation_level="structural")
        )
        assert len(store) == 4

    def test_recovers_from_a_damaged_cache_by_rebuilding(self, corpus_root, cache_dir):
        build_or_load(config_for(corpus_root, cache_dir))
        (cache_dir / POINTER_FILE).write_text("gen-nonsense\n")

        messages: list[str] = []
        store = build_or_load(config_for(corpus_root, cache_dir), log=messages.append)
        assert len(store) == 4
        assert any("building the index" in m for m in messages)

    def test_missing_corpus_is_reported(self, tmp_path):
        from autocomplete.corpus import CorpusNotFoundError

        with pytest.raises(CorpusNotFoundError):
            build_or_load(config_for(tmp_path / "absent", tmp_path / "cache"))


class TestPortableDurability:
    """Publishing a generation must work on Linux, macOS and Windows.

    The three differ in what they will let a process do to a file it has just
    written, and each difference here is one that stops a build outright
    rather than degrading it.
    """

    def test_a_directory_that_cannot_be_opened_is_not_an_error(self, tmp_path, monkeypatch):
        """Windows refuses to open a directory as a file at all, so there is no
        handle to sync. That is a platform without the call, not a failed
        call: it must not stop a generation being published."""
        import autocomplete.cache as cache_module

        real_open = cache_module.os.open

        def refuse_directories(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(cache_module.os, "open", refuse_directories)

        root = build_tree(tmp_path / "corpus")
        cache_dir = tmp_path / "cache"
        generation = save(build_index(root), cache_dir, corpus.fingerprint(root))

        assert (cache_dir / POINTER_FILE).read_text(encoding="utf-8").strip() == (
            generation.name
        )
        assert current_generation_name(cache_dir) == generation.name

    def test_files_are_flushed_through_a_writable_handle_on_windows(
        self, tmp_path, monkeypatch
    ):
        """Windows flushes through the handle, so a read-only one has nothing
        to commit; POSIX fsyncs the file and takes any descriptor."""
        import autocomplete.cache as cache_module

        seen: list[int] = []
        real_open = cache_module.os.open

        def record_flags(path, flags, *args, **kwargs):
            seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(cache_module.os, "open", record_flags)
        monkeypatch.setattr(cache_module, "_IS_WINDOWS", True)

        target = tmp_path / "artifact.bin"
        target.write_bytes(b"published")
        cache_module._flush_file(target)

        assert seen == [cache_module.os.O_RDWR]

    def test_the_pointer_rename_waits_out_a_windows_reader(self, tmp_path, monkeypatch):
        """A server polling CURRENT holds it open for an instant, and Windows
        makes a rename onto a held file fail instead of queue. Losing an
        already-built generation to that overlap would be absurd."""
        import autocomplete.cache as cache_module

        monkeypatch.setattr(cache_module, "_IS_WINDOWS", True)

        attempts = 0
        real_replace = cache_module.os.replace

        def fail_twice(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise PermissionError(13, "Permission denied")
            return real_replace(source, destination)

        monkeypatch.setattr(cache_module.os, "replace", fail_twice)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache_module._point_at(cache_dir, "gen-abcdef012345-0011a2b3")

        assert attempts == 3
        assert (cache_dir / POINTER_FILE).read_text(encoding="utf-8").strip() == (
            "gen-abcdef012345-0011a2b3"
        )

    def test_a_pointer_rename_that_never_succeeds_still_raises(self, tmp_path, monkeypatch):
        """Retrying is for outlasting a reader, not for hiding a real failure."""
        import autocomplete.cache as cache_module

        monkeypatch.setattr(cache_module, "_IS_WINDOWS", True)
        monkeypatch.setattr(cache_module, "_POINTER_REPLACE_TIMEOUT", 0.05)

        def always_fail(source, destination):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(cache_module.os, "replace", always_fail)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with pytest.raises(PermissionError):
            cache_module._point_at(cache_dir, "gen-abcdef012345-0011a2b3")
