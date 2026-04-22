import pytest
from datetime import datetime, date, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo
import sqlite3
import json
import pandas as pd
from unittest.mock import patch, MagicMock, call

from src.event_study.event_study_builder import EventStudyBuilder

IST_ZI = ZoneInfo("Asia/Kolkata")


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    cursor.execute(
        "INSERT INTO filings_archive (id, symbol, filed_at, event_type, quality_score, processed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (filing_id, symbol, filed_at, "TEST", 0.8, 0),
    )
    builder.conn.commit()


def create_dummy_event_study_row(
    builder, es_id: int, filing_id: int, symbol: str, filed_at: str
):
    """Seed filings_archive + event_study row with NULL intraday_bars_json."""
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
    cursor.execute(
        "INSERT OR IGNORE INTO filings_archive "
        "(id, symbol, filed_at, event_type, quality_score, processed) VALUES (?,?,?,?,?,?)",
        (filing_id, symbol, filed_at, "TEST", 0.8, 1),
    )
    cursor.execute(
        "INSERT INTO event_study (id, filing_id, symbol, filed_at, intraday_bars_json) "
        "VALUES (?, ?, ?, ?, NULL)",
        (es_id, filing_id, symbol, filed_at),
    )
    builder.conn.commit()


def make_index_df(bars: list) -> pd.DataFrame:
    """
    Return a DataFrame with named timestamp index matching get_ohlcv return format.
    bars: list of (timestamp, open, high, low, close, volume)
    """
    records = [
        {"open": o, "high": h, "low": l, "close": c, "volume": v}
        for (ts, o, h, l, c, v) in bars
    ]
    idx = pd.DatetimeIndex([b[0] for b in bars], name="timestamp")
    return pd.DataFrame(records, index=idx)


# ── Tests 1-7 (originals — do not modify) ────────────────────────────────────

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
    base_date = date(2026, 4, 17)
    next_td = builder._get_nth_trading_day(base_date, 1)
    assert next_td == date(2026, 4, 20)
    next_td_2 = builder._get_nth_trading_day(base_date, 2)
    assert next_td_2 == date(2026, 4, 21)


def test_get_nth_trading_day_backward():
    builder = EventStudyBuilder(":memory:")
    base_date = date(2026, 4, 20)
    prev_td = builder._get_nth_trading_day(base_date, -1)
    assert prev_td == date(2026, 4, 17)


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

    df = pd.DataFrame(
        {"close": prices, "open": prices, "high": prices, "low": prices, "volume": 100},
        index=dates,
    )
    mock_get.return_value = df

    rows_built = builder.build_all()
    assert rows_built == 1

    cursor = builder.conn.cursor()
    cursor.execute(
        "SELECT price_t0_close, price_t3_close, price_t5_close, processed "
        "FROM event_study JOIN filings_archive ON event_study.filing_id = filings_archive.id "
        "WHERE filing_id=1"
    )
    row = cursor.fetchone()
    assert row[0] is not None
    assert row[1] is None
    assert row[2] is None
    assert row[3] == 1


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_build_marks_processed(mock_get):
    builder = EventStudyBuilder(":memory:")
    create_dummy_filing(builder, 1, "RIL",  "2026-04-15T10:00:00+05:30")
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

    filing_row = {
        "id": 1, "symbol": "HDFC",
        "filed_at": "2026-04-15T10:00:00+05:30",
        "event_type": "T", "quality_score": 1.0,
    }

    res1 = builder._build_one(filing_row)
    assert res1 is True
    res2 = builder._build_one(filing_row)
    assert res2 is True

    cursor = builder.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM event_study WHERE filing_id=1")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT processed FROM filings_archive")
    assert cursor.fetchone()[0] == 1


# ── Tests 8-20 (new intraday tests) ──────────────────────────────────────────

def test_vwap5_calculation():
    """VWAP of 09:15 and 09:20 bars (<=09:20 cutoff), ignoring 09:30 bar."""
    builder = EventStudyBuilder(":memory:")
    window_start = datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI)
    df = pd.DataFrame({
        "timestamp": [
            datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI),
            datetime(2026, 4, 17, 9, 20, tzinfo=IST_ZI),
            datetime(2026, 4, 17, 9, 30, tzinfo=IST_ZI),
        ],
        "open":   [100.0, 101.0, 102.0],
        "close":  [100.0, 102.0, 104.0],
        "volume": [1000,  2000,   500],
    })
    # vwap = (100*1000 + 102*2000) / 3000 = 304000/3000 = 101.3333
    result = builder._calc_vwap5(df, window_start)
    assert result == round(304000 / 3000, 4)


