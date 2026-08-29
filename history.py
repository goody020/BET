from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db
from sports_models import Prediction, Fixture, AutomationState
from picks import HIGH_ODDS_ONLY, SAFE_MARKET_LABELS, PIN_PREFIX


router = APIRouter(prefix="/history", tags=["history"])

# Receipts floor: below 5% model probability a hit is lottery noise, not skill.
# Never celebrate it in public — it was never on a sellable card anyway.
RECEIPT_MIN_PROB = 0.05


def kickoff_key(fixture):
    return fixture.kickoff or datetime(1970, 1, 1)


def _card_anchor(db: Session):
    """(pinned_ids, earliest_pin_day) — the set of prediction IDs that were
    actually published on a daily card, and when card-freezing began.
    (empty set / None) means the freeze feature never ran (legacy/history mode)."""
    pinned_ids = set()
    earliest_day = None
    try:
        rows = db.query(AutomationState).filter(
            AutomationState.state_key.like(f"{PIN_PREFIX}:%")
        ).all()
        for row in rows:
            value = (row.state_value or "").strip()
            if value and value != "-":
                for token in value.split(","):
                    token = token.strip()
                    if token.isdigit():
                        pinned_ids.add(int(token))
            try:
                day = datetime.fromisoformat(row.state_key.rsplit(":", 1)[1]).date()
                earliest_day = day if earliest_day is None else min(earliest_day, day)
            except Exception:
                continue
    except Exception:
        return set(), None
    return pinned_ids, earliest_day


def _is_receipt(pred, fixture, pinned_ids, earliest_day):
    """Was this something we actually stood behind?
    True if the pick sat on a daily card. For matches that predate the
    freeze feature, falls back to the 5% honesty floor."""
    if pred.id in pinned_ids:
        return True
    prob = pred.probability or 0.0
    if earliest_day is None:
        return prob >= RECEIPT_MIN_PROB
    kickoff_date = fixture.kickoff.date() if fixture.kickoff else None
    if kickoff_date is None or kickoff_date < earliest_day:
        return prob >= RECEIPT_MIN_PROB
    return False


@router.get("/wins")
def get_wins(limit: int = 20, db: Session = Depends(get_db)):
    pinned_ids, earliest_day = _card_anchor(db)

    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won"
    ).order_by(Prediction.id.desc()).limit(300).all()

    # One headline win per match: biggest-odds CARD win (lowest probability
    # among receipts) — never lottery noise that was never given to anyone.
    best = {}
    for pred, fixture in rows:
        if not _is_receipt(pred, fixture, pinned_ids, earliest_day):
            continue
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

    # One line per match: the main pick (highest probability) — taking the
    # max-probability line already excludes lottery-noise entries by design.
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
    """Itemized receipts behind the high-odds record: every won pick in a
    premium high-odds market THAT WAS ACTUALLY ON A CARD, newest first.
    Picks that were never given to users (sub-5% lottery noise) never appear."""
    pinned_ids, earliest_day = _card_anchor(db)

    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won",
        Prediction.market.in_(HIGH_ODDS_ONLY),
    ).order_by(Prediction.settled_at.desc(), Prediction.id.desc()).limit(150).all()

    receipts = [
        (pred, fixture) for pred, fixture in rows
        if _is_receipt(pred, fixture, pinned_ids, earliest_day)
    ][:limit]

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
        for pred, fixture in receipts
    ]
