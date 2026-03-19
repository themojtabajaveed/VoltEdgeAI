import os
import csv
from db import init_db, SessionLocal, JurorSignal
from sniper.core import evaluate_signal

def main():
    print("Initializing Database...")
    init_db()

    print("Reading recent Juror signals and applying Sniper rules...")
    
    with SessionLocal() as session:
        # Query the 20 most recent signals
        recent_signals = (
            session.query(JurorSignal)
            .order_by(JurorSignal.created_at.desc())
            .limit(20)
            .all()
        )

        if not recent_signals:
            print("No signals found in the database.")
            return

        log_file = "logs/sniper_decisions.csv"
        file_exists = os.path.isfile(log_file)
        
        with open(log_file, mode='a', newline='') as csvfile:
            fieldnames = ['symbol', 'status', 'reason', 'close', 'ema_200', 'rsi_14', 'vol_today', 'vol_20']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            if not file_exists:
                writer.writeheader()

            for row in recent_signals:
                confidence = row.confidence if row.confidence is not None else 0.0
                
                # Evaluate using Sniper logic
            result = evaluate_signal(row.symbol)
            
            status = result["status"]
            close = result["close"]
            ema_200 = result["ema_200"]
            rsi_14 = result["rsi_14"]
            vol_today = result["vol_today"]
            vol_20 = result["vol_20"]
            
            # Format numbers safely
            close_str = f"{close:.2f}" if close is not None else "NA"
            ema_str = f"{ema_200:.2f}" if ema_200 is not None else "NA"
            rsi_str = f"{rsi_14:.1f}" if rsi_14 is not None else "NA"
            
            # Use comma formatting for volumes if available
            vol_today_str = f"{vol_today:,.0f}" if vol_today is not None else "NA"
            vol_20_str = f"{vol_20:,.0f}" if vol_20 is not None else "NA"

            print(
                f"{row.symbol} | Juror: {row.label} ({confidence:.2f}) | "
                f"Sniper: {status} | Close={close_str}, "
                f"EMA200={ema_str}, "
                f"RSI14={rsi_str}, "
                f"VolToday={vol_today_str}, Vol20={vol_20_str}"
            )
            print(f"  -> {result['reason']}")
            
            # Log to CSV
            writer.writerow({
                'symbol': row.symbol,
                'status': status,
                'reason': result.get('reason', ''),
                'close': close,
                'ema_200': ema_200,
                'rsi_14': rsi_14,
                'vol_today': vol_today,
                'vol_20': vol_20
            })

if __name__ == "__main__":
    main()
