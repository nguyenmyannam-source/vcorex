"""
Dead Letter Queue (DLQ) module for quarantining and recovering from event processing failures.
"""

import json
from datetime import datetime, timezone
from loguru import logger
from sqlalchemy import select, update

from core.event_bus_components import Event
from core.metrics import MetricsAdapter
from infrastructure.storage.database import DeadLetterEvent, get_session


class DeadLetterQueue:
    """Manages event quarantine, failure analysis, and retry processing."""

    def __init__(self, metrics: MetricsAdapter):
        self.metrics = metrics
        # [ISSUE #4 FIX] Fallback in-memory queue for when DB persistence fails
        self._fallback_queue = []
        self._db_persistence_failed_count = 0
        self._max_fallback_size = 1000

    def classify_failure(self, error_msg: str) -> str:
        """Classify failure as either structural or transient."""
        err_lower = error_msg.lower()
        # Structural issues (un-retryable without code fix)
        structural_indicators = [
            "keyerror", "valueerror", "typeerror", "attributeerror",
            "jsondecodeerror", "serializationerror", "invalid format"
        ]
        for indicator in structural_indicators:
            if indicator in err_lower:
                return "structural"
        return "transient"

    async def quarantine(self, event: Event, error: str, retry_count: int = 0) -> None:
        """Move a failed event into the DB-backed quarantine storage."""
        await self.metrics.increment_dlq_event()
        failure_type = self.classify_failure(error)
        logger.warning(
            f"Quarantining event {event.event_id} ({event.event_type}). "
            f"Type: {failure_type}. Error: {error}"
        )

        # Handle poison detection
        if retry_count > 5:
            await self.metrics.increment_poison_event()
            logger.error(f"POISON MESSAGE DETECTED: Event {event.event_id} has failed {retry_count} times.")

        payload_dict = {
            "event_type": event.event_type,
            "data": event.data,
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "source": event.source,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "parent_request_id": event.parent_request_id,
            "event_version": event.event_version,
        }

        try:
            async with get_session() as session:
                async with session.begin():
                    # [BUGFIX] Robust JSON serializer for custom dataclasses like OHLCV
                    def _fallback_serializer(obj):
                        try:
                            import dataclasses
                            if dataclasses.is_dataclass(obj):
                                return dataclasses.asdict(obj)
                            if hasattr(obj, "to_dict"):
                                return obj.to_dict()
                            if hasattr(obj, "__dict__"):
                                return obj.__dict__
                        except Exception:
                            pass
                        return str(obj)

                    dlq_record = DeadLetterEvent(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        payload=json.dumps(payload_dict, sort_keys=True, default=_fallback_serializer),
                        error=error,
                        retry_count=retry_count,
                        quarantined=True
                    )
                    session.add(dlq_record)
        except Exception as ex:
            # [ISSUE #4 FIX] Add fallback in-memory queue when DB persistence fails
            self._db_persistence_failed_count += 1
            logger.error(
                f"[DLQ-PERSISTENCE-FAILURE #{self._db_persistence_failed_count}] "
                f"Failed to persist DLQ record for event {event.event_id}: {ex}. "
                f"Using fallback in-memory queue."
            )

            # Store event in fallback queue for eventual retry
            if len(self._fallback_queue) < self._max_fallback_size:
                self._fallback_queue.append({
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "data": event.data,
                    "error": error,
                    "retry_count": retry_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "dlq_fallback"
                })
                logger.warning(
                    f"[DLQ-FALLBACK] Event {event.event_id} stored in fallback queue. "
                    f"Queue size: {len(self._fallback_queue)}/{self._max_fallback_size}"
                )
            else:
                logger.critical(
                    f"[DLQ-OVERFLOW] Fallback queue is FULL! Event {event.event_id} will be LOST! "
                    f"DB persistence must be restored immediately."
                )

            # Alert if persistence failures are accumulating
            if self._db_persistence_failed_count % 10 == 0:
                logger.critical(
                    f"[DLQ-ALERT] {self._db_persistence_failed_count} persistence failures detected. "
                    f"System may lose event recovery capability!"
                )

            # Do not re-raise; allow caller to continue even when persistent storage fails

    async def replay_event(self, event_id: str, event_bus) -> bool:
        """Retrieve a quarantined event and publish it back to the event bus."""
        logger.info(f"Replaying quarantined event: {event_id}")
        await self.metrics.increment_retry_attempts()

        async with get_session() as session:
            async with session.begin():
                stmt = select(DeadLetterEvent).where(
                    DeadLetterEvent.event_id == event_id,
                    DeadLetterEvent.quarantined == True
                )
                result = await session.execute(stmt)
                dlq_record = result.scalars().first()
                if not dlq_record:
                    logger.warning(f"Quarantined event {event_id} not found.")
                    return False

                payload = json.loads(dlq_record.payload)
                # Reconstruct Event
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

                # Un-quarantine in DB
                dlq_record.quarantined = False

                # Publish back to event bus
                await event_bus.publish(event)
                logger.info(f"Successfully replayed event {event_id} to EventBus.")
                return True
        return False

    async def drain_fallback_queue(self) -> int:
        """[ISSUE #4 FIX] Drain fallback in-memory queue to DB when persistence recovers."""
        if not self._fallback_queue:
            return 0

        drained_count = 0
        failed_to_drain = []

        logger.info(f"[DLQ-DRAIN] Attempting to drain {len(self._fallback_queue)} events from fallback queue...")

        for fallback_event in list(self._fallback_queue):
            try:
                async with get_session() as session:
                    async with session.begin():
                        dlq_record = DeadLetterEvent(
                            event_id=fallback_event["event_id"],
                            event_type=fallback_event["event_type"],
                            payload=json.dumps(fallback_event, sort_keys=True),
                            error=fallback_event.get("error", "unknown"),
                            retry_count=fallback_event.get("retry_count", 0),
                            quarantined=True
                        )
                        session.add(dlq_record)
                        self._fallback_queue.remove(fallback_event)
                        drained_count += 1
            except Exception as drain_err:
                logger.error(f"[DLQ-DRAIN-FAILED] Could not drain event {fallback_event['event_id']}: {drain_err}")
                failed_to_drain.append(fallback_event["event_id"])

        logger.info(
            f"[DLQ-DRAIN-COMPLETE] Drained {drained_count} events from fallback queue. "
            f"Remaining: {len(self._fallback_queue)}, Failed: {len(failed_to_drain)}"
        )
        return drained_count
