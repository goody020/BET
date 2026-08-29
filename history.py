from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db
from sports_models import Prediction, Fixture
from picks import HIGH_ODDS_ONLY, SAFE_MARKET_LABELS


router = APIRouter(prefix="/history", tags=["history"])


def kickoff_key(fixture):
    return fixture.kickoff or datetime(1970, 1, 1)


@router.get("/wins")
def get_wins(limit: int = 20, db: Session = Depends(get_db)):
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won"
    ).order_by(Prediction.id.desc()).limit(300).all()

    # One headline win per match: the biggest-odds win (lowest probability)
    best = {}
    for pred, fixture in rows:
        cur = best.get(fixture.id)
        if cur is None or pred.probability < cur[0].probability:
            best[fixture.id] = (pred, fixture)

    chosen = sorted(best.values(), key=lambda pf: kickoff_key(pf[1]), reverse=True)

    return [
        {
            "market": pred.market,
            "pick": pred.pick,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "league": fixture.league,
        }
        for pred, fixture in chosen[:limit]
    ]


@router.get("/results")
def get_results(limit: int = 30, db: Session = Depends(get_db)):
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status.in_(["won", "lost"])
    ).order_by(Prediction.id.desc()).limit(300).all()

    # One line per match: the main pick (highest probability)
    best = {}
    for pred, fixture in rows:
        cur = best.get(fixture.id)
        if cur is None or pred.probability > cur[0].probability:
            best[fixture.id] = (pred, fixture)

    chosen = sorted(best.values(), key=lambda pf: kickoff_key(pf[1]), reverse=True)

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
        for pred, fixture in chosen[:limit]
    ]


@router.get("/high-odds-wins")
def get_high_odds_wins(limit: int = 20, db: Session = Depends(get_db)):
    """The itemized receipts behind the high-odds record (e.g. 11/76):
    every won prediction in a premium high-odds market, newest settled first."""
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won",
        Prediction.market.in_(HIGH_ODDS_ONLY),
    ).order_by(Prediction.settled_at.desc(), Prediction.id.desc()).limit(limit).all()

    return [
        {
            "market": pred.market,
            "market_label": SAFE_MARKET_LABELS.get(pred.market, pred.market),
            "pick": pred.pick,
            "probability": pred.probability,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "settled_at": pred.settled_at.isoformat() if pred.settled_at else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "league": fixture.league,
            "score": f"{fixture.home_goals}-{fixture.away_goals}" if fixture.home_goals is not None else "-",
        }
        for pred, fixture in rows
    ]
