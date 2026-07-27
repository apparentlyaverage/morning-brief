"""The frequently-used panel.

The detection halves - reading browser history and the Windows launch counts -
depend on this machine's real data, so they are exercised against a synthetic
SQLite database and skipped where the registry isn't available. Everything
else (grouping, pinning, hiding, and the id-only launch rule) is pure logic
and tested directly.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import shortcuts


class StoreCase(unittest.TestCase):
    """Isolates the store, and cuts the tests off from this machine's real
    history and launch counts - otherwise "the panel should now be empty"
    depends on whatever the developer happens to have installed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

        original_store = shortcuts.STORE
        shortcuts.STORE = self.dir / "shortcuts.json"
        self.addCleanup(setattr, shortcuts, "STORE", original_store)

        original_browsers = shortcuts.BROWSERS
        shortcuts.BROWSERS = {}
        self.addCleanup(setattr, shortcuts, "BROWSERS", original_browsers)

        original_apps = shortcuts.frequent_apps
        shortcuts.frequent_apps = lambda limit=8: []
        self.addCleanup(setattr, shortcuts, "frequent_apps", original_apps)


class Domains(unittest.TestCase):
    def test_www_is_stripped_so_it_groups_with_the_bare_domain(self):
        self.assertEqual(shortcuts._domain("https://www.chess.com/play"), "chess.com")
        self.assertEqual(shortcuts._domain("https://chess.com/"), "chess.com")

    def test_the_path_is_discarded(self):
        # The raw history is topped by deep CMS links; the domain is the useful
        # unit, otherwise one PDF anchor outranks everything you actually use.
        self.assertEqual(
            shortcuts._domain("https://learn.example.ac/pluginfile.php/58121/notes.pdf"),
            "learn.example.ac")

    def test_rubbish_does_not_raise(self):
        self.assertEqual(shortcuts._domain("not a url"), "")


class HistoryReading(StoreCase):
    """browser_sites against a stand-in History database."""

    def make_history(self, rows):
        path = self.dir / "History"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE urls (url TEXT, title TEXT, visit_count INT)")
        con.executemany("INSERT INTO urls VALUES (?,?,?)", rows)
        con.commit()
        con.close()
        original = shortcuts.BROWSERS
        shortcuts.BROWSERS = {"Test": path}
        self.addCleanup(setattr, shortcuts, "BROWSERS", original)

    def test_visits_are_summed_per_domain(self):
        self.make_history([
            ("https://chess.com/play", "Play", 100),
            ("https://www.chess.com/puzzles", "Puzzles", 50),
            ("https://other.example/", "Other", 10),
        ])
        sites = {s["label"]: s for s in shortcuts.browser_sites(10)}
        self.assertEqual(sites["chess.com"]["visits"], 150)

    def test_the_busiest_domain_comes_first(self):
        self.make_history([("https://quiet.example/", "q", 5),
                           ("https://busy.example/", "b", 500)])
        self.assertEqual(shortcuts.browser_sites(10)[0]["label"], "busy.example")

    def test_local_files_are_excluded(self):
        # A downloaded PDF read 4,000 times is not a site you visit.
        self.make_history([("file:///C:/Users/x/notes.pdf", "Notes", 4000),
                           ("https://real.example/", "Real", 3)])
        labels = [s["label"] for s in shortcuts.browser_sites(10)]
        self.assertEqual(labels, ["real.example"])

    def test_search_engines_and_ad_hosts_are_excluded(self):
        self.make_history([("https://www.google.com/search?q=x", "s", 900),
                           ("https://googleadservices.com/x", "a", 800),
                           ("https://real.example/", "Real", 3)])
        self.assertEqual([s["label"] for s in shortcuts.browser_sites(10)], ["real.example"])

    def test_the_limit_is_respected(self):
        self.make_history([(f"https://s{i}.example/", "t", 100 - i) for i in range(20)])
        self.assertEqual(len(shortcuts.browser_sites(5)), 5)

    def test_a_missing_history_file_is_not_fatal(self):
        original = shortcuts.BROWSERS
        shortcuts.BROWSERS = {"Nope": self.dir / "does-not-exist"}
        self.addCleanup(setattr, shortcuts, "BROWSERS", original)
        self.assertEqual(shortcuts.browser_sites(5), [])

    def test_a_corrupt_history_file_is_skipped_rather_than_raising(self):
        path = self.dir / "History"
        path.write_bytes(b"this is not a database")
        original = shortcuts.BROWSERS
        shortcuts.BROWSERS = {"Broken": path}
        self.addCleanup(setattr, shortcuts, "BROWSERS", original)
        self.assertEqual(shortcuts.browser_sites(5), [])


