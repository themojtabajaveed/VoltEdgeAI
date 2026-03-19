import sys
import os

# Ensure the root directory logic works locally when running from CLI anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import init_db, SessionLocal, JurorSignal
from juror.gemini_client import classify_announcement

def main():
    print("Initializing Database...")
    init_db()

    announcements = [
        {
            "source": "manual_demo",
            "symbol": "ABC",
            "text": "ABC Ltd wins a 500 crore order from Government of India."
        },
        {
            "source": "manual_demo",
            "symbol": "XYZ",
            "text": "XYZ Industries reports a 20% decline in quarterly profits due to rising input costs."
        },
        {
            "source": "manual_demo",
            "symbol": "DEF",
            "text": "DEF Corp announces a strategic partnership with a leading tech firm."
        },
        {
            "source": "manual_demo",
            "symbol": "LMN",
            "text": "LMN faces a lawsuit regarding environmental violations at their primary plant."
        }
    ]

    for item in announcements:
        text = item["text"]
        symbol = item["symbol"]
        source = item["source"]

        try:
            print(f"\nProcessing {symbol}: {text[:50]}...")
            result = classify_announcement(text)

            label = result.get("label", "Unknown")
            confidence = result.get("confidence", 0.0)
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
            
            shortened_reason = reason[:80] + ("..." if len(reason) > 80 else "")
            print(f"{symbol} | {label} ({confidence:.2f}) - {shortened_reason}")

        except Exception as e:
            print(f"Failed to process {symbol}: {e}")

if __name__ == "__main__":
    main()
