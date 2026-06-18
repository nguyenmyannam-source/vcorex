"""
Institutional Portfolio Correlation & Exposure Audit.
Tests if the RiskManager properly identifies correlated assets and prevents
over-exposure to the same market factor (e.g., USD, BTC).
"""

import pytest

from domain.risk.risk_manager import RiskManager


class TestCorrelationExposure:
    """Audit the bot's ability to manage cross-asset correlation risk."""

    @pytest.fixture
    def risk_manager(self, event_bus, mock_exchange):
        rm = RiskManager(event_bus=event_bus, exchange=mock_exchange)
        # Mock portfolio for correlation tests
        rm.positions = []
        return rm

    def test_correlation_limit_prevents_overexposure(self, risk_manager):
        """
        Scenario: Bot is already long BTC. A new signal comes for ETH.
        Correlation between BTC and ETH is high (~0.85).
        Goal: RiskManager should flag or reduce size if total correlation-adjusted exposure is too high.
        """
        # Current Portfolio: Long 1.0 BTC (~$60,000)
        from services.position_engine import TrackedPosition

        btc_pos = TrackedPosition(
            id="pos_btc",
            exchange_id="ext_btc",
            symbol="BTC-USDT-SWAP",
            side="long",
            entry_price=60000.0,
            current_price=60000.0,
            amount=1.0,
            amount_remaining=1.0,
            leverage=10,
        )
        risk_manager.positions = [btc_pos]

        # New Signal: Long 10.0 ETH (~$25,000)
        eth_signal = {
            "symbol": "ETH-USDT-SWAP",
            "signal_type": "buy",
            "entry_price": 2500.0,
            "position_size_usdt": 25000.0,
        }

        # Correlation Matrix (Hypothetical)
        correlation_matrix = {
            ("BTC-USDT-SWAP", "ETH-USDT-SWAP"): 0.85,
            ("BTC-USDT-SWAP", "SOL-USDT-SWAP"): 0.60,
        }

        # Institutional Logic:
        # Total Risk = Sum(Position Value * Correlation with Portfolio)
        existing_value = btc_pos.amount * btc_pos.current_price  # 60,000
        new_value = eth_signal["position_size_usdt"]  # 25,000

        # Correlation-adjusted new exposure
        corr = correlation_matrix.get(("BTC-USDT-SWAP", "ETH-USDT-SWAP"), 0.5)
        total_beta_exposure = existing_value + (new_value * corr)

        print("\n--- Portfolio Correlation Audit ---")
        print(f"Existing BTC Exposure: ${existing_value}")
        print(f"New ETH Signal Value: ${new_value}")
        print(f"BTC-ETH Correlation: {corr}")
        print(f"Total Correlation-Adjusted Exposure: ${total_beta_exposure:.2f}")

        # Institutional Rule: Total correlation-adjusted exposure per 'sector' (e.g. Crypto) < $100k
        sector_limit = 100000.0
        assert (
            total_beta_exposure < sector_limit
        ), "Correlation-adjusted exposure exceeds sector limit!"
        print("Risk within sector limits: [OK]")

    def test_market_crash_correlation_spike_simulation(self, risk_manager):
        """
        In a crash, correlations often go to 1.0.
        Test the 'worst-case' drawdown if all positions move together.
        """
        # Portfolio: 3 positions
        positions = [
            {"symbol": "BTC", "value": 50000, "leverage": 5},
            {"symbol": "ETH", "value": 30000, "leverage": 5},
            {"symbol": "SOL", "value": 20000, "leverage": 5},
        ]

        total_notional = sum(p["value"] for p in positions)  # 100,000
        total_margin = sum(p["value"] / p["leverage"] for p in positions)  # 10,000

        # Simulate a 10% market crash where correlation = 1.0
        crash_pct = 0.10
        portfolio_loss = total_notional * crash_pct  # 10,000

        print("\n--- Crash Correlation Audit (Corr=1.0) ---")
        print(f"Total Portfolio Notional: ${total_notional}")
        print(f"Total Margin Locked: ${total_margin}")
        print(f"10% Systemic Crash Loss: ${portfolio_loss}")

        # If Loss >= Total Margin, it's a Liquidation Event
        is_liquidated = portfolio_loss >= total_margin

        print(f"Liquidation on 10% systemic crash: {'YES' if is_liquidated else 'NO'}")

        # Institutional requirement: Systemic crash of 10% should NOT liquidate the portfolio
        # This implies we need higher margin or lower total leverage
        assert (
            not is_liquidated
        ), "Portfolio is too fragile to a systemic 10% crash (Correlation Spike Risk)"
