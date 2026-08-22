from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db, get_current_user, User
from sports_models import Fixture, Prediction, OddsInsight


router = APIRouter(prefix="/picks", tags=["picks"])

UPCOMING_STATUSES = {"NS", "TBD", "NOT_STARTED", "SCHEDULED", "TIMED", ""}

SAFE_MARKET_LABELS = {
    "OVER_05": "Over 0.5 Goals", "OVER_15": "Over 1.5 Goals", "OVER_25": "Over 2.5 Goals",
    "OVER_35": "Over 3.5 Goals", "UNDER_35": "Under 3.5 Goals", "UNDER_45": "Under 4.5 Goals",
    "BTTS_YES": "Both Teams To Score - Yes", "BTTS_NO": "Both Teams To Score - No",
    "BTTS_O25": "BTTS & Over 2.5 Goals",
    "DC_1X": "Home or Draw", "DC_X2": "Away or Draw", "DC_12": "Any Team To Win",
    "HOME_DNB": "Home Draw No Bet", "AWAY_DNB": "Away Draw No Bet",
    "HOME_OVER_15": "Home Over 1.5 Goals", "AWAY_OVER_15": "Away Over 1.5 Goals",
    "WIN_TO_NIL_HOME": "Home Win To Nil", "WIN_TO_NIL_AWAY": "Away Win To Nil",
    "HOME_MULTI_LOW": "Home Multiscore (1-0, 2-0)", "HOME_MULTI_HIGH": "Home Multiscore (2-1, 3-1)",
    "AWAY_MULTI_LOW": "Away Multiscore (0-1, 0-2)", "AWAY_MULTI_HIGH": "Away Multiscore (1-2, 1-3)",
    "DRAW_MULTI": "Draw Multiscore (1-1, 2-2)",
    "HOME_MARGIN_2": "Home Win By 2+ Goals", "AWAY_MARGIN_2": "Away Win By 2+ Goals",
    "HT_HOME_MULTI": "HT Lead - Home (1-0, 2-0)", "HT_AWAY_MULTI": "HT Lead - Away (0-1, 0-2)",
    "HT_DRAW_MULTI": "HT Draw (0-0, 1-1)",
    "HOME_WIN": "Home Win", "AWAY_WIN": "Away Win",
    "CS": "Correct Score", "HTFT": "HT/FT",
}

# FREE: high hit-rate markets. Free users win with these and trust you.
FREE_MARKETS = {
    "OVER_05", "OVER_15", "OVER_25", "UNDER_35", "UNDER_45",
    "BTTS_YES", "BTTS_NO",
    "HOME_WIN", "AWAY_WIN",
    "DC_1X", "DC_X2", "DC_12",
    "HOME_DNB", "AWAY_DNB",
    "HOME_OVER_15", "AWAY_OVER_15",
}

# PREMIUM: specialised high-odds markets (HT options, multiscores, CS, HTFT)
PREMIUM_MARKETS = {
    "BTTS_O25", "WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY",
    "HOME_MULTI_LOW", "HOME_MULTI_HIGH",
    "AWAY_MULTI_LOW", "AWAY_MULTI_HIGH",
    "DRAW_MULTI", "HOME_MARGIN_2", "AWAY_MARGIN_2",
    "HT_HOME_MULTI", "HT_AWAY_MULTI", "HT_DRAW_MULTI",
    "CS", "HTFT",
}

HIGH_ODDS_ONLY = PREMIUM_MARKETS - {"CS", "HTFT"}


def format_kickoff(dt):
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.isoformat()


def merge_selected(existing, new_rows, limit, max_per_fixture=1, max_per_market=None):
    selected = list(existing)
    fixture_counts = {}
    market_counts = {}
    excluded_ids = set()

    for pred, fixture in selected:
        excluded_ids.add(pred.id)
        fixture_counts[fixture.id] = fixture_counts.get(fixture.id, 0) + 1
        market_counts[pred.market] = market_counts.get(pred.market, 0) + 1

    for pred, fixture in new_rows:
        if len(selected) >= limit:
            break
        if pred.id in excluded_ids:
            continue
        if fixture_counts.get(fixture.id, 0) >= max_per_fixture:
            continue
        if max_per_market and market_counts.get(pred.market, 0) >= max_per_market:
            continue

        selected.append((pred, fixture))
        excluded_ids.add(pred.id)
        fixture_counts[fixture.id] = fixture_counts.get(fixture.id, 0) + 1
        market_counts[pred.market] = market_counts.get(pred.market, 0) + 1

    return selected


