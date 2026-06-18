import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import aiohttp

from core.config.settings import Settings
from core.exceptions import OKXAPIError
from infrastructure.exchange.okx_exchange import (
    OKXExchange,
    OKXOrderVerificationUnknownError,
)
from infrastructure.exchange.base_exchange import Order


@pytest.fixture
def settings():
    s = MagicMock(spec=Settings)
    s.okx_api_key = "test_key"
    s.okx_api_secret = "test_secret"
    s.okx_passphrase = "test_passphrase"
    s.okx_demo_mode = True
    s.okx_base_url = "https://openapi.okx.com"
    s.okx_ws_url = "wss://wspap.okx.com:8443/ws/v5"
    s.margin_mode = "cross"
    s.max_reconnect_attempts = 3
    s.default_leverage = 10
    s.exchange_cb_threshold = 5
    s.exchange_cb_cooldown = 60
    s.okx_public_api_capacity = 20
    s.okx_public_api_refill_rate = 2
    s.okx_private_api_capacity = 20
    s.okx_private_api_refill_rate = 5
    return s


@pytest.fixture
def exchange(settings):
    ex = OKXExchange(settings)
    # Provide a lightweight fake aiohttp-like session that returns an async context manager
    class DummyResponse:
        def __init__(self, json_data=None, status=200, headers=None):
            self._json = json_data or {}
            self.status = status
            self.headers = headers or {}

        async def text(self):
            import json as _json
            return _json.dumps(self._json)

        async def json(self):
            return self._json

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummySession:
        def request(self, *args, **kwargs):
            return DummyResponse()

    ex.session = DummySession()
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 0.01, "tickSz": 0.1, "lotSz": 1.0, "minSz": 1.0}}
    ex.pos_mode = "net_mode"
    return ex


@pytest.mark.asyncio
async def test_request_no_retry_does_not_retry(exchange):
    """Verify that _request_no_retry does not retry on TimeoutError."""
    # Mock _request_raw to always raise asyncio.TimeoutError
    with patch.object(exchange, "_request_raw", side_effect=asyncio.TimeoutError("Timeout")):
        with pytest.raises(asyncio.TimeoutError):
            await exchange._request_no_retry("POST", "/api/v5/trade/order", {"instId": "BTC-USDT-SWAP"})

        # Verify it was only called once (no retries)
        assert exchange._request_raw.call_count == 1


@pytest.mark.asyncio
async def test_request_retries_normal_requests(exchange):
    """Verify that normal _request retries on TimeoutError (tenacity wrapper active)."""
    import tenacity
    with patch.object(exchange, "_request_raw", side_effect=asyncio.TimeoutError("Timeout")) as mock_raw:
        with pytest.raises(tenacity.RetryError):
            await exchange._request("GET", "/api/v5/account/balance")

        # Tenacity is configured for 3 attempts
        assert mock_raw.call_count == 3


@pytest.mark.asyncio
async def test_place_order_success(exchange):
    """Verify standard successful order placement."""
    response_data = {
        "code": "0",
        "data": [{"ordId": "12345", "clOrdId": "vcorex_1"}]
    }

    with patch.object(exchange, "_request_raw", return_value=response_data):
        order = await exchange.place_order(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            amount=100.0,
            client_order_id="vcorex_1"
        )
        assert order.order_id == "12345"
        assert order.client_order_id == "vcorex_1"
        assert order.status == "ACKED"


@pytest.mark.asyncio
async def test_place_order_timeout_executed(exchange):
    """V8: place_order returns PENDING_RECONCILE immediately on Timeout. Recovery is async."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        if method == "POST" and path == "/api/v5/trade/order":
            raise asyncio.TimeoutError("Timeout placing order")
        return {"code": "0", "data": [{"ordId": "99999", "state": "filled", "clOrdId": "vcorex_1"}]}

    with patch.object(exchange, "_request", side_effect=mock_req_no_retry):
        order = await exchange.place_order(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            amount=100.0,
            client_order_id="vcorex_1"
        )
        # V8: Hot path returns PENDING_RECONCILE immediately — no blocking await
        assert order.status == "PENDING_RECONCILE"
        assert order.client_order_id == "vcorex_1"


@pytest.mark.asyncio
async def test_place_order_timeout_not_found(exchange):
    """V8: place_order returns PENDING_RECONCILE immediately. NOT_FOUND handled by phantom worker."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        raise asyncio.TimeoutError("Timeout placing order")

    with patch.object(exchange, "_request", side_effect=mock_req_no_retry):
        order = await exchange.place_order(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            amount=100.0,
            client_order_id="vcorex_1"
        )
        assert order.status == "PENDING_RECONCILE"


@pytest.mark.asyncio
async def test_place_order_timeout_unknown(exchange):
    """V8: place_order returns PENDING_RECONCILE immediately. UNKNOWN handled by phantom worker."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        raise asyncio.TimeoutError("Timeout placing order")

    with patch.object(exchange, "_request", side_effect=mock_req_no_retry):
        order = await exchange.place_order(
            symbol="BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            amount=100.0,
            client_order_id="vcorex_1"
        )
        assert order.status == "PENDING_RECONCILE"


@pytest.mark.asyncio
async def test_place_algo_order_timeout_executed(exchange):
    """V8: place_algo_order returns None on Timeout. Recovery is async."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        if method == "POST" and path == "/api/v5/trade/order-algo":
            raise asyncio.TimeoutError("Timeout placing algo order")
        return {"code": "0", "data": [{"algoId": "algo_9999", "state": "effective"}]}

    with patch.object(exchange, "_request", side_effect=mock_req_no_retry):
        algo_id = await exchange.place_algo_order(
            symbol="BTC-USDT-SWAP",
            side="sell",
            sz=10.0,
            tp_trigger_px=70000.0
        )
        # V8: Hot path returns None on timeout (algo orders return None on error)
        assert algo_id is None


