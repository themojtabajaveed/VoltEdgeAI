from dataclasses import dataclass
import os

@dataclass
class RiskConfig:
    live_mode: bool
    max_trades_per_day: int
    max_daily_loss_rupees: float
    per_trade_capital_rupees: float  # e.g. 100 or 500 to start
    max_open_positions: int
    intraday_stop_pct: float = 0.01
    intraday_exit_time: str = "15:20"
    min_shares_per_trade: int = 1
    max_shares_per_trade: int = 200
    weak_market_size_factor: float = 0.5
    strong_market_size_factor: float = 1.0
    min_avg_daily_turnover_rupees: float = 2000000.0
    min_price_rupees: float = 50.0

def load_risk_config() -> RiskConfig:
    """
    Load risk config from environment variables with sane defaults.
    For example:
    - VOLTEDGE_LIVE_MODE: "0" or "1"
    - VOLTEDGE_MAX_TRADES_PER_DAY: default 5
    - VOLTEDGE_MAX_DAILY_LOSS: default 500.0
    - VOLTEDGE_PER_TRADE_CAPITAL: default 100.0
    - VOLTEDGE_MAX_OPEN_POSITIONS: default 3
    """
    
    live_str = os.getenv("VOLTEDGE_LIVE_MODE", "0")
    live_mode = True if live_str in ["1", "true", "True", "TRUE"] else False
    
    # Safely cast integers with a fallback
    try:
        max_trades = int(os.getenv("VOLTEDGE_MAX_TRADES_PER_DAY", "5"))
    except ValueError:
        max_trades = 5
        
    try:
        max_positions = int(os.getenv("VOLTEDGE_MAX_OPEN_POSITIONS", "3"))
    except ValueError:
        max_positions = 3
        
    # Safely cast floats with a fallback
    try:
        max_loss = float(os.getenv("VOLTEDGE_MAX_DAILY_LOSS", "500.0"))
    except ValueError:
        max_loss = 500.0
        
    try:
        trade_capital = float(os.getenv("VOLTEDGE_PER_TRADE_CAPITAL", "100.0"))
    except ValueError:
        trade_capital = 100.0
        
    try:
        stop_pct = float(os.getenv("VOLTEDGE_INTRADAY_STOP_PCT", "0.01"))
    except ValueError:
        stop_pct = 0.01
        
    exit_time = os.getenv("VOLTEDGE_INTRADAY_EXIT_TIME", "15:20")
    
    try:
        min_shares = int(os.getenv("VOLTEDGE_MIN_SHARES_PER_TRADE", "1"))
    except ValueError:
        min_shares = 1

    try:
        max_shares = int(os.getenv("VOLTEDGE_MAX_SHARES_PER_TRADE", "200"))
    except ValueError:
        max_shares = 200

    try:
        weak_factor = float(os.getenv("VOLTEDGE_WEAK_MARKET_SIZE_FACTOR", "0.5"))
    except ValueError:
        weak_factor = 0.5

    try:
        strong_factor = float(os.getenv("VOLTEDGE_STRONG_MARKET_SIZE_FACTOR", "1.0"))
    except ValueError:
        strong_factor = 1.0

    try:
        min_turnover = float(os.getenv("VOLTEDGE_MIN_AVG_DAILY_TURNOVER_RUPEES", "2000000.0"))
    except ValueError:
        min_turnover = 2_000_000.0

    try:
        min_price = float(os.getenv("VOLTEDGE_MIN_PRICE_RUPEES", "50.0"))
    except ValueError:
        min_price = 50.0

    return RiskConfig(
        live_mode=live_mode,
        max_trades_per_day=max_trades,
        max_daily_loss_rupees=max_loss,
        per_trade_capital_rupees=trade_capital,
        max_open_positions=max_positions,
        intraday_stop_pct=stop_pct,
        intraday_exit_time=exit_time,
        min_shares_per_trade=min_shares,
        max_shares_per_trade=max_shares,
        weak_market_size_factor=weak_factor,
        strong_market_size_factor=strong_factor,
        min_avg_daily_turnover_rupees=min_turnover,
        min_price_rupees=min_price
    )
