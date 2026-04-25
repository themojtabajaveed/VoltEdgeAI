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
    # Step 6: builder skips symbols absent from the token map. Inject test
    # tokens so the build path is exercised instead of the skip-and-mark path.
    builder._token_map.update({"RIL": 100001, "INFY": 408065})
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
    builder._token_map.update({"HDFC": 100002})
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
    builder._token_map.update({"RIL": 100001})
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


# ── Tests 21-42 (Step 4: TA indicators) ──────────────────────────────────────

import ta as _ta_lib_test  # used for expected-value computation in tests


def make_ta_daily_df(
    n_bars: int,
    t0_date: date,
    *,
    t0_open: float = 119.5,
    t0_high: float = 125.0,
    t0_low: float = 115.0,
    t0_close: float = 120.0,
    t0_volume: int = 500_000,
    t1_close: float = 100.0,
    t1_open: float = 99.5,
    t1_high: float = 105.0,
    t1_low: float = 95.0,
    t1_volume: int = 200_000,
) -> pd.DataFrame:
    """Build daily OHLCV df (timestamp as index) ending on t0_date.
    Last bar = T0, second-to-last = T-1; earlier bars trend slowly."""
    dates: list = []
    d = t0_date
    while len(dates) < n_bars:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()

    records = []
    for i, dt in enumerate(dates):
        if dt == t0_date:
            records.append({
                "open": t0_open, "high": t0_high, "low": t0_low,
                "close": t0_close, "volume": t0_volume,
            })
        elif i == len(dates) - 2:
            records.append({
                "open": t1_open, "high": t1_high, "low": t1_low,
                "close": t1_close, "volume": t1_volume,
            })
        else:
            p = 90.0 + i * 0.4
            records.append({
                "open": p - 0.3, "high": p + 3.0, "low": p - 3.0,
                "close": p, "volume": 200_000,
            })

    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="timestamp")
    return pd.DataFrame(records, index=idx)


# ── 21: schema ────────────────────────────────────────────────────────────────

def test_schema_has_ta_columns():
    builder = EventStudyBuilder(":memory:")
    cursor = builder.conn.cursor()
    cursor.execute("PRAGMA table_info(event_study)")
    cols = {row[1] for row in cursor.fetchall()}
    for expected in [
        "rsi_7_pre", "rsi_14_pre",
        "vol_spike_5d", "vol_spike_20d",
        "vwap_delta_t0", "atr_rel_range", "gap_pct",
        "sector_rel_return", "market_rel_return", "sector_alpha",
    ]:
        assert expected in cols, f"Missing column: {expected}"


# ── 22-24: _compute_vwap_from_bars ───────────────────────────────────────────

def test_compute_vwap_from_bars_correct():
    builder = EventStudyBuilder(":memory:")
    # (100*1000 + 102*2000 + 104*1000) / 4000 = 408000/4000 = 102.0
    bars_json = '[{"close":100,"volume":1000},{"close":102,"volume":2000},{"close":104,"volume":1000}]'
    assert builder._compute_vwap_from_bars(bars_json) == 102.0


def test_compute_vwap_zero_volume_returns_none():
    builder = EventStudyBuilder(":memory:")
    bars_json = '[{"close":100,"volume":0},{"close":102,"volume":0}]'
    assert builder._compute_vwap_from_bars(bars_json) is None


def test_compute_vwap_empty_json_returns_none():
    builder = EventStudyBuilder(":memory:")
    assert builder._compute_vwap_from_bars("[]") is None
    assert builder._compute_vwap_from_bars(None) is None
    assert builder._compute_vwap_from_bars("invalid_json") is None


# ── 25-26: gap_pct ────────────────────────────────────────────────────────────

def test_gap_pct_positive():
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(30, t0, t0_open=105.0, t1_close=100.0)
    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["gap_pct"] == 5.0


def test_gap_pct_negative():
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(30, t0, t0_open=95.0, t1_close=100.0)
    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["gap_pct"] == -5.0


