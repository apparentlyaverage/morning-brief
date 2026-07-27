"""Briefing logic that runs without a network: the academic calendar, the
speech-shaping helpers, and feed parsing.

The academic-calendar tests matter most. Getting them wrong means the briefing
cheerfully announces lectures on a public holiday, which is exactly the sort
of mistake you don't notice until you've walked to campus.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import briefing

ROOT = Path(__file__).resolve().parent.parent


def calendar(terms=(), no_lectures=()):
    return {"_calendar": {"terms": list(terms), "no_lectures": list(no_lectures)}}


TERMS = [
    {"name": "Term 1", "start": "2026-02-09", "end": "2026-03-20"},
    {"name": "Term 2", "start": "2026-03-30", "end": "2026-06-12"},
]


class Span(unittest.TestCase):
    def test_valid_block(self):
        self.assertEqual(briefing._span({"start": "2026-02-09", "end": "2026-03-20"}),
                         (dt.date(2026, 2, 9), dt.date(2026, 3, 20)))

    def test_malformed_blocks_return_none_rather_than_raising(self):
        for bad in ({}, {"start": "2026-02-09"}, {"start": "nope", "end": "nope"},
                    {"start": None, "end": None}):
            with self.subTest(block=bad):
                self.assertIsNone(briefing._span(bad))


class AcademicStatus(unittest.TestCase):
    def status(self, today, terms=TERMS, no_lectures=()):
        return briefing.academic_status(calendar(terms, no_lectures), today)

    def test_no_calendar_at_all_assumes_lectures_run(self):
        # Someone who never uploads term dates should still get their timetable.
        running, note = briefing.academic_status({}, dt.date(2026, 2, 20))
        self.assertTrue(running)
        self.assertEqual(note, "")

    def test_a_day_inside_a_term_runs_lectures(self):
        running, note = self.status(dt.date(2026, 2, 20))
        self.assertTrue(running)
        self.assertEqual(note, "Term 1")

    def test_term_boundaries_are_inclusive(self):
        self.assertTrue(self.status(dt.date(2026, 2, 9))[0])    # first day
        self.assertTrue(self.status(dt.date(2026, 3, 20))[0])   # last day

    def test_a_holiday_inside_a_term_stops_lectures_and_names_itself(self):
        holiday = [{"name": "Freedom Day", "start": "2026-04-27", "end": "2026-04-27"}]
        running, note = self.status(dt.date(2026, 4, 27), no_lectures=holiday)
        self.assertFalse(running)
        self.assertEqual(note, "Freedom Day")

    def test_a_multi_day_block_inside_a_term_stops_lectures(self):
        swot = [{"name": "Swot week", "start": "2026-06-01", "end": "2026-06-05"}]
        for day in (dt.date(2026, 6, 1), dt.date(2026, 6, 3), dt.date(2026, 6, 5)):
            with self.subTest(day=day):
                self.assertEqual(self.status(day, no_lectures=swot), (False, "Swot week"))
        # The day after is a normal teaching day again.
        self.assertTrue(self.status(dt.date(2026, 6, 8), no_lectures=swot)[0])

    def test_a_no_lectures_block_wins_over_the_term_it_sits_in(self):
        # Order matters: the block is checked before the term.
        block = [{"name": "Exams", "start": "2026-02-09", "end": "2026-02-20"}]
        self.assertEqual(self.status(dt.date(2026, 2, 10), no_lectures=block),
                         (False, "Exams"))

    def test_an_unnamed_block_still_explains_itself(self):
        block = [{"start": "2026-02-10", "end": "2026-02-10"}]
        self.assertEqual(self.status(dt.date(2026, 2, 10), no_lectures=block)[1],
                         "no lectures")

    def test_between_terms_counts_down_to_the_next_one(self):
        running, note = self.status(dt.date(2026, 3, 25))
        self.assertFalse(running)
        self.assertIn("vacation", note)
        self.assertIn("in 5 days", note)
        self.assertIn("30 Mar", note)

    def test_the_day_before_term_says_tomorrow_not_in_one_days(self):
        self.assertIn("tomorrow", self.status(dt.date(2026, 3, 29))[1])

    def test_before_the_first_term_also_counts_down(self):
        note = self.status(dt.date(2026, 1, 5))[1]
        self.assertIn("next term starts", note)
        self.assertIn("09 Feb", note)

    def test_past_the_last_term_says_the_calendar_ran_out(self):
        # Saying "vacation" here would be quietly wrong for months.
        running, note = self.status(dt.date(2027, 1, 15))
        self.assertFalse(running)
        self.assertNotIn("vacation", note)
        self.assertIn("12 Jun 2026", note)
        self.assertIn("timetable.json", note)

    def test_malformed_terms_do_not_crash_the_briefing(self):
        bad = [{"name": "Broken", "start": "not-a-date", "end": "also-not"}]
        running, _ = self.status(dt.date(2026, 5, 1), terms=bad)
        self.assertFalse(running)

    def test_a_malformed_term_alongside_good_ones_is_simply_ignored(self):
        terms = TERMS + [{"name": "Broken", "start": "x", "end": "y"}]
        self.assertEqual(self.status(dt.date(2026, 2, 20), terms=terms),
                         (True, "Term 1"))


class RealCalendar(unittest.TestCase):
    """A real academic calendar, exercised end to end.

    Rhodes University 2026, kept in tests/fixtures rather than shipped as the
    default timetable: one university's term dates are wrong for everyone
    else, and the briefing would confidently announce "Term 2" to someone who
    has never been to Makhanda. The dates stay here because gating lectures on
    real holidays is the part worth testing, and invented dates wouldn't catch
    the observed-Monday case below.
    """

    @classmethod
    def setUpClass(cls):
        path = ROOT / "tests" / "fixtures" / "rhodes-2026.json"
        if not path.exists():
            raise unittest.SkipTest("the calendar fixture is not present")
        cls.timetable = json.loads(path.read_text(encoding="utf-8-sig"))

    def status(self, day):
        return briefing.academic_status(self.timetable, day)

    def test_the_calendar_actually_has_terms_and_holidays(self):
        cal = self.timetable["_calendar"]
        self.assertTrue(cal["terms"])
        self.assertTrue(cal["no_lectures"])

    def test_womens_day_observed_on_the_monday_has_no_lectures(self):
        # 9 August 2026 is a Sunday, so the holiday moves to Monday the 10th -
        # a normal teaching Monday that would otherwise announce a full day.
        self.assertEqual(dt.date(2026, 8, 10).weekday(), 0)
        running, note = self.status(dt.date(2026, 8, 10))
        self.assertFalse(running, f"10 Aug 2026 should be a holiday, got: {note}")

    def test_heritage_day_has_no_lectures(self):
        # 24 September 2026 is a Thursday inside Term 3.
        self.assertEqual(dt.date(2026, 9, 24).weekday(), 3)
        running, note = self.status(dt.date(2026, 9, 24))
        self.assertFalse(running, f"24 Sep 2026 should be a holiday, got: {note}")

    def test_every_listed_holiday_stops_lectures(self):
        for block in self.timetable["_calendar"]["no_lectures"]:
            span = briefing._span(block)
            self.assertIsNotNone(span, f"unparseable block: {block}")
            with self.subTest(block=block.get("name")):
                self.assertFalse(self.status(span[0])[0])
                self.assertFalse(self.status(span[1])[0])

    def test_an_ordinary_midterm_weekday_does_run_lectures(self):
        # A sanity check in the other direction, so the suite would notice a
        # calendar that accidentally blocks out every day of the year.
        running, _ = self.status(dt.date(2026, 2, 17))   # Tuesday, Term 1
        self.assertTrue(running)

    def test_no_holiday_block_is_backwards(self):
        for block in self.timetable["_calendar"]["no_lectures"]:
            start, end = briefing._span(block)
            with self.subTest(block=block.get("name")):
                self.assertLessEqual(start, end)


class ShippedExample(unittest.TestCase):
    """timetable.example.json is what a stranger starts from.

    It must stay institution-neutral. Shipping Rhodes' dates as the default
    means anyone else is told about terms and holidays that have nothing to do
    with them, which is worse than being told nothing.
    """

    @classmethod
    def setUpClass(cls):
        path = ROOT / "timetable.example.json"
        if not path.exists():
            raise unittest.SkipTest("timetable.example.json is not present")
        cls.raw = path.read_text(encoding="utf-8-sig")
        cls.timetable = json.loads(cls.raw)

    def test_it_is_valid_json_with_every_weekday(self):
        for day in ("monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday"):
            with self.subTest(day=day):
                self.assertIsInstance(self.timetable[day], list)

    def test_it_ships_no_term_dates(self):
        cal = self.timetable["_calendar"]
        self.assertEqual(cal["terms"], [])
        self.assertEqual(cal["no_lectures"], [])

    def test_it_names_no_particular_institution(self):
        for word in ("Rhodes", "Makhanda", "Grahamstown"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.raw)

    def test_with_no_calendar_the_briefing_assumes_classes_run(self):
        # The documented consequence of leaving it empty.
        running, note = briefing.academic_status(self.timetable, dt.date(2026, 8, 10))
        self.assertTrue(running)
        self.assertEqual(note, "")

    def test_the_worked_example_lecture_is_shaped_like_a_real_one(self):
        lecture = self.timetable["monday"][0]
        for field in ("start", "end", "name"):
            with self.subTest(field=field):
                self.assertTrue(lecture[field])
        dt.datetime.strptime(lecture["start"], "%H:%M")
        dt.datetime.strptime(lecture["end"], "%H:%M")

    def test_it_still_loads_through_the_real_loader(self):
        original = briefing.TIMETABLE_PATH
        briefing.TIMETABLE_PATH = ROOT / "timetable.example.json"
        self.addCleanup(setattr, briefing, "TIMETABLE_PATH", original)
        self.assertNotIn("_error", briefing.load_timetable())


class FileLoading(unittest.TestCase):
    """A fresh install must survive files written by Windows tooling.

    setup.ps1 writes config.json with Set-Content -Encoding utf8, and
    PowerShell 5.1 puts a BOM on that. Reading it as plain utf-8 raised
    "Unexpected UTF-8 BOM" and the app died on its first ever run. Notepad
    saves the same way, so this applies to anything hand-edited too.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def redirect(self, name, path):
        original = getattr(briefing, name)
        setattr(briefing, name, path)
        self.addCleanup(setattr, briefing, name, original)

    def test_config_with_a_bom_loads(self):
        path = self.dir / "config.json"
        path.write_text(json.dumps({"name": "Sam", "city": "Makhanda"}),
                        encoding="utf-8-sig")
        self.redirect("CONFIG_PATH", path)
        self.assertEqual(briefing.load_config()["city"], "Makhanda")

    def test_config_without_a_bom_still_loads(self):
        path = self.dir / "config.json"
        path.write_text(json.dumps({"name": "Sam", "city": "Makhanda"}), encoding="utf-8")
        self.redirect("CONFIG_PATH", path)
        self.assertEqual(briefing.load_config()["city"], "Makhanda")

    def test_timetable_with_a_bom_loads(self):
        path = self.dir / "timetable.json"
        path.write_text(json.dumps({"monday": [{"name": "Stats", "start": "08:00"}]}),
                        encoding="utf-8-sig")
        self.redirect("TIMETABLE_PATH", path)
        self.assertEqual(len(briefing.load_timetable()["monday"]), 1)

    def test_a_missing_timetable_is_not_fatal(self):
        self.redirect("TIMETABLE_PATH", self.dir / "nope.json")
        self.assertEqual(briefing.load_timetable(), {})

    def test_a_broken_timetable_reports_itself_rather_than_crashing(self):
        path = self.dir / "timetable.json"
        path.write_text("{not json", encoding="utf-8")
        self.redirect("TIMETABLE_PATH", path)
        self.assertIn("_error", briefing.load_timetable())

    def test_non_ascii_survives_either_encoding(self):
        for enc in ("utf-8", "utf-8-sig"):
            with self.subTest(encoding=enc):
                path = self.dir / f"config-{enc}.json"
                path.write_text(json.dumps({"city": "Gqeberha", "name": "Zoë"},
                                           ensure_ascii=False), encoding=enc)
                self.redirect("CONFIG_PATH", path)
                self.assertEqual(briefing.load_config()["name"], "Zoë")


