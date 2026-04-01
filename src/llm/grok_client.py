"""
grok_client.py — xAI Grok 4.20 Portfolio Orchestrator
------------------------------------------------------
Grok 4.20 Integration v2: Portfolio-Level Reasoning

ARCHITECTURE CHANGE (v2):
  v1 used Grok as a narrow per-symbol gate:
    - grok_conviction_analysis() → one symbol at a time
    - grok_watchlist_ranking()   → one strategy's watchlist at a time
    This was wasteful (25 scattered calls/day) and Grok never saw the
    full portfolio, risk state, or cross-strategy correlation.

  v2 models Grok as a portfolio-level orchestrator (Alpha Arena pattern):
    - grok_morning_strategist()   → pre-market regime + ranked watchlist
    - grok_portfolio_optimizer()  → batched portfolio decision at key times
    - grok_eod_review()           → post-market learning notes

  Each call receives the FULL picture: all open positions, both strategy
  heads' candidates, risk budgets, and macro context. Grok reasons over
  the entire board like a chess master.

CALL BUDGET:
  ~7 calls/day (08:30, 09:17, 09:30, 10:00, 10:45, 11:45, 15:40)
  = ~154 calls/month (22 trading days)

SAFETY:
  Grok PROPOSES, hard-coded risk DISPOSES. Every output is validated
  by SlotManager, DailyRiskState, and the full risk stack. The LLM
  can never bypass mechanical safety rails.
"""
import os
import json
import re
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_GROK_MODEL = "grok-4"
_GROK_BASE_URL = "https://api.x.ai/v1"

# Daily call budget — tracked by runner, not by individual strategies
GROK_DAILY_BUDGET = 10  # generous upper bound; typical usage = 7

# ── Conviction stability tracker (P1-2) ──────────────────────────────────
# Tracks symbol → (last_conviction, timestamp) to detect wild swings
_conviction_history: Dict[str, List[Dict]] = {}  # symbol → [{conviction, timestamp}]

def _check_conviction_stability(symbol: str, new_conviction: float) -> float:
    """
    Check if conviction changed >30pts in <30min with no new data.
    If unstable, log WARNING and return average of old and new.
    Otherwise return new_conviction unchanged.
    """
    from datetime import datetime, timedelta
    now = datetime.now()

    history = _conviction_history.get(symbol, [])

    # Prune entries older than 60 min
    cutoff = now - timedelta(minutes=60)
    history = [h for h in history if h["ts"] > cutoff]

    adjusted = new_conviction
    if history:
        last = history[-1]
        delta = abs(new_conviction - last["conviction"])
        elapsed_min = (now - last["ts"]).total_seconds() / 60
        if delta > 30 and elapsed_min < 30:
            avg = (last["conviction"] + new_conviction) / 2
            logger.warning(
                f"[Grok/Stability] {symbol}: conviction swing {last['conviction']:.0f} → "
                f"{new_conviction:.0f} ({delta:+.0f}pts in {elapsed_min:.0f}min) — "
                f"using average {avg:.0f}"
            )
            adjusted = avg

    history.append({"conviction": adjusted, "ts": now})
    _conviction_history[symbol] = history
    return adjusted


def reset_conviction_history() -> None:
    """Called at daily reset to clear stale conviction data."""
    _conviction_history.clear()


def _get_client():
    """Lazy-init the OpenAI client pointing at xAI."""
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return None

    api_key = os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
    if not api_key:
        logger.error("GROK_API_KEY / XAI_API_KEY not set in environment")
        return None

    return OpenAI(api_key=api_key, base_url=_GROK_BASE_URL, timeout=45.0)


def _classify_grok_error(e: Exception, raw_response: str = "") -> str:
    """Classify a Grok API failure into a human-readable reason."""
    err_type = type(e).__name__
    err_str = str(e).lower()

    if "timeout" in err_str or "timed out" in err_str:
        return "timeout"
    if "rate" in err_str or "429" in err_str or "too many" in err_str:
        return "rate_limit"
    if isinstance(e, (json.JSONDecodeError, ValueError)) or "json" in err_str:
        return f"json_error (raw={raw_response[:200]})" if raw_response else "json_error"
    if "api_key" in err_str or "auth" in err_str or "401" in err_str:
        return "auth_error"
    if "connection" in err_str or "network" in err_str:
        return "network_error"
    if not raw_response or raw_response.strip() == "":
        return "empty_response"
    return f"unknown ({err_type}: {str(e)[:100]})"


