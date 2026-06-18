"""
Async task scheduler for managing recurring and one-time tasks.
Provides robust error handling and task lifecycle management.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, Optional
from uuid import uuid4

from loguru import logger


@dataclass
class ScheduledTask:
    """Represents a scheduled task with metadata."""

    task_id: str
    name: str
    callback: Callable[..., Coroutine[Any, Any, None]]
    interval: Optional[float] = None  # seconds for recurring tasks
    run_at: Optional[datetime] = None  # datetime for one-time tasks
    enabled: bool = True
    max_retries: int = 3
    retry_delay: float = 1.0
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    total_runs: int = 0
    total_failures: int = 0
    task: Optional[asyncio.Task] = None

    def is_recurring(self) -> bool:
        """Check if this is a recurring task."""
        return self.interval is not None

    def calculate_next_run(self) -> datetime:
        """Calculate the next run time for recurring tasks."""
        if not self.is_recurring() or self.interval is None:
            raise ValueError("Cannot calculate next run for non-recurring task")
        return datetime.now(timezone.utc) + timedelta(seconds=self.interval)


class TaskScheduler:
    """
    Advanced async task scheduler with support for recurring and one-time tasks.
    Provides error handling, retries, and monitoring capabilities.
    """

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._running = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._lock = asyncio.Lock()
        logger.info("TaskScheduler initialized")

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            logger.warning("TaskScheduler is already running")
            return

        self._running = True
        self._shutdown_event.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("TaskScheduler started successfully")

    async def stop(self) -> None:
        """Stop the scheduler and cancel all running tasks."""
        if not self._running:
            return

        self._running = False
        self._shutdown_event.set()

        # Cancel scheduler task
        if self._scheduler_task:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task

        # Cancel all running task instances
        async with self._lock:
            for task in self._tasks.values():
                if task.task and not task.task.done():
                    task.task.cancel()

        logger.info("TaskScheduler stopped successfully")

    def add_recurring_task(
        self,
        name: str,
        callback: Callable[..., Coroutine[Any, Any, None]],
        interval_seconds: float,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        enabled: bool = True,
    ) -> str:
        """
        Add a recurring task that runs every interval_seconds.
        Returns the task ID.
        """
        task_id = str(uuid4())
        now = datetime.now(timezone.utc)

        task = ScheduledTask(
            task_id=task_id,
            name=name,
            callback=callback,
            interval=interval_seconds,
            enabled=enabled,
            max_retries=max_retries,
            retry_delay=retry_delay,
            last_run=None,
            next_run=now + timedelta(seconds=interval_seconds),
        )

        self._tasks[task_id] = task
        logger.debug(
            f"Added recurring task '{name}' with ID {task_id}, interval={interval_seconds}s"
        )
        return task_id

    def add_one_time_task(
        self,
        name: str,
        callback: Callable[..., Coroutine[Any, Any, None]],
        run_at: datetime,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> str:
        """
        Add a one-time task that runs at the specified datetime.
        Returns the task ID.
        """
        task_id = str(uuid4())

        task = ScheduledTask(
            task_id=task_id,
            name=name,
            callback=callback,
            run_at=run_at,
            interval=None,
            enabled=True,
            max_retries=max_retries,
            retry_delay=retry_delay,
            last_run=None,
            next_run=run_at,
        )

        self._tasks[task_id] = task
        logger.debug(f"Added one-time task '{name}' with ID {task_id}, run_at={run_at}")
        return task_id

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task."""
        if task_id not in self._tasks:
            logger.warning(f"Attempted to remove non-existent task: {task_id}")
            return False

        task = self._tasks[task_id]
        if task.task and not task.task.done():
            task.task.cancel()

        del self._tasks[task_id]
        logger.debug(f"Removed task {task_id}: {task.name}")
        return True

    def enable_task(self, task_id: str) -> bool:
        """Enable a disabled task."""
        if task_id not in self._tasks:
            return False
        self._tasks[task_id].enabled = True
        return True

    def disable_task(self, task_id: str) -> bool:
        """Disable a task from running."""
        if task_id not in self._tasks:
            return False
        self._tasks[task_id].enabled = False
        return True

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """Get task by ID."""
        return self._tasks.get(task_id)

    def get_all_tasks(self) -> Dict[str, ScheduledTask]:
        """Get all registered tasks."""
        return self._tasks.copy()

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop that checks for tasks to execute."""
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                await self._check_and_execute_tasks(now)
                # Sleep for a short interval to avoid high CPU usage
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {str(e)}", exc_info=True)
                await asyncio.sleep(1.0)

    async def _check_and_execute_tasks(self, now: datetime) -> None:
        """Check all tasks and execute those that are due."""
        async with self._lock:
            for task in self._tasks.values():
                if not task.enabled:
                    continue

                if task.next_run and now >= task.next_run:
                    if not task.task or task.task.done():
                        # Task is due to run
                        task.task = asyncio.create_task(self._execute_task(task))

    async def _execute_task(self, task: ScheduledTask) -> None:
        """Execute a task with retries and error handling."""
        task.last_run = datetime.now(timezone.utc)
        task.total_runs += 1

        logger.debug(f"Executing task: {task.name} ({task.task_id})")

        # Execute with retries
        for attempt in range(task.max_retries):
            try:
                await task.callback()
                break
            except Exception as e:
                task.total_failures += 1
                if attempt < task.max_retries - 1:
                    logger.warning(
                        f"Task {task.name} failed (attempt {attempt + 1}/{task.max_retries}): {str(e)}. Retrying..."
                    )
                    await asyncio.sleep(task.retry_delay)
                else:
                    logger.error(
                        f"Task {task.name} failed permanently after {task.max_retries} attempts: {str(e)}",
                        exc_info=True,
                    )

        # Calculate next run for recurring tasks
        if task.is_recurring():
            task.next_run = task.calculate_next_run()
        else:
            # One-time task, disable after execution
            task.enabled = False

    def get_task_stats(self) -> Dict[str, Any]:
        """Get scheduler statistics."""
        total_runs = sum(t.total_runs for t in self._tasks.values())
        total_failures = sum(t.total_failures for t in self._tasks.values())
        enabled_tasks = sum(1 for t in self._tasks.values() if t.enabled)

        return {
            "total_tasks": len(self._tasks),
            "enabled_tasks": enabled_tasks,
            "total_runs": total_runs,
            "total_failures": total_failures,
            "is_running": self._running,
        }


# Global scheduler instance
task_scheduler = TaskScheduler()
