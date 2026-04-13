"""
macro_sector_mapper.py — Macro-Driven Sector Plays
----------------------------------------------------
Rule-based mapper: when macro data crosses thresholds, inject
sector plays (LONG/SHORT) into hot stocks pipeline.

Example: Crude +7% → SHORT OMCs (HPCL, BPCL, IOC), LONG upstream (ONGC, OIL).

These are machine-confirmed catalysts from live market data —
they skip LLM catalyst validation in the hot stocks pipeline.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Max plays injected per rule group (avoid flooding hot stocks)
MAX_PER_RULE = 3


# ── Macro Impact Rules ──────────────────────────────────────────────────

MACRO_IMPACT_RULES: List[dict] = [
    # ── CRUDE SPIKE: OMCs (downstream — margin compression) ──
    {
        "name": "CRUDE_SPIKE_OMC",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v > 2.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "HPCL", "direction": "SHORT", "conviction": 80,
             "reason_template": "Crude +{val:.1f}% → OMC margin compression"},
            {"symbol": "BPCL", "direction": "SHORT", "conviction": 78,
             "reason_template": "Crude +{val:.1f}% → OMC margin compression"},
            {"symbol": "IOC", "direction": "SHORT", "conviction": 75,
             "reason_template": "Crude +{val:.1f}% → OMC margin compression"},
            {"symbol": "MRPL", "direction": "SHORT", "conviction": 70,
             "reason_template": "Crude +{val:.1f}% → refining margin squeeze"},
        ],
    },
    # ── CRUDE SPIKE: Upstream (revenue boost) ──
    {
        "name": "CRUDE_SPIKE_UPSTREAM",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v > 2.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "ONGC", "direction": "LONG", "conviction": 75,
             "reason_template": "Crude +{val:.1f}% → upstream revenue boost"},
            {"symbol": "OIL", "direction": "LONG", "conviction": 72,
             "reason_template": "Crude +{val:.1f}% → upstream revenue boost"},
        ],
    },
    # ── CRUDE SPIKE: Aviation (ATF cost) ──
    {
        "name": "CRUDE_SPIKE_AVIATION",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v > 2.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "INDIGO", "direction": "SHORT", "conviction": 72,
             "reason_template": "Crude +{val:.1f}% → ATF cost spike for airlines"},
            {"symbol": "SPICEJET", "direction": "SHORT", "conviction": 68,
             "reason_template": "Crude +{val:.1f}% → ATF cost spike for airlines"},
        ],
    },
    # ── CRUDE SPIKE: Paint / chemicals (crude derivatives) ──
    {
        "name": "CRUDE_SPIKE_PAINT",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v > 2.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "ASIANPAINT", "direction": "SHORT", "conviction": 65,
             "reason_template": "Crude +{val:.1f}% → crude-derivative raw material cost"},
            {"symbol": "BERGEPAINT", "direction": "SHORT", "conviction": 65,
             "reason_template": "Crude +{val:.1f}% → crude-derivative raw material cost"},
            {"symbol": "KANSAINER", "direction": "SHORT", "conviction": 65,
             "reason_template": "Crude +{val:.1f}% → crude-derivative raw material cost"},
        ],
    },
    # ── CRUDE CRASH: OMCs (margin expansion) ──
    {
        "name": "CRUDE_CRASH_OMC",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v < -3.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "HPCL", "direction": "LONG", "conviction": 78,
             "reason_template": "Crude {val:+.1f}% → OMC margin expansion"},
            {"symbol": "BPCL", "direction": "LONG", "conviction": 76,
             "reason_template": "Crude {val:+.1f}% → OMC margin expansion"},
            {"symbol": "IOC", "direction": "LONG", "conviction": 73,
             "reason_template": "Crude {val:+.1f}% → OMC margin expansion"},
        ],
    },
    # ── CRUDE CRASH: Upstream (revenue hit) ──
    {
        "name": "CRUDE_CRASH_UPSTREAM",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v < -3.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "ONGC", "direction": "SHORT", "conviction": 72,
             "reason_template": "Crude {val:+.1f}% → upstream revenue hit"},
            {"symbol": "OIL", "direction": "SHORT", "conviction": 70,
             "reason_template": "Crude {val:+.1f}% → upstream revenue hit"},
        ],
    },
    # ── CRUDE CRASH: Aviation (fuel relief) ──
    {
        "name": "CRUDE_CRASH_AVIATION",
        "trigger_field": "crude_wti_pct",
        "condition": lambda v: v is not None and v < -3.0,
        "catalyst_type": "MACRO_CRUDE",
        "stocks": [
            {"symbol": "INDIGO", "direction": "LONG", "conviction": 75,
             "reason_template": "Crude {val:+.1f}% → ATF cost relief for airlines"},
        ],
    },
    # ── USD STRENGTH (DXY up) ──
    {
        "name": "USD_STRENGTH",
        "trigger_field": "dxy_pct",
        "condition": lambda v: v is not None and v > 0.5,
        "catalyst_type": "MACRO_FX",
        "stocks": [
            {"symbol": "INFY", "direction": "LONG", "conviction": 72,
             "reason_template": "DXY +{val:.1f}% → IT export earnings FX tailwind"},
            {"symbol": "TCS", "direction": "LONG", "conviction": 70,
             "reason_template": "DXY +{val:.1f}% → IT export earnings FX tailwind"},
            {"symbol": "WIPRO", "direction": "LONG", "conviction": 68,
             "reason_template": "DXY +{val:.1f}% → IT export earnings FX tailwind"},
            {"symbol": "HCLTECH", "direction": "LONG", "conviction": 68,
             "reason_template": "DXY +{val:.1f}% → IT export earnings FX tailwind"},
            {"symbol": "SUNPHARMA", "direction": "LONG", "conviction": 68,
             "reason_template": "DXY +{val:.1f}% → pharma export revenue boost"},
            {"symbol": "DRREDDY", "direction": "LONG", "conviction": 67,
             "reason_template": "DXY +{val:.1f}% → pharma export revenue boost"},
            {"symbol": "CIPLA", "direction": "LONG", "conviction": 66,
             "reason_template": "DXY +{val:.1f}% → pharma export revenue boost"},
        ],
    },
    # ── USD WEAKNESS (DXY down) ──
    {
        "name": "USD_WEAKNESS",
        "trigger_field": "dxy_pct",
        "condition": lambda v: v is not None and v < -0.5,
        "catalyst_type": "MACRO_FX",
        "stocks": [
            {"symbol": "INFY", "direction": "SHORT", "conviction": 68,
             "reason_template": "DXY {val:+.1f}% → IT export earnings FX headwind"},
            {"symbol": "TCS", "direction": "SHORT", "conviction": 67,
             "reason_template": "DXY {val:+.1f}% → IT export earnings FX headwind"},
            {"symbol": "WIPRO", "direction": "SHORT", "conviction": 65,
             "reason_template": "DXY {val:+.1f}% → IT export earnings FX headwind"},
            {"symbol": "SUNPHARMA", "direction": "SHORT", "conviction": 66,
             "reason_template": "DXY {val:+.1f}% → pharma export revenue headwind"},
            {"symbol": "DRREDDY", "direction": "SHORT", "conviction": 65,
             "reason_template": "DXY {val:+.1f}% → pharma export revenue headwind"},
        ],
    },
    # ── US MARKET CRASH ──
    {
        "name": "US_CRASH",
        "trigger_field": "spy_pct",
        "condition": lambda v: v is not None and v < -2.0,
        "catalyst_type": "MACRO_US",
        "stocks": [
            {"symbol": "INFY", "direction": "SHORT", "conviction": 75,
             "reason_template": "SPY {val:+.1f}% → US recession fear, IT spending cuts"},
            {"symbol": "TCS", "direction": "SHORT", "conviction": 73,
             "reason_template": "SPY {val:+.1f}% → US recession fear, IT spending cuts"},
            {"symbol": "WIPRO", "direction": "SHORT", "conviction": 70,
             "reason_template": "SPY {val:+.1f}% → US recession fear, IT spending cuts"},
        ],
    },
    # ── US MARKET SURGE ──
    {
        "name": "US_SURGE",
        "trigger_field": "spy_pct",
        "condition": lambda v: v is not None and v > 2.0,
        "catalyst_type": "MACRO_US",
        "stocks": [
            {"symbol": "INFY", "direction": "LONG", "conviction": 72,
             "reason_template": "SPY +{val:.1f}% → US risk-on, IT demand boost"},
            {"symbol": "TCS", "direction": "LONG", "conviction": 70,
             "reason_template": "SPY +{val:.1f}% → US risk-on, IT demand boost"},
            {"symbol": "WIPRO", "direction": "LONG", "conviction": 68,
             "reason_template": "SPY +{val:.1f}% → US risk-on, IT demand boost"},
        ],
    },
    # ── GOLD SPIKE ──
    {
        "name": "GOLD_SPIKE",
        "trigger_field": "gold_pct",
        "condition": lambda v: v is not None and v > 1.5,
        "catalyst_type": "MACRO_GOLD",
        "stocks": [
            {"symbol": "TITAN", "direction": "LONG", "conviction": 72,
             "reason_template": "Gold +{val:.1f}% → jewellery revenue & inventory gains"},
            {"symbol": "MUTHOOTFIN", "direction": "LONG", "conviction": 70,
             "reason_template": "Gold +{val:.1f}% → gold loan collateral value up"},
        ],
    },
]


def scan_macro_impacts(macro_data: dict) -> List[dict]:
    """
    Evaluate macro data against all impact rules.
    Returns list of hot-stock-compatible dicts for injection into pipeline.

    Args:
        macro_data: dict with keys like spy_pct, crude_wti_pct, dxy_pct, gold_pct, etc.

    Returns:
        List of dicts: [{symbol, direction, catalyst, catalyst_type,
                         catalyst_strength, conviction, source}]
    """
    if not macro_data:
        return []

    plays: List[dict] = []
    triggered_rules: List[str] = []
    seen_symbols: set = set()  # Avoid duplicates across rules

    for rule in MACRO_IMPACT_RULES:
        trigger_field = rule["trigger_field"]
        trigger_val = macro_data.get(trigger_field)

        if trigger_val is None:
            continue

        try:
            if not rule["condition"](trigger_val):
                continue
        except Exception:
            continue

        triggered_rules.append(rule["name"])
        count = 0

        for stock in rule["stocks"]:
            if count >= MAX_PER_RULE:
                break
            if stock["symbol"] in seen_symbols:
                continue
            if stock["conviction"] < 65:
                continue

            catalyst = stock["reason_template"].format(val=trigger_val)
            plays.append({
                "symbol": stock["symbol"],
                "direction": stock["direction"],
                "catalyst": catalyst,
                "catalyst_type": rule["catalyst_type"],
                "catalyst_strength": "HIGH",
                "conviction": stock["conviction"],
                "source": "MACRO_RULES",
            })
            seen_symbols.add(stock["symbol"])
            count += 1

    if triggered_rules:
        logger.info(
            f"[MacroMapper] Triggered: {', '.join(triggered_rules)} → "
            f"{len(plays)} sector plays"
        )
    else:
        logger.info("[MacroMapper] No macro thresholds triggered")

    return plays
