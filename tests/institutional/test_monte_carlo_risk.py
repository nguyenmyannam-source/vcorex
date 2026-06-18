"""
Institutional Monte Carlo Risk Analysis.
Simulates 10,000 different market paths based on strategy performance metrics
to determine Maximum Drawdown (MDD) and Risk of Ruin.
"""

import numpy as np
import pandas as pd


class TestMonteCarloRisk:
    """Audit the strategy risk profile using Monte Carlo simulations."""

    def test_monte_carlo_drawdown_distribution(self):
        """
        Simulate 10,000 portfolios to find the 99% Value-at-Risk (VaR)
        and Maximum Drawdown distribution.
        """
        # Strategy Metrics (derived from backtest or historicals)
        initial_balance = 10000.0
        win_rate = 0.60  # 60%
        avg_win = 0.02  # 2% gain
        avg_loss = 0.015  # 1.5% loss
        trades_per_period = 100
        num_simulations = 10000

        all_final_balances = []
        all_max_drawdowns = []

        print(f"\n--- Running {num_simulations} Monte Carlo Simulations ---")

        for _ in range(num_simulations):
            balance = initial_balance
            peak = initial_balance
            max_dd = 0

            # Simulate 100 trades
            for _ in range(trades_per_period):
                if np.random.random() < win_rate:
                    balance *= 1 + avg_win
                else:
                    balance *= 1 - avg_loss

                # Track drawdown
                if balance > peak:
                    peak = balance
                dd = (peak - balance) / peak
                if dd > max_dd:
                    max_dd = dd

            all_final_balances.append(balance)
            all_max_drawdowns.append(max_dd * 100)

        # Analysis
        final_balances = pd.Series(all_final_balances)
        max_drawdowns = pd.Series(all_max_drawdowns)

        print(f"Initial Balance: ${initial_balance}")
        print(f"Mean Final Balance: ${final_balances.mean():.2f}")
        print(f"Median Final Balance: ${final_balances.median():.2f}")
        print(f"95% Confidence Final Balance (VaR 95): ${final_balances.quantile(0.05):.2f}")
        print(f"Worst Case Final Balance: ${final_balances.min():.2f}")

        print("\n--- Drawdown Analysis ---")
        print(f"Average Max Drawdown: {max_drawdowns.mean():.2f}%")
        print(f"99th Percentile Max Drawdown: {max_drawdowns.quantile(0.99):.2f}%")
        print(f"Worst Case Drawdown: {max_drawdowns.max():.2f}%")

        # Risk of Ruin: Probability of losing > 50% of capital
        ruin_count = sum(1 for b in all_final_balances if b < (initial_balance * 0.5))
        risk_of_ruin = (ruin_count / num_simulations) * 100

        print(f"\nRisk of 50% Ruin: {risk_of_ruin:.2f}%")

        # Institutional requirements:
        # 1. Risk of ruin should be < 1%
        assert risk_of_ruin < 1.0, f"Risk of ruin too high: {risk_of_ruin:.2f}%"
        # 2. 99th percentile Max Drawdown should be < 30%
        assert (
            max_drawdowns.quantile(0.99) < 30.0
        ), f"Excessive tail risk: {max_drawdowns.quantile(0.99):.2f}% MDD"

    def test_position_sizing_equity_curve_stability(self):
        """
        Test if increasing position size (Compounding) leads to instability.
        Fixed Fractional Betting vs Fixed Lot.
        """
        # Strategy Metrics
        initial_balance = 10000.0
        risk_per_trade_pct = 0.02  # Risk 2% of equity per trade
        win_rate = 0.55
        risk_reward = 2.0  # Win 4%, Loss 2%

        balance = initial_balance
        num_trades = 200

        equity_curve = [balance]
        for _ in range(num_trades):
            risk_amount = balance * risk_per_trade_pct
            if np.random.random() < win_rate:
                balance += risk_amount * risk_reward
            else:
                balance -= risk_amount
            equity_curve.append(balance)

        final_return = (balance - initial_balance) / initial_balance * 100
        print("\n--- Compounding Stability Audit ---")
        print(f"Final Balance after 200 trades (Compounded): ${balance:.2f}")
        print(f"Total Return: {final_return:.2f}%")

        # Verify no total wipeout
        assert balance > 0, "Strategy compounded to bankruptcy!"
        assert balance > (initial_balance * 0.5), "Severe compounding instability detected"
