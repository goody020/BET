import os
import time
import re
import json
import requests
from pathlib import Path
from datetime import date, datetime, timezone, timedelta
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app import SessionLocal, engine, Base, score_matrix, ht_ft, confidence_label
from sports_models import Fixture, Prediction


load_dotenv()

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()

try:
    MAX_STANDINGS_REQUESTS = int(os.getenv("MAX_STANDINGS_REQUESTS", "45"))
except Exception:
    MAX_STANDINGS_REQUESTS = 45

API_FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
API_STANDINGS_URL = "https://v3.football.api-sports.io/standings"
try:
    FREE_PLAN_MAX_SEASON = int(os.getenv("FREE_PLAN_MAX_SEASON", "2024"))
except Exception:
    FREE_PLAN_MAX_SEASON = 2024
CACHE_FILE = Path(__file__).resolve().parent / "ratings_cache.json"
try:
    RATINGS_CACHE_DAYS = int(os.getenv("RATINGS_CACHE_DAYS", "3"))
except Exception:
    RATINGS_CACHE_DAYS = 3

PRIORITY_LEAGUE_KEYWORDS = {
    "premier league",
    "la liga",
    "serie a",
    "bundesliga",
    "ligue 1",
    "uefa champions league",
    "uefa europa league",
    "uefa europa conference league",
    "uefa conference league",
    "championship",
}
PREDICTABLE_STATUSES = {
    "NS",
    "TBD",
    "NOT_STARTED",
    "",
}

BLOCKED_LEAGUE_KEYWORDS = {
    "friendly",
    "friendlies",
    "club friendly",
    "national team friendly",
}

BLOCKED_LEAGUE_KEYWORDS = {
    "friendly",
    "friendlies",
    "club friendly",
    "national team friendly",
    "youth",
    "academy",
    "reserve",
    "reserves",
    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u22",
    "u23",
}

BLOCKED_TEAM_TOKENS = {
    "academy",
    "reserve",
    "reserves",
    "youth",
    "ii",
    "iii",
    "iv",
    "women"
}

YOUTH_PATTERN = re.compile(r"\bu\d{1,2}\b")
def normalize_league(name):
    return (name or "").strip().lower()

def is_priority_league(league_name):
    normalized = normalize_league(league_name)

    for keyword in PRIORITY_LEAGUE_KEYWORDS:
        if keyword in normalized:
            return True

    return False
def is_blocked_text(name):
    if not name:
        return False

    normalized = normalize_league(name)

    if YOUTH_PATTERN.search(normalized):
        return True

    tokens = set(normalized.replace("-", " ").split())

    if tokens & BLOCKED_TEAM_TOKENS:
        return True

    if normalized.endswith(" b"):
        return True

    if normalized.endswith(" ii"):
        return True

    return False
def is_blocked_league(league_name):
    normalized = normalize_league(league_name)

    for keyword in BLOCKED_LEAGUE_KEYWORDS:
        if keyword in normalized:
            return True

    return is_blocked_text(league_name)


def parse_api_date(date_str: str):
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def api_headers():
    return {
        "x-apisports-key": API_KEY,
    }


def clamp_prob(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def clamp_xg(value: float) -> float:
    return max(0.30, min(3.50, float(value)))


def load_ratings_cache():
    if not CACHE_FILE.exists():
        return {}

    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ratings_cache(cache):
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False),
        encoding="utf-8",
    )


def cache_is_fresh(entry):
    updated = entry.get("updated")

    if not updated:
        return False

    try:
        updated_date = date.fromisoformat(updated)
    except Exception:
        return False

    age_days = (date.today() - updated_date).days

    # Empty standings entries expire faster so we can retry them
    ttl_days = RATINGS_CACHE_DAYS if entry.get("teams") else 1

    return age_days <= ttl_days
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

    if data.get("errors"):
        print(f"API-Football errors: {data.get('errors')}")

    return data.get("response", [])


def seasons_for_standings(season):
    try:
        s = int(season)
    except Exception:
        return [
            FREE_PLAN_MAX_SEASON,
            FREE_PLAN_MAX_SEASON - 1,
            FREE_PLAN_MAX_SEASON - 2,
        ]

    if s > FREE_PLAN_MAX_SEASON:
        return [
            FREE_PLAN_MAX_SEASON,
            FREE_PLAN_MAX_SEASON - 1,
            FREE_PLAN_MAX_SEASON - 2,
        ]

    if s == FREE_PLAN_MAX_SEASON:
        return [
            s,
            s - 1,
            s - 2,
        ]

    return [
        s,
        FREE_PLAN_MAX_SEASON,
        s - 1,
    ]


