import os
import re
import json
import math
import time
import requests
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import SessionLocal
from sports_models import Fixture, Prediction


load_dotenv()

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
API_STANDINGS_URL = "https://v3.football.api-sports.io/standings"

try:
    MAX_STANDINGS_REQUESTS = int(os.getenv("MAX_STANDINGS_REQUESTS", "25"))
except Exception:
    MAX_STANDINGS_REQUESTS = 25

try:
    FREE_PLAN_MAX_SEASON = int(os.getenv("FREE_PLAN_MAX_SEASON", "2024"))
except Exception:
    FREE_PLAN_MAX_SEASON = 2024

try:
    RATINGS_CACHE_DAYS = int(os.getenv("RATINGS_CACHE_DAYS", "3"))
except Exception:
    RATINGS_CACHE_DAYS = 3


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
}

YOUTH_PATTERN = re.compile(r"\bu\d{1,2}\b")

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

QUALITY_LEAGUE_KEYWORDS = {
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
    "championship", "league one", "league two", "eredivisie",
    "primeira liga", "liga portugal", "segunda", "la liga 2",
    "champions league", "europa league", "conference league",
    "mls", "brasileiro", "brazil", "argentina", "liga profesional",
    "super lig", "süper lig", "scotland", "premiership",
    "belgium", "jupiler", "denmark", "norway", "sweden", "allsvenskan",
    "austria", "switzerland", "greece", "croatia", "czech",
    "russia", "ukraine", "poland", "saudi", "egypt", "morocco",
    "south africa", "japan", "j1", "j2", "k league", "a-league",
    "mexico", "liga mx", "colombia", "chile", "ecuador",
    "serie b", "2. bundesliga", "ligue 2",
}

PREDICTABLE_STATUSES = {"NS", "TBD", "NOT_STARTED", ""}

LEAGUE_AVG_GOALS = 1.35


# ---------------------------------------------------------------- helpers
def normalize_league(name):
    return (name or "").strip().lower()


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


def is_priority_league(league_name):
    normalized = normalize_league(league_name)

    for keyword in PRIORITY_LEAGUE_KEYWORDS:
        if keyword in normalized:
            return True

    return False


def is_quality_league(league_name):
    normalized = normalize_league(league_name)

    for keyword in QUALITY_LEAGUE_KEYWORDS:
        if keyword in normalized:
            return True

    return False


def clamp_prob(value):
    return max(0.0, min(1.0, value))


def clamp_xg(value):
    return max(0.2, min(3.5, value))