class AppNaming(unittest.TestCase):
    def test_a_packaged_app_id_becomes_a_readable_name(self):
        self.assertEqual(
            shortcuts._pretty("Microsoft.ScreenSketch_8wekyb3d8bbwe!App"), "Snipping Tool")

    def test_camel_case_is_spaced_when_there_is_no_override(self):
        self.assertEqual(
            shortcuts._pretty("SomeVendor.MediaPlayer_abc123!App"), "Media Player")

    def test_an_exe_path_becomes_its_stem(self):
        # Title-cased on the way out: an all-lowercase filename looks like a
        # path fragment on screen, not the name of an app.
        self.assertEqual(
            shortcuts._pretty(r"{GUID}\WindowsPowerShell\v1.0\powershell.exe"), "Powershell")

    def test_a_packaged_app_launches_through_the_apps_folder(self):
        # There is no .exe to point at for a Store app.
        self.assertEqual(
            shortcuts._launch_target("CiderCollective.Cider_a6qxe093bx5xj!App"),
            "shell:AppsFolder\\CiderCollective.Cider_a6qxe093bx5xj!App")

    def test_a_folder_guid_is_expanded_to_a_real_path(self):
        # Left unexpanded this is "System32\x.exe", relative to nothing.
        target = shortcuts._launch_target(
            r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe")
        self.assertTrue(Path(target).is_absolute(), target)
        self.assertTrue(target.lower().endswith("powershell.exe"))


class Pinning(StoreCase):
    def test_a_pinned_site_gets_a_scheme_and_a_label(self):
        row = shortcuts.add("site", "", "chess.com")
        self.assertEqual(row["target"], "https://chess.com")
        self.assertEqual(row["label"], "chess.com")

    def test_an_explicit_label_is_kept(self):
        self.assertEqual(shortcuts.add("site", "Chess", "chess.com")["label"], "Chess")

    def test_pins_appear_before_detected_items(self):
        shortcuts.add("site", "Mine", "example.com")
        self.assertEqual(shortcuts.listing()["sites"][0]["label"], "Mine")

    def test_a_bad_kind_is_refused(self):
        with self.assertRaises(ValueError):
            shortcuts.add("spaceship", "x", "y")

    def test_an_empty_target_is_refused(self):
        with self.assertRaises(ValueError):
            shortcuts.add("site", "x", "   ")

    def test_a_pin_can_be_removed(self):
        row = shortcuts.add("app", "Thing", r"C:\thing.exe")
        self.assertTrue(shortcuts.remove(row["id"]))
        self.assertEqual(shortcuts.listing()["apps"], [])

    def test_pins_survive_each_other(self):
        keep = shortcuts.add("site", "Keep", "keep.example")
        drop = shortcuts.add("site", "Drop", "drop.example")
        shortcuts.remove(drop["id"])
        labels = [s["label"] for s in shortcuts.listing()["sites"]]
        self.assertIn("Keep", labels)
        self.assertNotIn("Drop", labels)
        self.assertTrue(keep["id"])


class Hiding(StoreCase):
    def test_removing_a_detected_item_hides_it(self):
        item = shortcuts.item_id("site", "chess.com")
        self.assertTrue(shortcuts.remove(item))
        self.assertIn(item, shortcuts._load()["hidden"])

    def test_hiding_the_same_thing_twice_reports_no_change(self):
        item = shortcuts.item_id("site", "chess.com")
        self.assertTrue(shortcuts.remove(item))
        self.assertFalse(shortcuts.remove(item))

    def test_a_hidden_site_stays_hidden_across_rebuilds(self):
        # The id is derived from the domain, so a changing visit count can't
        # bring a dismissed row back.
        item = shortcuts.item_id("site", "chess.com")
        shortcuts.remove(item)
        self.assertEqual(shortcuts.item_id("site", "chess.com"), item)

    def test_restore_brings_them_all_back(self):
        shortcuts.remove(shortcuts.item_id("site", "a.example"))
        shortcuts.remove(shortcuts.item_id("site", "b.example"))
        self.assertEqual(shortcuts.restore_hidden(), 2)
        self.assertEqual(shortcuts._load()["hidden"], [])

    def test_pinning_something_you_hid_un_hides_it(self):
        item = shortcuts.item_id("site", "chess.com")
        shortcuts.remove(item)
        shortcuts.add("site", "", "chess.com")
        self.assertNotIn(item, shortcuts._load()["hidden"])

    def test_a_corrupt_store_reads_as_empty(self):
        shortcuts.STORE.write_text("{not json", encoding="utf-8")
        self.assertEqual(shortcuts._load(), {"pinned": [], "hidden": []})


class LaunchSafety(StoreCase):
    """resolve() is what stops the open endpoint being 'run anything'."""

    def test_a_known_item_resolves(self):
        row = shortcuts.add("site", "Mine", "example.com")
        self.assertEqual(shortcuts.resolve(row["id"])["target"], "https://example.com")

    def test_an_unknown_id_resolves_to_nothing(self):
        self.assertIsNone(shortcuts.resolve("pin:does-not-exist"))

    def test_a_raw_path_is_not_a_valid_id(self):
        # The whole point: a path in the request body can never be launched,
        # only an id already present in the panel.
        self.assertIsNone(shortcuts.resolve(r"C:\Windows\System32\calc.exe"))
        self.assertIsNone(shortcuts.resolve(""))

    def test_a_removed_item_stops_resolving(self):
        row = shortcuts.add("app", "Thing", r"C:\thing.exe")
        shortcuts.remove(row["id"])
        self.assertIsNone(shortcuts.resolve(row["id"]))


if __name__ == "__main__":
    unittest.main()
