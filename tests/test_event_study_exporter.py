import pytest
import sqlite3
import os
import json
import csv
from src.event_study.event_study_exporter import EventStudyExporter
import numpy as np

def setup_db(db_path, has_event_study=True, rows=None):
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE filings_archive (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            company_name TEXT,
            exchange TEXT,
            filed_at TEXT,
            headline TEXT,
            category TEXT,
            event_type TEXT,
            event_direction TEXT,
            event_summary TEXT,
            freshness_tag TEXT,
            quality_score REAL,
            urgency_score REAL,
            deal_size_inr REAL,
            processed INTEGER
        )
    """)
    
    if has_event_study:
        cursor.execute("""
            CREATE TABLE event_study (
                id INTEGER PRIMARY KEY,
                filing_id INTEGER,
                vol_spike_20d REAL,
                rsi_14_pre REAL,
                gap_pct REAL,
                atr_rel_range REAL,
                sector_rel_return REAL,
                sector_alpha REAL,
                intraday_anchor_open REAL,
                intraday_anchor_vwap5 REAL,
                ret_t0_1h_open REAL,
                ret_t5_day REAL,
                intraday_bars_json TEXT,
                daily_bars_json TEXT,
                processed INTEGER
            )
        """)
        
    if rows:
        for r in rows:
            cursor.execute("""
                INSERT INTO filings_archive (id, symbol, event_type, freshness_tag, processed)
                VALUES (?, ?, ?, ?, 1)
            """, (r.get("filing_id"), r.get("symbol", "TEST"), r.get("event_type"), r.get("freshness_tag")))
            
            cursor.execute("""
                INSERT INTO event_study (
                    filing_id, vol_spike_20d, rsi_14_pre, gap_pct, atr_rel_range, 
                    sector_rel_return, sector_alpha, intraday_anchor_open, 
                    intraday_anchor_vwap5, ret_t0_1h_open, ret_t5_day,
                    intraday_bars_json, daily_bars_json, processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                r.get("filing_id"), r.get("vol_spike_20d"), r.get("rsi_14_pre"), 
                r.get("gap_pct"), r.get("atr_rel_range"), r.get("sector_rel_return"),
                r.get("sector_alpha"), r.get("intraday_anchor_open"), 
                r.get("intraday_anchor_vwap5"), r.get("ret_t0_1h_open"), 
                r.get("ret_t5_day"), r.get("intraday_bars_json"), r.get("daily_bars_json")
            ))
            
    conn.commit()
    conn.close()


def test_output_dir_created(tmp_path):
    out_dir = tmp_path / "test_exports_voltedge"
    EventStudyExporter(db_path=":memory:", output_dir=str(out_dir))
    assert os.path.isdir(str(out_dir))

def test_export_all_returns_empty_on_no_rows(tmp_path):
    db_path = str(tmp_path / "empty.db")
    setup_db(db_path, has_event_study=True, rows=[])
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    assert res == {}

def test_percentiles_computed_correctly():
    exporter = EventStudyExporter(db_path=":memory:")
    rows = [{"vol_spike_20d": i} for i in range(1, 11)]
    p = exporter._compute_percentiles(rows)
    assert abs(p["vol_p90"] - np.percentile(range(1, 11), 90)) < 0.01

def test_percentile_skipped_when_fewer_than_5_values():
    exporter = EventStudyExporter(db_path=":memory:")
    rows = [{"vol_spike_20d": 1}, {"vol_spike_20d": 2}, {"vol_spike_20d": 3}]
    p = exporter._compute_percentiles(rows)
    assert "vol_p75" not in p
    assert "vol_p90" not in p

def test_tag_pristine_filing():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"freshness_tag": "PRISTINE"}
    tags = exporter._generate_tags(row, {})
    assert "PRISTINE_FILING" in tags

def test_tag_vol_z_score_extreme():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"vol_spike_20d": 10}
    p = {"vol_p90": 9, "vol_p75": 7}
    tags = exporter._generate_tags(row, p)
    assert "VOL_Z_SCORE_EXTREME" in tags
    assert "VOL_Z_SCORE_HIGH" not in tags

def test_tag_vol_z_score_high_not_extreme():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"vol_spike_20d": 8}
    p = {"vol_p90": 9, "vol_p75": 7}
    tags = exporter._generate_tags(row, p)
    assert "VOL_Z_SCORE_HIGH" in tags
    assert "VOL_Z_SCORE_EXTREME" not in tags

