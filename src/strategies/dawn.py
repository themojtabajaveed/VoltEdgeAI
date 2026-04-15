"""
dawn.py — DAWN (Day's Alpha Watch Network)
-------------------------------------------
Pre-market catalyst strategy. Bridges the 30-minute gap between
morning brief (08:45 IST) and HYDRA scan (09:00 IST).

Trades LONG and SHORT based on pre-market catalysts.
DRY-RUN ONLY — no live orders, ever (until explicitly enabled).

Fully independent of ConvictionEngine, SlotManager, TradeExecutor.
Own scanner, own scoring, own position tracking, own CSV logging.
"""
import os
import csv
import json
import logging
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Optional, Tuple, Dict

logger = logging.getLogger(__name__)

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

# ── Constants ────────────────────────────────────────────────────────────
MAX_SIGNALS_PER_DAY = 5
HARD_SL_PCT = 0.02             # 2% hard cap
TIGHT_SL_PCT = 0.015           # 1.5% for gap > 5%
LONG_THRESHOLD = 60
SHORT_THRESHOLD = 65
CSV_DIR = "logs/dawn_dryrun"

CSV_COLUMNS = [
    "date", "symbol", "direction", "catalyst", "catalyst_strength",
    "dawn_score", "entry_price", "current_price", "sl_price", "sl_type",
    "pnl_pct", "pnl_rupees", "status", "exit_price", "exit_time",
    "exit_reason", "max_favorable", "max_adverse", "liquidity_warning",
    "notes",
]

# Catalyst types accepted for LONG and SHORT
LONG_CATALYST_TYPES = {
    "FDA_APPROVAL", "CONTRACT_WIN", "FDI", "EARNINGS_BEAT",
    "REGULATORY_APPROVAL", "MERGER", "BLOCK_DEAL_LARGE",
    "UPGRADE", "PRODUCT_LAUNCH", "EXPANSION",
}
SHORT_CATALYST_TYPES = {
    "EARNINGS_MISS", "DOWNGRADE", "REGULATORY_ACTION",
    "FRAUD_ALLEGATION", "KEY_DEPARTURE", "DEBT_DEFAULT",
    "BLOCK_DEAL_SELL",
}


# ── Dataclasses ──────────────────────────────────────────────────────────

@dataclass
class DawnCandidate:
    """A pre-qualified catalyst candidate before scoring."""
    symbol: str
    direction: str              # "BUY" or "SHORT"
    catalyst: str
    catalyst_type: str
    source: str                 # "DAWN_SCAN", "MORNING_BRIEF", "BOTH"
    urgency: float              # 1-10
    catalyst_strength: str      # "HIGH", "MEDIUM", "LOW"
    freshness_hours: float = 12.0
    avg_daily_turnover: float = 0.0   # 0 = unknown
    metadata: dict = field(default_factory=dict)


@dataclass
class DawnSignal:
    """A qualified, scored DAWN signal for virtual trading."""
    symbol: str
    direction: str
    catalyst: str
    catalyst_type: str
    source: str
    dawn_score: float
    entry_price: float = 0.0
    current_price: float = 0.0
    sl_price: float = 0.0
    sl_type: str = "INITIAL"
    status: str = "PENDING"
    exit_price: float = 0.0
    exit_time: Optional[str] = None
    exit_reason: str = ""
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    pnl_pct: float = 0.0
    pnl_rupees: float = 0.0
    liquidity_warning: bool = False
    notes: str = ""
    created_at: Optional[datetime] = None
    quantity: int = 0
    # Scoring breakdown
    score_catalyst: float = 0.0
    score_freshness: float = 0.0
    score_liquidity: float = 0.0
    score_technical: float = 0.0
    score_context: float = 0.0


@dataclass
class DawnDailyMetrics:
    """End-of-day performance metrics."""
    date: str
    total_signals: int = 0
    active_signals: int = 0
    stopped_signals: int = 0
    closed_signals: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    total_pnl_rupees: float = 0.0
    max_favorable_avg: float = 0.0
    max_adverse_avg: float = 0.0
    source_breakdown: dict = field(default_factory=dict)


# ── HYDRA Integration ────────────────────────────────────────────────────

def load_hydra_watchlist() -> list[dict]:
    """Load HYDRA watchlist saved at 08:15. Returns empty list if missing."""
    path = pathlib.Path("data/hydra_watchlist.json")
    if not path.exists():
        logger.warning("[DAWN] hydra_watchlist.json not found — running without HYDRA input")
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        candidates = data.get("candidates", [])
        logger.info(f"[DAWN] Loaded {len(candidates)} HYDRA candidates")
        return candidates
    except Exception as e:
        logger.error(f"[DAWN] Failed to load hydra_watchlist: {e}")
        return []


def _fetch_history_signals(symbol: str) -> dict:
    """
    Single yfinance history call for L3/L5/L6/L7/L8 scoring.
    Returns dict with safe defaults on any failure. Never raises.
    """
    defaults = {
        "rel_volume": 1.0,
        "vs_30d_high_pct": 0.0,
        "vs_20d_sma_pct": 0.0,
        "gap_followthrough_rate": 0.5,
        "momentum_7d_pct": 0.0,
        "momentum_30d_pct": 0.0,
        "avg_daily_volume": 0,
    }
    try:
        import yfinance as yf
        hist = yf.Ticker(f"{symbol}.NS").history(period="2mo")
        if hist.empty or len(hist) < 10:
            return defaults

        close = hist["Close"]
        volume = hist["Volume"]
        high = hist["High"]
        open_ = hist["Open"]

        # L3 — relative volume (yesterday vs 20d avg)
        avg_vol_20d = volume.iloc[-21:-1].mean()
        last_vol = volume.iloc[-1]
        rel_volume = (last_vol / avg_vol_20d) if avg_vol_20d > 0 else 1.0

        # L5 — technical setup
        high_30d = high.iloc[-30:].max()
        sma_20d = close.iloc[-20:].mean()
        last_close = close.iloc[-1]
        vs_30d_high_pct = ((last_close - high_30d) / high_30d) * 100
        vs_20d_sma_pct = ((last_close - sma_20d) / sma_20d) * 100

        # L6 — gap follow-through rate (last 30 days)
        follow_throughs = 0
        gap_days = 0
        prev_closes = close.iloc[-31:-1].values
        opens = open_.iloc[-30:].values
        closes = close.iloc[-30:].values
        for i in range(min(30, len(opens))):
            if i >= len(prev_closes):
                break
            gap = opens[i] - prev_closes[i]
            if abs(gap) > 0.3:
                gap_days += 1
                if gap > 0 and closes[i] > opens[i]:
                    follow_throughs += 1
                elif gap < 0 and closes[i] < opens[i]:
                    follow_throughs += 1
        gap_followthrough_rate = (follow_throughs / gap_days) if gap_days >= 3 else 0.5

        # L7 — momentum
        momentum_7d = ((close.iloc[-1] - close.iloc[-8]) / close.iloc[-8]) * 100
        momentum_30d = ((close.iloc[-1] - close.iloc[-31]) / close.iloc[-31]) * 100

        # L8 — liquidity
        avg_daily_volume = int(volume.iloc[-20:].mean())

        return {
            "rel_volume": round(rel_volume, 2),
            "vs_30d_high_pct": round(vs_30d_high_pct, 2),
            "vs_20d_sma_pct": round(vs_20d_sma_pct, 2),
            "gap_followthrough_rate": round(gap_followthrough_rate, 2),
            "momentum_7d_pct": round(momentum_7d, 2),
            "momentum_30d_pct": round(momentum_30d, 2),
            "avg_daily_volume": avg_daily_volume,
        }
    except Exception as e:
        logger.debug(f"[DAWN] History signals failed for {symbol}: {e}")
        return defaults


