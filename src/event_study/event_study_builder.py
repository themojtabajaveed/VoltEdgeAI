import sqlite3
import json
import logging
import time
from datetime import datetime, date, timedelta, timezone, time as datetime_time
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import pandas as pd

# pandas_ta requires Python >=3.12; using ta==0.11.0 for RSI/ATR (Python 3.11 compat)
try:
    import ta as _ta_lib
    _TA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ta_lib = None  # type: ignore
    _TA_AVAILABLE = False

from src.data_ingestion.market_history import get_ohlcv
from src.utils.market_calendar import is_market_day
from src.trading.sector_guard import get_sector

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_IST_ZI = ZoneInfo("Asia/Kolkata")

# Nifty 50 symbol for get_ohlcv historical data calls
_NIFTY50_SYMBOL = "NIFTY 50"

# Sector name (from sector_guard.SECTOR_MAP) → Nifty sector index symbol
_SECTOR_TO_NIFTY_INDEX: Dict[str, str] = {
    "IT":       "NIFTY IT",
    "BANKING":  "NIFTY BANK",
    "PHARMA":   "NIFTY PHARMA",
    "AUTO":     "NIFTY AUTO",
    "ENERGY":   "NIFTY ENERGY",
    "METALS":   "NIFTY METAL",
    "FMCG":     "NIFTY FMCG CONSUMPTION",
    "INFRA":    "NIFTY INFRA",
    "NBFC":     "NIFTY FIN SERVICE",
    "CONSUMER": "NIFTY CONSUMER DURABLES",
}


def _ensure_ist(dt: Any) -> Any:
    """Return dt as a tz-aware IST datetime regardless of input type.

    Handles plain datetime (naive/aware) and pandas Timestamp (naive/aware).
    Never raises on valid datetime-like input.
    """
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(_IST_ZI)
    # pandas Timestamp that is tz-naive — use tz_localize
    if hasattr(dt, "tz_localize"):
        return dt.tz_localize("Asia/Kolkata")
    # plain datetime that is tz-naive
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=_IST_ZI)
    return dt


def _localize_series(series: pd.Series) -> pd.Series:
    """Localize a datetime Series to IST — safe for both tz-naive and tz-aware."""
    if series.dt.tz is None:
        return series.dt.tz_localize("Asia/Kolkata")
    return series.dt.tz_convert("Asia/Kolkata")


