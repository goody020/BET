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
    "CS": "Correct Score", "HTFT": "HT/FT",
}

PREMIUM_SAFE_MARKETS = {
    "OVER_15", "OVER_25", "OVER_35", "UNDER_35", "BTTS_NO", "BTTS_O25",
    "DC_1X", "DC_X2", "DC_12", "HOME_DNB", "AWAY_DNB",
    "HOME_OVER_15", "AWAY_OVER_15", "WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY",
    "HOME_MULTI_LOW", "HOME_MULTI_HIGH", "AWAY_MULTI_LOW", "AWAY_MULTI_HIGH",
    "DRAW_MULTI", "HOME_MARGIN_2", "AWAY_MARGIN_2",
    "HT_HOME_MULTI", "HT_AWAY_MULTI", "HT_DRAW_MULTI",
    "OVER_15",
    "CS", "HTFT",
    "OVER_25",
    "OVER_35",
    "UNDER_35",
    "BTTS_NO",
    "BTTS_O25",
    "DC_1X",
    "DC_X2",
    "DC_12",
    "HOME_DNB",
    "AWAY_DNB",
    "HOME_OVER_15",
    "AWAY_OVER_15",
    "WIN_TO_NIL_HOME",
    "WIN_TO_NIL_AWAY",
    "HOME_MULTI",
    "AWAY_MULTI",
    "DRAW_MULTI",
}


def format_kickoff(dt):
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.isoformat()


def base_safe_query(db: Session):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)

    return db.query(Prediction, Fixture).join(
        Fixture,
        Prediction.fixture_id == Fixture.id,
    ).filter(
        Fixture.status.in_(UPCOMING_STATUSES),
        Prediction.market.in_(PREMIUM_SAFE_MARKETS),
        Prediction.status == "pending",
        Prediction.confidence_score >= 60,
        Fixture.kickoff >= cutoff,
    )


def apply_exclusion(query, prediction_ids):
    if prediction_ids:
        return query.filter(~Prediction.id.in_(prediction_ids))
    return query


def merge_selected(existing, new_rows, limit, max_per_fixture=2):
    selected = list(existing)
    fixture_counts = {}
    excluded_ids = set()

    for pred, fixture in selected:
        excluded_ids.add(pred.id)
        fixture_counts[fixture.id] = fixture_counts.get(fixture.id, 0) + 1

    for pred, fixture in new_rows:
        if len(selected) >= limit:
            break
        if pred.id in excluded_ids:
            continue
        if fixture_counts.get(fixture.id, 0) >= max_per_fixture:
            continue

        selected.append((pred, fixture))
        excluded_ids.add(pred.id)
        fixture_counts[fixture.id] = fixture_counts.get(fixture.id, 0) + 1

    return selected


def serialize_pick(pred: Prediction, fixture: Fixture, db: Session, locked: bool = False):
    # LOCKED PICKS SEND FAKE DATA SO FREE USERS CANNOT SEE THE PREDICTION
    if locked:
        return {
            "prediction_id": pred.id,
            "fixture_id": fixture.id,
            "league": fixture.league,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "kickoff": format_kickoff(fixture.kickoff),
            "status": fixture.status,
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
        "status": fixture.status,
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


def fetch_safe_picks(db: Session, limit: int = 7):
    selected = []

    rows = (
        base_safe_query(db)
        .filter(
            Prediction.probability >= 0.75,
            Prediction.confidence_score >= 68,
        )
        .order_by(Prediction.probability.desc())
        .limit(150)
        .all()
    )
    selected = merge_selected(selected, rows, limit)

    if len(selected) < 5:
        existing_ids = [pred.id for pred, _ in selected]
        query = base_safe_query(db).filter(
            Prediction.probability >= 0.70,
            Prediction.confidence_score >= 64,
        )
        query = apply_exclusion(query, existing_ids)
        rows = query.order_by(Prediction.probability.desc()).limit(150).all()
        selected = merge_selected(selected, rows, limit)

    if len(selected) < 3:
        existing_ids = [pred.id for pred, _ in selected]
        query = base_safe_query(db)
        query = apply_exclusion(query, existing_ids)
        rows = query.order_by(Prediction.probability.desc()).limit(150).all()
        selected = merge_selected(selected, rows, limit)

    return selected[:limit]


def fetch_bonus(db: Session, market: str, limit: int = 2):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)

    rows = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market == market,
            Prediction.status == "pending",
            Prediction.confidence_score >= 55,
            Fixture.kickoff >= cutoff,
        )
        .order_by(
            Prediction.confidence_score.desc(),
            Prediction.probability.desc(),
        )
        .limit(40)
        .all()
    )

    return merge_selected([], rows, limit, max_per_fixture=1)


@router.get("/today")
def picks_today(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    premium_safe_rows = fetch_safe_picks(db, limit=7)
    cs_rows = fetch_bonus(db, "CS", limit=2)
    htft_rows = fetch_bonus(db, "HTFT", limit=2)

    safe_picks = []
    bonus_picks = []

    if user.is_premium:
        for pred, fixture in premium_safe_rows:
            safe_picks.append(serialize_pick(pred, fixture, db, locked=False))

        for pred, fixture in cs_rows + htft_rows:
            bonus_picks.append(serialize_pick(pred, fixture, db, locked=False))

        return {
            "plan": user.plan,
            "safe_picks": safe_picks,
            "bonus_picks": bonus_picks,
        }

    unlocked_free = []
    locked_preview = []
    included_prediction_ids = []
    unlocked_fixture_ids = []

    for index, (pred, fixture) in enumerate(premium_safe_rows):
        included_prediction_ids.append(pred.id)

        if index == 0:
            unlocked_free.append(serialize_pick(pred, fixture, db, locked=False))
            unlocked_fixture_ids.append(fixture.id)
        else:
            locked_preview.append(serialize_pick(pred, fixture, db, locked=True))

    extra_query = base_safe_query(db).filter(
        Prediction.probability >= 0.66,
        Prediction.probability < 0.76,
    )
    extra_query = apply_exclusion(extra_query, included_prediction_ids)

    if unlocked_fixture_ids:
        extra_query = extra_query.filter(~Fixture.id.in_(unlocked_fixture_ids))

    extra_rows = extra_query.order_by(Prediction.probability.desc()).limit(80).all()
    extra_selected = merge_selected([], extra_rows, limit=3, max_per_fixture=1)
    extra_picks = [
        serialize_pick(pred, fixture, db, locked=False)
        for pred, fixture in extra_selected
    ]

    safe_picks = unlocked_free + extra_picks + locked_preview

    if not safe_picks:
        fallback_rows = base_safe_query(db).order_by(Prediction.probability.desc()).limit(80).all()
        fallback_selected = merge_selected([], fallback_rows, limit=3, max_per_fixture=1)
        safe_picks = [
            serialize_pick(pred, fixture, db, locked=False)
            for pred, fixture in fallback_selected
        ]

    for pred, fixture in cs_rows + htft_rows:
        bonus_picks.append(serialize_pick(pred, fixture, db, locked=True))

    return {
        "plan": user.plan,
        "safe_picks": safe_picks,
        "bonus_picks": bonus_picks,
    }