def parse_kickoff(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def filter_columns(model, kwargs):
    cols = model.__table__.columns.keys()
    return {k: v for k, v in kwargs.items() if k in cols}


def make_fixture(**kwargs):
    return Fixture(**filter_columns(Fixture, kwargs))


def make_prediction(**kwargs):
    return Prediction(**filter_columns(Prediction, kwargs))


# ---------------------------------------------------------------- ratings cache (database)
def load_ratings_cache(db: Session):
    from sports_models import RatingsCache

    cache = {}

    try:
        rows = db.query(RatingsCache).all()
    except Exception:
        return cache

    for row in rows:
        try:
            teams_raw = json.loads(row.teams_json or "{}")
        except Exception:
            continue

        teams = {}
        for k, v in teams_raw.items():
            try:
                teams[int(k)] = v
            except Exception:
                continue

        cache[row.cache_key] = {
            "updated": row.updated,
            "teams": teams,
        }

    return cache


def save_ratings_cache(db: Session, cache):
    from sports_models import RatingsCache

    for key, entry in cache.items():
        row = db.query(RatingsCache).filter(
            RatingsCache.cache_key == key
        ).first()

        teams_json = json.dumps(entry.get("teams", {}), ensure_ascii=False)

        if not row:
            db.add(RatingsCache(
                cache_key=key,
                updated=entry.get("updated"),
                teams_json=teams_json,
            ))
        else:
            row.updated = entry.get("updated")
            row.teams_json = teams_json

    db.commit()


def cache_is_fresh(entry):
    updated = entry.get("updated")

    if not updated:
        return False

    try:
        updated_date = date.fromisoformat(updated)
    except Exception:
        return False

    age_days = (date.today() - updated_date).days

    ttl_days = RATINGS_CACHE_DAYS if entry.get("teams") else 1

    return age_days <= ttl_days


# ---------------------------------------------------------------- API calls
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

    if data.get("errors"):
        print(f"API-Football errors: {data.get('errors')}")

    return data.get("response", [])


def fetch_standings(league_id, season):
    params = {
        "league": league_id,
        "season": season,
    }

    try:
        response = requests.get(
            API_STANDINGS_URL,
            headers=api_headers(),
            params=params,
            timeout=30,
        )
        data = response.json()
    except Exception as e:
        print(f"Failed to connect to standings API: {e}")
        return None

    if response.status_code != 200:
        return None

    errors = data.get("errors") or {}

    if errors and season != FREE_PLAN_MAX_SEASON:
        print(f"Season {season} blocked, falling back to {FREE_PLAN_MAX_SEASON}")
        return fetch_standings(league_id, FREE_PLAN_MAX_SEASON)

    return data.get("response") or None


# ---------------------------------------------------------------- model
def build_team_ratings(standings_response):
    teams = {}

    for entry in standings_response:
        league_data = entry.get("league") or {}
        standings = league_data.get("standings") or []

        for table in standings:
            for row in table:
                team = row.get("team") or {}
                team_id = team.get("id")

                stats = row.get("all") or {}
                goals = stats.get("goals") or {}
                played = stats.get("played") or 0

                if not team_id or played <= 0:
                    continue

                gf = goals.get("for") or 0
                ga = goals.get("against") or 0
                points = row.get("points") or 0
                form = row.get("form") or ""

                teams[team_id] = {
                    "name": team.get("name"),
                    "attack": gf / played,
                    "defense": ga / played,
                    "points_per_game": points / played,
                    "form": form,
                }

    return teams


def form_score(form):
    if not form:
        return 0.5

    values = {"W": 1.0, "D": 0.5, "L": 0.0}
    chars = [c for c in form.upper() if c in values]

    if not chars:
        return 0.5

    return sum(values[c] for c in chars) / len(chars)


def get_expected_goals(home_team_id, away_team_id, ratings):
    home = ratings.get(home_team_id)
    away = ratings.get(away_team_id)

    if not home or not away:
        return 1.30, 1.10, 0.35

    home_form = form_score(home.get("form"))
    away_form = form_score(away.get("form"))

    home_base = (home["attack"] * away["defense"]) / LEAGUE_AVG_GOALS
    away_base = (away["attack"] * home["defense"]) / LEAGUE_AVG_GOALS

    home_base *= 1.15
    away_base *= 0.95

    home_xg = clamp_xg(home_base * (0.85 + 0.30 * home_form))
    away_xg = clamp_xg(away_base * (0.85 + 0.30 * away_form))

    return home_xg, away_xg, 0.75


def poisson_p(lam, k):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def score_matrix(lh, la, max_goals=6):
    scores = {}

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            scores[(h, a)] = poisson_p(lh, h) * poisson_p(la, a)

    total = sum(scores.values())

    if total > 0:
        scores = {k: v / total for k, v in scores.items()}

    return scores


def confidence_from_prob(prob):
    score = int(round(prob * 100))

    if score >= 75:
        label = "High"
    elif score >= 60:
        label = "Medium"
    else:
        label = "Low"

    return score, label


def safe_market_predictions(scores: dict, home_team: str, away_team: str):
    p00 = scores.get((0, 0), 0.0)

    over_05 = clamp_prob(1.0 - p00)
    over_15 = clamp_prob(1.0 - p00 - scores.get((1, 0), 0.0) - scores.get((0, 1), 0.0))
    over_25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a >= 3))
    over_35 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a >= 4))

    under_35 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a <= 3))
    under_45 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a <= 4))

    btts_yes = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > 0 and a > 0))
    btts_no = clamp_prob(1.0 - btts_yes)

    btts_o25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > 0 and a > 0 and h + a >= 3))

    home_win = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > a))
    draw = clamp_prob(sum(pr for (h, a), pr in scores.items() if h == a))
    away_win = clamp_prob(sum(pr for (h, a), pr in scores.items() if a > h))

    dc_1x = clamp_prob(home_win + draw)
    dc_x2 = clamp_prob(away_win + draw)
    dc_12 = clamp_prob(home_win + away_win)

    no_draw_denominator = home_win + away_win
    home_dnb = clamp_prob(home_win / no_draw_denominator if no_draw_denominator > 0 else 0.0)
    away_dnb = clamp_prob(away_win / no_draw_denominator if no_draw_denominator > 0 else 0.0)

    home_over_15 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h >= 2))
    away_over_15 = clamp_prob(sum(pr for (h, a), pr in scores.items() if a >= 2))

    win_nil_home = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > a and a == 0))
    win_nil_away = clamp_prob(sum(pr for (h, a), pr in scores.items() if a > h and h == 0))

    home_multi_scores = [(2, 0), (2, 1), (3, 0), (3, 1)]
    away_multi_scores = [(0, 2), (1, 2), (0, 3), (1, 3)]
    draw_multi_scores = [(1, 1), (2, 2)]

    home_multi_prob = clamp_prob(sum(scores.get(s, 0) for s in home_multi_scores))
    away_multi_prob = clamp_prob(sum(scores.get(s, 0) for s in away_multi_scores))
    draw_multi_prob = clamp_prob(sum(scores.get(s, 0) for s in draw_multi_scores))

    return [
        ("OVER_05", "Over 0.5 Goals", over_05),
        ("OVER_15", "Over 1.5 Goals", over_15),
        ("OVER_25", "Over 2.5 Goals", over_25),
        ("OVER_35", "Over 3.5 Goals", over_35),
        ("UNDER_35", "Under 3.5 Goals", under_35),
        ("UNDER_45", "Under 4.5 Goals", under_45),
        ("BTTS_YES", "Both Teams To Score - Yes", btts_yes),
        ("BTTS_NO", "Both Teams To Score - No", btts_no),
        ("BTTS_O25", "BTTS & Over 2.5 Goals", btts_o25),
        ("DC_1X", f"{home_team} or Draw", dc_1x),
        ("DC_X2", f"{away_team} or Draw", dc_x2),
        ("DC_12", f"{home_team} or {away_team}", dc_12),
        ("HOME_DNB", f"{home_team} Draw No Bet", home_dnb),
        ("AWAY_DNB", f"{away_team} Draw No Bet", away_dnb),
        ("HOME_OVER_15", f"{home_team} To Score Over 1.5 Goals", home_over_15),
        ("AWAY_OVER_15", f"{away_team} To Score Over 1.5 Goals", away_over_15),
        ("WIN_TO_NIL_HOME", f"{home_team} To Win To Nil", win_nil_home),
        ("WIN_TO_NIL_AWAY", f"{away_team} To Win To Nil", win_nil_away),
        ("HOME_MULTI", f"{home_team} Multiscore (2-0, 2-1, 3-0, 3-1)", home_multi_prob),
        ("AWAY_MULTI", f"{away_team} Multiscore (0-2, 1-2, 0-3, 1-3)", away_multi_prob),
        ("DRAW_MULTI", "Draw Multiscore (1-1, 2-2)", draw_multi_prob),
    ]


