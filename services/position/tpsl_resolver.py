"""Resolve TP/SL from local RAM, exchange mirror, and OKX pending algo orders."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from services.position.exchange_mirror import MirrorPosition
    from services.position.models import TrackedPosition

ALGO_ORD_TYPES = ("conditional", "oco", "move_order_stop", "trigger")


def parse_algo_px(value: Any) -> Optional[float]:
    """Parse OKX algo trigger price; ignore empty and market (-1)."""
    if value is None or value == "" or value == "-1":
        return None
    try:
        px = float(value)
        return px if px > 0 else None
    except (TypeError, ValueError):
        return None


async def build_algo_tpsl_map(exchange) -> Dict[str, Dict[str, Any]]:
    """
    Fetch pending algo orders from OKX and group TP/SL by instId.
    Returns: {instId: {tp_prices: [...], sl_prices: [...], algo_ids: [...]}}
    """
    grouped: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"tp_prices": [], "sl_prices": [], "algo_ids": []}
    )

    if not exchange or not hasattr(exchange, "fetch_pending_algo_orders"):
        return {}

    seen_algo: set[str] = set()

    for ord_type in ALGO_ORD_TYPES:
        try:
            orders = await exchange.fetch_pending_algo_orders(limit=200, ord_type=ord_type)
        except Exception as e:
            logger.debug("[TPSL] fetch_pending_algo_orders ordType={} failed: {}", ord_type, e)
            continue

        for order in orders or []:
            inst = order.get("instId")
            if not inst:
                continue

            algo_id = order.get("algoId") or order.get("algoOrderId")
            if algo_id and algo_id not in seen_algo:
                seen_algo.add(algo_id)
                grouped[inst]["algo_ids"].append(str(algo_id))

            tp = parse_algo_px(order.get("tpTriggerPx"))
            sl = parse_algo_px(order.get("slTriggerPx"))
            trigger = parse_algo_px(order.get("triggerPx"))
            if tp and tp not in grouped[inst]["tp_prices"]:
                grouped[inst]["tp_prices"].append(tp)
            if sl and sl not in grouped[inst]["sl_prices"]:
                grouped[inst]["sl_prices"].append(sl)
            # Standalone trigger orders (no tp/sl fields) — treat as SL by default
            if trigger and not tp and not sl:
                if trigger not in grouped[inst]["sl_prices"]:
                    grouped[inst]["sl_prices"].append(trigger)

    return dict(grouped)


def extract_tpsl_from_raw_position(raw: Dict[str, Any]) -> Tuple[Optional[float], List[float]]:
    """
    Parse TP/SL from OKX position payload (REST/WS).
    Includes top-level tpTriggerPx/slTriggerPx and closeOrderAlgo[].
    """
    sl_price = parse_algo_px(raw.get("slTriggerPx"))
    tp_prices: List[float] = []

    tp_top = parse_algo_px(raw.get("tpTriggerPx"))
    if tp_top:
        tp_prices.append(tp_top)

    for algo in raw.get("closeOrderAlgo") or []:
        if not isinstance(algo, dict):
            continue
        tp = parse_algo_px(algo.get("tpTriggerPx"))
        sl = parse_algo_px(algo.get("slTriggerPx"))
        if tp and tp not in tp_prices:
            tp_prices.append(tp)
        if sl and (sl_price is None or sl_price <= 0):
            sl_price = sl

    return sl_price, sorted(tp_prices)


def merge_tpsl(
    local_pos: Optional["TrackedPosition"],
    mirror_pos: Optional["MirrorPosition"],
    algo_info: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, bool, float, List[float]]:
    """
    Merge TP/SL from local position, mirror snapshot, and pending algo orders.
    Returns: (has_sl, has_tp, sl_price, tp_prices)
    """
    sl_price: Optional[float] = None
    tp_prices: List[float] = []

    if local_pos is not None:
        if local_pos.stop_loss is not None and local_pos.stop_loss > 0:
            sl_price = float(local_pos.stop_loss)
        for tp in local_pos.take_profit_levels or []:
            px = float(tp.price)
            if px > 0 and px not in tp_prices:
                tp_prices.append(px)

    if mirror_pos is not None:
        mirror_tp_list = list(getattr(mirror_pos, "tpPrices", ()) or ())
        if not mirror_tp_list and mirror_pos.tpTriggerPx:
            mirror_tp_list = [float(mirror_pos.tpTriggerPx)]
        if mirror_pos.slTriggerPx and (sl_price is None or sl_price <= 0):
            sl_price = float(mirror_pos.slTriggerPx)
        for px in mirror_tp_list:
            if px > 0 and px not in tp_prices:
                tp_prices.append(px)

    if algo_info:
        for sl in algo_info.get("sl_prices") or []:
            if sl_price is None or sl_price <= 0:
                sl_price = float(sl)
                break
        for tp in algo_info.get("tp_prices") or []:
            px = float(tp)
            if px > 0 and px not in tp_prices:
                tp_prices.append(px)

    has_sl = sl_price is not None and sl_price > 0
    has_tp = len(tp_prices) > 0
    return has_sl, has_tp, sl_price or 0.0, sorted(tp_prices)


def enrich_raw_position_dict(
    raw: Dict[str, Any], algo_info: Optional[Dict[str, Any]]
) -> None:
    """Fill tpTriggerPx/slTriggerPx on mirror raw dict from algo orders if missing."""
    sl_px, tp_list = extract_tpsl_from_raw_position(raw)
    if not tp_list and algo_info and algo_info.get("tp_prices"):
        tp_list = list(algo_info["tp_prices"])
        raw["tpTriggerPx"] = str(tp_list[0])
    if (sl_px is None or sl_px <= 0) and algo_info and algo_info.get("sl_prices"):
        sl_px = algo_info["sl_prices"][0]
        raw["slTriggerPx"] = str(sl_px)
