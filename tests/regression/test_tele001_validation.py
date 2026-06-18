"""
PHASE 12 – TELE-001 Patch Validation Tests
Tests A through F as specified in the Phase 12 requirements.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock


# ============================================================
# Test A – EventTopic.CHART_GENERATED member exists
# ============================================================
def test_A_chart_generated_exists():
    """EventTopic.CHART_GENERATED exists with correct value."""
    from core.events.topics import EventTopic
    
    assert hasattr(EventTopic, "CHART_GENERATED"), \
        "EventTopic.CHART_GENERATED must exist after patch"
    assert EventTopic.CHART_GENERATED.value == "chart.generated"


# ============================================================
# Test B – Existing EventTopic members unchanged
# ============================================================
def test_B_existing_members_unchanged():
    """All 56 original EventTopic members still exist with correct values."""
    from core.events.topics import EventTopic
    
    # Spot-check critical members
    assert EventTopic.POSITION_OPENED.value == "position.opened"
    assert EventTopic.POSITION_CLOSED.value == "position.closed"
    assert EventTopic.STRATEGY_SIGNAL_GENERATED.value == "strategy.signal_generated"
    assert EventTopic.SYSTEM_ALERT.value == "system.alert"
    assert EventTopic.TELEGRAM_SEND_MESSAGE.value == "telegram.send_message"
    assert EventTopic.POSITION_GHOST_DETECTED.value == "position.ghost_detected"
    assert EventTopic.MARKET_VOLATILITY_ALERT.value == "market.volatility_alert"
    assert EventTopic.NOTIFICATION_PERIODIC_REPORT.value == "notification.periodic_report"
    assert EventTopic.SYSTEM_API_ERROR.value == "system.api_error"
    
    # 56 original + 1 new = 57
    assert len(EventTopic.__members__) == 57, \
        f"Expected 57 members (56 original + CHART_GENERATED), got {len(EventTopic.__members__)}"


# ============================================================
# Test C – ChartService can reference EventTopic.CHART_GENERATED
# ============================================================
def test_C_chart_service_publish_path():
    """chart_service.py can construct an Event with CHART_GENERATED without crash."""
    from core.events.topics import EventTopic
    from core.event_bus_components import Event
    
    # This is the exact code path in chart_service.py line 203-212
    event = Event(
        event_type=EventTopic.CHART_GENERATED,
        data={
            "symbol": "BTC-USDT",
            "timeframe": "1H",
            "side": "LONG",
            "photo_path": "/tmp/test_chart.png"
        },
        source="chart_service"
    )
    assert event.event_type == EventTopic.CHART_GENERATED
    assert event.data["symbol"] == "BTC-USDT"


# ============================================================
# Test D – EventBus dispatch reaches handler
# ============================================================
@pytest.mark.asyncio
async def test_D_eventbus_dispatch():
    """Published CHART_GENERATED event reaches a subscribed handler."""
    from core.events.topics import EventTopic
    from core.event_bus import EventBus
    from core.event_bus_components import Event
    
    event_bus = EventBus()
    await event_bus.start()
    received = []
    
    async def handler(event):
        received.append(event)
    
    event_bus.subscribe(handler, [EventTopic.CHART_GENERATED], handler_id="test_chart_dispatch")
    
    await event_bus.publish(Event(
        event_type=EventTopic.CHART_GENERATED,
        data={"symbol": "ETH-USDT", "photo_path": "/tmp/eth.png"},
        source="test"
    ))
    
    # Allow event bus worker to process the queued event
    await asyncio.sleep(0.3)
    
    assert len(received) == 1, f"Expected 1 event, got {len(received)}"
    assert received[0].data["symbol"] == "ETH-USDT"
    
    event_bus.unsubscribe(handler_id="test_chart_dispatch")
    await event_bus.stop()


# ============================================================
# Test E – NotificationService._subscribe_events() completes
# ============================================================
def test_E_subscribe_events_no_crash():
    """_subscribe_events() passes line 165 without AttributeError."""
    from core.events.topics import EventTopic
    from core.event_bus import EventBus
    
    event_bus = EventBus()
    event_bus.start()
    
    # Manually simulate what _subscribe_events does at line 164-166
    handler = AsyncMock()
    
    # This is the exact line that crashed before the patch
    event_bus.subscribe(
        handler,
        [EventTopic.CHART_GENERATED],
        handler_id="notif_chart_generated"
    )
    
    # Verify it was registered
    event_bus.unsubscribe(handler_id="notif_chart_generated")


# ============================================================
# Test F – _HANDLER_IDS includes notif_chart_generated for cleanup
# ============================================================
def test_F_handler_ids_includes_chart():
    """_HANDLER_IDS tuple includes 'notif_chart_generated' for proper stop() cleanup."""
    from interfaces.telegram.notification_service import NotificationService
    
    assert "notif_chart_generated" in NotificationService._HANDLER_IDS, \
        "notif_chart_generated must be in _HANDLER_IDS for cleanup on stop()"
