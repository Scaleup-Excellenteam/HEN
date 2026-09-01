"""Tests for the imported corpus on disk.

The properties under test are the ones the feature's safety rests on: a name
from Drive can only ever become a file name, a publish is all-or-nothing, and a
failure leaves whatever was serving before still serving.
"""

from __future__ import annotations

import json

import pytest

from autocomplete.cache import POINTER_FILE
from autocomplete.drive.errors import StoreCorruptError
from autocomplete.drive.store import (
    FORMAT_VERSION,
    MANIFEST_FILE,
    SOURCES_DIR,
    DriveStore,
    ImportedDocument,
    PreparedDocument,
    document_id,
    safe_filename,
)

WIDTH = 5


def document(name="notes", *, drive_id=None, source_text=None, sha="0" * 64):
    drive_id = drive_id or f"drive-{name}"
    return ImportedDocument(
        id=document_id(drive_id),
        drive_file_id=drive_id,
        name=name,
        mime_type="text/plain",
        source_text=source_text or f"Google Drive/{name}.txt",
        modified_time="2026-09-01T10:00:00.000Z",
        revision_id="rev-1",
        imported_at="2026-09-01T10:00:01+00:00",
        content_sha256=sha,
        bytes=0,
        sentences=0,
    )


def prepared(name="notes", text=b"one line\ntwo line\n", **kwargs):
    entry = document(name, **kwargs)
    return PreparedDocument(document=entry, text=text)


@pytest.fixture
def store(tmp_path) -> DriveStore:
    return DriveStore(tmp_path / "data", summary_width=WIDTH, use_mmap=False)


class TestSafeFilename:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("notes.txt", "notes"),
            ("Meeting Notes", "Meeting Notes"),
            ("notes.TXT", "notes"),
            ("report (final).txt", "report (final)"),
            ("a  b   c", "a b c"),
            ("  padded  ", "padded"),
        ],
    )
    def test_ordinary_names_survive_readably(self, given, expected):
        assert safe_filename(given) == expected

    @pytest.mark.parametrize(
        "attack",
        [
            "../../etc/passwd",
            "/etc/passwd",
            "..\\..\\windows\\system32\\config",
            "....//....//secret",
            "dir/sub/file.txt",
        ],
    )
    def test_a_path_can_never_survive_as_a_path(self, attack):
        result = safe_filename(attack)
        assert "/" not in result
        assert "\\" not in result
        assert not result.startswith(".")
        assert result != ".."

    @pytest.mark.parametrize(
        "given", ["", "   ", "...", "///", "\x00\x01", "..", "\n\t"]
    )
    def test_a_name_that_empties_out_becomes_a_placeholder(self, given):
        assert safe_filename(given) == "document"

    def test_control_characters_are_replaced(self):
        assert "\n" not in safe_filename("two\nlines")
        assert "\x00" not in safe_filename("nul\x00byte")

    def test_expansion_and_redirection_characters_are_replaced(self):
        """Round brackets are deliberately kept: real names use them, the
        disambiguating counter needs them, and nothing here reaches a shell."""
        result = safe_filename("$(rm -rf ~) `id` & echo > out | cat; '\"*?.txt")
        for character in "$`&|<>;'\"*?~":
            assert character not in result

    def test_a_very_long_name_is_bounded(self):
        assert len(safe_filename("x" * 5000)) <= 120

    def test_the_result_is_the_same_every_time(self):
        assert safe_filename("Réunion") == safe_filename("Réunion")


class TestDocumentId:
    def test_it_is_stable(self):
        assert document_id("abc") == document_id("abc")

    def test_different_files_get_different_identifiers(self):
        assert document_id("abc") != document_id("abd")

    def test_it_is_safe_in_a_path_or_a_url(self):
        assert document_id("../../etc/passwd").isalnum()

    def test_it_does_not_contain_the_drive_identifier(self):
        assert "1a2b3c" not in document_id("1a2b3c")