class SpokenSelection(unittest.TestCase):
    """Which stories get read out, and the dashboard's marker for them.

    One selection feeds three consumers - the spoken script, the model's
    prompt, and the "read out" badge. They must agree, because a badge that
    marks a story the presenter never mentioned is worse than no badge.
    """

    @staticmethod
    def feed(name, *titles):
        return {"name": name, "error": None,
                "items": [{"title": t, "summary": "", "link": f"https://x/{i}"}
                          for i, t in enumerate(titles)]}

    @staticmethod
    def cfg(limit=5):
        return {"speech": {"headlines": limit}}

    def test_the_limit_is_respected(self):
        news = [self.feed("A", *[f"Story number {i} about something" for i in range(20)])]
        self.assertEqual(len(briefing.choose_spoken(news, self.cfg(3))), 3)

    def test_it_round_robins_so_one_feed_cannot_take_every_slot(self):
        news = [self.feed("Chatty", "Chatty one", "Chatty two", "Chatty three"),
                self.feed("Quiet", "Quiet one")]
        chosen = briefing.choose_spoken(news, self.cfg(3))
        self.assertEqual([c["source"] for c in chosen], ["Chatty", "Quiet", "Chatty"])

    def test_duplicates_across_sources_are_taken_once(self):
        shared = "Ramaphosa accepts the resignation with immediate effect"
        news = [self.feed("A", shared), self.feed("B", shared), self.feed("C", "Something else entirely")]
        chosen = briefing.choose_spoken(news, self.cfg(5))
        self.assertEqual(len(chosen), 2)

    def test_asking_for_more_than_exists_returns_what_there_is(self):
        news = [self.feed("A", "Only story here")]
        self.assertEqual(len(briefing.choose_spoken(news, self.cfg(10))), 1)

    def test_no_news_selects_nothing(self):
        self.assertEqual(briefing.choose_spoken([], self.cfg()), [])
        self.assertEqual(briefing.choose_spoken([self.feed("Dead")], self.cfg()), [])

    def test_the_kicker_is_stripped_for_speech(self):
        news = [self.feed("DM", "DIGITAL DEFICIT: SA mining falls behind on technology")]
        chosen = briefing.choose_spoken(news, self.cfg())
        self.assertEqual(chosen[0]["title"], "SA mining falls behind on technology")

    def test_spoken_key_ignores_the_kicker_and_the_masthead(self):
        self.assertEqual(
            briefing.spoken_key("News24 | BIG NEWS: A thing occurred today"),
            briefing.spoken_key("A thing occurred today"))


