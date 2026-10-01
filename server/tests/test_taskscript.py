"""Regression tests for transient list_tasks failures in upstream scripts."""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import taskscript  # noqa: E402


class TransientErrorTest(unittest.TestCase):
    def test_recognizes_only_retryable_list_errors(self) -> None:
        for status in (-1, 408, 429, 500, 503, 599):
            self.assertTrue(taskscript._is_transient_list_tasks_error(
                RuntimeError(f"list_tasks http={status}")))
        for text in ("list_tasks http=400", "list_tasks http=401",
                     "list_tasks http=404", "other http=-1"):
            self.assertFalse(taskscript._is_transient_list_tasks_error(RuntimeError(text)))


class ListTasksRetryTest(unittest.TestCase):
    def test_transient_failure_is_retried_then_recovers(self) -> None:
        module = ModuleType("task_common_test")
        calls = {"count": 0}
        sleeps: list[float] = []

        def list_tasks(auth):
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("list_tasks http=-1")
            return [{"task_code": "Buddy_App"}]

        module.list_tasks = list_tasks
        taskscript.install_list_tasks_retry(module, sleep=sleeps.append)

        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = module.list_tasks({"uid": "abcdefgh"})

        self.assertEqual(result, [{"task_code": "Buddy_App"}])
        self.assertEqual(calls["count"], 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertIn("重试 2/3", output.getvalue())

    def test_non_transient_failure_is_not_retried(self) -> None:
        module = ModuleType("task_common_test")
        calls = {"count": 0}
        sleeps: list[float] = []

        def list_tasks(auth):
            calls["count"] += 1
            raise RuntimeError("list_tasks http=401")

        module.list_tasks = list_tasks
        taskscript.install_list_tasks_retry(module, sleep=sleeps.append)

        with self.assertRaisesRegex(RuntimeError, "http=401"):
            module.list_tasks({})
        self.assertEqual(calls["count"], 1)
        self.assertEqual(sleeps, [])


class ProcessTaskGuardTest(unittest.TestCase):
    def test_exhausted_transient_read_skips_one_task_and_continues(self) -> None:
        module = ModuleType("task_runner_test")

        def process_task(auth, code, task, opts, stats):
            raise RuntimeError("list_tasks http=-1")

        module.process_task = process_task
        taskscript.install_process_task_guard(module)
        stats = {"fail": 2}

        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = module.process_task(
                {"uid": "8763e505xxxx"}, "Buddy_App", {}, object(), stats)

        self.assertIsNone(result)
        self.assertEqual(stats["fail"], 3)
        self.assertIn("跳过本任务并继续后续任务", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_programming_error_still_propagates(self) -> None:
        module = ModuleType("task_runner_test")

        def process_task(auth, code, task, opts, stats):
            raise TypeError("bad task shape")

        module.process_task = process_task
        taskscript.install_process_task_guard(module)
        with self.assertRaisesRegex(TypeError, "bad task shape"):
            module.process_task({}, "x", {}, object(), {"fail": 0})


if __name__ == "__main__":
    unittest.main()
