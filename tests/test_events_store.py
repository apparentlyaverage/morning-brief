"""Calendar events: manual entries plus imported ones that can be hidden.

Every test redirects events_store's two path constants at a temp directory,
so the real calendar.json / events.json are never touched.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import events_store as store


class StoreCase(unittest.TestCase):
    """Base class that isolates the on-disk state."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

        self.calendar = self.dir / "calendar.json"
        self.events = self.dir / "events.json"

        for name, value in (("CALENDAR", self.calendar), ("EVENTS", self.events)):
            original = getattr(store, name)
            setattr(store, name, value)
            self.addCleanup(setattr, store, name, original)

    def write_imported(self, *events):
        self.calendar.write_text(json.dumps({"events": list(events)}), encoding="utf-8")

    @staticmethod
    def imported(date, summary, time=""):
        return {"date": date, "time": time, "summary": summary,
                "location": "", "all_day": not time}


class LoadStore(StoreCase):
    def test_missing_file_gives_an_empty_store(self):
        self.assertEqual(store.load_store(), {"manual": [], "hidden": []})

    def test_corrupt_file_does_not_raise(self):
        self.events.write_text("{not json", encoding="utf-8")
        self.assertEqual(store.load_store(), {"manual": [], "hidden": []})

    def test_a_json_list_is_not_mistaken_for_a_store(self):
        self.events.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(store.load_store(), {"manual": [], "hidden": []})


class EventKey(StoreCase):
    def test_key_is_stable_across_calls(self):
        ev = self.imported("2026-07-30", "Dentist", "14:00")
        self.assertEqual(store.event_key(ev), store.event_key(dict(ev)))

    def test_key_ignores_fields_that_a_reimport_may_reword(self):
        a = {"date": "2026-07-30", "time": "14:00", "summary": "Dentist",
             "location": "High Street"}
        b = {"date": "2026-07-30", "time": "14:00", "summary": "Dentist",
             "location": "Somewhere else"}
        self.assertEqual(store.event_key(a), store.event_key(b))

    def test_different_events_get_different_keys(self):
        self.assertNotEqual(
            store.event_key(self.imported("2026-07-30", "Dentist", "14:00")),
            store.event_key(self.imported("2026-07-30", "Dentist", "15:00")))

    def test_missing_fields_do_not_raise(self):
        self.assertTrue(store.event_key({}))


class AddEvent(StoreCase):
    def test_valid_event_is_stored_and_returned(self):
        ev = store.add_event("2026-07-30", "Coffee", time="09:30",
                             location="Cafe", note="with Sam")
        self.assertEqual(ev["summary"], "Coffee")
        self.assertEqual(ev["date"], "2026-07-30")
        self.assertFalse(ev["all_day"])
        self.assertTrue(ev["id"].startswith("m-"))
        self.assertEqual(store.load_store()["manual"], [ev])

    def test_event_without_a_time_is_all_day(self):
        self.assertTrue(store.add_event("2026-07-30", "Public holiday")["all_day"])

    def test_blank_summary_is_refused(self):
        with self.assertRaises(ValueError):
            store.add_event("2026-07-30", "   ")

    def test_bad_date_is_refused(self):
        for bad in ("30-07-2026", "2026-13-01", "", None):
            with self.subTest(date=bad), self.assertRaises(ValueError):
                store.add_event(bad, "Thing")

    def test_bad_time_is_refused(self):
        for bad in ("9am", "25:00", "14:60"):
            with self.subTest(time=bad), self.assertRaises(ValueError):
                store.add_event("2026-07-30", "Thing", time=bad)

    def test_surrounding_whitespace_is_trimmed(self):
        ev = store.add_event("2026-07-30", "  Coffee  ", location="  Cafe  ")
        self.assertEqual(ev["summary"], "Coffee")
        self.assertEqual(ev["location"], "Cafe")

    def test_ids_are_unique(self):
        ids = {store.add_event("2026-07-30", f"Thing {i}")["id"] for i in range(20)}
        self.assertEqual(len(ids), 20)

    def test_a_failed_add_leaves_the_store_untouched(self):
        store.add_event("2026-07-30", "Good one")
        with self.assertRaises(ValueError):
            store.add_event("nonsense", "Bad one")
        self.assertEqual(len(store.load_store()["manual"]), 1)


