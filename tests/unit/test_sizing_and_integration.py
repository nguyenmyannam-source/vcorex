import asyncio
import json
import math
import time
import websockets
from unittest.mock import AsyncMock

import pytest
from types import SimpleNamespace

from core.config.settings import settings
from core.events.topics import EventTopic
from core.exceptions import OKXAPIError
from infrastructure.exchange.base_exchange import Order
from infrastructure.exchange.okx_exchange import OKXExchange
from services.position.order_handler import OrderHandler
from services.position.models import PositionStatus, TrackedPosition


@pytest.mark.asyncio
async def test_place_order_sizing_quantization_and_min_size():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    # Use realistic contract value and lot size
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": 0.5, "minSz": 1.0}}

    # Patch network call to return success
    async def mock_request(method, path, params=None, auth_required=True):
        return {"code": "0", "data": [{"ordId": "ord_ok", "clOrdId": params.get("clOrdId")} ]}

    ex._request = mock_request

    # Amount that yields raw_contracts = 1.5 -> quantized to lotSz multiple (1.5) -> but final int() truncates to 1
    order = await ex.place_order(symbol="BTC-USDT-SWAP", side="buy", order_type="market", amount=1500.0, client_order_id="c1")
    assert math.isclose(order.contracts, 1.5)

    # Amount below min contracts should raise ValueError
    with pytest.raises(ValueError):
        await ex.place_order(symbol="BTC-USDT-SWAP", side="buy", order_type="market", amount=500.0, client_order_id="c2")


@pytest.mark.asyncio
async def test_place_algo_order_quantization_behavior():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": 0.1, "minSz": 0.5}}

    # Return algo response
    async def mock_request(method, path, params=None, auth_required=True):
        return {"code": "0", "data": [{"algoId": "alg_1"}]}

    ex._request = mock_request

    # sz below min_sz -> ValueError
    with pytest.raises(ValueError):
        await ex.place_algo_order(symbol="BTC-USDT-SWAP", side="sell", sz=0.49, tp_trigger_px=70000.0)

    # sz above min and fractional -> quantize down to 1.2 -> returns algo id
    algo_id = await ex.place_algo_order(symbol="BTC-USDT-SWAP", side="sell", sz=1.23, tp_trigger_px=70000.0)
    assert algo_id == "alg_1"


@pytest.mark.asyncio
async def test_order_handler_ws_fill_triggers_tp_dispatch():
    # Dummy OKX client that records algo placements
    class DummyOKX:
        def __init__(self):
            self.placed = []

        async def place_algo_order(self, symbol, side, sz, tp_trigger_px=None, sl_trigger_px=None, reduce_only=None, correlation_id=None, **kwargs):
            aid = f"algo_{len(self.placed)+1}"
            self.placed.append({"symbol": symbol, "side": side, "sz": sz, "tp_px": tp_trigger_px, "sl_px": sl_trigger_px})
            return aid

        async def fetch_pending_algo_orders(self, symbol, limit=100):
            return []

        async def fetch_ticker(self, symbol):
            class T:
                last_price = 1000.0
            return T()

    okx = DummyOKX()

    class DummyPersistence:
        def __init__(self):
            self.saved = []

        async def save_position(self, pos):
            self.saved.append(pos)

    persistence = DummyPersistence()

    handler = OrderHandler(okx, persistence=persistence)

    # Prepare pending cache with signal
    cl = "vcorex_test_cl"
    signal = {
        "symbol": "BTC-USDT-SWAP",
        "signal_type": "buy",
        "take_profit_prices": [1100.0, 1200.0],
        "stop_loss_price": 900.0,
        "correlation_id": "cid",
    }

    handler._pending_order_cache[cl] = signal

    # Simulate WS fill payload where accFillSz present
    ws_payload = {"data": [{"clOrdId": cl, "ordId": "ord_ws_1", "accFillSz": "2", "avgPx": "1000", "state": "filled"}]}

    await handler.handle_ws_raw_order_fill(ws_payload)

    # One position should be created and TP algos placed
    active = handler.get_active_positions()
    assert len(active) == 1
    pos = active[0]
    assert len(pos.take_profit_levels) == 2
    assert hasattr(pos, "algo_order_ids")
    assert len(pos.algo_order_ids) == 2


