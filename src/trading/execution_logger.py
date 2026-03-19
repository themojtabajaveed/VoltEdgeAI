import logging
import os
from src.trading.orders import OrderResult

LOG_PATH = os.path.join("logs", "executions.log")

def get_executions_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("executions")
    # Only adding the handler if it hasn't been attached yet to prevent duplicate lines
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger

def log_execution(logger: logging.Logger, symbol: str, ltp: float, result: OrderResult, meta: dict | None = None) -> None:
    """Log a single execution attempt (DRY_RUN or LIVE)."""
    meta_str = ""
    if meta:
        meta_str = " | Meta: " + ", ".join([f"{k}={v}" for k, v in meta.items()])
        
    status = "SUCCESS" if result.success else "FAILED"
    msg = f"[{status}] {symbol} @ {ltp} | Msg: {result.message}{meta_str}"
    
    if result.success:
        logger.info(msg)
    else:
        logger.error(msg)
