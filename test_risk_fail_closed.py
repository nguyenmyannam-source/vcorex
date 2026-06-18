"""
Test Fail-Closed Mechanism in RiskManager
=========================================

This test validates that the RiskManager correctly rejects signals when both
Mirror Cache and Exchange API are unreachable (Fail-Closed principle).

Run with: python test_risk_fail_closed.py
"""

import unittest
from unittest.mock import Mock, AsyncMock, MagicMock, patch
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestRiskFailClosed(unittest.TestCase):
    """Test suite for Fail-Closed mechanism in RiskManager."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        # Mock Signal object
        self.signal = Mock()
        self.signal.symbol = "BTC-USDT-SWAP"
        self.signal.signal_type = Mock()
        self.signal.signal_type.value = "LONG"
        self.signal.timeframe = "1H"
        self.signal.risk_approved = True  # Initially approved

        # Mock Event Bus
        self.event_bus = Mock()

        # Mock Exchange
        self.exchange = Mock()
        self.exchange.fetch_positions = AsyncMock()

        # Mock Mirror
        self.mirror = Mock()
        self.mirror.get_all_positions = AsyncMock()

        # Import RiskManager after setting up path
        from domain.risk.risk_manager import RiskManager
        from core.config import settings

        # Mock settings to ensure max_symbol_concentration is not 9999
        # This prevents early return None in _check_symbol_concentration
        settings.max_symbol_concentration = 1.0

        # Create RiskManager instance
        self.risk_manager = RiskManager(
            event_bus=self.event_bus,
            exchange=self.exchange,
            settings_obj=settings
        )

        # Inject mock mirror
        self.risk_manager.exchange_mirror = self.mirror

    def test_check_symbol_concentration_fail_closed(self):
        """
        Test that _check_symbol_concentration rejects signal when both mirror and exchange fail.
        """
        # Configure mocks to raise exceptions
        self.mirror.get_all_positions.side_effect = RuntimeError("Mirror Timeout")
        self.exchange.fetch_positions.side_effect = ConnectionError("OKX Unreachable")

        # Run async test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                self.risk_manager._check_symbol_concentration(self.signal)
            )
        finally:
            loop.close()

        # Assertions
        self.assertIsNotNone(result, "Result should not be None")
        self.assertFalse(result.approved, "Signal should be rejected (approved=False)")
        self.assertFalse(
            self.signal.risk_approved,
            "Signal.risk_approved should be False"
        )
        self.assertIn(
            "Hệ thống mù dữ liệu exposure",
            result.reason,
            "Reason should contain 'Hệ thống mù dữ liệu exposure'"
        )
        self.assertIn(
            "Cả Mirror và Exchange API đều mất kết nối",
            result.reason,
            "Reason should mention both Mirror and Exchange API failure"
        )

    def test_check_pending_orders_fail_closed(self):
        """
        Test that _check_pending_orders rejects signal when both mirror and exchange fail.
        """
        # Configure mocks to raise exceptions
        self.mirror.get_all_orders = AsyncMock(side_effect=RuntimeError("Mirror Timeout"))
        self.exchange.fetch_orders = AsyncMock(side_effect=ConnectionError("OKX Unreachable"))

        # Run async test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                self.risk_manager._check_pending_orders(self.signal)
            )
        finally:
            loop.close()

        # Assertions
        self.assertIsNotNone(result, "Result should not be None")
        self.assertFalse(result.approved, "Signal should be rejected (approved=False)")
        self.assertFalse(
            self.signal.risk_approved,
            "Signal.risk_approved should be False"
        )
        self.assertIn(
            "Hệ thống mù dữ liệu exposure",
            result.reason,
            "Reason should contain 'Hệ thống mù dữ liệu exposure'"
        )
        self.assertIn(
            "Cả Mirror và Exchange API đều mất kết nối",
            result.reason,
            "Reason should mention both Mirror and Exchange API failure"
        )

    def test_check_symbol_concentration_mirror_success(self):
        """
        Test that _check_symbol_concentration passes when mirror succeeds.
        """
        # Configure mirror to return empty positions (no existing positions)
        self.mirror.get_all_positions = AsyncMock(return_value={})

        # Run async test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                self.risk_manager._check_symbol_concentration(self.signal)
            )
        finally:
            loop.close()

        # Assertions - should pass (return None) since no positions exist
        self.assertIsNone(result, "Result should be None when no positions exist")

    def test_check_symbol_concentration_exchange_fallback_success(self):
        """
        Test that _check_symbol_concentration uses exchange fallback when mirror fails.
        """
        # Configure mirror to fail, exchange to succeed
        self.mirror.get_all_positions = AsyncMock(side_effect=RuntimeError("Mirror Timeout"))
        self.exchange.fetch_positions = AsyncMock(return_value=[])

        # Run async test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                self.risk_manager._check_symbol_concentration(self.signal)
            )
        finally:
            loop.close()

        # Assertions - should pass (return None) since no positions exist
        self.assertIsNone(result, "Result should be None when no positions exist")


if __name__ == "__main__":
    # Run tests with verbose output
    unittest.main(verbosity=2)
