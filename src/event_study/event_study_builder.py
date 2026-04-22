import sqlite3
import json
import logging
import time
from datetime import datetime, date, timedelta, timezone, time as datetime_time
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import pandas as pd
from src.data_ingestion.market_history import get_ohlcv
from src.utils.market_calendar import is_market_day

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_IST_ZI = ZoneInfo("Asia/Kolkata")


class EventStudyBuilder:
    def __init__(self, db_path: str = "data/history.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

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

        new_columns = [
            ("intraday_anchor_open",  "REAL"),
            ("intraday_anchor_vwap5", "REAL"),
            ("ret_t0_15min_open",     "REAL"),
            ("ret_t0_15min_vwap",     "REAL"),
            ("ret_t0_30min_open",     "REAL"),
            ("ret_t0_30min_vwap",     "REAL"),
            ("ret_t0_1h_open",        "REAL"),
            ("ret_t0_1h_vwap",        "REAL"),
        ]
        for col_name, col_type in new_columns:
            try:
                cursor.execute(f"ALTER TABLE event_study ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_es_ret_1h_open ON event_study(ret_t0_1h_open)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_es_ret_1h_vwap ON event_study(ret_t0_1h_vwap)"
        )
        self.conn.commit()

    def build_all(self) -> int:
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
            if self._build_one(filing_dict):
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

    def _calc_return(self, base: Optional[float], target: Optional[float]) -> Optional[float]:
        if base is None or target is None:
            return None
        if base == 0.0:
            return None
        return round((target - base) / base * 100, 4)

    def _calc_vwap5(self, df: pd.DataFrame, window_start: datetime) -> Optional[float]:
        vwap_cutoff = window_start + timedelta(minutes=5)
        ts = df["timestamp"]
        # Normalize both sides to tz-naive for robust mixed-tz comparison
        if ts.dt.tz is not None:
            ts_cmp = ts.dt.tz_localize(None)
        else:
            ts_cmp = ts
        cutoff_cmp = vwap_cutoff.replace(tzinfo=None) if vwap_cutoff.tzinfo else vwap_cutoff
        vwap_bars = df[ts_cmp <= cutoff_cmp]
        if vwap_bars.empty:
            return float(df.iloc[0]["open"]) if not df.empty else None
        total_vol = vwap_bars["volume"].sum()
        if total_vol == 0:
            return float(df.iloc[0]["open"]) if not df.empty else None
        vwap = (vwap_bars["close"] * vwap_bars["volume"]).sum() / total_vol
        return round(float(vwap), 4)

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
            # Case A: Pre-market — full day window on t0_date
            ws = datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI)
            we = datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI)
            reaction_windows: List[tuple] = [(t0_date, ws, we)]

        elif filing_time <= MARKET_CLOSE:
            # Case B: During market — centered ±2h/+4h clamped to market hours
            ws = max(
                filing_ist - timedelta(hours=2),
                datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI),
            )
            we = min(
                filing_ist + timedelta(hours=4),
                datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI),
            )
            # Guard: non-trading-day filings during "market hours" produce we < ws
            if we <= ws:
                ws = datetime(t0_date.year, t0_date.month, t0_date.day, 9, 15, tzinfo=_IST_ZI)
                we = datetime(t0_date.year, t0_date.month, t0_date.day, 15, 30, tzinfo=_IST_ZI)
            reaction_windows = [(t0_date, ws, we)]

        else:
            # Case C: Post-market — fetch t0_date (T+1) AND t0_date+1 trading day (T+2)
            # t0_date is already the first reaction day (next trading day after filing)
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
                raw_df = get_ohlcv(symbol, 0, "15minute", ws, we)
                time.sleep(0.5)
                if not raw_df.empty:
                    # get_ohlcv returns timestamp as index; reset to column
                    raw_df.index.name = raw_df.index.name or "timestamp"
                    df_reset = raw_df.reset_index()
                    if "index" in df_reset.columns and "timestamp" not in df_reset.columns:
                        df_reset = df_reset.rename(columns={"index": "timestamp"})
                    all_dfs.append(df_reset)
            except Exception as e:
                logger.warning(
                    "[EventStudyBuilder] intraday fetch failed %s %s: %s", symbol, rdate, e
                )

        if not all_dfs:
            return EMPTY_RESULT

        combined = pd.concat(all_dfs).sort_values("timestamp").reset_index(drop=True)

        if len(combined) < 2:
            return EMPTY_RESULT

        # Ensure timestamps are tz-aware for arithmetic
        if combined["timestamp"].dt.tz is None:
            combined["timestamp"] = combined["timestamp"].dt.tz_localize("Asia/Kolkata")

        first_bar    = combined.iloc[0]
        anchor_open  = float(first_bar["open"])
        anchor_vwap5 = self._calc_vwap5(all_dfs[0], first_window_start)

        first_bar_ts = first_bar["timestamp"]
        if first_bar_ts.tzinfo is None:
            first_bar_ts = first_bar_ts.tz_localize("Asia/Kolkata")

        def _get_return(anchor: Optional[float], offset_min: int) -> Optional[float]:
            if anchor is None:
                return None
            target_ts = first_bar_ts + timedelta(minutes=offset_min)
            deltas = (combined["timestamp"] - target_ts).abs()
            idx = deltas.idxmin()
            if deltas[idx].total_seconds() > 1800:  # 30-min gap threshold
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

    def _build_one(self, filing_row: Dict[str, Any]) -> bool:
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
                instrument_token=0,
                interval="day",
                start=start_dt,
                end=end_dt,
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

            # Fetch intraday window and update — idempotent, runs even if INSERT was ignored
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

            return True

        except Exception as e:
            logger.error(
                "Error building event study for %s filing %s: %s",
                filing_row.get("symbol"), filing_row.get("id"), e,
            )
            self.conn.rollback()
            return False