def fetch_standings(league_id: int, season: int):
    if not league_id:
        return None

    for try_season in seasons_for_standings(season):
        params = {
            "league": league_id,
            "season": try_season,
        }

        try:
            response = requests.get(
                API_STANDINGS_URL,
                headers=api_headers(),
                params=params,
                timeout=30,
            )
            data = response.json()
        except Exception:
            continue

        if response.status_code == 200 and data.get("response"):
            print(f"Standings loaded: league={league_id}, season={try_season}")
            return data.get("response")

    return None
    attempts = []

    if season:
        attempts.append(
            {
                "league": league_id,
                "season": season,
            }
        )

    attempts.append(
        {
            "league": league_id,
        }
    )

    for params in attempts:
        try:
            response = requests.get(
                API_STANDINGS_URL,
                headers=api_headers(),
                params=params,
                timeout=30,
            )
            data = response.json()
        except Exception:
            continue

        if response.status_code == 200 and data.get("response"):
            return data.get("response")

    return None
    try:
        response = requests.get(
            API_STANDINGS_URL,
            headers=api_headers(),
            params=params,
            timeout=30,
        )
        data = response.json()
    except Exception as e:
        print(f"Failed to fetch standings: {e}")
        return None

    if response.status_code != 200:
        print(f"Standings API error: {data}")
        return None

    return data.get("response")


def build_team_ratings(standings_response):
    ratings = {}

    for league_data in standings_response or []:
        standings_groups = league_data.get("league", {}).get("standings", [])

        rows = []

        for group in standings_groups:
            for row in group:
                team = row.get("team", {})
                all_stats = row.get("all", {})

                team_id = team.get("id")
                name = team.get("name")

                played = all_stats.get("played") or 0
                goals = all_stats.get("goals", {})

                goals_for = goals.get("for") or 0
                goals_against = goals.get("against") or 0
                rank = row.get("rank")

                if team_id and name and played > 0:
                    rows.append(
                        {
                            "id": team_id,
                            "name": name,
                            "played": played,
                            "gf": goals_for,
                            "ga": goals_against,
                            "rank": rank,
                        }
                    )

        if not rows:
            continue

        avg_for = sum(r["gf"] / r["played"] for r in rows) / len(rows)
        avg_against = sum(r["ga"] / r["played"] for r in rows) / len(rows)

        if avg_for <= 0:
            avg_for = 1.30

        if avg_against <= 0:
            avg_against = 1.30

        for r in rows:
            team_for_rate = r["gf"] / r["played"]
            team_against_rate = r["ga"] / r["played"]

            attack = team_for_rate / avg_for
            defense = team_against_rate / avg_against

            attack = max(0.40, min(2.50, attack))
            defense = max(0.40, min(2.50, defense))

            ratings[str(r["id"])] = {
                "name": r["name"],
                "attack": attack,
                "defense": defense,
                "rank": r["rank"],
            }

    return ratings


def get_expected_goals(home_team_id, away_team_id, ratings: dict):
    home_rating = ratings.get(str(home_team_id)) if home_team_id else None
    away_rating = ratings.get(str(away_team_id)) if away_team_id else None

    base_home = 1.35
    base_away = 1.15

    if not home_rating or not away_rating:
        return base_home, base_away, 0.35

    home_attack = home_rating.get("attack", 1.0)
    home_defense = home_rating.get("defense", 1.0)

    away_attack = away_rating.get("attack", 1.0)
    away_defense = away_rating.get("defense", 1.0)

    lh = base_home * home_attack * away_defense
    la = base_away * away_attack * home_defense

    return clamp_xg(lh), clamp_xg(la), 0.65


def has_predictions(db: Session, fixture_id: int) -> bool:
    count = db.query(Prediction).filter(
        Prediction.fixture_id == fixture_id
    ).count()

    return count > 0


def safe_confidence_score(prob: float, data_quality: float) -> int:
    score = (float(prob) * 100 * 0.70) + (float(data_quality) * 100 * 0.30)

    if data_quality < 0.55:
        score = min(score, 54)

    return max(5, min(94, int(round(score))))


def safe_confidence_label(score: int) -> str:
    if score >= 75:
        return "High"
    if score >= 60:
        return "Medium"
    return "Low"


def cs_htft_confidence_score(prob: float, data_quality: float, market: str) -> int:
    if market == "CS":
        p_norm = min(1.0, float(prob) / 0.18)
    else:
        p_norm = min(1.0, float(prob) / 0.40)

    score = (0.55 * p_norm + 0.45 * float(data_quality)) * 100

    if data_quality < 0.55:
        score = min(score, 54)

    return max(5, min(94, int(round(score))))


