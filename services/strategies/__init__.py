"""
Strategies module containing all trading strategy implementations and the strategy engine.
Implements a plugin-based system where new strategies can be added without modifying core engine.
"""

from .base_strategy import BaseStrategy, Signal, SignalStrength, SignalType, StrategyConfig
from .ema_crossover import EMACrossoverStrategy
from .strategy_engine import StrategyEngine

__all__ = [
    "BaseStrategy",
    "Signal",
    "SignalType",
    "SignalStrength",
    "StrategyConfig",
    "EMACrossoverStrategy",
    "StrategyEngine",
]
