from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.tools.web.contracts import WebReaderError
from app.tools.web.resources import ReaderResourceScope


class ResourceScopeTests(unittest.TestCase):
    def scope(self):
        return ReaderResourceScope(max_memory_mb=96, max_cpu_percent=100, max_tasks=32)

    def write_controls(self, root, scope, **overrides):
        controls = {"memory.max": "100663296", "memory.swap.max": "0", "memory.oom.group": "1",
                    "pids.max": "32", "cpu.max": "100000 100000"}
        controls.update(overrides)
        directory = root / "user.slice" / scope.unit
        directory.mkdir(parents=True, exist_ok=True)
        for name, value in controls.items():
            (directory / name).write_text(value)
        return f"0::/user.slice/{scope.unit}"

    def test_scope_has_group_wide_memory_cpu_swap_tasks_and_oom_limits(self):
        scope = self.scope()
        with patch("app.tools.web.resources.shutil.which", return_value="/usr/bin/systemd-run"):
            command = scope.command(["/usr/bin/bwrap", "fixture"])
        for item in ("--user", "--scope", "--collect", "--no-ask-password", "--expand-environment=no",
                     "--property=MemoryMax=100663296", "--property=MemorySwapMax=0",
                     "--property=OOMPolicy=kill", "--property=CPUQuota=100%", "--property=TasksMax=32"):
            self.assertIn(item, command)
        self.assertEqual(command[-2:], ["/usr/bin/bwrap", "fixture"])

    def test_missing_manager_binary_is_actionable_and_has_no_uncontained_fallback(self):
        with patch("app.tools.web.resources.shutil.which", return_value=None):
            with self.assertRaises(WebReaderError) as raised:
                self.scope().command(["fixture"])
        self.assertEqual(raised.exception.category, "reader_unavailable")

    def test_actual_kernel_controls_and_owned_membership_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = self.scope()
            membership = self.write_controls(root, scope)
            limits = scope.verify(membership, root=root)
            self.assertEqual(limits["memory_max_bytes"], 100663296)
            for bad in ("0::/user.slice/unrelated.scope", "0::/../" + scope.unit, "0:://outside/" + scope.unit,
                        "bad", None, membership + "\nother"):
                with self.subTest(membership=bad), self.assertRaises(WebReaderError):
                    scope.verify(bad, root=root)

    def test_missing_unlimited_or_relaxed_controls_fail_closed(self):
        cases = ({"memory.max": "max"}, {"memory.max": "200000000"}, {"memory.swap.max": "1"},
                 {"memory.oom.group": "0"}, {"pids.max": "max"}, {"pids.max": "33"},
                 {"cpu.max": "max 100000"}, {"cpu.max": "200000 100000"}, {"cpu.max": "0 0"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = self.scope()
            for controls in cases:
                with self.subTest(controls=controls):
                    membership = self.write_controls(root, scope, **controls)
                    with self.assertRaises(WebReaderError) as raised:
                        scope.verify(membership, root=root)
                    self.assertEqual(raised.exception.category, "resource_isolation")
            (root / "user.slice" / scope.unit / "memory.max").unlink()
            with self.assertRaises(WebReaderError):
                scope.verify(membership, root=root)


if __name__ == "__main__":
    unittest.main()
