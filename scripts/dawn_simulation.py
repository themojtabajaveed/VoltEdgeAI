#!/usr/bin/env python3
"""
dawn_simulation.py — Simulate what DAWN would have done on Apr 9 & Apr 10, 2025.
Uses actual morning brief data and realistic price trajectories.
Produces CSV files in logs/dawn_dryrun/.
"""
import os
import sys
import csv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.strategies.dawn import (
    DawnStrategy, DawnCandidate, DawnSignal, DawnDailyMetrics,
    MAX_SIGNALS_PER_DAY, HARD_SL_PCT, LONG_THRESHOLD, SHORT_THRESHOLD,
    CSV_COLUMNS, CSV_DIR,
)

# ═══════════════════════════════════════════════════════════════════════════
# APR 9, 2025 — NORMAL OPERATION
# ═══════════════════════════════════════════════════════════════════════════

def simulate_apr_9():
    """
    Apr 9: Full operation. Morning brief available (sector plays).
    HYDRA found: GLENMARK (FDA), AWHCL (FDI), INTLCONV, PREMIERENE.
    Brief hot stocks: INDIGO, OIL, HPCL, ASIANPAINT, ADITYABSL.
    """
    dawn = DawnStrategy()
    dawn._today = __import__("datetime").date(2025, 4, 9)
    date_str = "2025-04-09"

    # ── Source B candidates (from EventScanner + Brave) ──
    source_b = [
        DawnCandidate(
            symbol="GLENMARK", direction="BUY",
            catalyst="FDA approval for generic drug application",
            catalyst_type="FDA_APPROVAL", source="DAWN_SCAN",
            urgency=9.0, catalyst_strength="HIGH", freshness_hours=3.0,
            avg_daily_turnover=45_000_000,
        ),
        DawnCandidate(
            symbol="AWHCL", direction="BUY",
            catalyst="750M JPY FDI from Japanese partner",
            catalyst_type="FDI", source="DAWN_SCAN",
            urgency=8.0, catalyst_strength="HIGH", freshness_hours=4.0,
            avg_daily_turnover=3_000_000,
        ),
        DawnCandidate(
            symbol="ITDC", direction="BUY",
            catalyst="Government contract win for hotel chain expansion",
            catalyst_type="CONTRACT_WIN", source="DAWN_SCAN",
            urgency=7.0, catalyst_strength="MEDIUM", freshness_hours=6.0,
            avg_daily_turnover=4_500_000,
        ),
        DawnCandidate(
            symbol="INTLCONV", direction="BUY",
            catalyst="Corporate restructuring announcement",
            catalyst_type="MERGER", source="DAWN_SCAN",
            urgency=7.5, catalyst_strength="MEDIUM", freshness_hours=4.0,
            avg_daily_turnover=1_500_000,  # Below threshold → liquidity warning
        ),
        DawnCandidate(
            symbol="PREMIERENE", direction="BUY",
            catalyst="Q4 earnings beat estimates by 40%",
            catalyst_type="EARNINGS_BEAT", source="DAWN_SCAN",
            urgency=6.0, catalyst_strength="MEDIUM", freshness_hours=8.0,
            avg_daily_turnover=800_000,  # Below threshold → liquidity warning
        ),
    ]

    # ── Source A candidates (from morning brief) ──
    source_a = [
        DawnCandidate(
            symbol="INDIGO", direction="BUY",
            catalyst="Aviation sector momentum + fare hike approval",
            catalyst_type="MORNING_BRIEF_PICK", source="MORNING_BRIEF",
            urgency=7.0, catalyst_strength="MEDIUM", freshness_hours=4.0,
            avg_daily_turnover=120_000_000,
        ),
        DawnCandidate(
            symbol="OIL", direction="BUY",
            catalyst="Crude oil macro play + government policy support",
            catalyst_type="MORNING_BRIEF_PICK", source="MORNING_BRIEF",
            urgency=7.0, catalyst_strength="MEDIUM", freshness_hours=4.0,
            avg_daily_turnover=15_000_000,
        ),
    ]

    # Score all candidates
    dawn._candidates = source_b

    # Manually merge (simulating merge_brief_and_generate logic)
    all_candidates = list(source_b) + list(source_a)

    print("=" * 80)
    print(f"DAWN SIMULATION — {date_str} (Normal Operation)")
    print("=" * 80)
    print()
    print("Source B (DAWN scan): GLENMARK, AWHCL, ITDC, INTLCONV, PREMIERENE")
    print("Source A (Brief):     INDIGO, OIL")
    print()

    # Score and qualify
    scored = []
    for c in all_candidates:
        passes, reason = dawn._qualifies(c)
        score = dawn._score_candidate(c) if passes else 0
        threshold = SHORT_THRESHOLD if c.direction == "SHORT" else LONG_THRESHOLD
        status = "✅ QUALIFIES" if passes and score >= threshold else f"❌ {reason}" if not passes else f"❌ Score {score:.0f} < {threshold}"
        print(f"  {c.symbol:12s} | urg={c.urgency:.0f} | C={c._score_catalyst if hasattr(c, '_score_catalyst') else 0:.0f} F={c._score_freshness if hasattr(c, '_score_freshness') else 0:.0f} L={c._score_liquidity if hasattr(c, '_score_liquidity') else 0:.0f} T={c._score_technical if hasattr(c, '_score_technical') else 0:.0f} M={c._score_context if hasattr(c, '_score_context') else 0:.0f} = {score:.0f} | {status}")
        if passes and score >= threshold:
            liq_warning = c.avg_daily_turnover > 0 and c.avg_daily_turnover < 2_000_000
            sig = DawnSignal(
                symbol=c.symbol, direction=c.direction, catalyst=c.catalyst,
                catalyst_type=c.catalyst_type, source=c.source, dawn_score=score,
                liquidity_warning=liq_warning,
                score_catalyst=getattr(c, '_score_catalyst', 0),
                score_freshness=getattr(c, '_score_freshness', 0),
                score_liquidity=getattr(c, '_score_liquidity', 0),
                score_technical=getattr(c, '_score_technical', 0),
                score_context=getattr(c, '_score_context', 0),
            )
            scored.append(sig)

    scored.sort(key=lambda s: s.dawn_score, reverse=True)
    signals = scored[:MAX_SIGNALS_PER_DAY]

    print(f"\nQualified: {len(scored)} → Top {MAX_SIGNALS_PER_DAY}: {[s.symbol for s in signals]}")
    print()

    # ── Simulate entries at 09:15 ──
    # Realistic open prices for Apr 9
    open_prices = {
        "GLENMARK": 1520.00,
        "AWHCL": 480.00,
        "ITDC": 390.00,
        "INTLCONV": 215.00,
        "INDIGO": 4450.00,
        "OIL": 310.00,
        "PREMIERENE": 145.00,
    }

    for sig in signals:
        price = open_prices.get(sig.symbol, 100.0)
        sig.entry_price = price
        sig.current_price = price
        sig.quantity = max(1, int(5000 / price))
        if sig.direction == "BUY":
            sig.sl_price = price * (1 - HARD_SL_PCT)
        else:
            sig.sl_price = price * (1 + HARD_SL_PCT)
        sig.sl_type = "INITIAL"
        sig.status = "ACTIVE"

    print("09:15 — Virtual entries recorded:")
    for sig in signals:
        print(f"  {sig.symbol:12s} entry={sig.entry_price:.2f} SL={sig.sl_price:.2f} qty={sig.quantity}")
    print()

    # ── Simulate intraday price trajectory ──
    # Price trajectories: (time_label, price_multiplier relative to open)
    trajectories = {
        "GLENMARK": [
            ("09:30", 1.01), ("10:00", 1.025), ("10:30", 1.04), ("11:00", 1.06),
            ("11:30", 1.055), ("12:00", 1.065), ("12:30", 1.07), ("13:00", 1.075),
            ("13:30", 1.08), ("14:00", 1.082), ("14:30", 1.075), ("15:00", 1.07), ("15:20", 1.07),
        ],
        "AWHCL": [
            ("09:30", 1.02), ("10:00", 1.04), ("10:30", 1.06), ("11:00", 1.085),
            ("11:30", 1.10), ("12:00", 1.11), ("12:30", 1.12), ("13:00", 1.10),
            ("13:30", 1.105), ("14:00", 1.10), ("14:30", 1.095), ("15:00", 1.10), ("15:20", 1.10),
        ],
        "ITDC": [
            ("09:30", 1.005), ("10:00", 1.015), ("10:30", 1.02), ("11:00", 1.025),
            ("11:30", 1.03), ("12:00", 1.032), ("12:30", 1.028), ("13:00", 1.025),
            ("13:30", 1.02), ("14:00", 1.018), ("14:30", 1.02), ("15:00", 1.02), ("15:20", 1.02),
        ],
        "INTLCONV": [
            # THIS IS THE LOSING TRADE — opens, drifts down, hits 2% SL
            ("09:30", 1.005), ("10:00", 0.995), ("10:30", 0.988), ("11:00", 0.982),
            ("11:30", 0.980),  # SL hit at -2%
        ],
        "INDIGO": [
            ("09:30", 1.003), ("10:00", 1.008), ("10:30", 1.012), ("11:00", 1.01),
            ("11:30", 1.008), ("12:00", 1.005), ("12:30", 1.006), ("13:00", 1.008),
            ("13:30", 1.007), ("14:00", 1.006), ("14:30", 1.008), ("15:00", 1.008), ("15:20", 1.008),
        ],
        "OIL": [
            ("09:30", 0.998), ("10:00", 0.995), ("10:30", 1.001), ("11:00", 1.003),
            ("11:30", 1.002), ("12:00", 1.004), ("12:30", 1.005), ("13:00", 1.003),
            ("13:30", 1.002), ("14:00", 1.004), ("14:30", 1.003), ("15:00", 1.003), ("15:20", 1.003),
        ],
    }

    print("Intraday simulation:")
    for sig in signals:
        trajectory = trajectories.get(sig.symbol, [])
        stopped = False
        for time_label, multiplier in trajectory:
            sig.current_price = sig.entry_price * multiplier
            move_pct = (sig.current_price - sig.entry_price) / sig.entry_price * 100
            sig.max_favorable = max(sig.max_favorable, move_pct)
            sig.max_adverse = min(sig.max_adverse, move_pct)

            # Update trailing SL
            dawn._update_stop_loss(sig)

            # Check stop hit
            if sig.direction == "BUY" and sig.current_price <= sig.sl_price:
                sig.status = "STOPPED"
                sig.exit_price = sig.sl_price
                sig.exit_time = time_label
                sig.exit_reason = f"SL_HIT ({sig.sl_type})"
                dawn._compute_pnl(sig)
                print(f"  {sig.symbol:12s} [{time_label}] STOPPED at {sig.exit_price:.2f} "
                      f"PnL={sig.pnl_pct:+.2f}% ₹{sig.pnl_rupees:+.0f}")
                stopped = True
                break

        if not stopped:
            # Time exit at 15:20
            sig.exit_price = sig.current_price
            sig.exit_time = "15:20"
            sig.exit_reason = "TIME_EXIT_15:20"
            sig.status = "CLOSED"
            dawn._compute_pnl(sig)
            print(f"  {sig.symbol:12s} [15:20] CLOSED at {sig.exit_price:.2f} "
                  f"PnL={sig.pnl_pct:+.2f}% ₹{sig.pnl_rupees:+.0f} "
                  f"(max_fav={sig.max_favorable:+.2f}% max_adv={sig.max_adverse:+.2f}%)")

    # Write CSV
    dawn._signals = signals
    csv_path = dawn._write_csv()
    print(f"\nCSV written to: {csv_path}")

    # Print CSV content
    print(f"\n{'='*80}")
    print(f"DAWN CSV — {date_str}")
    print(f"{'='*80}")
    with open(csv_path) as f:
        print(f.read())

    # Metrics
    metrics = dawn.compute_metrics()
    print(f"Metrics: {metrics.win_count}W / {metrics.loss_count}L | "
          f"Win rate: {metrics.win_rate:.0f}% | "
          f"Avg PnL: {metrics.avg_pnl_pct:+.2f}% | "
          f"Total PnL: ₹{metrics.total_pnl_rupees:+.0f}")

    return csv_path


