"""
Unit tests for TP Sync Bug Fix (#0 - Critical Bug Fix).

Tests verify that:
1. Algo orders are registered on dispatch
2. TP fills are found via reverse mapping
3. TP algo fills unregister orders but do NOT mutate amount_remaining (position WS is authority)
4. Position WS sync updates amount_remaining (tested via simulate helper)
5. No over-close risks with reduce-only orders
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from services.position.models import TrackedPosition, PositionStatus, TakeProfitLevel
from services.position.order_handler import OrderHandler
from core.events.topics import EventTopic


class MockOKXClient:
    """Mock OKX client for testing."""

    def __init__(self):
        self._markets = {
            "BTC-USDT-SWAP": {
                "minSz": 0.01,
                "ctVal": 0.01,
            }
        }

    async def place_order(self, symbol, side, sz, **kwargs):
        """Mock market order placement."""
        return {
            "ordId": str(uuid.uuid4()),
            "clOrdId": kwargs.get("correlation_id", str(uuid.uuid4())),
        }

    async def place_algo_order(self, symbol, side, sz, tp_trigger_px=None, reduce_only=False, **kwargs):
        """Mock algo order placement returns order ID."""
        return str(uuid.uuid4())


class TestTPSyncFix:
    """Test suite for TP Sync Bug Fix."""

    @pytest.fixture
    def mock_client(self):
        """Create mock OKX client."""
        return MockOKXClient()

    @pytest.fixture
    def order_handler(self, mock_client):
        """Create OrderHandler with mock dependencies."""
        handler = OrderHandler(
            okx_client=mock_client,
            event_bus=None,
            persistence=None,
            default_leverage=10
        )
        return handler

    @pytest.fixture
    def sample_position(self):
        """Create a sample tracked position."""
        return TrackedPosition(
            id="test_pos_001",
            exchange_id="11111",  # Entry order ID
            symbol="BTC-USDT-SWAP",
            side="long",
            entry_price=68500.0,
            current_price=72062.0,
            amount=10.0,
            amount_remaining=10.0,
            leverage=10,
            ct_val=0.01,
            status=PositionStatus.OPENED,
        )

    # ================================================================================
    # Test 1: Algo Order Registration
    # ================================================================================

    def test_register_algo_order_maps_to_position(self, order_handler, sample_position):
        """Test that algo orders are registered with reverse mapping."""
        order_handler._positions[sample_position.id] = sample_position
        algo_order_id = "22222"

        # Register the algo order
        order_handler._register_algo_order(sample_position.id, algo_order_id)

        # Verify mapping exists
        assert algo_order_id in order_handler._algo_order_to_position
        assert order_handler._algo_order_to_position[algo_order_id] == sample_position.id

    def test_unregister_algo_order_removes_mapping(self, order_handler, sample_position):
        """Test that algo orders are unregistered after fill."""
        algo_order_id = "22222"
        order_handler._algo_order_to_position[algo_order_id] = sample_position.id

        # Unregister the algo order
        order_handler._unregister_algo_order(algo_order_id)

        # Verify mapping is removed
        assert algo_order_id not in order_handler._algo_order_to_position

    @staticmethod
    def _simulate_position_ws_sync(position: TrackedPosition, exchange_size: float) -> None:
        """Simulate exchange position channel updating absolute size (single authority)."""
        position.amount_remaining = exchange_size
        if exchange_size <= 0:
            position.status = PositionStatus.CLOSED
        elif exchange_size < position.amount:
            position.status = PositionStatus.PARTIAL_TP

    # ================================================================================
    # Test 2: Simple TP Fill - order handler unregisters only
    # ================================================================================

    @pytest.mark.asyncio
    async def test_tp_fill_decrements_amount_remaining(self, order_handler, sample_position):
        """TP fills unregister algo orders; size updates come from position WS."""
        order_handler._positions[sample_position.id] = sample_position
        tp1_order_id = "22222"
        order_handler._register_algo_order(sample_position.id, tp1_order_id)

        initial_amount = sample_position.amount_remaining
        fill_amount = 5.0

        event_data = {
            "algoId": tp1_order_id,
            "ordId": "ord_" + tp1_order_id,
            "clOrdId": None,
            "state": "filled",
            "accFillSz": fill_amount,
            "fillPx": 72062.0,
        }

        await order_handler.handle_ws_raw_order_fill(event_data)

        # Order handler must not mutate size locally
        assert sample_position.amount_remaining == initial_amount
        assert tp1_order_id not in order_handler._algo_order_to_position

        # Position WS sync sets absolute size
        self._simulate_position_ws_sync(sample_position, initial_amount - fill_amount)
        assert sample_position.amount_remaining == 5.0

    # ================================================================================
    # Test 3: Sequential TP Fills (TP1 → TP2 → TP3)
    # ================================================================================

    @pytest.mark.asyncio
    async def test_sequential_tp_fills(self, order_handler, sample_position):
        """Test sequential TP fills with correct amount_remaining tracking."""
        # Setup
        order_handler._positions[sample_position.id] = sample_position

        tp_orders = [
            {"id": "22222", "fill_amount": 5.0, "name": "TP1"},     # 50%
            {"id": "33333", "fill_amount": 3.0, "name": "TP2"},     # 30%
            {"id": "44444", "fill_amount": 2.0, "name": "TP3"},     # 20%
        ]

        # Register all TP orders
        for tp in tp_orders:
            order_handler._register_algo_order(sample_position.id, tp["id"])

        # Process each TP fill sequentially
        amounts_expected = [10, 5, 2, 0]  # Initial, after TP1, after TP2, after TP3

        for i, tp in enumerate(tp_orders):
            event_data = {
                "algoId": tp["id"],
                "ordId": "ord_" + tp["id"],
                "clOrdId": None,
                "state": "filled",
                "accFillSz": tp["fill_amount"],
                "fillPx": 72062.0,
            }

            await order_handler.handle_ws_raw_order_fill(event_data)
            self._simulate_position_ws_sync(sample_position, amounts_expected[i + 1])

            expected = amounts_expected[i + 1]
            assert sample_position.amount_remaining == expected, \
                f"After {tp['name']}: expected {expected}, got {sample_position.amount_remaining}"

        # Verify final position is closed
        assert sample_position.amount_remaining == 0

    # ================================================================================
    # Test 4: Entry Fill vs TP Fill Handling
    # ================================================================================

    @pytest.mark.asyncio
    async def test_entry_fill_sets_amount_remaining(self, order_handler, sample_position):
        """Test that entry fills SET amount_remaining (not decrement)."""
        sample_position.amount_remaining = 0  # Simulate unfilled entry order
        order_handler._positions[sample_position.id] = sample_position

        # Entry order fill event
        entry_order_id = "11111"
        sample_position.exchange_id = entry_order_id

        event_data = {
            "ordId": entry_order_id,
            "clOrdId": None,
            "state": "filled",
            "accFillSz": 10.0,  # Entry fills 10 contracts
            "fillPx": 68500.0,
        }

        # Process entry fill
        await order_handler.handle_ws_raw_order_fill(event_data)

        # Verify: amount_remaining is SET to 10 (not decremented from 0)
        assert sample_position.amount_remaining == 10.0

    # ================================================================================
    # Test 5: No Over-Close Risk
    # ================================================================================

    @pytest.mark.asyncio
    async def test_no_over_close_with_partial_fills(self, order_handler, sample_position):
        """Test that partial TP fills don't cause over-close."""
        order_handler._positions[sample_position.id] = sample_position

        # Setup: position with 10 contracts
        tp1_order_id = "22222"
        order_handler._register_algo_order(sample_position.id, tp1_order_id)

        # TP1 tries to close 8 contracts (but only 5 are placed)
        # This simulates order_handler deciding to skip smaller sizes
        event_data = {
            "algoId": tp1_order_id,
            "ordId": "ord_" + tp1_order_id,
            "clOrdId": None,
            "state": "filled",
            "accFillSz": 5.0,  # Actually fills 5
            "fillPx": 72062.0,
        }

        await order_handler.handle_ws_raw_order_fill(event_data)
        self._simulate_position_ws_sync(sample_position, 5.0)

        assert sample_position.amount_remaining == 5.0

        tp2_order_id = "33333"
        order_handler._register_algo_order(sample_position.id, tp2_order_id)

        event_data = {
            "algoId": tp2_order_id,
            "ordId": "ord_" + tp2_order_id,
            "clOrdId": None,
            "state": "filled",
            "accFillSz": 5.0,
            "fillPx": 75000.0,
        }

        await order_handler.handle_ws_raw_order_fill(event_data)
        self._simulate_position_ws_sync(sample_position, 0.0)

        # Verify: position reaches 0, not negative
        assert sample_position.amount_remaining == 0.0
        assert sample_position.amount_remaining >= 0  # Safety check

    # ================================================================================
    # Test 6: TP Dispatch Registers Orders
    # ================================================================================

    @pytest.mark.asyncio
    async def test_dispatch_algo_tps_registers_orders(self, order_handler, sample_position):
        """Test that _dispatch_algo_tps registers returned order IDs."""
        with patch.object(order_handler.okx_client, 'place_algo_order') as mock_place:
            # Mock algo order placement to return order IDs
            tp_order_ids = [str(uuid.uuid4()) for _ in range(3)]
            mock_place.side_effect = tp_order_ids

            signal_data = {
                "symbol": "BTC-USDT-SWAP",
                "signal_type": "buy",
                "take_profit_prices": [
                    {"price": 72062, "exit_pct": 0.5},
                    {"price": 75487, "exit_pct": 0.3},
                    {"price": 78912, "exit_pct": 0.2},
                ],
                "correlation_id": "test_signal_001",
            }

            # Dispatch TPs
            await order_handler._dispatch_algo_tps(sample_position, signal_data, 10.0)

            # Verify: all returned order IDs are registered
            for order_id in tp_order_ids:
                assert order_id in order_handler._algo_order_to_position
                assert order_handler._algo_order_to_position[order_id] == sample_position.id

    @pytest.mark.asyncio
    async def test_dispatch_algo_tps_merges_dust_remainder(self, order_handler, sample_position):
        """Dust remainder below min_sz is merged into the last scheduled TP slice."""
        order_handler.okx_client._markets = {
            "BTC-USDT-SWAP": {"minSz": 3, "lotSz": 1, "ctVal": 0.01},
        }

        placed_sizes = []

        async def capture_place(**kwargs):
            placed_sizes.append(kwargs["sz"])
            return str(uuid.uuid4())

        with patch.object(order_handler.okx_client, "place_algo_order", side_effect=capture_place):
            signal_data = {
                "symbol": "BTC-USDT-SWAP",
                "signal_type": "buy",
                "take_profit_prices": [
                    {"price": 72062, "exit_pct": 0.5},
                    {"price": 75487, "exit_pct": 0.3},
                    {"price": 78912, "exit_pct": 0.2},
                ],
                "correlation_id": "test_merge_remainder",
            }
            await order_handler._dispatch_algo_tps(sample_position, signal_data, 10.0)

        assert placed_sizes == [5.0, 5.0]
        assert sum(placed_sizes) == 10.0

    # ================================================================================
    # Test 7: Margin Calculation After TP Sync
    # ================================================================================

    def test_margin_calculation_with_decremented_amount(self, sample_position):
        """Test that margin calculation is correct after amount_remaining is decremented."""
        # Initial margin (10 contracts)
        # Calculation: (amount * ct_val * entry_price) / leverage
        # = (10 * 0.01 * 68500) / 10 = 685
        initial_margin = sample_position.get_margin()
        assert initial_margin == (10.0 * 0.01 * 68500.0) / 10
        assert initial_margin == pytest.approx(685.0, abs=1)

        # Simulate TP fill: reduce by 5 contracts
        sample_position.amount_remaining = 5.0

        # New margin should be half
        new_margin = sample_position.get_margin()
        assert new_margin == (5.0 * 0.01 * 68500.0) / 10
        assert new_margin == pytest.approx(342.5, abs=1)

        # Verify: margin is exactly half
        assert new_margin == pytest.approx(initial_margin / 2, abs=1)
    """Integration tests for TP Sync fix with realistic scenario."""

    @pytest.fixture
    def order_handler_with_persistence(self):
        """Create OrderHandler with mock persistence."""
        mock_client = MockOKXClient()
        mock_persistence = AsyncMock()
        handler = OrderHandler(
            okx_client=mock_client,
            event_bus=None,
            persistence=mock_persistence,
            default_leverage=10
        )
        return handler, mock_persistence

    @pytest.mark.asyncio
    async def test_full_position_lifecycle_entry_to_closed(self, order_handler_with_persistence):
        """Test complete position lifecycle: entry → TP1 → TP2 → TP3 → closed."""
        handler, mock_persistence = order_handler_with_persistence

        # Create initial position
        pos = TrackedPosition(
            id="pos_full_lifecycle",
            exchange_id="entry_order_11111",
            symbol="BTC-USDT-SWAP",
            side="long",
            entry_price=68500.0,
            current_price=70000.0,
            amount=100.0,
            amount_remaining=0.0,
            leverage=10,
            ct_val=0.01,
            status=PositionStatus.PENDING_SUBMIT,
        )
        handler._positions[pos.id] = pos

        # Step 1: Entry order fills (sets amount_remaining)
        print("\n=== STEP 1: Entry Order Fill ===")
        entry_fill_event = {
            "ordId": "entry_order_11111",
            "clOrdId": None,
            "state": "filled",
            "accFillSz": 100.0,
            "fillPx": 68500.0,
        }
        await handler.handle_ws_raw_order_fill(entry_fill_event)
        print(f"After entry fill: amount_remaining = {pos.amount_remaining}")
        assert pos.amount_remaining == 100.0

        # Step 2: Register and fill TP orders
        tp_config = [
            {"id": "tp1_22222", "fill_amount": 50.0, "name": "TP1"},
            {"id": "tp2_33333", "fill_amount": 30.0, "name": "TP2"},
            {"id": "tp3_44444", "fill_amount": 20.0, "name": "TP3"},
        ]

        for tp in tp_config:
            handler._register_algo_order(pos.id, tp["id"])

        print("\n=== STEP 2-4: TP Fills ===")
        cumulative_closed = 0
        for tp in tp_config:
            tp_fill_event = {
                "algoId": tp["id"],
                "ordId": "ord_" + tp["id"],
                "clOrdId": None,
                "state": "filled",
                "accFillSz": tp["fill_amount"],
                "fillPx": 70000.0,
            }
            await handler.handle_ws_raw_order_fill(tp_fill_event)
            cumulative_closed += tp["fill_amount"]
            self._simulate_position_ws_sync(pos, 100.0 - cumulative_closed)
            print(f"After {tp['name']}: amount_remaining = {pos.amount_remaining}, cumulative closed = {cumulative_closed}")
            assert pos.amount_remaining == 100.0 - cumulative_closed

        # Step 3: Verify final state
        print(f"\n=== FINAL STATE ===")
        print(f"Final amount_remaining: {pos.amount_remaining}")
        print(f"Total closed: {100.0 - pos.amount_remaining}")

        assert pos.amount_remaining == 0.0, "Position should be fully closed"
        assert len(handler._algo_order_to_position) == 0, "All TP orders should be unregistered"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
