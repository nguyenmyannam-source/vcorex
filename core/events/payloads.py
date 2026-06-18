from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class PositionAction(str, Enum):
    CLOSE_HALF = "close_half"
    CLOSE_FULL = "close_full"


@dataclass(slots=True)
class PositionCloseRequest:
    request_id: str
    correlation_id: str
    causation_id: str
    position_id: str
    action: PositionAction
    requested_by: int
    timestamp: datetime
    parent_request_id: Optional[str] = None