class EventStudyBuilder:
    def __init__(self, db_path: str = "data/history.db", kite_client: Optional[Any] = None):
        self.db_path = db_path
        self.kite_client = kite_client
        self._token_map: dict = {}
        try:
            from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
            _df = load_instruments_csv()
            self._token_map = build_symbol_token_map(_df)
            logger.info("[EventStudyBuilder] Loaded %d instrument tokens.", len(self._token_map))
        except Exception as e:
            logger.warning(
                "[EventStudyBuilder] Could not load instrument token map: %s"
                " — all tokens will be 0.", e
            )
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _get_token(self, symbol: str) -> int:
        """Return Kite instrument_token for symbol, or 0 if not found."""
        return int(self._token_map.get(symbol, 0))

    def _ensure_schema(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS event_study (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filing_id       INTEGER NOT NULL,
                symbol          TEXT NOT NULL,
                filed_at        TEXT NOT NULL,
                event_type      TEXT,
                quality_score   REAL,
                ret_t0_day      REAL,
                ret_t1_day      REAL,
                ret_t3_day      REAL,
                ret_t5_day      REAL,
                price_t_minus_1 REAL,
                price_t0_close  REAL,
                price_t1_close  REAL,
                price_t3_close  REAL,
                price_t5_close  REAL,
                daily_bars_json TEXT,
                intraday_bars_json  TEXT,
                ret_t0_15min        REAL,
                ret_t0_30min        REAL,
                ret_t0_1h           REAL,
                rsi_pre             REAL,
                vwap_delta          REAL,
                vol_spike_ratio     REAL,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(filing_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_es_symbol ON event_study(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_es_filed_at ON event_study(filed_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_es_event_type ON event_study(event_type)")

        # Step 3: intraday anchor + dual-anchor return columns
        for col_name, col_type in [
            ("intraday_anchor_open",  "REAL"),
            ("intraday_anchor_vwap5", "REAL"),
            ("ret_t0_15min_open",     "REAL"),
            ("ret_t0_15min_vwap",     "REAL"),
            ("ret_t0_30min_open",     "REAL"),
            ("ret_t0_30min_vwap",     "REAL"),
            ("ret_t0_1h_open",        "REAL"),
            ("ret_t0_1h_vwap",        "REAL"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE event_study ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_es_ret_1h_open ON event_study(ret_t0_1h_open)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_es_ret_1h_vwap ON event_study(ret_t0_1h_vwap)"
        )

        # Step 4: TA indicator columns
        for col in [
            "rsi_7_pre REAL",
            "rsi_14_pre REAL",
            "vol_spike_5d REAL",
            "vol_spike_20d REAL",
            "vwap_delta_t0 REAL",
            "atr_rel_range REAL",
            "gap_pct REAL",
            "sector_rel_return REAL",
            "market_rel_return REAL",
            "sector_alpha REAL",
        ]:
            try:
                cursor.execute(f"ALTER TABLE event_study ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already exists

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_es_rsi14     ON event_study(rsi_14_pre)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_es_gap_pct   ON event_study(gap_pct)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_es_vol_spike ON event_study(vol_spike_20d)")
        self.conn.commit()

    # ── Core helpers ──────────────────────────────────────────────────────────

    def build_all(self) -> int:
        # Pre-fetch Nifty 50 daily data once for the whole session
        today = date.today()
        nifty_start = datetime.combine(
            today - timedelta(days=59), datetime_time(9, 0)
        ).replace(tzinfo=IST)
        nifty_end = datetime.combine(today, datetime_time(16, 0)).replace(tzinfo=IST)
        try:
            nifty_cache: pd.DataFrame = get_ohlcv(
                _NIFTY50_SYMBOL, self._get_token(_NIFTY50_SYMBOL), "day", nifty_start, nifty_end,
                kite_client=self.kite_client,
            )
            time.sleep(0.5)
        except Exception as e:
            logger.warning("[EventStudyBuilder] Nifty 50 pre-fetch failed: %s", e)
            nifty_cache = pd.DataFrame()

        sector_cache: Dict[str, pd.DataFrame] = {}

        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT id, symbol, filed_at, event_type, quality_score "
                "FROM filings_archive WHERE processed = 0"
            )
        except sqlite3.OperationalError:
            return 0

        rows = cursor.fetchall()
        inserted_count = 0
        for row in rows:
            filing_dict = dict(row)
            if self._build_one(
                filing_dict, nifty_cache=nifty_cache, sector_cache=sector_cache
            ):
                inserted_count += 1
        return inserted_count

    def _get_nth_trading_day(self, base_date: date, n: int) -> date:
        current_date = base_date
        if n == 0:
            while not is_market_day(current_date):
                current_date += timedelta(days=1)
            return current_date
        step = 1 if n > 0 else -1
        days_to_move = abs(n)
        while days_to_move > 0:
            current_date += timedelta(days=step)
            if is_market_day(current_date):
                days_to_move -= 1
        return current_date

    def _calc_return(
        self, base: Optional[float], target: Optional[float]
    ) -> Optional[float]:
        if base is None or target is None:
            return None
        if base == 0.0:
            return None
        return round((target - base) / base * 100, 4)

    def _calc_vwap5(
        self, df: pd.DataFrame, window_start: datetime
    ) -> Optional[float]:
        vwap_cutoff = window_start + timedelta(minutes=5)
        ts = df["timestamp"]
        if ts.dt.tz is not None:
            ts_cmp = ts.dt.tz_localize(None)
        else:
            ts_cmp = ts
        cutoff_cmp = (
            vwap_cutoff.replace(tzinfo=None) if vwap_cutoff.tzinfo else vwap_cutoff
        )
        vwap_bars = df[ts_cmp <= cutoff_cmp]
        if vwap_bars.empty:
            return float(df.iloc[0]["open"]) if not df.empty else None
        total_vol = vwap_bars["volume"].sum()
        if total_vol == 0:
            return float(df.iloc[0]["open"]) if not df.empty else None
        vwap = (vwap_bars["close"] * vwap_bars["volume"]).sum() / total_vol
        return round(float(vwap), 4)

    # ── Step 4: TA indicator methods ──────────────────────────────────────────

    def _compute_vwap_from_bars(self, bars_json: Optional[str]) -> Optional[float]:
        """VWAP from intraday_bars_json (close × volume weighted average)."""
        if not bars_json:
            return None
        try:
            bars = json.loads(bars_json)
        except (json.JSONDecodeError, TypeError):
            return None
        if not bars:
            return None
        total_vol = sum(b.get("volume", 0) for b in bars)
        if total_vol == 0:
            return None
        vwap = sum(b["close"] * b["volume"] for b in bars) / total_vol
        return round(float(vwap), 4)

    def _get_sector_symbol(self, symbol: str, event_type: str = "") -> Optional[str]:
        """Map stock symbol → Nifty sector index symbol via sector_guard.get_sector()."""
        try:
            sector = get_sector(symbol, event_type)
            return _SECTOR_TO_NIFTY_INDEX.get(sector)
        except Exception as e:
            logger.warning(
                "[EventStudyBuilder] Sector mapping failed for %s: %s", symbol, e
            )
            return None

    def _compute_ta_indicators(
        self,
        symbol: str,
        t0_date: date,
        daily_df: pd.DataFrame,
        intraday_bars_json: Optional[str],
        sector_df: pd.DataFrame,
        nifty_df: pd.DataFrame,
    ) -> dict:
        """Compute all 10 TA indicators. Each indicator is isolated — one failure
        sets that indicator to None without aborting the rest."""
        EMPTY_TA: dict = {
            "rsi_7_pre": None, "rsi_14_pre": None,
            "vol_spike_5d": None, "vol_spike_20d": None,
            "vwap_delta_t0": None, "atr_rel_range": None,
            "gap_pct": None, "sector_rel_return": None,
            "market_rel_return": None, "sector_alpha": None,
        }

        if daily_df is None or daily_df.empty:
            return EMPTY_TA

        result = dict(EMPTY_TA)

        # Normalise and sort daily_df ascending by date
        daily_df = daily_df.sort_index()
        idx_as_dates = [
            ts.date() if hasattr(ts, "date") else pd.Timestamp(ts).date()
            for ts in pd.to_datetime(daily_df.index)
        ]

        # Locate T0 row
        t0_locs = [i for i, d in enumerate(idx_as_dates) if d == t0_date]
        if not t0_locs:
            return EMPTY_TA
        t0_iloc = t0_locs[-1]

        # T-1 is the row immediately before T0 in the sorted daily_df
        t1_iloc = t0_iloc - 1
        if t1_iloc < 0:
            return EMPTY_TA

        t1_date_actual = idx_as_dates[t1_iloc]
        pre_t0_df = daily_df.iloc[:t0_iloc]  # all rows strictly before T0

        # 1. RSI ──────────────────────────────────────────────────────────────
        try:
            if not _TA_AVAILABLE or _ta_lib is None:
                raise ImportError("ta library not available")
            close_series = daily_df["close"].astype(float)
            rsi_7_series  = _ta_lib.momentum.RSIIndicator(close_series, window=7).rsi()
            rsi_14_series = _ta_lib.momentum.RSIIndicator(close_series, window=14).rsi()
            v7  = rsi_7_series.iloc[t1_iloc]
            v14 = rsi_14_series.iloc[t1_iloc]
            result["rsi_7_pre"]  = None if pd.isna(v7)  else round(float(v7),  4)
            result["rsi_14_pre"] = None if pd.isna(v14) else round(float(v14), 4)
        except Exception as e:
            logger.warning("[EventStudyBuilder] RSI failed for %s: %s", symbol, e)

        # 2. Volume spike ──────────────────────────────────────────────────────
        try:
            t0_volume    = float(daily_df.iloc[t0_iloc]["volume"])
            vol_5d_mean  = pre_t0_df["volume"].astype(float).tail(5).mean()
            vol_20d_mean = pre_t0_df["volume"].astype(float).tail(20).mean()
            result["vol_spike_5d"] = (
                round(t0_volume / vol_5d_mean,  4)
                if vol_5d_mean > 0 and not pd.isna(vol_5d_mean) else None
            )
            result["vol_spike_20d"] = (
                round(t0_volume / vol_20d_mean, 4)
                if vol_20d_mean > 0 and not pd.isna(vol_20d_mean) else None
            )
        except Exception as e:
            logger.warning("[EventStudyBuilder] Vol spike failed for %s: %s", symbol, e)

        # 3. VWAP delta ────────────────────────────────────────────────────────
        try:
            true_vwap = self._compute_vwap_from_bars(intraday_bars_json)
            t0_row = daily_df.iloc[t0_iloc]
            if true_vwap is None:
                # Fallback: typical price (HLC/3) from T0 daily bar
                true_vwap = float(
                    (float(t0_row["high"]) + float(t0_row["low"]) + float(t0_row["close"])) / 3
                )
            t0_close = float(t0_row["close"])
            if true_vwap > 0:
                result["vwap_delta_t0"] = round(
                    (t0_close - true_vwap) / true_vwap * 100, 4
                )
        except Exception as e:
            logger.warning("[EventStudyBuilder] VWAP delta failed for %s: %s", symbol, e)

        # 4. ATR relative range ────────────────────────────────────────────────
        try:
            if not _TA_AVAILABLE or _ta_lib is None:
                raise ImportError("ta library not available")
            atr_series = _ta_lib.volatility.AverageTrueRange(
                daily_df["high"].astype(float),
                daily_df["low"].astype(float),
                daily_df["close"].astype(float),
                window=14,
            ).average_true_range()
            atr_t1 = atr_series.iloc[t1_iloc]
            if not pd.isna(atr_t1) and float(atr_t1) > 0:
                t0_row = daily_df.iloc[t0_iloc]
                result["atr_rel_range"] = round(
                    (float(t0_row["high"]) - float(t0_row["low"])) / float(atr_t1), 4
                )
        except Exception as e:
            logger.warning("[EventStudyBuilder] ATR failed for %s: %s", symbol, e)

        # 5. Gap percent ───────────────────────────────────────────────────────
        try:
            t0_open  = float(daily_df.iloc[t0_iloc]["open"])
            t1_close = float(daily_df.iloc[t1_iloc]["close"])
            if t1_close > 0:
                result["gap_pct"] = round(
                    (t0_open - t1_close) / t1_close * 100, 4
                )
        except Exception as e:
            logger.warning("[EventStudyBuilder] Gap pct failed for %s: %s", symbol, e)

        # 6. Benchmark relative returns ────────────────────────────────────────
        try:
            t0_close_s = float(daily_df.iloc[t0_iloc]["close"])
            t1_close_s = float(daily_df.iloc[t1_iloc]["close"])
            t0_stock_return: Optional[float] = (
                (t0_close_s - t1_close_s) / t1_close_s * 100
                if t1_close_s > 0 else None
            )

            def _bench_return(bdf: pd.DataFrame) -> Optional[float]:
                if bdf is None or bdf.empty:
                    return None
                bdf_s = bdf.sort_index()
                b_dates = [
                    ts.date() if hasattr(ts, "date") else pd.Timestamp(ts).date()
                    for ts in pd.to_datetime(bdf_s.index)
                ]
                b_t0 = [i for i, d in enumerate(b_dates) if d == t0_date]
                b_t1 = [i for i, d in enumerate(b_dates) if d == t1_date_actual]
                if not b_t0 or not b_t1:
                    return None
                b_t0_c = float(bdf_s.iloc[b_t0[-1]]["close"])
                b_t1_c = float(bdf_s.iloc[b_t1[-1]]["close"])
                return (b_t0_c - b_t1_c) / b_t1_c * 100 if b_t1_c > 0 else None

            t0_sector_return = _bench_return(sector_df)
            t0_nifty_return  = _bench_return(nifty_df)

            if t0_stock_return is not None and t0_sector_return is not None:
                result["sector_rel_return"] = round(
                    t0_stock_return - t0_sector_return, 4
                )
            if t0_stock_return is not None and t0_nifty_return is not None:
                result["market_rel_return"] = round(
                    t0_stock_return - t0_nifty_return, 4
                )
            if t0_sector_return is not None and t0_nifty_return is not None:
                result["sector_alpha"] = round(
                    t0_sector_return - t0_nifty_return, 4
                )
        except Exception as e:
            logger.warning(
                "[EventStudyBuilder] Benchmark returns failed for %s: %s", symbol, e
            )

        return result

    def build_ta_only(self) -> int:
        """Backfill TA indicators for event_study rows where rsi_14_pre IS NULL."""
        today = date.today()
        nifty_start = datetime.combine(
            today - timedelta(days=59), datetime_time(9, 0)
        ).replace(tzinfo=IST)
        nifty_end = datetime.combine(today, datetime_time(16, 0)).replace(tzinfo=IST)
        try:
            nifty_cache: pd.DataFrame = get_ohlcv(
                _NIFTY50_SYMBOL, self._get_token(_NIFTY50_SYMBOL), "day", nifty_start, nifty_end,
                kite_client=self.kite_client,
            )
            time.sleep(0.5)
        except Exception as e:
            logger.warning(
                "[EventStudyBuilder] Nifty 50 pre-fetch failed in build_ta_only: %s", e
            )
            nifty_cache = pd.DataFrame()

        sector_cache: Dict[str, pd.DataFrame] = {}

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT es.id, es.filing_id, es.filed_at, es.event_type,
                   es.intraday_bars_json, fa.symbol
            FROM event_study es
            JOIN filings_archive fa ON fa.id = es.filing_id
            WHERE es.rsi_14_pre IS NULL
        """)
        rows = cursor.fetchall()
        count = 0

        for row in rows:
            row_dict = dict(row)
            try:
                filed_at_str = row_dict["filed_at"]
                if filed_at_str.endswith("Z"):
                    filing_dt: datetime = datetime.fromisoformat(
                        filed_at_str[:-1]
                    ).replace(tzinfo=timezone.utc)
                else:
                    filing_dt = datetime.fromisoformat(filed_at_str)
                    if filing_dt.tzinfo is None:
                        filing_dt = filing_dt.replace(tzinfo=IST)

                filing_ist  = filing_dt.astimezone(_IST_ZI)
                filing_time = filing_ist.time()
                filing_date = filing_ist.date()

                if filing_time < datetime_time(15, 30) and is_market_day(filing_date):
                    t0_date = filing_date
                else:
                    t0_date = filing_date + timedelta(days=1)
                    for _ in range(7):
                        if is_market_day(t0_date):
                            break
                        t0_date += timedelta(days=1)

                symbol     = row_dict["symbol"]
                event_type = row_dict.get("event_type") or ""

                daily_start = datetime.combine(
                    t0_date - timedelta(days=59), datetime_time(9, 0)
                ).replace(tzinfo=IST)
                daily_end = datetime.combine(
                    t0_date, datetime_time(16, 0)
                ).replace(tzinfo=IST)
                daily_df = get_ohlcv(symbol, self._get_token(symbol), "day", daily_start, daily_end, kite_client=self.kite_client)
                time.sleep(0.5)

                sector_symbol = self._get_sector_symbol(symbol, event_type)
                sector_df = pd.DataFrame()
                if sector_symbol:
                    if sector_symbol not in sector_cache:
                        try:
                            sector_cache[sector_symbol] = get_ohlcv(
                                sector_symbol, self._get_token(sector_symbol), "day", daily_start, daily_end,
                                kite_client=self.kite_client,
                            )
                            time.sleep(0.5)
                        except Exception as se:
                            logger.warning(
                                "[EventStudyBuilder] Sector fetch failed %s: %s",
                                sector_symbol, se,
                            )
                            sector_cache[sector_symbol] = pd.DataFrame()
                    sector_df = sector_cache[sector_symbol]

                ta_data = self._compute_ta_indicators(
                    symbol, t0_date, daily_df,
                    row_dict.get("intraday_bars_json"),
                    sector_df, nifty_cache,
                )
                self.conn.execute("""
                    UPDATE event_study SET
                        rsi_7_pre=?,        rsi_14_pre=?,
                        vol_spike_5d=?,     vol_spike_20d=?,
                        vwap_delta_t0=?,    atr_rel_range=?,
                        gap_pct=?,          sector_rel_return=?,
                        market_rel_return=?, sector_alpha=?
                    WHERE id=?
                """, (
                    ta_data["rsi_7_pre"],        ta_data["rsi_14_pre"],
                    ta_data["vol_spike_5d"],      ta_data["vol_spike_20d"],
                    ta_data["vwap_delta_t0"],     ta_data["atr_rel_range"],
                    ta_data["gap_pct"],           ta_data["sector_rel_return"],
                    ta_data["market_rel_return"], ta_data["sector_alpha"],
                    row_dict["id"],
                ))
                self.conn.commit()
                count += 1
            except Exception as e:
                logger.error(
                    "[EventStudyBuilder] build_ta_only failed id=%s: %s",
                    row_dict.get("id"), e,
                )

        self.conn.commit()
        return count

    # ── Step 3: intraday window ───────────────────────────────────────────────

    def _build_intraday_window(
        self, symbol: str, filing_dt: datetime, t0_date: date
    ) -> dict:
        MARKET_OPEN  = datetime_time(9, 15)
        MARKET_CLOSE = datetime_time(15, 30)
        EMPTY_RESULT: dict = {
            "intraday_bars_json":    "[]",
            "intraday_anchor_open":  None,
            "intraday_anchor_vwap5": None,
            "ret_t0_15min_open":     None,
            "ret_t0_15min_vwap":     None,
            "ret_t0_30min_open":     None,
            "ret_t0_30min_vwap":     None,
            "ret_t0_1h_open":        None,
            "ret_t0_1h_vwap":        None,
        }

        filing_ist  = filing_dt.astimezone(_IST_ZI)
        filing_time = filing_ist.time()

        if filing_time < MARKET_OPEN:
            ws = datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI)
            we = datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI)
            reaction_windows: List[tuple] = [(t0_date, ws, we)]

        elif filing_time <= MARKET_CLOSE:
            ws = max(
                filing_ist - timedelta(hours=2),
                datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI),
            )
            we = min(
                filing_ist + timedelta(hours=4),
                datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI),
            )
            if we <= ws:
                ws = datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI)
                we = datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI)
            reaction_windows = [(t0_date, ws, we)]

        else:
            t_next = self._get_nth_trading_day(t0_date, 1)
            reaction_windows = [
                (
                    t0_date,
                    datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI),
                    datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI),
                ),
                (
                    t_next,
                    datetime(t_next.year, t_next.month, t_next.day, 9, 15, tzinfo=_IST_ZI),
                    datetime(t_next.year, t_next.month, t_next.day, 15, 30, tzinfo=_IST_ZI),
                ),
            ]

        all_dfs: List[pd.DataFrame] = []
        first_window_start = reaction_windows[0][1]
        for (rdate, ws, we) in reaction_windows:
            try:
                raw_df = get_ohlcv(symbol, self._get_token(symbol), "15minute", ws, we, kite_client=self.kite_client)
                time.sleep(0.5)
                if not raw_df.empty:
                    raw_df.index.name = raw_df.index.name or "timestamp"
                    df_reset = raw_df.reset_index()
                    if "index" in df_reset.columns and "timestamp" not in df_reset.columns:
                        df_reset = df_reset.rename(columns={"index": "timestamp"})
                    all_dfs.append(df_reset)
            except Exception as e:
                logger.warning(
                    "[EventStudyBuilder] intraday fetch failed %s %s: %s",
                    symbol, rdate, e,
                )

        if not all_dfs:
            return EMPTY_RESULT

        combined = pd.concat(all_dfs).sort_values("timestamp").reset_index(drop=True)

        if len(combined) < 2:
            return EMPTY_RESULT

        combined["timestamp"] = _localize_series(combined["timestamp"])

        first_bar    = combined.iloc[0]
        anchor_open  = float(first_bar["open"])
        anchor_vwap5 = self._calc_vwap5(all_dfs[0], first_window_start)

        first_bar_ts = _ensure_ist(first_bar["timestamp"])

        def _get_return(anchor: Optional[float], offset_min: int) -> Optional[float]:
            if anchor is None:
                return None
            target_ts = first_bar_ts + timedelta(minutes=offset_min)
            deltas = (combined["timestamp"] - target_ts).abs()
            idx = deltas.idxmin()
            if deltas[idx].total_seconds() > 1800:
                return None
            return self._calc_return(anchor, float(combined.loc[idx, "close"]))

        bars_json = json.dumps([
            {
                "timestamp": str(row["timestamp"].isoformat()),
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": int(row["volume"]),
            }
            for _, row in combined.iterrows()
        ])

        return {
            "intraday_bars_json":    bars_json,
            "intraday_anchor_open":  anchor_open,
            "intraday_anchor_vwap5": anchor_vwap5,
            "ret_t0_15min_open":     _get_return(anchor_open,  15),
            "ret_t0_15min_vwap":     _get_return(anchor_vwap5, 15),
            "ret_t0_30min_open":     _get_return(anchor_open,  30),
            "ret_t0_30min_vwap":     _get_return(anchor_vwap5, 30),
            "ret_t0_1h_open":        _get_return(anchor_open,  60),
            "ret_t0_1h_vwap":        _get_return(anchor_vwap5, 60),
        }

    def build_intraday_only(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT es.id, es.filing_id, fa.symbol, fa.filed_at
            FROM event_study es
            JOIN filings_archive fa ON fa.id = es.filing_id
            WHERE es.intraday_bars_json IS NULL OR es.intraday_bars_json = '[]'
        """)
        rows = cursor.fetchall()
        count = 0
        for row in rows:
            row_dict = dict(row)
            try:
                filed_at_str = row_dict["filed_at"]
                if filed_at_str.endswith("Z"):
                    filing_dt: datetime = (
                        datetime.fromisoformat(filed_at_str[:-1]).replace(tzinfo=timezone.utc)
                    )
                else:
                    filing_dt = datetime.fromisoformat(filed_at_str)
                    if filing_dt.tzinfo is None:
                        filing_dt = filing_dt.replace(tzinfo=IST)

                filing_ist  = filing_dt.astimezone(_IST_ZI)
                filing_time = filing_ist.time()
                filing_date = filing_ist.date()

                if filing_time < datetime_time(15, 30) and is_market_day(filing_date):
                    t0_date = filing_date
                else:
                    t0_date = filing_date + timedelta(days=1)
                    for _ in range(7):
                        if is_market_day(t0_date):
                            break
                        t0_date += timedelta(days=1)

                intraday = self._build_intraday_window(row_dict["symbol"], filing_dt, t0_date)
                self.conn.execute("""
                    UPDATE event_study SET
                        intraday_bars_json=?,
                        intraday_anchor_open=?,  intraday_anchor_vwap5=?,
                        ret_t0_15min_open=?,     ret_t0_15min_vwap=?,
                        ret_t0_30min_open=?,     ret_t0_30min_vwap=?,
                        ret_t0_1h_open=?,        ret_t0_1h_vwap=?
                    WHERE id=?
                """, (
                    intraday["intraday_bars_json"],
                    intraday["intraday_anchor_open"],  intraday["intraday_anchor_vwap5"],
                    intraday["ret_t0_15min_open"],     intraday["ret_t0_15min_vwap"],
                    intraday["ret_t0_30min_open"],     intraday["ret_t0_30min_vwap"],
                    intraday["ret_t0_1h_open"],        intraday["ret_t0_1h_vwap"],
                    row_dict["id"],
                ))
                count += 1
            except Exception as e:
                logger.error(
                    "[EventStudyBuilder] build_intraday_only failed for id=%s: %s",
                    row_dict.get("id"), e,
                )

        self.conn.commit()
        return count

    def _build_one(
        self,
        filing_row: Dict[str, Any],
        nifty_cache: Optional[pd.DataFrame] = None,
        sector_cache: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> bool:
        if sector_cache is None:
            sector_cache = {}
        if nifty_cache is None:
            nifty_cache = pd.DataFrame()

        try:
            filed_at_str = filing_row["filed_at"]
            if filed_at_str.endswith("Z"):
                filing_dt = (
                    datetime.fromisoformat(filed_at_str[:-1])
                    .replace(tzinfo=timezone.utc)
                    .astimezone(IST)
                )
            else:
                filing_dt = datetime.fromisoformat(filed_at_str)
                if filing_dt.tzinfo is None:
                    filing_dt = filing_dt.replace(tzinfo=IST)

            if filing_dt.time() < datetime_time(15, 30) and is_market_day(filing_dt.date()):
                t0_date = filing_dt.date()
            else:
                t0_date = filing_dt.date() + timedelta(days=1)
                for _ in range(7):
                    if is_market_day(t0_date):
                        break
                    t0_date += timedelta(days=1)

            t_minus1 = self._get_nth_trading_day(t0_date, -1)
            t_minus5 = self._get_nth_trading_day(t0_date, -5)
            t_plus1  = self._get_nth_trading_day(t0_date, 1)
            t_plus3  = self._get_nth_trading_day(t0_date, 3)
            t_plus5  = self._get_nth_trading_day(t0_date, 5)

            start_dt = datetime.combine(t_minus5, datetime_time(9, 0)).replace(tzinfo=IST)
            end_dt   = datetime.combine(t_plus5,  datetime_time(16, 0)).replace(tzinfo=IST)

            df = get_ohlcv(
                symbol=filing_row["symbol"],
                instrument_token=self._get_token(filing_row["symbol"]),
                interval="day",
                start=start_dt,
                end=end_dt,
                kite_client=self.kite_client,
            )
            time.sleep(0.5)

            def get_price(target_date: date) -> Optional[float]:
                if df.empty:
                    return None
                if target_date > date.today():
                    return None
                df_dates = pd.to_datetime(df.index).date
                matches = df[df_dates == target_date]
                if not matches.empty:
                    return float(matches.iloc[-1]["close"])
                diffs = [abs((d - target_date).days) for d in df_dates]
                min_diff = min(diffs)
                closest_idx = diffs.index(min_diff)
                return float(df.iloc[closest_idx]["close"])

            price_t_minus_1 = get_price(t_minus1)
            price_t0_close  = get_price(t0_date)
            price_t1_close  = get_price(t_plus1)
            price_t3_close  = get_price(t_plus3)
            price_t5_close  = get_price(t_plus5)

            ret_t0_day = self._calc_return(price_t_minus_1, price_t0_close)
            ret_t1_day = self._calc_return(price_t_minus_1, price_t1_close)
            ret_t3_day = self._calc_return(price_t_minus_1, price_t3_close)
            ret_t5_day = self._calc_return(price_t_minus_1, price_t5_close)

            daily_bars = []
            if not df.empty:
                for idx, row_series in df.iterrows():
                    bar = {
                        "date":   idx.date().isoformat() if hasattr(idx, "date") else str(idx).split(" ")[0],
                        "open":   float(row_series["open"]),
                        "high":   float(row_series["high"]),
                        "low":    float(row_series["low"]),
                        "close":  float(row_series["close"]),
                        "volume": int(row_series["volume"]),
                    }
                    daily_bars.append(bar)
            daily_bars_json = json.dumps(daily_bars)

            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO event_study (
                    filing_id, symbol, filed_at, event_type, quality_score,
                    ret_t0_day, ret_t1_day, ret_t3_day, ret_t5_day,
                    price_t_minus_1, price_t0_close, price_t1_close, price_t3_close,
                    price_t5_close, daily_bars_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                filing_row["id"], filing_row["symbol"], filing_row["filed_at"],
                filing_row.get("event_type"), filing_row.get("quality_score"),
                ret_t0_day, ret_t1_day, ret_t3_day, ret_t5_day,
                price_t_minus_1, price_t0_close, price_t1_close, price_t3_close,
                price_t5_close, daily_bars_json,
            ))
            cursor.execute("UPDATE filings_archive SET processed=1 WHERE id=?", (filing_row["id"],))
            self.conn.commit()

            # Intraday window — idempotent, runs even on duplicate INSERT
            intraday = self._build_intraday_window(filing_row["symbol"], filing_dt, t0_date)
            self.conn.execute("""
                UPDATE event_study SET
                    intraday_bars_json=?,
                    intraday_anchor_open=?,  intraday_anchor_vwap5=?,
                    ret_t0_15min_open=?,     ret_t0_15min_vwap=?,
                    ret_t0_30min_open=?,     ret_t0_30min_vwap=?,
                    ret_t0_1h_open=?,        ret_t0_1h_vwap=?
                WHERE filing_id=?
            """, (
                intraday["intraday_bars_json"],
                intraday["intraday_anchor_open"],  intraday["intraday_anchor_vwap5"],
                intraday["ret_t0_15min_open"],     intraday["ret_t0_15min_vwap"],
                intraday["ret_t0_30min_open"],     intraday["ret_t0_30min_vwap"],
                intraday["ret_t0_1h_open"],        intraday["ret_t0_1h_vwap"],
                filing_row["id"],
            ))
            self.conn.commit()

            # TA indicators ───────────────────────────────────────────────────
            symbol     = filing_row["symbol"]
            event_type = filing_row.get("event_type") or ""

            ta_daily_start = datetime.combine(
                t0_date - timedelta(days=59), datetime_time(9, 0)
            ).replace(tzinfo=IST)
            ta_daily_end = datetime.combine(
                t0_date, datetime_time(16, 0)
            ).replace(tzinfo=IST)
            ta_daily_df = get_ohlcv(symbol, self._get_token(symbol), "day", ta_daily_start, ta_daily_end, kite_client=self.kite_client)
            time.sleep(0.5)

            sector_symbol = self._get_sector_symbol(symbol, event_type)
            sector_df = pd.DataFrame()
            if sector_symbol:
                if sector_symbol not in sector_cache:
                    try:
                        sector_cache[sector_symbol] = get_ohlcv(
                            sector_symbol, self._get_token(sector_symbol), "day", ta_daily_start, ta_daily_end,
                            kite_client=self.kite_client,
                        )
                        time.sleep(0.5)
                    except Exception as se:
                        logger.warning(
                            "[EventStudyBuilder] Sector fetch failed %s: %s",
                            sector_symbol, se,
                        )
                        sector_cache[sector_symbol] = pd.DataFrame()
                sector_df = sector_cache[sector_symbol]

            # Retrieve stored intraday_bars_json for VWAP computation
            cur = self.conn.execute(
                "SELECT intraday_bars_json FROM event_study WHERE filing_id=?",
                (filing_row["id"],),
            )
            irow = cur.fetchone()
            stored_intraday_json = irow[0] if irow else None

            ta_data = self._compute_ta_indicators(
                symbol, t0_date, ta_daily_df,
                stored_intraday_json, sector_df, nifty_cache,
            )
            self.conn.execute("""
                UPDATE event_study SET
                    rsi_7_pre=?,        rsi_14_pre=?,
                    vol_spike_5d=?,     vol_spike_20d=?,
                    vwap_delta_t0=?,    atr_rel_range=?,
                    gap_pct=?,          sector_rel_return=?,
                    market_rel_return=?, sector_alpha=?
                WHERE filing_id=?
            """, (
                ta_data["rsi_7_pre"],        ta_data["rsi_14_pre"],
                ta_data["vol_spike_5d"],      ta_data["vol_spike_20d"],
                ta_data["vwap_delta_t0"],     ta_data["atr_rel_range"],
                ta_data["gap_pct"],           ta_data["sector_rel_return"],
                ta_data["market_rel_return"], ta_data["sector_alpha"],
                filing_row["id"],
            ))
            self.conn.commit()

            return True

        except Exception as e:
            logger.error(
                "Error building event study for %s filing %s: %s",
                filing_row.get("symbol"), filing_row.get("id"), e,
            )
            self.conn.rollback()
            return False
