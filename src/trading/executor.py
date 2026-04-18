"""
executor.py (v2)
----------------
Handles LONG entry (execute_buy), LONG exit (execute_sell),
SHORT entry (execute_short_sell), and SHORT exit (execute_short_cover).
All methods respect DRY_RUN / LIVE_MODE from RiskConfig.

Phase 7: Live execution path is gated by a 5-gate safety system. Any
gate failure blocks the real order — dry-run path is unaffected.
"""
import logging
from datetime import datetime, time as dt_time
from typing import Optional, Tuple
import zoneinfo

from src.config.risk import RiskConfig
from src.trading.orders import OrderRequest, OrderResult, OrderSide, OrderType
from src.trading.daily_risk_state import DailyRiskState
from src.trading.sizing import calculate_position_size, SymbolStats, MarketRegime
from src.data_ingestion.short_ban_list import is_safe_to_short

logger = logging.getLogger(__name__)

try:
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except zoneinfo.ZoneInfoNotFoundError:
    import pytz  # type: ignore
    IST = pytz.timezone("Asia/Kolkata")

# Trading hours window for Gate 5 (IST). Orders must fire in 09:15 ≤ t ≤ 15:15.
LIVE_TRADE_START_IST = dt_time(9, 15)
LIVE_TRADE_END_IST = dt_time(15, 15)


def _dry_run_log_enabled() -> bool:
    try:
        from src.config_loader import get_dry_run_log
        return get_dry_run_log()
    except Exception:
        return True


try:
    from src.brokers.zerodha_client import ZerodhaClient
except ImportError:
    ZerodhaClient = None


# ── Phase 7: 5-gate live-order safety system ─────────────────────────────
def _now_ist_time() -> dt_time:
    return datetime.now(IST).time()


def run_live_gates(
    symbol: str,
    conviction: float,
    now_ist: Optional[dt_time] = None,
) -> Tuple[bool, str]:
    """
    Run the 5-gate safety check before any live order is placed.
    Returns (ok, fail_reason). Every gate outcome is logged.
    """
    from src.config_loader import (
        get_dry_run, get_conviction_threshold,
        get_max_trades, get_max_open_positions,
    )
    from src.trading.live_trade_tracker import (
        get_trades_placed_today, get_open_positions,
    )

    # Gate 1 — Config gate: dry_run must be False
    if get_dry_run():
        msg = f"[LIVE GATE 1 FAIL] dry_run=true — order blocked"
        logger.warning(msg)
        return False, msg
    logger.info("[LIVE GATE 1 PASS] dry_run=false")

    # Gate 2 — Conviction gate
    threshold = get_conviction_threshold()
    if float(conviction) < float(threshold):
        msg = f"[LIVE GATE 2 FAIL] {symbol} conviction={conviction:.0f} < {threshold}"
        logger.warning(msg)
        return False, msg
    logger.info(f"[LIVE GATE 2 PASS] {symbol} conviction={conviction:.0f} >= {threshold}")

    # Gate 3 — Daily trade limit gate
    max_trades = get_max_trades()
    placed = get_trades_placed_today()
    if placed >= max_trades:
        msg = f"[LIVE GATE 3 FAIL] Daily limit {max_trades} reached — {symbol} blocked"
        logger.warning(msg)
        return False, msg
    logger.info(f"[LIVE GATE 3 PASS] trades_placed_today={placed} < {max_trades}")

    # Gate 4 — Position limit gate
    max_open = get_max_open_positions()
    open_pos = get_open_positions()
    if open_pos >= max_open:
        msg = f"[LIVE GATE 4 FAIL] Max positions {max_open} reached — {symbol} blocked"
        logger.warning(msg)
        return False, msg
    logger.info(f"[LIVE GATE 4 PASS] open_positions={open_pos} < {max_open}")

    # Gate 5 — Market hours gate (09:15 ≤ t ≤ 15:15 IST)
    t = now_ist if now_ist is not None else _now_ist_time()
    if not (LIVE_TRADE_START_IST <= t <= LIVE_TRADE_END_IST):
        tstr = t.strftime("%H:%M")
        msg = f"[LIVE GATE 5 FAIL] Outside trading hours {tstr} IST — {symbol} blocked"
        logger.warning(msg)
        return False, msg
    logger.info(f"[LIVE GATE 5 PASS] IST time {t.strftime('%H:%M')} within 09:15–15:15")

    return True, "all gates passed"