def correct_score_predictions(scores):
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)

    top = ranked[0]
    alts = ranked[1:4]

    return {
        "pick": f"{top[0][0]}-{top[0][1]}",
        "probability": top[1],
        "alternatives": [
            {"score": f"{h}-{a}", "probability": p}
            for (h, a), p in alts
        ],
    }


def ht_ft_predictions(lh, la):
    ht_lh = lh * 0.45
    ht_la = la * 0.45
    sh_lh = max(0.05, lh - ht_lh)
    sh_la = max(0.05, la - ht_la)

    ht_scores = score_matrix(ht_lh, ht_la, max_goals=4)
    sh_scores = score_matrix(sh_lh, sh_la, max_goals=4)

    options = {}

    for (hh, ha), hp in ht_scores.items():
        hr = "H" if hh > ha else "A" if ha > hh else "D"

        for (sh, sa), sp in sh_scores.items():
            fh = hh + sh
            fa = ha + sa
            fr = "H" if fh > fa else "A" if fa > fh else "D"

            key = f"{hr}/{fr}"
            options[key] = options.get(key, 0.0) + hp * sp

    return sorted(options.items(), key=lambda item: item[1], reverse=True)


def generate_predictions_for_fixture(db: Session, fixture, lh, la, data_quality):
    existing = db.query(Prediction).filter(
        Prediction.fixture_id == fixture.id
    ).first()

    if existing:
        return False

    scores = score_matrix(lh, la)
    created = 0

    for market, pick, prob in safe_market_predictions(scores, fixture.home_team, fixture.away_team):
        score, label = confidence_from_prob(prob)
        db.add(make_prediction(
            fixture_id=fixture.id,
            market=market,
            pick=pick,
            probability=round(prob, 4),
            confidence_score=score,
            confidence_label=label,
            status="pending",
        ))
        created += 1

    cs = correct_score_predictions(scores)
    score, label = confidence_from_prob(cs["probability"])
    db.add(make_prediction(
        fixture_id=fixture.id,
        market="CS",
        pick=cs["pick"],
        probability=round(cs["probability"], 4),
        confidence_score=score,
        confidence_label=label,
        status="pending",
    ))
    created += 1

    htft = ht_ft_predictions(lh, la)
    if htft:
        pick, prob = htft[0]
        score, label = confidence_from_prob(prob)
        db.add(make_prediction(
            fixture_id=fixture.id,
            market="HTFT",
            pick=pick,
            probability=round(prob, 4),
            confidence_score=score,
            confidence_label=label,
            status="pending",
        ))
        created += 1

    db.commit()
    return created > 0


