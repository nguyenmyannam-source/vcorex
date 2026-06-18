"""
Counter-example test cho ULTRA-DEEP FALSIFICATION AUDIT
Test các trường hợp EMA crossover với confirmation_candles=0 và =1
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.market_data.indicators import IndicatorPipeline
from services.market_data_engine import CandleBuffer
from dataclasses import dataclass

# Mock OHLCV class tương tự như trong codebase
@dataclass
class MockOHLCV:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0  # Thêm volume bắt buộc theo interface OHLCV gốc

class MockBuffer(CandleBuffer):
    def __init__(self, symbol, timeframe, candles):
        self.symbol = symbol
        self.timeframe = timeframe
        self._candles = candles
    
    def get_candles(self, limit=100):
        return self._candles[-limit:] if limit < len(self._candles) else self._candles
    
    def get_close_prices(self, limit=100):
        closes = [c.close for c in self._candles]
        return closes[-limit:] if limit < len(closes) else closes
    
    def get_high_prices(self, limit=100):
        highs = [c.high for c in self._candles]
        return highs[-limit:] if limit < len(highs) else highs
    
    def get_low_prices(self, limit=100):
        lows = [c.low for c in self._candles]
        return lows[-limit:] if limit < len(lows) else lows

def create_test_cases():
    """Tạo các test case theo yêu cầu"""
    pipeline = IndicatorPipeline()
    
    # Tạo base candles với đủ dữ liệu để tính EMA
    base_price = 100.0
    base_candles = []
    for i in range(30):  # Tạo 30 candles base để EMA ổn định
        base_candles.append(MockOHLCV(
            timestamp=1620000000 + i*300,
            open=base_price + i*0.1,
            high=base_price + i*0.1 + 1,
            low=base_price + i*0.1 - 1,
            close=base_price + i*0.1
        ))
    
    print("="*80)
    print("ULTRA-DEEP FALSIFICATION AUDIT - COUNTER-EXAMPLE TESTS")
    print("="*80)
    
    # ========== CASE A: EMA9 cross EMA21 tại candle[-1] (realtime crossover) ==========
    print("\n[CASE A] EMA9 cross EMA21 TẠI CANDLE[-1] (confirmation_candles=0 - realtime mode)")
    print("-"*60)
    candles_a = base_candles.copy()
    # Thêm 3 candles cuối với crossover tại candle cuối
    candles_a.extend([
        MockOHLCV(1620009000, 103.0, 104.0, 102.0, 103.0),  # candle[-3]
        MockOHLCV(1620009300, 103.2, 104.2, 102.2, 103.2),  # candle[-2] - EMA9 chưa vượt EMA21
        MockOHLCV(1620009600, 105.0, 106.0, 104.0, 105.5),  # candle[-1] - EMA9 vượt EMA21 tại đây
    ])
    buffer_a = MockBuffer("BTC-USDT-SWAP", "5m", candles_a)
    snapshot_a = pipeline.compute_indicators(buffer_a, confirmation_candles=0)
    print(f"reference_candle_index: {snapshot_a.reference_candle_index}")
    print(f"candle_type: {snapshot_a.candle_type}")
    print(f"crossover_detected: {snapshot_a.indicators.get('crossover_detected', 0)}")
    print(f"crossover_bullish: {snapshot_a.indicators.get('crossover_bullish', 0)}")
    print(f"crossover_bearish: {snapshot_a.indicators.get('crossover_bearish', 0)}")
    
    # ========== CASE A-1: Cùng dữ liệu nhưng confirmation_candles=1 ==========
    print("\n[CASE A-1] Cùng dữ liệu Case A nhưng confirmation_candles=1")
    print("-"*60)
    snapshot_a1 = pipeline.compute_indicators(buffer_a, confirmation_candles=1)
    print(f"reference_candle_index: {snapshot_a1.reference_candle_index}")
    print(f"candle_type: {snapshot_a1.candle_type}")
    print(f"crossover_detected: {snapshot_a1.indicators.get('crossover_detected', 0)}")
    print(f"crossover_bullish: {snapshot_a1.indicators.get('crossover_bullish', 0)}")
    print(f"crossover_bearish: {snapshot_a1.indicators.get('crossover_bearish', 0)}")
    
    # ========== CASE B: EMA9 cross EMA21 tại candle[-2] (confirmation crossover) ==========
    print("\n[CASE B] EMA9 cross EMA21 TẠI CANDLE[-2] (confirmation_candles=1 - confirmation mode)")
    print("-"*60)
    candles_b = base_candles.copy()
    candles_b.extend([
        MockOHLCV(1620009000, 103.0, 104.0, 102.0, 103.0),  # candle[-3]
        MockOHLCV(1620009300, 104.0, 105.0, 103.0, 105.0),  # candle[-2] - crossover xảy ra ở đây
        MockOHLCV(1620009600, 105.5, 106.5, 104.5, 106.0),  # candle[-1] - đang forming
    ])
    buffer_b = MockBuffer("BTC-USDT-SWAP", "5m", candles_b)
    snapshot_b = pipeline.compute_indicators(buffer_b, confirmation_candles=1)
    print(f"reference_candle_index: {snapshot_b.reference_candle_index}")
    print(f"candle_type: {snapshot_b.candle_type}")
    print(f"crossover_detected: {snapshot_b.indicators.get('crossover_detected', 0)}")
    print(f"crossover_bullish: {snapshot_b.indicators.get('crossover_bullish', 0)}")
    print(f"crossover_bearish: {snapshot_b.indicators.get('crossover_bearish', 0)}")
    
    # ========== CASE C: EMA9 > EMA21 nhưng KHÔNG có crossover ==========
    print("\n[CASE C] EMA9 > EMA21 nhưng KHÔNG CÓ CROSSOVER (đã cross từ trước)")
    print("-"*60)
    candles_c = base_candles.copy()
    # EMA9 đã lớn hơn EMA21 từ nhiều candle trước
    for i in range(5):
        candles_c.append(MockOHLCV(
            timestamp=1620008000 + i*300,
            open=110.0 + i*0.5,
            high=111.0 + i*0.5,
            low=109.0 + i*0.5,
            close=110.5 + i*0.5
        ))
    buffer_c = MockBuffer("BTC-USDT-SWAP", "5m", candles_c)
    snapshot_c = pipeline.compute_indicators(buffer_c, confirmation_candles=0)
    print(f"reference_candle_index: {snapshot_c.reference_candle_index}")
    print(f"ema9: {snapshot_c.indicators.get('ema9', 0):.4f}")
    print(f"ema21: {snapshot_c.indicators.get('ema21', 0):.4f}")
    print(f"EMA9 > EMA21: {snapshot_c.indicators.get('ema9',0) > snapshot_c.indicators.get('ema21',0)}")
    print(f"crossover_detected: {snapshot_c.indicators.get('crossover_detected', 0)}")
    print(f"crossover_bullish: {snapshot_c.indicators.get('crossover_bullish', 0)}")
    print(f"crossover_bearish: {snapshot_c.indicators.get('crossover_bearish', 0)}")
    
    # ========== CASE D: EMA9 < EMA21 nhưng crossover flag cũ còn tồn tại ==========
    print("\n[CASE D] EMA9 < EMA21 nhưng crossover flag cũ (nếu có)")
    print("-"*60)
    candles_d = base_candles.copy()
    # Đảo ngược: EMA9 giờ nhỏ hơn EMA21, không có crossover mới
    candles_d.extend([
        MockOHLCV(1620009000, 105.0, 106.0, 104.0, 104.5),
        MockOHLCV(1620009300, 104.0, 105.0, 103.0, 103.5),
        MockOHLCV(1620009600, 103.0, 104.0, 102.0, 102.5),
    ])
    buffer_d = MockBuffer("BTC-USDT-SWAP", "5m", candles_d)
    snapshot_d = pipeline.compute_indicators(buffer_d, confirmation_candles=0)
    print(f"reference_candle_index: {snapshot_d.reference_candle_index}")
    print(f"ema9: {snapshot_d.indicators.get('ema9', 0):.4f}")
    print(f"ema21: {snapshot_d.indicators.get('ema21', 0):.4f}")
    print(f"EMA9 < EMA21: {snapshot_d.indicators.get('ema9',0) < snapshot_d.indicators.get('ema21',0)}")
    print(f"crossover_detected: {snapshot_d.indicators.get('crossover_detected', 0)}")
    print(f"crossover_bullish: {snapshot_d.indicators.get('crossover_bullish', 0)}")
    print(f"crossover_bearish: {snapshot_d.indicators.get('crossover_bearish', 0)}")
    
    print("\n" + "="*80)
    print("TỔNG KẾT COUNTER-EXAMPLE TESTS")
    print("="*80)

if __name__ == "__main__":
    create_test_cases()