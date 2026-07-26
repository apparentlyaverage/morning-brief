"""Article extraction.

No network: every test feeds HTML straight to `extract`. The fixtures are
modelled on what the real feeds actually serve - the awkward cases here were
all found by probing live pages, not invented.
"""

import unittest

import article


def page(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


def paras(count: int, words: int = 20, word: str = "word") -> str:
    """Distinct paragraphs.

    They have to differ from the very first characters: the extractor drops
    near-duplicates on an 80-character prefix, so filler that starts the same
    way every time collapses into a single paragraph and every assertion below
    would be testing an empty string.
    """
    return "".join(
        f"<p>Item {i}: " + " ".join([f"{word}{i}"] * words) + ".</p>"
        for i in range(count))


class JsonLd(unittest.TestCase):
    def test_article_body_is_preferred(self):
        body = " ".join(["sentence"] * 200)
        html = page(paras(3) + f'''
            <script type="application/ld+json">
            {{"@type": "NewsArticle", "headline": "H", "articleBody": "{body}"}}
            </script>''')
        method, text = article.extract(html)
        self.assertEqual(method, "json-ld")
        self.assertIn("sentence", text)

    def test_it_is_found_inside_a_graph(self):
        body = " ".join(["deep"] * 200)
        html = page(f'''
            <script type="application/ld+json">
            {{"@context": "x", "@graph": [{{"@type": "WebPage"}},
              {{"@type": "NewsArticle", "articleBody": "{body}"}}]}}
            </script>''')
        method, text = article.extract(html)
        self.assertEqual(method, "json-ld")
        self.assertIn("deep", text)

    def test_a_short_article_body_is_not_trusted(self):
        html = page(paras(12) + '''
            <script type="application/ld+json">
            {"@type": "NewsArticle", "articleBody": "Too short to be the body."}
            </script>''')
        method, _ = article.extract(html)
        self.assertNotEqual(method, "json-ld")

    def test_malformed_json_ld_is_skipped_not_fatal(self):
        html = page(paras(12) + '<script type="application/ld+json">{not json</script>')
        method, text = article.extract(html)
        self.assertIn(method, ("article-tag", "p-density"))
        self.assertTrue(text)


class ArticleTag(unittest.TestCase):
    def test_paragraphs_inside_article_are_used(self):
        html = page(f"<article>{paras(10)}</article>")
        method, text = article.extract(html)
        self.assertEqual(method, "article-tag")
        self.assertIn("Item 0:", text)

    def test_the_block_with_the_most_prose_wins_not_the_biggest_markup(self):
        """The Moneyweb bug: every <article> on the page was a reader comment,
        and the largest chunk of markup was the comment thread."""
        padding = "<div>" + ("<span>x</span>" * 800) + "</div>"
        html = page(
            f"<article>{padding}{paras(3, 14, 'chatter')}</article>"      # big, thin
            f"<article>{paras(12, 20, 'realstory')}</article>")           # small, dense
        method, text = article.extract(html)
        self.assertEqual(method, "article-tag")
        self.assertIn("realstory", text)
        self.assertNotIn("chatter", text)


class Comments(unittest.TestCase):
    def test_a_comment_section_is_cut_before_extraction(self):
        html = page(f"<article>{paras(10, 20, 'realstory')}</article>"
                    f'<div id="comments">{paras(20, 20, "readeropinion")}</div>')
        _, text = article.extract(html)
        self.assertIn("realstory", text)
        self.assertNotIn("readeropinion", text)

    def test_wordpress_comment_articles_are_cut(self):
        html = page(f"<article>{paras(10, 20, 'realstory')}</article>"
                    f'<article class="comment byuser depth-1">{paras(20, 20, "readeropinion")}</article>')
        _, text = article.extract(html)
        self.assertIn("realstory", text)
        self.assertNotIn("readeropinion", text)

    def test_a_comments_marker_in_early_page_furniture_does_not_truncate(self):
        # A "comments" link in the header must not cost us the whole article.
        html = page('<nav><a id="comments" href="#c">Comments</a></nav>'
                    + paras(12, 20, "realstory"))
        _, text = article.extract(html)
        self.assertIn("realstory", text)


class ParagraphFiltering(unittest.TestCase):
    def test_short_paragraphs_are_dropped(self):
        html = page("<p>Short caption.</p>" + paras(10))
        _, text = article.extract(html)
        self.assertNotIn("Short caption.", text)

    def test_an_absurdly_long_paragraph_is_dropped(self):
        """SABC served a single <p> of 3958 words - a markup accident that
        swamped the real text."""
        blob = " ".join(["junk"] * 3000)
        html = page(f"<p>{blob}</p>" + paras(10, 20, "realstory"))
        _, text = article.extract(html)
        self.assertNotIn("junk", text)
        self.assertIn("realstory", text)

    def test_boilerplate_lines_are_dropped(self):
        for lead in ("ALSO READ: something something something something more words here",
                     "Subscribe: to keep reading this article you need a subscription now",
                     "Reporting by: someone at some agency in some city on some date here"):
            with self.subTest(lead=lead):
                html = page(f"<p>{lead}</p>" + paras(10, 20, "realstory"))
                _, text = article.extract(html)
                self.assertNotIn(lead.split(":")[0], text)

    def test_repeated_paragraphs_appear_once(self):
        dupe = "<p>" + " ".join(["repeated"] * 20) + "</p>"
        html = page(dupe + dupe + dupe + paras(10, 20, "realstory"))
        _, text = article.extract(html)
        # Count paragraphs, not substrings - one paragraph of twenty identical
        # words contains the pair many times over.
        repeated = [p for p in text.split("\n\n") if p.startswith("repeated")]
        self.assertEqual(len(repeated), 1)

    def test_script_and_nav_text_never_reaches_the_output(self):
        html = page("<article>"
                    "<script>var secret = 'do not read me aloud at all please';</script>"
                    "<nav><p>" + " ".join(["navjunk"] * 20) + "</p></nav>"
                    + paras(10, 20, "realstory") + "</article>")
        _, text = article.extract(html)
        self.assertNotIn("secret", text)
        self.assertNotIn("navjunk", text)
        self.assertIn("realstory", text)


class Limits(unittest.TestCase):
    def test_a_very_long_article_is_trimmed(self):
        html = page(paras(300, 30))
        _, text = article.extract(html)
        # The trim stops once the budget is reached, so one paragraph of
        # overshoot is expected - but not hundreds.
        self.assertLessEqual(len(text.split()), article.MAX_BODY_WORDS + 40)

    def test_a_page_with_no_prose_returns_nothing(self):
        method, text = article.extract(page("<div>Not much here at all.</div>"))
        self.assertEqual(method, "none")
        self.assertEqual(text, "")

    def test_a_page_of_only_short_paragraphs_returns_nothing(self):
        # eNCA-style: the body isn't in <p> tags, so there is nothing to find.
        method, text = article.extract(page("<p>One.</p><p>Two.</p><p>Three.</p>"))
        self.assertEqual(method, "none")
        self.assertEqual(text, "")

    def test_entities_are_decoded(self):
        html = page("<article><p>" + "Tom &amp; Jerry &quot;quoted&quot; " * 8 + "</p>"
                    + paras(10) + "</article>")
        _, text = article.extract(html)
        self.assertIn("Tom & Jerry", text)
        self.assertNotIn("&amp;", text)


class Decoding(unittest.TestCase):
    def test_the_documents_own_meta_charset_beats_a_wrong_header(self):
        raw = '<meta charset="utf-8"><p>café</p>'.encode("utf-8")
        # Server wrongly claims latin-1; the <meta> is right.
        self.assertIn("café", article._decode(raw, "iso-8859-1"))

    def test_the_header_is_used_when_the_document_says_nothing(self):
        raw = "<p>café</p>".encode("iso-8859-1")
        self.assertIn("café", article._decode(raw, "iso-8859-1"))

    def test_an_unknown_charset_does_not_raise(self):
        raw = b"<p>plain ascii</p>"
        self.assertIn("plain ascii", article._decode(raw, "not-a-real-charset"))


class FetchGuards(unittest.TestCase):
    def test_a_non_http_url_is_refused_without_a_request(self):
        for bad in ("", "file:///C:/Windows/win.ini", "javascript:alert(1)", "ftp://x/y"):
            with self.subTest(url=bad):
                res = article.fetch_article(bad)
                self.assertFalse(res["ok"])


class Script(unittest.TestCase):
    def test_the_prompt_carries_the_story_and_the_rules(self):
        prompt = article.build_prompt("Be warm.", "A thing happened", "News24", "Body text.")
        for needed in ("Be warm.", "A thing happened", "News24", "Body text.",
                       "no markdown", "Do not", "No greeting"):
            with self.subTest(needed=needed):
                self.assertIn(needed, prompt)

    def test_the_prompt_forbids_a_signoff_because_something_follows(self):
        self.assertIn("No sign-off",
                      article.build_prompt("p", "t", "s", "b"))

    def test_the_plain_fallback_introduces_then_reads(self):
        out = article.plain_script("A thing happened", "News24", "The body.")
        self.assertTrue(out.startswith("From News24: A thing happened."))
        self.assertIn("The body.", out)

    def test_the_plain_fallback_copes_without_a_source(self):
        self.assertTrue(article.plain_script("A thing", "", "Body").startswith("A thing."))


if __name__ == "__main__":
    unittest.main()
