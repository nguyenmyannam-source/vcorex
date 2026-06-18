"""
Minimal application entrypoint. Delegates bot composition to `core.bootstrap`.

Shutdown sequence on SIGINT/SIGTERM:
  1. Signal handler sets asyncio.Event -> bot.start() returns
  2. finally block calls bot.shutdown() with 30-second hard timeout
  3. shutdown() cancels all Algo orders + closes all positions at market
  4. DB flushed, PID lock released, process exits cleanly
"""

import asyncio
import contextlib
import signal
import sys

from loguru import logger

from core.bootstrap import VCoreXTradingBot
from core.config.settings import settings
from core.pid_lock import PIDLock
from services.market_data.timeframe_validator import timeframe_validator


async def main() -> None:
    # --- SINGLE INSTANCE SAFETY LOCK ---
    pid_lock = PIDLock()
    if not pid_lock.acquire():
        return
    # -----------------------------------

    await timeframe_validator.initialize(use_demo=settings.okx_demo_mode)

    bot = VCoreXTradingBot()
    loop = asyncio.get_running_loop()

    # --- GRACEFUL SHUTDOWN SIGNAL HANDLER ---
    def _request_shutdown(sig_name: str) -> None:
        logger.warning(f"[SHUTDOWN] Received {sig_name}. Initiating graceful liquidation and cleanup...")
        bot._shutdown_event.set()

    # Windows-friendly signal handling
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_shutdown, sig.name)
            except (NotImplementedError, RuntimeError):
                pass
    else:
        # On Windows, only SIGINT is reliably supported (Ctrl+C)
        try:
            loop.add_signal_handler(signal.SIGINT, _request_shutdown, "SIGINT")
        except (NotImplementedError, RuntimeError):
            logger.warning("Could not register signal handler on Windows, using KeyboardInterrupt fallback")
    # ----------------------------------------

    try:
        await bot.start()
    except asyncio.CancelledError:
        logger.info("Bot execution cancelled.")
    except Exception as e:
        logger.critical(f"Fatal error in main loop: {e}", exc_info=True)
    finally:
        logger.warning("[SHUTDOWN] Running graceful shutdown with 30-second hard timeout...")
        try:
            # Wait for shutdown to complete or timeout (increased to 30s for Windows)
            await asyncio.wait_for(bot.shutdown(), timeout=30.0)
            logger.info("[SHUTDOWN] Graceful shutdown complete. Exchange is clean.")
        except asyncio.TimeoutError:
            logger.error("[SHUTDOWN] Hard timeout reached (30s). Forcing exit — some positions may remain open!")
        except Exception as e:
            logger.error(f"[SHUTDOWN] Unexpected error during shutdown: {e}")
        finally:
            pid_lock.release()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
