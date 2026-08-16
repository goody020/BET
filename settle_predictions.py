import os
import requests
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import SessionLocal
from sports_models import Fixture, Prediction

load_dotenv()

API_KEY = os.getenv("FOOTBALL_DATA_KEY", "").strip()
BASE_URL = "https://api.football-data.org/v4"


def api_headers():
    return {"X-Auth-Token": API_KEY}


def fetch_finished_matches(target_date: date):
    url = f"{BASE_URL}/matches"
    params = {
        "dateFrom": target_date.isoformat(),
        "dateTo": target_date.isoformat(),
        "status": "FINISHED"
    }

    try:
        response = requests.get(url, headers=api_headers(), params=params, timeout=30)
        data = response.json()
    except Exception as e:
        print(f"Failed to connect to settlement API: {e}")
        return []

    if response.status_code != 200:
        print(f"Settlement API Error: {data}")
        return []

    return data.get("matches", [])


def update_fixtures_from_api(db: Session):
    today = date.today()
    dates = [today, today - timedelta(days=1), today - timedelta(days=2)]
    updated_count = 0

    for target_date in dates:
        print(f"Fetching results for {target_date.isoformat()}...")
        matches = fetch_finished_matches(target_date)
        print(f"Found {len(matches)} finished matches.")

        for match in matches:
            api_fixture_id = match.get("id")
            score = match.get("score", {}).get("fullTime", {})
            home_goals = score.get("home")
            away_goals = score.get("away")

            if home_goals is None or away_goals is None:
                continue

            fixture = db.query(Fixture).filter(Fixture.api_fixture_id == api_fixture_id).first()
            
            if fixture and fixture.home_goals is None:
                fixture.home_goals = home_goals
                fixture.away_goals = away_goals
                fixture.status = "FT"
                updated_count += 1

    db.commit()
    return updated_count


def settle_prediction(pred: Prediction, home_goals: int, away_goals: int):
    market = pred.market
    total_goals = home_goals + away_goals

    if market == "OVER_05": return "won" if total_goals >= 1 else "lost"
    if market == "OVER_15": return "won" if total_goals >= 2 else "lost"
    if market == "OVER_25": return "won" if total_goals >= 3 else "lost"
    if market == "OVER_35": return "won" if total_goals >= 4 else "lost"
    if market == "UNDER_35": return "won" if total_goals <= 3 else "lost"
    if market == "UNDER_45": return "won" if total_goals <= 4 else "lost"
    if market == "BTTS_YES": return "won" if (home_goals > 0 and away_goals > 0) else "lost"
    if market == "BTTS_NO": return "won" if (home_goals == 0 or away_goals == 0) else "lost"
    if market == "BTTS_O25": return "won" if (home_goals > 0 and away_goals > 0 and total_goals >= 3) else "lost"

    home_win = home_goals > away_goals
    away_win = away_goals > home_goals
    draw = home_goals == away_goals

    if market == "DC_1X": return "won" if (home_win or draw) else "lost"
    if market == "DC_X2": return "won" if (away_win or draw) else "lost"
    if market == "DC_12": return "won" if (home_win or away_win) else "lost"
    if market == "HOME_DNB": return "void" if draw else ("won" if home_win else "lost")
    if market == "AWAY_DNB": return "void" if draw else ("won" if away_win else "lost")

    if market == "HOME_OVER_15": return "won" if home_goals >= 2 else "lost"
    if market == "AWAY_OVER_15": return "won" if away_goals >= 2 else "lost"
    if market == "WIN_TO_NIL_HOME": return "won" if (home_win and away_goals == 0) else "lost"
    if market == "WIN_TO_NIL_AWAY": return "won" if (away_win and home_goals == 0) else "lost"

    if market == "HOME_MULTI": return "won" if (home_goals, away_goals) in [(2,0), (2,1), (3,0), (3,1)] else "lost"
    if market == "AWAY_MULTI": return "won" if (home_goals, away_goals) in [(0,2), (1,2), (0,3), (1,3)] else "lost"
    if market == "DRAW_MULTI": return "won" if (home_goals, away_goals) in [(1,1), (2,2)] else "lost"

    if market == "CS": return "won" if pred.pick == f"{home_goals}-{away_goals}" else "lost"
    if market == "HTFT":
        ht_h, ht_a = home_goals // 2, away_goals // 2 # rough approximation
        hr = "H" if ht_h > ht_a else "A" if ht_a > ht_h else "D"
        fr = "H" if home_goals > away_goals else "A" if away_goals > home_goals else "D"
        return "won" if pred.pick == f"{hr}/{fr}" else "lost"

    return "void"


def settle_all(db: Session):
    fixtures = db.query(Fixture).filter(
        Fixture.status == "FT",
        Fixture.home_goals != None,
        Fixture.away_goals != None,
    ).all()

    counts = {"won": 0, "lost": 0, "void": 0}

    for fixture in fixtures:
        preds = db.query(Prediction).filter(
            Prediction.fixture_id == fixture.id,
            Prediction.status == "pending",
        ).all()

        for pred in preds:
            result = settle_prediction(pred, fixture.home_goals, fixture.away_goals)
            pred.status = result
            pred.settled_at = datetime.now(timezone.utc)
            counts[result] = counts.get(result, 0) + 1

    db.commit()
    print(f"Settled {counts['won']} wins, {counts['lost']} losses, {counts['void']} voids.")
    return counts


if __name__ == "__main__":
    db = SessionLocal()
    updated = update_fixtures_from_api(db)
    print(f"Updated {updated} fixtures with scores.")
    settle_all(db)
    db.close()