def safe_market_predictions(scores: dict, home_team: str, away_team: str):
    p00 = scores.get((0, 0), 0.0)

    over_05 = clamp_prob(1.0 - p00)

    over_15 = clamp_prob(
        1.0
        - p00
        - scores.get((1, 0), 0.0)
        - scores.get((0, 1), 0.0)
    )

    over_25 = clamp_prob(
        sum(p for (h, a), p in scores.items() if h + a >= 3)
    )

    under_35 = clamp_prob(
        sum(p for (h, a), p in scores.items() if h + a <= 3)
    )

    under_45 = clamp_prob(
        sum(p for (h, a), p in scores.items() if h + a <= 4)
    )

    btts_yes = clamp_prob(
        sum(p for (h, a), p in scores.items() if h > 0 and a > 0)
    )

    btts_no = clamp_prob(1.0 - btts_yes)

    home_win = clamp_prob(
        sum(p for (h, a), p in scores.items() if h > a)
    )

    draw = clamp_prob(
        sum(p for (h, a), p in scores.items() if h == a)
    )

    away_win = clamp_prob(
        sum(p for (h, a), p in scores.items() if a > h)
    )

    dc_1x = clamp_prob(home_win + draw)
    dc_x2 = clamp_prob(away_win + draw)
    dc_12 = clamp_prob(home_win + away_win)

    no_draw_denominator = home_win + away_win

    home_dnb = clamp_prob(
        home_win / no_draw_denominator
        if no_draw_denominator > 0
        else 0.0
    )

    away_dnb = clamp_prob(
        away_win / no_draw_denominator
        if no_draw_denominator > 0
        else 0.0
    )

    return [
        ("OVER_05", "Over 0.5 Goals", over_05),
        ("OVER_15", "Over 1.5 Goals", over_15),
        ("OVER_25", "Over 2.5 Goals", over_25),
        ("UNDER_35", "Under 3.5 Goals", under_35),
        ("UNDER_45", "Under 4.5 Goals", under_45),
        ("BTTS_YES", "Both Teams To Score - Yes", btts_yes),
        ("BTTS_NO", "Both Teams To Score - No", btts_no),
        ("DC_1X", f"{home_team} or Draw", dc_1x),
        ("DC_X2", f"{away_team} or Draw", dc_x2),
        ("DC_12", f"{home_team} or {away_team}", dc_12),
        ("HOME_DNB", f"{home_team} Draw No Bet", home_dnb),
        ("AWAY_DNB", f"{away_team} Draw No Bet", away_dnb),
    ]


def generate_predictions_for_fixture(
    db: Session,
    fixture: Fixture,
    lh: float,
    la: float,
    data_quality: float,
) -> bool:
    if has_predictions(db, fixture.id):
        return False

    home_team = fixture.home_team
    away_team = fixture.away_team

    scores = score_matrix(lh, la)

    # Correct Score
    top_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_score, best_score_prob = top_scores[0]

    cs_pick = f"{best_score[0]}-{best_score[1]}"
    cs_score = cs_htft_confidence_score(best_score_prob, data_quality, "CS")

    db.add(
        Prediction(
            fixture_id=fixture.id,
            market="CS",
            pick=cs_pick,
            probability=best_score_prob,
            confidence_score=cs_score,
            confidence_label=confidence_label(cs_score),
            status="pending",
        )
    )

    # HT/FT
    htft_probs = ht_ft(lh, la)
    top_htft = sorted(htft_probs.items(), key=lambda item: item[1], reverse=True)
    best_htft, best_htft_prob = top_htft[0]

    htft_score = cs_htft_confidence_score(best_htft_prob, data_quality, "HTFT")

    db.add(
        Prediction(
            fixture_id=fixture.id,
            market="HTFT",
            pick=best_htft,
            probability=best_htft_prob,
            confidence_score=htft_score,
            confidence_label=confidence_label(htft_score),
            status="pending",
        )
    )

    # Safe markets
    safe_markets = safe_market_predictions(scores, home_team, away_team)

    for market, pick, prob in safe_markets:
        if prob < 0.55:
            continue

        score = safe_confidence_score(prob, data_quality)

        db.add(
            Prediction(
                fixture_id=fixture.id,
                market=market,
                pick=pick,
                probability=prob,
                confidence_score=score,
                confidence_label=safe_confidence_label(score),
                status="pending",
            )
        )

    db.commit()

    print(f"Generated predictions for: {home_team} vs {away_team}")

    return True