class SpokenMarker(SpokenSelection):
    """build_data's `spoken` flag."""

    def build(self, news, limit=5):
        cfg = {"speech": {"headlines": limit}, "name": "Sam", "city": "Makhanda"}
        return briefing.build_data(cfg, dt.datetime(2026, 7, 27, 6, 30),
                                   None, {}, news, {})

    def test_exactly_the_chosen_stories_are_marked(self):
        news = [self.feed("A", "First story about a thing", "Second story about another"),
                self.feed("B", "Third story from elsewhere", "Fourth story from elsewhere")]
        data = self.build(news, limit=2)
        marked = [h for h in data["headlines"] if h["spoken"]]
        self.assertEqual(len(marked), 2)
        self.assertEqual({h["source"] for h in marked}, {"A", "B"})

    def test_every_headline_carries_the_field(self):
        news = [self.feed("A", *[f"Story {i} with enough words here" for i in range(8)])]
        data = self.build(news, limit=3)
        self.assertTrue(all("spoken" in h for h in data["headlines"]))
        self.assertEqual(sum(h["spoken"] for h in data["headlines"]), 3)

    def test_a_story_whose_card_title_keeps_a_kicker_is_still_matched(self):
        """The card shows "BODY AND SOUL OP-ED: Go inward..." while the
        presenter says "Go inward...". Matching the displayed titles would
        silently fail to mark it."""
        news = [self.feed("DM", "BODY AND SOUL OP-ED: Go inward and let it spill outward")]
        data = self.build(news, limit=1)
        card = data["headlines"][0]
        self.assertTrue(card["title"].startswith("BODY AND SOUL OP-ED:"))
        self.assertTrue(card["spoken"], "kicker headline was not marked")

    def test_nothing_is_marked_when_speech_takes_no_headlines(self):
        news = [self.feed("A", "A story that will not be read aloud")]
        data = self.build(news, limit=0)
        self.assertFalse(any(h["spoken"] for h in data["headlines"]))

    def test_unmarked_stories_are_still_listed(self):
        news = [self.feed("A", *[f"Story {i} with enough words here" for i in range(6)])]
        data = self.build(news, limit=2)
        self.assertEqual(len(data["headlines"]), 6)

    def test_the_marked_set_matches_what_the_model_is_given(self):
        # The prompt and the badge must describe the same stories.
        news = [self.feed("A", "Alpha story about something notable",
                               "Beta story about something else"),
                self.feed("B", "Gamma story from another source")]
        cfg = {"speech": {"headlines": 2}, "name": "", "city": ""}
        payload = briefing._llm_payload(cfg, dt.datetime(2026, 7, 27, 6, 30),
                                        None, {}, news, {})
        data = briefing.build_data(cfg, dt.datetime(2026, 7, 27, 6, 30),
                                   None, {}, news, {})
        for h in data["headlines"]:
            if h["spoken"]:
                with self.subTest(title=h["title"]):
                    self.assertIn(briefing.speakable_title(h["title"]), payload["headlines"])

    def test_the_spoken_script_names_the_marked_stories(self):
        news = [self.feed("A", "Alpha story about something notable"),
                self.feed("B", "Beta story from another source")]
        cfg = {"speech": {"headlines": 2}, "name": "", "city": ""}
        script = briefing.spoken_news(news, cfg)
        data = briefing.build_data(cfg, dt.datetime(2026, 7, 27, 6, 30), None, {}, news, {})
        for h in data["headlines"]:
            if h["spoken"]:
                with self.subTest(title=h["title"]):
                    self.assertIn(briefing.speakable_title(h["title"]), script)


