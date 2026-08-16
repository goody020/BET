from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey, Boolean
from datetime import datetime, timezone
from app import Base


class Fixture(Base):
    __tablename__ = "fixtures"

    id = Column(Integer, primary_key=True, index=True)
    api_fixture_id = Column(Integer, unique=True, index=True)
    
    league = Column(String(255))
    home_team = Column(String(255))
    away_team = Column(String(255))
    
    kickoff = Column(DateTime)
    status = Column(String(50), default="NS") # NS = Not Started
    
    home_goals = Column(Integer, nullable=True)
    away_goals = Column(Integer, nullable=True)
    
    ht_home_goals = Column(Integer, nullable=True)
    ht_away_goals = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, index=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"))
    
    market = Column(String(50)) # "CS" or "HTFT"
    pick = Column(String(255))    
    probability = Column(Float)
    confidence_score = Column(Integer)
    confidence_label = Column(String(50))
    
    status = Column(String(50), default="pending") # pending, won, lost, void
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class OddsInsight(Base):
    __tablename__ = "odds_insights"

    id = Column(Integer, primary_key=True, index=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"))
    
    market = Column(String(50))          # e.g., OVER_25, DC_1X
    bookmaker_odds = Column(Float)       # Average decimal odds
    market_prob = Column(Float)          # True probability after removing bookie margin
    model_prob = Column(Float)           # Our Poisson model probability
    value_edge = Column(Float)           # model_prob - market_prob
    is_value_bet = Column(Boolean, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AutomationState(Base):
    __tablename__ = "automation_state"

    id = Column(Integer, primary_key=True, index=True)
    state_key = Column(String(50), unique=True, index=True)
    state_value = Column(String(100))