@pytest.mark.parametrize(
    "lot_sz,min_sz,amount,expected",
    [
        (1.0, 1.0, 1000.0, 1.0),
        (0.5, 1.0, 1500.0, 1.5),
        (0.1, 0.5, 500.0, 0.5),
        (0.2, 0.2, 1000.0, 1.0),
        (0.2, 0.2, 1100.0, 1.0),
    ],
)
@pytest.mark.asyncio
async def test_place_order_sizing_matrix(lot_sz, min_sz, amount, expected):
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": lot_sz, "minSz": min_sz}}

    async def mock_request(method, path, params=None, auth_required=True):
        return {"code": "0", "data": [{"ordId": "ord_ok", "clOrdId": params.get("clOrdId")} ]}

    ex._request = mock_request
    if expected is ValueError:
        with pytest.raises(ValueError):
            await ex.place_order(symbol="BTC-USDT-SWAP", side="buy", order_type="market", amount=amount, client_order_id="stress")
    else:
        order = await ex.place_order(symbol="BTC-USDT-SWAP", side="buy", order_type="market", amount=amount, client_order_id="stress")
        assert math.isclose(order.contracts, expected)


@pytest.mark.asyncio
async def test_place_order_attach_tp_sl_payload():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.5, "lotSz": 1.0, "minSz": 1.0}}

    called = {}

    async def mock_request(method, path, params=None, auth_required=True):
        called["params"] = params
        return {"code": "0", "data": [{"ordId": "ord_ok", "clOrdId": params.get("clOrdId")} ]}

    ex._request = mock_request
    await ex.place_order(
        symbol="BTC-USDT-SWAP",
        side="buy",
        order_type="limit",
        amount=1000.0,
        price=68000.0,
        tp_price=70000.0,
        sl_price=66000.0,
        client_order_id="c3",
    )

    assert "attachAlgoOrds" in called["params"]
    assert called["params"]["attachAlgoOrds"][0]["tpTriggerPx"] == "70000.0"
    assert called["params"]["attachAlgoOrds"][0]["slTriggerPx"] == "66000.0"