def test_tag_rsi_overbought():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"rsi_14_pre": 85}
    p = {"rsi_p90": 80}
    tags = exporter._generate_tags(row, p)
    assert "RSI_OVERBOUGHT_90P" in tags

def test_tag_rsi_oversold():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"rsi_14_pre": 15}
    p = {"rsi_p10": 20}
    tags = exporter._generate_tags(row, p)
    assert "RSI_OVERSOLD_10P" in tags

def test_tag_alpha_decoupling_positive():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"sector_rel_return": 2.0, "sector_alpha": -1.0}
    tags = exporter._generate_tags(row, {})
    assert "ALPHA_DECOUPLING_POSITIVE" in tags
    assert "ALPHA_DECOUPLING_NEGATIVE" not in tags

def test_tag_alpha_decoupling_negative():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"sector_rel_return": -2.0, "sector_alpha": 1.0}
    tags = exporter._generate_tags(row, {})
    assert "ALPHA_DECOUPLING_NEGATIVE" in tags

def test_tag_cat_event_type():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"event_type": "ORDER_WIN"}
    tags = exporter._generate_tags(row, {})
    assert "CAT_ORDER_WIN" in tags

def test_tag_open_vwap_divergence():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"intraday_anchor_open": 100.0, "intraday_anchor_vwap5": 100.8}
    tags = exporter._generate_tags(row, {})
    assert "OPEN_VWAP_DIVERGENCE" in tags

def test_tag_no_divergence_below_threshold():
    exporter = EventStudyExporter(db_path=":memory:")
    row = {"intraday_anchor_open": 100.0, "intraday_anchor_vwap5": 100.3}
    tags = exporter._generate_tags(row, {})
    assert "OPEN_VWAP_DIVERGENCE" not in tags

