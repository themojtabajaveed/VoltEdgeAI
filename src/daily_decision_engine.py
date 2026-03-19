import os
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime
from src.db import init_db, SessionLocal, JurorSignal
from src.sniper.core import evaluate_signal
from src.sniper.logger import log_sniper_decision
from src.trade_planner import plan_trade
from src.broker.zerodha_client import place_equity_trade_from_plan

ACCOUNT_EQUITY = 100000.0
MIN_JUROR_CONFIDENCE = 0.80

def main(watcher=None):
    print("--- VoltEdgeAI Daily Decision Engine ---")
    print(f"Account Equity configured at ₹{ACCOUNT_EQUITY:,.2f}")
    print(f"Minimum Juror Confidence: {MIN_JUROR_CONFIDENCE:.2f}")
    print("Initializing Database...")
    init_db()

    with SessionLocal() as session:
        # Fetch latest 50 signals
        recent_signals = (
            session.query(JurorSignal)
            .order_by(JurorSignal.created_at.desc())
            .limit(50)
            .all()
        )

        if not recent_signals:
            print("No signals found in the database. Run Juror NSE live script first.")
            return

        print(f"Found {len(recent_signals)} recent signal(s). Filtering for actionable trades...\n")

        for row in recent_signals:
            # Step 1: Filter by Juror criteria
            if row.label != "Positive" or row.confidence is None or row.confidence < MIN_JUROR_CONFIDENCE:
                continue
                
            # Step 2: Apply Sniper technical rules
            res = evaluate_signal(row.symbol)
            status = res["status"]
            
            # Log the decision
            log_sniper_decision(
                symbol=row.symbol,
                res=res,
                context={"juror_label": row.label, "juror_confidence": row.confidence}
            )
            
            if status != "KEEP":
                print(f"{row.symbol} | SKIP | Juror={row.label} ({row.confidence:.2f}), Sniper={status} ({res.get('reason', '')})")
                
                # Check if we should watch this for antigravity bounces
                if status == "WAIT" and res.get("antigravity", {}).get("status") == "WAITING_FOR_GRAVITY":
                    if watcher:
                        ag = res["antigravity"]
                        watcher.add_wait_signal(
                            symbol=row.symbol,
                            z_score=ag.get("z_score", 0.0),
                            vwap=ag.get("vwap", 0.0),
                            ltp=ag.get("ltp", 0.0),
                            now=datetime.now()
                        )
                continue

            # Step 3: Compute Trade Plan
            plan = plan_trade(row.symbol, equity=ACCOUNT_EQUITY)
            
            if plan is None:
                print(f"{row.symbol} | SKIP | Juror={row.label} ({row.confidence:.2f}), Sniper={status} (Trade Plan Generation Failed / Insufficient ATR data)")
                continue

            # Step 4: Output approved "Trade Card"
            print("===")
            print(f"Candidate: {row.symbol}")
            print(f"  Juror:  {row.label} ({row.confidence:.2f})")
            print(f"  Sniper: {status} - {res.get('reason', '')}")
            print(f"  Price:  Close=₹{plan['entry']:.2f}, ATR=₹{plan['atr']:.2f}")
            print(f"  Plan:   Entry=₹{plan['entry']:.2f}, SL=₹{plan['stop_loss']:.2f}, Target=₹{plan['target']:.2f}")
            print(f"  Size:   Qty={plan['qty']}, Risk=₹{plan['risk_amount']:.2f}, Reward=₹{plan['reward_amount']:.2f}, R:R={plan['rr_effective']:.2f}")
            print("===")
            
            place_equity_trade_from_plan(plan, side="BUY", dry_run=True)

if __name__ == "__main__":
    main()