def score_dawn_conviction(candidate: dict) -> tuple[int, dict]:
    """
    Score a HYDRA candidate for DAWN gap-up/gap-down potential at 9:15 open.
    Returns (total_score, breakdown_dict). Score capped at 100.

    Scoring layers (8 total):
      L1 — Catalyst strength   : 0-25 pts
      L2 — Pre-market gap      : 0-20 pts
      L3 — Relative volume     : 0-15 pts  (yfinance history)
      L4 — Sector momentum     : 0-8  pts
      L5 — Technical setup     : 0-12 pts  (yfinance history)
      L6 — Gap follow-through  : 0-8  pts  (yfinance history)
      L7 — Momentum 7d/30d     : 0-7  pts  (yfinance history)
      L8 — Liquidity           : 0-5  pts  (yfinance history)
    """
    symbol = candidate.get("symbol", "")
    direction = candidate.get("direction", "LONG")
    if direction == "BUY":
        direction = "LONG"
    breakdown: dict = {}
    total = 0

    # L1 — catalyst strength (0-25)
    cs = candidate.get("catalyst_strength", "LOW")
    l1 = 25 if cs == "HIGH" else 18 if cs == "MEDIUM" else 8
    breakdown["L1_catalyst"] = l1
    total += l1

    # L2 — gap_pct (0-20)
    gap = abs(candidate.get("gap_pct", 0.0))
    l2 = 20 if gap >= 3.0 else 15 if gap >= 2.0 else 8 if gap >= 1.0 else 4 if gap >= 0.5 else 0
    breakdown["L2_gap"] = l2
    total += l2

    # Fetch history signals once for L3/L5/L6/L7/L8
    hist = _fetch_history_signals(symbol) if symbol else {}

    # L3 — relative volume (0-15)
    rv = hist.get("rel_volume", 1.0)
    l3 = 15 if rv >= 3.0 else 10 if rv >= 2.0 else 6 if rv >= 1.5 else 0
    breakdown["L3_volume"] = l3
    total += l3

    # L4 — sector momentum (0-8)
    sm = candidate.get("sector_momentum", "NEUTRAL")
    if (sm == "BULLISH" and direction == "LONG") or (sm == "BEARISH" and direction == "SHORT"):
        l4 = 8
    elif sm == "NEUTRAL":
        l4 = 4
    else:
        l4 = 0
    breakdown["L4_sector"] = l4
    total += l4

    # L5 — technical setup (0-12)
    vs_high = hist.get("vs_30d_high_pct", 0.0)
    vs_sma = hist.get("vs_20d_sma_pct", 0.0)
    if vs_high >= 0:
        l5 = 12
    elif vs_sma >= 0:
        l5 = 7
    elif vs_sma >= -3:
        l5 = 3
    else:
        l5 = 0
    breakdown["L5_technical"] = l5
    total += l5

    # L6 — gap follow-through rate (0-8)
    ftr = hist.get("gap_followthrough_rate", 0.5)
    l6 = 8 if ftr >= 0.70 else 4 if ftr >= 0.50 else 0
    breakdown["L6_followthrough"] = l6
    total += l6

    # L7 — momentum (0-7)
    m7 = hist.get("momentum_7d_pct", 0.0)
    m30 = hist.get("momentum_30d_pct", 0.0)
    dir_mult = 1 if direction == "LONG" else -1
    l7 = 0
    if m7 * dir_mult > 3.0:
        l7 += 4
    if m30 * dir_mult > 8.0:
        l7 += 3
    breakdown["L7_momentum"] = l7
    total += l7

    # L8 — liquidity (0-5)
    avg_vol = hist.get("avg_daily_volume", 0)
    l8 = 5 if avg_vol >= 1_000_000 else 3 if avg_vol >= 500_000 else 1 if avg_vol >= 200_000 else 0
    breakdown["L8_liquidity"] = l8
    total += l8

    final = min(total, 100)
    breakdown["total"] = final
    logger.debug(f"[DAWN] {symbol} conviction={final} | {breakdown}")
    return final, breakdown


def select_dawn_candidates(min_conviction: int = 50) -> list[dict]:
    """
    Load HYDRA watchlist, score each candidate for gap-open potential,
    filter by min_conviction. Returns sorted list (highest first), max 5.
    """
    candidates = load_hydra_watchlist()
    if not candidates:
        return []

    scored = []
    for c in candidates:
        score, breakdown = score_dawn_conviction(c)
        if score >= min_conviction:
            scored.append({
                **c,
                "dawn_conviction": score,
                "dawn_breakdown": breakdown,
            })

    scored.sort(key=lambda x: x["dawn_conviction"], reverse=True)
    result = scored[:5]
    summary = [f"{c['symbol']}={c['dawn_conviction']}" for c in result]
    logger.info(
        f"[DAWN] {len(result)} HYDRA candidates above conviction {min_conviction}: {summary}"
    )
    return result


def format_dawn_email(candidates: list[dict], date_str: str) -> tuple[str, str]:
    """
    Format DAWN candidates into email subject + plain-text body.
    Returns (subject, body_md).
    """
    if not candidates:
        subject = f"VoltEdge DAWN | {date_str} | No candidates above threshold"
        body = (
            f"DAWN Pre-Market Scanner — {date_str}\n\n"
            f"No stocks cleared the minimum conviction threshold (50/100) today.\n"
            f"HYDRA watchlist was scanned. Market opens at 09:15 IST.\n"
        )
        return subject, body

    top = candidates[0]
    subject = (
        f"VoltEdge DAWN | {date_str} | "
        f"{len(candidates)} candidate(s) | "
        f"Top: {top['symbol']} {top['direction']} conviction={top['dawn_conviction']}"
    )

    lines = [
        f"DAWN Pre-Market Scanner — {date_str}",
        f"Scan complete at 08:45 IST | Market opens 09:15 IST",
        f"Candidates above threshold: {len(candidates)} (max 5)",
        f"Strategy: Buy/Short at 09:15 open | Trailing SL | No time stop",
        "=" * 60,
        "",
    ]

    for i, c in enumerate(candidates, 1):
        direction = c.get("direction", "UNKNOWN")
        symbol = c.get("symbol", "?")
        score = c.get("dawn_conviction", 0)
        breakdown = c.get("dawn_breakdown", {})
        gap = c.get("gap_pct", 0.0)
        catalyst = c.get("catalyst_strength", "?")
        sector_mom = c.get("sector_momentum", "?")
        summary = c.get("event_summary", "No catalyst summary available.")

        gap_note = f"{gap:+.2f}% vs prev close" if gap != 0.0 else "gap unknown (pre-market)"
        action = "BUY at 09:15 open" if direction in ("LONG", "BUY") else "SHORT at 09:15 open"
        sl_side = "below" if direction in ("LONG", "BUY") else "above"

        lines += [
            f"#{i} {symbol} — {direction} | Conviction: {score}/100",
            f"   Catalyst Strength : {catalyst}",
            f"   Pre-Market Gap    : {gap_note}",
            f"   Sector Momentum   : {sector_mom}",
            f"   Score Breakdown   : {breakdown}",
            f"   Catalyst Summary  : {summary}",
            f"   Action            : {action}",
            f"   Stop Loss         : 1.5% {sl_side} entry",
            f"   Target            : trailing SL — no fixed target, no time stop",
            "",
        ]

    lines += [
        "=" * 60,
        "VoltEdge | Dry Run Only | Not financial advice",
    ]

    body = "\n".join(lines)
    return subject, body


