import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from infrastructure.exchange.okx_exchange import OKXExchange
from services.market_data_engine import MarketDataEngine
from services.position.exchange_mirror import ExchangeMirrorCache
from core.config.settings import Settings
from core.event_bus import EventBus, Event
from core.events.topics import EventTopic
from core.exceptions import OKXAPIError

@pytest.fixture
def mock_settings():
    settings = Settings(
        okx_api_key="test",
        okx_api_secret="test",
        okx_passphrase="test",
        okx_demo_mode=True,
    )
    return settings

@pytest.fixture
def event_bus():
    return EventBus()

@pytest.mark.asyncio
async def test_okx_request_raw_exponential_backoff(mock_settings, event_bus):
    """Kiểm tra _request_raw tự động đợi theo cơ chế Exponential Backoff + Jitter"""
    exchange = OKXExchange(mock_settings, event_bus)
    exchange.session = MagicMock()

    # Mock phản hồi 429 Too Many Requests từ OKX API
    mock_response = AsyncMock()
    mock_response.status = 429
    mock_response.headers = {}
    mock_response.text.return_value = '{"code": "50011", "msg": "Too Many Requests"}'

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    exchange.session.request.return_value = mock_context_manager

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        try:
            await exchange._request_raw("GET", "/api/v5/market/ticker", {"instId": "BTC-USDT-SWAP"})
        except OKXAPIError:
            pass # Expected sau khi vượt quá max_retries (5)

        # Filter out pre-emptive desync jitters which are < 0.1s
        backoff_delays = [call.args[0] for call in mock_sleep.call_args_list if call.args[0] >= 0.1]
        print(f"\n[Test] Backoff Delays: {backoff_delays}")

        assert len(backoff_delays) >= 4, f"Expected at least 4 backoff retries, got {len(backoff_delays)}"

        # Verify base_delay=0.5, max_delay=8.0 with 1.5 multiplier + Jitter
        # attempt 0: 0.5 * 1.5^0 = 0.5 -> 0.5 to 0.6 (jitter)
        assert 0.5 <= backoff_delays[0] <= 0.6
        # attempt 1: 0.5 * 1.5^1 = 0.75 -> 0.75 to 0.85
        assert 0.75 <= backoff_delays[1] <= 0.85
        # attempt 2: 0.5 * 1.5^2 = 1.125 -> 1.125 to 1.225
        assert 1.125 <= backoff_delays[2] <= 1.25
        # attempt 3: 0.5 * 1.5^3 = 1.6875 -> 1.6875 to 1.8
        assert 1.68 <= backoff_delays[3] <= 1.8


@pytest.mark.asyncio
async def test_okx_demo_mode_public_request_adds_simulated_trading_header(mock_settings, event_bus):
    """Kiểm tra demo public request thêm x-simulated-trading vào header."""
    exchange = OKXExchange(mock_settings, event_bus)
    exchange.session = MagicMock()
    exchange.session = MagicMock()

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.text.return_value = '{"code":"0","data":[]}'

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    exchange.session.request.return_value = mock_context_manager

    with patch("asyncio.sleep", new_callable=AsyncMock):
        response = await exchange._request_raw(
            "GET",
            "/api/v5/public/time",
            {"instId": "BTC-USDT-SWAP"},
            auth_required=False,
        )

    exchange.session.request.assert_called_once()
    request_args = exchange.session.request.call_args
    headers = request_args.kwargs["headers"]

    assert headers["x-simulated-trading"] == "1"
    assert headers["Content-Type"] == "application/json"
    assert response == {"code": "0", "data": []}


@pytest.mark.asyncio
async def test_okx_demo_mode_verification_allows_numeric_uid(mock_settings, event_bus):
    """Kiểm tra demo verification không fail khi OKX demo trả về UID numeric."""
    exchange = OKXExchange(mock_settings, event_bus)
    exchange._cached_account_config = {"uid": "682651107994596407"}

    # Nếu OKX demo hiện nay dùng numeric UIDs, bot vẫn nên tiếp tục chạy demo mode.
    await exchange._verify_demo_mode_on_startup()


@pytest.mark.asyncio
async def test_market_data_engine_semaphore_concurrency(mock_settings, event_bus):
    """Kiểm tra Semaphore(5) giới hạn số lượng request chạy song song"""
    exchange = AsyncMock()

    active_fetches = 0
    max_active_fetches = 0

    async def mock_fetch_ohlcv(*args, **kwargs):
        nonlocal active_fetches, max_active_fetches
        active_fetches += 1
        max_active_fetches = max(max_active_fetches, active_fetches)
        await asyncio.sleep(0.05) # Giả lập delay mạng
        active_fetches -= 1
        return []

    exchange.fetch_ohlcv = mock_fetch_ohlcv
    mde = MarketDataEngine(exchange, event_bus, mock_settings)

    # Bắn 20 requests đồng loạt
    print(f"\n[Test] Sending 20 concurrent fetch requests...")
    tasks = [mde._fetch_latest_candle(f"COIN{i}", "5m") for i in range(20)]
    await asyncio.gather(*tasks)

    print(f"[Test] Max concurrent fetches observed: {max_active_fetches}")
    assert max_active_fetches == 5, f"Concurrency limit broken! Max was {max_active_fetches}"


@pytest.mark.asyncio
async def test_exchange_mirror_reconnect_cooldown(mock_settings, event_bus):
    """Kiểm tra cơ chế Cool-down đợi 3 giây sau khi WebSocket Reconnect"""
    exchange = OKXExchange(mock_settings, event_bus)
    mirror = ExchangeMirrorCache(event_bus, exchange)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Patch _run_atomic_resync để không chạy thực tế
        with patch.object(mirror, "_run_atomic_resync", new_callable=AsyncMock) as mock_resync:
            print("\n[Test] Triggering Reconnect event...")
            await mirror._handle_ws_reconnect(Event(EventTopic.WS_RECONNECTED, {}))

            # Debounce chạy trong background task — cần await để kiểm tra sleep(2)
            if mirror._resync_task is not None:
                await mirror._resync_task

            # Kiểm tra xem sleep(2) có được gọi không
            mock_sleep.assert_any_call(2)
            print("[Test] Verified 2s cool-down before atomic resync.")
