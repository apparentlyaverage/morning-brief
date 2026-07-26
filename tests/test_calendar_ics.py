"""The .ics reader - hand-rolled, so the fiddly parts need pinning down."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import calendar_ics as ics


def make_ics(body: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + body + "\nEND:VCALENDAR\n"


class Unfolding(unittest.TestCase):
    def test_space_continuation_rejoins(self):
        lines = ics._unfold("SUMMARY:A very long\n  title")
        self.assertEqual(lines, ["SUMMARY:A very long title"])

    def test_tab_continuation_rejoins(self):
        # RFC 5545 allows either a space or a tab as the folding character.
        self.assertEqual(ics._unfold("SUMMARY:one\n\ttwo"), ["SUMMARY:onetwo"])

    def test_crlf_and_cr_both_split(self):
        self.assertEqual(ics._unfold("A\r\nB\rC"), ["A", "B", "C"])

    def test_leading_continuation_with_no_predecessor_is_not_a_crash(self):
        self.assertEqual(ics._unfold(" orphan"), [" orphan"])


class Unescaping(unittest.TestCase):
    def test_escapes(self):
        self.assertEqual(ics._unescape(r"Line\none"), "Line one")
        self.assertEqual(ics._unescape(r"Smith\, John"), "Smith, John")
        self.assertEqual(ics._unescape(r"a\;b"), "a;b")
        self.assertEqual(ics._unescape(r"C:\\path"), "C:\\path")

    def test_capital_n_newline(self):
        self.assertEqual(ics._unescape(r"Line\Ntwo"), "Line two")


class DateParsing(unittest.TestCase):
    def test_date_only_is_all_day(self):
        stamp, all_day = ics._parse_dt("20260727")
        self.assertEqual(stamp, dt.datetime(2026, 7, 27))
        self.assertTrue(all_day)

    def test_local_datetime_is_left_alone(self):
        stamp, all_day = ics._parse_dt("20260727T074500")
        self.assertEqual(stamp, dt.datetime(2026, 7, 27, 7, 45))
        self.assertFalse(all_day)

    def test_utc_is_converted_to_local_and_left_naive(self):
        stamp, _ = ics._parse_dt("20260727T054500Z")
        # Derive the expectation the same way a caller would, so the test holds
        # in any timezone rather than assuming the author's.
        expected = (dt.datetime(2026, 7, 27, 5, 45, tzinfo=dt.timezone.utc)
                    .astimezone().replace(tzinfo=None))
        self.assertEqual(stamp, expected)
        self.assertIsNone(stamp.tzinfo)

    def test_junk_returns_none(self):
        self.assertEqual(ics._parse_dt("not-a-date"), (None, False))


class LineSplitting(unittest.TestCase):
    def test_params_are_parsed_and_upper_cased(self):
        name, params, value = ics._split_line(
            "DTSTART;TZID=Africa/Johannesburg:20260727T074500")
        self.assertEqual(name, "DTSTART")
        self.assertEqual(params, {"TZID": "Africa/Johannesburg"})
        self.assertEqual(value, "20260727T074500")

    def test_value_may_contain_colons(self):
        _, _, value = ics._split_line("URL:https://example.com/a:b")
        self.assertEqual(value, "https://example.com/a:b")

    def test_line_without_colon_is_ignored(self):
        self.assertEqual(ics._split_line("GARBAGE"), ("", {}, ""))


class Recurrence(unittest.TestCase):
    def setUp(self):
        self.window_start = dt.date(2026, 7, 27)   # a Monday
        self.window_end = dt.date(2026, 8, 21)

    def expand(self, start, rule, exdates=()):
        return ics._expand(start, rule, set(exdates),
                           self.window_start, self.window_end)

    def test_weekly_byday_emits_every_named_weekday(self):
        hits = self.expand(dt.datetime(2026, 7, 27, 8, 0),
                           "FREQ=WEEKLY;BYDAY=MO,WE,FR")
        self.assertTrue(all(h.weekday() in (0, 2, 4) for h in hits))
        # Four full Mon/Wed/Fri weeks between 27 Jul and 21 Aug inclusive.
        self.assertEqual(len(hits), 12)
        self.assertTrue(all(h.hour == 8 and h.minute == 0 for h in hits))

    def test_exdate_removes_that_occurrence(self):
        rule = "FREQ=WEEKLY;BYDAY=MO"
        base = self.expand(dt.datetime(2026, 7, 27, 8, 0), rule)
        skipped = self.expand(dt.datetime(2026, 7, 27, 8, 0), rule,
                              exdates=[dt.date(2026, 8, 3)])
        self.assertEqual(len(skipped), len(base) - 1)
        self.assertNotIn(dt.date(2026, 8, 3), [h.date() for h in skipped])

    def test_count_caps_the_series(self):
        hits = self.expand(dt.datetime(2026, 7, 27, 8, 0), "FREQ=DAILY;COUNT=3")
        self.assertEqual([h.date() for h in hits],
                         [dt.date(2026, 7, 27), dt.date(2026, 7, 28),
                          dt.date(2026, 7, 29)])

    def test_until_stops_the_series(self):
        hits = self.expand(dt.datetime(2026, 7, 27, 8, 0),
                           "FREQ=DAILY;UNTIL=20260730")
        self.assertEqual(max(h.date() for h in hits), dt.date(2026, 7, 30))

    def test_interval_is_respected(self):
        hits = self.expand(dt.datetime(2026, 7, 27, 8, 0),
                           "FREQ=DAILY;INTERVAL=7;COUNT=3")
        self.assertEqual([h.date() for h in hits],
                         [dt.date(2026, 7, 27), dt.date(2026, 8, 3),
                          dt.date(2026, 8, 10)])

    def test_monthly_from_the_31st_clamps_to_short_months(self):
        # Stepping 31 Jan by a month must not raise "day is out of range".
        self.window_start = dt.date(2026, 1, 31)
        self.window_end = dt.date(2026, 5, 1)
        hits = self.expand(dt.datetime(2026, 1, 31, 9, 0), "FREQ=MONTHLY")
        self.assertEqual([h.date() for h in hits][:3],
                         [dt.date(2026, 1, 31), dt.date(2026, 2, 28),
                          dt.date(2026, 3, 28)])

    def test_yearly_from_29_february_survives_a_common_year(self):
        self.window_start = dt.date(2028, 2, 29)
        self.window_end = dt.date(2030, 3, 2)
        hits = self.expand(dt.datetime(2028, 2, 29, 9, 0), "FREQ=YEARLY")
        self.assertEqual([h.date() for h in hits],
                         [dt.date(2028, 2, 29), dt.date(2029, 2, 28),
                          dt.date(2030, 2, 28)])

    def test_unknown_frequency_yields_nothing(self):
        self.assertEqual(self.expand(dt.datetime(2026, 7, 27), "FREQ=HOURLY"), [])

    def test_unbounded_rule_terminates_and_stays_in_the_window(self):
        # No COUNT, no UNTIL: the loop must stop on its own.
        hits = self.expand(dt.datetime(2026, 7, 27, 8, 0), "FREQ=DAILY")
        self.assertLessEqual(len(hits), ics.MAX_OCCURRENCES)
        self.assertTrue(all(self.window_start <= h.date() <= self.window_end
                            for h in hits))

    def test_occurrences_before_the_window_are_dropped(self):
        # A series that started months ago should only report what's ahead.
        hits = self.expand(dt.datetime(2026, 1, 5, 8, 0), "FREQ=WEEKLY;BYDAY=MO")
        self.assertTrue(all(h.date() >= self.window_start for h in hits))


class ParseEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cal.ics"

    def write(self, body):
        self.path.write_text(make_ics(body), encoding="utf-8")
        return self.path

    def test_single_event_inside_the_window(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Dentist\nLOCATION:High Street\n"
                   "DTSTART:20260730T140000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["events"][0],
                         {"summary": "Dentist", "location": "High Street",
                          "date": "2026-07-30", "time": "14:00", "all_day": False})

    def test_event_outside_the_window_is_excluded(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Far off\nDTSTART:20271201T090000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual(out["count"], 0)

    def test_all_day_event_has_no_time(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Public holiday\nDTSTART:20260810\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertTrue(out["events"][0]["all_day"])
        self.assertEqual(out["events"][0]["time"], "")

    def test_missing_summary_becomes_untitled(self):
        self.write("BEGIN:VEVENT\nDTSTART:20260730T140000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual(out["events"][0]["summary"], "(untitled)")

    def test_event_without_dtstart_is_skipped_not_fatal(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Broken\nEND:VEVENT\n"
                   "BEGIN:VEVENT\nSUMMARY:Fine\nDTSTART:20260730T140000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual([e["summary"] for e in out["events"]], ["Fine"])

    def test_output_is_sorted_by_date_then_time(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Later\nDTSTART:20260730T160000\nEND:VEVENT\n"
                   "BEGIN:VEVENT\nSUMMARY:Earlier\nDTSTART:20260730T090000\nEND:VEVENT\n"
                   "BEGIN:VEVENT\nSUMMARY:Yesterday\nDTSTART:20260729T235900\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual([e["summary"] for e in out["events"]],
                         ["Yesterday", "Earlier", "Later"])

    def test_all_day_sorts_before_timed_events_on_the_same_day(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Timed\nDTSTART:20260730T090000\nEND:VEVENT\n"
                   "BEGIN:VEVENT\nSUMMARY:AllDay\nDTSTART:20260730\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual([e["summary"] for e in out["events"]], ["AllDay", "Timed"])

    def test_tzid_raises_a_warning_because_zones_are_not_converted(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Lecture\n"
                   "DTSTART;TZID=America/New_York:20260730T140000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertTrue(any("TZID" in w for w in out["warnings"]))

    def test_recurrence_id_raises_a_warning(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Moved one\nDTSTART:20260730T140000\n"
                   "RECURRENCE-ID:20260730T140000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertTrue(any("per-occurrence" in w for w in out["warnings"]))

    def test_complex_rule_is_flagged_by_name(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Board meeting\nDTSTART:20260730T140000\n"
                   "RRULE:FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=60, today=dt.date(2026, 7, 27))
        self.assertTrue(any("Board meeting" in w for w in out["warnings"]))

    def test_folded_summary_is_rejoined_end_to_end(self):
        self.write("BEGIN:VEVENT\nSUMMARY:A rather long lecture\n  title that folded\n"
                   "DTSTART:20260730T140000\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual(out["events"][0]["summary"],
                         "A rather long lecture title that folded")

    def test_utf8_bom_file_is_readable(self):
        # Outlook exports are commonly UTF-8 with a BOM.
        self.path.write_text(
            make_ics("BEGIN:VEVENT\nSUMMARY:Café\nDTSTART:20260730T140000\nEND:VEVENT"),
            encoding="utf-8-sig")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        self.assertEqual(out["events"][0]["summary"], "Café")

    def test_weekly_lecture_series_expands_end_to_end(self):
        self.write("BEGIN:VEVENT\nSUMMARY:Stats 2\nDTSTART:20260727T074500\n"
                   "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20260807\n"
                   "EXDATE:20260729T074500\nEND:VEVENT")
        out = ics.parse(self.path, days_ahead=30, today=dt.date(2026, 7, 27))
        dates = [e["date"] for e in out["events"]]
        self.assertNotIn("2026-07-29", dates)          # excluded
        self.assertEqual(dates[0], "2026-07-27")
        self.assertEqual(max(dates), "2026-08-07")     # UNTIL honoured
        self.assertTrue(all(e["time"] == "07:45" for e in out["events"]))


if __name__ == "__main__":
    unittest.main()
