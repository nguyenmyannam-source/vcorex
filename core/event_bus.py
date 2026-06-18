"""
Asynchronous event bus for inter-component communication.
Implements publish-subscribe pattern with type-safe event handling.
Supports both in-process queues and distributed Redis Streams.
"""

import asyncio
import contextlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4
from loguru import logger
from collections import deque

from core.event_bus_components import Event, EventHandler
from core.events.topics import EventTopic
from core.dlq import DeadLetterQueue
from core.metrics import InMemoryMetricsAdapter, MetricsAdapter
from core.circuit_breaker import BaseCircuitBreaker, CircuitState
from core.config.settings import settings
import threading

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class IEventBus(ABC):
    """Abstract interface for EventBus transport layers."""

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass

    @abstractmethod
    def subscribe(
        self,
        callback: Callable[[Event], Any],
        event_types: List[str],
        filter_func: Optional[Callable[[Event], bool]] = None,
        handler_id: Optional[str] = None,
    ) -> str:
        pass

    @abstractmethod
    def unsubscribe(self, handler_id: str) -> bool:
        pass

    @abstractmethod
    async def publish(self, event: Event) -> None:
        pass

    @abstractmethod
    async def acknowledge(self, event_id: str, handler_id: str) -> None:
        pass

    @abstractmethod
    async def retry(self, event: Event, handler_id: str) -> None:
        pass

    @abstractmethod
    async def dead_letter(self, event: Event, handler_id: str, error: str) -> None:
        pass