# ═══════════════════════════════════════════════════════════════════════════
# APR 10, 2025 — DEGRADED MODE (Brief Failed)
# ═══════════════════════════════════════════════════════════════════════════

def simulate_apr_10():
    """
    Apr 10: Morning brief FAILED (Gemini API unavailable).
    HYDRA found 2 events at 09:00. DAWN operates Source B only.
    """
    dawn = DawnStrategy()
    dawn._today = __import__("datetime").date(2025, 4, 10)
    date_str = "2025-04-10"

    print()
    print("=" * 80)
    print(f"DAWN SIMULATION — {date_str} (Degraded Mode — Brief Failed)")
    print("=" * 80)
    print()
    print("Source B (DAWN scan): 2 events from EventScanner")
    print("Source A (Brief):     FAILED (Gemini API unavailable)")
    print()

    # Source B only — 2 events
    source_b = [
        DawnCandidate(
            symbol="NATIONALUM", direction="BUY",
            catalyst="Board approves expansion capex of ₹2,000 crore",
            catalyst_type="EXPANSION", source="DAWN_SCAN",
            urgency=7.5, catalyst_strength="MEDIUM", freshness_hours=5.0,
            avg_daily_turnover=35_000_000,
        ),
        DawnCandidate(
            symbol="COCHINSHIP", direction="BUY",
            catalyst="Defence ministry order worth ₹3,500 crore",
            catalyst_type="CONTRACT_WIN", source="DAWN_SCAN",
            urgency=8.0, catalyst_strength="HIGH", freshness_hours=3.0,
            avg_daily_turnover=22_000_000,
        ),
    ]

    dawn._candidates = source_b

    # Score and qualify
    scored = []
    for c in source_b:
        passes, reason = dawn._qualifies(c)
        score = dawn._score_candidate(c) if passes else 0
        threshold = LONG_THRESHOLD
        status = "✅ QUALIFIES" if passes and score >= threshold else f"❌ {reason}" if not passes else f"❌ Score {score:.0f} < {threshold}"
        print(f"  {c.symbol:12s} | urg={c.urgency:.0f} | C={getattr(c, '_score_catalyst', 0):.0f} F={getattr(c, '_score_freshness', 0):.0f} L={getattr(c, '_score_liquidity', 0):.0f} T={getattr(c, '_score_technical', 0):.0f} M={getattr(c, '_score_context', 0):.0f} = {score:.0f} | {status}")
        if passes and score >= threshold:
            sig = DawnSignal(
                symbol=c.symbol, direction=c.direction, catalyst=c.catalyst,
                catalyst_type=c.catalyst_type, source=c.source, dawn_score=score,
                score_catalyst=getattr(c, '_score_catalyst', 0),
                score_freshness=getattr(c, '_score_freshness', 0),
                score_liquidity=getattr(c, '_score_liquidity', 0),
                score_technical=getattr(c, '_score_technical', 0),
                score_context=getattr(c, '_score_context', 0),
            )
            scored.append(sig)

    scored.sort(key=lambda s: s.dawn_score, reverse=True)
    signals = scored[:MAX_SIGNALS_PER_DAY]

    print(f"\nQualified: {len(scored)} → Signals: {[s.symbol for s in signals]}")
    print("⚠ Operating in degraded mode (Source B only)")
    print()

    # Simulate entries
    open_prices = {
        "NATIONALUM": 125.00,
        "COCHINSHIP": 1850.00,
    }

    for sig in signals:
        price = open_prices.get(sig.symbol, 100.0)
        sig.entry_price = price
        sig.current_price = price
        sig.quantity = max(1, int(5000 / price))
        sig.sl_price = price * (1 - HARD_SL_PCT)
        sig.sl_type = "INITIAL"
        sig.status = "ACTIVE"

    print("09:15 — Virtual entries recorded:")
    for sig in signals:
        print(f"  {sig.symbol:12s} entry={sig.entry_price:.2f} SL={sig.sl_price:.2f} qty={sig.quantity}")
    print()

    # Price trajectories
    trajectories = {
        "COCHINSHIP": [
            ("09:30", 1.01), ("10:00", 1.02), ("10:30", 1.035), ("11:00", 1.04),
            ("11:30", 1.045), ("12:00", 1.05), ("12:30", 1.048), ("13:00", 1.045),
            ("13:30", 1.042), ("14:00", 1.04), ("14:30", 1.038), ("15:00", 1.04), ("15:20", 1.04),
        ],
        "NATIONALUM": [
            ("09:30", 1.005), ("10:00", 1.01), ("10:30", 1.015), ("11:00", 1.02),
            ("11:30", 1.018), ("12:00", 1.015), ("12:30", 1.012), ("13:00", 1.01),
            ("13:30", 1.008), ("14:00", 1.01), ("14:30", 1.012), ("15:00", 1.015), ("15:20", 1.015),
        ],
    }

    print("Intraday simulation:")
    for sig in signals:
        trajectory = trajectories.get(sig.symbol, [])
        for time_label, multiplier in trajectory:
            sig.current_price = sig.entry_price * multiplier
            move_pct = (sig.current_price - sig.entry_price) / sig.entry_price * 100
            sig.max_favorable = max(sig.max_favorable, move_pct)
            sig.max_adverse = min(sig.max_adverse, move_pct)
            dawn._update_stop_loss(sig)

        # Time exit
        sig.exit_price = sig.current_price
        sig.exit_time = "15:20"
        sig.exit_reason = "TIME_EXIT_15:20"
        sig.status = "CLOSED"
        dawn._compute_pnl(sig)
        print(f"  {sig.symbol:12s} [15:20] CLOSED at {sig.exit_price:.2f} "
              f"PnL={sig.pnl_pct:+.2f}% ₹{sig.pnl_rupees:+.0f} "
              f"(max_fav={sig.max_favorable:+.2f}% max_adv={sig.max_adverse:+.2f}%)")

    # Write CSV
    dawn._signals = signals
    csv_path = dawn._write_csv()
    print(f"\nCSV written to: {csv_path}")

    print(f"\n{'='*80}")
    print(f"DAWN CSV — {date_str}")
    print(f"{'='*80}")
    with open(csv_path) as f:
        print(f.read())

    metrics = dawn.compute_metrics()
    print(f"Metrics: {metrics.win_count}W / {metrics.loss_count}L | "
          f"Win rate: {metrics.win_rate:.0f}% | "
          f"Avg PnL: {metrics.avg_pnl_pct:+.2f}% | "
          f"Total PnL: ₹{metrics.total_pnl_rupees:+.0f}")

    return csv_path


if __name__ == "__main__":
    print("DAWN Strategy Simulation")
    print("=" * 80)

    csv1 = simulate_apr_9()
    csv2 = simulate_apr_10()

    print()
    print("=" * 80)
    print("SIMULATION COMPLETE")
    print(f"  Apr 9 CSV: {csv1}")
    print(f"  Apr 10 CSV: {csv2}")
    print("=" * 80)