# ── 27-29: volume spike ───────────────────────────────────────────────────────

def _make_vol_df(t0_date: date, volumes: list) -> pd.DataFrame:
    """Build a minimal daily df whose volumes are exactly the given list (last = T0)."""
    n = len(volumes)
    dates: list = []
    d = t0_date
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()
    p = 100.0
    records = [{"open": p, "high": p+1, "low": p-1, "close": p, "volume": v} for v in volumes]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="timestamp")
    return pd.DataFrame(records, index=idx)


def test_vol_spike_5d_correct():
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    vols = [200_000] * 25 + [200_000] * 5 + [1_000_000]  # last = T0
    df = _make_vol_df(t0, vols)
    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["vol_spike_5d"] == 5.0


def test_vol_spike_excludes_t0():
    """T0 must not be included in the pre-T0 average window."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    # 7 bars: first 5 = 100K, T-1 = 200K, T0 = 900K
    vols = [100_000] * 5 + [200_000, 900_000]
    df = _make_vol_df(t0, vols)
    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    # pre_t0 tail(5): [100K, 100K, 100K, 100K, 200K] → mean = 120K
    # vol_spike_5d = 900_000 / 120_000 = 7.5
    assert result["vol_spike_5d"] == 7.5
    # If T0 were included: tail(5) = [100K, 100K, 100K, 200K, 900K] → mean = 280K → spike ≠ 7.5
    assert result["vol_spike_5d"] != round(900_000 / 280_000, 4)


def test_vol_spike_zero_avg_returns_none():
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    vols = [0] * 25 + [1_000_000]
    df = _make_vol_df(t0, vols)
    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["vol_spike_5d"] is None
    assert result["vol_spike_20d"] is None


# ── 30-31: ATR relative range ─────────────────────────────────────────────────

def test_atr_rel_range_correct():
    """atr_rel_range = (T0_high - T0_low) / ATR(14) at T-1."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(30, t0, t0_high=125.0, t0_low=115.0)
    # Compute expected ATR at T-1 using the real ta library
    atr_series = _ta_lib_test.volatility.AverageTrueRange(
        df["high"].astype(float), df["low"].astype(float), df["close"].astype(float), window=14
    ).average_true_range()
    t0_iloc = len(df) - 1
    t1_iloc = len(df) - 2
    atr_t1 = float(atr_series.iloc[t1_iloc])
    expected = round((125.0 - 115.0) / atr_t1, 4)

    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["atr_rel_range"] == expected


def test_atr_rel_range_zero_atr_returns_none():
    """Zero ATR at T-1 must not cause ZeroDivisionError."""
    from unittest.mock import patch, MagicMock
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(30, t0)

    mock_atr_instance = MagicMock()
    mock_atr_instance.average_true_range.return_value = pd.Series([0.0] * len(df))
    with patch(
        "src.event_study.event_study_builder._ta_lib.volatility.AverageTrueRange",
        return_value=mock_atr_instance,
    ):
        result = builder._compute_ta_indicators(
            "TCS", t0, df, None, pd.DataFrame(), pd.DataFrame()
        )
    assert result["atr_rel_range"] is None


# ── 32-33: VWAP delta ─────────────────────────────────────────────────────────

def test_vwap_delta_uses_intraday_bars_when_available():
    """Intraday bars with VWAP=101 → delta = (103-101)/101*100 = 1.9802."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(30, t0, t0_close=103.0)
    bars_json = '[{"close":101.0,"volume":1000},{"close":101.0,"volume":1000}]'
    result = builder._compute_ta_indicators("TCS", t0, df, bars_json, pd.DataFrame(), pd.DataFrame())
    expected = round((103.0 - 101.0) / 101.0 * 100, 4)
    assert result["vwap_delta_t0"] == expected


def test_vwap_delta_fallback_to_hlc3():
    """Empty intraday → HLC/3 fallback. T0 H=110, L=90, C=105 → hlc3=101.6667."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(30, t0, t0_high=110.0, t0_low=90.0, t0_close=105.0)
    result = builder._compute_ta_indicators("TCS", t0, df, "[]", pd.DataFrame(), pd.DataFrame())
    hlc3 = (110.0 + 90.0 + 105.0) / 3
    expected = round((105.0 - hlc3) / hlc3 * 100, 4)
    assert abs(result["vwap_delta_t0"] - expected) < 0.0001


