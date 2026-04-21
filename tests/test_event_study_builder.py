import pytest
from datetime import datetime, date, timedelta, timezone, time as dt_time
import sqlite3
import pandas as pd
from unittest.mock import patch, MagicMock

from src.event_study.event_study_builder import EventStudyBuilder

def create_dummy_filing(builder, filing_id: int, symbol: str, filed_at: str):
    cursor = builder.conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS filings_archive (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            filed_at TEXT,
            event_type TEXT,
            quality_score REAL,
            processed INTEGER DEFAULT 0
        )
    """)
    cursor.execute("INSERT INTO filings_archive (id, symbol, filed_at, event_type, quality_score, processed) VALUES (?, ?, ?, ?, ?, ?)",
        (filing_id, symbol, filed_at, "TEST", 0.8, 0))
    builder.conn.commit()

def test_schema_created():
    builder = EventStudyBuilder(":memory:")
    cursor = builder.conn.cursor()
    cursor.execute("PRAGMA table_info(event_study)")
    cols = {row[1]: row[2] for row in cursor.fetchall()}
    assert "filing_id" in cols
    assert "ret_t0_day" in cols
    assert "price_t5_close" in cols
    assert "vwap_delta" in cols

def test_calc_return_basic():
    builder = EventStudyBuilder(":memory:")
    assert builder._calc_return(100.0, 105.0) == 5.0
    assert builder._calc_return(100.0, 95.0) == -5.0
    assert builder._calc_return(None, 105.0) is None
    assert builder._calc_return(100.0, None) is None
    assert builder._calc_return(0.0, 105.0) is None

def test_get_nth_trading_day_forward():
    builder = EventStudyBuilder(":memory:")
    # April 17, 2026 is Friday
    base_date = date(2026, 4, 17)
    next_td = builder._get_nth_trading_day(base_date, 1)
    assert next_td == date(2026, 4, 20)  # skip weekend
    next_td_2 = builder._get_nth_trading_day(base_date, 2)
    assert next_td_2 == date(2026, 4, 21)

def test_get_nth_trading_day_backward():
    builder = EventStudyBuilder(":memory:")
    # Monday 2026-04-20
    base_date = date(2026, 4, 20)
    prev_td = builder._get_nth_trading_day(base_date, -1)
    assert prev_td == date(2026, 4, 17) # skip weekend

@patch("src.event_study.event_study_builder.get_ohlcv")
def test_future_price_is_null(mock_get):
    builder = EventStudyBuilder(":memory:")
    
    today_dt = datetime.now()
    t0_date = builder._get_nth_trading_day(today_dt.date(), -2)
    filed_at = f"{t0_date.isoformat()}T10:00:00+05:30"
    
    create_dummy_filing(builder, 1, "TCS", filed_at)
    
    dates = []
    prices = []
    for i in range(-5, 2):
        dates.append(pd.to_datetime(t0_date) + timedelta(days=i))
        prices.append(100.0)
        
    df = pd.DataFrame({"close": prices, "open": prices, "high": prices, "low": prices, "volume": 100}, index=dates)
    mock_get.return_value = df
    
    rows_built = builder.build_all()
    assert rows_built == 1
    
    cursor = builder.conn.cursor()
    cursor.execute("SELECT price_t0_close, price_t3_close, price_t5_close, processed FROM event_study JOIN filings_archive ON event_study.filing_id = filings_archive.id WHERE filing_id=1")
    row = cursor.fetchone()
    
    assert row[0] is not None  # T0 price
    assert row[1] is None      # T3 future
    assert row[2] is None      # T5 future
    assert row[3] == 1         # processed

@patch("src.event_study.event_study_builder.get_ohlcv")
def test_build_marks_processed(mock_get):
    builder = EventStudyBuilder(":memory:")
    create_dummy_filing(builder, 1, "RIL", "2026-04-15T10:00:00+05:30")
    create_dummy_filing(builder, 2, "INFY", "2026-04-16T10:00:00+05:30")
    
    mock_get.return_value = pd.DataFrame()
    
    assert builder.build_all() == 2
    
    cursor = builder.conn.cursor()
    cursor.execute("SELECT processed FROM filings_archive")
    assert all(row[0] == 1 for row in cursor.fetchall())

@patch("src.event_study.event_study_builder.get_ohlcv")
def test_duplicate_filing_is_skipped(mock_get):
    builder = EventStudyBuilder(":memory:")
    create_dummy_filing(builder, 1, "HDFC", "2026-04-15T10:00:00+05:30")
    mock_get.return_value = pd.DataFrame()
    
    filing_row = {"id": 1, "symbol": "HDFC", "filed_at": "2026-04-15T10:00:00+05:30", "event_type": "T", "quality_score": 1.0}
    
    res1 = builder._build_one(filing_row)
    assert res1 is True
    
    res2 = builder._build_one(filing_row)
    assert res2 is True 
    
    cursor = builder.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM event_study WHERE filing_id=1")
    assert cursor.fetchone()[0] == 1
    
    cursor.execute("SELECT processed FROM filings_archive")
    assert cursor.fetchone()[0] == 1
