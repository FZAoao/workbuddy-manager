"""Run upstream task scripts with narrow network-failure hardening.

The upstream ``task_runner.py`` deliberately owns the task protocol.  This
module does not reimplement that protocol; it only adds two safety rails around
its read-only ``list_tasks`` call:

* retry transient transport/server failures a few times; and
* if every retry fails while processing one task, skip that task instead of
  crashing the whole multi-account run after earlier writes already succeeded.

Only errors explicitly identified as transient are handled.  Authentication,
business-logic, and programming errors still propagate unchanged.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Callable

_RETRY_DELAYS = (1.0, 2.0, 4.0)
_LIST_TASK_ERROR = re.compile(r"(?:^|\s)(?:mp\s+)?list_tasks http=(-?\d+)(?:\s|$)")


def _is_transient_list_tasks_error(exc: BaseException) -> bool:
    """Return whether an upstream list read is safe to retry/skip."""
    match = _LIST_TASK_ERROR.search(str(exc))
    if not match:
        return False
    status = int(match.group(1))
    return status == -1 or status in (408, 429) or 500 <= status <= 599


def install_list_tasks_retry(
    task_common: ModuleType,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wrap ``task_common.list_tasks`` with bounded transient retries."""
    original = getattr(task_common, "list_tasks", None)
    if not callable(original) or getattr(original, "_workbuddy_retry", False):
        return

    def list_tasks_with_retry(*args, **kwargs):
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return original(*args, **kwargs)
            except Exception as exc:  # upstream exposes failures as RuntimeError
                if not _is_transient_list_tasks_error(exc) or attempt >= len(_RETRY_DELAYS):
                    raise
                delay = _RETRY_DELAYS[attempt]
                print(
                    f"[task_runner] list_tasks 临时失败（{exc}），"
                    f"{delay:g}s 后重试 {attempt + 1}/{len(_RETRY_DELAYS)}",
                    flush=True,
                )
                sleep(delay)
        raise AssertionError("unreachable")

    list_tasks_with_retry._workbuddy_retry = True  # type: ignore[attr-defined]
    task_common.list_tasks = list_tasks_with_retry


def install_process_task_guard(task_runner: ModuleType) -> None:
    """Keep one exhausted transient read from aborting the entire task run."""
    original = getattr(task_runner, "process_task", None)
    if not callable(original) or getattr(original, "_workbuddy_guard", False):
        return

    def process_task_guarded(auth, code, task, opts, stats):
        try:
            return original(auth, code, task, opts, stats)
        except Exception as exc:
            if not _is_transient_list_tasks_error(exc):
                raise
            uid = str((auth or {}).get("uid") or "unknown")[:8]
            print(
                f"[task_runner] {uid} {code}: list_tasks 重试后仍失败（{exc}），"
                "跳过本任务并继续后续任务",
                flush=True,
            )
            if isinstance(stats, dict):
                stats["fail"] = int(stats.get("fail") or 0) + 1
            return None

    process_task_guarded._workbuddy_guard = True  # type: ignore[attr-defined]
    task_runner.process_task = process_task_guarded


def _load_task_common(script_dir: Path) -> ModuleType:
    """Import the task_common next to the selected upstream script."""
    sys.path.insert(0, str(script_dir))
    importlib.invalidate_caches()
    task_common = importlib.import_module("task_common")
    install_list_tasks_retry(task_common)
    return task_common


def _run_task_runner(script: Path) -> int:
    """Load task_runner without its __main__ block, install guard, then run."""
    module_name = "_workbuddy_upstream_task_runner"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载上游任务脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    install_process_task_guard(module)
    main = getattr(module, "main", None)
    if not callable(main):
        raise RuntimeError(f"上游任务脚本缺少 main()：{script}")
    result = main()
    return result if isinstance(result, int) else 0


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：taskscript.py <上游脚本> [参数...]", file=sys.stderr)
        return 2

    script = Path(sys.argv[1]).resolve()
    if not script.is_file():
        print(f"上游任务脚本不存在：{script}", file=sys.stderr)
        return 2

    # Hide this compatibility runner from the upstream argparse contract.
    sys.argv = [str(script), *sys.argv[2:]]

    if script.name == "task_runner.py":
        _load_task_common(script.parent)
        return _run_task_runner(script)

    # The school helper also imports task_common, so it benefits from the same
    # read retry.  Arbitrary scripts are passed through unchanged; this keeps
    # the runner transparent for diagnostics and smoke tests.
    if script.name == "school_open_day_2026.py":
        _load_task_common(script.parent)
    import runpy
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
