from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import Mock

from app.tools.web.contracts import PageLink, ReadDocument, WebReaderError
from app.tools.web.service import WebReaderService
from app.tools.web.sources import SourceCache


URL = "https://example.org/guide"


def document(markdown="Alpha\nZażółć gęślą\nStraße\nlast", **overrides):
    values = dict(requested_url=URL, final_url=URL, title="Guide",
                  retrieved_at="2026-10-26T12:00:00+00:00", markdown=markdown,
                  links=(PageLink("https://example.org/next", "Next"),))
    values.update(overrides)
    return ReadDocument(**values)


def record(source_id="search_one", url=URL):
    return dict(source_id=source_id, url=url, title="Guide", retrieved_at="now")


class WebSourcesTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.deleted = set()
        self.cache = SourceCache(clock=lambda: self.now, is_session_deleted=self.deleted.__contains__)
        self.backend = Mock()
        self.backend.read.return_value = document()
        self.service = WebReaderService(self.cache, self.backend, window_lines=2)

    def category(self, expected, function, *arguments):
        with self.assertRaises(WebReaderError) as raised:
            function(*arguments)
        self.assertEqual(raised.exception.category, expected)

    def test_search_references_are_conversation_owned_and_not_page_evidence(self):
        self.cache.retain_search("owner", [record()])
        self.category("forbidden_reference", self.service.open, "other", "search_one")
        self.category("not_opened", self.service.find, "owner", "search_one", "alpha")
        self.category("unknown_reference", self.service.open, "owner", "page_unknown")
        self.category("invalid_session", self.service.open, "", URL)
        self.backend.read.assert_not_called()

    def test_open_windows_and_find_share_one_immutable_snapshot(self):
        self.cache.retain_search("owner", [record()])
        first = json.loads(self.service.open("owner", "search_one"))
        snapshot = self.cache.get("owner", first["snapshot_id"])
        with self.assertRaises(FrozenInstanceError):
            snapshot.title = "changed"
        self.assertEqual(first["excerpt"], "0: Alpha\n1: Zażółć gęślą")
        self.assertEqual(first["next_line"], 2)
        self.assertTrue(first["truncated"])
        second = json.loads(self.service.open("owner", first["snapshot_id"], 2))
        self.assertEqual(second["excerpt"], "2: Straße\n3: last")
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertIsNone(second["next_line"])
        self.assertFalse(second["truncated"])
        result = json.loads(self.service.find("owner", "search_one", "STRASSE"))
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertEqual(result["matches"][0]["excerpt"], "Straße")
        self.assertEqual(json.loads(self.service.find("owner", "search_one", ".*"))["matches"], [])
        self.service.open("owner", URL)
        self.backend.read.assert_called_once_with(URL)

    def test_new_search_for_cached_url_binds_existing_snapshot(self):
        page = json.loads(self.service.open("owner", URL))
        self.cache.retain_search("owner", [record("search_two")])
        reused = json.loads(self.service.open("owner", "search_two"))
        self.assertEqual(reused["snapshot_id"], page["snapshot_id"])
        self.assertEqual(json.loads(self.service.find("owner", "search_two", "alpha"))["match_count"], 1)
        self.backend.read.assert_called_once()

    def test_direct_links_use_same_reader_and_other_conversations_refetch(self):
        self.service.open("owner", URL)
        self.service.open("other", URL)
        self.assertEqual(self.backend.read.call_count, 2)
        self.category("blocked_url", self.service.open, "owner", "file:///etc/passwd")
        self.category("blocked_url", self.service.open, "owner", "https://user:pass@example.org/")

    def test_expiry_lru_and_restart_have_explicit_reference_failures(self):
        cache = SourceCache(ttl_s=5, max_session_entries=2, max_total_entries=3, clock=lambda: self.now)
        cache.retain_search("owner", [record("search_a"), record("search_b")])
        cache.get("owner", "search_a")
        cache.retain_search("owner", [record("search_c")])
        self.category("expired_reference", cache.get, "owner", "search_b")
        self.category("forbidden_reference", cache.get, "other", "search_b")
        cache.retain_search("other", [record("search_d"), record("search_e")])
        self.assertLessEqual(len(cache._entries), 3)
        self.now = 6
        self.category("expired_reference", cache.get, "owner", "search_c")
        self.category("unknown_reference", SourceCache().get, "owner", "search_c")

    def test_entry_bytes_and_tombstones_are_bounded(self):
        cache = SourceCache(max_session_bytes=1600, max_total_bytes=2400,
                            max_session_entries=3, max_total_entries=4)
        for index in range(50):
            cache.retain_search(str(index % 2), [record(f"search_{index}")])
            self.assertLessEqual(sum(entry.size for entry in cache._entries.values()), 2400)
            for owner in ("0", "1"):
                self.assertLessEqual(sum(entry.size for entry in cache._entries.values() if entry.session_id == owner), 1600)
        self.assertLessEqual(len(cache._expired), 4)
        self.category("cache_limit", cache.add_document, "owner", document("x" * 3000))

    def test_document_utf8_bytes_and_lines_are_bounded(self):
        cache = SourceCache(max_document_bytes=5)
        snapshot = cache.add_document("owner", document("żółć"))
        self.assertEqual(snapshot.lines, ("żó",))
        self.assertTrue(snapshot.truncated)
        cache = SourceCache(max_document_lines=2)
        snapshot = cache.add_document("owner", document())
        self.assertEqual(len(snapshot.lines), 2)
        self.assertTrue(snapshot.truncated)
        self.category("empty_content", cache.add_document, "owner", document(" \n\t"))
        with self.assertRaises(ValueError):
            SourceCache(max_session_entries=0)

    def test_late_reads_and_cross_owner_writes_are_rejected_after_deletion(self):
        self.cache.retain_search("owner", [record()])
        self.category("forbidden_reference", self.cache.add_document, "other", document(), "search_one")

        def late_read(_url):
            self.deleted.add("owner")
            self.cache.delete_session("owner")
            return document()

        self.backend.read.side_effect = late_read
        self.category("session_deleted", self.service.open, "owner", "search_one")
        self.assertFalse(self.cache._entries)
        self.category("session_deleted", self.cache.retain_search, "owner", [record()])
        self.category("session_deleted", self.cache.get, "owner", "search_one")

    def test_window_and_find_outputs_include_json_escaping_in_budget(self):
        self.backend.read.return_value = document(('"\\\t' * 2000) + "NEEDLE" + "x" * 10000)
        self.service.max_output_chars = 1024
        output = self.service.open("owner", URL)
        self.assertLessEqual(len(output), 1024)
        page = json.loads(output)
        self.assertTrue(page["line_truncated"])
        self.assertTrue(page["truncated"])
        output = self.service.find("owner", page["snapshot_id"], "needle")
        self.assertLessEqual(len(output), 1024)
        found = json.loads(output)
        self.assertEqual(found["match_count"], 1)
        if found["matches"]:
            self.assertIn("NEEDLE", found["matches"][0]["excerpt"])
        else:
            self.assertTrue(found["truncated"])

    def test_find_counts_all_lines_but_only_returns_bounded_matches(self):
        self.backend.read.return_value = document("\n".join(["alpha alpha"] * 20))
        page = json.loads(self.service.open("owner", URL))
        result = json.loads(self.service.find("owner", page["snapshot_id"], "alpha"))
        self.assertEqual(result["match_count"], 40)
        self.assertEqual(result["matching_lines"], 20)
        self.assertLessEqual(len(result["matches"]), 5)
        self.assertTrue(result["truncated"])
        self.category("invalid_arguments", self.service.find, "owner", page["snapshot_id"], "\n")
        self.category("invalid_arguments", self.service.find, "owner", page["snapshot_id"], "x" * 201)
        for start in (-1, True, "1"):
            self.category("invalid_arguments", self.service.open, "owner", page["snapshot_id"], start)
        self.category("invalid_line", self.service.open, "owner", page["snapshot_id"], 21)

    def test_close_clears_cache_even_if_backend_cleanup_fails(self):
        self.cache.retain_search("owner", [record()])
        self.backend.close.side_effect = RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            self.service.close()
        self.assertFalse(self.cache._entries)
        self.assertFalse(self.cache._expired)


if __name__ == "__main__":
    unittest.main()