def test_vwap5_zero_volume_fallback():
    """Zero total volume → fall back to first bar open."""
    builder = EventStudyBuilder(":memory:")
    window_start = datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI)
    df = pd.DataFrame({
        "timestamp": [datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI)],
        "open":   [99.0],
        "close":  [100.0],
        "volume": [0],
    })
    result = builder._calc_vwap5(df, window_start)
    assert result == 99.0


def test_vwap5_no_bars_in_window_fallback():
    """No bars at or before vwap_cutoff → return first bar open."""
    builder = EventStudyBuilder(":memory:")
    window_start = datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI)
    df = pd.DataFrame({
        "timestamp": [datetime(2026, 4, 17, 9, 45, tzinfo=IST_ZI)],
        "open":   [99.0],
        "close":  [102.0],
        "volume": [1000],
    })
    result = builder._calc_vwap5(df, window_start)
    assert result == 99.0


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_premarket_full_day_window(mock_get):
    """Pre-market filing → get_ohlcv called once with T0 09:15–15:30."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    filing_dt = datetime(2026, 4, 17, 8, 0, tzinfo=IST_ZI)

    four_bars = make_index_df([
        (datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
        (datetime(2026, 4, 17, 9, 30, tzinfo=IST_ZI), 100.5, 102, 100, 101, 900),
        (datetime(2026, 4, 17, 9, 45, tzinfo=IST_ZI), 101, 103, 100.5, 102, 800),
        (datetime(2026, 4, 17, 10, 0, tzinfo=IST_ZI), 102, 104, 101, 103, 700),
    ])
    mock_get.return_value = four_bars

    builder._build_intraday_window("TCS", filing_dt, t0)

    assert mock_get.call_count == 1
    _, _, _, actual_start, actual_end = mock_get.call_args[0]
    assert actual_start.date() == t0
    assert actual_start.hour == 9 and actual_start.minute == 15
    assert actual_end.date() == t0
    assert actual_end.hour == 15 and actual_end.minute == 30


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_during_market_clamped(mock_get):
    """11:30 IST filing → window = 09:30–15:30 (2h back clamped is 09:30)."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    filing_dt = datetime(2026, 4, 17, 11, 30, tzinfo=IST_ZI)

    four_bars = make_index_df([
        (datetime(2026, 4, 17, 9, 30, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
        (datetime(2026, 4, 17, 9, 45, tzinfo=IST_ZI), 100.5, 102, 100, 101, 900),
        (datetime(2026, 4, 17, 10, 0, tzinfo=IST_ZI), 101, 103, 100.5, 102, 800),
        (datetime(2026, 4, 17, 10, 15, tzinfo=IST_ZI), 102, 104, 101, 103, 700),
    ])
    mock_get.return_value = four_bars

    builder._build_intraday_window("TCS", filing_dt, t0)

    assert mock_get.call_count == 1
    _, _, _, actual_start, actual_end = mock_get.call_args[0]
    assert actual_start.hour == 9 and actual_start.minute == 30
    assert actual_end.hour == 15 and actual_end.minute == 30


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_during_market_early_filing_clamped_to_open(mock_get):
    """09:30 IST filing → window_start clamped to 09:15 (2h back = 07:30)."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    filing_dt = datetime(2026, 4, 17, 9, 30, tzinfo=IST_ZI)

    four_bars = make_index_df([
        (datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
        (datetime(2026, 4, 17, 9, 30, tzinfo=IST_ZI), 100.5, 102, 100, 101, 900),
        (datetime(2026, 4, 17, 9, 45, tzinfo=IST_ZI), 101, 103, 100.5, 102, 800),
        (datetime(2026, 4, 17, 10, 0, tzinfo=IST_ZI), 102, 104, 101, 103, 700),
    ])
    mock_get.return_value = four_bars

    builder._build_intraday_window("TCS", filing_dt, t0)

    _, _, _, actual_start, actual_end = mock_get.call_args[0]
    assert actual_start.hour == 9 and actual_start.minute == 15
    # window_end = min(09:30 + 4h = 13:30, 15:30) = 13:30
    assert actual_end.hour == 13 and actual_end.minute == 30


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_postmarket_fetches_two_days(mock_get):
    """Post-market Friday 16:30 filing → get_ohlcv called twice (Monday + Tuesday)."""
    builder = EventStudyBuilder(":memory:")
    # filing_dt = Friday at 16:30 IST (post-market)
    friday = date(2026, 4, 17)
    filing_dt = datetime(2026, 4, 17, 16, 30, tzinfo=IST_ZI)
    # t0_date = next Monday (as _build_one would compute for post-market Friday)
    monday = date(2026, 4, 20)
    tuesday = date(2026, 4, 21)

    def make_4_bars(day: date) -> pd.DataFrame:
        return make_index_df([
            (datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
            (datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST_ZI), 100.5, 102, 100, 101, 900),
            (datetime(day.year, day.month, day.day, 9, 45, tzinfo=IST_ZI), 101, 103, 100.5, 102, 800),
            (datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST_ZI), 102, 104, 101, 103, 700),
        ])

    mock_get.side_effect = [make_4_bars(monday), make_4_bars(tuesday)]

    result = builder._build_intraday_window("TCS", filing_dt, monday)

    assert mock_get.call_count == 2

    _, _, _, start1, end1 = mock_get.call_args_list[0][0]
    assert start1.date() == monday
    assert start1.hour == 9 and start1.minute == 15
    assert end1.date() == monday
    assert end1.hour == 15 and end1.minute == 30

    _, _, _, start2, end2 = mock_get.call_args_list[1][0]
    assert start2.date() == tuesday
    assert start2.hour == 9 and start2.minute == 15
    assert end2.date() == tuesday
    assert end2.hour == 15 and end2.minute == 30

    bars = json.loads(result["intraday_bars_json"])
    assert len(bars) == 8


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_empty_df_all_nulls(mock_get):
    """Empty get_ohlcv → EMPTY_RESULT with no exceptions."""
    builder = EventStudyBuilder(":memory:")
    mock_get.return_value = pd.DataFrame()
    filing_dt = datetime(2026, 4, 17, 8, 0, tzinfo=IST_ZI)

    result = builder._build_intraday_window("TCS", filing_dt, date(2026, 4, 17))

    assert result["intraday_bars_json"] == "[]"
    assert result["intraday_anchor_open"] is None
    assert result["intraday_anchor_vwap5"] is None
    assert result["ret_t0_15min_open"] is None
    assert result["ret_t0_15min_vwap"] is None
    assert result["ret_t0_30min_open"] is None
    assert result["ret_t0_30min_vwap"] is None
    assert result["ret_t0_1h_open"] is None
    assert result["ret_t0_1h_vwap"] is None


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_single_bar_all_nulls(mock_get):
    """Single bar → len < 2 guard fires → EMPTY_RESULT."""
    builder = EventStudyBuilder(":memory:")
    one_bar = make_index_df([
        (datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
    ])
    mock_get.return_value = one_bar
    filing_dt = datetime(2026, 4, 17, 8, 0, tzinfo=IST_ZI)

    result = builder._build_intraday_window("TCS", filing_dt, date(2026, 4, 17))

    assert result["intraday_anchor_open"] is None
    assert result["ret_t0_15min_open"] is None
    assert result["intraday_bars_json"] == "[]"


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_intraday_target_bar_gap_exceeds_threshold(mock_get):
    """Bars at 09:15 and 13:00 only.
    ret_t0_15min: target=09:30, closest=09:15 (15 min ≤ 30 min) → not None.
    ret_t0_1h:    target=10:15, closest=09:15 (60 min > 30 min) → None.
    """
    builder = EventStudyBuilder(":memory:")
    two_bars = make_index_df([
        (datetime(2026, 4, 17, 9,  15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
        (datetime(2026, 4, 17, 13,  0, tzinfo=IST_ZI), 105, 108, 104, 108.0, 500),
    ])
    mock_get.return_value = two_bars
    filing_dt = datetime(2026, 4, 17, 8, 0, tzinfo=IST_ZI)

    result = builder._build_intraday_window("TCS", filing_dt, date(2026, 4, 17))

    assert result["ret_t0_15min_open"] is not None
    assert result["ret_t0_1h_open"] is None


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_dual_anchor_returns_differ(mock_get):
    """Open anchor and VWAP5 anchor produce different 15-min returns.

    Bars: 09:15 open=100.0 close=100.5 vol=1000
          09:20 close=101.0 vol=2000   (note: spec had 103.0 — typo; 101.0 gives vwap=100.8333)
          09:30 close=105.0 vol=500

    anchor_open  = 100.0
    anchor_vwap5 = (100.5*1000 + 101.0*2000) / 3000 = 302500/3000 = 100.8333
    ret_t0_15min_open = (105 - 100.0)   / 100.0   * 100 = 5.0
    ret_t0_15min_vwap = (105 - 100.8333)/ 100.8333 * 100 ≈ 4.1322
    """
    builder = EventStudyBuilder(":memory:")
    bars = make_index_df([
        (datetime(2026, 4, 17, 9, 15, tzinfo=IST_ZI), 100.0, 101.0, 99.0,  100.5, 1000),
        (datetime(2026, 4, 17, 9, 20, tzinfo=IST_ZI), 100.5, 102.0, 100.0, 101.0, 2000),
        (datetime(2026, 4, 17, 9, 30, tzinfo=IST_ZI), 101.0, 106.0, 100.0, 105.0,  500),
    ])
    mock_get.return_value = bars
    filing_dt = datetime(2026, 4, 17, 8, 0, tzinfo=IST_ZI)

    result = builder._build_intraday_window("TCS", filing_dt, date(2026, 4, 17))

    assert result["ret_t0_15min_open"] != result["ret_t0_15min_vwap"]
    assert result["ret_t0_15min_open"] == 5.0
    assert result["ret_t0_15min_vwap"] is not None
    assert abs(result["ret_t0_15min_vwap"] - 4.1322) < 0.001


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_build_intraday_only_backfills_nulls(mock_get):
    """build_intraday_only() updates 2 rows that have NULL intraday_bars_json."""
    builder = EventStudyBuilder(":memory:")
    create_dummy_event_study_row(builder, 1, 1, "TCS",  "2026-04-15T10:00:00+05:30")
    create_dummy_event_study_row(builder, 2, 2, "INFY", "2026-04-16T10:00:00+05:30")

    four_bars = make_index_df([
        (datetime(2026, 4, 15, 9, 15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
        (datetime(2026, 4, 15, 9, 30, tzinfo=IST_ZI), 100.5, 102, 100, 101, 900),
        (datetime(2026, 4, 15, 9, 45, tzinfo=IST_ZI), 101, 103, 100.5, 102, 800),
        (datetime(2026, 4, 15, 10, 0, tzinfo=IST_ZI), 102, 104, 101, 103, 700),
    ])
    mock_get.return_value = four_bars

    count = builder.build_intraday_only()
    assert count == 2

    cursor = builder.conn.cursor()
    cursor.execute("SELECT intraday_bars_json, intraday_anchor_open FROM event_study ORDER BY id")
    rows = cursor.fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["intraday_bars_json"] is not None
        assert row["intraday_bars_json"] != "[]"
        assert row["intraday_anchor_open"] is not None


@patch("src.event_study.event_study_builder.get_ohlcv")
def test_build_one_updates_intraday_on_duplicate_insert(mock_get):
    """Second _build_one call hits INSERT OR IGNORE but UPDATE still runs."""
    builder = EventStudyBuilder(":memory:")
    create_dummy_filing(builder, 1, "RIL", "2026-04-15T10:00:00+05:30")

    four_bars = make_index_df([
        (datetime(2026, 4, 15, 9, 15, tzinfo=IST_ZI), 100, 101, 99, 100.5, 1000),
        (datetime(2026, 4, 15, 9, 30, tzinfo=IST_ZI), 100.5, 102, 100, 101, 900),
        (datetime(2026, 4, 15, 9, 45, tzinfo=IST_ZI), 101, 103, 100.5, 102, 800),
        (datetime(2026, 4, 15, 10, 0, tzinfo=IST_ZI), 102, 104, 101, 103, 700),
    ])
    mock_get.return_value = four_bars

    filing_row = {
        "id": 1, "symbol": "RIL",
        "filed_at": "2026-04-15T10:00:00+05:30",
        "event_type": "TEST", "quality_score": 0.8,
    }

    builder._build_one(filing_row)
    builder._build_one(filing_row)

    cursor = builder.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM event_study WHERE filing_id=1")
    assert cursor.fetchone()[0] == 1

    cursor.execute("SELECT intraday_bars_json FROM event_study WHERE filing_id=1")
    row = cursor.fetchone()
    assert row["intraday_bars_json"] is not None
    assert row["intraday_bars_json"] != "[]"