# ── 34-35: RSI ────────────────────────────────────────────────────────────────

def test_rsi_14_pre_is_at_t_minus_1():
    """rsi_14_pre must reflect T-1, not T0 (big T0 jump makes them differ materially)."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    # closes[-2]=100 (T-1 drop), closes[-1]=120 (T0 surge) → large RSI divergence
    df = make_ta_daily_df(30, t0, t0_close=120.0, t1_close=100.0)
    close_s = df["close"].astype(float)
    rsi14 = _ta_lib_test.momentum.RSIIndicator(close_s, window=14).rsi()
    t1_iloc = len(df) - 2
    t0_iloc = len(df) - 1
    expected_t1 = round(float(rsi14.iloc[t1_iloc]), 4)
    unexpected_t0 = round(float(rsi14.iloc[t0_iloc]), 4)

    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["rsi_14_pre"] == expected_t1
    assert result["rsi_14_pre"] != unexpected_t0


def test_rsi_nan_warmup_returns_none():
    """10-bar DataFrame → RSI(14) all NaN → rsi_14_pre is None."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    df = make_ta_daily_df(10, t0)
    result = builder._compute_ta_indicators("TCS", t0, df, None, pd.DataFrame(), pd.DataFrame())
    assert result["rsi_14_pre"] is None  # insufficient data for RSI(14)


# ── 36-38: benchmark returns ──────────────────────────────────────────────────

def _make_bench_df(t1_close: float, t0_close: float, t0_date: date) -> pd.DataFrame:
    """2-row daily df: T-1 and T0, both on weekdays."""
    t1_date = t0_date - timedelta(days=1)
    while t1_date.weekday() >= 5:
        t1_date -= timedelta(days=1)
    idx = pd.DatetimeIndex([pd.Timestamp(t1_date), pd.Timestamp(t0_date)], name="timestamp")
    return pd.DataFrame([
        {"open": t1_close, "high": t1_close + 1, "low": t1_close - 1, "close": t1_close, "volume": 100_000},
        {"open": t0_close, "high": t0_close + 1, "low": t0_close - 1, "close": t0_close, "volume": 100_000},
    ], index=idx)


def test_sector_rel_return_correct():
    """Stock +5%, sector +2% → sector_rel_return = 3.0."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    stock_df  = make_ta_daily_df(30, t0, t0_close=105.0, t1_close=100.0)
    sector_df = _make_bench_df(100.0, 102.0, t0)
    result = builder._compute_ta_indicators(
        "TCS", t0, stock_df, None, sector_df, pd.DataFrame()
    )
    assert result["sector_rel_return"] == 3.0


def test_market_rel_return_correct():
    """Stock +5%, Nifty +1.5% → market_rel_return = 3.5."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    stock_df  = make_ta_daily_df(30, t0, t0_close=105.0, t1_close=100.0)
    nifty_df  = _make_bench_df(100.0, 101.5, t0)
    result = builder._compute_ta_indicators(
        "TCS", t0, stock_df, None, pd.DataFrame(), nifty_df
    )
    assert result["market_rel_return"] == 3.5


