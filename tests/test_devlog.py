"""The dev log: GitHub commits, Notion edits, and the state that stops it
repeating itself.

No network: `devlog._get_json` is replaced with canned API payloads, so the
parsing, labelling and failure handling are all exercised directly.
"""

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

import devlog


class DevlogCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

        original = devlog.STATE
        devlog.STATE = self.dir / "devlog_state.json"
        self.addCleanup(setattr, devlog, "STATE", original)

        original_get = devlog._get_json
        self.addCleanup(setattr, devlog, "_get_json", original_get)
        self.requests = []

    def serve(self, handler):
        """handler(url, headers, body) -> (payload, problem)"""
        def stub(url, headers, body=None, timeout=devlog.TIMEOUT):
            self.requests.append({"url": url, "headers": headers, "body": body})
            return handler(url, headers, body)
        devlog._get_json = stub

    @staticmethod
    def commit(message, login, when="2026-07-27T05:00:00Z", url="https://gh/c/1"):
        return {"html_url": url,
                "author": {"login": login},
                "commit": {"message": message,
                           "author": {"name": login.title(), "date": when}}}

    @staticmethod
    def cfg(**github):
        base = {"enabled": True, "token": "tok", "username": "me",
                "repos": ["me/app"]}
        base.update(github)
        return {"devlog": {"enabled": True, "github": base,
                           "notion": {"enabled": False}}}


