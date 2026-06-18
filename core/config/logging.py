"""
Logging configuration - Đã được TÁI CẤU TRÚC gọn gàng.
Thay vì chẻ thành 9 file log gây lộn xộn, hệ thống giờ chỉ dùng 3 luồng:

  1. vcorex.log    — Nhật ký tổng hợp toàn bộ hoạt động (dễ theo dõi dòng thời gian)
  2. errors.log    — Chỉ ghi ERROR / CRITICAL để phát hiện lỗi nhanh
  3. trades.jsonl  — Kiểm toán giao dịch dạng JSON Lines cho phân tích PnL

Console (stdout) hiển thị màu sắc ở mức INFO trở lên, ẩn nhiễu từ thư viện OKX.
"""

import io
import logging
import sys
from pathlib import Path
from typing import Any, Dict

from loguru import logger

from core.config.settings import settings


# ---------------------------------------------------------------------------
# FORMAT TEMPLATES
# ---------------------------------------------------------------------------
_CONSOLE_FMT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{message}"
)

_FILE_FMT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{name}:{function}:{line} | "
    "{message}"
)

# JSONL cho trades.jsonl — một dòng JSON mỗi event
_TRADE_JSONL_FMT = (
    '{{"time":"{time:YYYY-MM-DDTHH:mm:ss.SSSZ}",'
    '"level":"{level}",'
    '"module":"{name}",'
    '"message":"{message}"}}'
)


# ---------------------------------------------------------------------------
# FILTER HELPERS
# ---------------------------------------------------------------------------
def _is_okx_noise(record: Dict[str, Any]) -> bool:
    """True nếu record là log cấp thấp từ module okx_exchange (nhiễu kết nối)."""
    return (
        "okx_exchange" in (record.get("name") or "").lower()
        and record["level"].name not in ("ERROR", "CRITICAL")
    )


def _is_trade_event(record: Dict[str, Any]) -> bool:
    """True nếu đây là sự kiện giao dịch cần ghi vào trades.jsonl."""
    msg = record["message"]
    return (
        "trade." in msg
        or "order." in msg
        or "IN_FLIGHT" in msg
        or "FILLED" in msg
        or "AUDIT-" in msg
    )


# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    """Cấu hình hệ thống log theo cấu trúc 3 luồng gọn gàng."""
    logs_dir = Path(settings.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Tắt mọi logger Python cũ từ thư viện (tránh duplicate)
    logging.getLogger("infrastructure.exchange.okx_exchange").setLevel(logging.ERROR)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    # Xóa toàn bộ handler mặc định của loguru
    logger.remove()

    console_level = (settings.log_level or "INFO").upper()
    file_level = "DEBUG" if console_level == "DEBUG" else console_level

    # Fix Windows console encoding để hiển thị emoji đúng (🔀 v.v.)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 1. CONSOLE — configurable via LOG_LEVEL env, ẩn nhiễu OKX
    # ------------------------------------------------------------------
    logger.add(
        sys.stdout,
        format=_CONSOLE_FMT,
        level=console_level,
        colorize=True,
        enqueue=True,
        filter=lambda r: not _is_okx_noise(r),
    )

    # ------------------------------------------------------------------
    # 2. vcorex.log — Tổng hợp MỌI hoạt động, xoay 50 MB, giữ 5 file
    # ------------------------------------------------------------------
    logger.add(
        logs_dir / "vcorex.log",
        format=_FILE_FMT,
        level=file_level,
        rotation="50 MB",
        retention=5,          # Giữ tối đa 5 file nén gần nhất
        compression="zip",
        enqueue=True,
        filter=lambda r: not _is_okx_noise(r),
    )

    # ------------------------------------------------------------------
    # 3. errors.log — Chỉ ERROR / CRITICAL, giữ 90 ngày
    # ------------------------------------------------------------------
    logger.add(
        logs_dir / "errors.log",
        format=_FILE_FMT,
        level="ERROR",
        rotation="20 MB",
        retention="90 days",
        compression="zip",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    # ------------------------------------------------------------------
    # 4. trades.jsonl — Kiểm toán giao dịch (JSON Lines)
    # ------------------------------------------------------------------
    logger.add(
        logs_dir / "trades.jsonl",
        format=_TRADE_JSONL_FMT,
        level="INFO",
        rotation="20 MB",
        retention="180 days",
        compression="zip",
        enqueue=True,
        filter=_is_trade_event,
    )

    # ------------------------------------------------------------------
    # 5. InMemoryLogHandler (Telegram Telemetry) — inject nếu có
    # ------------------------------------------------------------------
    try:
        from services.position.telegram_handler import LOG_CONTAINER
        logger.add(LOG_CONTAINER, level="INFO", enqueue=True)
    except Exception as e:
        logger.debug(f"LOG_CONTAINER not available: {e}")

    logger.info("[OK] Logging tai cau truc: vcorex.log | errors.log | trades.jsonl")


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS (Giữ nguyên để không phá vỡ các module đang gọi)
# ---------------------------------------------------------------------------

def log_trade(trade_data: dict) -> None:
    """Ghi sự kiện giao dịch vào trades.jsonl."""
    logger.info(f"trade.executed | {trade_data}")


def log_order_lifecycle(event_type: str, order_data: dict) -> None:
    """Ghi vòng đời lệnh (IN_FLIGHT, FILLED, CANCELLED, AUDITED)."""
    logger.info(f"order.{event_type} | {order_data}")


def log_audit_event(event_type: str, audit_data: dict) -> None:
    """Ghi sự kiện kiểm toán."""
    logger.info(f"AUDIT-{event_type} | {audit_data}")


def log_telegram_event(event_type: str, msg_data: dict) -> None:
    """Ghi sự kiện Telegram."""
    logger.debug(f"telegram.{event_type} | {msg_data}")


def log_marketdata_event(event_type: str, data: dict) -> None:
    """Ghi sự kiện dữ liệu thị trường."""
    logger.debug(f"marketdata.{event_type} | {data}")


def log_websocket_event(event_data: dict) -> None:
    """Ghi sự kiện WebSocket."""
    logger.debug(f"ws.event | {event_data}")


def log_signal(signal_data: dict) -> None:
    """Ghi tín hiệu giao dịch được tạo ra."""
    logger.info(f"strategy.signal | {signal_data}")