# ---------------------------------------------------------------- sync
def process_fixture(db: Session, item, ratings, counters):
    league = item.get("league") or {}
    league_name = league.get("name")

    if is_blocked_league(league_name):
        counters["skipped"] += 1
        return

    if not is_quality_league(league_name):
        counters["skipped"] += 1
        return

    api_fixture = item.get("fixture") or {}
    api_fixture_id = api_fixture.get("id")

    teams = item.get("teams") or {}
    home_team = (teams.get("home") or {}).get("name")
    away_team = (teams.get("away") or {}).get("name")
    home_team_id = (teams.get("home") or {}).get("id")
    away_team_id = (teams.get("away") or {}).get("id")

    if is_blocked_text(home_team) or is_blocked_text(away_team):
        counters["skipped"] += 1
        return

    if not api_fixture_id or not home_team or not away_team:
        return

    status = api_fixture.get("status", {}).get("short") or ""

    if status not in PREDICTABLE_STATUSES:
        return

    lh, la, data_quality = get_expected_goals(home_team_id, away_team_id, ratings)

    if data_quality < 0.55:
        counters["unrated_skipped"] += 1
        return

    kickoff = parse_kickoff(api_fixture.get("date"))

    existing_fixture = db.query(Fixture).filter(
        Fixture.api_fixture_id == api_fixture_id
    ).first()

    if not existing_fixture:
        fixture = make_fixture(
            api_fixture_id=api_fixture_id,
            league=league_name,
            home_team=home_team,
            away_team=away_team,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            kickoff=kickoff,
            status=status,
        )
        db.add(fixture)
        db.commit()
        db.refresh(fixture)
    else:
        fixture = existing_fixture
        fixture.status = status
        db.commit()

    counters["processed"] += 1
    counters["rated"] += 1

    if generate_predictions_for_fixture(db, fixture, lh, la, data_quality):
        counters["generated"] += 1


def sync_fixtures_and_predictions():
    if not API_KEY:
        print("ERROR: API_FOOTBALL_KEY is missing in .env")
        return

    db = SessionLocal()

    today = date.today()
    dates = [today, today + timedelta(days=1)]

    print("Connecting to API-Football...")

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

        if not is_quality_league(league_name):
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

    cache = load_ratings_cache(db)
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
            f"Fetching standings: league={league_id}, season={season}, "
            f"fixtures={count}, priority={info.get('priority', False)}"
        )

        standings_response = fetch_standings(league_id, season)
        requests_used += 1

        teams = {}

        if standings_response:
            teams = build_team_ratings(standings_response)
            print(f"Standings loaded: league={league_id}, teams={len(teams)}")

        cache[key] = {
            "updated": today_str,
            "teams": teams,
        }

        ratings.update(teams)

        time.sleep(0.25)

    save_ratings_cache(db, cache)

    print(f"Loaded ratings for {len(ratings)} teams.")

    counters = {
        "processed": 0,
        "generated": 0,
        "skipped": 0,
        "rated": 0,
        "unrated_skipped": 0,
    }

    for item in all_fixtures:
        process_fixture(db, item, ratings, counters)

    print(f"Processed rated fixtures: {counters['processed']}")
    print(f"Skipped friendlies/blocked: {counters['skipped']}")
    print(f"Skipped unrated fixtures: {counters['unrated_skipped']}")
    print(f"Rated fixtures: {counters['rated']}")
    print(f"Generated predictions for {counters['generated']} fixtures.")
    print("Sync complete.")

    db.close()


if __name__ == "__main__":
    sync_fixtures_and_predictions()