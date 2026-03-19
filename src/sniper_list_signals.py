from src.db import init_db, SessionLocal, JurorSignal

def main():
    print("Reading recent Juror signals...")
    init_db()

    with SessionLocal() as session:
        # Query the 20 most recent signals, sorted by created_at descending
        recent_signals = (
            session.query(JurorSignal)
            .order_by(JurorSignal.created_at.desc())
            .limit(20)
            .all()
        )

        if not recent_signals:
            print("No signals found in the database.")
            return

        for row in recent_signals:
            confidence = row.confidence if row.confidence is not None else 0.0
            reason = row.reason if row.reason else "No reason"
            
            # Print a one-line summary per requirement
            print(f"{row.created_at} | {row.symbol} | {row.label} ({confidence:.2f}) - {reason[:80]}")

if __name__ == "__main__":
    main()
