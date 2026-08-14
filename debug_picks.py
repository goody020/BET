from app import SessionLocal
from sports_models import Fixture, Prediction


UPCOMING_STATUSES = {
    "NS",
    "TBD",
    "NOT_STARTED",
    "",
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


def debug():
    db = SessionLocal()

    total_fixtures = db.query(Fixture).count()

    upcoming_fixtures = db.query(Fixture).filter(
        Fixture.status.in_(UPCOMING_STATUSES)
    ).count()

    total_predictions = db.query(Prediction).count()

    safe_predictions = db.query(Prediction).filter(
        Prediction.market.in_(PREMIUM_SAFE_MARKETS)
    ).count()

    safe_confidence_60 = db.query(Prediction).filter(
        Prediction.market.in_(PREMIUM_SAFE_MARKETS),
        Prediction.confidence_score >= 60,
    ).count()

    safe_prob_70 = db.query(Prediction).filter(
        Prediction.market.in_(PREMIUM_SAFE_MARKETS),
        Prediction.probability >= 0.70,
    ).count()

    safe_prob_78 = db.query(Prediction).filter(
        Prediction.market.in_(PREMIUM_SAFE_MARKETS),
        Prediction.probability >= 0.78,
    ).count()

    print("----- PICK DEBUG -----")
    print(f"Total fixtures: {total_fixtures}")
    print(f"Upcoming fixtures: {upcoming_fixtures}")
    print(f"Total predictions: {total_predictions}")
    print(f"Safe predictions: {safe_predictions}")
    print(f"Safe predictions with confidence >= 60: {safe_confidence_60}")
    print(f"Safe predictions with probability >= 70%: {safe_prob_70}")
    print(f"Safe predictions with probability >= 78%: {safe_prob_78}")

    print("\nTop 10 safe predictions by confidence:")

    rows = db.query(Prediction, Fixture).join(
        Fixture,
        Prediction.fixture_id == Fixture.id,
    ).filter(
        Fixture.status.in_(UPCOMING_STATUSES),
        Prediction.market.in_(PREMIUM_SAFE_MARKETS),
    ).order_by(
        Prediction.confidence_score.desc(),
        Prediction.probability.desc(),
    ).limit(10).all()

    if not rows:
        print("No safe upcoming predictions found.")

    for pred, fixture in rows:
        print(
            f"{fixture.home_team} vs {fixture.away_team} | "
            f"{pred.market} | {pred.pick} | "
            f"prob={round(pred.probability, 3)} | "
            f"conf={pred.confidence_score} | "
            f"status={fixture.status}"
        )

    db.close()


if __name__ == "__main__":
    debug()