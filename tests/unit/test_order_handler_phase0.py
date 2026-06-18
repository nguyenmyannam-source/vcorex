"""Phase 0 fixes: close side, unit conversion, zombie eviction, active position filter."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.position.models import PositionStatus, TrackedPosition
from services.position.order_handler import OrderHandler


def _make_position(**overrides) -> TrackedPosition:
    defaults = dict(
        id="pos_test_1",
        exchange_id="ex_1",
        symbol="BTC-USDT-SWAP",
        side="buy",
        entry_price=50000.0,
        current_price=50000.0,
        amount=2.0,
        amount_remaining=2.0,
        leverage=10.0,
        ct_val=0.01,
        status=PositionStatus.OPENED,
    )
    defaults.update(overrides)
    return TrackedPosition(**defaults)


@pytest.mark.parametrize(
    "side,expected",
    [
        ("buy", "sell"),
        ("long", "sell"),
        ("sell", "buy"),
        ("short", "buy"),
        ("BUY", "sell"),
    ],
)
def test_close_order_side_mapping(side, expected):
    assert OrderHandler._close_order_side(side) == expected


def test_resolve_close_amount_contracts_when_opened():
    handler = OrderHandler(MagicMock())
    pos = _make_position(status=PositionStatus.OPENED, amount_remaining=5.0, ct_val=0.01)
    assert handler._resolve_close_place_order_amount(pos, 5.0) == pytest.approx(0.05)


def test_resolve_close_amount_usdt_when_pending():
    handler = OrderHandler(MagicMock())
    pos = _make_position(
        status=PositionStatus.PENDING_SUBMIT,
        amount_remaining=100.0,
        entry_price=50000.0,
    )
    assert handler._resolve_close_place_order_amount(pos, 100.0) == pytest.approx(100.0 / 50000.0)


def test_get_active_positions_excludes_terminal():
    handler = OrderHandler(MagicMock())
    open_pos = _make_position(id="open", status=PositionStatus.OPENED)
    closed_pos = _make_position(id="closed", status=PositionStatus.CLOSED)
    failed_pos = _make_position(id="failed", status=PositionStatus.FAILED)
    handler._positions[open_pos.id] = open_pos
    handler._positions[closed_pos.id] = closed_pos
    handler._positions[failed_pos.id] = failed_pos

    active = handler.get_active_positions()
    assert len(active) == 1
    assert active[0].id == "open"


@pytest.mark.asyncio
async def test_close_position_buy_side_sends_sell_with_contract_conversion():
    okx = MagicMock()
    okx.place_order = AsyncMock(return_value=SimpleNamespace(order_id="ord_close"))
    okx.cancel_algo_orders = AsyncMock()

    persistence = MagicMock()
    persistence.save_position = AsyncMock()

    handler = OrderHandler(okx, persistence=persistence)
    pos = _make_position(side="buy", amount_remaining=10.0, ct_val=0.01, algo_order_ids=["algo1"])
    handler._positions[pos.id] = pos

    result = await handler.close_position(pos.id)

    assert result is True
    okx.place_order.assert_awaited_once()
    call_kwargs = okx.place_order.await_args.kwargs
    assert call_kwargs["side"] == "sell"
    assert call_kwargs["amount"] == pytest.approx(10.0 * 0.01)
    # Full close waits for WS fill — position stays in RAM as CLOSING until confirmed
    assert pos.id in handler._positions
    assert pos.status == PositionStatus.CLOSING
    persistence.save_position.assert_awaited()


@pytest.mark.asyncio
async def test_close_position_partial_keeps_position_in_ram():
    okx = MagicMock()
    okx.place_order = AsyncMock(return_value=SimpleNamespace(order_id="ord_partial"))
    okx.cancel_algo_orders = AsyncMock()

    persistence = MagicMock()
    persistence.save_position = AsyncMock()

    handler = OrderHandler(okx, persistence=persistence)
    pos = _make_position(side="long", amount_remaining=10.0, ct_val=0.01)
    handler._positions[pos.id] = pos

    result = await handler.close_position(pos.id, close_amount=4.0)

    assert result is True
    assert pos.id in handler._positions
    assert pos.amount_remaining == pytest.approx(6.0)
    assert pos.status == PositionStatus.PARTIAL_TP
    okx.cancel_algo_orders.assert_not_awaited()


def test_event_topic_enum_has_phase0_members():
    from core.events.topics import EventTopic

    assert hasattr(EventTopic, "TELEGRAM_SEND_MESSAGE")
    assert hasattr(EventTopic, "MIRROR_RESYNC_FAILED")
    assert EventTopic.TELEGRAM_SEND_MESSAGE.value == "telegram.send_message"
    assert EventTopic.MIRROR_RESYNC_FAILED.value == "mirror.resync_failed"


@pytest.mark.asyncio
async def test_dispatch_algo_tps_overlapping_orders_no_skip():
    okx = MagicMock()
    # Mock return order id for place_algo_order
    okx.place_algo_order = AsyncMock(return_value="algo_tp_1")
    
    # Mock _markets to avoid falling back to OKX_SYMBOL_SPECS and getting KeyError
    okx._markets = {"BTC-USDT-SWAP": {"tickSz": "0.1", "minSz": "0.01", "lotSz": "1.0"}}

    persistence = MagicMock()
    persistence.save_position = AsyncMock()

    handler = OrderHandler(okx, persistence=persistence)
    
    pos = _make_position(
        side="buy", 
        amount_remaining=10.0, 
        ct_val=1.0, 
        entry_price=100.0,
        take_profit_levels=[],
        algo_order_ids=[],
    )
    pos.tp_dispatched = False
    
    signal_data = {
        "symbol": "BTC-USDT-SWAP",
        "signal_type": "buy",
        "entry_price": 100.0,
        "take_profit_prices": [{"price": 110.0, "exit_pct": 1.0}]
    }

    await handler._dispatch_algo_tps(pos, signal_data, 10.0)
    
    # Assert place_algo_order was called exactly once for the single TP
    assert okx.place_algo_order.call_count == 1
    call_kwargs = okx.place_algo_order.await_args.kwargs
    assert call_kwargs["tp_trigger_px"] == 110.0
    assert call_kwargs["sz"] == 10.0
    assert pos.tp_dispatched is True

@pytest.mark.asyncio
async def test_dispatch_algo_sl_overlapping_orders_no_skip():
    okx = MagicMock()
    okx.place_algo_order = AsyncMock(return_value="algo_sl_1")
    
    okx._markets = {"BTC-USDT-SWAP": {"tickSz": "0.1", "minSz": "0.01", "lotSz": "1.0"}}
    
    # Mock fetch_pending_algo_orders to return an existing SL order to simulate overlap
    okx.fetch_pending_algo_orders = AsyncMock(return_value=[
        {"slTriggerPx": "90.0"}
    ])
    
    persistence = MagicMock()
    persistence.save_position = AsyncMock()

    handler = OrderHandler(okx, persistence=persistence)
    
    pos = _make_position(
        side="buy", 
        amount_remaining=10.0, 
        ct_val=1.0, 
        entry_price=100.0,
        stop_loss=None,
    )
    pos.sl_algo_order_id = None
    
    signal_data = {
        "symbol": "BTC-USDT-SWAP",
        "signal_type": "buy",
        "entry_price": 100.0,
        "stop_loss_price": 90.0
    }

    await handler._dispatch_algo_sl(pos, signal_data, 10.0)
    
    # Assert place_algo_order was called despite existing SL order
    assert okx.place_algo_order.call_count == 1
    call_kwargs = okx.place_algo_order.await_args.kwargs
    assert call_kwargs["sl_trigger_px"] == 90.0
    assert call_kwargs["sz"] == 10.0
    assert pos.sl_algo_order_id == "algo_sl_1"