def _extract_json(raw: str) -> Any:
    """
    Robust JSON extraction from LLM output.
    Handles: <think> blocks, ```json fences, bare JSON, partial matches.
    """
    # Strip <think> blocks (Grok reasoning traces)
    cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    # Try fenced JSON first
    if "```json" in cleaned:
        json_str = cleaned.split("```json")[1].split("```")[0].strip()
    elif "```" in cleaned:
        json_str = cleaned.split("```")[1].split("```")[0].strip()
    else:
        json_str = cleaned

    # Attempt parse
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # Fallback: extract first JSON object or array via regex
    # Try array first (portfolio optimizer returns arrays)
    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try single object
    match = re.search(r'\{[^{}]*\}', cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON found in Grok response: {cleaned[:300]}")


# ═══════════════════════════════════════════════════════════════════════
# 1. MORNING STRATEGIST — Pre-market regime + watchlist (08:30 IST)
# ═══════════════════════════════════════════════════════════════════════

def grok_morning_strategist(
    macro_context: Dict[str, Any],
    hydra_events: List[Dict[str, Any]],
    viper_movers: List[Dict[str, Any]],
    previous_day_pnl: float = 0.0,
    risk_budget: Dict[str, Any] = None,
) -> Optional[Dict[str, Any]]:
    """
    Pre-market portfolio strategist call.

    Called ONCE at 08:30 IST before market open. Sets the daily
    risk stance and prioritizes the combined watchlist.

    Args:
        macro_context: {trend, strength, fii_dii, crude_move, usd_inr, pcr, ...}
        hydra_events: Top 5-8 events [{symbol, direction, urgency, event_summary}]
        viper_movers: Top 5-10 pre-market movers [{symbol, pct_change, volume_ratio, move_type, direction}]
        previous_day_pnl: Yesterday's realized P&L in ₹
        risk_budget: {daily_loss_cap, per_trade_capital, max_trades, slots_available}

    Returns:
        {
            "regime": "AGGRESSIVE_BULLISH|CAUTIOUS_BULLISH|NEUTRAL|CAUTIOUS_BEARISH|AGGRESSIVE_BEARISH",
            "regime_reasoning": "...",
            "watchlist": [
                {"symbol": "X", "bias": "LONG|SHORT", "priority": 1, "entry_zone": [lo, hi],
                 "stop": val, "target": val, "catalyst": "...", "max_allocation_pct": 25}
            ],
            "avoid": ["SYMBOL — reason", ...],
            "risk_stance": "Deploy max X% capital today..."
        }
    """
    client = _get_client()
    if client is None:
        return None

    # Format compact tables for the prompt
    events_table = "symbol,direction,urgency,catalyst\n"
    for e in (hydra_events or [])[:8]:
        events_table += f"{e.get('symbol','?')},{e.get('direction','?')},{e.get('urgency',0):.0f},{e.get('event_summary','')[:60]}\n"

    movers_table = "symbol,direction,pct_change,volume_ratio,move_type\n"
    for m in (viper_movers or [])[:10]:
        movers_table += (
            f"{m.get('symbol','?')},{m.get('direction','?')},"
            f"{m.get('pct_change',0):+.1f}%,{m.get('volume_ratio',0):.1f}x,"
            f"{m.get('move_type','?')}\n"
        )

    risk_info = risk_budget or {}

    prompt = f"""You are a senior Indian equity portfolio strategist with 20 years of NSE experience.
It is 08:30 IST, market opens at 09:15. You must set today's trading plan.

## Macro Context
{json.dumps(macro_context, indent=2, default=str)}

## Yesterday's P&L
₹{previous_day_pnl:,.2f}

## Risk Budget
Daily loss cap: ₹{risk_info.get('daily_loss_cap', 2500):,}
Per trade capital: ₹{risk_info.get('per_trade_capital', 5000):,}
Max trades today: {risk_info.get('max_trades', 5)}
Slots available: {risk_info.get('slots_available', 5)}

## HYDRA Events (overnight catalysts classified by urgency)
{events_table}

## VIPER Top Movers (pre-market momentum)
{movers_table}

Based on your experience, produce a TRADING PLAN for today.

Rules:
- Be CONSERVATIVE. Most days should deploy 40-60% capital, not 100%.
- If macro is risk-off, FAVOR SHORT candidates over sitting out. Deploy ≤30% on LONG, but SHORT setups with strong catalysts (earnings miss, FDA rejection, promoter selling) should be prioritized.
- If macro is risk-on, favor LONG candidates. SHORT setups need higher conviction (≥80) to justify.
- Rank at most 5 symbols across BOTH lists. Do NOT rank more than you'd actually trade.
- Set realistic entry zones, stops, and targets based on ATR-appropriate levels.
- Flag any symbols that appear in BOTH HYDRA events and VIPER movers (confluence = higher priority).
- Check X/Twitter for real-time sentiment on the top 3 candidates — are traders bullish or fading?

Return ONLY valid JSON:
{{
    "regime": "<AGGRESSIVE_BULLISH|CAUTIOUS_BULLISH|NEUTRAL|CAUTIOUS_BEARISH|AGGRESSIVE_BEARISH>",
    "regime_reasoning": "<2-3 sentences>",
    "watchlist": [
        {{
            "symbol": "SYMBOL",
            "bias": "LONG or SHORT",
            "priority": 1,
            "entry_zone": [low_price, high_price],
            "stop": stop_price,
            "target": target_price,
            "catalyst": "1 sentence why",
            "max_allocation_pct": 20
        }}
    ],
    "avoid": ["SYMBOL — reason", ...],
    "risk_stance": "1 sentence on capital deployment"
}}"""

    raw = ""
    try:
        response = client.chat.completions.create(
            model=_GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1200,
        )
        raw = response.choices[0].message.content.strip()
        if not raw:
            logger.warning("[Grok/Morning] Empty response — reason: empty_response")
            return None
        result = _extract_json(raw)
        logger.info(
            f"[Grok/Morning] regime={result.get('regime', '?')} "
            f"watchlist={[w.get('symbol') for w in result.get('watchlist', [])]}"
        )
        return result
    except Exception as e:
        reason = _classify_grok_error(e, raw)
        logger.warning(f"[Grok/Morning] Empty response — reason: {reason}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# 2. PORTFOLIO OPTIMIZER — Intraday batch decision (09:17–11:45 IST)
# ═══════════════════════════════════════════════════════════════════════

def grok_portfolio_optimizer(
    open_positions: List[Dict[str, Any]],
    hydra_candidates: List[Dict[str, Any]],
    viper_candidates: List[Dict[str, Any]],
    risk_state: Dict[str, Any],
    market_pulse: Dict[str, Any],
    morning_plan: Optional[Dict[str, Any]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Portfolio-level trade decision at key intraday milestones.

    Called at: 09:17, 09:30, 10:00, 10:45, 11:45 IST.

    This replaces ALL individual grok_conviction_analysis() calls.
    Instead of asking "should I buy RELIANCE?", we ask "given my entire
    portfolio and all candidates from both strategies, what should I do NOW?"

    Args:
        open_positions: [{symbol, side, qty, entry_price, current_pnl, time_in_trade_min, strategy}]
        hydra_candidates: Top 5 [{symbol, direction, urgency, event_summary, ta_score, vwap_dist}]
        viper_candidates: Top 5 [{symbol, direction, move_type, pct_change, ta_score, volume_ratio}]
        risk_state: {daily_pnl, trades_taken, slots_used, slots_remaining, daily_loss_cap}
        market_pulse: {nifty_change_pct, breadth, pcr, volume_regime, time_bucket}
        morning_plan: Output from grok_morning_strategist() if available

    Returns:
        List of action dicts:
        [
            {
                "symbol": "RELIANCE",
                "action": "BUY|SHORT|SKIP|TIGHTEN_STOP|CLOSE",
                "conviction": 0-100,
                "reason": "...",
                "entry_timing": "immediate|wait_for_pullback",
                "stop": float (optional),
                "target": float (optional),
                "allocation_pct": 20 (optional, for new entries)
            }
        ]
    """
    client = _get_client()
    if client is None:
        return None

    # Format positions table
    pos_table = "symbol,side,qty,entry_price,current_pnl,time_min,strategy\n"
    for p in (open_positions or []):
        pos_table += (
            f"{p.get('symbol','?')},{p.get('side','?')},{p.get('qty',0)},"
            f"{p.get('entry_price',0):.2f},₹{p.get('current_pnl',0):+.2f},"
            f"{p.get('time_in_trade_min',0):.0f},{p.get('strategy','?')}\n"
        )
    if not open_positions:
        pos_table += "(no open positions)\n"

    # Format HYDRA candidates
    hydra_table = "symbol,direction,urgency,ta_score,vwap_dist,catalyst\n"
    for c in (hydra_candidates or [])[:5]:
        hydra_table += (
            f"{c.get('symbol','?')},{c.get('direction','?')},{c.get('urgency',0):.0f},"
            f"{c.get('ta_score',0):.0f},{c.get('vwap_dist',0):.1f}%,"
            f"{c.get('event_summary','')[:50]}\n"
        )

    # Format VIPER candidates
    viper_table = "symbol,direction,move_type,pct_change,ta_score,volume_ratio\n"
    for c in (viper_candidates or [])[:5]:
        viper_table += (
            f"{c.get('symbol','?')},{c.get('direction','?')},{c.get('move_type','?')},"
            f"{c.get('pct_change',0):+.1f}%,{c.get('ta_score',0):.0f},"
            f"{c.get('volume_ratio',0):.1f}x\n"
        )

    # Confluence detection
    hydra_syms = {c.get('symbol') for c in (hydra_candidates or [])}
    viper_syms = {c.get('symbol') for c in (viper_candidates or [])}
    confluence = hydra_syms & viper_syms
    confluence_str = ", ".join(confluence) if confluence else "(none)"

    # Morning plan context
    morning_context = ""
    if morning_plan:
        morning_context = (
            f"Regime: {morning_plan.get('regime', '?')}\n"
            f"Risk stance: {morning_plan.get('risk_stance', '?')}\n"
            f"Morning priorities: {[w.get('symbol') for w in morning_plan.get('watchlist', [])]}"
        )
    else:
        morning_context = "(no morning plan available)"

    prompt = f"""You are a senior Indian equity portfolio manager making real-time trading decisions.

## Current Open Positions
{pos_table}

## HYDRA Candidates (event-driven catalysts)
{hydra_table}

## VIPER Candidates (momentum/top movers)
{viper_table}

## Cross-Strategy Confluence
Symbols in BOTH HYDRA + VIPER: {confluence_str}
(Confluence = higher conviction, these should be prioritized)

## Risk State
Daily P&L so far: ₹{risk_state.get('daily_pnl', 0):+,.2f}
Trades taken: {risk_state.get('trades_taken', 0)}
Slots used/remaining: {risk_state.get('slots_used', 0)}/{risk_state.get('slots_remaining', 5)}
Daily loss cap: ₹{risk_state.get('daily_loss_cap', 2500):,}

## Market Pulse
{json.dumps(market_pulse, indent=2, default=str)}

## Morning Plan
{morning_context}

DECIDE what to do RIGHT NOW. You may:
- BUY or SHORT a new candidate (if slots available and risk allows)
- TIGHTEN_STOP on an existing position (if momentum is fading)
- CLOSE an existing position (if thesis is broken)
- SKIP a candidate (with reason)

Rules:
- NEVER exceed the daily loss cap. If daily P&L is near the cap, SKIP all new entries.
- Check X/Twitter for real-time sentiment on any candidate you rate highly.
- Confluence symbols get priority — both strategy heads agree.
- Max 2 actions per call (we execute one at a time).
- Be HARSH on conviction. Only recommend BUY/SHORT if you'd bet your own money.
- If the morning plan said "sit out" or regime is bearish, respect that for LONG entries. However, in a bearish/risk-off regime, SHORT candidates with strong catalysts should be MORE favorable — this is exactly when shorts work best.
- In a risk-on/bullish regime, SHORT candidates need conviction ≥80 to override the macro tailwind.

Return ONLY valid JSON array:
[
    {{
        "symbol": "SYMBOL",
        "action": "BUY|SHORT|SKIP|TIGHTEN_STOP|CLOSE",
        "conviction": 0-100,
        "reason": "1-2 sentences",
        "entry_timing": "immediate|wait_for_pullback",
        "stop": 0.0,
        "target": 0.0,
        "allocation_pct": 20
    }}
]"""

    raw = ""
    try:
        response = client.chat.completions.create(
            model=_GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        )
        raw = response.choices[0].message.content.strip()
        if not raw:
            logger.warning("[Grok/Optimizer] Empty response — reason: empty_response")
            return None
        result = _extract_json(raw)

        # Normalize: ensure we always return a list
        if isinstance(result, dict):
            result = [result]

        # Apply conviction stability check (P1-2)
        for action in result:
            sym = action.get("symbol", "")
            conv = action.get("conviction", 0)
            reason = action.get("reason", "")
            if sym and conv > 0:
                stabilized = _check_conviction_stability(sym, conv)
                action["conviction"] = stabilized
                logger.info(
                    f"[Grok/Optimizer] {sym} {action.get('action','?')}: "
                    f"conviction={stabilized:.0f} | {reason}"
                )

        logger.info(
            f"[Grok/Optimizer] actions="
            f"{[(a.get('symbol'), a.get('action'), a.get('conviction', 0)) for a in result]}"
        )
        return result
    except Exception as e:
        reason = _classify_grok_error(e, raw)
        logger.warning(f"[Grok/Optimizer] Empty response — reason: {reason}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# 3. EOD REVIEW — Post-market learning (15:40 IST)
# ═══════════════════════════════════════════════════════════════════════

def grok_eod_review(
    trades_today: List[Dict[str, Any]],
    daily_pnl: float,
    morning_plan: Optional[Dict[str, Any]] = None,
    market_summary: str = "",
) -> Optional[Dict[str, Any]]:
    """
    End-of-day review and learning notes.

    Called ONCE at 15:40 IST after market close.

    Args:
        trades_today: [{symbol, direction, entry, exit, pnl, strategy, reason}]
        daily_pnl: Total realized P&L today in ₹
        morning_plan: The morning strategist output (to compare plan vs reality)
        market_summary: Brief market summary (NIFTY close, breadth, etc.)

    Returns:
        {
            "grade": "A|B|C|D|F",
            "summary": "...",
            "lessons": ["lesson1", ...],
            "regime_update": "BULLISH|NEUTRAL|BEARISH (for tomorrow's context)",
            "mistakes": ["mistake1", ...],
            "tomorrow_watch": ["SYMBOL — why", ...]
        }
    """
    client = _get_client()
    if client is None:
        return None

    trades_str = ""
    for t in (trades_today or []):
        trades_str += (
            f"  {t.get('symbol','?')} {t.get('direction','?')}: "
            f"entry={t.get('entry',0):.2f} exit={t.get('exit',0):.2f} "
            f"P&L=₹{t.get('pnl',0):+.2f} [{t.get('strategy','?')}] "
            f"reason={t.get('reason','')}\n"
        )
    if not trades_str:
        trades_str = "  (no trades executed today)\n"

    morning_context = ""
    if morning_plan:
        morning_context = (
            f"Planned regime: {morning_plan.get('regime', '?')}\n"
            f"Planned watchlist: {[w.get('symbol') for w in morning_plan.get('watchlist', [])]}\n"
            f"Risk stance: {morning_plan.get('risk_stance', '?')}"
        )
    else:
        morning_context = "(no morning plan was generated)"

    prompt = f"""You are reviewing today's trading performance for an Indian equity intraday system.

## Today's Trades
{trades_str}

## Daily P&L
₹{daily_pnl:+,.2f}

## Morning Plan (what we PLANNED at 08:30)
{morning_context}

## Market Summary
{market_summary or "(no market summary available)"}

Grade today's performance (A-F) and extract actionable lessons.
- Did the morning plan align with reality?
- Were the right trades taken? Were any good setups missed?
- What patterns should we watch for tomorrow?
- Check X/Twitter for after-market sentiment on stocks we traded.

Return ONLY valid JSON:
{{
    "grade": "A|B|C|D|F",
    "summary": "2-3 sentences on today's performance",
    "lessons": ["actionable lesson 1", "actionable lesson 2"],
    "mistakes": ["mistake if any"],
    "regime_update": "BULLISH|NEUTRAL|BEARISH",
    "tomorrow_watch": ["SYMBOL — why to watch"]
}}"""

    raw = ""
    try:
        response = client.chat.completions.create(
            model=_GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=800,
        )
        raw = response.choices[0].message.content.strip()
        if not raw:
            logger.warning("[Grok/EOD] Empty response — reason: empty_response")
            return None
        result = _extract_json(raw)
        logger.info(f"[Grok/EOD] grade={result.get('grade', '?')} pnl=₹{daily_pnl:+.2f}")
        return result
    except Exception as e:
        reason = _classify_grok_error(e, raw)
        logger.warning(f"[Grok/EOD] Empty response — reason: {reason}")
        return None