class InProcessEventBus(IEventBus):
    """
    Central in-process event bus for asynchronous event distribution.
    Uses asyncio.Queue.
    """

    def __init__(self, metrics: Optional[MetricsAdapter] = None):
        """Initialize in-process event bus with optional custom metrics adapter"""
        self._handlers: List[EventHandler] = []
        self._event_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10000)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._running_tasks: Set[asyncio.Task] = set()
        self._max_history_size = 1000
        self._event_history: deque = deque(maxlen=self._max_history_size)
        
        # Use provided metrics or create default
        self._metrics = metrics or InMemoryMetricsAdapter()
        
        # Initialize DLQ with same metrics
        self.dlq = DeadLetterQueue(self._metrics)
        self._retry_counts: Dict[str, int] = {}

        # Circuit Breaker - use unified implementation
        self._circuit_breaker = BaseCircuitBreaker(
            threshold=settings.eventbus_cb_threshold,
            cooldown=settings.eventbus_cb_cooldown,
            name="eventbus"
        )
        
        # Legacy metrics for backward compatibility - will be deprecated
        self.metrics = {
            "retry_count_total": 0,
            "handler_failure_rate": 0.0,
            "dlq_count": 0,
            "duplicate_prevented_count": 0,
            "total_executions": 0,
            "total_failures": 0,
            "retry_tasks_active": 0,
            "retry_tasks_cancelled": 0,
            "events_dropped_by_circuit_breaker": 0
        }
        self.backoff_multiplier = 0.25
        logger.info("InProcessEventBus initialized with unified circuit breaker and metrics")
        # Concurrency guards for handler registry and running tasks (use threading lock + snapshot reads)
        self._handlers_lock = threading.RLock()
        self._running_tasks_lock = threading.RLock()

    async def start(self) -> None:
        if self._running:
            logger.warning("EventBus is already running")
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._event_worker())
        logger.info("InProcessEventBus started successfully")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

        # Cancel active handler tasks and await cleanup
        with self._running_tasks_lock:
            tasks_to_cancel = list(self._running_tasks)
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        remaining_events = []
        while not self._event_queue.empty():
            try:
                remaining_events.append(self._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        logger.info(f"InProcessEventBus stopped. Remaining events: {len(remaining_events)}")

    def subscribe(
        self,
        callback: Callable[[Event], Any],
        event_types: List[str],
        filter_func: Optional[Callable[[Event], bool]] = None,
        handler_id: Optional[str] = None,
    ) -> str:
        handler_id = handler_id or str(uuid4())
        handler = EventHandler(
            callback=callback,
            event_types=set(event_types),
            handler_id=handler_id,
            filter_func=filter_func,
        )
        with self._handlers_lock:
            self._handlers.append(handler)
            total = len(self._handlers)
        logger.info(
            f"[EVENTBUS] Handler {handler_id} subscribed to events: {event_types}, is_async={handler.is_async}, total_handlers={total}"
        )
        return handler_id

    def unsubscribe(self, handler_id: str) -> bool:
        with self._handlers_lock:
            for i, handler in enumerate(self._handlers):
                if handler.handler_id == handler_id:
                    del self._handlers[i]
                    logger.debug(f"Handler {handler_id} unsubscribed")
                    return True
        logger.warning(f"Handler {handler_id} not found for unsubscribe")
        return False

    async def publish(self, event: Event) -> None:
        if not self._running:
            logger.warning(f"EventBus not running, event {event.event_type} queued")
            
        # CIRCUIT BREAKER: Check if we should allow this event through
        if not self._circuit_breaker.allow_request():
            # Only allow critical events through even when circuit is open
            critical_events = {
                EventTopic.STRATEGY_SIGNAL_GENERATED,
                EventTopic.RISK_SIGNAL_APPROVED,
                EventTopic.POSITION_OPENED,
                EventTopic.POSITION_CLOSED,
                EventTopic.POSITION_CLOSE_REQUEST,
                EventTopic.CONTROL_HALT_TRADING,
                EventTopic.CONTROL_EMERGENCY_STOP
            }
            
            if event.event_type not in critical_events:
                logger.warning(
                    f"[CIRCUIT BREAKER] Dropped non-critical event {event.event_type}. "
                    f"Circuit state: {self._circuit_breaker.state.value}"
                )
                self._metrics.increment("eventbus_events_dropped", tags={"event_type": event.event_type})
                self.metrics["events_dropped_by_circuit_breaker"] += 1
                return

        # Queue size monitoring - trigger circuit breaker if queue is filling up
        queue_size = self._event_queue.qsize()
        max_queue_size = self._event_queue.maxsize  # 10000
        
        if queue_size > max_queue_size * 0.8:  # >80% capacity
            self._circuit_breaker.record_failure()
            self._metrics.gauge("eventbus_queue_size", queue_size)

        try:
            self._event_queue.put_nowait(event)
            self._event_history.append(event)
        except asyncio.QueueFull:
            logger.error(f"Event queue full, dropped event: {event.event_type}")

    async def acknowledge(self, event_id: str, handler_id: str) -> None:
        # No-op for in-process queue
        key = f"{event_id}:{handler_id}"
        if key in self._retry_counts:
            del self._retry_counts[key]

    async def retry(self, event: Event, handler_id: str) -> None:
        if not self._running:
            logger.warning(f"EventBus is shutting down. Dropping retry for event {event.event_id} on handler {handler_id}")
            return

        event.retry_count += 1
        self.metrics["retry_count_total"] += 1

        if not event.first_failure_timestamp:
            event.first_failure_timestamp = datetime.now(timezone.utc)

        key = f"{event.event_id}:{handler_id}"
        self._retry_counts[key] = event.retry_count

        if event.retry_count > 3:
            await self.dead_letter(event, handler_id, f"Max retries exceeded on handler {handler_id}")
        else:
            # Exponential backoff: min 5s, starts small
            backoff = min(5.0, self.backoff_multiplier * (2 ** event.retry_count))
            logger.warning(f"Retrying event {event.event_id} on handler {handler_id} (attempt {event.retry_count}/3) after {backoff}s backoff")

            self.metrics["retry_tasks_active"] += 1

            async def delayed_retry():
                try:
                    await asyncio.sleep(backoff)
                    if not self._running:
                        return
                    # Re-queue the event via normal worker path so _process_event
                    # can skip already-successful handlers (incrementing duplicate_prevented_count)
                    # and only retry the failed handler.
                    await self._event_queue.put(event)
                except asyncio.CancelledError:
                    self.metrics["retry_tasks_cancelled"] += 1
                    logger.debug(f"Delayed retry task cancelled for event {event.event_id}")
                    raise
                finally:
                    self.metrics["retry_tasks_active"] = max(0, self.metrics["retry_tasks_active"] - 1)

            task = asyncio.create_task(delayed_retry())
            with self._running_tasks_lock:
                self._running_tasks.add(task)
            task.add_done_callback(self._on_task_done)

    async def dead_letter(self, event: Event, handler_id: str, error: str) -> None:
        logger.error(f"Routing event {event.event_id} to DLQ. Error: {error}")
        self.metrics["dlq_count"] += 1
        key = f"{event.event_id}:{handler_id}"
        await self.dlq.quarantine(event, error, self._retry_counts.get(key, event.retry_count))
        dlq_event = Event(
            event_type="system.dead_letter",
            data={
                "failed_event_id": event.event_id,
                "failed_topic": event.event_type,
                "handler_id": handler_id,
                "error": error,
                "payload": event.data
            },
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            parent_request_id=event.parent_request_id,
            event_version="1.0"
        )
        await self.publish(dlq_event)
        if key in self._retry_counts:
            del self._retry_counts[key]

    async def _event_worker(self) -> None:
        while self._running:
            try:
                event = await self._event_queue.get()
                await self._process_event(event)
                self._event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in event worker: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    async def _process_event(self, event: Event) -> None:
        # Create a snapshot copy of handlers to prevent modification during iteration
        with self._handlers_lock:
            handlers_snapshot = list(self._handlers)
        matched_handlers = [h for h in handlers_snapshot if h.matches(event)]
        if event.event_type == EventTopic.RISK_SIGNAL_APPROVED:
            logger.info(f"[EVENTBUS] Processing {event.event_type}: matched {len(matched_handlers)} handlers out of {len(self._handlers)} total")
        for handler in matched_handlers:
            # 1. Skip if already successfully executed
            if handler.handler_id in event.successful_handler_ids:
                self.metrics["duplicate_prevented_count"] += 1
                logger.debug(f"[EVENTBUS] Skipping already successful handler {handler.handler_id} for event {event.event_type}")
                continue

            # 2. Check if this is a retry and only retry the failed ones
            if event.failed_handler_ids and handler.handler_id not in event.failed_handler_ids:
                continue

            if handler.is_async:
                task = asyncio.create_task(self._execute_async_handler(handler, event))
                with self._running_tasks_lock:
                    self._running_tasks.add(task)
                task.add_done_callback(self._on_task_done)
            else:
                try:
                    self.metrics["total_executions"] += 1
                    handler.callback(event)
                    event.successful_handler_ids.add(handler.handler_id)
                    if handler.handler_id in event.failed_handler_ids:
                        event.failed_handler_ids.discard(handler.handler_id)
                    await self.acknowledge(event.event_id, handler.handler_id)
                except Exception as e:
                    self.metrics["total_failures"] += 1
                    self.metrics["handler_failure_rate"] = self.metrics["total_failures"] / self.metrics["total_executions"]
                    logger.error(f"Error in sync handler {handler.handler_id}: {e}", exc_info=True)
                    event.failed_handler_ids.add(handler.handler_id)
                    await self.dead_letter(event, handler.handler_id, str(e))

    def _on_task_done(self, task: asyncio.Task) -> None:
        with self._running_tasks_lock:
            self._running_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"Event handler task failed: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass

    def get_handlers_snapshot(self) -> List[EventHandler]:
        """Return a shallow copy snapshot of handlers for safe external inspection."""
        with self._handlers_lock:
            return list(self._handlers)

    async def _execute_async_handler(self, handler: EventHandler, event: Event) -> None:
        try:
            if event.event_type == EventTopic.RISK_SIGNAL_APPROVED:
                logger.info(f"[EVENTBUS] Executing async handler {handler.handler_id} for {event.event_type}")
            self.metrics["total_executions"] += 1
            await handler.callback(event)
            event.successful_handler_ids.add(handler.handler_id)
            if handler.handler_id in event.failed_handler_ids:
                event.failed_handler_ids.discard(handler.handler_id)
            await self.acknowledge(event.event_id, handler.handler_id)
        except Exception as e:
            self.metrics["total_failures"] += 1
            self.metrics["handler_failure_rate"] = self.metrics["total_failures"] / self.metrics["total_executions"]
            logger.error(
                f"Error in async handler {handler.handler_id} for event {event.event_type}: {e}",
                exc_info=True,
            )
            event.failed_handler_ids.add(handler.handler_id)
            await self.retry(event, handler.handler_id)

    def get_recent_events(self, limit: int = 100) -> List[Event]:
        return list(self._event_history)[-limit:]

    def get_handler_count(self) -> int:
        return len(self._handlers)


class RedisStreamsEventBus(IEventBus):
    """
    Distributed EventBus prototype implementing Redis Streams transport.
    Falls back gracefully to InProcessEventBus if Redis is unavailable.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", group_name: str = "vcorex_group"):
        self.redis_url = redis_url
        self.group_name = group_name
        self.consumer_name = f"vcorex_consumer_{str(uuid4())[:8]}"
        self.stream_key = "vcorex:events"
        self.fallback = InProcessEventBus()
        self.use_fallback = not REDIS_AVAILABLE
        self.client: Optional[Any] = None
        self._running = False
        self._poller_task: Optional[asyncio.Task] = None
        self._local_handlers: List[EventHandler] = []
        self._retry_counts: Dict[str, int] = {}
        self._processed_handler_events: Dict[str, set] = {}
        self._running_tasks: set[asyncio.Task] = set()
        self.dlq = DeadLetterQueue(InMemoryMetricsAdapter())

        if self.use_fallback:
            logger.warning("Redis is not available. Falling back to InProcessEventBus.")

    @property
    def _metrics(self):
        """Delegate metrics to in-process fallback bus (shared adapter)."""
        return self.fallback._metrics

    def get_handlers_snapshot(self) -> List["EventHandler"]:
        """Return merged handler list for bus promotion / migration."""
        if self.use_fallback:
            return self.fallback.get_handlers_snapshot()
        handlers = list(self._local_handlers)
        if hasattr(self.fallback, "get_handlers_snapshot"):
            seen = {h.handler_id for h in handlers}
            for handler in self.fallback.get_handlers_snapshot():
                if handler.handler_id not in seen:
                    handlers.append(handler)
        return handlers

    async def start(self) -> None:
        if self.use_fallback:
            await self.fallback.start()
            return

        self._running = True
        try:
            # Try to connect to Redis with connection pooling
            self.client = aioredis.from_url(
                self.redis_url, 
                decode_responses=True,
                max_connections=10,
                socket_connect_timeout=5,
                socket_keepalive=True
            )
            # Ping redis to verify active connection
            await self.client.ping()

            # Create consumer group (ignoring BUSYGROUP errors if already exists)
            try:
                await self.client.xgroup_create(self.stream_key, self.group_name, id="0", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise e

            self._poller_task = asyncio.create_task(self._redis_poller())
            logger.info("RedisStreamsEventBus started successfully.")
        except Exception as e:
            logger.warning(f"Failed to connect to Redis: {e}. Falling back to InProcessEventBus.")
            self.use_fallback = True
            await self.fallback.start()

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._running_tasks.discard(task)
        if not task.cancelled():
            err = task.exception()
            if err:
                logger.error(f"Background task failed in RedisStreamsEventBus: {err}")

    async def stop(self) -> None:
        self._running = False
        if self.use_fallback:
            await self.fallback.stop()
            return

        if self._poller_task:
            self._poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller_task
        if self.client:
            await self.client.close()
        logger.info("RedisStreamsEventBus stopped.")

    def subscribe(
        self,
        callback: Callable[[Event], Any],
        event_types: List[str],
        filter_func: Optional[Callable[[Event], bool]] = None,
        handler_id: Optional[str] = None,
    ) -> str:
        if self.use_fallback:
            return self.fallback.subscribe(callback, event_types, filter_func, handler_id)

        handler_id = handler_id or str(uuid4())
        handler = EventHandler(
            callback=callback,
            event_types=set(event_types),
            handler_id=handler_id,
            filter_func=filter_func,
        )
        self._local_handlers.append(handler)
        logger.debug(f"Local handler {handler_id} subscribed to Redis stream.")
        return handler_id

    def unsubscribe(self, handler_id: str) -> bool:
        if self.use_fallback:
            return self.fallback.unsubscribe(handler_id)

        for i, handler in enumerate(self._local_handlers):
            if handler.handler_id == handler_id:
                del self._local_handlers[i]
                logger.debug(f"Local handler {handler_id} unsubscribed from Redis stream.")
                return True
        return False

    async def publish(self, event: Event) -> None:
        if self.use_fallback:
            await self.fallback.publish(event)
            return

        try:
            # Deterministic serialization using sort_keys
            serialized_event = json.dumps({
                "event_type": event.event_type,
                "data": event.data,
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
                "source": event.source,
                "correlation_id": event.correlation_id,
                "causation_id": event.causation_id,
                "parent_request_id": event.parent_request_id,
                "event_version": event.event_version,
            }, sort_keys=True)

            # Limit queue size (maxlen=10000) for backpressure safety
            await self.client.xadd(self.stream_key, {"payload": serialized_event}, maxlen=10000, approximate=True)
        except Exception as e:
            logger.error(f"Failed to publish to Redis stream: {e}. Falling back to InProcessEventBus.")
            # Trigger immediate fallback publish
            self.use_fallback = True
            await self.fallback.start()
            existing_ids = {h.handler_id for h in getattr(self.fallback, "_handlers", [])}
            for handler in self._local_handlers:
                if handler.handler_id in existing_ids:
                    continue
                self.fallback.subscribe(
                    handler.callback,
                    list(handler.event_types),
                    handler.filter_func,
                    handler.handler_id,
                )
                existing_ids.add(handler.handler_id)
            await self.fallback.publish(event)

    async def acknowledge(self, event_id: str, handler_id: str) -> None:
        if self.use_fallback:
            await self.fallback.acknowledge(event_id, handler_id)
            return

        key = f"{event_id}:{handler_id}"
        if key in self._retry_counts:
            del self._retry_counts[key]
        # xack takes the stream message ID, which is mapping to redis message ID

    async def retry(self, event: Event, handler_id: str) -> None:
        if self.use_fallback:
            await self.fallback.retry(event, handler_id)
            return

        key = f"{event.event_id}:{handler_id}"
        count = self._retry_counts.get(key, 0) + 1
        self._retry_counts[key] = count
        if count > 3:
            await self.dead_letter(event, handler_id, f"Max retries exceeded on handler {handler_id}")
        else:
            logger.warning(f"Retrying event {event.event_id} on handler {handler_id} (attempt {count}/3)")
            backoff = min(5.0, 1.5 * (2 ** count))

            async def delayed_retry():
                try:
                    await asyncio.sleep(backoff)
                    if not self._running:
                        return
                    handler = next((h for h in self._local_handlers if h.handler_id == handler_id), None)
                    if not handler:
                        logger.warning(f"Handler {handler_id} not found during Redis retry.")
                        return

                    # Hardened execution path ensuring idempotency
                    if handler_id not in self._processed_handler_events:
                        self._processed_handler_events[handler_id] = set()

                    if handler.is_async:
                        await handler.callback(event)
                    else:
                        handler.callback(event)

                    self._processed_handler_events[handler_id].add(event.event_id)
                    if len(self._processed_handler_events[handler_id]) > 5000:
                        self._processed_handler_events[handler_id] = set(list(self._processed_handler_events[handler_id])[-4000:])

                    await self.acknowledge(event.event_id, handler_id)
                except Exception as e:
                    logger.error(f"Local handler {handler_id} failed processing Redis event during retry: {e}", exc_info=True)
                    await self.retry(event, handler_id)

            task = asyncio.create_task(delayed_retry())
            self._running_tasks.add(task)
            task.add_done_callback(self._on_task_done)
    async def dead_letter(self, event: Event, handler_id: str, error: str) -> None:
        if self.use_fallback:
            await self.fallback.dead_letter(event, handler_id, error)
            return

        logger.error(f"Routing event {event.event_id} to DLQ. Error: {error}")
        key = f"{event.event_id}:{handler_id}"
        await self.dlq.quarantine(event, error, self._retry_counts.get(key, 0))
        dlq_event = Event(
            event_type="system.dead_letter",
            data={
                "failed_event_id": event.event_id,
                "failed_topic": event.event_type,
                "handler_id": handler_id,
                "error": error,
                "payload": event.data
            },
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            parent_request_id=event.parent_request_id,
            event_version="1.0"
        )
        await self.publish(dlq_event)
        if key in self._retry_counts:
            del self._retry_counts[key]

    async def _redis_poller(self) -> None:
        while self._running:
            try:
                # Read new messages from the stream
                response = await self.client.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={self.stream_key: ">"},
                    count=10,
                    block=1000
                )
                if not response:
                    continue

                for stream, messages in response:
                    for msg_id, fields in messages:
                        payload_str = fields.get("payload")
                        if not payload_str:
                            continue

                        payload = json.loads(payload_str)
                        event = Event(
                            event_type=payload["event_type"],
                            data=payload["data"],
                            event_id=payload["event_id"],
                            timestamp=datetime.fromisoformat(payload["timestamp"]),
                            source=payload["source"],
                            correlation_id=payload["correlation_id"],
                            causation_id=payload["causation_id"],
                            parent_request_id=payload["parent_request_id"],
                            event_version=payload.get("event_version", "1.0"),
                        )

                        # Dispatch event locally
                        await self._dispatch_local(event, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Redis poller loop: {e}")
                await asyncio.sleep(1.0)

    async def _dispatch_local(self, event: Event, msg_id: str) -> None:
        # Check feature flag
        enable_safe_ack = True
        if hasattr(self, "settings") and self.settings:
            enable_safe_ack = getattr(self.settings, "ENABLE_SAFE_REDIS_ACK", True)
        elif hasattr(self.fallback, "settings") and self.fallback.settings:
            enable_safe_ack = getattr(self.fallback.settings, "ENABLE_SAFE_REDIS_ACK", True)

        if not enable_safe_ack:
            # Fallback path (legacy / unsafe)
            for handler in list(self._local_handlers):
                if handler.matches(event):
                    try:
                        if handler.is_async:
                            await handler.callback(event)
                        else:
                            handler.callback(event)
                        await self.client.xack(self.stream_key, self.group_name, msg_id)
                        await self.acknowledge(event.event_id, handler.handler_id)
                    except Exception as e:
                        logger.error(f"Local handler failed processing Redis event: {e}")
                        await self.retry(event, handler.handler_id)
            return

        # Hardened path: safe xack-after-all-handlers and per-handler idempotency
        if not hasattr(self, "_processed_handler_events"):
            self._processed_handler_events = {}

        all_succeeded = True
        matched_any = False

        for handler in list(self._local_handlers):
            if handler.matches(event):
                matched_any = True
                handler_id = handler.handler_id

                if handler_id not in self._processed_handler_events:
                    self._processed_handler_events[handler_id] = set()

                # Per-handler idempotency check
                if event.event_id in self._processed_handler_events[handler_id]:
                    logger.debug(f"[REDIS-ACK] Skipping already processed event {event.event_id} for handler {handler_id}")
                    continue

                try:
                    if handler.is_async:
                        await handler.callback(event)
                    else:
                        handler.callback(event)

                    # Track successful execution to prevent duplicate mutation
                    self._processed_handler_events[handler_id].add(event.event_id)
                    if len(self._processed_handler_events[handler_id]) > 5000:
                        # Slice to keep memory footprint bounded
                        self._processed_handler_events[handler_id] = set(list(self._processed_handler_events[handler_id])[-4000:])

                    await self.acknowledge(event.event_id, handler_id)
                except Exception as e:
                    all_succeeded = False
                    logger.error(f"Local handler {handler_id} failed processing Redis event: {e}", exc_info=True)
                    event.failed_handler_ids.add(handler_id)
                    await self.retry(event, handler_id)

        if matched_any:
            if all_succeeded:
                await self.client.xack(self.stream_key, self.group_name, msg_id)
                logger.debug(f"[REDIS-ACK] Acknowledged message {msg_id} for event {event.event_id}")
            else:
                logger.warning(f"[REDIS-ACK] Partial failure. Postponing xack for message {msg_id} until all handlers succeed.")
        else:
            # If no local handlers matched, acknowledge to avoid message leaks in the stream
            await self.client.xack(self.stream_key, self.group_name, msg_id)


# Backward compatibility alias
class EventBus(InProcessEventBus):
    """Facilitates backward compatibility by proxying InProcessEventBus."""
    pass