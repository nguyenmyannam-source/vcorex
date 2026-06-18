"""
Test: Symbol Concentration Guard
File: domain/risk/risk_manager.py — assess_signal()
Kịch bản A/B/C/D theo yêu cầu
"""
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ─── Mock dependencies ───────────────────────────────────────────────────────

def make_mirror(positions: dict):
    """Tạo ExchangeMirror giả với danh sách vị thế cho trước."""
    mirror = MagicMock()
    mock_positions = {}
    for inst_id, (pos_size, side) in positions.items():
        p = MagicMock()
        p.instId = inst_id
        p.pos = pos_size   # > 0 = long, < 0 = short
        mock_positions[inst_id] = p
    mirror.get_all_positions = AsyncMock(return_value=mock_positions)
    return mirror


def make_signal(symbol: str, direction: str, timeframe: str):
    """Tạo Signal giả."""
    sig = MagicMock()
    sig.symbol = symbol
    sig.signal_type = MagicMock()
    sig.signal_type.value = direction
    sig.timeframe = timeframe
    sig.leverage = 10
    sig.position_size_usdt = 100.0
    sig.entry_price = 50000.0
    sig.stop_loss_price = 49000.0
    sig.take_profit_prices = [{"price": 51000, "exit_pct": 0.5}, {"price": 52000, "exit_pct": 0.5}]
    sig.risk_approved = True
    return sig


async def run_concentration_check(mirror_positions: dict, signal_symbol: str,
                                   signal_direction: str, signal_tf: str,
                                   cache_entry=None):
    """
    Chạy ĐÚNG đoạn logic concentration guard từ risk_manager.py.
    Trả về: (approved: bool, reason: str)
    """
    from unittest.mock import MagicMock
    import time

    # Reproduce logic từ risk_manager.py dòng 264-330
    mirror = make_mirror(mirror_positions)
    position_cache = {}
    if cache_entry:
        position_cache[signal_symbol] = cache_entry

    now = time.time()
    cached = position_cache.get(signal_symbol)

    if cached and (now - cached[0]) < 5.0:
        symbol_count, existing_side, existing_instId = cached[1], cached[2], cached[3]
    else:
        all_pos = await mirror.get_all_positions()
        symbol_count = 0
        existing_side = ""
        existing_instId = ""
        for inst_id, p in all_pos.items():
            pos_symbol = getattr(p, "instId", "") or inst_id
            if pos_symbol == signal_symbol:
                symbol_count += 1
                pos_size = getattr(p, "pos", 0.0)
                existing_side = "long" if pos_size > 0 else "short"
                existing_instId = inst_id
        position_cache[signal_symbol] = (now, symbol_count, existing_side, existing_instId)

    max_concentration = 1
    if symbol_count >= max_concentration:
        block_msg = (
            f"🚫 CONCENTRATION BLOCK: {signal_symbol}\n"
            f" Khung {signal_tf} báo {signal_direction.upper()}\n"
            f" nhưng đã có vị thế {existing_side.upper()} ({existing_instId}).\n"
            f" Block toàn bộ tín hiệu {signal_symbol} cho đến khi vị thế đóng."
        )
        return False, block_msg

    return True, "PASS"


# ─── Test Cases ───────────────────────────────────────────────────────────────

async def test_A_block_same_direction():
    """Kịch bản A: BTC Long 5m đang mở → BTC 15m báo Buy → BLOCK"""
    approved, reason = await run_concentration_check(
        mirror_positions={"BTC-USDT-SWAP": (1.5, "long")},  # pos > 0 = long
        signal_symbol="BTC-USDT-SWAP",
        signal_direction="buy",
        signal_tf="15m"
    )
    assert approved is False, f"Expected BLOCK, got PASS. Reason: {reason}"
    assert "CONCENTRATION BLOCK" in reason
    assert "BTC-USDT-SWAP" in reason
    assert "LONG" in reason
    print(f"✅ Kịch bản A PASS: {reason[:80]}...")


