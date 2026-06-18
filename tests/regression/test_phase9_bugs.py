import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from services.position.models import TrackedPosition, PositionStatus, TakeProfitLevel
from services.position.order_handler import OrderHandler

class MockExchange:
    def __init__(self):
        # MOCK both methods to prevent crash, returning fake order ids
        self.place_algo_order = AsyncMock(return_value="tp_new_id_1")
        self.place_algo_order_tps_sls = AsyncMock(return_value="tp_new_id_2")

@pytest.fixture
def order_handler():
    handler = OrderHandler(MockExchange(), None)
    return handler

# --- PART 1: LOGIC-003 TEST REBUILD ---

@pytest.mark.asyncio
async def test_logic003_test_a(order_handler):
    """
    Test A: tp_dispatched=False, algo_order_ids=["sl_1"]
    Expected: TP được dispatch. (PASS sau patch).
    """
    pos = TrackedPosition(
        id="pos1", exchange_id="exch1", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1
    )
    pos.tp_dispatched = False
    pos.algo_order_ids = ["sl_1"]
    
    signal_data = {"take_profit_prices": [{"price": 110, "exit_pct": 100}]}
    
    with patch.object(order_handler.okx_client, 'place_algo_order', new_callable=AsyncMock, return_value="tp_id") as mock_place:
        await order_handler._dispatch_algo_tps(pos, signal_data, 1.0)
        mock_place.assert_called()
        assert pos.tp_dispatched is True

@pytest.mark.asyncio
async def test_logic003_test_b(order_handler):
    """
    Test B: tp_dispatched=True.
    Expected: không dispatch lại. (PASS).
    """
    pos = TrackedPosition(
        id="pos2", exchange_id="exch2", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1
    )
    pos.tp_dispatched = True
    pos.algo_order_ids = ["sl_1"]
    
    signal_data = {"take_profit_prices": [{"price": 110, "exit_pct": 100}]}
    
    with patch.object(order_handler.okx_client, 'place_algo_order', new_callable=AsyncMock, return_value="tp_id") as mock_place:
        await order_handler._dispatch_algo_tps(pos, signal_data, 1.0)
        mock_place.assert_not_called()

@pytest.mark.asyncio
async def test_logic003_test_c(order_handler):
    """
    Test C: Corner Case. tp_dispatched=False, algo_order_ids=["sl_1", "tp_1"]
    """
    pos = TrackedPosition(
        id="pos3", exchange_id="exch3", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1
    )
    pos.tp_dispatched = False
    pos.algo_order_ids = ["sl_1", "tp_1"]
    
    signal_data = {"take_profit_prices": [{"price": 110, "exit_pct": 100}]}
    
    with patch.object(order_handler.okx_client, 'place_algo_order', new_callable=AsyncMock, return_value="tp_id_2") as mock_place:
        await order_handler._dispatch_algo_tps(pos, signal_data, 1.0)
        mock_place.assert_called()

# --- PART 2: LOGIC-004 TEST REBUILD ---

@pytest.mark.asyncio
async def test_logic004_test_a(order_handler):
    """Test A: TP dạng dict. Expected: 101. (PASS)."""
    pos = TrackedPosition(
        id="posA", exchange_id="exchA", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1,
        status=PositionStatus.PENDING_SUBMIT
    )
    pos.take_profit_levels = [{"price": 100}]
    order_handler._positions["posA"] = pos
    
    event_data = {
        "data": [{
            "ordId": "exchA",
            "state": "partially_filled",
            "accFillSz": "1",
            "avgPx": "101.0"
        }]
    }
    
    await order_handler.handle_ws_raw_order_fill(event_data)
    assert pos.take_profit_levels[0]["price"] == 101.0

@pytest.mark.asyncio
async def test_logic004_test_b(order_handler):
    """Test B: TP dạng TakeProfitLevel dataclass. Expected: 101. (PASS sau patch)."""
    pos = TrackedPosition(
        id="posB", exchange_id="exchB", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1,
        status=PositionStatus.PENDING_SUBMIT
    )
    pos.take_profit_levels = [TakeProfitLevel(price=100, exit_pct=100)]
    order_handler._positions["posB"] = pos
    
    event_data = {
        "data": [{
            "ordId": "exchB",
            "state": "partially_filled",
            "accFillSz": "1",
            "avgPx": "101.0"
        }]
    }
    
    await order_handler.handle_ws_raw_order_fill(event_data)
    assert pos.take_profit_levels[0].price == 101.0

@pytest.mark.asyncio
async def test_logic004_test_c(order_handler):
    """Test C: TP dạng object có .price. Expected: 101. (PASS sau patch)."""
    class CustomTP:
        def __init__(self, price):
            self.price = price
            
    pos = TrackedPosition(
        id="posC", exchange_id="exchC", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1,
        status=PositionStatus.PENDING_SUBMIT
    )
    pos.take_profit_levels = [CustomTP(price=100)]
    order_handler._positions["posC"] = pos
    
    event_data = {
        "data": [{
            "ordId": "exchC",
            "state": "partially_filled",
            "accFillSz": "1",
            "avgPx": "101.0"
        }]
    }
    
    await order_handler.handle_ws_raw_order_fill(event_data)
    assert pos.take_profit_levels[0].price == 101.0

@pytest.mark.asyncio
async def test_logic004_test_d(order_handler):
    """Test D: Object có price = None. Expected: Không crash, giá trị giữ nguyên None."""
    class CustomTPNone:
        def __init__(self):
            self.price = None
            
    pos = TrackedPosition(
        id="posD", exchange_id="exchD", symbol="BTC", side="long", 
        entry_price=100, current_price=100, amount=1, amount_remaining=1, leverage=1,
        status=PositionStatus.PENDING_SUBMIT
    )
    pos.take_profit_levels = [CustomTPNone()]
    order_handler._positions["posD"] = pos
    
    event_data = {
        "data": [{
            "ordId": "exchD",
            "state": "partially_filled",
            "accFillSz": "1",
            "avgPx": "101.0"
        }]
    }
    
    # Should not crash
    await order_handler.handle_ws_raw_order_fill(event_data)
    assert pos.take_profit_levels[0].price is None