class State(DevlogCase):
    def test_the_first_run_looks_back_a_window_not_forever(self):
        since = devlog.last_checked("github:me/app", lookback_hours=24)
        moment = dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        gap = dt.datetime.now(dt.timezone.utc) - moment
        self.assertAlmostEqual(gap.total_seconds() / 3600, 24, delta=1)

    def test_a_recorded_time_is_used_next_run(self):
        devlog.mark_checked("github:me/app",
                            dt.datetime(2026, 7, 27, 6, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(devlog.last_checked("github:me/app"), "2026-07-27T06:30:00Z")

    def test_naive_datetimes_are_treated_as_utc(self):
        devlog.mark_checked("x", dt.datetime(2026, 7, 27, 6, 30))
        self.assertTrue(devlog.last_checked("x").endswith("Z"))

    def test_sources_are_tracked_separately(self):
        devlog.mark_checked("github:a/b", dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        devlog.mark_checked("notion:work", dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(devlog.last_checked("github:a/b"), "2026-01-01T00:00:00Z")
        self.assertEqual(devlog.last_checked("notion:work"), "2026-06-01T00:00:00Z")

    def test_a_corrupt_state_file_reads_as_empty(self):
        devlog.STATE.write_text("{not json", encoding="utf-8")
        self.assertEqual(devlog._load_state()["last_checked"], {})

    def test_reset_forgets_every_timestamp(self):
        devlog.mark_checked("a")
        devlog.mark_checked("b")
        self.assertEqual(devlog.reset_state(), 2)
        self.assertEqual(devlog._load_state()["last_checked"], {})


class Tokens(DevlogCase):
    def test_a_literal_token_is_used(self):
        self.assertEqual(devlog._token({"token": "abc123"}), "abc123")

    def test_env_prefix_reads_the_environment(self):
        os.environ["MB_TEST_TOKEN"] = "from-env"
        self.addCleanup(os.environ.pop, "MB_TEST_TOKEN", None)
        self.assertEqual(devlog._token({"token": "env:MB_TEST_TOKEN"}), "from-env")

    def test_a_missing_env_var_is_empty_not_the_literal(self):
        self.assertEqual(devlog._token({"token": "env:MB_NOT_SET_ANYWHERE"}), "")

    def test_an_empty_config_falls_back_to_named_variables(self):
        os.environ["MB_FALLBACK"] = "fallback-token"
        self.addCleanup(os.environ.pop, "MB_FALLBACK", None)
        self.assertEqual(devlog._token({}, "MB_FALLBACK"), "fallback-token")

    def test_no_token_anywhere_is_empty(self):
        self.assertEqual(devlog._token({}, "MB_DEFINITELY_NOT_SET"), "")


class GitHub(DevlogCase):
    def test_commits_are_read_and_attributed(self):
        self.serve(lambda url, h, b: ([
            self.commit("Add the calendar importer", "me"),
            self.commit("Fix the timezone bug", "collab"),
        ], ""))
        out = devlog.github_activity(self.cfg())
        self.assertEqual(len(out["entries"]), 2)
        mine = [e for e in out["entries"] if e["mine"]]
        theirs = [e for e in out["entries"] if not e["mine"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["summary"], "Add the calendar importer")
        self.assertEqual(theirs[0]["who"], "collab")

    def test_only_the_first_line_of_a_commit_message_is_kept(self):
        body = "Short subject\n\nA long body nobody wants read aloud."
        self.serve(lambda url, h, b: ([self.commit(body, "me")], ""))
        out = devlog.github_activity(self.cfg())
        self.assertEqual(out["entries"][0]["summary"], "Short subject")

    def test_the_since_parameter_carries_the_stored_time(self):
        devlog.mark_checked("github:me/app",
                            dt.datetime(2026, 7, 26, 6, 0, tzinfo=dt.timezone.utc))
        self.serve(lambda url, h, b: ([], ""))
        devlog.github_activity(self.cfg())
        self.assertIn("since=2026-07-26T06%3A00%3A00Z", self.requests[0]["url"])

    def test_the_token_is_sent_as_a_bearer_header(self):
        self.serve(lambda url, h, b: ([], ""))
        devlog.github_activity(self.cfg())
        self.assertEqual(self.requests[0]["headers"]["Authorization"], "Bearer tok")

    def test_no_new_commits_is_a_quiet_success(self):
        self.serve(lambda url, h, b: ([], ""))
        out = devlog.github_activity(self.cfg())
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["checked"], ["github:me/app"])

    def test_several_repos_are_all_read(self):
        self.serve(lambda url, h, b: ([self.commit("A change", "me")], ""))
        out = devlog.github_activity(self.cfg(repos=["me/app", "collab/other"]))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual({e["repo"] for e in out["entries"]}, {"me/app", "collab/other"})

    def test_a_rejected_token_is_reported_and_does_not_raise(self):
        self.serve(lambda url, h, b: (None, "token was rejected - check it hasn't expired"))
        out = devlog.github_activity(self.cfg())
        self.assertEqual(out["entries"], [])
        self.assertIn("rejected", out["problems"][0])

    def test_a_failed_repo_is_not_marked_as_checked(self):
        # Otherwise the next run would skip whatever this one failed to read.
        self.serve(lambda url, h, b: (None, "rate limit reached"))
        out = devlog.github_activity(self.cfg())
        self.assertEqual(out["checked"], [])

    def test_one_dead_repo_does_not_sink_the_others(self):
        def selective(url, h, b):
            if "broken" in url:
                return None, "not found, or not shared with the integration"
            return [self.commit("Still works", "me")], ""
        self.serve(selective)
        out = devlog.github_activity(self.cfg(repos=["me/broken", "me/app"]))
        self.assertEqual(len(out["entries"]), 1)
        self.assertEqual(len(out["problems"]), 1)
        self.assertEqual(out["checked"], ["github:me/app"])

    def test_no_token_says_so_rather_than_calling_the_api(self):
        out = devlog.github_activity(self.cfg(token=""))
        self.assertIn("no token", out["problems"][0])
        self.assertEqual(self.requests, [])

    def test_no_repos_configured_is_silent(self):
        out = devlog.github_activity(self.cfg(repos=[]))
        self.assertEqual((out["entries"], out["problems"]), ([], []))

    def test_a_response_that_is_not_a_list_is_reported(self):
        self.serve(lambda url, h, b: ({"message": "Bad credentials"}, ""))
        out = devlog.github_activity(self.cfg())
        self.assertIn("unexpected response", out["problems"][0])


class Notion(DevlogCase):
    @staticmethod
    def page(title, edited, page_id="p1"):
        return {"id": page_id, "last_edited_time": edited,
                "url": f"https://notion.so/{page_id}",
                "last_edited_by": {"id": "u1"},
                "properties": {"Name": {"type": "title",
                                        "title": [{"plain_text": title}]}}}

    @staticmethod
    def cfg(**notion):
        base = {"enabled": True, "token": "tok", "workspace": "work",
                "collaborator": "Sam"}
        base.update(notion)
        return {"devlog": {"enabled": True, "github": {"enabled": False},
                           "notion": base}}

    def test_edited_pages_are_returned_with_their_titles(self):
        self.serve(lambda url, h, b:
                   ({"results": [self.page("Project plan", "2026-07-27T05:00:00Z")]}, "")
                   if "search" in url else ({"results": []}, ""))
        out = devlog.notion_activity(self.cfg())
        self.assertEqual(out["entries"][0]["title"], "Project plan")
        self.assertEqual(out["entries"][0]["who"], "Sam")

    def test_pages_older_than_the_cutoff_stop_the_walk(self):
        # Notion's search cannot filter on last_edited_time, so results come
        # back sorted and the cutoff is applied here.
        devlog.mark_checked("notion:work",
                            dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc))
        self.serve(lambda url, h, b:
                   ({"results": [self.page("New", "2026-07-27T05:00:00Z", "new"),
                                 self.page("Old", "2026-07-26T05:00:00Z", "old"),
                                 self.page("Older", "2026-07-25T05:00:00Z", "older")]}, "")
                   if "search" in url else ({"results": []}, ""))
        out = devlog.notion_activity(self.cfg())
        self.assertEqual([e["title"] for e in out["entries"]], ["New"])

    def test_the_sort_is_by_last_edited_descending(self):
        self.serve(lambda url, h, b: ({"results": []}, ""))
        devlog.notion_activity(self.cfg())
        sort = self.requests[0]["body"]["sort"]
        self.assertEqual(sort["timestamp"], "last_edited_time")
        self.assertEqual(sort["direction"], "descending")

    def test_the_notion_version_header_is_sent(self):
        self.serve(lambda url, h, b: ({"results": []}, ""))
        devlog.notion_activity(self.cfg())
        self.assertEqual(self.requests[0]["headers"]["Notion-Version"],
                         devlog.NOTION_VERSION)

    def test_block_text_is_collected_as_raw_material(self):
        def handler(url, h, b):
            if "search" in url:
                return {"results": [self.page("Doc", "2026-07-27T05:00:00Z")]}, ""
            return {"results": [
                {"type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "Decided on the schema."}]}},
                {"type": "heading_2",
                 "heading_2": {"rich_text": [{"plain_text": "Next steps"}]}},
            ]}, ""
        self.serve(handler)
        out = devlog.notion_activity(self.cfg())
        self.assertIn("Decided on the schema.", out["entries"][0]["summary"])
        self.assertIn("Next steps", out["entries"][0]["summary"])

    def test_a_title_on_the_object_itself_is_found(self):
        db = {"id": "d1", "last_edited_time": "2026-07-27T05:00:00Z",
              "title": [{"plain_text": "Tasks database"}], "properties": {}}
        self.serve(lambda url, h, b: ({"results": [db]}, "") if "search" in url
                   else ({"results": []}, ""))
        out = devlog.notion_activity(self.cfg())
        self.assertEqual(out["entries"][0]["title"], "Tasks database")

    def test_page_ids_narrow_the_result(self):
        self.serve(lambda url, h, b:
                   ({"results": [self.page("Wanted", "2026-07-27T05:00:00Z", "keep"),
                                 self.page("Ignored", "2026-07-27T04:00:00Z", "skip")]}, "")
                   if "search" in url else ({"results": []}, ""))
        out = devlog.notion_activity(self.cfg(page_ids=["keep"]))
        self.assertEqual([e["title"] for e in out["entries"]], ["Wanted"])

    def test_nothing_shared_says_so(self):
        self.serve(lambda url, h, b: ({"results": []}, ""))
        out = devlog.notion_activity(self.cfg())
        self.assertIn("nothing shared", out["problems"][0])

    def test_an_api_error_is_reported_not_raised(self):
        self.serve(lambda url, h, b: (None, "token was rejected - check it hasn't expired"))
        out = devlog.notion_activity(self.cfg())
        self.assertEqual(out["entries"], [])
        self.assertIn("Notion:", out["problems"][0])
        self.assertEqual(out["checked"], [])

    def test_no_token_says_so_rather_than_calling_the_api(self):
        out = devlog.notion_activity(self.cfg(token=""))
        self.assertIn("no token", out["problems"][0])
        self.assertEqual(self.requests, [])


class Collect(DevlogCase):
    def setUp(self):
        super().setUp()
        original = devlog.summarise
        devlog.summarise = lambda activity, cfg: ""
        self.addCleanup(setattr, devlog, "summarise", original)

    def test_state_advances_after_a_good_run(self):
        self.serve(lambda url, h, b: ([self.commit("Did a thing", "me")], ""))
        devlog.collect(self.cfg(), dt.datetime(2026, 7, 27, 6, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(devlog.last_checked("github:me/app"), "2026-07-27T06:30:00Z")

    def test_advance_false_leaves_the_timestamps_alone(self):
        # A preview shouldn't consume the morning's commits.
        self.serve(lambda url, h, b: ([self.commit("Did a thing", "me")], ""))
        before = devlog.last_checked("github:me/app")
        devlog.collect(self.cfg(), advance=False)
        self.assertEqual(devlog.last_checked("github:me/app"), before)

    def test_a_second_run_the_same_day_keeps_the_earlier_entries(self):
        now = dt.datetime(2026, 7, 27, 6, 30, tzinfo=dt.timezone.utc)
        self.serve(lambda url, h, b: ([self.commit("Morning work", "me", url="u1")], ""))
        devlog.collect(self.cfg(), now)
        self.serve(lambda url, h, b: ([], ""))
        second = devlog.collect(self.cfg(), now)
        self.assertEqual(len(second["entries"]), 1,
                         "the morning's entries were dropped on re-run")

    def test_new_entries_accumulate_onto_the_day(self):
        now = dt.datetime(2026, 7, 27, 6, 30, tzinfo=dt.timezone.utc)
        self.serve(lambda url, h, b: ([self.commit("First", "me", url="u1")], ""))
        devlog.collect(self.cfg(), now)
        self.serve(lambda url, h, b: ([self.commit("Second", "me", url="u2")], ""))
        out = devlog.collect(self.cfg(), now)
        self.assertEqual(sorted(e["summary"] for e in out["entries"]), ["First", "Second"])

    def test_the_same_commit_twice_is_not_duplicated(self):
        now = dt.datetime(2026, 7, 27, 6, 30, tzinfo=dt.timezone.utc)
        self.serve(lambda url, h, b: ([self.commit("Only once", "me", url="u1")], ""))
        devlog.collect(self.cfg(), now)
        out = devlog.collect(self.cfg(), now)
        self.assertEqual(len(out["entries"]), 1)

    def test_yesterdays_digest_does_not_leak_into_today(self):
        self.serve(lambda url, h, b: ([self.commit("Yesterday", "me", url="u1")], ""))
        devlog.collect(self.cfg(), dt.datetime(2026, 7, 26, 6, 30, tzinfo=dt.timezone.utc))
        self.serve(lambda url, h, b: ([], ""))
        out = devlog.collect(self.cfg(), dt.datetime(2026, 7, 27, 6, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(out["entries"], [])


class PlainSummary(unittest.TestCase):
    def test_your_own_commits_are_addressed_as_you(self):
        activity = {"entries": [
            {"source": "github", "repo": "me/app", "mine": True, "summary": "x"},
            {"source": "github", "repo": "me/app", "mine": True, "summary": "y"}]}
        self.assertIn("You pushed 2 changes in me/app.", devlog.plain_summary(activity))

    def test_one_change_is_singular(self):
        activity = {"entries": [
            {"source": "github", "repo": "me/app", "mine": True, "summary": "x"}]}
        self.assertIn("1 change in", devlog.plain_summary(activity))

    def test_a_collaborator_is_named(self):
        activity = {"entries": [
            {"source": "notion", "who": "Sam", "mine": False, "title": "Project doc"}]}
        self.assertIn("Sam: updated Project doc.", devlog.plain_summary(activity))

    def test_nothing_gives_an_empty_string(self):
        self.assertEqual(devlog.plain_summary({"entries": []}), "")


class Summarise(DevlogCase):
    def test_the_model_is_not_called_when_llm_is_off(self):
        activity = {"entries": [{"source": "github", "repo": "a/b",
                                 "mine": True, "summary": "x"}]}
        self.assertEqual(devlog.summarise(activity, {"llm": {"enabled": False}}), "")

    def test_no_entries_means_no_call(self):
        self.assertEqual(devlog.summarise({"entries": []},
                                          {"llm": {"enabled": True, "personality": "p"}}), "")

    def test_the_prompt_separates_the_two_people(self):
        prompt = devlog._prompt(
            "Be warm.",
            [{"source": "github", "repo": "me/app", "summary": "Added imports"}],
            {"Sam": [{"source": "notion", "title": "Plan", "summary": "Wrote the schema"}]})
        self.assertIn("WHAT I DID:", prompt)
        self.assertIn("WHAT SAM DID:", prompt)
        self.assertIn("Added imports", prompt)
        self.assertIn("Wrote the schema", prompt)

    def test_the_prompt_forbids_invention_and_sign_offs(self):
        prompt = devlog._prompt("p", [], {})
        self.assertIn("Only describe what is listed above", prompt)
        self.assertIn("no sign-off", prompt)


if __name__ == "__main__":
    unittest.main()
