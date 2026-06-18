"""
Lightweight components for EventBus: Event dataclass and EventHandler wrapper.
These are extracted to keep `core.event_bus` focused on runtime behavior.
"""

import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set
from uuid import uuid4


@dataclass
class Event:
    event_type: str
    data: Any  # Can be dict, dataclass or other payload types
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    parent_request_id: Optional[str] = None
    event_version: str = "1.0"
    successful_handler_ids: Set[str] = field(default_factory=set)
    failed_handler_ids: Set[str] = field(default_factory=set)
    retry_count: int = 0
    first_failure_timestamp: Optional[datetime] = None

    def __post_init__(self):
        if not self.source:
            self.source = "unknown"
        # Auto-extract tracing fields from standard dict or object payload
        if isinstance(self.data, dict):
            if not self.correlation_id:
                self.correlation_id = self.data.get("correlation_id")
            if not self.causation_id:
                self.causation_id = self.data.get("causation_id")
            if not self.parent_request_id:
                self.parent_request_id = self.data.get("parent_request_id")
            if "event_version" in self.data:
                self.event_version = self.data["event_version"]
        else:
            if not self.correlation_id and hasattr(self.data, "correlation_id"):
                self.correlation_id = getattr(self.data, "correlation_id")
            if not self.causation_id and hasattr(self.data, "causation_id"):
                self.causation_id = getattr(self.data, "causation_id")
            if not self.parent_request_id and hasattr(self.data, "parent_request_id"):
                self.parent_request_id = getattr(self.data, "parent_request_id")
            if hasattr(self.data, "event_version"):
                self.event_version = getattr(self.data, "event_version") or "1.0"


class EventHandler:
    def __init__(
        self,
        callback: Callable[[Event], Any],
        event_types: Set[str],
        handler_id: str,
        filter_func: Optional[Callable[[Event], bool]] = None,
    ):
        self.callback = callback
        self.event_types = event_types
        self.handler_id = handler_id
        self.filter_func = filter_func
        self.is_async = inspect.iscoroutinefunction(callback)

    def matches(self, event: Event) -> bool:
        if event.event_type not in self.event_types:
            return False
        return not (self.filter_func and not self.filter_func(event))
