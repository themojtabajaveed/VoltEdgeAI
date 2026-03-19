from dataclasses import dataclass
from typing import TYPE_CHECKING
import math

if TYPE_CHECKING:
    from config.risk import RiskConfig

@dataclass
class SymbolStats:
    symbol: str
    last_price: float
    avg_daily_turnover_rupees: float

@dataclass
class MarketRegime:
    trend: str     # e.g., "bullish", "bearish", "neutral"
    strength: float # 0.0 to 1.0

def allow_new_long(
    symbol_stats: SymbolStats,
    market_regime: MarketRegime,
    risk: "RiskConfig",
) -> bool:
    """Return True if it's acceptable to open a new long position.
    Rules (v1):
    - Symbol last_price >= risk.min_price_rupees
    - Symbol avg_daily_turnover_rupees >= risk.min_avg_daily_turnover_rupees
    - If market_regime.trend == "bearish" and market_regime.strength > 0.7, disallow new longs.
    - Otherwise allow.
    """
    if symbol_stats.last_price < risk.min_price_rupees:
        return False
        
    if symbol_stats.avg_daily_turnover_rupees < risk.min_avg_daily_turnover_rupees:
        return False
        
    if market_regime.trend == "bearish" and market_regime.strength > 0.7:
        return False
        
    return True

def sizing_market_factor(market_regime: MarketRegime, risk: "RiskConfig") -> float:
    """Return a multiplicative factor for position size based on market regime.
    - If trend == "bearish" and strength > 0.7 -> risk.weak_market_size_factor
    - Else -> risk.strong_market_size_factor
    """
    if market_regime.trend == "bearish" and market_regime.strength > 0.7:
        return risk.weak_market_size_factor
    return risk.strong_market_size_factor

def calculate_position_size(
    symbol_stats: SymbolStats,
    market_regime: MarketRegime,
    risk: "RiskConfig",
) -> int:
    """Calculate integer share quantity for a new LONG position.
 
    Steps:
    - Base rupee allocation = risk.per_trade_capital_rupees.
    - Scale by sizing_market_factor() based on market_regime.
    - Compute raw_qty = floor( scaled_capital / symbol_stats.last_price ).
    - Clamp to [risk.min_shares_per_trade, risk.max_shares_per_trade].
    - If result < 1, return 0 (meaning: do not trade).
    """
    if symbol_stats.last_price <= 0:
        return 0
        
    factor = sizing_market_factor(market_regime, risk)
    scaled_capital = risk.per_trade_capital_rupees * factor
    
    raw_qty = int(scaled_capital / symbol_stats.last_price)
    
    qty = max(risk.min_shares_per_trade, min(raw_qty, risk.max_shares_per_trade))
    
    return qty if qty >= 1 else 0
