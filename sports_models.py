from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship

# IF THIS ERRORS, CHANGE 'database' TO 'app' (e.g., from app import Base)
from app import Base  


class Fixture(Base):
    __tablename__ = "fixtures"

    id = Column(Integer, primary_key=True, index=True)
    api_fixture_id = Column(Integer, unique=True, index=True)
    league = Column(String(200), index=True)
    home_team = Column(String(200))
    away_team = Column(String(200))
    home_team_id = Column(Integer)
    away_team_id = Column(Integer)
    kickoff = Column(DateTime, index=True)
    status = Column(String(20), index=True)
    home_goals = Column(Integer)
    away_goals = Column(Integer)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    predictions = relationship("Prediction", back_populates="fixture")


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, index=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"), index=True)
    market = Column(String(50), index=True)
    pick = Column(String(100))
    probability = Column(Float)
    confidence_score = Column(Integer)
    confidence_label = Column(String(20))
    status = Column(String(20), default="pending", index=True)
    settled_at = Column(DateTime)
    created_at = Column(DateTime)

    fixture = relationship("Fixture", back_populates="predictions")


class OddsInsight(Base):
    __tablename__ = "odds_insights"

    id = Column(Integer, primary_key=True, index=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"))
    market = Column(String(50))
    bookmaker_odds = Column(Float)
    market_prob = Column(Float)
    model_prob = Column(Float)
    value_edge = Column(Float)
    is_value_bet = Column(Boolean, default=False)
    created_at = Column(DateTime)


class RatingsCache(Base):
    __tablename__ = "ratings_cache"

    id = Column(Integer, primary_key=True, index=True)
    cache_key = Column(String(100), unique=True, index=True)
    updated = Column(String(20))
    teams_json = Column(Text)


class AutomationState(Base):
    __tablename__ = "automation_state"

    id = Column(Integer, primary_key=True, index=True)
    state_key = Column(String(50), unique=True, index=True)
    state_value = Column(String(100))