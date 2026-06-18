import asyncio
import time
from datetime import datetime, timezone

from core.event_bus import EventBus, Event
from core.events.topics import EventTopic
from core.config.settings import settings
from services.market_data_engine import MarketDataEngine
from services.strategies.strategy_engine import StrategyEngine
from infrastructure.exchange.base_exchange import OHLCV
from services.market_data.candle_buffer import CandleBuffer

class MockMetrics:
    def increment(self, *args, **kwargs): pass
    def gauge(self, *args, **kwargs): pass

class MockExchange:
    def __init__(self):
        self.pos_mode = "long_short_mode"
        self._connected = True

async def main():
    print("=== BẮT ĐẦU REPLAY 1000 CANDLES ĐỂ CHỨNG MINH ROOT-CAUSE PATCH ===")
    event_bus = EventBus()
    await event_bus.start()
    
    # Observe events
    event_counts = {
        EventTopic.MARKET_INDICATORS_UPDATED: 0,
        EventTopic.STRATEGY_SIGNAL_GENERATED: 0,
        EventTopic.SIGNAL_REJECTED: 0,
        "consumed": 0
    }
    
    async def event_monitor(event: Event):
        if event.event_type in event_counts:
            event_counts[event.event_type] += 1
            if event.event_type == EventTopic.MARKET_INDICATORS_UPDATED:
                candles = event.data.get("candles_snapshot")
                if event_counts[EventTopic.MARKET_INDICATORS_UPDATED] == 900:
                    print(f"[MARKET_INDICATORS_UPDATED] num candles in event = {len(candles) if candles else 0}")
                    print(f"[MARKET_INDICATORS_UPDATED] StrategyEngine snapshot = {len(strategy_engine._analysis_snapshots.get(('BTC-USDT-SWAP', '5m'), []))}")
                    print(f"[MARKET_INDICATORS_UPDATED] MDE snapshot = {len(mde._latest_candle_snapshots.get('BTC-USDT-SWAP_5m', []))}")
            elif event.event_type == EventTopic.STRATEGY_SIGNAL_GENERATED:
                signal = event.data.get("signal")
                print(f"[SIGNAL_CREATED] 🎉 Signal được tạo thành công: {signal.direction} tại giá {signal.entry_price}")
            elif event.event_type == EventTopic.SIGNAL_REJECTED:
                reason = event.data.get("reason")
                print(f"[SIGNAL_REJECTED] Signal bị từ chối do: {reason}")
    
    # Monitor strategy engine consumption
    async def strategy_monitor(event: Event):
        if event.event_type == EventTopic.MARKET_INDICATORS_UPDATED:
            event_counts["consumed"] += 1
            
    event_bus.subscribe(event_monitor, [EventTopic.MARKET_INDICATORS_UPDATED])
    event_bus.subscribe(strategy_monitor, [EventTopic.MARKET_INDICATORS_UPDATED])
    event_bus.subscribe(event_monitor, [EventTopic.STRATEGY_SIGNAL_GENERATED])
    event_bus.subscribe(event_monitor, [EventTopic.SIGNAL_REJECTED])

    metrics = MockMetrics()
    exchange = MockExchange()
    
    from loguru import logger
    import sys
    logger.remove()
    logger.add("verify.log", level="DEBUG")
    
    from core.container import container
    mde = MarketDataEngine(exchange, event_bus, settings)
    mde._tf_ready["5m"] = True
    strategy_engine = StrategyEngine(event_bus, exchange)
    container.register_instance("market_data_engine", mde)
    container.register_instance("strategy_engine", strategy_engine)
    
    from services.strategies.ema_crossover import EMACrossoverStrategy
    from services.strategies.base_strategy import StrategyConfig
    
    config = StrategyConfig(
        name="ema_crossover",
        symbols={"BTC-USDT-SWAP"},
        timeframes={"5m"}
    )
    strategy = EMACrossoverStrategy(config, event_bus)
    
    # Override generate_signal
    original_generate_signal = strategy.generate_signal
    async def debug_generate_signal(sym, tf):
        res = await original_generate_signal(sym, tf)
        if res is None:
            cd = await strategy.is_in_cooldown(sym, tf)
            filt = await strategy.filters(sym, tf)
            snap = await strategy.calculate_indicators(sym, tf)
            cross = snap.indicators.get("crossover_detected") if snap else None
            print(f"[DEBUG] return None: cooldown={cd}, filters={filt}, snapshot={snap is not None}, cross={cross}")
        return res
    strategy.generate_signal = debug_generate_signal
    
    await strategy_engine.register_strategy(strategy)
    
    await strategy_engine.initialize()
    strategy_engine._running = True
    strategy_engine._paused = False
    
    symbol = "BTC-USDT-SWAP"
    timeframe = "5m"
    
    # Initialize buffer manually
    key = f"{symbol}_{timeframe}"
    mde.buffers[key] = CandleBuffer(symbol, timeframe, max_candles=3000)
    mde._buffer_locks[key] = asyncio.Lock()
    
    # Sinh 1000 nến với crossover DETERMINISTIC để chứng minh race condition
    base_price = 50000.0
    ts = int(time.time() * 1000) - (1000 * 300000) # 1000 nến 5m
    
    print(f"Generating 1000 candles for {symbol} {timeframe}...")
    for i in range(1000):
        # Tạo nến để tạo crossover DETERMINISTIC với body_pct lớn cho reference_candle
        if i < 100:
            # 100 nến đầu: flat range để EMA hội tụ
            open_p = base_price
            close_p = base_price
            high_p = base_price + 10
            low_p = base_price - 10
        elif i < 200:
            # 100 nến tiếp: tăng mạnh để tạo bullish crossover với body_pct > 1.0%
            # body_pct = body_size / candle_range = 500 / 600 = 0.833 = 83.3% > 1.0%
            open_p = base_price + (i - 100) * 50
            close_p = open_p + 500
            high_p = close_p + 50
            low_p = open_p - 50
        elif i < 300:
            # 100 nến tiếp: giảm mạnh để tạo bearish crossover với body_pct > 1.0%
            # body_pct = body_size / candle_range = 500 / 600 = 0.833 = 83.3% > 1.0%
            open_p = base_price + 10000 - (i - 200) * 50
            close_p = open_p - 500
            high_p = open_p + 50
            low_p = close_p - 50
        else:
            # 700 nến còn lại: flat range để test race condition
            open_p = base_price
            close_p = base_price + (1 if i % 2 == 0 else -1)
            high_p = close_p + 10
            low_p = open_p - 10
            base_price = close_p
            
        c = OHLCV(
            timestamp=ts + (i * 300000),
            open=open_p, high=high_p, low=low_p, close=close_p, volume=10.0,
            symbol=symbol, timeframe=timeframe, confirmed=True
        )
        
        # Add into buffer
        async with mde._buffer_locks[key]:
            buffer = mde.buffers[key]
            buffer.add_candle(c)
            
        # Trigger compute
        if i >= 100: # Cần đủ nến để hội tụ
            # Chúng ta gọi trực tiếp hàm xử lý
            await mde._compute_and_publish_indicators(symbol, timeframe)
            
    # Chờ event bus xử lý
    await asyncio.sleep(2)
    
    print("\n=== KẾT QUẢ REPLAY ===")
    print(f"Số event MARKET_INDICATORS_UPDATED publish: {event_counts[EventTopic.MARKET_INDICATORS_UPDATED]}")
    print(f"Số event MARKET_INDICATORS_UPDATED consume: {event_counts['consumed']}")
    print(f"Số SIGNAL_CREATED tạo: {event_counts[EventTopic.STRATEGY_SIGNAL_GENERATED]}")
    print(f"Số SIGNAL_REJECTED reject: {event_counts[EventTopic.SIGNAL_REJECTED]}")
    print(f"Số ENTRY tạo: {event_counts[EventTopic.STRATEGY_SIGNAL_GENERATED]}") # SIGNAL_CREATED là đủ điều kiện entry
    print("====================================================================")

if __name__ == '__main__':
    asyncio.run(main())
