"""The read-aloud library: text tidying, passage splitting, position memory.

PDF extraction is deliberately not tested here - it needs pypdf and a real
PDF, and the interesting logic (cleaning and splitting) is downstream of it
and covered directly.
"""

import tempfile
import unittest
from pathlib import Path

import documents as docs


class CleanForSpeech(unittest.TestCase):
    def test_hyphenated_line_break_is_rejoined(self):
        self.assertEqual(docs.clean_for_speech("inter-\nnational"), "international")

    def test_ordinary_line_break_is_kept(self):
        self.assertEqual(docs.clean_for_speech("one\ntwo"), "one\ntwo")

    def test_runs_of_spaces_and_tabs_collapse(self):
        self.assertEqual(docs.clean_for_speech("a   \t  b"), "a b")

    def test_three_or_more_blank_lines_become_one_paragraph_break(self):
        self.assertEqual(docs.clean_for_speech("a\n\n\n\n\nb"), "a\n\nb")

    def test_markdown_headings_are_stripped(self):
        self.assertEqual(docs.clean_for_speech("## Chapter One"), "Chapter One")
        self.assertEqual(docs.clean_for_speech("   # Indented"), "Indented")

    def test_emphasis_markers_are_stripped(self):
        self.assertEqual(docs.clean_for_speech("**bold** and *italic* and `code`"),
                         "bold and italic and code")
        self.assertEqual(docs.clean_for_speech("__also bold__"), "also bold")

    def test_windows_line_endings_are_normalised(self):
        self.assertEqual(docs.clean_for_speech("a\r\nb\rc"), "a\nb\nc")

    def test_leading_and_trailing_whitespace_goes(self):
        self.assertEqual(docs.clean_for_speech("\n\n  hello  \n\n"), "hello")

    def test_a_hash_mid_sentence_is_not_a_heading(self):
        self.assertEqual(docs.clean_for_speech("issue #42 was closed"),
                         "issue #42 was closed")


class SplitPassages(unittest.TestCase):
    @staticmethod
    def para(words, word="word"):
        return " ".join([word] * words)

    def test_short_text_is_a_single_passage(self):
        self.assertEqual(docs.split_passages("just a few words", words=220),
                         ["just a few words"])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(docs.split_passages(""), [])
        self.assertEqual(docs.split_passages("\n\n   \n\n"), [])

    def test_paragraphs_are_packed_up_to_the_limit(self):
        text = "\n\n".join([self.para(30)] * 10)
        passages = docs.split_passages(text, words=100)
        # 30-word paragraphs pack four at a time (the fourth crosses 100).
        self.assertEqual([len(p.split()) for p in passages], [120, 120, 60])

    def test_packed_paragraphs_keep_their_blank_line(self):
        text = f"{self.para(10, 'alpha')}\n\n{self.para(10, 'beta')}"
        self.assertIn("\n\n", docs.split_passages(text, words=100)[0])

    def test_an_enormous_paragraph_is_split_on_sentences(self):
        sentences = " ".join(f"{self.para(20)} number {i}." for i in range(20))
        passages = docs.split_passages(sentences, words=50)
        self.assertGreater(len(passages), 1)
        # Splitting happens after a full stop, so no passage starts mid-sentence.
        self.assertTrue(all(p[0].isalpha() for p in passages))
        self.assertTrue(all(p.rstrip().endswith(".") for p in passages))

    def test_a_long_paragraph_flushes_the_buffer_first(self):
        # The short paragraph must not get glued onto the long one's chunks.
        text = f"{self.para(5, 'short')}\n\n" + " ".join(
            f"{self.para(20, 'long')} n{i}." for i in range(20))
        passages = docs.split_passages(text, words=50)
        self.assertEqual(passages[0], self.para(5, "short"))

    def test_no_passage_is_blank(self):
        text = "\n\n".join([self.para(30), "", "   ", self.para(30)])
        self.assertTrue(all(p.strip() for p in docs.split_passages(text, words=20)))

    def test_every_word_survives_the_split(self):
        text = "\n\n".join(f"para {i} " + self.para(40) for i in range(12))
        rejoined = " ".join(docs.split_passages(text, words=100)).split()
        self.assertEqual(len(rejoined), len(text.split()))


