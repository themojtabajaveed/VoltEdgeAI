import sqlite3
import json
import logging
import time
from datetime import datetime, date, timedelta, timezone, time as datetime_time
from typing import Optional, Dict, Any

import pandas as pd
from src.data_ingestion.market_history import get_ohlcv
from src.utils.market_calendar import is_market_day

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

class EventStudyBuilder:
    def __init__(self, db_path: str = "data/history.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self):
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
        self.conn.commit()

    def build_all(self) -> int:
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT id, symbol, filed_at, event_type, quality_score FROM filings_archive WHERE processed = 0")
        except sqlite3.OperationalError:
            # If filings_archive doesn't exist, we just skip smoothly
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

    def _build_one(self, filing_row: Dict[str, Any]) -> bool:
        try:
            filed_at_str = filing_row["filed_at"]
            if filed_at_str.endswith("Z"):
                filing_dt = datetime.fromisoformat(filed_at_str[:-1]).replace(tzinfo=timezone.utc).astimezone(IST)
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
            t_plus1 = self._get_nth_trading_day(t0_date, 1)
            t_plus3 = self._get_nth_trading_day(t0_date, 3)
            t_plus5 = self._get_nth_trading_day(t0_date, 5)
            
            start_dt = datetime.combine(t_minus5, datetime_time(9, 0)).replace(tzinfo=IST)
            end_dt = datetime.combine(t_plus5, datetime_time(16, 0)).replace(tzinfo=IST)
            
            df = get_ohlcv(
                symbol=filing_row["symbol"],
                instrument_token=0,
                interval="day",
                start=start_dt,
                end=end_dt
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
                
                # If no exact match (e.g. data gaps), find closest valid available row
                diffs = [abs((d - target_date).days) for d in df_dates]
                min_diff = min(diffs)
                closest_idx = diffs.index(min_diff)
                return float(df.iloc[closest_idx]["close"])
                
            price_t_minus_1 = get_price(t_minus1)
            price_t0_close = get_price(t0_date)
            price_t1_close = get_price(t_plus1)
            price_t3_close = get_price(t_plus3)
            price_t5_close = get_price(t_plus5)
            
            ret_t0_day = self._calc_return(price_t_minus_1, price_t0_close)
            ret_t1_day = self._calc_return(price_t_minus_1, price_t1_close)
            ret_t3_day = self._calc_return(price_t_minus_1, price_t3_close)
            ret_t5_day = self._calc_return(price_t_minus_1, price_t5_close)
            
            daily_bars = []
            if not df.empty:
                for idx, row_series in df.iterrows():
                    bar = {
                        "date": idx.date().isoformat() if hasattr(idx, 'date') else str(idx).split(" ")[0],
                        "open": float(row_series["open"]),
                        "high": float(row_series["high"]),
                        "low":  float(row_series["low"]),
                        "close": float(row_series["close"]),
                        "volume": int(row_series["volume"])
                    }
                    daily_bars.append(bar)
                    
            daily_bars_json = json.dumps(daily_bars)

            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE into event_study (
                    filing_id, symbol, filed_at, event_type, quality_score,
                    ret_t0_day, ret_t1_day, ret_t3_day, ret_t5_day,
                    price_t_minus_1, price_t0_close, price_t1_close, price_t3_close,
                    price_t5_close, daily_bars_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                filing_row["id"], filing_row["symbol"], filing_row["filed_at"], filing_row.get("event_type"),
                filing_row.get("quality_score"), ret_t0_day, ret_t1_day, ret_t3_day, ret_t5_day,
                price_t_minus_1, price_t0_close, price_t1_close, price_t3_close, price_t5_close,
                daily_bars_json
            ))
            
            cursor.execute("UPDATE filings_archive SET processed=1 WHERE id=?", (filing_row["id"],))
            self.conn.commit()
            
            return True
            
        except Exception as e:
            logger.error(f"Error building event study for {filing_row.get('symbol')} filing {filing_row.get('id')}: {e}")
            self.conn.rollback()
            return False
