import asyncio
from loguru import logger
from typing import Any

class ShadowValidator:
    """Runs in parallel with the old PositionEngine to validate ExchangeMirror logic."""
    
    def __init__(self, position_engine: Any, exchange_mirror: Any):
        self.position_engine = position_engine
        self.exchange_mirror = exchange_mirror
        self._running = False
        self._task = None

    def start(self):
        """Start the shadow validation loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._validate_loop())
        logger.info("ShadowValidator started in Shadow Mode.")

    def stop(self):
        """Stop the shadow validation loop."""
        self._running = False
        if self._task:
            self._task.cancel()

    async def _validate_loop(self):
        while self._running:
            try:
                await asyncio.sleep(10)
                self._run_validation()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ShadowValidator error: {e}")

    async def _run_validation(self):
        try:
            old_positions = self.position_engine.get_active_positions()
            new_positions = await self.exchange_mirror.get_all_positions()

            old_count = len(old_positions)
            new_count = len(new_positions)

            if old_count != new_count:
                logger.warning(f"[SHADOW DIFF] Position Count: Old={old_count}, New={new_count}")

            # 1. Quét các vị thế Local để đối chiếu với sàn
            old_symbols = set()
            for old_pos in old_positions:
                old_symbols.add(old_pos.symbol)
                new_pos = new_positions.get(old_pos.symbol)
                if not new_pos:
                    # Có trên Local nhưng mất trên Sàn (Orphan)
                    logger.debug(f"[SHADOW DIFF] Lệch Orphan: Local Position {old_pos.symbol} (status={old_pos.status}) không có trên Sàn")
                    continue
                    
                # Compare PnL using uplLastPx (ưu tiên nếu có) để tránh bẫy lệch pha PnL do Mark Price vs Last Price
                exchange_pnl = getattr(new_pos, 'uplLastPx', new_pos.upl)
                if exchange_pnl == 0.0 and new_pos.upl != 0.0:
                    exchange_pnl = new_pos.upl # Fallback to upl if uplLastPx is explicitly 0 but upl isn't

                pnl_diff = abs(old_pos.pnl - exchange_pnl)
                if pnl_diff > 0.5: # 0.5 USDT threshold
                    logger.debug(
                        f"[SHADOW DIFF] PnL Lệch pha {old_pos.symbol}: "
                        f"Local_PnL(LastPx)={old_pos.pnl:.2f}, "
                        f"Mirror_PnL(uplLastPx)={exchange_pnl:.2f} (Diff: {pnl_diff:.2f})"
                    )

            # 2. Ghost on exchange but not local — alert only (recovery via ReconciliationService / PE handler)
            for sym, new_pos in new_positions.items():
                if sym not in old_symbols:
                    logger.warning(
                        f"[SHADOW DIFF] Ghost: exchange has {sym} (UPL={new_pos.upl}) "
                        f"but local OPENED missing — reconciliation will heal"
                    )
                    if hasattr(self.position_engine, "event_bus") and self.position_engine.event_bus:
                        from core.events.topics import EventTopic
                        from core.event_bus import Event

                        alert_evt = Event(
                            event_type=EventTopic.SYSTEM_ALERT,
                            data={
                                "message": (
                                    f"PHAT HIEN GHOST POSITION: {sym} tren san nhung bot chua track. "
                                    "Cho reconciliation/ghost handler xu ly."
                                )
                            },
                            source="shadow_validator",
                        )
                        asyncio.create_task(self.position_engine.event_bus.publish(alert_evt))

        except Exception as e:
            logger.error(f"Shadow validation run failed: {e}")