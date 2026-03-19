import os
from dotenv import load_dotenv
load_dotenv()

from src.db import init_db, SessionLocal, JurorSignal
from src.juror.gemini_client import classify_announcement
from src.sources.nse_announcements import fetch_latest_announcements

def main():
    print("Initializing Database...")
    init_db()

    print("Fetching latest NSE announcements...")
    announcements = fetch_latest_announcements(limit=10)

    if not announcements:
        print("No announcements fetched from NSE.")
        return

    for ann in announcements:
        source = ann["source"]
        symbol = ann["symbol"]
        text = ann["text"]

        print(f"Processing {symbol}: {text[:80]}...")

        try:
            result = classify_announcement(text)
            
            label = result.get("label", "Unknown")
            confidence = float(result.get("confidence", 0.0))
            reason = result.get("reason", "No reason provided")

            with SessionLocal() as session:
                signal = JurorSignal(
                    source=source,
                    symbol=symbol,
                    raw_text=text,
                    label=label,
                    confidence=confidence,
                    reason=reason
                )
                session.add(signal)
                session.commit()

            print(f"{symbol} | {label} ({confidence:.2f}) - {reason[:80]}")

        except Exception as e:
            print(f"Error processing {symbol}: {e}")

if __name__ == "__main__":
    main()
