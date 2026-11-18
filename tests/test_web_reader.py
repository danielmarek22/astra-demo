import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import time
import unittest
from unittest.mock import patch

from app.tools.web.contracts import WebReaderError
from app.tools.web.reader import Crawl4AIReader, isolated_command


class WebReaderTests(unittest.TestCase):
    def reader(self, **settings):
        reader = Crawl4AIReader(**settings)
        self.addCleanup(reader.close)
        return reader

    def test_missing_isolation_prerequisites_fail_closed(self):
        with patch("app.tools.web.reader.shutil.which", return_value=None):
            with self.assertRaises(WebReaderError) as raised:
                isolated_command("/usr/bin/python3", "/tmp")
        self.assertEqual(raised.exception.category, "reader_unavailable")

    def test_isolation_mounts_no_project_or_home_and_has_no_network_namespace_access(self):
        with patch("app.tools.web.reader.shutil.which", return_value="/usr/bin/bwrap"), \
                patch.object(Path, "is_file", return_value=True), \
                patch.object(Path, "is_dir", return_value=True):
            command = isolated_command("/opt/reader-env/bin/python", "/opt/browser-cache")
        for flag in ("--unshare-net", "--unshare-pid", "--clearenv", "--die-with-parent"):
            self.assertIn(flag, command)
        self.assertNotIn(str(Path.home()), command)
        self.assertNotIn(str(Path.cwd()), command)
        self.assertNotIn("--no-sandbox", command)
        self.assertIn("/opt/reader-env", command)
        self.assertIn("67108864", command)

    def test_total_deadline_cancels_active_read_and_worker_can_be_reused(self):
        reader = self.reader(total_timeout_s=0.05)
        cancelled = Event()

        async def blocked(_url):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        reader._read = blocked
        started = time.monotonic()
        with self.assertRaises(WebReaderError) as raised:
            reader.read("https://example.org/")
        self.assertEqual(raised.exception.category, "timeout")
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(cancelled.is_set())

        async def immediate(_url):
            return "ready"

        reader._read = immediate
        self.assertEqual(reader.read("https://example.org/"), "ready")

    def test_close_cancels_active_work_and_is_repeatable_without_leaking_thread(self):
        reader = self.reader()
        entered, cancelled = Event(), Event()

        async def blocked(_url):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        reader._read = blocked
        with ThreadPoolExecutor(max_workers=1) as callers:
            future = callers.submit(reader.read, "https://example.org/")
            self.assertTrue(entered.wait(1))
            reader.close()
            reader.close()
            with self.assertRaises(WebReaderError) as raised:
                future.result(timeout=1)
        self.assertEqual(raised.exception.category, "reader_unavailable")
        self.assertTrue(cancelled.is_set())
        self.assertFalse(reader._thread.is_alive())
        with self.assertRaises(WebReaderError):
            reader.read("https://example.org/")

    def test_queue_wait_is_part_of_total_deadline(self):
        reader = self.reader(total_timeout_s=0.03)
        reader._slot.acquire()
        try:
            with self.assertRaises(WebReaderError) as raised:
                reader.read("https://example.org/")
            self.assertEqual(raised.exception.category, "timeout")
            self.assertIsNone(reader._thread)
        finally:
            reader._slot.release()

    def test_event_loop_start_failure_and_unstarted_close_are_safe(self):
        reader = self.reader()
        with patch("app.tools.web.reader.asyncio.new_event_loop", side_effect=PermissionError("blocked")):
            with self.assertRaises(WebReaderError) as raised:
                reader.read("https://example.org/")
        self.assertEqual(raised.exception.category, "reader_unavailable")
        reader.close()
        unopened = self.reader()
        unopened.close()
        self.assertIsNone(unopened._thread)


if __name__ == "__main__":
    unittest.main()
