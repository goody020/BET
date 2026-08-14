from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db, get_current_user, User
from sports_models import Prediction


router = APIRouter(prefix="/performance", tags=["performance"])

SAFE_MARKETS = [
    "OVER_05",
    "OVER_15",
    "OVER_25",
    "UNDER_35",
    "UNDER_45",
    "BTTS_YES",
    "BTTS_NO",
    "DC_1X",
    "DC_X2",
    "DC_12",
    "HOME_DNB",
    "AWAY_DNB",
]

BONUS_MARKETS = [
    "CS",
    "HTFT",
]

MARKET_LABELS = {
    "OVER_05": "Over 0.5 Goals",
    "OVER_15": "Over 1.5 Goals",
    "OVER_25": "Over 2.5 Goals",
    "UNDER_35": "Under 3.5 Goals",
    "UNDER_45": "Under 4.5 Goals",
    "BTTS_YES": "BTTS Yes",
    "BTTS_NO": "BTTS No",
    "DC_1X": "Home or Draw",
    "DC_X2": "Away or Draw",
    "DC_12": "Any Team To Win",
    "HOME_DNB": "Home Draw No Bet",
    "AWAY_DNB": "Away Draw No Bet",
    "CS": "Correct Score",
    "HTFT": "HT/FT",
}


def market_stats(db: Session, market: str):
    settled_statuses = ("won", "lost", "void")

    base_query = db.query(Prediction).filter(
        Prediction.market == market,
        Prediction.status.in_(settled_statuses),
    )

    total = base_query.count()

    wins = base_query.filter(
        Prediction.status == "won",
    ).count()

    losses = base_query.filter(
        Prediction.status == "lost",
    ).count()

    voids = base_query.filter(
        Prediction.status == "void",
    ).count()

    decided = wins + losses

    hit_rate = round((wins / decided) * 100, 1) if decided > 0 else 0.0

    return {
        "market": market,
        "market_label": MARKET_LABELS.get(market, market),
        "total_settled": total,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "hit_rate": hit_rate,
    }


@router.get("/summary")
def performance_summary(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settled_statuses = ("won", "lost", "void")

    base_query = db.query(Prediction).filter(
        Prediction.status.in_(settled_statuses),
    )

    total_settled = base_query.count()

    total_wins = base_query.filter(
        Prediction.status == "won",
    ).count()

    total_losses = base_query.filter(
        Prediction.status == "lost",
    ).count()

    total_voids = base_query.filter(
        Prediction.status == "void",
    ).count()

    decided = total_wins + total_losses

    overall_hit_rate = round((total_wins / decided) * 100, 1) if decided > 0 else 0.0

    safe_stats = []

    for market in SAFE_MARKETS:
        safe_stats.append(market_stats(db, market))

    bonus_stats = []

    for market in BONUS_MARKETS:
        bonus_stats.append(market_stats(db, market))

    return {
        "plan": user.plan,
        "total_settled": total_settled,
        "wins": total_wins,
        "losses": total_losses,
        "voids": total_voids,
        "decided": decided,
        "overall_hit_rate": overall_hit_rate,
        "safe_markets": safe_stats,
        "bonus_markets": bonus_stats,
    }