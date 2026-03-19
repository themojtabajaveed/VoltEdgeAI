from jugaad_data.nse import NSELive

def is_potentially_price_moving(text: str) -> bool:
    """Return True only if this announcement text looks like it could move the stock price intraday."""
    text_lower = text.lower()
    
    exclude_keywords = [
        "copy of newspaper publication",
        "trading window",
        "intimation of board meeting",
        "intimation regarding board meeting",
        "closure of trading window",
        "newspaper publication",
        "employee stock option",
        "esop",
        "esos",
        "employee stock purchase",
        "allotment of equity shares under esop"
    ]
    
    for phrase in exclude_keywords:
        if phrase in text_lower:
            return False
            
    include_keywords = [
        "order", "contract", "wins", "award", "allotment", "merger", "acquisition", "scheme of arrangement",
        "dividend", "buyback", "bonus", "split",
        "profit", "loss", "q1", "q2", "q3", "q4", "financial results",
        "approval", "license", "approval received"
    ]
    
    for phrase in include_keywords:
        if phrase in text_lower:
            return True
            
    return False

def fetch_latest_announcements(limit: int = 10) -> list[dict]:
    """
    Fetch latest NSE corporate announcements and return a list of dicts
    with keys: source, symbol, text, raw (original dict).
    """
    n = NSELive()
    data = n.corporate_announcements()

    if not data:
        return []

    results = []
    
    for item in data[:limit]:
        symbol = item.get("symbol")
        attchmntText = item.get("attchmntText", "")
        desc = item.get("desc", "")
        
        # Prefer attachment text, otherwise fallback to description
        text = attchmntText if attchmntText else desc
        
        if is_potentially_price_moving(text):
            results.append({
                "source": "nse_live",
                "symbol": symbol,
                "text": text,
                "raw": item
            })
        
    return results
