"""Unit tests for TP/SL resolution from OKX mirror + algo orders."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pytest

from services.position.exchange_mirror import MirrorPosition
from services.position.tpsl_resolver import (
    build_algo_tpsl_map,
    enrich_raw_position_dict,
    extract_tpsl_from_raw_position,
    merge_tpsl,
    parse_algo_px,
)


@dataclass
class _FakeTP:
    price: float


@dataclass
class _FakeLocalPos:
    stop_loss: Optional[float] = None
    take_profit_levels: List[_FakeTP] = field(default_factory=list)


class _FakeExchange:
    def __init__(self, orders_by_type):
        self._orders_by_type = orders_by_type

    async def fetch_pending_algo_orders(self, limit=200, ord_type=None, symbol=None):
        return self._orders_by_type.get(ord_type, [])


def test_parse_algo_px_ignores_empty_and_market():
    assert parse_algo_px(None) is None
    assert parse_algo_px("") is None
    assert parse_algo_px("-1") is None
    assert parse_algo_px("65000.5") == 65000.5


def test_extract_tpsl_from_raw_position_top_level():
    raw = {"tpTriggerPx": "61000", "slTriggerPx": "62000"}
    sl, tps = extract_tpsl_from_raw_position(raw)
    assert sl == 62000.0
    assert tps == [61000.0]


def test_extract_tpsl_from_close_order_algo():
    raw = {
        "tpTriggerPx": "",
        "slTriggerPx": "",
        "closeOrderAlgo": [
            {"tpTriggerPx": "1.05", "slTriggerPx": ""},
            {"tpTriggerPx": "", "slTriggerPx": "1.10"},
        ],
    }
    sl, tps = extract_tpsl_from_raw_position(raw)
    assert sl == 1.10
    assert tps == [1.05]


@pytest.mark.asyncio
async def test_build_algo_tpsl_map_groups_by_inst():
    exchange = _FakeExchange(
        {
            "conditional": [
                {"instId": "BTC-USDT-SWAP", "algoId": "a1", "tpTriggerPx": "60000", "slTriggerPx": ""},
                {"instId": "BTC-USDT-SWAP", "algoId": "a2", "tpTriggerPx": "", "slTriggerPx": "62000"},
                {"instId": "XRP-USDT-SWAP", "algoId": "b1", "tpTriggerPx": "1.2", "slTriggerPx": "1.3"},
            ],
            "oco": [],
            "move_order_stop": [],
            "trigger": [],
        }
    )
    algo_map = await build_algo_tpsl_map(exchange)
    assert algo_map["BTC-USDT-SWAP"]["tp_prices"] == [60000.0]
    assert algo_map["BTC-USDT-SWAP"]["sl_prices"] == [62000.0]
    assert algo_map["XRP-USDT-SWAP"]["tp_prices"] == [1.2]
    assert algo_map["XRP-USDT-SWAP"]["sl_prices"] == [1.3]


def test_merge_tpsl_prefers_local_then_mirror_then_algo():
    local = _FakeLocalPos(stop_loss=59000.0, take_profit_levels=[_FakeTP(61000.0)])
    mirror = MirrorPosition(
        instId="BTC-USDT-SWAP",
        pos=-1.0,
        avgPx=60000.0,
        upl=10.0,
        uplLastPx=10.0,
        uplRatio=0.01,
        margin=100.0,
        markPx=59900.0,
        liqPx=70000.0,
        cTime=1,
        uTime=2,
        tpTriggerPx=60500.0,
        slTriggerPx=59500.0,
        tpPrices=(60500.0, 61500.0),
    )
    algo = {"tp_prices": [62000.0], "sl_prices": [58000.0]}

    has_sl, has_tp, sl_price, tp_prices = merge_tpsl(local, mirror, algo)
    assert has_sl is True
    assert has_tp is True
    assert sl_price == 59000.0
    assert tp_prices == [60500.0, 61000.0, 61500.0, 62000.0]


def test_merge_tpsl_algo_only_when_local_and_mirror_empty():
    mirror = MirrorPosition(
        instId="XRP-USDT-SWAP",
        pos=-10.0,
        avgPx=1.0,
        upl=1.0,
        uplLastPx=1.0,
        uplRatio=0.01,
        margin=10.0,
        markPx=1.0,
        liqPx=2.0,
        cTime=1,
        uTime=2,
    )
    algo = {"tp_prices": [0.95], "sl_prices": [1.05]}

    has_sl, has_tp, sl_price, tp_prices = merge_tpsl(None, mirror, algo)
    assert has_sl is True
    assert has_tp is True
    assert sl_price == 1.05
    assert tp_prices == [0.95]


def test_enrich_raw_position_dict_fills_missing_from_algo():
    raw = {"instId": "BTC-USDT-SWAP", "pos": "-1"}
    enrich_raw_position_dict(raw, {"tp_prices": [60000.0], "sl_prices": [62000.0]})
    assert raw["tpTriggerPx"] == "60000.0"
    assert raw["slTriggerPx"] == "62000.0"
