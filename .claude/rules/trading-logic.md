# Trading Logic Rules — VoltEdgeAI

## Critical Invariants — NEVER VIOLATE

1. **ConvictionScore ≥ 70 to execute a trade** — universal gate, no exceptions
2. **COIL mode = DRY-RUN only** — never place live orders for COIL signals
3. **Illiquid = hard kill** — if depth analysis returns `signal="illiquid"`, set conviction to 0
4. **Grok budget: 25 calls/day** — track via `_grok_check_count`, don't exceed
5. **IST timezone for ALL time logic** — use `zoneinfo.ZoneInfo("Asia/Kolkata")`
6. **Runner must never crash** — wrap every loop iteration in try-except
7. **SlotManager is THE arbiter** — always check `can_trade()` before executing
8. **`reset_daily()` must be called** on all stateful objects at midnight

## Scoring System

- HYDRA: Event(0-70) + TA(0-22) + Depth(0-10) + Grok(30% weighted)
- VIPER: Move(0-30) + TA(0-25) + Depth(0-10) + Context(0-10) + Grok(30% weighted)
- Confluence bonus: +15 to conviction (applied ONCE, never double-count in capital allocation)
- Capital allocation: ≥85 → 100%, 70-84 → 70%, <70 → 0%

## Risk Stack (Order of Evaluation)

1. Conviction threshold (≥70)
2. SlotManager (max positions, symbol lock)
3. Daily loss cap (₹ hard limit)
4. ATR position sizing (2.5% max stop)
5. Trading costs viability
6. Liquidity check (depth analysis)
7. Circuit breaker guard
8. Sector concentration (max 2/sector)
9. Time-of-day guard (no entries last 30 min)
10. F&O expiry factor
11. Macro risk-off dampener
12. PCR modifier

## Known Proxies / Limitations

- VIPER `volume_ratio` = `abs(pct_change) / 2` — NOT actual relative volume
- Macro context scores are estimated composites, not real-time
- Depth analysis quality depends on Kite tick data availability