def process_fixture(db: Session, item: dict, ratings: dict, counters: dict):
    league = item.get("league") or {}
    league_name = league.get("name")

    if is_blocked_league(league_name):
        counters["skipped"] += 1
        return

    api_fixture = item.get("fixture", {})
    teams = item.get("teams", {})
    goals = item.get("goals") or {}
    halftime = (item.get("score") or {}).get("halftime", {})

    api_fixture_id = api_fixture.get("id")
    status = api_fixture.get("status", {}).get("short") or "NS"
    kickoff = parse_api_date(api_fixture.get("date"))

    home_team = teams.get("home", {}).get("name")
    away_team = teams.get("away", {}).get("name")

    home_team_id = teams.get("home", {}).get("id")
    away_team_id = teams.get("away", {}).get("id")
    if is_blocked_text(home_team) or is_blocked_text(away_team):
        counters["skipped"] += 1
        return
    if not api_fixture_id or not home_team or not away_team:
        return
    if status not in PREDICTABLE_STATUSES:
        return

    lh, la, data_quality = get_expected_goals(
        home_team_id,
        away_team_id,
        ratings,
    )

    if data_quality < 0.55:
        counters["unrated_skipped"] += 1
        return
    existing_fixture = db.query(Fixture).filter(
        Fixture.api_fixture_id == api_fixture_id
    ).first()

    if not existing_fixture:
        fixture = Fixture(
            api_fixture_id=api_fixture_id,
            league=league_name,
            home_team=home_team,
            away_team=away_team,
            kickoff=kickoff,
            status=status,
            home_goals=goals.get("home"),
            away_goals=goals.get("away"),
            ht_home_goals=halftime.get("home"),
            ht_away_goals=halftime.get("away"),
        )

        db.add(fixture)
        db.commit()
        db.refresh(fixture)
    else:
        fixture = existing_fixture

        fixture.status = status
        fixture.home_goals = goals.get("home")
        fixture.away_goals = goals.get("away")
        fixture.ht_home_goals = halftime.get("home")
        fixture.ht_away_goals = halftime.get("away")

        db.commit()
        db.refresh(fixture)

    counters["processed"] += 1
    counters["rated"] += 1

    if generate_predictions_for_fixture(db, fixture, lh, la, data_quality):
        counters["generated"] += 1

def sync_fixtures_and_predictions():
    if not API_KEY:
        print("ERROR: API_FOOTBALL_KEY is missing in .env")
        return

    print("Connecting to API-Football...")

    db: Session = SessionLocal()

    counters = {
        "processed": 0,
        "generated": 0,
        "skipped": 0,
        "rated": 0,
        "unrated_skipped": 0,
    }
    today = date.today()
    tomorrow = today + timedelta(days=1)

    dates = [today, tomorrow]

    all_fixtures = []

    for target_date in dates:
        print(f"Fetching fixtures for {target_date.isoformat()}...")

        fixtures = fetch_fixtures_for_date(target_date)

        print(f"Found {len(fixtures)} matches.")

        all_fixtures.extend(fixtures)

    league_info = {}

    for item in all_fixtures:
        league = item.get("league") or {}
        league_name = league.get("name")

        if is_blocked_league(league_name):
            continue

        league_id = league.get("id")
        season = league.get("season")

        if league_id and season:
            key = f"{league_id}:{season}"

            info = league_info.get(key)

            if not info:
                league_info[key] = {
                    "count": 1,
                    "league_id": league_id,
                    "season": season,
                    "name": league_name,
                    "priority": is_priority_league(league_name),
                }
            else:
                info["count"] += 1

                if is_priority_league(league_name):
                    info["priority"] = True

    cache = load_ratings_cache()
    ratings = {}
    requests_used = 0
    today_str = today.isoformat()

    sorted_leagues = sorted(
        league_info.items(),
        key=lambda item: (
            item[1].get("priority", False),
            item[1].get("count", 0),
        ),
        reverse=True,
    )

    print(f"Found {len(sorted_leagues)} active leagues.")
    print(f"Standings request budget: {MAX_STANDINGS_REQUESTS}")
    print(f"Ratings cache validity: {RATINGS_CACHE_DAYS} days")

    for key, info in sorted_leagues:
        entry = cache.get(key)

        if entry and cache_is_fresh(entry):
            ratings.update(entry.get("teams", {}))
            continue

        if requests_used >= MAX_STANDINGS_REQUESTS:
            continue

        league_id = info.get("league_id")
        season = info.get("season")
        count = info.get("count", 0)

        print(
            f"Fetching standings: league={league_id}, "
            f"season={season}, fixtures={count}, "
            f"priority={info.get('priority', False)}"
        )

        standings_response = fetch_standings(league_id, season)
        requests_used += 1

        teams = {}

        if standings_response:
            teams = build_team_ratings(standings_response)

        cache[key] = {
            "updated": today_str,
            "teams": teams,
        }

        ratings.update(teams)

        time.sleep(0.25)

    save_ratings_cache(cache)

    print(f"Loaded ratings for {len(ratings)} teams.")

    for item in all_fixtures:
        process_fixture(db, item, ratings, counters)

    db.close()

    print(f"Processed rated fixtures: {counters['processed']}")
    print(f"Skipped friendlies/blocked: {counters['skipped']}")
    print(f"Skipped unrated fixtures: {counters['unrated_skipped']}")
    print(f"Rated fixtures: {counters['rated']}")
    print(f"Generated predictions for {counters['generated']} fixtures.")
    print("Sync complete.")


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    sync_fixtures_and_predictions()