class AllEvents(StoreCase):
    def test_imported_and_manual_are_merged_and_sorted(self):
        self.write_imported(self.imported("2026-07-31", "Imported late", "16:00"),
                            self.imported("2026-07-29", "Imported early", "08:00"))
        store.add_event("2026-07-30", "Manual middle", time="12:00")
        self.assertEqual([e["summary"] for e in store.all_events()],
                         ["Imported early", "Manual middle", "Imported late"])

    def test_source_is_tagged(self):
        self.write_imported(self.imported("2026-07-29", "From ics", "08:00"))
        store.add_event("2026-07-30", "By hand")
        sources = {e["summary"]: e["source"] for e in store.all_events()}
        self.assertEqual(sources, {"From ics": "ics", "By hand": "manual"})

    def test_imported_events_gain_a_stable_id(self):
        ev = self.imported("2026-07-29", "From ics", "08:00")
        self.write_imported(ev)
        self.assertEqual(store.all_events()[0]["id"], store.event_key(ev))

    def test_missing_calendar_file_is_not_fatal(self):
        store.add_event("2026-07-30", "By hand")
        self.assertEqual(len(store.all_events()), 1)

    def test_all_day_sorts_before_timed_on_the_same_date(self):
        self.write_imported(self.imported("2026-07-30", "Timed", "09:00"),
                            self.imported("2026-07-30", "All day"))
        self.assertEqual([e["summary"] for e in store.all_events()],
                         ["All day", "Timed"])


class Upcoming(StoreCase):
    def setUp(self):
        super().setUp()
        self.write_imported(
            self.imported("2026-07-25", "Yesterday", "09:00"),
            self.imported("2026-07-26", "Today", "09:00"),
            self.imported("2026-08-01", "In six days", "09:00"),
            self.imported("2026-09-01", "Far off", "09:00"))

    def test_window_includes_today_and_excludes_the_past(self):
        got = store.upcoming(days_ahead=7, today=dt.date(2026, 7, 26))
        self.assertEqual([e["summary"] for e in got], ["Today", "In six days"])

    def test_horizon_is_inclusive(self):
        got = store.upcoming(days_ahead=6, today=dt.date(2026, 7, 26))
        self.assertIn("In six days", [e["summary"] for e in got])
        got = store.upcoming(days_ahead=5, today=dt.date(2026, 7, 26))
        self.assertNotIn("In six days", [e["summary"] for e in got])

    def test_unparseable_dates_are_skipped_not_fatal(self):
        self.write_imported({"date": "not-a-date", "summary": "Broken", "time": ""},
                            self.imported("2026-07-26", "Fine", "09:00"))
        got = store.upcoming(days_ahead=7, today=dt.date(2026, 7, 26))
        self.assertEqual([e["summary"] for e in got], ["Fine"])


class Deletion(StoreCase):
    def test_deleting_a_manual_event_removes_it(self):
        ev = store.add_event("2026-07-30", "Coffee")
        self.assertTrue(store.delete_event(ev["id"]))
        self.assertEqual(store.all_events(), [])
        self.assertEqual(store.load_store()["hidden"], [])

    def test_deleting_an_imported_event_hides_it(self):
        ev = self.imported("2026-07-30", "Lecture", "08:00")
        self.write_imported(ev)
        self.assertTrue(store.delete_event(store.event_key(ev)))
        self.assertEqual(store.all_events(), [])
        self.assertEqual(store.load_store()["hidden"], [store.event_key(ev)])

    def test_a_hidden_event_stays_hidden_after_reimport(self):
        # The whole reason the key is a content hash rather than a row number.
        ev = self.imported("2026-07-30", "Lecture", "08:00")
        self.write_imported(ev)
        store.delete_event(store.event_key(ev))

        self.write_imported(self.imported("2026-07-29", "New one", "08:00"), ev)
        self.assertEqual([e["summary"] for e in store.all_events()], ["New one"])

    def test_deleting_the_same_imported_event_twice_reports_no_change(self):
        ev = self.imported("2026-07-30", "Lecture", "08:00")
        self.write_imported(ev)
        key = store.event_key(ev)
        self.assertTrue(store.delete_event(key))
        self.assertFalse(store.delete_event(key))
        self.assertEqual(store.load_store()["hidden"], [key])

    def test_deleting_one_manual_event_leaves_the_others(self):
        keep = store.add_event("2026-07-30", "Keep me")
        drop = store.add_event("2026-07-31", "Drop me")
        store.delete_event(drop["id"])
        self.assertEqual([e["summary"] for e in store.all_events()], ["Keep me"])
        self.assertEqual(store.load_store()["manual"][0]["id"], keep["id"])


class RestoreHidden(StoreCase):
    def test_restores_every_hidden_event_and_reports_the_count(self):
        events = [self.imported("2026-07-30", "One", "08:00"),
                  self.imported("2026-07-31", "Two", "09:00")]
        self.write_imported(*events)
        for ev in events:
            store.delete_event(store.event_key(ev))
        self.assertEqual(store.all_events(), [])

        self.assertEqual(store.restore_hidden(), 2)
        self.assertEqual([e["summary"] for e in store.all_events()], ["One", "Two"])

    def test_restoring_nothing_returns_zero(self):
        self.assertEqual(store.restore_hidden(), 0)

    def test_manual_events_are_untouched_by_a_restore(self):
        store.add_event("2026-07-30", "By hand")
        store.restore_hidden()
        self.assertEqual([e["summary"] for e in store.all_events()], ["By hand"])


if __name__ == "__main__":
    unittest.main()
