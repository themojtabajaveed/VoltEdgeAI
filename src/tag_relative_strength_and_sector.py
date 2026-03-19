from typing import Optional
from src.db import init_db, SessionLocal, FundamentalUniverse
from src.sources.nse_prices import fetch_daily_ohlcv

def compute_52w_return(symbol: str) -> Optional[float]:
    """Return simple % price change over last ~252 trading days using fetch_daily_ohlcv."""
    df = fetch_daily_ohlcv(symbol, days=260)
    
    if df is None or df.empty or len(df) < 252:
        return None
        
    start_price = float(df['close'].iloc[-252])
    end_price = float(df['close'].iloc[-1])
    
    if start_price <= 0:
        return None
        
    return ((end_price / start_price) - 1.0) * 100.0

def get_sector_for_symbol(symbol: str) -> str:
    """Temporary stub mapping for sector name; returns e.g. 'IT', 'BANK', etc."""
    mapping = {
        "TCS": "IT",
        "INFY": "IT",
        "HDFCBANK": "BANK",
        "ICICIBANK": "BANK",
        "RELIANCE": "ENERGY",
        "ONGC": "ENERGY"
    }
    return mapping.get(symbol, "UNKNOWN")

def main():
    print("Initializing Database...")
    init_db()

    with SessionLocal() as session:
        rows = session.query(FundamentalUniverse).all()
        
        if not rows:
            print("No rows found in Fundamental Universe.")
            return
            
        print(f"Loaded {len(rows)} symbol(s) from Fundamental Universe. Computing 52W returns...")
        
        # Calculate returns and store them
        returns_map = {}
        for row in rows:
            ret = compute_52w_return(row.symbol)
            if ret is not None:
                returns_map[row.symbol] = ret
                
            # Set the static temporary properties
            row.sector = get_sector_for_symbol(row.symbol)
            row.sector_trend_ok = True
            
        # Compute Relative Strength Percentiles (0-100)
        valid_returns = list(returns_map.values())
        if valid_returns:
            print("Ranking relative strengths...")
            # Sort the returns to find percentiles
            sorted_returns = sorted(valid_returns)
            n_returns = len(sorted_returns)
            
            for row in rows:
                if row.symbol in returns_map:
                    ret = returns_map[row.symbol]
                    # Find rank (0-indexed position in sorted array)
                    # For duplicate values, list.index finds first occurrence, which is fine for small sets
                    rank = sorted_returns.index(ret) 
                    
                    # Percentile formula: (Rank / (N - 1)) * 100
                    # Handle edge case where there's only 1 item
                    if n_returns > 1:
                        rs_score = (rank / (n_returns - 1)) * 100
                    else:
                        rs_score = 100.0
                        
                    row.rs_52w = rs_score
                else:
                    row.rs_52w = None
        else:
            print("Warning: Could not compute returns for any symbols.")
            
        session.commit()
        print(f"Successfully processed and updated {len(rows)} row(s).")

if __name__ == "__main__":
    main()
