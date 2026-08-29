import os
import requests
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import SessionLocal
from sports_models import Fixture, Prediction
from espn_adapter import EXTRA_LEAGUES, fetch_scoreboard, espn_fixture_id
load_dotenv()

API_KEY = os.getenv("FOOTBALL_DATA_KEY", "").strip()
BASE_URL = "https://api.football-data.org/v4"

# How far back settlement searches for finished matches.
# 3 days was too short for a pipeline that can freeze (API delay, restarts,
# outages): any finished match older than that became permanently unsettleable.
FD_LOOKBACK_DAYS = 5
ESPN_LOOKBACK_DAYS = 4


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
    dates = [today - timedelta(days=i) for i in range(FD_LOOKBACK_DAYS)]
    updated_count = 0

    for target_date in dates:
        print(f"Fetching results for {target_date.isoformat()}...")
        matches = fetch_finished_matches(target_date)
        print(f"Found {len(matches)} finished matches.")

        for match in matches:
            api_fixture_id = match.get("id")
            score = match.get("score", {}) or {}
            full = score.get("fullTime", {}) or {}
            half = score.get("halfTime", {}) or {}
            home_goals = full.get("home")
            away_goals = full.get("away")
            ht_home = half.get("home")
            ht_away = half.get("away")

            if home_goals is None or away_goals is None:
                continue

            fixture = db.query(Fixture).filter(Fixture.api_fixture_id == api_fixture_id).first()

            if fixture and fixture.home_goals is None:
                fixture.home_goals = home_goals
                fixture.away_goals = away_goals
                fixture.ht_home_goals = ht_home
                fixture.ht_away_goals = ht_away
                fixture.status = "FT"
                updated_count += 1

        db.commit()

    try:
        updated_count += update_espn_fixtures(db)
    except Exception as e:
        print(f"ESPN settlement error: {e}")

    return updated_count


def update_espn_fixtures(db: Session):
    today = date.today()
    updated = 0

    for days_back in range(ESPN_LOOKBACK_DAYS):
        d = today - timedelta(days=days_back)

        for slug in EXTRA_LEAGUES:
            events = fetch_scoreboard(slug, d)

            for ev in events:
                if ev.get("state") != "post":
                    continue

                fid = espn_fixture_id(ev.get("id"))
                if not fid:
                    continue

                fixture = db.query(Fixture).filter(Fixture.api_fixture_id == fid).first()

                if not fixture or fixture.home_goals is not None:
                    continue

                if ev.get("home_score") is None or ev.get("away_score") is None:
                    continue

                fixture.home_goals = ev.get("home_score")
                fixture.away_goals = ev.get("away_score")
                fixture.ht_home_goals = ev.get("home_ht")
                fixture.ht_away_goals = ev.get("away_ht")
                fixture.status = "FT"
                updated += 1

    db.commit()
    return updated


def settle_prediction(pred: Prediction, home_goals: int, away_goals: int, ht_home=None, ht_away=None):
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

    if market == "HOME_MULTI_LOW": return "won" if (home_goals, away_goals) in [(1, 0), (2, 0)] else "lost"
    if market == "HOME_MULTI_HIGH": return "won" if (home_goals, away_goals) in [(2, 1), (3, 1)] else "lost"
    if market == "AWAY_MULTI_LOW": return "won" if (home_goals, away_goals) in [(0, 1), (0, 2)] else "lost"
    if market == "AWAY_MULTI_HIGH": return "won" if (home_goals, away_goals) in [(1, 2), (1, 3)] else "lost"
    if market == "DRAW_MULTI": return "won" if (home_goals, away_goals) in [(1, 1), (2, 2)] else "lost"

    if market == "CS": return "won" if pred.pick == f"{home_goals}-{away_goals}" else "lost"
    if market == "HOME_WIN": return "won" if home_goals > away_goals else "lost"
    if market == "AWAY_WIN": return "won" if away_goals > home_goals else "lost"
    if market == "HOME_MARGIN_2": return "won" if (home_goals - away_goals) >= 2 else "lost"
    if market == "AWAY_MARGIN_2": return "won" if (away_goals - home_goals) >= 2 else "lost"

    if market == "HTFT":
        if ht_home is None or ht_away is None:
            return "void"
        hr = "H" if ht_home > ht_away else "A" if ht_away > ht_home else "D"
        fr = "H" if home_goals > away_goals else "A" if away_goals > home_goals else "D"
        return "won" if pred.pick == f"{hr}/{fr}" else "lost"

    if market == "HT_HOME_MULTI":
        if ht_home is None or ht_away is None:
            return "void"
        return "won" if (ht_home, ht_away) in [(1, 0), (2, 0)] else "lost"

    if market == "HT_AWAY_MULTI":
        if ht_home is None or ht_away is None:
            return "void"
        return "won" if (ht_home, ht_away) in [(0, 1), (0, 2)] else "lost"

    if market == "HT_DRAW_MULTI":
        if ht_home is None or ht_away is None:
            return "void"
        return "won" if (ht_home, ht_away) in [(0, 0), (1, 1)] else "lost"

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
            result = settle_prediction(pred, fixture.home_goals, fixture.away_goals, fixture.ht_home_goals, fixture.ht_away_goals)
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
