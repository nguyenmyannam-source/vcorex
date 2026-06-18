"""
Decoupled AuditSubscriber for VCOREX.
Listens to all system events via EventBus and pushes them to the AuditJournal.
"""

from loguru import logger
from core.event_bus_components import Event
from core.event_bus import EventBus
from core.events.topics import EventTopic
from core.audit_journal import AuditJournal


class AuditSubscriber:
    """
    Subscribes to all EventBus topics to extract and log events into the AuditJournal.
    Decoupled from EventBus transport logic.
    """

    def __init__(self, event_bus: EventBus, audit_journal: AuditJournal):
        self.event_bus = event_bus
        self.audit_journal = audit_journal
        self._handler_id = "audit_subscriber_handler"

    def start(self) -> None:
        """Subscribe to all EventTopic enum values."""
        topics = [topic.value for topic in EventTopic]
        self.event_bus.subscribe(
            self.handle_event,
            topics,
            handler_id=self._handler_id
        )
        logger.info(f"AuditSubscriber successfully subscribed to {len(topics)} topics")

    def stop(self) -> None:
        """Unsubscribe from the EventBus."""
        self.event_bus.unsubscribe(self._handler_id)
        logger.info("AuditSubscriber unsubscribed from EventBus")

    async def handle_event(self, event: Event) -> None:
        """Receive published events and route to AuditJournal queue."""
        # Determine actor source
        actor = event.source if event.source else "unknown"

        # Safe payload representation extraction
        payload = event.data

        # Determine request ID from dict or object payload if present
        request_id = None
        if isinstance(payload, dict):
            request_id = payload.get("request_id")
        elif hasattr(payload, "request_id"):
            request_id = getattr(payload, "request_id")

        self.audit_journal.log_event(
            event_id=event.event_id,
            request_id=request_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            parent_request_id=event.parent_request_id,
            event_type=event.event_type,
            payload=payload,
            actor=actor,
            event_version=event.event_version,
            timestamp=event.timestamp
        )
