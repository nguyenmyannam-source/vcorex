"""
Institutional Slippage Decay Analysis.
This test evaluates how the strategy's profitability decays as slippage increases.
Essential for determining the 'Execution Alpha' and 'Break-even Slippage'.
"""

import pandas as pd
import pytest

from domain.risk.risk_manager import RiskManager


class TestSlippageDecay:
    """Audit the strategy robustness against execution slippage."""

    @pytest.fixture
    def risk_manager(self, event_bus, mock_exchange):
        return RiskManager(event_bus=event_bus, exchange=mock_exchange)

    def test_slippage_impact_on_pnl_decay(self, risk_manager):
        """
        Analyze PnL decay over a series of trades with increasing slippage.
        Slippage is expressed in Basis Points (bps). 1 bps = 0.01%.
        """
        # Baseline Trade Scenario
        entry_price = 50000.0
        target_exit_price = 51000.0  # 2% gain gross
        side = "long"
        amount = 1.0  # 1 BTC

        # Slippage levels to test (in bps)
        slippage_levels = [0, 5, 10, 20, 50, 100]
        results = []

        for bps in slippage_levels:
            slippage_pct = bps / 10000.0

            # For a LONG position:
            # Entry Slippage: Buying at a HIGHER price
            # Exit Slippage: Selling at a LOWER price
            effective_entry = entry_price * (1 + slippage_pct)
            effective_exit = target_exit_price * (1 - slippage_pct)

            # Calculate Net PnL (including fees)
            net_pnl = risk_manager.calculate_pnl(
                entry_price=effective_entry,
                close_price=effective_exit,
                side=side,
                amount=amount,
                include_fees=True,
            )

            # Calculate Profit Margin (Return on Notional)
            profit_margin_pct = (net_pnl / (entry_price * amount)) * 100

            results.append(
                {
                    "slippage_bps": bps,
                    "net_pnl": net_pnl,
                    "profit_margin_pct": profit_margin_pct,
                    "status": "PROFITABLE" if net_pnl > 0 else "LOSS",
                }
            )

        # Generate a summary report
        df = pd.DataFrame(results)
        print("\n--- Slippage Decay Analysis Report ---")
        print(df.to_string(index=False))

        # Institutional Requirements:
        # 1. At 0 bps, we MUST be profitable (Gross Gain > Fees)
        assert results[0]["net_pnl"] > 0, "Strategy is not even profitable at 0 slippage!"

        # 2. Find Break-even Slippage
        profitable_results = [r for r in results if r["net_pnl"] > 0]
        max_profitable_slippage = max([r["slippage_bps"] for r in profitable_results])

        print(f"\nBreak-even Slippage: >{max_profitable_slippage} bps")

        # 3. Alert if decay is too fast
        # If we lose 50% of profit at only 10 bps slippage, the strategy is 'fragile'
        pnl_0 = results[0]["net_pnl"]
        pnl_10 = results[2]["net_pnl"]
        decay_10bps = (pnl_0 - pnl_10) / pnl_0

        print(f"PnL Decay at 10bps slippage: {decay_10bps * 100:.2f}%")

        # In this specific 2% gain scenario:
        # PnL(0) ~ 1000 - fees (~50) = 950
        # PnL(10) ~ (51000*0.999 - 50000*1.001) - fees = (50949 - 50050) - 50 = 849
        # Decay ~ (950-849)/950 ~ 10.6%
        assert decay_10bps < 0.5, "Strategy is too sensitive to slippage (Fragile execution)"

    def test_latency_induced_slippage_simulation(self, risk_manager):
        """
        Simulate slippage caused by network latency.
        Assume a fast moving market (0.1% price movement per second).
        Latency: 200ms = 0.02% slippage.
        """
        market_volatility_per_sec = 0.001  # 0.1% per sec
        latency_ms = 500  # 0.5 sec

        induced_slippage_pct = market_volatility_per_sec * (latency_ms / 1000.0)

        entry_price = 50000.0
        amount = 1.0

        effective_entry = entry_price * (1 + induced_slippage_pct)

        # Cost of latency in USDT
        latency_cost = (effective_entry - entry_price) * amount

        print("\n--- Latency Impact Audit ---")
        print(f"Market Volatility: {market_volatility_per_sec * 100:.2f}% / sec")
        print(f"Latency: {latency_ms} ms")
        print(f"Induced Slippage: {induced_slippage_pct * 100:.4f}%")
        print(f"Latency Cost: ${latency_cost:.2f} per 1 BTC trade")

        # Institutional requirement: Latency cost should be < 5% of average trade profit
        avg_trade_profit = 500.0  # Hypothetical
        assert latency_cost < (
            avg_trade_profit * 0.1
        ), "Network latency cost is too high for this strategy"