def serialize_pick(pred: Prediction, fixture: Fixture, db: Session, locked: bool = False):
    if locked:
        return {
            "prediction_id": pred.id,
            "fixture_id": fixture.id,
            "league": fixture.league,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "kickoff": format_kickoff(fixture.kickoff),
            "status": "Upcoming",
            "market": "LOCKED",
            "market_label": "Premium Pick",
            "pick": "Locked",
            "probability": None,
            "confidence_score": None,
            "confidence_label": "Locked",
            "locked": True,
            "is_value_bet": False,
            "market_edge": 0,
        }

    insight = None
    try:
        insight = db.query(OddsInsight).filter(
            OddsInsight.fixture_id == fixture.id,
            OddsInsight.market == pred.market,
            OddsInsight.is_value_bet == True,
        ).first()
    except Exception:
        pass

    return {
        "prediction_id": pred.id,
        "fixture_id": fixture.id,
        "league": fixture.league,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "kickoff": format_kickoff(fixture.kickoff),
        "status": "Upcoming",
        "market": pred.market,
        "market_label": SAFE_MARKET_LABELS.get(pred.market, pred.market),
        "pick": pred.pick,
        "probability": pred.probability,
        "confidence_score": pred.confidence_score,
        "confidence_label": pred.confidence_label,
        "locked": False,
        "is_value_bet": bool(insight),
        "market_edge": round(insight.value_edge * 100, 1) if insight else 0,
    }


def fetch_free_picks(db: Session, limit: int = 10):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)

    rows = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market.in_(FREE_MARKETS),
            Prediction.status == "pending",
            Prediction.confidence_score >= 60,
            Prediction.probability <= 0.90,
            Fixture.kickoff >= cutoff,
        )
        .order_by(Prediction.probability.desc())
        .limit(200)
        .all()
    )

    return merge_selected([], rows, limit, max_per_fixture=1, max_per_market=3)


def fetch_high_odds(db: Session, limit: int = 4):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)

    rows = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market.in_(HIGH_ODDS_ONLY),
            Prediction.status == "pending",
            Fixture.kickoff >= cutoff,
        )
        .order_by(Prediction.probability.desc())
        .limit(120)
        .all()
    )

    return merge_selected([], rows, limit, max_per_fixture=1, max_per_market=1)


def fetch_bonus(db: Session, market: str, limit: int = 2):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)

    min_prob = {"CS": 0.15, "HTFT": 0.20}.get(market, 0.0)

    rows = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market == market,
            Prediction.status == "pending",
            Prediction.probability >= min_prob,
            Fixture.kickoff >= cutoff,
        )
        .order_by(Prediction.probability.desc())
        .limit(40)
        .all()
    )

    return merge_selected([], rows, limit, max_per_fixture=1)


@router.get("/today")
def picks_today(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    free_rows = fetch_free_picks(db, limit=10)
    high_odds_rows = fetch_high_odds(db, limit=4)
    cs_rows = fetch_bonus(db, "CS", limit=2)
    htft_rows = fetch_bonus(db, "HTFT", limit=2)

    if user.is_premium:
        return {
            "plan": user.plan,
            "safe_picks": [serialize_pick(p, f, db) for p, f in free_rows],
            "bonus_picks": [serialize_pick(p, f, db) for p, f in high_odds_rows + cs_rows + htft_rows],
        }

    # Free users: all free-market picks unlocked, premium section locked
    return {
        "plan": user.plan,
        "safe_picks": [serialize_pick(p, f, db) for p, f in free_rows],
        "bonus_picks": [serialize_pick(p, f, db, locked=True) for p, f in high_odds_rows + cs_rows + htft_rows],
    }