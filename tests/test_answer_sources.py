import json
import unittest

from app.tools.web.answer_sources import RetrievedSources


def search(*records):
    return json.dumps({"kind": "search_results", "results": list(records)})


def page(url="https://example.org/guide", title="Guide", **extra):
    return json.dumps({"kind": "page_excerpt", "final_url": url, "requested_url": url,
                       "title": title, "excerpt": "Evidence", **extra})


class AnswerSourcesTests(unittest.TestCase):
    def test_only_actual_web_results_produce_a_footer(self):
        sources = RetrievedSources()
        sources.observe("shell__execute", search({"url": "https://example.org/", "title": "Ignore"}), succeeded=True)
        sources.observe(["malformed tool name"], page(), succeeded=False)
        self.assertEqual(sources.append_to("Answer"), "Answer")

    def test_search_and_open_are_deduplicated_and_opened_pages_are_listed_first(self):
        sources = RetrievedSources()
        sources.observe("web__search", search(
            {"url": "https://example.org/other", "title": "Other result"},
            {"url": "https://EXAMPLE.org:443/guide", "title": "Discovery title"},
        ), succeeded=True)
        sources.observe("web__open", page(), succeeded=True)
        sources.observe("web__find", page(kind="page_find", matches=[{"line": 170}]), succeeded=True)
        footer = sources.append_to("Answer")
        self.assertEqual(footer.count("https://example.org/guide"), 1)
        self.assertIn("[Guide](<https://example.org/guide>) — Page opened", footer)
        self.assertIn("[Other result](<https://example.org/other>) — Search result", footer)
        self.assertLess(footer.index("[Guide]"), footer.index("[Other result]"))

    def test_redirects_replace_the_discovery_url_with_the_opened_destination(self):
        sources = RetrievedSources()
        sources.observe("web__search", search({"url": "https://example.org/old", "title": "Old"}), succeeded=True)
        sources.observe("web__open", page("https://example.org/new", requested_url="https://example.org/old"), succeeded=True)
        footer = sources.append_to("Answer")
        self.assertIn("https://example.org/new", footer)
        self.assertNotIn("https://example.org/old", footer)

    def test_empty_failed_denied_and_malformed_results_never_invent_source_links(self):
        for content, succeeded in ((search(), True), (page(), False), ("not json", True),
                                   ("[]", True), ('{"kind":"search_results","results":null}', True)):
            with self.subTest(content=content, succeeded=succeeded):
                sources = RetrievedSources()
                sources.observe("web__search", content, succeeded=succeeded)
                footer = sources.append_to("Answer")
                self.assertIn("No live sources were retrieved for this turn.", footer)
                self.assertNotIn("](", footer)

    def test_unrelated_payload_kinds_navigation_and_snippet_links_are_not_sources(self):
        sources = RetrievedSources()
        sources.observe("web__search", page(), succeeded=True)
        sources.observe("web__open", search({"url": "https://example.org/unexecuted"}), succeeded=True)
        sources.observe("web__search", search({"url": "https://example.org/result", "title": "Result",
                                              "snippet": "https://example.org/unvisited"}), succeeded=True)
        sources.observe("web__open", page(links=[{"url": "https://example.org/navigation"}]), succeeded=True)
        footer = sources.append_to("Answer")
        self.assertIn("https://example.org/result", footer)
        for unwanted in ("unexecuted", "unvisited", "navigation"):
            self.assertNotIn(unwanted, footer)

    def test_source_titles_and_destinations_cannot_inject_markdown_or_unsafe_links(self):
        sources = RetrievedSources()
        sources.observe("web__search", search(
            {"url": "javascript:alert(1)", "title": "Script"},
            {"url": "https://user:secret@example.org/", "title": "Credentials"},
            {"url": "https://example.org/\nattack", "title": "Newline"},
            {"url": "https://example.org/a>(x)", "title": '<script>hidden</script>![bad](javascript:evil)\n*label*'},
        ), succeeded=True)
        footer = sources.append_to("Answer")
        self.assertEqual(footer.count("](<https://"), 1)
        self.assertIn("https://example.org/a%3E%28x%29", footer)
        self.assertNotIn("hidden", footer)
        self.assertNotIn("<script>", footer)
        self.assertIn(r"!\[bad\]\(javascript:evil\)", footer)
        self.assertIn(r"\*label\*", footer)

    def test_collection_and_footer_are_bounded_and_prioritize_opened_pages(self):
        sources = RetrievedSources()
        sources.observe("web__search", search(*[
            {"url": f"https://example.org/{index}", "title": f"Result {index}"} for index in range(30)
        ]), succeeded=True)
        sources.observe("web__open", page("https://example.org/opened"), succeeded=True)
        footer = sources.append_to("Answer")
        self.assertEqual(footer.count("](<"), 8)
        self.assertIn("https://example.org/opened", footer)
        self.assertIn("Additional sources omitted", footer)
        self.assertLessEqual(len(footer) - len("Answer\n\n"), 6000)
        for index in range(8):
            sources.observe("web__open", page(f"https://example.org/{index}/" + "x" * 1800), succeeded=True)
        self.assertLessEqual(len(sources.append_to("Answer")) - len("Answer\n\n"), 6000)

    def test_failed_lookup_does_not_erase_previously_retrieved_sources(self):
        sources = RetrievedSources()
        sources.observe("web__open", page(), succeeded=True)
        sources.observe("web__search", "Unavailable", succeeded=False)
        footer = sources.append_to("Answer")
        self.assertIn("https://example.org/guide", footer)
        self.assertNotIn("No live sources", footer)
