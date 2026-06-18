"""
Risk management engine that validates all trading signals against risk rules.
Enforces position sizing, leverage limits, drawdown protection, and other risk constraints.
"""

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional

from loguru import logger

from core.config.settings import settings
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from infrastructure.exchange.base_exchange import BaseExchange
from services.strategies.base_strategy import Signal, SignalType
from domain.risk.risk_utilities import _validate_sl_distance, _validate_entry_against_market, RiskAssessment


@dataclass
class PortfolioMetrics:
    """Current portfolio risk metrics."""

    total_balance_usdt: float = 0.0
    available_margin_usdt: float = 0.0
    total_open_positions: int = 0


class RiskManager:
    """
    Comprehensive risk management system that evaluates every signal before execution.
    Enforces all risk rules and protects the portfolio from excessive losses.
    """

    def __init__(self, event_bus: EventBus, exchange: BaseExchange, settings_obj=None):
        self.event_bus = event_bus
        self.exchange = exchange
        self.settings = settings_obj or settings
        self._portfolio_metrics = PortfolioMetrics()
        self._in_flight_orders_count = 0
        self._peak_equity_usdt = 0.0
        self._peak_equity_date: Optional[datetime] = None  # [FIX P0-1] Track date for daily reset
        self._halt_triggered: bool = False  # [FIX P0-1] Prevent re-firing breaker every 60s
        self._historical_pnl: List[float] = []
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
        self._position_history: List[dict] = []
        # [CONCENTRATION GUARD] Cache vị thế theo symbol, TTL = 5s
        # _position_cache[symbol] = (timestamp, count, existing_side, existing_instId)
        self._position_cache: dict = {}
        self._position_cache_ttl = 5.0
        self._position_cache_max_age = 30.0
        logger.info("RiskManager initialized")

    def _purge_stale_position_cache(self, now: float) -> None:
        """Drop cache entries older than max age to avoid unbounded growth."""
        stale_symbols = [
            symbol
            for symbol, (cached_at, *_rest) in self._position_cache.items()
            if now - cached_at > self._position_cache_max_age
        ]
        for symbol in stale_symbols:
            del self._position_cache[symbol]

    async def initialize(self) -> None:
        """Initialize risk manager and start periodic portfolio updates."""
        # Subscribe to signal events
        self.event_bus.subscribe(
            self._handle_new_signal,
            [EventTopic.STRATEGY_SIGNAL_GENERATED],
            handler_id="risk_manager_signals",
        )

        # Subscribe to position events
        self.event_bus.subscribe(
            self._handle_position_update,
            [
                EventTopic.POSITION_OPENED,
                EventTopic.POSITION_CLOSED,
                EventTopic.POSITION_PARTIAL_CLOSED,
            ],
            handler_id="risk_manager_positions",
        )

        # Subscribe to mirror resync failure events
        self.event_bus.subscribe(
            self._handle_mirror_resync_failed,
            [EventTopic.MIRROR_RESYNC_FAILED],
            handler_id="risk_manager_mirror",
        )

        await self.update_portfolio_metrics()
        self._running = True
        self._update_task = asyncio.create_task(self._periodic_metrics_update())
        logger.info("RiskManager initialization complete")

    async def stop(self) -> None:
        """Bootstrap-compatible shutdown hook (delegates to shutdown)."""
        await self.shutdown()

    async def shutdown(self) -> None:
        """Gracefully shutdown the risk manager."""
        self._running = False
        if self._update_task:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
        self.event_bus.unsubscribe(handler_id="risk_manager_signals")
        self.event_bus.unsubscribe(handler_id="risk_manager_positions")
        logger.info("RiskManager shutdown complete")

    async def _handle_mirror_resync_failed(self, event: Event) -> None:
        """Handle mirror resync failure - halt all new trades until resolved."""
        logger.critical("[RISK] Mirror resync failed - halting all new position opens")
        self._halt_triggered = True
        # Publish event to notify all components that trading is halted
        await self.event_bus.publish(Event(
            event_type=EventTopic.TRADING_HALTED,
            data={"reason": "Mirror resync failed - state compromised"},
            source="risk_manager"
        ))

    async def _periodic_metrics_update(self) -> None:
        """Periodically update portfolio metrics."""
        while self._running:
            try:
                await self.update_portfolio_metrics()
                await asyncio.sleep(60)  # Update every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics update task: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def update_portfolio_metrics(self) -> None:
        """Fetch latest portfolio metrics from infrastructure.exchange or cache."""
        try:
            # Fetch balances (fast path via cache)
            total_balance = 0.0
            available = 0.0
            # Check exchange_mirror to retrieve cached balance (fast-path)
            cache = getattr(self, "exchange_mirror", None)
            if cache is not None:
                total_balance = await cache.get_total_balance()
                available = await cache.get_free_margin()

            if total_balance <= 0:
                # Fallback to direct REST fetch if cache is not populated
                balances = await self.exchange.fetch_balance()
                usdt_balance = balances.get("USDT")
                if usdt_balance:
                    total_balance = usdt_balance.total
                    available = usdt_balance.free

            # Fetch positions
            positions = await self.exchange.fetch_positions()

            self._portfolio_metrics = PortfolioMetrics(
                total_balance_usdt=total_balance,
                available_margin_usdt=available,
                total_open_positions=len(positions),
            )

            # Drawdown Circuit Breaker Logic
            # [FIX P0-1] Reset peak equity daily so drawdown is measured within the trading day,
            # not from the beginning of time. This prevents permanent bot halt after a Demo reset.
            today = datetime.now(timezone.utc).date()
            if self._peak_equity_date is None or self._peak_equity_date != today:
                logger.info(
                    f"[DRAWDOWN] Daily reset: peak equity reset from ${self._peak_equity_usdt:.2f} "
                    f"to ${total_balance:.2f} for new trading day {today}"
                )
                self._peak_equity_usdt = total_balance
                self._peak_equity_date = today
                self._halt_triggered = False  # Allow breaker to fire again on new day

            if total_balance > self._peak_equity_usdt:
                self._peak_equity_usdt = total_balance
                self._halt_triggered = False  # New peak → reset halt guard

            if self._peak_equity_usdt > 0 and not self._halt_triggered:
                drawdown = (self._peak_equity_usdt - total_balance) / self._peak_equity_usdt
                # [FIX P0-2] Read from settings attribute; fallback = 0.30 (30%)
                max_dd = getattr(self.settings, "max_daily_drawdown", 0.30)
                if drawdown >= max_dd:
                    self._halt_triggered = True  # [FIX P0-1] Only fire ONCE per peak window
                    logger.critical(
                        f"MAX DRAWDOWN BREAKER TRIGGERED: {drawdown*100:.2f}% >= {max_dd*100:.2f}%"
                        f" | Peak=${self._peak_equity_usdt:.2f}, Current=${total_balance:.2f}"
                    )
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.CONTROL_HALT_TRADING,
                            data={"reason": "MAX_DRAWDOWN_BREAKER", "drawdown": drawdown},
                            source="risk_manager"
                        )
                    )
                elif drawdown > 0.01:
                    logger.debug(
                        f"[DRAWDOWN] Current drawdown: {drawdown*100:.2f}% (limit: {max_dd*100:.2f}%)"
                    )

            logger.debug(
                f"Portfolio metrics updated: Balance=${total_balance:.2f}, "
                f"Open positions={len(positions)}"
            )

        except Exception as e:
            logger.error(f"Failed to update portfolio metrics: {e}", exc_info=True)

    async def _check_symbol_concentration(self, signal: Signal) -> Optional[RiskAssessment]:
        """Block when symbol already has max concurrent positions (demo + production)."""
        max_concentration = int(getattr(settings, "max_symbol_concentration", 1))
        if max_concentration >= 9999:
            return None

        try:
            now = time.time()
            self._purge_stale_position_cache(now)
            cached = self._position_cache.get(signal.symbol)

            if cached and (now - cached[0]) < self._position_cache_ttl:
                symbol_count, existing_side, existing_instId = cached[1], cached[2], cached[3]
            else:
                mirror = getattr(self, "exchange_mirror", None)
                symbol_count = 0
                existing_side = ""
                existing_instId = ""

                if mirror:
                    all_pos = await mirror.get_all_positions()
                    for inst_id, p in all_pos.items():
                        pos_symbol = getattr(p, "instId", "") or inst_id
                        if pos_symbol == signal.symbol:
                            symbol_count += 1
                            pos_size = getattr(p, "pos", 0.0)
                            existing_side = "long" if pos_size > 0 else "short"
                            existing_instId = inst_id
                else:
                    positions = await self.exchange.fetch_positions()
                    for p in positions:
                        p_symbol = p.symbol if hasattr(p, "symbol") else ""
                        if p_symbol == signal.symbol:
                            symbol_count += 1
                            existing_side = str(getattr(p, "side", ""))
                            existing_instId = getattr(p, "position_id", signal.symbol)

                self._position_cache[signal.symbol] = (now, symbol_count, existing_side, existing_instId)

            if symbol_count >= max_concentration:
                direction = str(
                    signal.signal_type.value
                    if hasattr(signal.signal_type, "value")
                    else signal.signal_type
                ).upper()
                tf = getattr(signal, "timeframe", "N/A")
                
                # [THUẬN THEO CHIỀU GIÓ - REVERSE]
                # Check if it's the SAME direction or OPPOSITE direction
                is_same_direction = False
                if direction in ("BUY", "LONG") and existing_side.lower() == "long":
                    is_same_direction = True
                elif direction in ("SELL", "SHORT") and existing_side.lower() == "short":
                    is_same_direction = True
                
                if not is_same_direction:
                    reverse_msg = f"REVERSE SIGNAL: {signal.symbol} — {tf} {direction} against existing {existing_side.upper()} ({existing_instId})"
                    logger.info(f"[REVERSE-SIGNAL] {reverse_msg}. Proceeding with REVERSE.")
                    # Return approved=True with reason="REVERSE" to tell OrderHandler to close old position first
                    return RiskAssessment(approved=True, reason="REVERSE")

                # SAME direction -> BLOCK (no averaging in)
                block_msg = (
                    f"⛔ TỪ CHỐI TÍN HIỆU: Đang có vị thế {existing_side.upper()} mở. "
                    f"Bỏ qua tín hiệu {direction} để tránh nhồi lệnh rủi ro."
                )
                logger.warning(block_msg)
                if self.event_bus:
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.RISK_SIGNAL_REJECTED,
                            data={
                                "reason": block_msg,
                                "symbol": signal.symbol,
                                "signal_type": direction,
                                "timeframe": tf,
                                "entry_price": getattr(signal, "entry_price", 0.0),
                            },
                            source="concentration_guard",
                        )
                    )
                signal.risk_approved = False
                return RiskAssessment(approved=False, reason=block_msg)
        except Exception as e:
            logger.error(f"[CONCENTRATION GUARD] Failed to check: {e}", exc_info=True)
            signal.risk_approved = False
            return RiskAssessment(
                approved=False,
                reason=f"⛔ TỪ CHỐI TÍN HIỆU: Lỗi kiểm tra vị thế ({e}). Tạm thời chặn lệnh để an toàn.",
            )
        return None

    async def assess_signal(self, signal: Signal) -> RiskAssessment:
        """
        Assess if a signal passes all risk checks.
        Returns a RiskAssessment with the decision and any adjustments.
        In Demo Mode, all limits are bypassed. In Production Mode, verifies available margin and limits.
        """
        # Update metrics for tracking
        await self.update_portfolio_metrics()

        # [FIX LỖI 3] GUARD CLAUSE: Chặn Lệnh Ma (Ghost Position Prevention)
        cache = getattr(self, "exchange_mirror", None)
        if cache and getattr(cache, "_is_resyncing", False):
            reason = "⏳ TỪ CHỐI TÍN HIỆU: Hệ thống đang đồng bộ (Syncing) với OKX. Tạm thời chặn lệnh để tránh lỗi."
            logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
            signal.risk_approved = False
            return RiskAssessment(approved=False, reason=reason)

        if settings.ENABLE_STRICT_ACCOUNT_SEEDING and cache:
            has_seed = (
                cache.has_account_seed()
                if hasattr(cache, "has_account_seed")
                else cache.is_snapshot_ready()
                if hasattr(cache, "is_snapshot_ready")
                else True
            )
            if not has_seed:
                reason = "⏳ TỪ CHỐI TÍN HIỆU: Đang chờ dữ liệu tài khoản từ OKX Websocket. Xin chờ trong giây lát."
                logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
                signal.risk_approved = False
                return RiskAssessment(approved=False, reason=reason)

        concentration = await self._check_symbol_concentration(signal)
        if concentration is not None:
            return concentration

        max_allowed = self._calculate_max_positions()
        if max_allowed < 9999:
            current_count = (
                self._portfolio_metrics.total_open_positions + self._in_flight_orders_count
            )
            if current_count > max_allowed:
                reason = f"❌ TỪ CHỐI TÍN HIỆU: Đã đạt giới hạn số lượng vị thế mở (Max: {max_allowed})."
                logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
                signal.risk_approved = False
                return RiskAssessment(approved=False, reason=reason)

        leverage = getattr(signal, "leverage", settings.default_leverage) or settings.default_leverage

        # [FIX P8] Liquidation proximity guard (warning only, not a hard block for demo)
        try:
            s_type = signal.signal_type.value if hasattr(signal.signal_type, "value") else signal.signal_type
            liq_side = "long" if s_type == "buy" else "short"
            lev = getattr(signal, "leverage", settings.default_leverage) or settings.default_leverage
            liq_price = self.calculate_liquidation_price(
                entry_price=signal.entry_price,
                leverage=int(lev),
                side=liq_side,
                symbol=signal.symbol,
            )
            proximity_pct = abs(signal.entry_price - liq_price) / signal.entry_price * 100
            if proximity_pct < 5.0:
                logger.warning(
                    f"[LIQ-PROXIMITY] {signal.symbol} entry={signal.entry_price:.4f} liq={liq_price:.4f} "
                    f"proximity={proximity_pct:.2f}% — dangerously close to liquidation!"
                )
        except Exception as e:
            logger.debug(f"Liq proximity check skipped: {e}")

        # Validate entry price against market (check deviation)
        try:
            # Fetch current market price
            ticker = await self.exchange.fetch_ticker(signal.symbol)
            if ticker and hasattr(ticker, "last_price"):
                market_price = float(ticker.last_price)
                entry_assessment = _validate_entry_against_market(signal.entry_price, market_price)
                if not entry_assessment.approved:
                    logger.warning(f"[RISK-REJECT] {entry_assessment.reason}")
                    signal.risk_approved = False
                    return RiskAssessment(approved=False, reason=entry_assessment.reason)
        except Exception as e:
            logger.debug(f"Entry price validation skipped: {e}")

        # Validate SL distance from entry
        try:
            if signal.stop_loss_price:
                sl_assessment = _validate_sl_distance(
                    signal.entry_price, 
                    signal.stop_loss_price, 
                    signal.signal_type
                )
                if not sl_assessment.approved:
                    logger.warning(f"[RISK-REJECT] {sl_assessment.reason}")
                    signal.risk_approved = False
                    return RiskAssessment(approved=False, reason=sl_assessment.reason)
        except Exception as e:
            logger.debug(f"SL distance validation skipped: {e}")

        if not settings.production_risk_mode:
            logger.info(
                f"Signal approved for {signal.symbol} (non-production risk mode — concentration/sync passed): "
                f"{signal.signal_type}, size=${signal.position_size_usdt:.2f}"
            )
            signal.risk_approved = True
            return RiskAssessment(
                approved=True,
                reason="Non-production risk mode: concentration and mirror gates passed",
                adjusted_position_size=signal.position_size_usdt,
                adjusted_stop_loss=signal.stop_loss_price,
            )

        # === PRODUCTION RISK VERIFICATION ===

        # Check 2: Available Margin Verification
        required_margin = signal.position_size_usdt / leverage
        available = self._portfolio_metrics.available_margin_usdt

        if available < required_margin:
            reason = f"💸 TỪ CHỐI TÍN HIỆU: Ký quỹ khả dụng không đủ (Có: ${available:.2f} < Cần: ${required_margin:.2f})"
            logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
            signal.risk_approved = False
            return RiskAssessment(approved=False, reason=reason)
            
        # Check: Max Leverage
        if leverage > settings.max_leverage:
            reason = f"⚠️ TỪ CHỐI TÍN HIỆU: Đòn bẩy {leverage}x vượt mức cho phép (Tối đa: {settings.max_leverage}x)"
            logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
            signal.risk_approved = False
            return RiskAssessment(approved=False, reason=reason)
            
        # Check: Max Risk Per Trade
        equity = self._portfolio_metrics.total_balance_usdt
        max_risk_amount = equity * (settings.max_risk_allowed_pct / 100.0)
        # Approximate risk based on position size and SL ROE
        sl_roe_pct = settings.sl_roe_pct + settings.fee_roe_buffer_pct
        risk_amount = signal.position_size_usdt * (sl_roe_pct / 100.0)
        
        if risk_amount > max_risk_amount and equity > 0:
            reason = f"🛡️ TỪ CHỐI TÍN HIỆU: Rủi ro vượt hạn mức cho phép (Rủi ro: ${risk_amount:.2f} > Tối đa: ${max_risk_amount:.2f})"
            logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
            signal.risk_approved = False
            return RiskAssessment(approved=False, reason=reason)
            
        # Check: Min Risk Reward Ratio - use best (highest) TP to calculate R:R
        if signal.take_profit_prices and len(signal.take_profit_prices) > 0:
            tp_best_raw = signal.take_profit_prices[-1]
            tp_best_price = (
                float(tp_best_raw["price"])
                if isinstance(tp_best_raw, dict)
                else float(tp_best_raw)
            )
            rr_assessment = self.validate_risk_reward(
                entry_price=signal.entry_price,
                sl_price=signal.stop_loss_price,
                tp_price=tp_best_price,
                min_rr=settings.min_risk_reward_ratio
            )
            if not rr_assessment.approved:
                logger.warning(f"Signal rejected for {signal.symbol}: {rr_assessment.reason}")
                signal.risk_approved = False
                return rr_assessment

        # Check 3: Minimum Position Size
        if signal.position_size_usdt <= 0:
            reason = f"⚠️ TỪ CHỐI TÍN HIỆU: Khối lượng vào lệnh không hợp lệ (${signal.position_size_usdt})"
            logger.warning(f"Signal rejected for {signal.symbol}: {reason}")
            signal.risk_approved = False
            return RiskAssessment(approved=False, reason=reason)

        logger.info(
            f"Signal approved for {signal.symbol} (Production Risk Passed): "
            f"{signal.signal_type}, size=${signal.position_size_usdt:.2f}, req margin=${required_margin:.2f}"
        )
        signal.risk_approved = True
        return RiskAssessment(
            approved=True,
            reason="All production risk checks passed successfully",
            adjusted_position_size=signal.position_size_usdt,
            adjusted_stop_loss=signal.stop_loss_price,
        )

    async def _handle_new_signal(self, event: Event) -> None:
        """Handle an incoming signal from strategy engine."""
        max_allowed = self._calculate_max_positions()
        mode_label = "PRODUCTION" if settings.production_risk_mode else "DEMO"
        logger.debug(f"[{mode_label}] Max positions allowed: {max_allowed}")

        # Reserve slot synchronously
        self._in_flight_orders_count += 1

        try:
            # Reconstruct Signal object safely by ignoring extra fields
            signal_data = event.data
            valid_fields = {k: v for k, v in signal_data.items() if k in Signal.__dataclass_fields__}
            signal = Signal(**valid_fields)

            logger.info(f"Assessing risk for signal: {signal.symbol} {signal.signal_type}")

            assessment = await self.assess_signal(signal)
            if assessment.approved:
                # Publish approved signal for execution
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.RISK_SIGNAL_APPROVED,
                        data={**signal.__dict__, "assessment": assessment.__dict__},
                        source="risk_manager",
                    )
                )
            else:
                self._in_flight_orders_count -= 1
                # Publish rejected signal
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.RISK_SIGNAL_REJECTED,
                        data={**signal.__dict__, "rejection_reason": assessment.reason},
                        source="risk_manager",
                    )
                )
                logger.warning(f"Signal rejected: {assessment.reason}")
        except Exception as e:
            self._in_flight_orders_count -= 1
            logger.error(f"Error handling signal: {e}")

    async def _handle_position_update(self, event: Event) -> None:
        """Handle position lifecycle events."""
        from datetime import datetime, timezone
        position_data = event.data
        event_type = event.event_type

        if event_type == EventTopic.POSITION_OPENED:
            # Decrement in-flight counter: position has landed
            if self._in_flight_orders_count > 0:
                self._in_flight_orders_count -= 1
            self._position_history.append(
                {"type": "opened", "timestamp": datetime.now(timezone.utc), "data": position_data}
            )
        elif event_type == EventTopic.POSITION_CLOSED:
            # Update P&L tracking
            pnl = position_data.get("realized_pnl", 0.0)
            self._historical_pnl.append(pnl)
            self._position_history.append(
                {
                    "type": "closed",
                    "timestamp": datetime.now(timezone.utc),
                    "pnl": pnl,
                    "data": position_data,
                }
            )

        # Refresh metrics after position changes
        await self.update_portfolio_metrics()

    def _get_mmr_for_symbol(self, symbol: str, contracts: float = 1.0) -> float:
        """
        Get dynamic MMR tier for symbol based on contracts according to OKX tier structures.
        """
        base_coin = symbol.split("-")[0] if symbol else ""
        is_tier_1 = base_coin in {"BTC", "ETH"}

        # OKX dynamic MMR tiers:
        if is_tier_1:
            if contracts <= 150:
                return 0.004
            elif contracts <= 500:
                return 0.005
            elif contracts <= 3000:
                return 0.010
            else:
                return 0.020
        else:
            # Others: high beta / high cap
            if contracts <= 100:
                return 0.005
            elif contracts <= 500:
                return 0.008
            elif contracts <= 2000:
                return 0.015
            else:
                return 0.030

    def calculate_liquidation_price(
        self, entry_price: float, leverage: int, side: str, symbol: str = "", contracts: float = 1.0
    ) -> float:
        """
        Calculate estimated liquidation price using OKX Maintenance Margin Ratio (MMR) tiers.
        Formula (approx for Isolated): Liq Price = Entry * (1 - 1/Leverage + MMR)
        """
        mmr = self._get_mmr_for_symbol(symbol, contracts)

        if side == "long":
            # Long: Price must stay above Entry * (1 - (InitialMargin - MMR))
            initial_margin = 1 / leverage
            return entry_price * (1 - (initial_margin - mmr))
        else:  # short
            # Short: Price must stay below Entry * (1 + (InitialMargin - MMR))
            initial_margin = 1 / leverage
            return entry_price * (1 + (initial_margin - mmr))

    def calculate_pnl(
        self,
        entry_price: float,
        close_price: float,
        side: str,
        amount: float,
        ct_val: float = 1.0,
        include_fees: bool = True,
        symbol: str = "BTC-USDT-SWAP",  # [DYNAMIC] Add symbol parameter for fee rate lookup
    ) -> float:
        """
        Calculate Net P&L (Profit and Loss) for a position, including trading fees.
        Net PnL = Gross PnL - (Entry Fee + Exit Fee)
        """
        if side == "long":
            gross_pnl = (close_price - entry_price) * amount * ct_val
        else:  # short
            gross_pnl = (entry_price - close_price) * amount * ct_val

        if not include_fees:
            return gross_pnl

        # [DYNAMIC] Fee Calculation using API fee rates
        # Try to get fee rate from exchange API, fallback to settings
        fee_rate = settings.taker_fee_rate  # Default fallback
        if hasattr(self.exchange, "get_fee_rate"):
            try:
                fee_rate_result = self.exchange.get_fee_rate(symbol, "taker")
                # Handle case where get_fee_rate might return coroutine in test contexts
                if isinstance(fee_rate_result, (int, float)):
                    fee_rate = fee_rate_result
            except Exception:
                # Fallback to settings if get_fee_rate fails
                pass

        entry_fee = (entry_price * amount * ct_val) * fee_rate
        exit_fee = (close_price * amount * ct_val) * fee_rate

        return gross_pnl - (entry_fee + exit_fee)

    def calculate_roe(
        self,
        entry_price: float,
        close_price: float,
        amount: float,
        leverage: int,
        side: str,
        ct_val: float = 1.0,
        include_fees: bool = True,
    ) -> float:
        """Calculate Return on Equity (ROE) %."""
        pnl = self.calculate_pnl(entry_price, close_price, side, amount, ct_val=ct_val, include_fees=include_fees)
        margin = (entry_price * amount * ct_val) / leverage
        return (pnl / margin) * 100 if margin > 0 else 0.0

    # =========================================================================
    # Trade Calculation Utilities - Institutional Grade
    # =========================================================================

    def validate_risk_reward(
        self, entry_price: float, sl_price: float, tp_price: float, min_rr: float
    ) -> RiskAssessment:
        """Validate Risk/Reward ratio meets institutional minimum (from .env)."""
        # Calculate risk and reward in price terms
        if entry_price > sl_price and tp_price > entry_price:  # long
            risk = entry_price - sl_price
            reward = tp_price - entry_price
        elif entry_price < sl_price and tp_price < entry_price:  # short
            risk = sl_price - entry_price
            reward = entry_price - tp_price
        else:
            return RiskAssessment(approved=False, reason="Invalid SL/TP configuration")

        if risk <= 0:
            return RiskAssessment(approved=False, reason="Risk cannot be zero or negative")

        rr_ratio = reward / risk
        if rr_ratio < min_rr:
            return RiskAssessment(
                approved=False, reason=f"Risk/Reward ratio too low: {rr_ratio:.2f} < {min_rr}"
            )
        return RiskAssessment(approved=True)

    def get_risk_metrics(self) -> dict:
        """Get comprehensive risk metrics."""
        return {
            "portfolio": self._portfolio_metrics.__dict__,
            "total_closed_positions": len(
                [p for p in self._position_history if p["type"] == "closed"]
            ),
            "total_pnl": sum(self._historical_pnl),
            "win_rate": self._calculate_win_rate() if self._historical_pnl else 0.0,
        }

    def _calculate_win_rate(self) -> float:
        """Calculate strategy win rate."""
        if not self._historical_pnl:
            return 0.0
        winning_trades = sum(1 for pnl in self._historical_pnl if pnl > 0)
        return winning_trades / len(self._historical_pnl) * 100

    def _calculate_max_positions(self) -> int:
        """Calculate max open positions using pure risk utilities."""
        from domain.risk.risk_utilities import _calculate_max_positions
        return _calculate_max_positions(self.settings)