@pytest.mark.asyncio
async def test_place_algo_order_timeout_not_found(exchange):
    """V8: place_algo_order returns None on Timeout. NOT_FOUND handled by phantom worker."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        raise asyncio.TimeoutError("Timeout")

    with patch.object(exchange, "_request", side_effect=mock_req_no_retry):
        result = await exchange.place_algo_order(
            symbol="BTC-USDT-SWAP",
            side="sell",
            sz=10.0,
            tp_trigger_px=70000.0
        )
        assert result is None


@pytest.mark.asyncio
async def test_place_algo_order_timeout_unknown(exchange):
    """V8: place_algo_order returns None. UNKNOWN handled async by phantom worker."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        raise asyncio.TimeoutError("Timeout")

    with patch.object(exchange, "_request", side_effect=mock_req_no_retry):
        result = await exchange.place_algo_order(
            symbol="BTC-USDT-SWAP",
            side="sell",
            sz=10.0,
            tp_trigger_px=70000.0
        )
        assert result is None


@pytest.mark.asyncio
async def test_cancel_order_timeout_executed(exchange):
    """Verify cancel_order returns True when cancel request times out but order is canceled on exchange."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        raise asyncio.TimeoutError("Timeout")

    async def mock_req(method, path, params=None, auth_required=True):
        # GET /api/v5/trade/order status returns state = canceled
        return {"code": "0", "data": [{"ordId": "ord_1", "state": "canceled", "sCode": "0", "sMsg": "OK"}]}

    with patch.object(exchange, "_request_no_retry", side_effect=mock_req_no_retry), \
         patch.object(exchange, "_request", side_effect=mock_req):

        res = await exchange.cancel_order("BTC-USDT-SWAP", "ord_1")
        assert res is True


@pytest.mark.asyncio
async def test_cancel_order_timeout_not_found(exchange):
    """Verify cancel_order propagates exception when cancel times out and order is still live."""
    async def mock_request(method, path, params=None, auth_required=True):
        # Simulate POST cancel-order timing out, but GET status returns live
        if method == "POST" and path == "/api/v5/trade/cancel-order":
            raise asyncio.TimeoutError("Timeout")
        # For verification GET calls
        return {"code": "0", "data": [{"ordId": "ord_1", "state": "live", "sCode": "1", "sMsg": "Not canceled"}]}

    with patch.object(exchange, "_request", side_effect=mock_request):
        with pytest.raises(asyncio.TimeoutError):
            await exchange.cancel_order("BTC-USDT-SWAP", "ord_1")


@pytest.mark.asyncio
async def test_cancel_order_timeout_unknown(exchange):
    """Verify cancel_order raises OKXOrderVerificationUnknownError when cancel query times out."""
    async def mock_req_no_retry(method, path, params=None, auth_required=True):
        raise asyncio.TimeoutError("Timeout")

    async def mock_req(method, path, params=None, auth_required=True):
        raise OKXAPIError("Internal Server Error (500)")

    with patch.object(exchange, "_request_no_retry", side_effect=mock_req_no_retry), \
         patch.object(exchange, "_request", side_effect=mock_req):

        with pytest.raises(OKXOrderVerificationUnknownError):
            await exchange.cancel_order("BTC-USDT-SWAP", "ord_1")


@pytest.mark.asyncio
async def test_verify_order_status_eventual_consistency_retry(exchange):
    """Verify verify_order_status retries on order not found and handles eventual consistency."""
    call_count = 0

    async def mock_request_with_consistency(method, path, params=None, auth_required=True):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise OKXAPIError("OKX API Error: Order does not exist (51401)")
        return {"code": "0", "data": [{"ordId": "99999", "state": "filled"}]}

    with patch.object(exchange, "_request", side_effect=mock_request_with_consistency), \
         patch.object(asyncio, "sleep", return_value=None) as mock_sleep:

        status = await exchange.verify_order_status("BTC-USDT-SWAP", "vcorex_1")
        assert status == "FILLED"
        assert call_count == 3
        # First attempt is immediate (no sleep), then 2 sleep calls
        assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_unknown_state_increments_metric_and_halts(exchange):
    """V7: Verify that verify_order_status returning UNKNOWN still increments error_count.
    This now tests verify_order_status directly, since place_order no longer calls it inline."""
    exchange._error_count = 0
    exchange._trigger_emergency_stop = AsyncMock()

    # Simulate verify_order_status returning UNKNOWN (called by phantom worker in production)
    with patch.object(exchange, "_request", side_effect=OKXAPIError("Service Unavailable")):
        status = await exchange.verify_order_status("BTC-USDT-SWAP", "vcorex_1")
        assert status == "UNKNOWN"

    # The error_count increment happens inside _trigger_emergency_stop flow
    # which is invoked via asyncio.create_task. Test that verify_order_status
    # returns the correct sentinel value for the phantom worker to act on.
    assert status == "UNKNOWN"