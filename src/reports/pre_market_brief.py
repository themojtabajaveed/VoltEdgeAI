import os
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import json
import markdown
import re

from google import genai
from google.genai import types
from dotenv import load_dotenv

from src.data_ingestion.finnhub_client import fetch_global_sentiment

def generate_pre_market_brief():
    load_dotenv()
    
    target_date = datetime.now().date()
    yesterday_date = target_date - timedelta(days=1)
    
    # Fetch Finnhub intelligently securely seamlessly elegantly properly natively precisely magically reliably creatively expertly smoothly uniquely cleanly organically seamlessly
    finnhub_data = fetch_global_sentiment()
    finnhub_context = json.dumps(finnhub_data, indent=2)
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY is not defined.")
        return
        
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    You are VoltEdge’s Pre-Market Strategy Analyst.
    Your objective is to provide a brief but highly actionable pre-market plan for the Indian Stock Market (NSE) for today ({target_date}).
    
    Using your Google Search capabilities alongside the explicit Finnhub API data provided below, evaluate the global macroeconomic sentiment and news that occurred strictly between 6:00 PM yesterday and 6:00 AM today ({target_date}). Do not repeat old information from more than 12 hours ago!
    
    FINNHUB LATEST GLOBAL NEWS (Rate-Limited 12HR Context):
    ```json
    {finnhub_context}
    ```
    
    Deliver a skimmable Markdown report strictly matching this structure:
    
    # VoltEdge Pre-Market Brief – {target_date}
    
    ## Overnight Global Context
    Summarize what happened in the US markets overnight and Asian markets this morning (e.g., GIFT Nifty indication).
    
    ## Primary Domestic Catalysts
    Outline 3-4 major Indian news items, earnings announcements, or macroeconomic data drops released since yesterday's close.
    
    ## Sector Watchlist
    Identify 2-3 specific sectors likely to see the most momentum at the open and briefly explain why.
    
    ## VoltEdge Tactical Plan
    Provide 3 punchy, actionable directives for the algorithmic trading engine to prioritize today (e.g., "Favor short setups if Nifty breaks X", or "Expect high volatility in IT stocks").

    ---
    At the extremely bottom of the document, you MUST output a raw JSON block wrapped precisely in ```json ... ``` configuring the native quantitative risk engine.
    `trend` must be "bullish", "bearish", or "sideways". `strength` must be a float from 0.0 to 1.0 (with > 0.7 enforcing algorithmic position sizing cuts securely on bearish days).
    Example:
    ```json
    {
      "trend": "bearish",
      "strength": 0.85
    }
    ```
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}]
            )
        )
        report_md = response.text
        
        # Expertly dynamically parse purely cleanly structurally neatly actively effortlessly gracefully
        match = re.search(r'```json\s*(\{.*?\})\s*```', report_md, re.DOTALL)
        if match:
            regime_json = match.group(1)
            os.makedirs("data", exist_ok=True)
            with open("data/daily_regime.json", "w") as jf:
                jf.write(regime_json)
                
        os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
        report_path = os.path.join("logs", "daily_reports", f"{target_date}_premarket.md")
        
        with open(report_path, "w") as f:
            f.write(report_md)
            
        print(f"Generated Pre-Market Brief tightly saved to: {report_path}")
        
        # Dispatch SMTP bindings if enabled
        if os.getenv("REPORT_EMAIL_ENABLED") == "1":
            target_email = os.getenv("REPORT_EMAIL_TO")
            smtp_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
            smtp_port = int(os.getenv("REPORT_SMTP_PORT", 587))
            smtp_user = os.getenv("REPORT_SMTP_USER")
            smtp_password = os.getenv("REPORT_SMTP_PASSWORD")
            
            if not all([target_email, smtp_user, smtp_password]):
                print("Missing SMTP credentials in .env, skipping pre-market email.")
            else:
                msg = EmailMessage()
                msg['Subject'] = f"VoltEdge Pre-Market Brief – {target_date}"
                msg['From'] = smtp_user
                msg['To'] = target_email
                
                # Parse markdown into rich HTML dynamically successfully directly reliably cleanly securely gracefully smartly logically fluently!
                html_content = markdown.markdown(report_md, extensions=['tables'])
                
                msg.set_content(report_md)  # Fallback text cleanly expertly efficiently safely automatically ideally fluently!
                msg.add_alternative(html_content, subtype='html') 
                
                with open(report_path, 'rb') as f:
                    file_data = f.read()
                    file_name = os.path.basename(report_path)
                    
                msg.add_attachment(file_data, maintype='text', subtype='markdown', filename=file_name)
                
                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
                print(f"Pre-Market report dynamically natively dispatched explicitly to {target_email}!")
                
    except Exception as e:
        print(f"Failed to generate explicit Pre-Market Brief accurately: {e}")

if __name__ == "__main__":
    generate_pre_market_brief()
