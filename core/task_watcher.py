import asyncio
import traceback
from typing import Any, Callable, Coroutine, Dict

from loguru import logger


class TaskWatcher:
    """Supervises background tasks and handles failures."""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = True
        logger.info("TaskWatcher initialized")

    def watch(
        self, coro_func: Callable[[], Coroutine[Any, Any, Any]], name: str, restart: bool = True
    ) -> asyncio.Task:
        """Watch a coroutine generator as a background task."""
        task = asyncio.create_task(self._run_and_monitor(coro_func, name, restart))
        self._tasks[name] = task
        return task

    async def _run_and_monitor(
        self, coro_func: Callable[[], Coroutine[Any, Any, Any]], name: str, restart: bool
    ) -> None:
        """Run coroutine and monitor its outcome."""
        while self._running:
            try:
                logger.debug(f"Starting watched task: {name}")
                await coro_func()
                logger.info(f"Task {name} completed normally")
                # Reset retry attempt sau khi task chạy thành công
                if hasattr(self, f'_{name}_retry_attempt'):
                    delattr(self, f'_{name}_retry_attempt')
                break
            except asyncio.CancelledError:
                logger.info(f"Task {name} was cancelled")
                if hasattr(self, f'_{name}_retry_attempt'):
                    delattr(self, f'_{name}_retry_attempt')
                break
            except Exception as e:
                logger.error(f"Task {name} failed with error: {e}")
                logger.error(traceback.format_exc())
                if not restart or not self._running:
                    if hasattr(self, f'_{name}_retry_attempt'):
                        delattr(self, f'_{name}_retry_attempt')
                    break
                # Thêm exponential backoff: 2s, 4s, 8s, max 30s để tránh thundering herd
                attempt = getattr(self, f'_{name}_retry_attempt', 0) + 1
                setattr(self, f'_{name}_retry_attempt', attempt)
                wait_time = min(2 ** attempt, 30)  # Exponential backoff, max 30s
                logger.info(f"Restarting task {name} in {wait_time}s (attempt #{attempt})...")
                await asyncio.sleep(wait_time)

    def stop_all(self) -> None:
        """Stop all watched tasks."""
        self._running = False
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                logger.debug(f"Cancelled task: {name}")