class LibraryCase(unittest.TestCase):
    """Redirects the library at a temp directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "library"
        self.dir.mkdir()

        for name, value in (("LIBRARY_DIR", self.dir),
                            ("INDEX", self.dir / "index.json")):
            original = getattr(docs, name)
            setattr(docs, name, value)
            self.addCleanup(setattr, docs, name, original)

    @staticmethod
    def sample(paragraphs=20, words=60):
        """Long enough to split into several passages at the 220-word default,
        so the clamping tests have room either side of a valid index."""
        return "\n\n".join(" ".join([f"p{i}"] * words) for i in range(paragraphs))


class AddDocument(LibraryCase):
    def test_a_text_file_is_added_and_indexed(self):
        book = docs.add_document("Notes.txt", self.sample().encode("utf-8"))
        self.assertEqual(book["title"], "Notes")
        self.assertEqual(book["filename"], "Notes.txt")
        self.assertEqual(book["position"], 0)
        self.assertIsNone(book["last_opened"])
        self.assertGreater(book["passages"], 0)
        self.assertEqual(book["words"], len(self.sample().split()))
        self.assertEqual([b["id"] for b in docs.list_books()], [book["id"]])

    def test_markdown_is_supported(self):
        book = docs.add_document("Notes.md", b"# Title\n\nSome body text here.")
        self.assertEqual(docs.get_passage(book["id"], 0)["text"],
                         "Title\n\nSome body text here.")

    def test_an_unsupported_extension_is_refused_with_a_useful_message(self):
        with self.assertRaises(ValueError) as caught:
            docs.add_document("photo.jpg", b"\xff\xd8\xff")
        self.assertIn(".txt", str(caught.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(ValueError):
            docs.add_document("empty.txt", b"")

    def test_a_file_with_no_readable_text_is_refused(self):
        with self.assertRaises(ValueError):
            docs.add_document("blank.txt", b"   \n\n  \n")

    def test_newest_document_comes_first(self):
        first = docs.add_document("First.txt", self.sample().encode("utf-8"))
        second = docs.add_document("Second.txt", self.sample().encode("utf-8"))
        self.assertEqual([b["id"] for b in docs.list_books()],
                         [second["id"], first["id"]])

    def test_a_very_long_filename_is_truncated_for_the_title(self):
        book = docs.add_document("x" * 300 + ".txt", self.sample().encode("utf-8"))
        self.assertEqual(len(book["title"]), 120)


class Passages(LibraryCase):
    def setUp(self):
        super().setUp()
        self.book = docs.add_document("Notes.txt", self.sample().encode("utf-8"))

    def test_passage_reports_position_and_progress(self):
        passage = docs.get_passage(self.book["id"], 0)
        self.assertEqual(passage["index"], 0)
        self.assertEqual(passage["total"], self.book["passages"])
        self.assertEqual(passage["title"], "Notes")
        self.assertTrue(passage["text"])

    def test_the_last_passage_reads_one_hundred_percent(self):
        last = self.book["passages"] - 1
        self.assertEqual(docs.get_passage(self.book["id"], last)["progress"], 100)

    def test_an_index_past_the_end_clamps_to_the_last_passage(self):
        passage = docs.get_passage(self.book["id"], 9999)
        self.assertEqual(passage["index"], self.book["passages"] - 1)

    def test_a_negative_index_clamps_to_the_first_passage(self):
        self.assertEqual(docs.get_passage(self.book["id"], -5)["index"], 0)

    def test_an_unknown_document_raises(self):
        with self.assertRaises(ValueError):
            docs.get_passage("nope", 0)

    def test_a_missing_text_file_raises_rather_than_crashing(self):
        (self.dir / f"{self.book['id']}.json").unlink()
        with self.assertRaises(ValueError):
            docs.get_passage(self.book["id"], 0)


class PositionMemory(LibraryCase):
    def setUp(self):
        super().setUp()
        self.book = docs.add_document("Notes.txt", self.sample().encode("utf-8"))

    def test_position_is_remembered(self):
        docs.set_position(self.book["id"], 2)
        self.assertEqual(docs.get_book(self.book["id"])["position"], 2)

    def test_setting_a_position_stamps_last_opened(self):
        self.assertIsNotNone(docs.set_position(self.book["id"], 1)["last_opened"])

    def test_position_clamps_to_the_last_passage(self):
        docs.set_position(self.book["id"], 9999)
        self.assertEqual(docs.get_book(self.book["id"])["position"],
                         self.book["passages"] - 1)

    def test_negative_position_clamps_to_zero(self):
        docs.set_position(self.book["id"], -3)
        self.assertEqual(docs.get_book(self.book["id"])["position"], 0)

    def test_setting_a_position_on_an_unknown_document_raises(self):
        with self.assertRaises(ValueError):
            docs.set_position("nope", 0)

    def test_one_document_s_position_does_not_disturb_another(self):
        other = docs.add_document("Other.txt", self.sample().encode("utf-8"))
        docs.set_position(self.book["id"], 2)
        self.assertEqual(docs.get_book(other["id"])["position"], 0)


class Deletion(LibraryCase):
    def test_deleting_removes_the_index_row_and_the_text(self):
        book = docs.add_document("Notes.txt", self.sample().encode("utf-8"))
        text_file = self.dir / f"{book['id']}.json"
        self.assertTrue(text_file.exists())

        self.assertTrue(docs.delete_book(book["id"]))
        self.assertEqual(docs.list_books(), [])
        self.assertFalse(text_file.exists())

    def test_deleting_an_unknown_document_reports_no_change(self):
        self.assertFalse(docs.delete_book("nope"))

    def test_deleting_one_leaves_the_others_readable(self):
        keep = docs.add_document("Keep.txt", self.sample().encode("utf-8"))
        drop = docs.add_document("Drop.txt", self.sample().encode("utf-8"))
        docs.delete_book(drop["id"])
        self.assertEqual([b["id"] for b in docs.list_books()], [keep["id"]])
        self.assertTrue(docs.get_passage(keep["id"], 0)["text"])


class IndexRobustness(LibraryCase):
    def test_a_corrupt_index_reads_as_empty_rather_than_raising(self):
        (self.dir / "index.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(docs.list_books(), [])

    def test_get_book_on_an_empty_library_returns_none(self):
        self.assertIsNone(docs.get_book("anything"))


if __name__ == "__main__":
    unittest.main()
