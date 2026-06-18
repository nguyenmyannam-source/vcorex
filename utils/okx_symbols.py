"""
Danh sách 20 coin chính thức, uy tín nhất trên OKX DEMO chuẩn FUTURES (SWAP).
Tất cả symbol names đều theo chuẩn OKX SWAP chính thức.
Thông số được cập nhật cho chế độ Isolated Margin và tính toán số lượng hợp đồng (Contract Size).
"""

from typing import Dict, List

# 20 cặp Futures (SWAP) uy tín nhất trên OKX
OKX_TOP20_COINS: List[str] = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "BNB-USDT-SWAP",
    "XRP-USDT-SWAP",
    "ADA-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "AVAX-USDT-SWAP",
    "LINK-USDT-SWAP",
    "ETC-USDT-SWAP",
    "DOT-USDT-SWAP",
    "LTC-USDT-SWAP",
    "BCH-USDT-SWAP",
    "TRX-USDT-SWAP",
    "ATOM-USDT-SWAP",
    "NEAR-USDT-SWAP",
    "FIL-USDT-SWAP",
    "SUI-USDT-SWAP",
    "ARB-USDT-SWAP",
    "TON-USDT-SWAP",
]

# Thông số kỹ thuật cho Futures SWAP (Dựa trên OKX API thực tế)
# contract_value: Giá trị của 1 hợp đồng (tính theo coin cơ sở hoặc USDT)
OKX_SYMBOL_SPECS: Dict[str, Dict] = {
    "BTC-USDT-SWAP": {
        "min_size": 1,  # 1 contract
        "contract_value": 0.01,  # 1 contract = 0.01 BTC
        "tick_size": 0.1,
        "lot_size": 1,
        "max_leverage": 125,
        "min_notional": 10.0,
        "taker_fee": 0.0005,
        "maker_fee": 0.0002,
    },
    "ETH-USDT-SWAP": {
        "min_size": 1,  # 1 contract
        "contract_value": 0.1,  # 1 contract = 0.1 ETH
        "tick_size": 0.01,
        "lot_size": 1,
        "max_leverage": 100,
        "min_notional": 10.0,
        "taker_fee": 0.0005,
        "maker_fee": 0.0002,
    },
    "SOL-USDT-SWAP": {
        "min_size": 1,  # 1 contract
        "contract_value": 1.0,  # 1 contract = 1 SOL
        "tick_size": 0.01,
        "lot_size": 1,
        "max_leverage": 75,
        "min_notional": 10.0,
        "taker_fee": 0.0005,
        "maker_fee": 0.0002,
    },
}

# Gán giá trị mặc định cho các coin còn lại
for symbol in OKX_TOP20_COINS:
    if symbol not in OKX_SYMBOL_SPECS:
        # Mặc định đa số các altcoin khác trên OKX có contract_value là 1 hoặc 10
        OKX_SYMBOL_SPECS[symbol] = {
            "min_size": 1,
            "contract_value": 1.0,
            "tick_size": 0.001,
            "lot_size": 1,
            "max_leverage": 50,
            "min_notional": 10.0,
            "taker_fee": 0.0005,
            "maker_fee": 0.0002,
        }

# Danh sách tất cả timeframes được hỗ trợ bởi OKX
OKX_SUPPORTED_TIMEFRAMES: List[str] = ["5m", "15m", "1H", "4H", "1D", "1W", "1M"]


def validate_okx_symbols() -> bool:
    """Validate all symbols in OKX_TOP20_COINS have complete specs."""
    for symbol in OKX_TOP20_COINS:
        if symbol not in OKX_SYMBOL_SPECS:
            raise ValueError(f"Missing specs for symbol: {symbol}")
        required_fields = [
            "min_size",
            "contract_value",
            "tick_size",
            "lot_size",
            "max_leverage",
            "min_notional",
        ]
        for field in required_fields:
            if field not in OKX_SYMBOL_SPECS[symbol]:
                raise ValueError(f"Missing {field} for symbol: {symbol}")
    return True


validate_okx_symbols()
