from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, Boolean, Date
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base, sessionmaker

# 1. Create an engine for a local SQLite database
engine = create_engine("sqlite:///voltedgeai.db")

# 2. Create a SessionLocal factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 3. Define a Base class
Base = declarative_base()

# 4. Model class for Juror outputs
class JurorSignal(Base):
    __tablename__ = "juror_signals"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String)
    symbol = Column(String)
    raw_text = Column(Text)
    label = Column(String)
    confidence = Column(Float)
    reason = Column(Text)

# 5. Helper function to initialize the database
def init_db():
    Base.metadata.create_all(bind=engine)

# 6. Model class for Daily Performance Snapshots
class DailyPerformanceSnapshot(Base):
    __tablename__ = "daily_performance_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, index=True, nullable=False)
    symbol = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)  # "gainer", "loser", or "sample"

    pct_change = Column(Float, nullable=True)
    gap_pct = Column(Float, nullable=True)

    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)

    volume = Column(Float, nullable=True)
    vol_20 = Column(Float, nullable=True)
    volume_multiple = Column(Float, nullable=True)

    rsi_14 = Column(Float, nullable=True)
    ema_200 = Column(Float, nullable=True)
    above_200ema = Column(Boolean, default=False)

    macd = Column(Float, nullable=True)
    macd_signal = Column(Float, nullable=True)
    macd_hist = Column(Float, nullable=True)

    adx_14 = Column(Float, nullable=True)
    plus_di = Column(Float, nullable=True)
    minus_di = Column(Float, nullable=True)

    bb_upper = Column(Float, nullable=True)
    bb_lower = Column(Float, nullable=True)
    bb_middle = Column(Float, nullable=True)
    bb_pos = Column(Float, nullable=True)  # normalized: (close - middle) / (upper - lower)

    had_juror_signal = Column(Boolean, default=False)
    juror_label = Column(String, nullable=True)
    juror_confidence = Column(Float, nullable=True)

    created_at = Column(DateTime, default=func.now())

# 7. Model class for Fundamental Universe
class FundamentalUniverse(Base):
    __tablename__ = "fundamental_universe"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, unique=True)
    name = Column(String, nullable=True)
    market_cap = Column(Float, nullable=True)
    eps_growth_ttm = Column(Float, nullable=True)
    eps_growth_qoq_3q = Column(Float, nullable=True)
    sales_growth_qoq_3q = Column(Float, nullable=True)
    margin_growth_qoq_3q = Column(Float, nullable=True)
    roce = Column(Float, nullable=True)
    roe = Column(Float, nullable=True)
    de_ratio = Column(Float, nullable=True)
    promoter_pledge_pct = Column(Float, nullable=True)
    institutional_holding_pct = Column(Float, nullable=True)
    rs_52w = Column(Float, nullable=True)
    sector = Column(String, nullable=True)
    sector_trend_ok = Column(Boolean, default=False)
    macro_ok = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    last_updated = Column(DateTime, default=func.now(), onupdate=func.now())

# 8. Helper function to get fundamental universe
def get_fundamental_universe(session):
    """Return a query for active FundamentalUniverse rows (is_active=True, macro_ok=True)."""
    return session.query(FundamentalUniverse).filter_by(is_active=True, macro_ok=True)