def send_dawn_email(candidates: list[dict]) -> bool:
    """Send DAWN pre-market email. Returns True if sent successfully."""
    from src.reports.email_sender import send_report_email
    from datetime import timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    date_str = datetime.now(IST).strftime("%Y-%m-%d")

    subject, body_md = format_dawn_email(candidates, date_str)

    try:
        sent = send_report_email(subject=subject, body_md=body_md)
        if sent:
            logger.info(f"[DAWN] Email sent — {len(candidates)} candidate(s)")
        else:
            logger.warning(f"[DAWN] Email send returned False — {len(candidates)} candidate(s)")
        return sent
    except Exception as e:
        logger.error(f"[DAWN] Email send failed: {type(e).__name__}: {e}")
        return False


# ── Dry-Run Execution ─────────────────────────────────────────────────────

_DRYRUN_CAPITAL = 5000.0  # hypothetical capital per trade (₹)


def _yf_last_price(symbol: str) -> float:
    """Fetch last traded / pre-market price for an NSE symbol via yfinance. 0.0 on failure."""
    try:
        import yfinance as yf
        fi = yf.Ticker(f"{symbol}.NS").fast_info
        price = fi["last_price"]
        return float(price) if price else 0.0
    except Exception as e:
        logger.debug(f"[DAWN] _yf_last_price({symbol}) failed: {e}")
        return 0.0


def execute_dawn_dryrun(candidates: list[dict], entry_prices: dict[str, float]) -> None:
    """
    Record DAWN dry-run entries at 09:15 open price.

    candidates   — output of select_dawn_candidates()
    entry_prices — {symbol: open_price}; missing symbols fetched via yfinance.

    For each candidate:
      • entry_price from entry_prices or yfinance fallback
      • initial_sl  = entry_price * 0.985 (LONG) / * 1.015 (SHORT)
      • status = ACTIVE
    Appends rows to logs/dawn_dryrun/YYYY-MM-DD.csv (header written once).
    """
    if not candidates:
        logger.info("[DAWN] execute_dawn_dryrun: no candidates — nothing to record")
        return

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    os.makedirs(CSV_DIR, exist_ok=True)
    csv_path = os.path.join(CSV_DIR, f"{today_str}.csv")
    file_is_new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0

    try:
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if file_is_new:
                writer.writeheader()

            for c in candidates:
                symbol = c.get("symbol", "")
                if not symbol:
                    continue

                direction = c.get("direction", "BUY")

                # Resolve entry price
                entry_price = float(entry_prices.get(symbol, 0.0))
                if not entry_price:
                    entry_price = _yf_last_price(symbol)
                if not entry_price:
                    logger.warning(f"[DAWN] {symbol}: no entry price — skipping dryrun entry")
                    continue

                # Initial SL and quantity
                if direction in ("LONG", "BUY"):
                    sl_price = round(entry_price * 0.985, 2)
                else:
                    sl_price = round(entry_price * 1.015, 2)
                quantity = max(1, int(_DRYRUN_CAPITAL / entry_price))

                writer.writerow({
                    "date": today_str,
                    "symbol": symbol,
                    "direction": direction,
                    "catalyst": c.get("event_summary", ""),
                    "catalyst_strength": c.get("catalyst_strength", ""),
                    "dawn_score": c.get("dawn_conviction", 0),
                    "entry_price": f"{entry_price:.2f}",
                    "current_price": f"{entry_price:.2f}",
                    "sl_price": f"{sl_price:.2f}",
                    "sl_type": "INITIAL_1.5",
                    "pnl_pct": "+0.00",
                    "pnl_rupees": "+0.00",
                    "status": "ACTIVE",
                    "exit_price": "",
                    "exit_time": "",
                    "exit_reason": "",
                    "max_favorable": "+0.00",
                    "max_adverse": "+0.00",
                    "liquidity_warning": "False",
                    "notes": f"HYDRA_DRYRUN qty={quantity}",
                })
                logger.info(
                    f"[DAWN] Dryrun entry: {symbol} {direction} @ {entry_price:.2f} "
                    f"SL={sl_price:.2f} qty={quantity}"
                )
    except Exception as e:
        logger.error(f"[DAWN] execute_dawn_dryrun failed: {e}")


def update_dawn_dryrun() -> None:
    """
    Update ACTIVE rows in today's DAWN dryrun CSV.
    Called every 15 minutes during market hours (09:15–15:30).

    For each ACTIVE row:
      • Fetch current price via yfinance
      • Update current_price, pnl_pct, pnl_rupees, max_favorable, max_adverse
      • Trail SL: LONG → max(sl, current * 0.985) | SHORT → min(sl, current * 1.015)
      • Exit if SL hit: status=CLOSED, exit_reason=SL_HIT
    No time stop — rows remain ACTIVE until SL is hit.
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    csv_path = os.path.join(CSV_DIR, f"{today_str}.csv")

    if not os.path.exists(csv_path):
        return

    try:
        with open(csv_path, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.error(f"[DAWN] update_dawn_dryrun read failed: {e}")
        return

    any_updated = False

    for row in rows:
        if row.get("status") != "ACTIVE":
            continue

        symbol = row.get("symbol", "")
        if not symbol:
            continue

        current_price = _yf_last_price(symbol)
        if not current_price:
            logger.debug(f"[DAWN] {symbol}: no price — skipping update")
            continue

        direction = row.get("direction", "BUY")

        try:
            entry_price = float(row.get("entry_price") or 0)
            sl_price = float(row.get("sl_price") or 0)
            max_fav = float(row.get("max_favorable") or 0)
            max_adv = float(row.get("max_adverse") or 0)
        except ValueError:
            logger.debug(f"[DAWN] {symbol}: unparseable CSV fields — skipping")
            continue

        if not entry_price or not sl_price:
            continue

        # P&L
        if direction in ("LONG", "BUY"):
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        quantity = max(1, int(_DRYRUN_CAPITAL / entry_price))
        pnl_rupees = pnl_pct / 100 * entry_price * quantity

        max_fav = max(max_fav, pnl_pct)
        max_adv = min(max_adv, pnl_pct)

        # SL check → exit
        sl_hit = (
            (direction in ("LONG", "BUY") and current_price <= sl_price) or
            (direction == "SHORT" and current_price >= sl_price)
        )
        if sl_hit:
            row["status"] = "CLOSED"
            row["exit_price"] = f"{current_price:.2f}"
            row["exit_time"] = datetime.now(IST).strftime("%H:%M:%S")
            row["exit_reason"] = "SL_HIT"
            logger.info(f"[DAWN] {symbol} SL_HIT @ {current_price:.2f} (SL was {sl_price:.2f})")
        else:
            # Trail SL (ratchet only — never moves against position)
            if direction in ("LONG", "BUY"):
                new_sl = max(sl_price, round(current_price * 0.985, 2))
            else:
                new_sl = min(sl_price, round(current_price * 1.015, 2))
            if new_sl != sl_price:
                row["sl_price"] = f"{new_sl:.2f}"
                logger.debug(f"[DAWN] {symbol} SL trailed {sl_price:.2f} → {new_sl:.2f}")

        row["current_price"] = f"{current_price:.2f}"
        row["pnl_pct"] = f"{pnl_pct:+.2f}"
        row["pnl_rupees"] = f"{pnl_rupees:+.2f}"
        row["max_favorable"] = f"{max_fav:+.2f}"
        row["max_adverse"] = f"{max_adv:+.2f}"
        any_updated = True

    if not any_updated:
        return

    try:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        active_count = sum(1 for r in rows if r.get("status") == "ACTIVE")
        logger.info(f"[DAWN] Dryrun CSV updated — {active_count} still ACTIVE")
    except Exception as e:
        logger.error(f"[DAWN] update_dawn_dryrun write failed: {e}")


# ── Main Strategy Class ─────────────────────────────────────────────────

class DawnStrategy:
    """
    DAWN — Day's Alpha Watch Network.
    Pre-market catalyst strategy. DRY-RUN ONLY.
    Fully independent of ConvictionEngine / SlotManager / TradeExecutor.
    """

    def __init__(self) -> None:
        self._candidates: List[DawnCandidate] = []
        self._signals: List[DawnSignal] = []
        self._daily_metrics: Optional[DawnDailyMetrics] = None
        self._hypothetical_capital: float = 5000.0
        self._is_active: bool = False
        self._today: Optional[date] = None
        logger.info("[DAWN] Strategy initialized (dry-run mode)")

    # ══════════════════════════════════════════════════════════════════════
    # PRE-MARKET PHASE
    # ══════════════════════════════════════════════════════════════════════

    def pre_market_scan(self) -> List[DawnCandidate]:
        """
        Source B: Independent catalyst scan via EventScanner + Brave Search.
        Called at 08:30 IST, BEFORE morning brief.
        """
        candidates: List[DawnCandidate] = []
        self._today = datetime.now(IST).date()

        # ── EventScanner: overnight corporate events ──
        try:
            from src.data_ingestion.event_scanner import EventScanner
            scanner = EventScanner()
            raw_events = scanner.scan_since_close()
            if raw_events:
                classified = scanner.classify_events(raw_events)
                for ev in classified:
                    if ev.urgency < 5.0 or ev.direction == "NEUTRAL":
                        continue
                    candidates.append(DawnCandidate(
                        symbol=ev.symbol,
                        direction=ev.direction,
                        catalyst=ev.summary or ev.headline,
                        catalyst_type=ev.event_type or ev.category or "UNKNOWN",
                        source="DAWN_SCAN",
                        urgency=ev.urgency,
                        catalyst_strength="HIGH" if ev.urgency >= 8 else "MEDIUM" if ev.urgency >= 6 else "LOW",
                        freshness_hours=max(0, (datetime.now() - (ev.timestamp or datetime.now())).total_seconds() / 3600),
                    ))
                logger.info(f"[DAWN] EventScanner: {len(candidates)} candidates from {len(classified)} events")
        except Exception as e:
            logger.error(f"[DAWN] EventScanner failed: {e}")

        # ── Brave Search: company-specific catalysts ──
        try:
            from src.llm.brief_analyzer import fetch_brave_news
            brave_queries = [
                "NSE stock FDA approval contract win regulatory approval today",
                "India company earnings results beat miss quarterly today",
                "NSE corporate action merger acquisition FDI investment today",
            ]
            news = fetch_brave_news(brave_queries, max_results_per_query=5)
            if news:
                brave_candidates = self._parse_brave_news(news)
                # Merge with existing, dedup by symbol
                existing_syms = {c.symbol for c in candidates}
                for bc in brave_candidates:
                    if bc.symbol not in existing_syms:
                        candidates.append(bc)
                        existing_syms.add(bc.symbol)
                logger.info(f"[DAWN] Brave Search: {len(brave_candidates)} candidates extracted")
        except Exception as e:
            logger.error(f"[DAWN] Brave Search failed: {e}")

        self._candidates = candidates
        return candidates

    def _parse_brave_news(self, news_items: List[dict]) -> List[DawnCandidate]:
        """Extract DawnCandidates from Brave Search results using Groq."""
        try:
            from src.llm.brief_analyzer import _call_groq
            # Build a prompt asking Groq to extract tradeable catalysts
            headlines = "\n".join(
                f"- {n.get('title', '')} | {n.get('description', '')[:100]}"
                for n in news_items[:15]
            )
            prompt = f"""Analyze these Indian stock market news headlines. Extract stocks with company-specific catalysts (NOT sector-wide themes).