@pytest.mark.asyncio
async def test_websocket_stream_reconnect_url_and_brokerid():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": 1.0, "minSz": 1.0}}

    connect_args = []

    class DummyWebSocket:
        def __init__(self):
            self.recv_count = 0

        async def send(self, message):
            return None

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return '{"arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"}, "data": [{"dummy": "x"}]}'
            raise websockets.exceptions.ConnectionClosedOK(1000, "done")

        async def close(self):
            return None

    class DummyConnect:
        async def __aenter__(self):
            connect_args.append(self.url)
            return DummyWebSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def connect_stub(url):
        conn = DummyConnect()
        conn.url = url
        return conn

    sleep_calls = 0

    async def sleep_stub(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise StopAsyncIteration()
        return None

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("infrastructure.exchange.okx_exchange.connect", connect_stub)
        mp.setattr("infrastructure.exchange.okx_exchange.asyncio.sleep", sleep_stub)

        agen = ex.websocket_stream(["tickers"], ["BTC-USDT-SWAP"], endpoint_type="public")
        msg = await agen.__anext__()
        assert msg.channel == "system"
        assert "brokerId=9999" in connect_args[0]

        msg = await agen.__anext__()
        assert msg.channel == "tickers"
        assert msg.symbol == "BTC-USDT-SWAP"
        assert msg.data == {"dummy": "x"}

        with pytest.raises(asyncio.CancelledError):
            await agen.aclose()


@pytest.mark.asyncio
async def test_order_handler_open_position_pending_reconcile_then_ws_fill_dispatches_tp_sl(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PHANTOM_VERIFIER", False)

    class DummyExchange:
        def __init__(self):
            self._markets = {"BTC-USDT-SWAP": {"minSz": 0.01, "lotSz": 0.01}}

        async def fetch_ticker(self, symbol):
            class T:
                last_price = 68000.0

            return T()

        async def place_order(self, symbol, side, order_type, amount, price=None, client_order_id=None, sl_price=None, tp_price=None, correlation_id=None, leverage=None):
            return Order(
                order_id="ord_main_1",
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                type=order_type,
                amount=amount,
                price=price,
                filled_amount=0.0,
                status="PENDING_RECONCILE",
                timestamp=int(time.time() * 1000),
                contracts=amount,
                position_side="long",
            )

        async def place_algo_order(self, symbol, side, sz, tp_trigger_px, reduce_only, correlation_id):
            return f"algo_{tp_trigger_px}"

        async def fetch_pending_algo_orders(self, symbol, **kwargs):
            return []

    class DummyPersistence:
        def __init__(self):
            self.saved = []

        async def save_position(self, pos):
            self.saved.append(pos)

        async def delete_position(self, internal_id):
            return None

    class DummyEventBus:
        def __init__(self):
            self.published = []

        async def publish(self, event):
            self.published.append(event)

    exchange = DummyExchange()
    persistence = DummyPersistence()
    event_bus = DummyEventBus()
    handler = OrderHandler(exchange, event_bus=event_bus, persistence=persistence, default_leverage=10)

    cl = "vcorex_test_cl"
    signal = {
        "symbol": "BTC-USDT-SWAP",
        "signal_type": "buy",
        "entry_price": 68000.0,
        "position_size_usdt": 68000.0,
        "amount": 1.0,
        "stop_loss_price": 65000.0,
        "take_profit_prices": [70000.0, 72000.0],
        "correlation_id": "cid",
        "client_order_id": cl,
    }

    internal_id = await handler.open_position(signal)
    assert internal_id is not None

    pos = handler.get_position(internal_id)
    assert pos is not None
    assert pos.status == PositionStatus.PENDING_RECONCILE
    assert handler._pending_order_cache.get(cl) is signal

    ws_payload = {
        "data": [
            {
                "clOrdId": cl,
                "ordId": "ord_main_1",
                "accFillSz": "1",
                "avgPx": "68000",
                "state": "filled",
            }
        ]
    }

    await handler.handle_ws_raw_order_fill(ws_payload)

    pos = handler.get_position(internal_id)
    assert pos.status == PositionStatus.OPENED
    assert pos.exchange_id == "ord_main_1"
    assert len(pos.algo_order_ids) == 2
    assert handler._pending_order_cache.get(cl) is None
    assert any(getattr(evt, "event_type", None) == EventTopic.POSITION_OPENED for evt in event_bus.published)


@pytest.mark.asyncio
async def test_okx_place_algo_order_demo_payload_and_live_response_shape():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": 0.1, "minSz": 0.5}}

    called = {}

    async def mock_request(method, path, params=None, auth_required=True):
        called["method"] = method
        called["path"] = path
        called["params"] = params
        return {"code": "0", "data": [{"algoId": "demo_algo_45"}]}

    ex._request = mock_request

    algo_id = await ex.place_algo_order(
        symbol="BTC-USDT-SWAP",
        side="sell",
        sz=1.23,
        tp_trigger_px=70500.1,
        sl_trigger_px=65000.0,
        position_side="short",
        reduce_only=True,
    )

    assert algo_id == "demo_algo_45"
    assert called["method"] == "POST"
    assert called["path"] == "/api/v5/trade/order-algo"
    assert called["params"]["ordType"] == "conditional"
    assert called["params"]["sz"] == "1.2"
    assert called["params"]["tpTriggerPx"] == "70500.1"
    assert called["params"]["slTriggerPx"] == "65000.0"
    assert called["params"]["tpOrdPx"] == "-1"
    assert called["params"]["slOrdPx"] == "-1"
    assert called["params"]["reduceOnly"] is True
    assert called["params"]["posSide"] == "short"
    assert called["params"]["algoClOrdId"].startswith("vcorex")


@pytest.mark.asyncio
async def test_okx_websocket_private_login_and_business_endpoint_demo():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": 1.0, "minSz": 1.0}}
    ex.fetch_positions = AsyncMock(return_value=[])

    connect_args = []
    sent_messages = []

    class DummyWebSocket:
        def __init__(self):
            self.recv_count = 0

        async def send(self, message):
            sent_messages.append(json.loads(message))

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({"code": "0"})
            if self.recv_count == 2:
                return json.dumps({"arg": {"channel": "orders", "instId": "BTC-USDT-SWAP"}, "data": [{"ordId": "ord_1"}]})
            raise Exception("closed")

        async def close(self):
            return None

    class DummyConnect:
        async def __aenter__(self):
            connect_args.append(self.url)
            return DummyWebSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def connect_stub(url):
        conn = DummyConnect()
        conn.url = url
        return conn

    async def sleep_stub(delay):
        raise StopAsyncIteration()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("infrastructure.exchange.okx_exchange.connect", connect_stub)
        mp.setattr("infrastructure.exchange.okx_exchange.asyncio.sleep", sleep_stub)

        agen = ex.websocket_stream(["orders"], ["BTC-USDT-SWAP"], endpoint_type="private")

        msg = await agen.__anext__()
        assert msg.channel == "system"
        assert "/ws/v5/private?brokerId=9999" in connect_args[0]

        assert len(sent_messages) >= 1
        login_payload = sent_messages[0]
        assert login_payload["op"] == "login"
        assert login_payload["args"][0]["apiKey"] == "k"
        assert login_payload["args"][0]["passphrase"] == "p"

        assert len(sent_messages) >= 2
        assert sent_messages[1]["op"] == "subscribe"
        assert sent_messages[1]["args"][0]["channel"] == "orders"
        assert sent_messages[1]["args"][0]["instType"] == "ANY"

        await agen.aclose()


@pytest.mark.asyncio
async def test_okx_websocket_business_endpoint_demo_url():
    s = SimpleNamespace(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        okx_demo_mode=True,
        okx_base_url="https://openapi.okx.com",
        okx_ws_url="wss://wspap.okx.com:8443/ws/v5",
        margin_mode="isolated",
        max_reconnect_attempts=3,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        exchange_cb_threshold=5,
        exchange_cb_cooldown=60,
        okx_public_api_capacity=20,
        okx_public_api_refill_rate=2,
        okx_private_api_capacity=20,
        okx_private_api_refill_rate=5,
    )

    ex = OKXExchange(s)
    ex._markets = {"BTC-USDT-SWAP": {"ctVal": 1000.0, "tickSz": 0.1, "lotSz": 1.0, "minSz": 1.0}}
    ex.fetch_positions = AsyncMock(return_value=[])

    connect_args = []

    class DummyWebSocket:
        async def send(self, message):
            return None

        async def recv(self):
            raise Exception("closed")

        async def close(self):
            return None

    class DummyConnect:
        async def __aenter__(self):
            connect_args.append(self.url)
            return DummyWebSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def connect_stub(url):
        conn = DummyConnect()
        conn.url = url
        return conn

    async def sleep_stub(delay):
        raise StopAsyncIteration()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("infrastructure.exchange.okx_exchange.connect", connect_stub)
        mp.setattr("infrastructure.exchange.okx_exchange.asyncio.sleep", sleep_stub)

        agen = ex.websocket_stream(["candle1m"], ["BTC-USDT-SWAP"], endpoint_type="business")

        msg = await agen.__anext__()
        assert msg.channel == "system"
        assert "/ws/v5/business?brokerId=9999" in connect_args[0]

        await agen.aclose()