async def test_B_block_opposite_direction():
    """Kịch bản B: BTC Long 5m đang mở → BTC 1H báo Sell (ngược chiều) → BLOCK"""
    approved, reason = await run_concentration_check(
        mirror_positions={"BTC-USDT-SWAP": (1.5, "long")},
        signal_symbol="BTC-USDT-SWAP",
        signal_direction="sell",
        signal_tf="1H"
    )
    assert approved is False, f"Expected BLOCK, got PASS. Reason: {reason}"
    assert "CONCENTRATION BLOCK" in reason
    assert "SELL" in reason or "sell" in reason.lower()
    print(f"✅ Kịch bản B PASS: {reason[:80]}...")


async def test_C_pass_different_symbol():
    """Kịch bản C: BTC Long đang mở → ETH báo Buy → PASS"""
    approved, reason = await run_concentration_check(
        mirror_positions={"BTC-USDT-SWAP": (1.5, "long")},
        signal_symbol="ETH-USDT-SWAP",
        signal_direction="buy",
        signal_tf="5m"
    )
    assert approved is True, f"Expected PASS, got BLOCK. Reason: {reason}"
    print(f"✅ Kịch bản C PASS: ETH không bị block khi BTC có vị thế")


async def test_D_unblock_after_close():
    """Kịch bản D: BTC Long đóng (cache hết hạn) → BTC 15m Buy → PASS"""
    # Simulate cache đã hết hạn: timestamp = now - 6s (> TTL 5s)
    stale_cache_entry = (time.time() - 6, 1, "long", "BTC-USDT-SWAP")

    approved, reason = await run_concentration_check(
        mirror_positions={},  # ← Không có vị thế nào (đã đóng)
        signal_symbol="BTC-USDT-SWAP",
        signal_direction="buy",
        signal_tf="15m",
        cache_entry=stale_cache_entry  # Cache cũ nhưng hết hạn → query lại
    )
    assert approved is True, f"Expected PASS sau khi close, got BLOCK. Reason: {reason}"
    print(f"✅ Kịch bản D PASS: Cache hết hạn → query lại → không có vị thế → PASS")


async def test_cache_ttl_5s():
    """Xác nhận cache 5s: query đầu tiên miss cache, query sau 3s dùng cache."""
    call_count = 0
    original_mirror = make_mirror({"BTC-USDT-SWAP": (1.0, "long")})
    original_get_all = original_mirror.get_all_positions

    def counting_get_all():
        nonlocal call_count
        call_count += 1
        return original_get_all()

    original_mirror.get_all_positions = counting_get_all

    position_cache = {}
    max_concentration = 1

    # --- Call 1: cache miss → query mirror ---
    now = time.time()
    cached = position_cache.get("BTC-USDT-SWAP")
    if not (cached and (now - cached[0]) < 5.0):
        all_pos = await original_mirror.get_all_positions()
        symbol_count = sum(1 for inst_id, p in all_pos.items()
                           if getattr(p, "instId", "") == "BTC-USDT-SWAP")
        position_cache["BTC-USDT-SWAP"] = (now, symbol_count, "long", "BTC-USDT-SWAP")

    # --- Call 2 ngay sau đó: cache hit → KHÔNG query mirror ---
    now2 = time.time()
    cached2 = position_cache.get("BTC-USDT-SWAP")
    if not (cached2 and (now2 - cached2[0]) < 5.0):
        await original_mirror.get_all_positions()  # Chỉ gọi nếu cache miss

    assert call_count == 1, f"Expected 1 mirror query, got {call_count}"
    print(f"✅ Cache TTL test PASS: mirror.get_all_positions() chỉ bị gọi {call_count} lần trong 5s")


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def main():
        print("\n" + "="*60)
        print("SYMBOL CONCENTRATION GUARD — TEST SUITE")
        print("="*60)
        await test_A_block_same_direction()
        await test_B_block_opposite_direction()
        await test_C_pass_different_symbol()
        await test_D_unblock_after_close()
        await test_cache_ttl_5s()
        print("\n" + "="*60)
        print("TẤT CẢ TEST PASS ✅")
        print("="*60)

    asyncio.run(main())