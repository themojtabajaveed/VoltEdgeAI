import csv
import os
from datetime import datetime
from typing import Dict

LOG_PATH = os.path.join("logs", "sniper_decisions.csv")

def log_sniper_decision(symbol: str, res: Dict, context: Dict) -> None:
    """Append one row with Sniper decision and context to a CSV file."""
    os.makedirs("logs", exist_ok=True)
    
    file_exists = os.path.isfile(LOG_PATH)
    
    # Define the fields we specifically want to track
    fieldnames = [
        'timestamp', 'symbol', 'status', 'reason',
        'close', 'ema_200', 'rsi_14', 'vol_today', 'vol_20',
        'macd_hist', 'adx_14', 'juror_label', 'juror_confidence',
        'antigravity_status', 'antigravity_z_score'
    ]
    
    ag = res.get('antigravity', {})
    
    # Extract known keys handling missing values gracefully
    row_data = {
        'timestamp': datetime.now().isoformat(),
        'symbol': symbol,
        'status': res.get('status', ''),
        'reason': res.get('reason', ''),
        'close': res.get('close', ''),
        'ema_200': res.get('ema_200', ''),
        'rsi_14': res.get('rsi_14', ''),
        'vol_today': res.get('vol_today', ''),
        'vol_20': res.get('vol_20', ''),
        'macd_hist': res.get('macd_hist', ''),
        'adx_14': res.get('adx_14', ''),
        'juror_label': context.get('juror_label', ''),
        'juror_confidence': context.get('juror_confidence', ''),
        'antigravity_status': ag.get('status', ''),
        'antigravity_z_score': ag.get('z_score', '')
    }
    
    # Capture any extra fields present in res that are not explicitly defined
    extra_fields = {k: v for k, v in res.items() if k not in row_data and k != 'symbol' and k != 'antigravity'}
    
    # If there are extra fields, dynamically update the fieldnames (might cause ragged CSV if not careful, 
    # but the instructions specified "plus any extra fields if present in res")
    for key in extra_fields:
        if key not in fieldnames:
            fieldnames.append(key)
    row_data.update(extra_fields)

    with open(LOG_PATH, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_data)