class TestSourceTextChoice:
    def test_a_first_document_keeps_its_name(self, store):
        assert store.choose_source_text("notes.txt", []) == "Google Drive/notes.txt"

    def test_a_google_doc_becomes_a_text_file(self, store):
        assert store.choose_source_text("Meeting Notes", []) == (
            "Google Drive/Meeting Notes.txt"
        )

    def test_a_clash_is_disambiguated(self, store):
        taken = ["Google Drive/notes.txt"]
        assert store.choose_source_text("notes", taken) == "Google Drive/notes (2).txt"

    def test_several_clashes_count_up(self, store):
        taken = ["Google Drive/notes.txt", "Google Drive/notes (2).txt"]
        assert store.choose_source_text("notes", taken) == "Google Drive/notes (3).txt"

    def test_a_traversal_attempt_lands_inside_the_namespace(self, store):
        chosen = store.choose_source_text("../../escape", [])
        assert chosen.startswith("Google Drive/")
        assert ".." not in chosen


class TestEmptyStore:
    def test_nothing_published_reads_as_nothing(self, store):
        assert store.load() is None

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert DriveStore(tmp_path / "absent", summary_width=WIDTH).load() is None


class TestPublishing:
    def test_a_published_generation_is_searchable(self, store):
        corpus = store.publish([prepared()])
        assert corpus.index is not None
        assert corpus.sentences == 2
        assert len(corpus.documents) == 1

    def test_it_reads_back_after_a_restart(self, store, tmp_path):
        store.publish([prepared()])
        reopened = DriveStore(tmp_path / "data", summary_width=WIDTH, use_mmap=False)
        corpus = reopened.load()
        assert corpus is not None
        assert corpus.sentences == 2
        assert corpus.documents[0].source_text == "Google Drive/notes.txt"

    def test_the_source_text_is_what_results_report(self, store):
        corpus = store.publish([prepared()])
        assert corpus.index.records.source_text(0) == "Google Drive/notes.txt"

    def test_sentence_counts_are_filled_in(self, store):
        corpus = store.publish([prepared(text=b"a\nb\nc\n\n\n")])
        assert corpus.documents[0].sentences == 3

    def test_the_text_is_kept_beside_the_index(self, store):
        corpus = store.publish([prepared()])
        stored = corpus.generation / SOURCES_DIR / "Google Drive/notes.txt"
        assert stored.read_bytes() == b"one line\ntwo line\n"

    def test_publishing_nothing_leaves_no_index(self, store):
        corpus = store.publish([])
        assert corpus.index is None
        assert corpus.documents == ()
        assert bool(corpus) is False

    def test_an_empty_state_reads_back_as_empty(self, store, tmp_path):
        store.publish([])
        reopened = DriveStore(tmp_path / "data", summary_width=WIDTH, use_mmap=False)
        assert reopened.load().index is None

    def test_several_documents_are_indexed_together(self, store):
        corpus = store.publish(
            [
                prepared("first", b"alpha one\n"),
                prepared("second", b"beta two\nbeta three\n"),
            ]
        )
        assert corpus.sentences == 3
        assert {document.name for document in corpus.documents} == {"first", "second"}

    def test_two_documents_with_the_same_identity_are_refused(self, store):
        entry = prepared("a")
        with pytest.raises(StoreCorruptError, match="same id"):
            store.publish([entry, entry])

    def test_the_fingerprint_follows_the_content(self, store):
        first = store.publish([prepared(text=b"one\n")]).fingerprint
        second = store.publish([prepared(text=b"two\n", sha="1" * 64)]).fingerprint
        assert first != second


