"""Dashboard controller for managing auto-update and refresh logic.

This controller keeps Telegram dashboard messages fresh while throttling
updates to avoid excessive API usage and repeated UI refreshes.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from core.event_bus_components import Event
from core.event_bus import EventBus
from core.events.topics import EventTopic


class DashboardController:
    """Manages dashboard auto-update, throttling, and message lifecycle."""

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._message_id: Optional[int] = None
        self._last_update: Optional[datetime] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._min_update_interval = 20  # Seconds between updates

    def set_message_id(self, message_id: int) -> None:
        """Set the active dashboard message ID."""
        self._message_id = message_id
        logger.debug(f"Dashboard message ID set to {message_id}")

    def clear_message_id(self) -> None:
        """Clear the active dashboard message ID."""
        if self._message_id is not None:
            logger.debug(f"Dashboard message ID cleared (was {self._message_id})")
        self._message_id = None

    def has_active_dashboard(self) -> bool:
        """Check if a dashboard message is currently active."""
        return self._message_id is not None

    async def start_auto_update(self) -> None:
        """Start the dashboard auto-update background loop."""
        if self._running:
            logger.warning("Dashboard auto-update already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._auto_update_loop())

    async def stop_auto_update(self) -> None:
        """Stop the dashboard auto-update task gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("Dashboard auto-update task cancelled")

    async def _auto_update_loop(self) -> None:
        """Background loop for periodic dashboard updates."""
        while self._running:
            try:
                if self._message_id is not None:
                    now = datetime.now(timezone.utc)
                    if self._last_update is not None:
                        elapsed = (now - self._last_update).total_seconds()
                        if elapsed < self._min_update_interval:
                            logger.debug(
                                f"Dashboard refresh delayed to avoid too-frequent updates ({elapsed:.1f}s)"
                            )
                            await asyncio.sleep(1)
                            continue

                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA,
                            data={"action": "dashboard", "message_id": self._message_id},
                            source="telegram_bot",
                        )
                    )
                    self._last_update = datetime.now(timezone.utc)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dashboard auto-update error: {e}", exc_info=True)

            await asyncio.sleep(5)

    def mark_updated(self) -> None:
        """Record the dashboard as recently updated to throttle subsequent refreshes."""
        self._last_update = datetime.now(timezone.utc)