def test_json_metadata_fields(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": i} for i in range(5)]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    assert "metadata" in data
    assert "summary_analytics" in data
    assert "event_clusters" in data
    assert data["metadata"]["export_version"] == "1.0"
    assert data["metadata"]["benchmark_index"] == "NIFTY 50"
    assert data["metadata"]["total_events"] == 5

def test_json_event_clusters_keyed_by_event_type(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "event_type": "ORDER_WIN"}, 
            {"filing_id": 2, "event_type": "ORDER_WIN"},
            {"filing_id": 3, "event_type": "DEAL"}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    assert "ORDER_WIN" in data["event_clusters"]
    assert "DEAL" in data["event_clusters"]
    assert len(data["event_clusters"]["ORDER_WIN"]["events"]) == 2
    assert len(data["event_clusters"]["DEAL"]["events"]) == 1

def test_json_cluster_summary_win_rate(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [
        {"filing_id": 1, "event_type": "ORDER_WIN", "ret_t0_1h_open": 1.0},
        {"filing_id": 2, "event_type": "ORDER_WIN", "ret_t0_1h_open": 2.0},
        {"filing_id": 3, "event_type": "ORDER_WIN", "ret_t0_1h_open": -1.0},
        {"filing_id": 4, "event_type": "ORDER_WIN", "ret_t0_1h_open": -2.0}
    ]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    assert data["event_clusters"]["ORDER_WIN"]["cluster_summary"]["win_rate_1h"] == "50%"

def test_json_cross_tab_minimum_2_events(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "event_type": "RARE_EVENT", "freshness_tag": "PRISTINE"}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    cross_tabs = data["summary_analytics"]["cross_tab_results"]
    assert not any(ct["segment"].startswith("RARE_EVENT") for ct in cross_tabs)

def test_json_cross_tab_has_correct_segment_string(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [
        {"filing_id": 1, "event_type": "ORDER_WIN", "freshness_tag": "PRISTINE", "sector_alpha": 100.0},
        {"filing_id": 2, "event_type": "ORDER_WIN", "freshness_tag": "PRISTINE", "sector_alpha": 100.0},
        {"filing_id": 3, "sector_alpha": 0.0},
        {"filing_id": 4, "sector_alpha": 0.0},
        {"filing_id": 5, "sector_alpha": 0.0},
    ]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    cross_tabs = data["summary_analytics"]["cross_tab_results"]
    assert any(ct["segment"] == "ORDER_WIN | PRISTINE | HIGH_ALPHA" for ct in cross_tabs)


def test_json_intraday_bars_parsed_not_string(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "event_type": "ORDER_WIN", "intraday_bars_json": '[{"close": 100}]'}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    ev = data["event_clusters"]["ORDER_WIN"]["events"][0]
    assert isinstance(ev["intraday_bars"], list)

def test_json_daily_bars_parsed_not_string(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "event_type": "ORDER_WIN", "daily_bars_json": '[{"close": 100}]'}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["json"], "r") as f:
        data = json.load(f)
    ev = data["event_clusters"]["ORDER_WIN"]["events"][0]
    assert isinstance(ev["daily_bars"], list)

def test_csv_master_created(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "event_type": "ORDER_WIN"}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    assert os.path.exists(res["csv_master"])
    with open(res["csv_master"], "r") as f:
        reader = csv.DictReader(f)
        assert "filing_id" in reader.fieldnames
        assert "intraday_bars_json" not in reader.fieldnames
        assert "contextual_tags" in reader.fieldnames

def test_csv_master_contextual_tags_pipe_separated(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "event_type": "ORDER_WIN", "freshness_tag": "PRISTINE"}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["csv_master"], "r") as f:
        reader = list(csv.DictReader(f))
        tags = reader[0]["contextual_tags"]
        assert isinstance(tags, str)
        assert "|" in tags or tags == "PRISTINE_FILING|CAT_ORDER_WIN"

def test_csv_timeseries_long_format(tmp_path):
    db_path = str(tmp_path / "test.db")
    bars = [{"date": "2026-04-01", "close": 100}, {"date": "2026-04-02", "close": 101}]
    rows = [{"filing_id": 1, "daily_bars_json": json.dumps(bars)}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["csv_daily"], "r") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2

def test_csv_intraday_long_format(tmp_path):
    db_path = str(tmp_path / "test.db")
    bars = [{"timestamp": "2026-04-01T09:15:00", "close": 100}] * 8
    rows = [{"filing_id": 1, "intraday_bars_json": json.dumps(bars)}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["csv_intraday"], "r") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 8

def test_csv_skips_null_bars_json(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [
        {"filing_id": 1, "daily_bars_json": None},
        {"filing_id": 2, "daily_bars_json": '[{"date": "2026-04-01", "close": 100}]'}
    ]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["csv_daily"], "r") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["filing_id"] == "2"

def test_sector_alpha_tercile_assignment(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": i, "sector_alpha": float(i)} for i in range(1, 10)]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["csv_master"], "r") as f:
        reader = list(csv.DictReader(f))
        highs = [r for r in reader if r["sector_alpha_tercile"] == "HIGH_ALPHA"]
        neuts = [r for r in reader if r["sector_alpha_tercile"] == "NEUTRAL_ALPHA"]
        lows = [r for r in reader if r["sector_alpha_tercile"] == "LOW_ALPHA"]
        assert len(highs) > 0
        assert len(neuts) > 0
        assert len(lows) > 0

def test_sector_alpha_tercile_none_is_unknown(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1, "sector_alpha": None}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    with open(res["csv_master"], "r") as f:
        reader = list(csv.DictReader(f))
        assert reader[0]["sector_alpha_tercile"] == "UNKNOWN"

def test_export_all_returns_correct_paths_and_count(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1}, {"filing_id": 2}, {"filing_id": 3}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    res = exporter.export_all()
    assert res["total_events"] == 3
    assert "json" in res
    assert "csv_master" in res
    assert "csv_daily" in res
    assert "csv_intraday" in res
    assert os.path.exists(res["json"])
    assert os.path.exists(res["csv_master"])
    assert os.path.exists(res["csv_daily"])
    assert os.path.exists(res["csv_intraday"])

from unittest.mock import patch

def test_export_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    rows = [{"filing_id": 1}]
    setup_db(db_path, rows=rows)
    exporter = EventStudyExporter(db_path=db_path, output_dir=str(tmp_path))
    
    with patch("src.event_study.event_study_exporter.datetime") as mock_dt:
        mock_dt.now.return_value.isoformat.return_value = "2026-04-01T00:00:00+00:00"
        
        res1 = exporter.export_all()
        with open(res1["json"], "r") as f:
            d1 = f.read()
            
        res2 = exporter.export_all()
        with open(res2["json"], "r") as f:
            d2 = f.read()
            
        assert d1 == d2