class SayTime(unittest.TestCase):
    def test_on_the_hour_drops_the_minutes(self):
        self.assertEqual(briefing.say_time("08:00"), "8 AM")

    def test_past_the_hour_keeps_them(self):
        self.assertEqual(briefing.say_time("14:45"), "2:45 PM")

    def test_midnight_and_noon(self):
        self.assertEqual(briefing.say_time("00:00"), "12 AM")
        self.assertEqual(briefing.say_time("12:00"), "12 PM")

    def test_just_after_midnight_and_noon(self):
        self.assertEqual(briefing.say_time("00:05"), "12:05 AM")
        self.assertEqual(briefing.say_time("12:30"), "12:30 PM")

    def test_minutes_keep_a_leading_zero(self):
        self.assertEqual(briefing.say_time("09:05"), "9:05 AM")

    def test_garbage_passes_straight_through(self):
        for bad in ("", "lunchtime", None, "8"):
            with self.subTest(value=bad):
                self.assertEqual(briefing.say_time(bad), bad)


class Ordinal(unittest.TestCase):
    def test_the_common_suffixes(self):
        self.assertEqual([briefing.ordinal(n) for n in (1, 2, 3, 4)],
                         ["1st", "2nd", "3rd", "4th"])

    def test_the_teens_are_all_th(self):
        self.assertEqual([briefing.ordinal(n) for n in (11, 12, 13)],
                         ["11th", "12th", "13th"])

    def test_twenties_pick_the_suffix_up_again(self):
        self.assertEqual([briefing.ordinal(n) for n in (21, 22, 23, 24)],
                         ["21st", "22nd", "23rd", "24th"])

    def test_every_day_of_a_month_gets_a_suffix(self):
        for day in range(1, 32):
            with self.subTest(day=day):
                self.assertTrue(briefing.ordinal(day).endswith(
                    ("st", "nd", "rd", "th")))


