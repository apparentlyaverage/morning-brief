"""Icon extraction.

The Windows half (SHGetFileInfoW, Get-AppxPackage) needs a real desktop, so
what's covered here is the part that can go wrong silently: the hand-written
PNG encoder, favicon lookup against a stand-in database, and the caching and
fallback rules.
"""

import sqlite3
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import icons


class PngWriter(unittest.TestCase):
    """No Pillow in this project, so the encoder is ours to get right."""

    def test_it_writes_a_real_png_header(self):
        data = icons._png(2, 2, bytes(2 * 2 * 4))
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_the_dimensions_are_in_the_header(self):
        data = icons._png(7, 3, bytes(7 * 3 * 4))
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (7, 3))

    def test_the_chunks_carry_valid_crcs(self):
        data = icons._png(4, 4, bytes(4 * 4 * 4))
        pos, seen = 8, []
        while pos < len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            tag = data[pos + 4:pos + 8]
            body = data[pos + 4:pos + 8 + length]
            crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
            self.assertEqual(crc, zlib.crc32(body) & 0xFFFFFFFF, tag)
            seen.append(tag)
            pos += 12 + length
        self.assertEqual(seen, [b"IHDR", b"IDAT", b"IEND"])

    def test_the_pixels_survive_a_round_trip(self):
        # One red pixel, fully opaque.
        data = icons._png(1, 1, bytes([255, 0, 0, 255]))
        start = data.index(b"IDAT") + 4
        end = data.index(b"IEND") - 4
        raw = zlib.decompress(data[start:end])
        self.assertEqual(raw, b"\x00" + bytes([255, 0, 0, 255]))


class Favicons(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "Favicons"
        original = icons.FAVICON_DBS
        icons.FAVICON_DBS = [self.db]
        self.addCleanup(setattr, icons, "FAVICON_DBS", original)

    def make(self, rows):
        """rows: (page_url, width, blob)"""
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE icon_mapping (icon_id INT, page_url TEXT)")
        con.execute("CREATE TABLE favicon_bitmaps (icon_id INT, width INT, image_data BLOB)")
        for i, (url, width, blob) in enumerate(rows):
            con.execute("INSERT INTO icon_mapping VALUES (?,?)", (i, url))
            con.execute("INSERT INTO favicon_bitmaps VALUES (?,?,?)", (i, width, blob))
        con.commit()
        con.close()

    def test_a_domain_finds_its_icon(self):
        self.make([("https://chess.com/play", 32, b"ICONDATA")])
        self.assertEqual(icons.site_icon("chess.com"), b"ICONDATA")

    def test_a_www_prefixed_page_still_matches_the_bare_domain(self):
        self.make([("https://www.chess.com/play", 32, b"ICONDATA")])
        self.assertEqual(icons.site_icon("chess.com"), b"ICONDATA")

    def test_the_largest_bitmap_wins(self):
        self.make([("https://chess.com/a", 16, b"SMALL"),
                   ("https://chess.com/b", 64, b"LARGE"),
                   ("https://chess.com/c", 32, b"MID")])
        self.assertEqual(icons.site_icon("chess.com"), b"LARGE")

    def test_absurdly_large_bitmaps_are_skipped(self):
        # A 512px favicon is a wasteful thing to inline into a 48px tile.
        self.make([("https://chess.com/a", 32, b"SENSIBLE"),
                   ("https://chess.com/b", 512, b"HUGE")])
        self.assertEqual(icons.site_icon("chess.com"), b"SENSIBLE")

    def test_an_unknown_domain_gives_nothing(self):
        self.make([("https://chess.com/play", 32, b"ICONDATA")])
        self.assertIsNone(icons.site_icon("never-visited.example"))

    def test_empty_blobs_are_ignored(self):
        self.make([("https://chess.com/play", 32, b"")])
        self.assertIsNone(icons.site_icon("chess.com"))

    def test_a_blank_domain_is_not_a_lookup(self):
        self.make([("https://chess.com/play", 32, b"ICONDATA")])
        self.assertIsNone(icons.site_icon(""))

    def test_a_missing_database_is_not_fatal(self):
        icons.FAVICON_DBS = [self.dir / "no-such-file"]
        self.assertIsNone(icons.site_icon("chess.com"))

    def test_a_corrupt_database_is_not_fatal(self):
        self.db.write_bytes(b"definitely not sqlite")
        self.assertIsNone(icons.site_icon("chess.com"))


class FileIcons(unittest.TestCase):
    def test_a_path_that_does_not_exist_gives_nothing(self):
        self.assertIsNone(icons.file_icon(r"C:\nope\does-not-exist.exe"))

    def test_an_empty_path_gives_nothing(self):
        self.assertIsNone(icons.file_icon(""))


class Caching(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        original = icons.CACHE_DIR
        icons.CACHE_DIR = self.dir / "icon_cache"
        self.addCleanup(setattr, icons, "CACHE_DIR", original)

        original_site = icons.site_icon
        self.calls = []
        def counting(domain):
            self.calls.append(domain)
            return b"FAKEPNG"
        icons.site_icon = counting
        self.addCleanup(setattr, icons, "site_icon", original_site)

    def row(self):
        return {"id": "site:chess.com", "kind": "site", "target": "https://chess.com/"}

    def test_the_first_lookup_hits_the_source(self):
        self.assertEqual(icons.for_shortcut(self.row()), b"FAKEPNG")
        self.assertEqual(self.calls, ["chess.com"])

    def test_the_second_lookup_comes_from_the_cache(self):
        icons.for_shortcut(self.row())
        icons.for_shortcut(self.row())
        self.assertEqual(len(self.calls), 1, "went back to the source unnecessarily")

    def test_www_is_stripped_before_the_lookup(self):
        icons.for_shortcut({"id": "site:x", "kind": "site", "target": "https://www.chess.com/"})
        self.assertEqual(self.calls, ["chess.com"])

    def test_a_row_without_an_id_is_not_cached(self):
        self.assertIsNone(icons.for_shortcut({"id": "", "kind": "site", "target": "https://x/"}))

    def test_nothing_found_means_nothing_written(self):
        icons.site_icon = lambda domain: None
        self.assertIsNone(icons.for_shortcut(self.row()))
        cached = list(icons.CACHE_DIR.glob("*.png")) if icons.CACHE_DIR.exists() else []
        self.assertEqual(cached, [])


if __name__ == "__main__":
    unittest.main()
