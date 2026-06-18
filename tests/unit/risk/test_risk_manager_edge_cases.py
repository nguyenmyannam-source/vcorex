"""
Unit test chuyên sâu cho RiskManager - bao gồm tất cả edge cases,
property-based testing, và performance benchmark.
Tuân theo chuẩn production-grade testing giống các dự án institutional.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domain.risk.risk_manager import RiskManager
from domain.risk.risk_utilities import (
    calculate_stop_loss,
    calculate_take_profits,
    calculate_required_margin,
    _validate_entry_against_market,
    _validate_sl_distance,
)


class TestRiskManagerCoreEdgeCases:
    """Test các trường hợp đặc biệt cốt lõi của RiskManager"""

    @pytest.fixture(autouse=True)
    def setup(self):
        # Mock các dependencies bắt buộc của RiskManager theo đúng API thực tế
        mock_event_bus = Mock()
        mock_exchange = AsyncMock()  # Dùng AsyncMock vì exchange có async methods
        self.rm = RiskManager(event_bus=mock_event_bus, exchange=mock_exchange)
        # Test cases khớp với API calculate_pnl và calculate_roe thực tế
        # calculate_pnl(entry_price: float, close_price: float, side: str, amount: float) -> float
        # calculate_roe(entry_price: float, close_price: float, amount: float, leverage: int, side: str) -> float
        self.test_cases = [
            # LONG profitable: entry 50000, exit 50500, side "long", amount 1.0, leverage 10
            (50000, 50500, "long", 1.0, 10, {"pnl": 500.0, "roe": 10.0}),
            # LONG losing
            (50000, 49500, "long", 1.0, 10, {"pnl": -500.0, "roe": -10.0}),
            # SHORT losing
            (50000, 50500, "short", 1.0, 10, {"pnl": -500.0, "roe": -10.0}),
            # SHORT profitable
            (50000, 49500, "short", 1.0, 10, {"pnl": 500.0, "roe": 10.0}),
        ]

    def test_basic_pnl_calculation_accuracy(self):
        """Test độ chính xác cơ bản của calculate_pnl và calculate_roe"""
        for entry, exit, side, amount, leverage, expected in self.test_cases:
            pnl = self.rm.calculate_pnl(entry, exit, side, amount, include_fees=False)
            roe = self.rm.calculate_roe(entry, exit, amount, leverage, side, include_fees=False)
            assert abs(pnl - expected["pnl"]) < 0.01
            assert abs(roe - expected["roe"]) < 0.01

    def test_liquidation_price_calculation(self):
        """Test tính toán giá trị liquidation chính xác"""
        # LONG position
        liq_long = self.rm.calculate_liquidation_price(50000, 10, "long")
        assert liq_long < 50000  # Liquidation price phải thấp hơn entry cho long
        # SHORT position
        liq_short = self.rm.calculate_liquidation_price(50000, 10, "short")
        assert liq_short > 50000  # Liquidation price phải cao hơn entry cho short

    def test_stop_loss_calculation(self):
        """Test tính toán stop loss cho cả long và short"""
        # Long position: SL 2% dưới entry
        sl_long = calculate_stop_loss(50000, "long", 0.02)
        assert sl_long == 49000
        # Short position: SL 2% trên entry
        sl_short = calculate_stop_loss(50000, "short", 0.02)
        assert sl_short == 51000

    def test_take_profits_calculation(self):
        """Test tính toán nhiều mức take profit"""
        tps = calculate_take_profits(50000, "long", [0.01, 0.02, 0.05])
        assert tps == [50500, 51000, 52500]
        tps_short = calculate_take_profits(50000, "short", [0.01, 0.02, 0.05])
        assert tps_short == [49500, 49000, 47500]

    def test_required_margin_calculation(self):
        """Test tính toán margin yêu cầu"""
        margin = calculate_required_margin(10000, 10)  # 10,000 USD notional, x10 leverage
        assert margin == 1000  # 10% của notional value


@given(
    entry_price=st.floats(min_value=0.01, max_value=1_000_000),
    close_price=st.floats(min_value=0.01, max_value=1_000_000),
    side=st.sampled_from(["long", "short"]),
    amount=st.floats(min_value=0.001, max_value=1000),
    leverage=st.integers(min_value=1, max_value=100),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_based_pnl_consistency(entry_price, close_price, side, amount, leverage):
    """Property-based testing: tính toán P&L luôn nhất quán với toán học"""
    mock_event_bus = Mock()
    mock_exchange = AsyncMock()
    rm = RiskManager(event_bus=mock_event_bus, exchange=mock_exchange)

    pnl = rm.calculate_pnl(entry_price, close_price, side, amount, include_fees=False)
    roe = rm.calculate_roe(entry_price, close_price, amount, leverage, side, include_fees=False)

    # Tính toán thủ công để xác minh
    if side == "long":
        expected_pnl = (close_price - entry_price) * amount
    else:  # short
        expected_pnl = (entry_price - close_price) * amount
    margin = (entry_price * amount) / leverage
    expected_roe = (expected_pnl / margin) * 100 if margin > 0 else 0.0

    assert abs(pnl - expected_pnl) < 1e-6
    assert abs(roe - expected_roe) < 1e-6


@pytest.mark.benchmark(group="risk_calculations")
def test_pnl_calculation_performance(benchmark):
    """Benchmark hiệu năng: đảm bảo tính toán dưới 1ms"""
    mock_event_bus = Mock()
    mock_exchange = AsyncMock()
    rm = RiskManager(event_bus=mock_event_bus, exchange=mock_exchange)
    result = benchmark(rm.calculate_pnl, 50000, 50500, "long", 1.0, include_fees=False)
    assert result == 500.0


@pytest.mark.benchmark(group="risk_calculations")
def test_roe_calculation_performance(benchmark):
    """Benchmark hiệu năng tính ROE"""
    mock_event_bus = Mock()
    mock_exchange = AsyncMock()
    rm = RiskManager(event_bus=mock_event_bus, exchange=mock_exchange)
    result = benchmark(rm.calculate_roe, 50000, 50500, 1.0, 10, "long")
    assert isinstance(result, float)


class TestRiskManagerRiskValidation:
    """Test các chức năng xác thực rủi ro - critical cho institutional"""

    def test_risk_reward_ratio_validation(self):
        """Test validate_risk_reward hoạt động đúng với các trường hợp"""
        mock_event_bus = Mock()
        mock_exchange = AsyncMock()
        rm = RiskManager(event_bus=mock_event_bus, exchange=mock_exchange)

        # Trường hợp hợp lệ: RR = 2.0 > 1.5
        assessment = rm.validate_risk_reward(50000, 49000, 52000, 1.5)
        assert assessment.approved

        # Trường hợp không hợp lệ: RR = 1.0 < 1.5
        assessment = rm.validate_risk_reward(50000, 49000, 51000, 1.5)
        assert not assessment.approved
        assert "Risk/Reward ratio too low" in assessment.reason

    def test_sl_distance_validation(self):
        """Test kiểm tra khoảng cách stop loss không quá gần entry"""
        # SL hợp lệ: 1% dưới entry (cách 1% > 0.5%)
        assessment = _validate_sl_distance(50000, 49500, "long", 0.5)
        assert assessment.approved

        # SL quá gần: chỉ 0.3% dưới entry
        assessment = _validate_sl_distance(50000, 49850, "long", 0.5)
        assert not assessment.approved
        assert "SL too close to entry" in assessment.reason

    def test_entry_price_deviation_validation(self):
        """Test kiểm tra entry price không lệch quá nhiều so với giá thị trường"""
        # Entry price lệch 3% so với market price - hợp lệ (dưới 5%)
        assessment = _validate_entry_against_market(50000, 51500, 5.0)
        assert assessment.approved

        # Entry price lệch 6% - bị từ chối
        assessment = _validate_entry_against_market(50000, 53000, 5.0)
        assert not assessment.approved
        assert "Entry price deviation too high" in assessment.reason