class Headlines(unittest.TestCase):
    def test_a_masthead_prefix_is_dropped(self):
        self.assertEqual(briefing.tidy_title("News24 | Something happened"),
                         "Something happened")

    def test_a_headline_with_no_prefix_is_untouched(self):
        self.assertEqual(briefing.tidy_title("Something happened"),
                         "Something happened")

    def test_a_pipe_later_in_the_headline_is_not_a_masthead(self):
        title = "The long and winding story of the thing | and its sequel"
        self.assertEqual(briefing.tidy_title(title), title)

    def test_a_shouty_kicker_is_removed_for_speech_only(self):
        title = "DIPLOMACY DANCE: Ambassador recalled"
        self.assertEqual(briefing.speakable_title(title), "Ambassador recalled")
        # The on-screen card keeps it - it reads fine, it just doesn't speak well.
        self.assertEqual(briefing.tidy_title(title), title)

    def test_prefix_and_kicker_together(self):
        self.assertEqual(
            briefing.speakable_title("News24 | BIG NEWS: A thing occurred"),
            "A thing occurred")

    def test_a_normal_colon_is_not_treated_as_a_kicker(self):
        title = "Ramaphosa: the year ahead"
        self.assertEqual(briefing.speakable_title(title), title)


class CleanText(unittest.TestCase):
    def test_tags_are_stripped_and_entities_decoded(self):
        self.assertEqual(briefing.clean_text("<p>Tom &amp; Jerry</p>"), "Tom & Jerry")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(briefing.clean_text("a\n\n   b\t c"), "a b c")

    def test_none_and_empty_are_safe(self):
        self.assertEqual(briefing.clean_text(None), "")
        self.assertEqual(briefing.clean_text(""), "")