{headlines}

Return JSON array of objects with fields: symbol (NSE ticker), direction (BUY or SHORT), catalyst (one-line), catalyst_type (FDA_APPROVAL/CONTRACT_WIN/FDI/EARNINGS_BEAT/REGULATORY_APPROVAL/MERGER/EARNINGS_MISS/DOWNGRADE/REGULATORY_ACTION), urgency (1-10).
Return ONLY stocks with clear company-specific catalysts. Return empty array [] if none found.
Return ONLY the JSON array, no other text."""

            result = _call_groq(prompt, max_tokens=800)
            if not result:
                return []

            # Parse response — may be in result dict or raw text
            raw = result if isinstance(result, str) else json.dumps(result)
            # Extract JSON array
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if not match:
                return []

            stocks = json.loads(match.group())
            candidates = []
            for s in stocks:
                sym = s.get("symbol", "").upper().strip()
                if not sym:
                    continue
                candidates.append(DawnCandidate(
                    symbol=sym,
                    direction=s.get("direction", "BUY"),
                    catalyst=s.get("catalyst", ""),
                    catalyst_type=s.get("catalyst_type", "UNKNOWN"),
                    source="DAWN_SCAN",
                    urgency=float(s.get("urgency", 5)),
                    catalyst_strength="HIGH" if float(s.get("urgency", 5)) >= 8 else "MEDIUM",
                    freshness_hours=4.0,
                ))
            return candidates
        except Exception as e:
            logger.warning(f"[DAWN] Brave news parsing failed: {e}")
            return []

    def merge_brief_and_generate(self, date_str: str) -> List[DawnSignal]:
        """
        Merge Source A (morning brief) + Source B (own scan), score, qualify,
        generate DAWN email. Called at 08:52 IST.
        """
        self._today = datetime.now(IST).date()

        # Source A: morning brief hot stocks
        brief_candidates = self._consume_morning_brief(date_str)
        if brief_candidates:
            logger.info(f"[DAWN] Morning brief: {len(brief_candidates)} candidates")
        else:
            logger.warning("[DAWN] Morning brief unavailable, operating Source B only")

        # Merge with Source B (self._candidates from pre_market_scan)
        all_candidates = list(self._candidates)  # Copy Source B
        existing_syms = {c.symbol for c in all_candidates}

        for bc in brief_candidates:
            if bc.symbol in existing_syms:
                # Same symbol in both — mark as BOTH, use higher urgency
                for existing in all_candidates:
                    if existing.symbol == bc.symbol:
                        existing.source = "BOTH"
                        existing.urgency = max(existing.urgency, bc.urgency)
                        if bc.catalyst_strength == "HIGH":
                            existing.catalyst_strength = "HIGH"
                        break
            else:
                all_candidates.append(bc)
                existing_syms.add(bc.symbol)

        # Source C: HYDRA pre-market watchlist (data/hydra_watchlist.json, written at 08:15)
        hydra_picks = select_dawn_candidates(min_conviction=50)
        for hc in hydra_picks:
            sym = hc.get("symbol", "")
            if not sym or sym in existing_syms:
                continue
            urgency = float(hc.get("urgency", 6.0))
            all_candidates.append(DawnCandidate(
                symbol=sym,
                direction=hc.get("direction", "BUY"),
                catalyst=hc.get("event_summary", "HYDRA event catalyst"),
                catalyst_type="UNKNOWN",   # "UNKNOWN" is in LONG/SHORT valid_types → passes _qualifies()
                source="HYDRA",
                urgency=urgency,
                catalyst_strength="HIGH" if urgency >= 8 else "MEDIUM" if urgency >= 6 else "LOW",
                freshness_hours=4.0,
            ))
            existing_syms.add(sym)
        if hydra_picks:
            logger.info(f"[DAWN] Source C (HYDRA): {len(hydra_picks)} candidates merged")
        send_dawn_email(hydra_picks)

        # Score and qualify
        scored_signals: List[DawnSignal] = []
        for candidate in all_candidates:
            passes, reason = self._qualifies(candidate)
            if not passes:
                logger.info(f"[DAWN] {candidate.symbol} rejected: {reason}")
                continue

            score = self._score_candidate(candidate)
            threshold = SHORT_THRESHOLD if candidate.direction == "SHORT" else LONG_THRESHOLD

            if score < threshold:
                logger.info(f"[DAWN] {candidate.symbol} below threshold: {score:.0f} < {threshold}")
                continue

            liq_warning = candidate.avg_daily_turnover > 0 and candidate.avg_daily_turnover < 2_000_000

            signal = DawnSignal(
                symbol=candidate.symbol,
                direction=candidate.direction,
                catalyst=candidate.catalyst,
                catalyst_type=candidate.catalyst_type,
                source=candidate.source,
                dawn_score=score,
                liquidity_warning=liq_warning,
                created_at=datetime.now(IST),
                score_catalyst=getattr(candidate, '_score_catalyst', 0),
                score_freshness=getattr(candidate, '_score_freshness', 0),
                score_liquidity=getattr(candidate, '_score_liquidity', 0),
                score_technical=getattr(candidate, '_score_technical', 0),
                score_context=getattr(candidate, '_score_context', 0),
            )
            scored_signals.append(signal)

        # Sort by score descending, take top MAX_SIGNALS_PER_DAY
        scored_signals.sort(key=lambda s: s.dawn_score, reverse=True)
        self._signals = scored_signals[:MAX_SIGNALS_PER_DAY]
        self._is_active = bool(self._signals)

        # Generate email (always — even with 0 signals)
        try:
            if self._signals:
                self._generate_email(self._signals, date_str, brief_available=bool(brief_candidates))
            else:
                self._send_no_signals_email(date_str)
        except Exception as e:
            logger.error(f"[DAWN] Email generation failed: {e}")

        logger.info(f"[DAWN] {len(self._signals)} qualified signals (max {MAX_SIGNALS_PER_DAY})")
        return self._signals

    def _consume_morning_brief(self, date_str: str) -> List[DawnCandidate]:
        """Parse morning brief hot stocks. Returns [] if brief failed."""
        candidates: List[DawnCandidate] = []

        # Try reading the morning brief markdown file
        brief_path = os.path.join("logs", "daily_reports", f"{date_str}_morning_brief.md")
        try:
            if not os.path.exists(brief_path):
                logger.warning(f"[DAWN] Morning brief not found: {brief_path}")
                return []

            with open(brief_path, "r") as f:
                content = f.read()

            # Extract the JSON block from the markdown
            json_match = re.search(r'```json\s*\n(.*?)\n```', content, re.DOTALL)
            if not json_match:
                logger.warning("[DAWN] No JSON block found in morning brief")
                return []

            regime_data = json.loads(json_match.group(1))
            predictions = regime_data.get("predictions", [])

            for pred in predictions:
                sym = pred.get("symbol", "").strip()
                if not sym:
                    continue
                direction = "BUY" if pred.get("predicted_direction") == "bullish" else "SHORT"
                reason = pred.get("reason", "")
                candidates.append(DawnCandidate(
                    symbol=sym,
                    direction=direction,
                    catalyst=reason,
                    catalyst_type="MORNING_BRIEF_PICK",
                    source="MORNING_BRIEF",
                    urgency=7.0,  # Default urgency for brief picks
                    catalyst_strength="MEDIUM",
                    freshness_hours=4.0,
                ))

            logger.info(f"[DAWN] Parsed {len(candidates)} candidates from morning brief")
        except Exception as e:
            logger.error(f"[DAWN] Morning brief parsing failed: {e}")

        return candidates

    # ══════════════════════════════════════════════════════════════════════
    # SCORING & QUALIFICATION
    # ══════════════════════════════════════════════════════════════════════

    def _qualifies(self, candidate: DawnCandidate) -> Tuple[bool, str]:
        """Apply hard filters. Returns (passes, rejection_reason)."""
        try:
            # Catalyst type filter (relaxed for MORNING_BRIEF_PICK)
            if candidate.catalyst_type != "MORNING_BRIEF_PICK":
                if candidate.direction == "BUY":
                    valid_types = LONG_CATALYST_TYPES | {"UNKNOWN"}
                else:
                    valid_types = SHORT_CATALYST_TYPES | {"UNKNOWN"}
                if candidate.catalyst_type not in valid_types:
                    return False, f"catalyst_type {candidate.catalyst_type} invalid for {candidate.direction}"

            # Catalyst strength
            if candidate.catalyst_strength == "LOW":
                return False, "catalyst_strength LOW"

            # Minimum urgency (SHORT has higher bar)
            min_urgency = 7.0 if candidate.direction == "SHORT" else 6.0
            if candidate.urgency < min_urgency:
                return False, f"urgency {candidate.urgency:.1f} < {min_urgency}"

            # F&O expiry day (Thursday)
            today = datetime.now(IST)
            if today.weekday() == 3:
                return False, "F&O expiry day (Thursday)"

            # Freshness filter
            if candidate.freshness_hours > 24:
                return False, f"stale catalyst ({candidate.freshness_hours:.0f}h old)"

            return True, ""
        except Exception as e:
            logger.error(f"[DAWN] Qualification error for {candidate.symbol}: {e}")
            return False, f"qualification error: {e}"

    def _score_candidate(self, candidate: DawnCandidate) -> float:
        """DAWN's own scoring. NOT the 5-layer conviction system. Returns 0-100."""
        try:
            # ── Catalyst Quality (0-40) ──
            if candidate.source == "BOTH":
                base = min(candidate.urgency * 4.0, 40.0)
                score_catalyst = min(40.0, base + 5.0)
            elif candidate.source == "DAWN_SCAN":
                score_catalyst = min(candidate.urgency * 4.0, 40.0)
            else:  # MORNING_BRIEF
                strength_map = {"HIGH": 35.0, "MEDIUM": 25.0, "LOW": 15.0}
                score_catalyst = strength_map.get(candidate.catalyst_strength, 20.0)

            # ── Catalyst Freshness (0-20) ──
            h = candidate.freshness_hours
            if h <= 4:
                score_freshness = 20.0
            elif h <= 12:
                score_freshness = 15.0
            elif h <= 24:
                score_freshness = 10.0
            else:
                score_freshness = 0.0

            # ── Liquidity (0-15) ──
            t = candidate.avg_daily_turnover
            if t <= 0:
                score_liquidity = 8.0  # Unknown
            elif t >= 50_000_000:
                score_liquidity = 15.0
            elif t >= 20_000_000:
                score_liquidity = 10.0
            elif t >= 5_000_000:
                score_liquidity = 5.0
            else:
                score_liquidity = 2.0

            # ── Technical Setup (0-15) — pre-market: neutral ──
            score_technical = 8.0

            # ── Market Context (0-10) ──
            score_context = self._get_context_score(candidate.direction)

            total = score_catalyst + score_freshness + score_liquidity + score_technical + score_context

            # Stash component scores for later reference
            candidate._score_catalyst = score_catalyst
            candidate._score_freshness = score_freshness
            candidate._score_liquidity = score_liquidity
            candidate._score_technical = score_technical
            candidate._score_context = score_context

            return max(0.0, min(100.0, total))
        except Exception as e:
            logger.error(f"[DAWN] Scoring error for {candidate.symbol}: {e}")
            return 0.0

    def _get_context_score(self, direction: str) -> float:
        """Market context score from daily regime."""
        try:
            regime_file = "data/daily_regime.json"
            if not os.path.exists(regime_file):
                return 5.0
            with open(regime_file, "r") as f:
                data = json.load(f)
            trend = data.get("trend", "sideways")

            if trend == "bullish" and direction == "BUY":
                return 10.0
            elif trend == "bearish" and direction == "SHORT":
                return 10.0
            elif trend == "sideways":
                return 5.0
            elif trend == "bullish" and direction == "SHORT":
                return 2.0
            elif trend == "bearish" and direction == "BUY":
                return 2.0
            return 5.0
        except Exception:
            return 5.0

    # ══════════════════════════════════════════════════════════════════════
    # MARKET PHASE — ENTRY
    # ══════════════════════════════════════════════════════════════════════

    def record_entries(self, live_client) -> None:
        """
        Capture open prices, record virtual entries, set initial SL.
        Called at 09:15-09:20 IST.
        """
        for signal in self._signals:
            if signal.status != "PENDING":
                continue
            try:
                tick = live_client.get_last_tick(signal.symbol)
                if tick is None or tick.ltp <= 0:
                    signal.status = "FAILED"
                    signal.notes = "NO_LTP_AT_OPEN"
                    logger.warning(f"[DAWN] {signal.symbol}: no LTP at open, marking FAILED")
                    continue

                signal.entry_price = tick.ltp
                signal.current_price = tick.ltp
                signal.quantity = max(1, int(self._hypothetical_capital / tick.ltp))

                # Gap check: estimate previous close from regime data or default
                prev_close = self._get_prev_close(signal.symbol)
                gap_pct = 0.0
                if prev_close and prev_close > 0:
                    gap_pct = abs(tick.ltp - prev_close) / prev_close * 100

                # Set initial SL
                if gap_pct > 5.0:
                    # Tight SL for large gaps (gap fill risk)
                    sl_pct = TIGHT_SL_PCT
                    signal.sl_type = "INITIAL_TIGHT"
                    signal.notes = f"GAP={gap_pct:.1f}%_TIGHT_SL"
                else:
                    sl_pct = HARD_SL_PCT
                    signal.sl_type = "INITIAL"

                if signal.direction == "BUY":
                    signal.sl_price = signal.entry_price * (1 - sl_pct)
                else:
                    signal.sl_price = signal.entry_price * (1 + sl_pct)

                signal.status = "ACTIVE"
                logger.info(
                    f"[DAWN] {signal.symbol} {signal.direction} entry={signal.entry_price:.2f} "
                    f"SL={signal.sl_price:.2f} ({signal.sl_type}) qty={signal.quantity}"
                )
            except Exception as e:
                signal.status = "FAILED"
                signal.notes = f"ENTRY_ERROR: {e}"
                logger.error(f"[DAWN] Entry recording failed for {signal.symbol}: {e}")

    def _get_prev_close(self, symbol: str) -> Optional[float]:
        """Get previous close price for gap calculation."""
        try:
            from src.data_ingestion.market_history import get_historical_data
            bars = get_historical_data(symbol, days=2)
            if bars and len(bars) >= 1:
                return bars[-1].close
        except Exception:
            pass
        return None

    # ══════════════════════════════════════════════════════════════════════
    # MARKET PHASE — POSITION MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    def manage_positions(self, live_client, technical_body=None) -> None:
        """
        Update prices, check SLs, trail stops, apply technical rules, update CSV.
        Called every 5 minutes during market hours.
        """
        for signal in self._signals:
            if signal.status != "ACTIVE":
                continue
            try:
                # Update current price
                tick = live_client.get_last_tick(signal.symbol)
                if tick and tick.ltp > 0:
                    signal.current_price = tick.ltp
                else:
                    continue  # Skip management if no price available

                # Update max favorable / adverse excursion
                if signal.direction == "BUY":
                    move_pct = (signal.current_price - signal.entry_price) / signal.entry_price * 100
                else:
                    move_pct = (signal.entry_price - signal.current_price) / signal.entry_price * 100

                signal.max_favorable = max(signal.max_favorable, move_pct)
                signal.max_adverse = min(signal.max_adverse, move_pct)

                # Apply technical rules (VWAP, ORB)
                snapshot = None
                if technical_body is not None:
                    try:
                        import pandas as pd
                        from src.data_ingestion.intraday_context import get_intraday_bars_for_symbol
                        bars = get_intraday_bars_for_symbol(signal.symbol, lookback_minutes=70)
                        if bars and len(bars) >= 5:
                            bars_df = pd.DataFrame([{
                                'date': b.timestamp, 'open': b.open, 'high': b.high,
                                'low': b.low, 'close': b.close, 'volume': b.volume
                            } for b in bars])
                            snapshot = technical_body.compute_or_stream(
                                signal.symbol, bars_df, latest_bar=bars[-1]
                            )
                    except Exception as ta_e:
                        logger.debug(f"[DAWN] TA snapshot failed for {signal.symbol}: {ta_e}")

                self._apply_technical_rules(signal, snapshot)

                # Trail stop loss
                self._update_stop_loss(signal)

                # Check stop hit
                if signal.direction == "BUY" and signal.current_price <= signal.sl_price:
                    signal.status = "STOPPED"
                    signal.exit_price = signal.sl_price
                    signal.exit_time = datetime.now(IST).strftime("%H:%M:%S")
                    signal.exit_reason = f"SL_HIT ({signal.sl_type})"
                    self._compute_pnl(signal)
                    logger.info(f"[DAWN] {signal.symbol} STOPPED at {signal.exit_price:.2f} ({signal.sl_type})")
                elif signal.direction == "SHORT" and signal.current_price >= signal.sl_price:
                    signal.status = "STOPPED"
                    signal.exit_price = signal.sl_price
                    signal.exit_time = datetime.now(IST).strftime("%H:%M:%S")
                    signal.exit_reason = f"SL_HIT ({signal.sl_type})"
                    self._compute_pnl(signal)
                    logger.info(f"[DAWN] {signal.symbol} STOPPED at {signal.exit_price:.2f} ({signal.sl_type})")
                else:
                    # Update running PnL
                    self._compute_pnl(signal)

            except Exception as e:
                logger.error(f"[DAWN] Position management failed for {signal.symbol}: {e}")

        # Write CSV after each management cycle
        try:
            self._write_csv()
        except Exception as e:
            logger.error(f"[DAWN] CSV write failed: {e}")

    def _apply_technical_rules(self, signal: DawnSignal, snapshot) -> None:
        """Apply VWAP, volume, ORB rules to modify SL behavior."""
        if snapshot is None:
            return
        try:
            # VWAP cross rule — tighten SL if price crosses VWAP against position
            vwap = getattr(snapshot, 'vwap', 0)
            if vwap and vwap > 0:
                if signal.direction == "BUY" and signal.current_price < vwap:
                    distance = signal.entry_price - signal.sl_price
                    tightened = signal.entry_price - (distance * 0.5)
                    if tightened > signal.sl_price:
                        signal.sl_price = tightened
                        if "VWAP_TIGHT" not in signal.notes:
                            signal.notes += " VWAP_TIGHT"
                elif signal.direction == "SHORT" and signal.current_price > vwap:
                    distance = signal.sl_price - signal.entry_price
                    tightened = signal.entry_price + (distance * 0.5)
                    if tightened < signal.sl_price:
                        signal.sl_price = tightened
                        if "VWAP_TIGHT" not in signal.notes:
                            signal.notes += " VWAP_TIGHT"

            # Volume confirmation (informational only)
            vol_ratio = getattr(snapshot, 'volume_spike_ratio', 0)
            if vol_ratio and vol_ratio >= 2.0:
                if "HIGH_VOL" not in signal.notes:
                    signal.notes += " HIGH_VOL"

            # ORB interaction (informational only)
            orb_high = getattr(snapshot, 'orb_high', 0)
            orb_low = getattr(snapshot, 'orb_low', 0)
            if orb_high and orb_low and orb_high > 0:
                if signal.direction == "BUY" and signal.current_price > orb_high:
                    if "ORB_BREAK" not in signal.notes:
                        signal.notes += " ORB_BREAK"
                elif signal.direction == "SHORT" and signal.current_price < orb_low:
                    if "ORB_BREAK" not in signal.notes:
                        signal.notes += " ORB_BREAK"

        except Exception as e:
            logger.debug(f"[DAWN] Technical rules error for {signal.symbol}: {e}")

    def _update_stop_loss(self, signal: DawnSignal) -> None:
        """Trail stop loss based on favorable price movement."""
        if signal.entry_price <= 0:
            return

        if signal.direction == "BUY":
            move_pct = (signal.current_price - signal.entry_price) / signal.entry_price * 100
            new_sl = signal.sl_price
            new_type = signal.sl_type

            if move_pct >= 5.0:
                candidate_sl = signal.entry_price * 1.03
                if candidate_sl > new_sl:
                    new_sl = candidate_sl
                    new_type = "TRAIL_3"
            elif move_pct >= 3.0:
                candidate_sl = signal.entry_price * 1.015
                if candidate_sl > new_sl:
                    new_sl = candidate_sl
                    new_type = "TRAIL_1.5"
            elif move_pct >= 2.0:
                candidate_sl = signal.entry_price * 1.01
                if candidate_sl > new_sl:
                    new_sl = candidate_sl
                    new_type = "TRAIL_1"
            elif move_pct >= 1.0:
                candidate_sl = signal.entry_price
                if candidate_sl > new_sl:
                    new_sl = candidate_sl
                    new_type = "BREAKEVEN"

            # SL only moves UP for LONG
            if new_sl > signal.sl_price:
                signal.sl_price = new_sl
                signal.sl_type = new_type

            # Hard cap enforcement
            hard_floor = signal.entry_price * (1 - HARD_SL_PCT)
            signal.sl_price = max(signal.sl_price, hard_floor)

        elif signal.direction == "SHORT":
            move_pct = (signal.entry_price - signal.current_price) / signal.entry_price * 100
            new_sl = signal.sl_price
            new_type = signal.sl_type

            if move_pct >= 5.0:
                candidate_sl = signal.entry_price * 0.97
                if candidate_sl < new_sl:
                    new_sl = candidate_sl
                    new_type = "TRAIL_3"
            elif move_pct >= 3.0:
                candidate_sl = signal.entry_price * 0.985
                if candidate_sl < new_sl:
                    new_sl = candidate_sl
                    new_type = "TRAIL_1.5"
            elif move_pct >= 2.0:
                candidate_sl = signal.entry_price * 0.99
                if candidate_sl < new_sl:
                    new_sl = candidate_sl
                    new_type = "TRAIL_1"
            elif move_pct >= 1.0:
                candidate_sl = signal.entry_price
                if candidate_sl < new_sl:
                    new_sl = candidate_sl
                    new_type = "BREAKEVEN"

            # SL only moves DOWN for SHORT
            if new_sl < signal.sl_price:
                signal.sl_price = new_sl
                signal.sl_type = new_type

            # Hard cap enforcement
            hard_ceiling = signal.entry_price * (1 + HARD_SL_PCT)
            signal.sl_price = min(signal.sl_price, hard_ceiling)

    def _compute_pnl(self, signal: DawnSignal) -> None:
        """Compute P&L for a signal."""
        if signal.entry_price <= 0 or signal.quantity <= 0:
            return
        price = signal.exit_price if signal.status in ("STOPPED", "CLOSED", "TARGET") else signal.current_price
        if signal.direction == "BUY":
            signal.pnl_pct = (price - signal.entry_price) / signal.entry_price * 100
        else:
            signal.pnl_pct = (signal.entry_price - price) / signal.entry_price * 100
        signal.pnl_rupees = signal.pnl_pct / 100 * signal.entry_price * signal.quantity

    # ══════════════════════════════════════════════════════════════════════
    # EOD PHASE
    # ══════════════════════════════════════════════════════════════════════

    def close_all(self, reason: str = "TIME_EXIT", live_client=None) -> None:
        """Close all active positions at current price."""
        for signal in self._signals:
            if signal.status != "ACTIVE":
                continue
            try:
                if live_client:
                    tick = live_client.get_last_tick(signal.symbol)
                    if tick and tick.ltp > 0:
                        signal.current_price = tick.ltp

                signal.exit_price = signal.current_price
                signal.exit_time = datetime.now(IST).strftime("%H:%M:%S")
                signal.exit_reason = reason
                signal.status = "CLOSED"
                self._compute_pnl(signal)
                logger.info(
                    f"[DAWN] {signal.symbol} CLOSED at {signal.exit_price:.2f} "
                    f"PnL={signal.pnl_pct:+.2f}% ({reason})"
                )
            except Exception as e:
                logger.error(f"[DAWN] Close failed for {signal.symbol}: {e}")

        try:
            self._write_csv()
        except Exception as e:
            logger.error(f"[DAWN] Final CSV write failed: {e}")

    def save_daily_report(self) -> str:
        """Write final CSV and metrics JSON. Returns CSV path."""
        csv_path = self._write_csv()
        try:
            metrics = self.compute_metrics()
            metrics_path = os.path.join(CSV_DIR, f"{self._today or datetime.now(IST).date()}_metrics.json")
            os.makedirs(CSV_DIR, exist_ok=True)
            with open(metrics_path, "w") as f:
                json.dump({
                    "date": metrics.date,
                    "total_signals": metrics.total_signals,
                    "win_count": metrics.win_count,
                    "loss_count": metrics.loss_count,
                    "win_rate": metrics.win_rate,
                    "avg_pnl_pct": metrics.avg_pnl_pct,
                    "total_pnl_rupees": metrics.total_pnl_rupees,
                    "max_favorable_avg": metrics.max_favorable_avg,
                    "max_adverse_avg": metrics.max_adverse_avg,
                    "source_breakdown": metrics.source_breakdown,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"[DAWN] Metrics save failed: {e}")
        return csv_path

    def compute_metrics(self) -> DawnDailyMetrics:
        """Compute daily performance metrics."""
        today_str = str(self._today or datetime.now(IST).date())
        m = DawnDailyMetrics(date=today_str)
        m.total_signals = len(self._signals)

        closed = [s for s in self._signals if s.status in ("STOPPED", "CLOSED", "TARGET")]
        m.active_signals = sum(1 for s in self._signals if s.status == "ACTIVE")
        m.stopped_signals = sum(1 for s in self._signals if s.status == "STOPPED")
        m.closed_signals = len(closed)

        if closed:
            m.win_count = sum(1 for s in closed if s.pnl_pct > 0)
            m.loss_count = sum(1 for s in closed if s.pnl_pct <= 0)
            m.win_rate = m.win_count / len(closed) * 100 if closed else 0.0
            m.avg_pnl_pct = sum(s.pnl_pct for s in closed) / len(closed)
            m.total_pnl_rupees = sum(s.pnl_rupees for s in closed)
            m.max_favorable_avg = sum(s.max_favorable for s in closed) / len(closed)
            m.max_adverse_avg = sum(s.max_adverse for s in closed) / len(closed)

        # Source breakdown
        for s in self._signals:
            src = s.source
            m.source_breakdown[src] = m.source_breakdown.get(src, 0) + 1

        self._daily_metrics = m
        return m

    def reset_daily(self) -> None:
        """Reset state at start of new trading day."""
        self._candidates = []
        self._signals = []
        self._daily_metrics = None
        self._is_active = False
        self._today = None
        logger.info("[DAWN] Daily reset complete")

    # ══════════════════════════════════════════════════════════════════════
    # EMAIL
    # ══════════════════════════════════════════════════════════════════════

    def _generate_email(self, signals: List[DawnSignal], date_str: str,
                        brief_available: bool = True) -> None:
        """Generate and send DAWN pre-market email."""
        # Build regime label
        regime_label = "unknown"
        try:
            if os.path.exists("data/daily_regime.json"):
                with open("data/daily_regime.json") as f:
                    rd = json.load(f)
                regime_label = f"{rd.get('trend', 'unknown')} ({rd.get('strength', 0):+.2f})"
        except Exception:
            pass

        lines = [
            f"# DAWN Pre-Market Signals — {date_str}",
            "",
            f"**Mode: DRY-RUN** | Signals: {len(signals)} | Regime: {regime_label}",
            "",
        ]

        if not brief_available:
            lines.append("⚠ Morning brief unavailable. Operating on independent scan only.")
            lines.append("")

        # Signal table
        lines.append("## Qualified Signals")
        lines.append("")
        lines.append("| # | Symbol | Dir | Catalyst | Score | SL% | Source |")
        lines.append("|---|--------|-----|----------|-------|-----|--------|")
        for i, sig in enumerate(signals, 1):
            sl_pct = "1.5%" if "TIGHT" in sig.sl_type else "2.0%"
            cat_short = sig.catalyst[:40] + "..." if len(sig.catalyst) > 40 else sig.catalyst
            lines.append(f"| {i} | {sig.symbol} | {sig.direction} | {cat_short} | {sig.dawn_score:.0f} | {sl_pct} | {sig.source} |")

        lines.append("")

        # Signal details
        lines.append("### Signal Details")
        lines.append("")
        for i, sig in enumerate(signals, 1):
            lines.append(f"#### {i}. {sig.symbol} — {sig.catalyst}")
            lines.append(f"- **Direction:** {'LONG' if sig.direction == 'BUY' else 'SHORT'}")
            lines.append(f"- **Source:** {sig.source}")
            lines.append(f"- **DAWN Score:** {sig.dawn_score:.0f}/100 "
                         f"(C={sig.score_catalyst:.0f} F={sig.score_freshness:.0f} "
                         f"L={sig.score_liquidity:.0f} T={sig.score_technical:.0f} "
                         f"M={sig.score_context:.0f})")
            lines.append(f"- **Entry:** Market open (09:15 IST)")
            sl_pct_val = "1.5%" if "TIGHT" in sig.sl_type else "2%"
            lines.append(f"- **Stop Loss:** {sl_pct_val} hard cap")
            if sig.liquidity_warning:
                lines.append(f"- **⚠ Liquidity Warning:** Below minimum turnover threshold")
            if sig.notes:
                lines.append(f"- **Notes:** {sig.notes.strip()}")
            lines.append("")

        lines.append("---")
        lines.append("*Generated by DAWN v1.0 (dry-run mode)*")

        body_md = "\n".join(lines)

        try:
            from src.reports.email_sender import send_report_email
            subject = f"DAWN Pre-Market Signals — {date_str}"
            email_ok = send_report_email(subject=subject, body_md=body_md)
            if email_ok:
                logger.info(f"[DAWN] Email sent: {len(signals)} signals | subject: {subject}")
            else:
                logger.warning(f"[DAWN] Email send returned False | {len(signals)} signals")
        except Exception as e:
            logger.error(f"[DAWN] Email FAILED: {e} — will retry once")
            try:
                email_ok = send_report_email(subject=subject, body_md=body_md)
                if email_ok:
                    logger.info(f"[DAWN] Email retry succeeded: {len(signals)} signals")
            except Exception as retry_e:
                logger.error(f"[DAWN] Email retry also FAILED: {retry_e}")

    def _send_no_signals_email(self, date_str: str) -> None:
        """Send a notification email when DAWN finds no qualifying signals."""
        body_md = "\n".join([
            f"# DAWN Pre-Market Signals — {date_str}",
            "",
            "**Mode: DRY-RUN** | Signals: 0",
            "",
            "No qualifying pre-market catalyst signals found today.",
            "",
            "The system will continue to monitor for intraday opportunities "
            "via HYDRA and VIPER strategies.",
            "",
            "---",
            "*Generated by DAWN v1.0 (dry-run mode)*",
        ])

        try:
            from src.reports.email_sender import send_report_email
            subject = f"DAWN — No Qualifying Signals — {date_str}"
            email_ok = send_report_email(subject=subject, body_md=body_md)
            if email_ok:
                logger.info(f"[DAWN] No-signals email sent: {subject}")
            else:
                logger.warning(f"[DAWN] No-signals email send returned False")
        except Exception as e:
            logger.error(f"[DAWN] No-signals email send failed: {e}")

    # ══════════════════════════════════════════════════════════════════════
    # CSV
    # ══════════════════════════════════════════════════════════════════════

    def _write_csv(self) -> str:
        """Write/overwrite today's CSV with current signal states."""
        today_str = str(self._today or datetime.now(IST).date())
        os.makedirs(CSV_DIR, exist_ok=True)
        csv_path = os.path.join(CSV_DIR, f"{today_str}.csv")

        try:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()

                for sig in self._signals:
                    writer.writerow({
                        "date": today_str,
                        "symbol": sig.symbol,
                        "direction": sig.direction,
                        "catalyst": sig.catalyst,
                        "catalyst_strength": sig.catalyst_type,
                        "dawn_score": f"{sig.dawn_score:.0f}",
                        "entry_price": f"{sig.entry_price:.2f}" if sig.entry_price else "",
                        "current_price": f"{sig.current_price:.2f}" if sig.current_price else "",
                        "sl_price": f"{sig.sl_price:.2f}" if sig.sl_price else "",
                        "sl_type": sig.sl_type,
                        "pnl_pct": f"{sig.pnl_pct:+.2f}" if sig.entry_price else "",
                        "pnl_rupees": f"{sig.pnl_rupees:+.2f}" if sig.entry_price else "",
                        "status": sig.status,
                        "exit_price": f"{sig.exit_price:.2f}" if sig.exit_price else "",
                        "exit_time": sig.exit_time or "",
                        "exit_reason": sig.exit_reason,
                        "max_favorable": f"{sig.max_favorable:+.2f}" if sig.entry_price else "",
                        "max_adverse": f"{sig.max_adverse:+.2f}" if sig.entry_price else "",
                        "liquidity_warning": str(sig.liquidity_warning),
                        "notes": sig.notes.strip(),
                    })

                # Summary row
                closed = [s for s in self._signals if s.status in ("STOPPED", "CLOSED", "TARGET")]
                if closed:
                    wins = sum(1 for s in closed if s.pnl_pct > 0)
                    total_pnl = sum(s.pnl_rupees for s in closed)
                    avg_pnl = sum(s.pnl_pct for s in closed) / len(closed)
                    avg_fav = sum(s.max_favorable for s in closed) / len(closed)
                    avg_adv = sum(s.max_adverse for s in closed) / len(closed)
                    writer.writerow({
                        "date": "SUMMARY",
                        "symbol": "",
                        "direction": "",
                        "catalyst": "",
                        "catalyst_strength": "",
                        "dawn_score": "",
                        "entry_price": "",
                        "current_price": "",
                        "sl_price": "",
                        "sl_type": "",
                        "pnl_pct": f"{avg_pnl:+.2f}",
                        "pnl_rupees": f"{total_pnl:+.2f}",
                        "status": f"{wins}/{len(closed)} wins",
                        "exit_price": "",
                        "exit_time": "",
                        "exit_reason": "",
                        "max_favorable": f"{avg_fav:+.2f}",
                        "max_adverse": f"{avg_adv:+.2f}",
                        "liquidity_warning": "",
                        "notes": "",
                    })

        except Exception as e:
            logger.error(f"[DAWN] CSV write error: {e}")

        return csv_path
