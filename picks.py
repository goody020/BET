from datetime import timezone
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db, get_current_user, User
from sports_models import Fixture, Prediction


router = APIRouter(prefix="/picks", tags=["picks"])

UPCOMING_STATUSES = {
    "NS",
    "TBD",
    "NOT_STARTED",
    "",
}

SAFE_MARKET_LABELS = {
    "OVER_05": "Over 0.5 Goals",
    "OVER_15": "Over 1.5 Goals",
    "OVER_25": "Over 2.5 Goals",
    "UNDER_35": "Under 3.5 Goals",
    "UNDER_45": "Under 4.5 Goals",
    "BTTS_YES": "Both Teams To Score - Yes",
    "BTTS_NO": "Both Teams To Score - No",
    "DC_1X": "Home or Draw",
    "DC_X2": "Away or Draw",
    "DC_12": "Any Team To Win",
    "HOME_DNB": "Home Draw No Bet",
    "AWAY_DNB": "Away Draw No Bet",
    "CS": "Correct Score",
    "HTFT": "HT/FT",
}

PREMIUM_SAFE_MARKETS = {
    "OVER_15",
    "OVER_25",
    "UNDER_35",
    "BTTS_NO",
    "DC_1X",
    "DC_X2",
    "DC_12",
    "HOME_DNB",
    "AWAY_DNB",
}


def format_kickoff(dt):
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.isoformat()


def base_safe_query(db: Session):
    return db.query(Prediction, Fixture).join(
        Fixture,
        Prediction.fixture_id == Fixture.id,
    ).filter(
        Fixture.status.in_(UPCOMING_STATUSES),
        Prediction.market.in_(PREMIUM_SAFE_MARKETS),
        Prediction.status == "pending",
        Prediction.confidence_score >= 60,
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


def fetch_safe_picks(db: Session, limit: int = 7):
    selected = []

    rows = base_safe_query(db).filter(
        Prediction.probability >= 0.68,
        Prediction.confidence_score >= 62,
    ).order_by(
        Prediction.probability.desc()
    ).limit(120).all()

    selected = merge_selected(selected, rows, limit)

    if len(selected) < 5:
        existing_ids = [pred.id for pred, _ in selected]

        query = base_safe_query(db).filter(
            Prediction.probability >= 0.72,
            Prediction.confidence_score >= 62,
        )

        query = apply_exclusion(query, existing_ids)

        rows = query.order_by(
            Prediction.probability.desc()
        ).limit(120).all()

        selected = merge_selected(selected, rows, limit)

    if len(selected) < 3:
        existing_ids = [pred.id for pred, _ in selected]

        query = base_safe_query(db)
        query = apply_exclusion(query, existing_ids)

        rows = query.order_by(
            Prediction.probability.desc()
        ).limit(120).all()

        selected = merge_selected(selected, rows, limit)

    return selected[:limit]


def fetch_bonus(db: Session, market: str, limit: int = 2):
    rows = db.query(Prediction, Fixture).join(
        Fixture,
        Prediction.fixture_id == Fixture.id,
    ).filter(
        Fixture.status.in_(UPCOMING_STATUSES),
        Prediction.market == market,
        Prediction.status == "pending",
        Prediction.confidence_score >= 55,
    ).order_by(
        Prediction.confidence_score.desc(),
        Prediction.probability.desc(),
    ).limit(40).all()

    return merge_selected([], rows, limit, max_per_fixture=1)


def serialize_pick(pred: Prediction, fixture: Fixture, locked: bool = False):
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
        "locked": locked,
    }


@router.get("/today")
def picks_today(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    premium_safe_rows = fetch_safe_picks(db, limit=7)

    cs_rows = fetch_bonus(db, "CS", limit=2)
    htft_rows = fetch_bonus(db, "HTFT", limit=2)

    safe_picks = []
    bonus_picks = []

    if user.is_premium:
        for pred, fixture in premium_safe_rows:
            safe_picks.append(serialize_pick(pred, fixture, locked=False))

        for pred, fixture in cs_rows + htft_rows:
            bonus_picks.append(serialize_pick(pred, fixture, locked=False))

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
            unlocked_free.append(serialize_pick(pred, fixture, locked=False))
            unlocked_fixture_ids.append(fixture.id)
        else:
            locked_preview.append(serialize_pick(pred, fixture, locked=True))

    extra_query = base_safe_query(db).filter(
        Prediction.probability >= 0.66,
        Prediction.probability < 0.76,
    )

    extra_query = apply_exclusion(extra_query, included_prediction_ids)

    if unlocked_fixture_ids:
        extra_query = extra_query.filter(~Fixture.id.in_(unlocked_fixture_ids))

    extra_rows = extra_query.order_by(
        Prediction.probability.desc()
    ).limit(80).all()

    extra_selected = merge_selected([], extra_rows, limit=3, max_per_fixture=1)

    extra_picks = [
        serialize_pick(pred, fixture, locked=False)
        for pred, fixture in extra_selected
    ]

    safe_picks = unlocked_free + extra_picks + locked_preview

    if not safe_picks:
        fallback_rows = base_safe_query(db).order_by(
            Prediction.probability.desc()
        ).limit(80).all()

        fallback_selected = merge_selected([], fallback_rows, limit=3, max_per_fixture=1)

        safe_picks = [
            serialize_pick(pred, fixture, locked=False)
            for pred, fixture in fallback_selected
        ]

    for pred, fixture in cs_rows + htft_rows:
        bonus_picks.append(serialize_pick(pred, fixture, locked=True))

    return {
        "plan": user.plan,
        "safe_picks": safe_picks,
        "bonus_picks": bonus_picks,
    }