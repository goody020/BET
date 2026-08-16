from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db
from sports_models import Prediction, Fixture


router = APIRouter(prefix="/history", tags=["history"])


@router.get("/wins")
def get_wins(limit: int = 20, db: Session = Depends(get_db)):
    # Fetch recent predictions that were settled as "won"
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won"
    ).order_by(Prediction.id.desc()).limit(limit).all()

    return [
        {
            "market": pred.market,
            "pick": pred.pick,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "league": fixture.league,
        }
        for pred, fixture in rows
    ]
@router.get("/results")
def get_results(limit: int = 30, db: Session = Depends(get_db)):
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status.in_(["won", "lost"])
    ).order_by(Prediction.id.desc()).limit(limit).all()

    return [
        {
            "market": pred.market,
            "pick": pred.pick,
            "status": pred.status,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "score": f"{fixture.home_goals}-{fixture.away_goals}" if fixture.home_goals is not None else "-",
        }
        for pred, fixture in rows
    ]