def _compute_live_quantity(symbol: str, entry_price: float) -> int:
    """Phase 7 live-mode position sizing: qty = int(per_trade_risk / entry_price)."""
    from src.config_loader import get_per_trade_risk
    per_trade_risk = get_per_trade_risk()
    if entry_price <= 0:
        return 0
    qty = int(per_trade_risk / entry_price)
    if qty < 1:
        logger.warning(
            f"[LIVE] {symbol} quantity=0 at ₹{entry_price:.2f} — skipped (price too high)"
        )
        return 0
    logger.info(
        f"[LIVE] {symbol} | qty={qty} | entry=₹{entry_price:.2f} | risk=₹{per_trade_risk:.0f}"
    )
    return qty


class TradeExecutor:
    def __init__(self, risk: RiskConfig, daily_state: DailyRiskState):
        self.risk = risk
        self.daily_state = daily_state
        self._zerodha: Optional["ZerodhaClient"] = None
        if self.risk.live_mode and ZerodhaClient is not None:
            try:
                self._zerodha = ZerodhaClient.from_env()
            except Exception:
                self._zerodha = None

    # ── Phase 7 live-order path ─────────────────────────────────────────
    def _place_live_market_order(
        self,
        symbol: str,
        side: OrderSide,
        entry_price: float,
        conviction: float,
        route: str,
        confidence: float,
    ) -> OrderResult:
        """
        Runs 5 gates → computes sized qty → places MIS market order via Zerodha →
        records to live tracker → fires email alert. Never crashes the caller.
        """
        from src.config_loader import get_live_order_tag

        ok, _reason = run_live_gates(symbol=symbol, conviction=conviction)
        if not ok:
            return OrderResult(
                success=False, broker_order_id=None, message=_reason, filled_qty=0
            )

        qty = _compute_live_quantity(symbol, entry_price)
        if qty < 1:
            return OrderResult(
                success=False, broker_order_id=None,
                message=f"[LIVE] {symbol} quantity=0 at ₹{entry_price:.2f} — skipped",
                filled_qty=0,
            )

        if self._zerodha is None:
            msg = f"[LIVE ORDER FAILED] {symbol} — ZerodhaClient unavailable"
            logger.error(msg)
            return OrderResult(success=False, broker_order_id=None, message=msg, filled_qty=0)

        tag = get_live_order_tag()
        req = OrderRequest(
            symbol=symbol, side=side, quantity=qty,
            order_type=OrderType.MARKET, timestamp=datetime.now(), tag=tag,
        )

        try:
            result = self._zerodha.place_equity_order(req)
        except Exception as e:  # runner must never crash
            msg = f"[LIVE ORDER FAILED] {symbol} — {type(e).__name__}: {e}"
            logger.error(msg)
            return OrderResult(success=False, broker_order_id=None, message=msg, filled_qty=0)

        if not result.success:
            logger.error(f"[LIVE ORDER FAILED] {symbol} — {result.message}")
            return result

        order_id = result.broker_order_id or ""
        logger.info(
            f"[LIVE ORDER] {symbol} | order_id={order_id} | qty={qty} | route={route}"
        )

        try:
            from src.trading.live_trade_tracker import record_live_trade
            record_live_trade(
                symbol=symbol, order_id=order_id, quantity=qty,
                entry_price=float(entry_price), route=route,
                confidence=float(confidence or 0.0),
                conviction_score=float(conviction or 0.0),
            )
        except Exception as e:
            logger.error(f"[LIVE] record_live_trade failed for {symbol}: {e}")

        try:
            from src.reports.email_sender import send_live_order_alert
            send_live_order_alert(
                symbol=symbol, order_id=order_id, quantity=qty,
                entry_price=float(entry_price), route=route,
                conviction=float(conviction or 0.0), side=str(side.value),
            )
        except Exception as e:
            logger.warning(f"[LIVE] email alert failed for {symbol}: {e}")

        result.filled_qty = qty
        result.avg_price = float(entry_price)
        return result

    # ── LONG entry ────────────────────────────────────────────────────────────
    def execute_buy(
        self,
        symbol: str,
        ltp: float,
        qty: int,
        market_regime: Optional[MarketRegime] = None,
        symbol_stats: Optional[SymbolStats] = None,
        route: str = "UNROUTED",
        confidence: float = 0.0,
        target: float = 0.0,
        sl: float = 0.0,
        conviction: float = 0.0,
    ) -> OrderResult:
        """Execute a LONG entry. qty is already computed from ATR-based sizing."""
        if ltp <= 0 or qty <= 0:
            return OrderResult(
                success=False, broker_order_id=None,
                message=f"Invalid ltp={ltp} or qty={qty} for {symbol}", filled_qty=0
            )

        if not self.risk.live_mode or self._zerodha is None:
            msg = (
                f"[DRY-RUN] {symbol} | route={route or 'UNROUTED'} | "
                f"confidence={float(confidence or 0.0):.2f} | entry={ltp:.2f} | "
                f"target={float(target or 0.0):.2f} | sl={float(sl or 0.0):.2f} | qty={qty} | side=BUY"
            )
            if _dry_run_log_enabled():
                logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)

        return self._place_live_market_order(
            symbol=symbol, side=OrderSide.BUY, entry_price=ltp,
            conviction=conviction, route=route, confidence=confidence,
        )

    # ── LONG exit ─────────────────────────────────────────────────────────────
    def execute_sell(
        self,
        symbol: str,
        qty: int,
        ltp: float,
        route: str = "UNROUTED",
        confidence: float = 0.0,
        target: float = 0.0,
        sl: float = 0.0,
    ) -> OrderResult:
        """Exit a LONG position."""
        if qty <= 0 or ltp <= 0:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"Invalid qty={qty} or ltp={ltp}", filled_qty=0)

        if not self.risk.live_mode or self._zerodha is None:
            msg = (
                f"[DRY-RUN] {symbol} | route={route or 'UNROUTED'} | "
                f"confidence={float(confidence or 0.0):.2f} | entry={ltp:.2f} | "
                f"target={float(target or 0.0):.2f} | sl={float(sl or 0.0):.2f} | qty={qty} | side=SELL"
            )
            if _dry_run_log_enabled():
                logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)
        try:
            from src.config_loader import get_live_order_tag
            req = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=qty,
                               order_type=OrderType.MARKET, timestamp=datetime.now(),
                               tag=get_live_order_tag())
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"SELL crash {symbol}: {e}", filled_qty=0)

    # ── SHORT entry ───────────────────────────────────────────────────────────
    def execute_short_sell(
        self,
        symbol: str,
        ltp: float,
        qty: int,
        route: str = "UNROUTED",
        confidence: float = 0.0,
        target: float = 0.0,
        sl: float = 0.0,
        conviction: float = 0.0,
    ) -> OrderResult:
        """
        Enter a SHORT position by selling shares we don't own (sell-to-open).
        Requires the broker to support intraday shorting (Kite supports MIS orders).
        """
        if ltp <= 0 or qty <= 0:
            return OrderResult(
                success=False, broker_order_id=None,
                message=f"Invalid ltp={ltp} or qty={qty} for SHORT {symbol}", filled_qty=0
            )

        # SHORT-6/7: Ban list + T2T gate
        if not is_safe_to_short(symbol):
            msg = f"SHORT blocked by ban list / T2T restriction: {symbol}"
            logger.warning(msg)
            return OrderResult(success=False, broker_order_id=None, message=msg, filled_qty=0)

        if not self.risk.live_mode or self._zerodha is None:
            msg = (
                f"[DRY-RUN] {symbol} | route={route or 'UNROUTED'} | "
                f"confidence={float(confidence or 0.0):.2f} | entry={ltp:.2f} | "
                f"target={float(target or 0.0):.2f} | sl={float(sl or 0.0):.2f} | qty={qty} | side=SHORT_SELL"
            )
            if _dry_run_log_enabled():
                logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)

        return self._place_live_market_order(
            symbol=symbol, side=OrderSide.SELL, entry_price=ltp,
            conviction=conviction, route=route, confidence=confidence,
        )

    # ── SHORT exit (buy-to-cover) ─────────────────────────────────────────────
    def execute_short_cover(
        self,
        symbol: str,
        qty: int,
        ltp: float,
        route: str = "UNROUTED",
        confidence: float = 0.0,
        target: float = 0.0,
        sl: float = 0.0,
    ) -> OrderResult:
        """
        Close a SHORT position by buying back (buy-to-cover).
        """
        if qty <= 0 or ltp <= 0:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"Invalid qty={qty} or ltp={ltp} for COVER {symbol}",
                               filled_qty=0)

        if not self.risk.live_mode or self._zerodha is None:
            msg = (
                f"[DRY-RUN] {symbol} | route={route or 'UNROUTED'} | "
                f"confidence={float(confidence or 0.0):.2f} | entry={ltp:.2f} | "
                f"target={float(target or 0.0):.2f} | sl={float(sl or 0.0):.2f} | qty={qty} | side=SHORT_COVER"
            )
            if _dry_run_log_enabled():
                logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)
        try:
            from src.config_loader import get_live_order_tag
            req = OrderRequest(symbol=symbol, side=OrderSide.BUY, quantity=qty,
                               order_type=OrderType.MARKET, timestamp=datetime.now(),
                               tag=get_live_order_tag())
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"SHORT_COVER crash {symbol}: {e}", filled_qty=0)
