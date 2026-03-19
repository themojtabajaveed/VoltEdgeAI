from dataclasses import dataclass
from typing import List, Dict, Optional
import logging

try:
    from src.data_ingestion.market_live import KiteLiveClient, Snapshot
except ImportError:
    KiteLiveClient, Snapshot = None, None

logger = logging.getLogger(__name__)

@dataclass
class IndexSentiment:
    index_name: str        # "NIFTY 50", "BANKNIFTY"
    trend: str             # "bullish" | "bearish" | "sideways"
    strength: float        # 0.0 - 1.0
    pcr: Optional[float]   # put-call ratio if computed
    comment: str

def get_index_snapshot(live_client: "KiteLiveClient", index_symbols: List[str]) -> Dict[str, "Snapshot"]:
    """Use market_live to get current index snapshots."""
    if not live_client:
        return {}
    return live_client.get_snapshot(index_symbols)

def compute_index_sentiment(
    live_client: "KiteLiveClient",
    index_name: str,
    option_instruments: List[str]
) -> IndexSentiment:
    """
    Use live quotes on index + selected option instruments to compute a simple unified sentiment structure explicitly.
    """
    if not live_client:
        return IndexSentiment(index_name, "sideways", 0.0, None, "KiteLiveClient disabled.")
        
    try:
        # 1. Fetch index underlying snapshot dynamically  
        snaps = get_index_snapshot(live_client, [index_name])
        index_snap = snaps.get(index_name)
        
        if not index_snap:
            return IndexSentiment(index_name, "sideways", 0.0, None, f"Could not fetch snapshot for {index_name}")
            
        open_price = index_snap.ohlc.get('open', index_snap.ltp)
        ltp = index_snap.ltp
        
        diff_pct = ((ltp - open_price) / open_price) * 100 if open_price > 0 else 0
        
        if diff_pct > 0.2:
            trend = "bullish"
        elif diff_pct < -0.2:
            trend = "bearish"
        else:
            trend = "sideways"
            
        strength = min(1.0, abs(diff_pct) / 1.0) 
        
        # 2. Calculate PCR arrays tracking Open Interest heavily biased by directional trades
        pcr = None
        if option_instruments:
            opt_snaps = live_client.get_snapshot(option_instruments)
            put_oi = 0
            call_oi = 0
            
            for sym, snap in opt_snaps.items():
                if sym.endswith("CE") and snap.oi:
                    call_oi += snap.oi
                elif sym.endswith("PE") and snap.oi:
                    put_oi += snap.oi
                    
            if call_oi > 0:
                pcr = put_oi / call_oi
                
        comment = f"Index moved {diff_pct:.2f}% relative to Open."
        if pcr is not None:
            comment += f" Option PCR at {pcr:.2f}."
            
        return IndexSentiment(
            index_name=index_name,
            trend=trend,
            strength=strength,
            pcr=pcr,
            comment=comment
        )
        
    except Exception as e:
        logger.error(f"Failed to compute index sentiment logic: {e}")
        return IndexSentiment(index_name, "sideways", 0.0, None, f"Error: {str(e)}")
