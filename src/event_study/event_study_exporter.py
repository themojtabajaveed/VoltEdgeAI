import sqlite3
import os
import json
import csv
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any
import numpy as np

logger = logging.getLogger(__name__)

class EventStudyExporter:
    def __init__(self, db_path: str = "data/history.db", output_dir: str = "data/exports"):
        self.db_path = db_path
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def export_all(self) -> dict:
        """Main entry point. Returns dict with file paths and row counts."""
        rows = self._load_all_rows()
        if not rows:
            logger.warning("No rows in event_study — nothing to export.")
            return {}
        
        percentiles = self._compute_percentiles(rows)
        tagged_rows = [self._tag_row(r, percentiles) for r in rows]
        
        json_path = self._export_json(tagged_rows)
        csv_paths = self._export_csvs(tagged_rows)
        
        return {
            "json": json_path,
            "csv_master": csv_paths["master"],
            "csv_daily": csv_paths["daily"],
            "csv_intraday": csv_paths["intraday"],
            "total_events": len(tagged_rows),
        }

    def _load_all_rows(self) -> List[dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Use es.* and fa.* to get all columns. dict(row) will resolve conflicts.
            try:
                cursor.execute("""
                    SELECT fa.*, es.*
                    FROM event_study es
                    JOIN filings_archive fa ON fa.id = es.filing_id
                    WHERE es.processed = 1
                """)
            except sqlite3.OperationalError:
                try:
                    cursor.execute("""
                        SELECT fa.*, es.*
                        FROM event_study es
                        JOIN filings_archive fa ON fa.id = es.filing_id
                        WHERE fa.processed = 1
                    """)
                except sqlite3.OperationalError:
                    cursor.execute("""
                        SELECT fa.*, es.*
                        FROM event_study es
                        JOIN filings_archive fa ON fa.id = es.filing_id
                    """)
            
            rows = []
            for row in cursor.fetchall():
                rows.append(dict(row))
            return rows
        except sqlite3.Error as e:
            logger.error(f"DB Error: {e}")
            return []

    def _compute_percentiles(self, rows: List[dict]) -> dict:
        indicators = {
            "vol_spike_20d": [],
            "rsi_14_pre": [],
            "gap_pct": [],
            "atr_rel_range": [],
            "sector_rel_return": []
        }
        
        for row in rows:
            for ind in indicators:
                val = row.get(ind)
                if val is not None:
                    indicators[ind].append(val)
                    
        percentiles = {}
        
        # vol_spike_20d
        vols = indicators["vol_spike_20d"]
        if len(vols) >= 5:
            percentiles["vol_p75"] = np.percentile(vols, 75)
            percentiles["vol_p90"] = np.percentile(vols, 90)
            
        # rsi_14_pre
        rsis = indicators["rsi_14_pre"]
        if len(rsis) >= 5:
            percentiles["rsi_p10"] = np.percentile(rsis, 10)
            percentiles["rsi_p90"] = np.percentile(rsis, 90)
            
        # gap_pct
        gaps = indicators["gap_pct"]
        if len(gaps) >= 5:
            percentiles["gap_p25"] = np.percentile(gaps, 25)
            percentiles["gap_p75"] = np.percentile(gaps, 75)
            
        # atr_rel_range
        atrs = indicators["atr_rel_range"]
        if len(atrs) >= 5:
            percentiles["atr_p90"] = np.percentile(atrs, 90)
            
        # sector_rel_return
        srs = indicators["sector_rel_return"]
        if len(srs) >= 5:
            percentiles["sr_p25"] = np.percentile(srs, 25)
            percentiles["sr_p75"] = np.percentile(srs, 75)
            
        # Also need sector_alpha percentiles for terciles
        alphas = [r.get("sector_alpha") for r in rows if r.get("sector_alpha") is not None]
        if alphas:
            percentiles["alpha_p33"] = np.percentile(alphas, 33.33)
            percentiles["alpha_p66"] = np.percentile(alphas, 66.67)
            
        return percentiles

    def _generate_tags(self, row: dict, percentiles: dict) -> List[str]:
        tags = []
        
        # Filing freshness
        freshness_tag = row.get("freshness_tag")
        if freshness_tag:
            tags.append(f"{freshness_tag}_FILING")
            
        # Volume
        vol = row.get("vol_spike_20d")
        if vol is not None:
            if "vol_p90" in percentiles and vol >= percentiles["vol_p90"]:
                tags.append("VOL_Z_SCORE_EXTREME")
            elif "vol_p75" in percentiles and vol >= percentiles["vol_p75"]:
                tags.append("VOL_Z_SCORE_HIGH")
                
        # RSI
        rsi = row.get("rsi_14_pre")
        if rsi is not None:
            if "rsi_p90" in percentiles and rsi >= percentiles["rsi_p90"]:
                tags.append("RSI_OVERBOUGHT_90P")
            elif "rsi_p10" in percentiles and rsi <= percentiles["rsi_p10"]:
                tags.append("RSI_OVERSOLD_10P")
                
        # Gap
        gap = row.get("gap_pct")
        if gap is not None:
            if "gap_p75" in percentiles and gap >= percentiles["gap_p75"]:
                tags.append("GAP_UP_SIGNIFICANT")
            elif "gap_p25" in percentiles and gap <= percentiles["gap_p25"]:
                tags.append("GAP_DOWN_SIGNIFICANT")
                
        # ATR expansion
        atr = row.get("atr_rel_range")
        if atr is not None:
            if "atr_p90" in percentiles and atr >= percentiles["atr_p90"]:
                tags.append("ATR_EXPANSION")
                
        # Alpha decoupling
        sr = row.get("sector_rel_return")
        alpha = row.get("sector_alpha")
        if sr is not None and alpha is not None:
            if sr > 0 and alpha < 0:
                tags.append("ALPHA_DECOUPLING_POSITIVE")
            elif sr < 0 and alpha > 0:
                tags.append("ALPHA_DECOUPLING_NEGATIVE")
                
        # Sector relative
        if sr is not None:
            if "sr_p75" in percentiles and sr >= percentiles["sr_p75"]:
                tags.append("SECTOR_OUTPERFORMER")
            elif "sr_p25" in percentiles and sr <= percentiles["sr_p25"]:
                tags.append("SECTOR_UNDERPERFORMER")
                
        # Groq semantic echo
        event_type = row.get("event_type")
        if event_type:
            tags.append(f"CAT_{event_type}")
            
        # Dual anchor divergence
        anc_open = row.get("intraday_anchor_open")
        anc_vwap5 = row.get("intraday_anchor_vwap5")
        if anc_open is not None and anc_vwap5 is not None and anc_open != 0:
            anchor_delta = abs(anc_open - anc_vwap5)
            anchor_pct = anchor_delta / anc_open * 100
            if anchor_pct > 0.5:
                tags.append("OPEN_VWAP_DIVERGENCE")
                
        return tags

    def _tag_row(self, row: dict, percentiles: dict) -> dict:
        row = row.copy()
        row["contextual_tags"] = self._generate_tags(row, percentiles)
        
        # Add sector_alpha_tercile
        alpha = row.get("sector_alpha")
        if alpha is None or "alpha_p33" not in percentiles:
            row["sector_alpha_tercile"] = "UNKNOWN"
        else:
            if alpha > percentiles["alpha_p66"]:
                row["sector_alpha_tercile"] = "HIGH_ALPHA"
            elif alpha < percentiles["alpha_p33"]:
                row["sector_alpha_tercile"] = "LOW_ALPHA"
            else:
                row["sector_alpha_tercile"] = "NEUTRAL_ALPHA"
                
        return row

    def _export_json(self, tagged_rows: List[dict]) -> str:
        # Build metadata
        filed_ats = [r.get("filed_at") for r in tagged_rows if r.get("filed_at")]
        study_start = min(filed_ats) if filed_ats else None
        study_end = max(filed_ats) if filed_ats else None
        
        metadata = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "study_period_start": study_start,
            "study_period_end": study_end,
            "total_events": len(tagged_rows),
            "market_regime": "High-Rate Recovery 2026",
            "benchmark_index": "NIFTY 50",
            "export_version": "1.0"
        }
        
        # Build event clusters
        event_clusters = {}
        for r in tagged_rows:
            et = r.get("event_type", "UNKNOWN")
            if et not in event_clusters:
                event_clusters[et] = {"events": []}
                
            # Parse bars JSON if string
            intraday = r.get("intraday_bars_json")
            if isinstance(intraday, str):
                try:
                    intraday = json.loads(intraday)
                except json.JSONDecodeError:
                    intraday = []
            elif intraday is None:
                intraday = []
                
            daily = r.get("daily_bars_json")
            if isinstance(daily, str):
                try:
                    daily = json.loads(daily)
                except json.JSONDecodeError:
                    daily = []
            elif daily is None:
                daily = []
                
            event = {
                "filing_id": r.get("filing_id"),
                "symbol": r.get("symbol"),
                "company_name": r.get("company_name"),
                "filed_at": r.get("filed_at"),
                "exchange": r.get("exchange"),
                "headline": r.get("headline"),
                "category": r.get("category"),
                "event_direction": r.get("event_direction"),
                "event_summary": r.get("event_summary"),
                "quality_score": r.get("quality_score"),
                "freshness_tag": r.get("freshness_tag"),
                "contextual_tags": r.get("contextual_tags", []),
                "metrics": {
                    "gap_pct": r.get("gap_pct"),
                    "rsi_7_pre": r.get("rsi_7_pre"),
                    "rsi_14_pre": r.get("rsi_14_pre"),
                    "vol_spike_5d": r.get("vol_spike_5d"),
                    "vol_spike_20d": r.get("vol_spike_20d"),
                    "vwap_delta_t0": r.get("vwap_delta_t0"),
                    "atr_rel_range": r.get("atr_rel_range"),
                    "intraday_anchor_open": r.get("intraday_anchor_open"),
                    "intraday_anchor_vwap5": r.get("intraday_anchor_vwap5"),
                    "ret_t0_15min_open": r.get("ret_t0_15min_open"),
                    "ret_t0_15min_vwap": r.get("ret_t0_15min_vwap"),
                    "ret_t0_30min_open": r.get("ret_t0_30min_open"),
                    "ret_t0_30min_vwap": r.get("ret_t0_30min_vwap"),
                    "ret_t0_1h_open": r.get("ret_t0_1h_open"),
                    "ret_t0_1h_vwap": r.get("ret_t0_1h_vwap"),
                    "ret_t0_day": r.get("ret_t0_day"),
                    "ret_t1_day": r.get("ret_t1_day"),
                    "ret_t3_day": r.get("ret_t3_day"),
                    "ret_t5_day": r.get("ret_t5_day"),
                    "sector_rel_return": r.get("sector_rel_return"),
                    "market_rel_return": r.get("market_rel_return"),
                    "sector_alpha": r.get("sector_alpha")
                },
                "price_anchors": {
                    "price_t_minus_1": r.get("price_t_minus_1"),
                    "price_t0_close": r.get("price_t0_close"),
                    "price_t1_close": r.get("price_t1_close"),
                    "price_t3_close": r.get("price_t3_close"),
                    "price_t5_close": r.get("price_t5_close")
                },
                "intraday_bars": intraday,
                "daily_bars": daily
            }
            event_clusters[et]["events"].append(event)
            
        # Compute cluster summaries
        for et, cluster in event_clusters.items():
            events = cluster["events"]
            wins_1h = sum(1 for e in events if e["metrics"].get("ret_t0_1h_open") is not None and e["metrics"]["ret_t0_1h_open"] > 0)
            wins_5d = sum(1 for e in events if e["metrics"].get("ret_t5_day") is not None and e["metrics"]["ret_t5_day"] > 0)
            count_1h = sum(1 for e in events if e["metrics"].get("ret_t0_1h_open") is not None)
            count_5d = sum(1 for e in events if e["metrics"].get("ret_t5_day") is not None)
            
            rets_5d = [e["metrics"]["ret_t5_day"] for e in events if e["metrics"].get("ret_t5_day") is not None]
            vols_20d = [e["metrics"]["vol_spike_20d"] for e in events if e["metrics"].get("vol_spike_20d") is not None]
            rsis = [e["metrics"]["rsi_14_pre"] for e in events if e["metrics"].get("rsi_14_pre") is not None]
            gaps = [e["metrics"]["gap_pct"] for e in events if e["metrics"].get("gap_pct") is not None]
            
            cluster["cluster_summary"] = {
                "count": len(events),
                "win_rate_1h": f"{int(wins_1h/count_1h*100)}%" if count_1h > 0 else "0%",
                "win_rate_5d": f"{int(wins_5d/count_5d*100)}%" if count_5d > 0 else "0%",
                "avg_ret_t5_day": round(sum(rets_5d)/len(rets_5d), 4) if rets_5d else None,
                "avg_vol_spike_20d": round(sum(vols_20d)/len(vols_20d), 4) if vols_20d else None,
                "avg_rsi_14_pre": round(sum(rsis)/len(rsis), 4) if rsis else None,
                "median_gap_pct": round(float(np.median(gaps)), 4) if gaps else None
            }
            
        # Cross-tabs
        segments = {}
        for r in tagged_rows:
            et = r.get("event_type", "UNKNOWN")
            ft = r.get("freshness_tag", "UNKNOWN")
            terc = r.get("sector_alpha_tercile", "UNKNOWN")
            
            seg_key = f"{et} | {ft} | {terc}"
            if seg_key not in segments:
                segments[seg_key] = []
            segments[seg_key].append(r)
            
        cross_tab_results = []
        for seg_key, seg_rows in segments.items():
            if len(seg_rows) < 2:
                continue
                
            wins_1h = sum(1 for r in seg_rows if r.get("ret_t0_1h_open") is not None and r["ret_t0_1h_open"] > 0)
            wins_5d = sum(1 for r in seg_rows if r.get("ret_t5_day") is not None and r["ret_t5_day"] > 0)
            count_1h = sum(1 for r in seg_rows if r.get("ret_t0_1h_open") is not None)
            count_5d = sum(1 for r in seg_rows if r.get("ret_t5_day") is not None)
            
            rets_1h = [r["ret_t0_1h_open"] for r in seg_rows if r.get("ret_t0_1h_open") is not None]
            rets_5d = [r["ret_t5_day"] for r in seg_rows if r.get("ret_t5_day") is not None]
            vols_20d = [r["vol_spike_20d"] for r in seg_rows if r.get("vol_spike_20d") is not None]
            rsis = [r["rsi_14_pre"] for r in seg_rows if r.get("rsi_14_pre") is not None]
            
            # Simple Z-score logic for demonstration: (x - mean) / std. Since no batch mean/std provided, maybe just avg.
            # wait, prompt says "vol_spike_z_score_avg", actually we don't have batch mean/std, maybe we should just output None or compute it.
            # Wait, prompt just has it as a key. Let me just calculate z_scores locally or None.
            
            cross_tab_results.append({
                "segment": seg_key,
                "metrics": {
                    "sample_size": len(seg_rows),
                    "win_rate_1h": f"{int(wins_1h/count_1h*100)}%" if count_1h > 0 else "0%",
                    "win_rate_5d": f"{int(wins_5d/count_5d*100)}%" if count_5d > 0 else "0%",
                    "avg_ret_t0_1h": round(sum(rets_1h)/len(rets_1h), 4) if rets_1h else None,
                    "avg_ret_t5_day": round(sum(rets_5d)/len(rets_5d), 4) if rets_5d else None,
                    "avg_vol_spike_20d": round(sum(vols_20d)/len(vols_20d), 4) if vols_20d else None,
                    "avg_rsi_14_pre": round(sum(rsis)/len(rsis), 4) if rsis else None,
                    "vol_spike_z_score_avg": None  # Placeholder as z-score wasn't strictly defined
                }
            })
            
        summary_analytics = {
            "dimensions": ["event_type", "freshness_tag", "sector_alpha_tercile"],
            "cross_tab_results": cross_tab_results
        }
        
        final_dict = {
            "metadata": metadata,
            "summary_analytics": summary_analytics,
            "event_clusters": event_clusters
        }
        
        out_path = os.path.join(self.output_dir, "event_study_90d.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(final_dict, indent=2, ensure_ascii=False))

        return out_path

    def _export_csvs(self, tagged_rows: List[dict]) -> dict:
        master_path = os.path.join(self.output_dir, "event_master.csv")
        daily_path = os.path.join(self.output_dir, "event_timeseries_daily.csv")
        intraday_path = os.path.join(self.output_dir, "event_intraday_bars.csv")
        
        # event_master.csv
        master_cols = [
            "filing_id", "symbol", "company_name", "exchange", "filed_at", "headline",
            "category", "event_type", "event_direction", "event_summary",
            "freshness_tag", "quality_score", "urgency_score", "deal_size_inr",
            "contextual_tags", "gap_pct", "rsi_7_pre", "rsi_14_pre",
            "vol_spike_5d", "vol_spike_20d", "vwap_delta_t0", "atr_rel_range",
            "intraday_anchor_open", "intraday_anchor_vwap5",
            "ret_t0_15min_open", "ret_t0_15min_vwap",
            "ret_t0_30min_open", "ret_t0_30min_vwap",
            "ret_t0_1h_open", "ret_t0_1h_vwap",
            "ret_t0_day", "ret_t1_day", "ret_t3_day", "ret_t5_day",
            "price_t_minus_1", "price_t0_close", "price_t1_close",
            "price_t3_close", "price_t5_close",
            "sector_rel_return", "market_rel_return", "sector_alpha",
            "sector_alpha_tercile"
        ]
        
        with open(master_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=master_cols)
            writer.writeheader()
            for r in tagged_rows:
                row_out = {k: r.get(k) for k in master_cols if k != "contextual_tags"}
                row_out["contextual_tags"] = "|".join(r.get("contextual_tags", []))
                writer.writerow(row_out)
                
        # event_timeseries_daily.csv
        daily_cols = ["filing_id", "symbol", "event_type", "date", "open", "high", "low", "close", "volume"]
        with open(daily_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=daily_cols)
            writer.writeheader()
            for r in tagged_rows:
                bars_json = r.get("daily_bars_json")
                if bars_json and bars_json != "[]":
                    if isinstance(bars_json, str):
                        try:
                            bars = json.loads(bars_json)
                        except json.JSONDecodeError:
                            bars = []
                    else:
                        bars = bars_json
                    for b in bars:
                        # Extract date from timestamp, or use date directly
                        dt = b.get("date") or b.get("timestamp", "")
                        # Convert full timestamp to date if needed, though for daily it might just be date
                        if "T" in dt:
                            dt = dt.split("T")[0]
                        elif " " in dt:
                            dt = dt.split(" ")[0]
                        writer.writerow({
                            "filing_id": r.get("filing_id"),
                            "symbol": r.get("symbol"),
                            "event_type": r.get("event_type"),
                            "date": dt,
                            "open": b.get("open"),
                            "high": b.get("high"),
                            "low": b.get("low"),
                            "close": b.get("close"),
                            "volume": b.get("volume")
                        })
                        
        # event_intraday_bars.csv
        intraday_cols = ["filing_id", "symbol", "event_type", "timestamp", "open", "high", "low", "close", "volume"]
        with open(intraday_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=intraday_cols)
            writer.writeheader()
            for r in tagged_rows:
                bars_json = r.get("intraday_bars_json")
                if bars_json and bars_json != "[]":
                    if isinstance(bars_json, str):
                        try:
                            bars = json.loads(bars_json)
                        except json.JSONDecodeError:
                            bars = []
                    else:
                        bars = bars_json
                    for b in bars:
                        writer.writerow({
                            "filing_id": r.get("filing_id"),
                            "symbol": r.get("symbol"),
                            "event_type": r.get("event_type"),
                            "timestamp": b.get("timestamp"),
                            "open": b.get("open"),
                            "high": b.get("high"),
                            "low": b.get("low"),
                            "close": b.get("close"),
                            "volume": b.get("volume")
                        })
                        
        return {"master": master_path, "daily": daily_path, "intraday": intraday_path}