class TestAtomicity:
    def test_only_one_generation_survives_a_publish(self, store):
        store.publish([prepared("first")])
        store.publish([prepared("second")])
        generations = sorted(
            entry.name for entry in store.data_dir.iterdir() if entry.is_dir()
        )
        assert len(generations) == 1

    def test_a_failed_publish_leaves_the_previous_one_serving(self, store, monkeypatch):
        store.publish([prepared("first", b"alpha one\n")])
        before = store.load()

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(
            "autocomplete.drive.store.SearchIndex.build", staticmethod(explode)
        )
        with pytest.raises(OSError):
            store.publish([prepared("second", b"beta two\n")])

        after = store.load()
        assert after.generation == before.generation
        assert [entry.name for entry in after.documents] == ["first"]
        assert after.sentences == 1

    def test_a_failed_publish_leaves_no_reachable_leftovers(self, store, monkeypatch):
        store.publish([prepared("first")])
        kept = store.load().generation.name

        monkeypatch.setattr(
            "autocomplete.drive.store.SearchIndex.build",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))),
        )
        with pytest.raises(OSError):
            store.publish([prepared("second")])

        generations = [entry.name for entry in store.data_dir.iterdir() if entry.is_dir()]
        assert generations == [kept]

    def test_an_interrupted_publish_before_the_pointer_moves_changes_nothing(
        self, store, monkeypatch
    ):
        store.publish([prepared("first")])
        before = store.load().generation

        monkeypatch.setattr(
            "autocomplete.drive.store.publish_pointer",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            store.publish([prepared("second")])
        assert store.load().generation == before

    def test_the_pointer_is_the_only_thing_that_publishes(self, store):
        store.publish([prepared("first")])
        pointer = (store.data_dir / POINTER_FILE).read_text(encoding="utf-8").strip()
        assert pointer == store.load().generation.name


class TestMalformedState:
    def test_a_manifest_that_is_not_json_fails_with_an_actionable_message(self, store):
        corpus = store.publish([prepared()])
        (corpus.generation / MANIFEST_FILE).write_text("{oh no", encoding="utf-8")
        with pytest.raises(StoreCorruptError, match="not valid JSON"):
            store.load()

    def test_a_missing_manifest_is_reported(self, store):
        corpus = store.publish([prepared()])
        (corpus.generation / MANIFEST_FILE).unlink()
        with pytest.raises(StoreCorruptError, match="no manifest"):
            store.load()

    def test_a_manifest_from_another_format_version_is_refused(self, store):
        corpus = store.publish([prepared()])
        path = corpus.generation / MANIFEST_FILE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["format_version"] = FORMAT_VERSION + 1
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(StoreCorruptError, match="format version"):
            store.load()

    def test_an_index_built_for_a_different_k_is_refused(self, store, tmp_path):
        store.publish([prepared()])
        narrower = DriveStore(tmp_path / "data", summary_width=WIDTH + 1, use_mmap=False)
        with pytest.raises(StoreCorruptError, match="results at a time"):
            narrower.load()

    def test_a_manifest_naming_a_path_outside_the_namespace_is_refused(self, store):
        corpus = store.publish([prepared()])
        path = corpus.generation / MANIFEST_FILE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["documents"][0]["source_text"] = "../../etc/passwd"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(StoreCorruptError, match="unsafe source path"):
            store.load()

    def test_a_manifest_whose_count_disagrees_with_the_index_is_refused(self, store):
        corpus = store.publish([prepared()])
        path = corpus.generation / MANIFEST_FILE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["record_count"] = 99
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(StoreCorruptError, match="its manifest says"):
            store.load()

    def test_missing_index_artifacts_are_reported_not_ignored(self, store):
        corpus = store.publish([prepared()])
        (corpus.generation / "block_summaries.npy").unlink()
        with pytest.raises(StoreCorruptError, match="could not be read"):
            store.load()

    def test_a_pointer_to_nowhere_reads_as_nothing_published(self, store):
        store.publish([prepared()])
        (store.data_dir / POINTER_FILE).write_text("gen-missing\n", encoding="utf-8")
        assert store.load() is None

    def test_a_pointer_that_escapes_the_directory_is_refused(self, store):
        store.publish([prepared()])
        (store.data_dir / POINTER_FILE).write_text("../../../etc\n", encoding="utf-8")
        assert store.load() is None

    def test_a_manifest_entry_missing_a_field_is_refused(self, store):
        corpus = store.publish([prepared()])
        path = corpus.generation / MANIFEST_FILE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["documents"][0]["drive_file_id"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(StoreCorruptError, match="missing drive_file_id"):
            store.load()


class TestReusingPublishedText:
    def test_a_rebuild_can_copy_from_the_generation_now_serving(self, store):
        first = store.publish([prepared("kept", b"kept line\n"), prepared("gone", b"x\n")])
        kept = first.documents[0] if first.documents[0].name == "kept" else first.documents[1]

        rebuilt = store.publish(
            [PreparedDocument(document=kept, copy_from=first.source_path(kept))]
        )
        assert rebuilt.sentences == 1
        assert rebuilt.index.records.sentence(0) == "kept line"

    def test_a_prepared_document_with_no_content_is_refused(self, store):
        with pytest.raises(StoreCorruptError, match="no content available"):
            store.publish([PreparedDocument(document=document())])
