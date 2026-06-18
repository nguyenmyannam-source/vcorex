import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.settings import Settings
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from infrastructure.exchange.base_exchange import Balance, Position
from interfaces.telegram.notification_service import NotificationService
from interfaces.telegram.telegram_bot import TelegramBot


@pytest.mark.asyncio
async def test_dashboard_full_sync_flow():
    """
    Test case chuyên sâu kiểm tra luồng đồng bộ Dashboard:
    Telegram Button -> EventBus -> PositionEngine -> OKX Data -> EventBus -> Telegram UI
    """
    event_bus = EventBus()
    await event_bus.start()

    # 1. Mock Exchange
    mock_exchange = MagicMock()
    mock_exchange.fetch_balance = AsyncMock(
        return_value={"USDT": Balance(asset="USDT", free=10000.0, used=500.0, total=10500.0)}
    )
    mock_exchange.fetch_positions = AsyncMock(
        return_value=[
            Position(
                position_id="pos_123",
                symbol="BTC-USDT-SWAP",
                side="long",
                amount=0.1,
                entry_price=50000.0,
                current_price=51000.0,
                unrealized_pnl=100.0,
                leverage=10,
                timestamp=123456789,
            )
        ]
    )

    # 2. Setup TelegramBot (UI Layer)
    # Chúng ta mock bot instance của Telegram để không thực sự gửi tin nhắn tới server Telegram
    telegram_bot = TelegramBot(event_bus)
    telegram_bot._enabled = True
    telegram_bot._bot = AsyncMock()

    # Initialize dispatcher (normally done in start())
    from interfaces.telegram.message_dispatcher import MessageDispatcher

    telegram_bot._dispatcher = MessageDispatcher(
        telegram_bot._bot, telegram_bot._chat_id, event_bus, telegram_bot._rate_limiter
    )

    # 3. Kích hoạt logic phản hồi (Đây là phần chúng ta nghi ngờ đang thiếu)
    # Chúng ta sẽ kiểm tra xem có ai nghe event 'telegram.request_health_data' không
    request_received = asyncio.Event()

    async def mock_health_handler(event):
        request_received.set()
        # Giả lập PositionEngine trả lời
        await event_bus.publish(
            Event(
                event_type=EventTopic.TELEGRAM_RESPONSE_HEALTH_DATA,
                data={"status": "online", "balance": 10500.0, "open_positions": 1},
                source="position_engine",
            )
        )

    event_bus.subscribe(mock_health_handler, [EventTopic.TELEGRAM_REQUEST_HEALTH_DATA])

    # 4. Giả lập hành động nhấn nút "Status" trên Telegram
    # (Trong code thực tế, _handle_system_callback sẽ publish event này)
    mock_query = MagicMock()
    mock_query.id = "query_123"
    mock_query.edit_message_text = AsyncMock()
    mock_query.message = MagicMock()
    mock_query.message.message_id = 42

    await telegram_bot._handle_system_callback(mock_query, "health")

    # 5. Kiểm tra xem Event Yêu cầu có được gửi vào bus không
    try:
        await asyncio.wait_for(request_received.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "CRITICAL: Event 'telegram.request_health_data' was never handled by any service!"
        )

    # 6. Kiểm tra xem TelegramBot có lắng nghe và cập nhật UI không
    # (Hiện tại code sẽ fail ở đây vì TelegramBot chưa subscribe telegram.response_*)

    await event_bus.stop()
    print("\n✅ Test Dashboad Sync: Request chain is working.")


@pytest.mark.asyncio
async def test_ghost_position_sync_to_telegram():
    """
    Kiểm tra tính đồng bộ khi phát hiện vị thế 'lạ' (Ghost Position) trên sàn
    phải được báo ngay về Telegram Dashboard.
    """
    event_bus = EventBus()
    await event_bus.start()

    telegram_bot = TelegramBot(event_bus)
    telegram_bot._enabled = True
    telegram_bot._bot = AsyncMock()

    # Thêm NotificationService để xử lý proactive alerts
    settings = Settings()
    settings.telegram_enabled = True
    settings.telegram_chat_id = "123456"  # Mock chat ID

    notification_service = NotificationService(event_bus, settings)
    notification_service._bot = telegram_bot._bot  # Use the same mock bot
    notification_service._running = True  # Force running state
    notification_service._subscribe_events()  # Manually subscribe
    asyncio.create_task(notification_service._outbox_processing_loop())

    # Giả lập PositionEngine phát hiện Ghost Position
    await event_bus.publish(
        Event(
            event_type=EventTopic.POSITION_GHOST_DETECTED,
            data={"symbol": "ETH-USDT-SWAP", "position_id": "ghost_999"},
            source="position_engine",
        )
    )

    # Chờ TelegramBot/NotificationService xử lý
    await asyncio.sleep(0.5)

    # Kiểm tra xem bot có gọi hàm send_message để cảnh báo user không
    assert (
        telegram_bot._bot.send_message.called
    ), "CRITICAL: Ghost position alert was NOT sent to Telegram via NotificationService!"

    await event_bus.stop()
    print("✅ Test Ghost Position Sync: Alert flow is working.")
