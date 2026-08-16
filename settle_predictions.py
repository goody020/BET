import os
import requests
from datetime import date, timedelta
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import SessionLocal
from sports_models import Fixture, Prediction


load_dotenv()

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"

FINISHED_STATUSES = {
    "FT",
    "AET",
    "PEN",
}

VOID_STATUSES = {
    "PST",
    "ABD",
    "CANC",
    "SUSP",
    "INT",
    "AWD",
    "WO",
}


def api_headers():
    return {
        "x-apisports-key": API_KEY,
    }


def fetch_fixtures_for_date(target_date: date):
    params = {
        "date": target_date.isoformat(),
    }

    try:
        response = requests.get(
            API_FIXTURES_URL,
            headers=api_headers(),
            params=params,
            timeout=30,
        )
        data = response.json()
    except Exception as e:
        print(f"Failed to connect to API: {e}")
        return []

    if response.status_code != 200:
        print(f"API Error: {data}")
        return []

    return data.get("response", [])


def result_from_goals(home_goals, away_goals):
    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        return "H"

    if away_goals > home_goals:
        return "A"

    return "D"


def settle_prediction(pred: Prediction, fixture: Fixture):
    if pred.status != "pending":
        return None

    if fixture.status in VOID_STATUSES:
        return "void"

    if fixture.status not in FINISHED_STATUSES:
        return None

    home_goals = fixture.home_goals
    away_goals = fixture.away_goals

    if home_goals is None or away_goals is None:
        return None

    total_goals = home_goals + away_goals

    market = pred.market

    if market == "CS":
        expected = f"{home_goals}-{away_goals}"
        return "won" if pred.pick == expected else "lost"
    if market == "HOME_MULTI":
        valid_scores = [(2, 0), (2, 1), (3, 0), (3, 1)]
        return "won" if (home_goals, away_goals) in valid_scores else "lost"

    if market == "AWAY_MULTI":
        valid_scores = [(0, 2), (1, 2), (0, 3), (1, 3)]
        return "won" if (home_goals, away_goals) in valid_scores else "lost"

    if market == "DRAW_MULTI":
        valid_scores = [(1, 1), (2, 2)]
        return "won" if (home_goals, away_goals) in valid_scores else "lost"
    if market == "HTFT":
        ht_home = fixture.ht_home_goals
        ht_away = fixture.ht_away_goals

        if ht_home is None or ht_away is None:
            return "void"

        ht_result = result_from_goals(ht_home, ht_away)
        ft_result = result_from_goals(home_goals, away_goals)

        if not ht_result or not ft_result:
            return "void"

        expected = f"{ht_result}/{ft_result}"

        return "won" if pred.pick == expected else "lost"

    if market == "OVER_05":
        return "won" if total_goals >= 1 else "lost"

    if market == "OVER_15":
        return "won" if total_goals >= 2 else "lost"

    if market == "OVER_25":
        return "won" if total_goals >= 3 else "lost"

    if market == "UNDER_35":
        return "won" if total_goals <= 3 else "lost"

    if market == "UNDER_45":
        return "won" if total_goals <= 4 else "lost"

    if market == "BTTS_YES":
        return "won" if home_goals > 0 and away_goals > 0 else "lost"

    if market == "BTTS_NO":
        return "won" if not (home_goals > 0 and away_goals > 0) else "lost"

    ft_result = result_from_goals(home_goals, away_goals)

    if market == "DC_1X":
        return "won" if ft_result in {"H", "D"} else "lost"

    if market == "DC_X2":
        return "won" if ft_result in {"A", "D"} else "lost"

    if market == "DC_12":
        return "won" if ft_result in {"H", "A"} else "lost"

    if market == "HOME_DNB":
        if ft_result == "H":
            return "won"
        if ft_result == "D":
            return "void"
        return "lost"

    if market == "AWAY_DNB":
        if ft_result == "A":
            return "won"
        if ft_result == "D":
            return "void"
        return "lost"

    return None


def update_fixtures_from_api(db: Session):
    today = date.today()

    dates = [
        today,
        today - timedelta(days=1),
        today - timedelta(days=2),
        today - timedelta(days=3),
        today - timedelta(days=4),
    ]

    updated_count = 0

    for target_date in dates:
        print(f"Fetching results for {target_date.isoformat()}...")

        fixtures = fetch_fixtures_for_date(target_date)

        print(f"Found {len(fixtures)} matches.")

        for item in fixtures:
            api_fixture = item.get("fixture", {})
            api_fixture_id = api_fixture.get("id")

            if not api_fixture_id:
                continue

            fixture = db.query(Fixture).filter(
                Fixture.api_fixture_id == api_fixture_id
            ).first()

            if not fixture:
                continue

            status = api_fixture.get("status", {}).get("short")
            goals = item.get("goals") or {}
            halftime = (item.get("score") or {}).get("halftime") or {}

            fixture.status = status or fixture.status
            fixture.home_goals = goals.get("home")
            fixture.away_goals = goals.get("away")
            fixture.ht_home_goals = halftime.get("home")
            fixture.ht_away_goals = halftime.get("away")

            updated_count += 1

    db.commit()

    return updated_count


def settle_all(db: Session):
    rows = db.query(Prediction, Fixture).join(
        Fixture,
        Prediction.fixture_id == Fixture.id,
    ).filter(
        Prediction.status == "pending",
    ).all()

    counts = {
        "won": 0,
        "lost": 0,
        "void": 0,
    }

    for pred, fixture in rows:
        new_status = settle_prediction(pred, fixture)

        if new_status:
            pred.status = new_status
            counts[new_status] += 1

    db.commit()

    return counts


def main():
    if not API_KEY:
        print("ERROR: API_FOOTBALL_KEY is missing in .env")
        return

    db = SessionLocal()

    print("Updating fixture results...")
    updated = update_fixtures_from_api(db)
    print(f"Updated {updated} fixtures.")

    print("Settling predictions...")
    counts = settle_all(db)

    print(f"Won: {counts['won']}")
    print(f"Lost: {counts['lost']}")
    print(f"Void: {counts['void']}")
    print("Settlement complete.")

    db.close()


if __name__ == "__main__":
    main()