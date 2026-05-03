"""Post-day analysis for event-intelligence shadow run.

Run after market close to measure pipeline quality:
    .venv/bin/python scripts/shadow_day_analysis.py              # today
    .venv/bin/python scripts/shadow_day_analysis.py 2026-05-05   # specific date

Outputs:
    - Filing collection stats
    - Classification distribution
    - Parse quality metrics
    - Surprise computation coverage
    - HYDRA eligibility count
    - Individual signal details for HYDRA-eligible
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path("data")


def load_json(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def analyze(date_str: str) -> None:
    print(f"\n{'='*60}")
    print(f"  EVENT INTELLIGENCE — SHADOW DAY ANALYSIS: {date_str}")
    print(f"{'='*60}\n")

    # ── 1. Raw filings collected ──────────────────────────────────
    official = load_json(DATA_DIR / f"official_events_{date_str}.json")
    print(f"[1] RAW FILINGS COLLECTED: {len(official)}")
    if official:
        cats = {}
        for e in official:
            cat = e.get("category") or "(empty)"
            cats[cat] = cats.get(cat, 0) + 1
        top_cats = sorted(cats.items(), key=lambda x: -x[1])[:5]
        for cat, n in top_cats:
            print(f"    {cat}: {n}")
    print()

    # ── 2. Classified signals ─────────────────────────────────────
    signals = load_json(DATA_DIR / f"earnings_signals_{date_str}.json")
    print(f"[2] CLASSIFIED SIGNALS: {len(signals)}")

    event_types: Dict[str, int] = {}
    subtypes: Dict[str, int] = {}
    for s in signals:
        et = s.get("event_type", "UNKNOWN")
        st = s.get("event_subtype", "UNKNOWN")
        event_types[et] = event_types.get(et, 0) + 1
        subtypes[st] = subtypes.get(st, 0) + 1

    print(f"    Event types:")
    for t, n in sorted(event_types.items(), key=lambda x: -x[1]):
        print(f"      {t}: {n}")
    print(f"    Top subtypes:")
    for st, n in sorted(subtypes.items(), key=lambda x: -x[1])[:8]:
        print(f"      {st}: {n}")
    print()

    # ── 3. Parse quality ──────────────────────────────────────────
    parse_dist: Dict[str, int] = {}
    for s in signals:
        ps = s.get("parse_status", "UNKNOWN")
        parse_dist[ps] = parse_dist.get(ps, 0) + 1

    total_signals = len(signals) or 1
    structured = sum(v for k, v in parse_dist.items() if k in ("PDF_TABLE", "PDF_REGEX", "SUCCESS", "PARTIAL"))
    heuristic = parse_dist.get("HEURISTIC", 0)

    print(f"[3] PARSE QUALITY:")
    for ps, n in sorted(parse_dist.items(), key=lambda x: -x[1]):
        print(f"    {ps}: {n} ({n/total_signals*100:.0f}%)")
    print(f"    Structured extraction rate: {structured}/{len(signals)} ({structured/total_signals*100:.0f}%)")
    print()

    # ── 4. Surprise computation ───────────────────────────────────
    financial_results = [s for s in signals if s.get("event_type") == "FINANCIAL_RESULTS"]
    has_surprise = [
        s for s in financial_results
        if (s.get("earnings") or {}).get("surprise_pct") is not None
    ]
    print(f"[4] SURPRISE COMPUTATION:")
    print(f"    Financial results signals: {len(financial_results)}")
    print(f"    With surprise_pct: {len(has_surprise)}/{len(financial_results)}")
    if financial_results:
        print(f"    Surprise coverage: {len(has_surprise)/len(financial_results)*100:.0f}%")
    print()

    # ── 5. HYDRA eligibility ──────────────────────────────────────
    hydra_eligible = [
        s for s in signals
        if s.get("material")
        and s.get("urgency", 0) >= 7.0
        and s.get("source_confidence", 0) >= 0.7
    ]
    print(f"[5] HYDRA ELIGIBILITY:")
    print(f"    Material signals: {sum(1 for s in signals if s.get('material'))}")
    print(f"    Urgency >= 7.0: {sum(1 for s in signals if s.get('urgency', 0) >= 7.0)}")
    print(f"    HYDRA eligible (material + urgency≥7 + conf≥0.7): {len(hydra_eligible)}")
    print()

    # ── 6. HYDRA-eligible signal details ──────────────────────────
    if hydra_eligible:
        print(f"[6] HYDRA-ELIGIBLE SIGNALS:")
        print(f"    {'Symbol':<12} {'Subtype':<25} {'Dir':<6} {'Urg':>4} {'Surprise':>10} {'Parse':<12}")
        print(f"    {'-'*12} {'-'*25} {'-'*6} {'-'*4} {'-'*10} {'-'*12}")
        for s in sorted(hydra_eligible, key=lambda x: -x.get("urgency", 0)):
            sp = (s.get("earnings") or {}).get("surprise_pct")
            sp_str = f"{sp:+.1f}%" if sp is not None else "N/A"
            print(
                f"    {s['symbol']:<12} {s.get('event_subtype',''):<25} "
                f"{s.get('direction_hint',''):<6} {s.get('urgency',0):>4.1f} "
                f"{sp_str:>10} {s.get('parse_status',''):<12}"
            )
    else:
        print(f"[6] HYDRA-ELIGIBLE SIGNALS: None")
    print()

    # ── 6b. Priced-in assessment ─────────────────────────────────
    if hydra_eligible:
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from src.event_intelligence.priced_in import assess_priced_in
            discounted = 0
            conflict_count = 0
            for s in hydra_eligible:
                a = assess_priced_in(s, data_dir=str(DATA_DIR), check_preopen=False)
                if a.priced_in_discount > 0:
                    discounted += 1
                if a.conflict_signal:
                    conflict_count += 1
            print(f"    ── Priced-in assessment:")
            print(f"       Signals with discount applied: {discounted}/{len(hydra_eligible)}")
            if conflict_count:
                print(f"       Conflict signals (gap opposes): {conflict_count}")
        except Exception as e:
            print(f"    ── Priced-in assessment: unavailable ({e})")
    print()

    # ── 7. Shadow router decisions ───────────────────��────────────
    shadow = load_json(DATA_DIR / f"event_intel_shadow_router_{date_str}.json")
    print(f"[7] SHADOW ROUTER DECISIONS: {len(shadow)}")
    if shadow:
        routes = {}
        for d in shadow:
            dec = d.get("decision", {})
            route = dec.get("route", "UNKNOWN") if isinstance(dec, dict) else "UNKNOWN"
            routes[route] = routes.get(route, 0) + 1
        for r, n in sorted(routes.items(), key=lambda x: -x[1]):
            print(f"    {r}: {n}")
    print()

    # ── 8. Service health ─────────────────────────────────────────
    heartbeat_path = DATA_DIR / "event_intel_heartbeat.json"
    print(f"[8] SERVICE HEALTH:")
    if heartbeat_path.exists():
        try:
            with open(heartbeat_path) as f:
                hb = json.load(f)
            print(f"    Last heartbeat: {hb.get('ts_ist', 'unknown')}")
            # Check staleness
            ts_str = hb.get("ts_utc", "")
            if ts_str:
                last = datetime.fromisoformat(ts_str)
                age_min = (datetime.now(IST) - last).total_seconds() / 60
                status = "OK" if age_min < 5 else "STALE" if age_min < 60 else "DEAD"
                print(f"    Age: {age_min:.0f} min → {status}")
        except Exception as e:
            print(f"    Heartbeat read error: {e}")
    else:
        print(f"    No heartbeat file found!")
    print()

    # ── 9. Summary verdict ────────────────────────────────────────
    print(f"{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    issues = []
    if len(official) == 0:
        issues.append("NO FILINGS COLLECTED — NSE poller may be down")
    if structured == 0 and len(signals) > 0:
        issues.append("0% STRUCTURED PARSE — PDF fetch likely failing")
    if len(financial_results) == 0 and len(signals) > 10:
        issues.append("NO FINANCIAL_RESULTS — classifier not recognizing results filings")
    if len(has_surprise) == 0 and len(financial_results) > 0:
        issues.append("NO SURPRISE COMPUTED — prior_quarters DB or unit detection issue")
    if len(hydra_eligible) == 0 and len(signals) > 20:
        issues.append("ZERO HYDRA ELIGIBLE — pipeline producing no actionable signals")

    if issues:
        print("  ISSUES:")
        for i in issues:
            print(f"    ⚠ {i}")
    else:
        print("  ✓ Pipeline producing actionable signals")
        print(f"  ✓ {len(hydra_eligible)} HYDRA-eligible signals")
        print(f"  ✓ {structured/total_signals*100:.0f}% structured parse rate")
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = datetime.now(IST).strftime("%Y-%m-%d")
    analyze(date_str)