def test_sector_alpha_correct():
    """Sector +2%, Nifty +1.5% → sector_alpha = 0.5."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    stock_df  = make_ta_daily_df(30, t0, t0_close=105.0, t1_close=100.0)
    sector_df = _make_bench_df(100.0, 102.0, t0)
    nifty_df  = _make_bench_df(100.0, 101.5, t0)
    result = builder._compute_ta_indicators(
        "TCS", t0, stock_df, None, sector_df, nifty_df
    )
    assert result["sector_alpha"] == 0.5


# ── 39: sector None when mapping fails ───────────────────────────────────────

def test_sector_rel_none_when_sector_mapping_fails():
    """Empty sector_df → sector_rel_return and sector_alpha are None;
    market_rel_return is still computed if nifty_df is valid."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    stock_df = make_ta_daily_df(30, t0, t0_close=105.0, t1_close=100.0)
    nifty_df = _make_bench_df(100.0, 101.5, t0)
    # Pass empty sector_df (simulates _get_sector_symbol returning None)
    result = builder._compute_ta_indicators(
        "TCS", t0, stock_df, None, pd.DataFrame(), nifty_df
    )
    assert result["sector_rel_return"] is None
    assert result["sector_alpha"] is None
    assert result["market_rel_return"] == 3.5  # still computed


# ── 40: Nifty 50 cached, not re-fetched per filing ───────────────────────────

@patch("src.event_study.event_study_builder.get_ohlcv")
def test_nifty_cache_not_refetched_per_filing(mock_get):
    """build_all() with 3 TCS filings → exactly 1 NIFTY 50 call and 1 NIFTY IT call."""
    builder = EventStudyBuilder(":memory:")
    for i in range(1, 4):
        create_dummy_filing(builder, i, "TCS", f"2026-04-15T{9+i:02d}:00:00+05:30")
    mock_get.return_value = pd.DataFrame()

    count = builder.build_all()
    assert count == 3

    def _first_arg(c) -> str:
        # handle both positional and keyword call styles
        if c.args:
            return c.args[0]
        return c.kwargs.get("symbol", "")

    nifty50_calls = sum(
        1 for c in mock_get.call_args_list if _first_arg(c) == "NIFTY 50"
    )
    nifty_it_calls = sum(
        1 for c in mock_get.call_args_list if _first_arg(c) == "NIFTY IT"
    )
    assert nifty50_calls == 1
    assert nifty_it_calls == 1


# ── 41: single indicator failure does not abort ───────────────────────────────

def test_single_indicator_failure_does_not_abort():
    """RSI exception must leave rsi_* None but not kill gap_pct or vol_spike_5d."""
    from unittest.mock import patch
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 17)
    vols = [200_000] * 29 + [1_000_000]
    df = _make_vol_df(t0, vols)
    # Re-build df with proper OHLC columns (prices = 100, needed for gap_pct)
    df = make_ta_daily_df(30, t0, t0_open=105.0, t1_close=100.0, t0_volume=1_000_000, t1_volume=200_000)

    with patch(
        "src.event_study.event_study_builder._ta_lib.momentum.RSIIndicator",
        side_effect=Exception("mock RSI failure"),
    ):
        result = builder._compute_ta_indicators(
            "TCS", t0, df, None, pd.DataFrame(), pd.DataFrame()
        )

    assert isinstance(result, dict)
    assert result["rsi_7_pre"] is None
    assert result["rsi_14_pre"] is None
    assert result["gap_pct"] is not None          # still computed
    assert result["vol_spike_5d"] is not None     # still computed


# ── 42: build_ta_only backfills NULL rsi ─────────────────────────────────────

@patch("src.event_study.event_study_builder.get_ohlcv")
def test_build_ta_only_backfills_null_rsi(mock_get):
    """build_ta_only() updates rows where rsi_14_pre IS NULL."""
    builder = EventStudyBuilder(":memory:")
    t0 = date(2026, 4, 15)
    filed_at = "2026-04-15T10:00:00+05:30"
    create_dummy_event_study_row(builder, 1, 1, "TCS",  filed_at)
    create_dummy_event_study_row(builder, 2, 2, "INFY", filed_at)

    mock_get.return_value = make_ta_daily_df(30, t0)

    count = builder.build_ta_only()
    assert count == 2

    cursor = builder.conn.cursor()
    cursor.execute("SELECT rsi_14_pre FROM event_study ORDER BY id")
    rows = cursor.fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["rsi_14_pre"] is not None
