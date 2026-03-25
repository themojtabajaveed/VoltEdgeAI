"""
log_daily_performance.py — End-of-Day Performance Logger
---------------------------------------------------------
Runs at 15:30+ IST. Fetches daily OHLCV for the trading universe via Kite API,
computes technicals, cross-references with Juror signals, and logs everything
to the daily_performance_snapshots table.

Previously used yfinance — fully replaced by Kite API as of Phase H.
"""
from datetime import date
from typing import List

import pandas as pd

from src.db import init_db, SessionLocal, DailyPerformanceSnapshot, JurorSignal
from src.sources.nse_prices import fetch_daily_ohlcv, compute_ema, compute_rsi, compute_avg_volume
from src.sniper.core import compute_macd, compute_adx, compute_bollinger_bands

NSE_UNIVERSE = ["TCS", "HDFCBANK", "RELIANCE", "INFY", "ICICIBANK"]
TOP_N = 10  # number of top gainers/losers to log


def get_today_ohlc_for_symbols(symbols: List[str]) -> pd.DataFrame:
    """
    Fetch today's daily OHLCV for a list of NSE symbols via Kite API.
    Returns a DataFrame with index as symbol and columns: open, high, low, close, volume, pct_change, gap_pct.
    """
    records = []
    
    for s in symbols:
        try:
            df = fetch_daily_ohlcv(s, days=5)
            if df is None or len(df) < 2:
                continue
            
            prev_close = float(df['close'].iloc[-2])
            today = df.iloc[-1]
            c_open = float(today['open'])
            c_high = float(today['high'])
            c_low = float(today['low'])
            c_close = float(today['close'])
            c_vol = float(today['volume'])
            
            pct_change = ((c_close / prev_close) - 1.0) * 100.0
            gap_pct = ((c_open / prev_close) - 1.0) * 100.0
            
            records.append({
                'symbol': s,
                'open': c_open, 'high': c_high, 'low': c_low, 'close': c_close, 'volume': c_vol,
                'pct_change': pct_change, 'gap_pct': gap_pct
            })
        except Exception as e:
            print(f"Failed to process {s}: {e}")
                
    df_out = pd.DataFrame(records)
    if not df_out.empty:
        df_out = df_out.set_index('symbol')
    return df_out


def main():
    print("Initializing Database...")
    init_db()

    today_val = date.today()
    print(f"Fetching daily OHLC for {len(NSE_UNIVERSE)} symbols ({today_val})...")
    
    df_today = get_today_ohlc_for_symbols(NSE_UNIVERSE)
    if df_today.empty:
        print("No data fetched or missing sufficient history. Exiting.")
        return
        
    df_today = df_today.sort_values(by="pct_change", ascending=False)
    
    gainers = df_today.head(TOP_N)
    losers = df_today.tail(TOP_N).sort_values(by="pct_change", ascending=True)
    
    # Keep track of what we've processed to avoid duplicates if universe is small
    processed = set()
    rows_to_save = []
    
    def process_group(group_df, side_label):
        count = 0
        for symbol, row in group_df.iterrows():
            if symbol in processed:
                continue
            processed.add(symbol)
            
            print(f"  -> {symbol} ({row['pct_change']:.2f}%)")
            
            # Fetch 250d history for technicals
            hist_df = fetch_daily_ohlcv(symbol, days=250)
            if hist_df is None or hist_df.empty or len(hist_df) < 50:
                print(f"     Skipping {symbol}: insufficient technical history")
                continue
                
            close = hist_df["close"]
            volume = hist_df["volume"]
            high = hist_df["high"]
            low = hist_df["low"]
            
            ema_200 = compute_ema(close, 200)
            rsi_14 = compute_rsi(close, 14)
            vol_20 = compute_avg_volume(volume, 20)
            
            c_ema_200 = float(ema_200) if ema_200 is not None else None
            c_rsi_14 = float(rsi_14) if rsi_14 is not None else None
            c_vol_20 = float(vol_20) if vol_20 is not None else None
            
            c_close = float(row['close'])
            c_volume = float(row['volume'])
            
            vol_mult = (c_volume / c_vol_20) if c_vol_20 and c_vol_20 > 0 else None
            above_200 = (c_close > c_ema_200) if c_ema_200 is not None else False
            
            macd_line, macd_signal, macd_hist = compute_macd(close)
            upper_bb, lower_bb, sma_bb, bandwidth = compute_bollinger_bands(close, 20, 2.0)
            adx, plus_di, minus_di = compute_adx(high, low, close, 14)
            
            c_macd = float(macd_line.iloc[-1])
            c_macd_sig = float(macd_signal.iloc[-1])
            c_macd_hist = float(macd_hist.iloc[-1])
            
            c_adx = float(adx.iloc[-1])
            c_pdi = float(plus_di.iloc[-1])
            c_mdi = float(minus_di.iloc[-1])
            
            c_upper = float(upper_bb.iloc[-1])
            c_lower = float(lower_bb.iloc[-1])
            c_middle = float(sma_bb.iloc[-1])
            bb_pos = (c_close - c_middle) / (c_upper - c_lower) if (c_upper - c_lower) > 0 else None
            
            # Query DB for matching JurorSignal today
            had_juror = False
            j_label = None
            j_conf = None
            
            with SessionLocal() as juror_session:
                juror_match = (
                    juror_session.query(JurorSignal)
                    .filter(JurorSignal.symbol == symbol)
                    .filter(JurorSignal.created_at >= today_val.strftime("%Y-%m-%d 00:00:00"))
                    .filter(JurorSignal.created_at <= today_val.strftime("%Y-%m-%d 23:59:59"))
                    .order_by(JurorSignal.confidence.desc())
                    .first()
                )
                
                if juror_match:
                    had_juror = True
                    j_label = juror_match.label
                    j_conf = juror_match.confidence
            
            snapshot = DailyPerformanceSnapshot(
                date=today_val,
                symbol=symbol,
                side=side_label,
                pct_change=float(row['pct_change']),
                gap_pct=float(row['gap_pct']),
                open_price=float(row['open']),
                high_price=float(row['high']),
                low_price=float(row['low']),
                close_price=c_close,
                volume=c_volume,
                vol_20=c_vol_20,
                volume_multiple=vol_mult,
                rsi_14=c_rsi_14,
                ema_200=c_ema_200,
                above_200ema=above_200,
                macd=c_macd,
                macd_signal=c_macd_sig,
                macd_hist=c_macd_hist,
                adx_14=c_adx,
                plus_di=c_pdi,
                minus_di=c_mdi,
                bb_upper=c_upper,
                bb_lower=c_lower,
                bb_middle=c_middle,
                bb_pos=bb_pos,
                had_juror_signal=had_juror,
                juror_label=j_label,
                juror_confidence=j_conf
            )
            rows_to_save.append(snapshot)
            count += 1
        return count

    print("\nProcessing Top Gainers...")
    g_count = process_group(gainers, "gainer")
    
    print("\nProcessing Top Losers...")
    l_count = process_group(losers, "loser")
    
    with SessionLocal() as session:
        for r in rows_to_save:
            session.add(r)
        session.commit()
    
    print(f"\nSuccessfully logged {g_count} gainers and {l_count} losers into daily_performance_snapshots.")

if __name__ == "__main__":
    main()