class Greeting(unittest.TestCase):
    def test_it_changes_through_the_day(self):
        cases = [(3, "You're up early"), (8, "Good morning"),
                 (14, "Good afternoon"), (20, "Good evening")]
        for hour, expected in cases:
            with self.subTest(hour=hour):
                now = dt.datetime(2026, 7, 26, hour, 0)
                self.assertEqual(briefing.greeting(now, ""), expected)

    def test_the_name_is_appended_when_known(self):
        now = dt.datetime(2026, 7, 26, 8, 0)
        self.assertEqual(briefing.greeting(now, "Uthando"), "Good morning, Uthando")

    def test_no_trailing_comma_without_a_name(self):
        now = dt.datetime(2026, 7, 26, 8, 0)
        self.assertEqual(briefing.greeting(now, ""), "Good morning")


class ParseFeed(unittest.TestCase):
    RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>First</title><description>One</description>
            <link>https://example.com/1</link></item>
      <item><title>Second</title><description>Two</description>
            <link>https://example.com/2</link></item>
    </channel></rss>"""

    ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <link rel="self" href="https://example.com/feed"/>
      <entry><title>Atom one</title><summary>Body</summary>
             <link rel="self" href="https://example.com/self"/>
             <link rel="alternate" href="https://example.com/article"/></entry>
    </feed>"""

    def test_rss_is_parsed(self):
        items = briefing.parse_feed(self.RSS, limit=10)
        self.assertEqual([i["title"] for i in items], ["First", "Second"])
        self.assertEqual(items[0]["link"], "https://example.com/1")
        self.assertEqual(items[0]["summary"], "One")

    def test_the_limit_is_respected(self):
        self.assertEqual(len(briefing.parse_feed(self.RSS, limit=1)), 1)

    def test_atom_is_parsed_and_the_self_link_is_not_the_article(self):
        items = briefing.parse_feed(self.ATOM, limit=10)
        self.assertEqual(items[0]["title"], "Atom one")
        self.assertEqual(items[0]["link"], "https://example.com/article")

    def test_items_without_a_title_are_skipped(self):
        xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
          <item><description>No title</description></item>
          <item><title>Has one</title></item>
        </channel></rss>"""
        self.assertEqual([i["title"] for i in briefing.parse_feed(xml, 10)],
                         ["Has one"])

    def test_an_empty_feed_yields_nothing(self):
        xml = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        self.assertEqual(briefing.parse_feed(xml, 10), [])


class LlmPrompt(unittest.TestCase):
    payload = {"date": "Sunday 26 July 2026", "name": "Uthando",
               "weather": "Mild.", "classes": "None today.",
               "upcoming": "Nothing.", "headlines": "A thing happened."}

    def prompt(self, max_words):
        return briefing._build_llm_prompt("Be warm.", self.payload, max_words)

    def test_a_word_cap_is_stated_when_one_is_set(self):
        text = self.prompt(200)
        self.assertIn("Hard cap: 200 words", text)

    def test_zero_means_no_cap_at_all(self):
        # A cap makes the model count instead of write, and you can hear it.
        text = self.prompt(0)
        self.assertNotIn("Hard cap", text)
        self.assertIn("there is no length limit", text)

    def test_none_also_means_no_cap(self):
        self.assertNotIn("Hard cap", self.prompt(None))

    def test_a_negative_cap_is_treated_as_no_cap(self):
        self.assertNotIn("Hard cap", self.prompt(-1))

    def test_the_payload_and_personality_reach_the_prompt(self):
        text = self.prompt(0)
        for value in ("Be warm.", "Uthando", "Mild.", "A thing happened."):
            with self.subTest(value=value):
                self.assertIn(value, text)

    def test_the_speech_rules_are_always_present(self):
        text = self.prompt(0)
        self.assertIn("no markdown", text)
        self.assertIn("Do not invent facts", text)


if __name__ == "__main__":
    unittest.main()
