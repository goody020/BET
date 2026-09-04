import os
import json
import math
import time
import requests
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import SessionLocal
from sports_models import Fixture, Prediction
from espn_adapter import (
    EXTRA_LEAGUES,
    RATING_LEAGUES,
    fetch_scoreboard,
    fetch_standings_ratings,
    espn_fixture_id,
)


load_dotenv()

API_KEY = os.getenv("FOOTBALL_DATA_KEY", "").strip()
BASE_URL = "https://api.football-data.org/v4"

try:
    RATINGS_CACHE_DAYS = int(os.getenv("RATINGS_CACHE_DAYS", "3"))
except Exception:
    RATINGS_CACHE_DAYS = 3

QUALITY_LEAGUE_CODES = {"PL", "PD", "BL1", "SA", "FL1", "CL", "EL", "DED", "PPL", "BSA", "ELC", "WC", "CLI", "EC"}
PRIORITY_LEAGUE_CODES = {"PL", "PD", "BL1", "SA", "FL1", "CL", "EL"}

PREDICTABLE_STATUSES = {"SCHEDULED", "TIMED"}
LEAGUE_AVG_GOALS = 1.35


# ---------------------------------------------------------------- helpers
def norm_name(n):
    return (n or "").strip().lower()


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
            cache[row.cache_key] = {
                "updated": row.updated,
                "teams": json.loads(row.teams_json or "{}"),
            }
        except Exception:
            continue

    return cache


def save_ratings_cache(db: Session, cache):
    from sports_models import RatingsCache

    for key, entry in cache.items():
        row = db.query(RatingsCache).filter(RatingsCache.cache_key == key).first()
        teams_json = json.dumps(entry.get("teams", {}), ensure_ascii=False)

        if not row:
            db.add(RatingsCache(cache_key=key, updated=entry.get("updated"), teams_json=teams_json))
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


# ---------------------------------------------------------------- football-data API
def api_headers():
    return {"X-Auth-Token": API_KEY}


def fetch_matches_for_dates(date_from: date, date_to: date):
    url = f"{BASE_URL}/matches"
    params = {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}

    print(f"Requesting matches from {date_from} to {date_to}...")
    try:
        response = requests.get(url, headers=api_headers(), params=params, timeout=30)
        print(f"API status: {response.status_code}")
        data = response.json()
    except Exception as e:
        print(f"Failed to connect to API: {e}")
        return []

    if response.status_code != 200:
        print(f"API Error: {data}")
        return []

    return data.get("matches", [])


def fetch_standings(comp_code):
    url = f"{BASE_URL}/competitions/{comp_code}/standings"

    print(f"Fetching standings for {comp_code}...")
    try:
        response = requests.get(url, headers=api_headers(), timeout=30)
        data = response.json()
    except Exception as e:
        print(f"Failed to connect to standings API: {e}")
        return None

    if response.status_code != 200:
        print(f"Standings API Error: {data}")
        return None

    return data.get("standings") or None


# ---------------------------------------------------------------- model
def build_team_ratings(standings_response):
    teams = {}

    for standing in standings_response:
        for row in standing.get("table", []):
            team = row.get("team", {})
            name = team.get("name")
            played = row.get("playedGames", 0)

            if not name or played <= 0:
                continue

            gf = row.get("goalsFor", 0)
            ga = row.get("goalsAgainst", 0)
            points = row.get("points", 0)
            form = row.get("form", "")

            teams[norm_name(name)] = {
                "name": name,
                "attack": gf / played,
                "defense": ga / played,
                "points_per_game": points / played,
                "form": form,
            }

    return teams


def form_score(form):
    if not form:
        return 0.5
    chars = [c for c in form.replace(",", "").upper() if c in "WDL"]
    if not chars:
        return 0.5
    values = {"W": 1.0, "D": 0.5, "L": 0.0}
    return sum(values[c] for c in chars) / len(chars)


def get_expected_goals(home_name, away_name, ratings):
    home = ratings.get(norm_name(home_name))
    away = ratings.get(norm_name(away_name))

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


def safe_market_predictions(scores: dict, home_team: str, away_team: str, lh: float, la: float):
    p00 = scores.get((0, 0), 0.0)

    over_05 = clamp_prob(1.0 - p00)
    over_15 = clamp_prob(1.0 - p00 - scores.get((1, 0), 0.0) - scores.get((0, 1), 0.0))
    over_25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a >= 3))
    over_35 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a >= 4))
    under_35 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a <= 3))
    under_45 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h + a <= 4))
    under_15 = clamp_prob(1.0 - over_15)
    under_25 = clamp_prob(1.0 - over_25)
    over_45 = clamp_prob(1.0 - under_45)

    odd_goals = clamp_prob(sum(pr for (h, a), pr in scores.items() if (h + a) % 2 == 1))
    even_goals = clamp_prob(1.0 - odd_goals)

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

    home_over_05 = clamp_prob(1.0 - sum(pr for (h, a), pr in scores.items() if h == 0))
    away_over_05 = clamp_prob(1.0 - sum(pr for (h, a), pr in scores.items() if a == 0))
    home_over_25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h >= 3))
    away_over_25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if a >= 3))

    win_nil_home = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > a and a == 0))
    win_nil_away = clamp_prob(sum(pr for (h, a), pr in scores.items() if a > h and h == 0))

    home_multi_low = clamp_prob(sum(scores.get(s, 0) for s in [(1, 0), (2, 0)]))
    home_multi_high = clamp_prob(sum(scores.get(s, 0) for s in [(2, 1), (3, 1)]))
    away_multi_low = clamp_prob(sum(scores.get(s, 0) for s in [(0, 1), (0, 2)]))
    away_multi_high = clamp_prob(sum(scores.get(s, 0) for s in [(1, 2), (1, 3)]))
    draw_multi = clamp_prob(sum(scores.get(s, 0) for s in [(1, 1), (2, 2)]))

    home_margin_2 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h - a >= 2))
    away_margin_2 = clamp_prob(sum(pr for (h, a), pr in scores.items() if a - h >= 2))

    # Combo coupons (SportyBet staples): winner + goals line in one ticket
    home_o25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > a and h + a >= 3))
    away_o25 = clamp_prob(sum(pr for (h, a), pr in scores.items() if a > h and h + a >= 3))
    home_gg = clamp_prob(sum(pr for (h, a), pr in scores.items() if h > a and a > 0))
    away_gg = clamp_prob(sum(pr for (h, a), pr in scores.items() if a > h and h > 0))

    ht_scores = score_matrix(lh * 0.45, la * 0.45)
    ht_over_05 = clamp_prob(1.0 - ht_scores.get((0, 0), 0.0))
    ht_over_15 = clamp_prob(sum(pr for (h, a), pr in ht_scores.items() if h + a >= 2))
    ht_home_multi = clamp_prob(sum(ht_scores.get(s, 0) for s in [(1, 0), (2, 0)]))
    ht_away_multi = clamp_prob(sum(ht_scores.get(s, 0) for s in [(0, 1), (0, 2)]))
    ht_draw_multi = clamp_prob(sum(ht_scores.get(s, 0) for s in [(0, 0), (1, 1)]))

    return [
        ("OVER_05", "Over 0.5 Goals", over_05),
        ("OVER_15", "Over 1.5 Goals", over_15),
        ("OVER_25", "Over 2.5 Goals", over_25),
        ("OVER_35", "Over 3.5 Goals", over_35),
        ("OVER_45", "Over 4.5 Goals", over_45),
        ("UNDER_15", "Under 1.5 Goals", under_15),
        ("UNDER_25", "Under 2.5 Goals", under_25),
        ("UNDER_35", "Under 3.5 Goals", under_35),
        ("UNDER_45", "Under 4.5 Goals", under_45),
        ("ODD_GOALS", "Odd Number Of Goals", odd_goals),
        ("EVEN_GOALS", "Even Number Of Goals", even_goals),
        ("DRAW", "Draw", draw),
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
        ("HOME_OVER_05", f"{home_team} To Score", home_over_05),
        ("AWAY_OVER_05", f"{away_team} To Score", away_over_05),
        ("HOME_OVER_25", f"{home_team} To Score Over 2.5 Goals", home_over_25),
        ("AWAY_OVER_25", f"{away_team} To Score Over 2.5 Goals", away_over_25),
        ("WIN_TO_NIL_HOME", f"{home_team} To Win To Nil", win_nil_home),
        ("WIN_TO_NIL_AWAY", f"{away_team} To Win To Nil", win_nil_away),
        ("HOME_O25", f"{home_team} To Win & Over 2.5 Goals", home_o25),
        ("AWAY_O25", f"{away_team} To Win & Over 2.5 Goals", away_o25),
        ("HOME_GG", f"{home_team} To Win & Both Teams Score", home_gg),
        ("AWAY_GG", f"{away_team} To Win & Both Teams Score", away_gg),
        ("HT_OVER_05", "1st Half Over 0.5 Goals", ht_over_05),
        ("HT_OVER_15", "1st Half Over 1.5 Goals", ht_over_15),
        ("HOME_MULTI_LOW", f"{home_team} Multiscore (1-0, 2-0)", home_multi_low),
        ("HOME_MULTI_HIGH", f"{home_team} Multiscore (2-1, 3-1)", home_multi_high),
        ("AWAY_MULTI_LOW", f"{away_team} Multiscore (0-1, 0-2)", away_multi_low),
        ("AWAY_MULTI_HIGH", f"{away_team} Multiscore (1-2, 1-3)", away_multi_high),
        ("DRAW_MULTI", "Draw Multiscore (1-1, 2-2)", draw_multi),
        ("HOME_MARGIN_2", f"{home_team} To Win By 2+ Goals", home_margin_2),
        ("AWAY_MARGIN_2", f"{away_team} To Win By 2+ Goals", away_margin_2),
        ("HT_HOME_MULTI", f"{home_team} To Lead At HT (1-0, 2-0)", ht_home_multi),
        ("HT_AWAY_MULTI", f"{away_team} To Lead At HT (0-1, 0-2)", ht_away_multi),
        ("HT_DRAW_MULTI", "Draw At HT (0-0, 1-1)", ht_draw_multi),
        ("HOME_WIN", f"{home_team} To Win", home_win),
        ("AWAY_WIN", f"{away_team} To Win", away_win),
    ]


def correct_score_predictions(scores):
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top = ranked[0]
    alts = ranked[1:4]
    return {
        "pick": f"{top[0][0]}-{top[0][1]}",
        "probability": top[1],
        "alternatives": [{"score": f"{h}-{a}", "probability": p} for (h, a), p in alts],
    }


def ht_ft_predictions(lh, la):
    ht_lh, ht_la = lh * 0.45, la * 0.45
    sh_lh, sh_la = max(0.05, lh - ht_lh), max(0.05, la - ht_la)
    ht_scores = score_matrix(ht_lh, ht_la, max_goals=4)
    sh_scores = score_matrix(sh_lh, sh_la, max_goals=4)

    options = {}
    for (hh, ha), hp in ht_scores.items():
        hr = "H" if hh > ha else "A" if ha > hh else "D"
        for (sh, sa), sp in sh_scores.items():
            fh, fa = hh + sh, ha + sa
            fr = "H" if fh > fa else "A" if fa > fh else "D"
            key = f"{hr}/{fr}"
            options[key] = options.get(key, 0.0) + hp * sp

    return sorted(options.items(), key=lambda item: item[1], reverse=True)


def generate_predictions_for_fixture(db: Session, fixture, lh, la, data_quality):
    existing = db.query(Prediction).filter(Prediction.fixture_id == fixture.id).first()
    if existing:
        return False

    scores = score_matrix(lh, la)
    created = 0

    for market, pick, prob in safe_market_predictions(scores, fixture.home_team, fixture.away_team, lh, la):
        score, label = confidence_from_prob(prob)
        db.add(make_prediction(
            fixture_id=fixture.id, market=market, pick=pick,
            probability=round(prob, 4), confidence_score=score,
            confidence_label=label, status="pending",
        ))
        created += 1

    cs = correct_score_predictions(scores)

    # Pure model output, no editorial bans: whatever scoreline the model ranks
    # #1 is the card, shown with its true probability (>= 10% to filter noise).
    if cs["probability"] >= 0.10:
        score, label = confidence_from_prob(cs["probability"])
        db.add(make_prediction(
            fixture_id=fixture.id, market="CS", pick=cs["pick"],
            probability=round(cs["probability"], 4), confidence_score=score,
            confidence_label=label, status="pending",
        ))
        created += 1

    # HT/FT: full 9-combo table, pure argmax — D/D, twists, straights all eligible.
    htft = ht_ft_predictions(lh, la)
    if htft:
        pick, prob = htft[0]
        score, label = confidence_from_prob(prob)
        db.add(make_prediction(
            fixture_id=fixture.id, market="HTFT", pick=pick,
            probability=round(prob, 4), confidence_score=score,
            confidence_label=label, status="pending",
        ))
        created += 1

    db.commit()
    return created > 0


# ---------------------------------------------------------------- fixture processing
def create_and_predict(db, counters, pairs, api_fixture_id, league_name, home_team, away_team, kickoff, status):
    lh, la, data_quality = get_expected_goals(home_team, away_team, ratings_holder["ratings"])

    if data_quality < 0.55:
        counters["unrated_skipped"] += 1
        return

    existing_fixture = db.query(Fixture).filter(Fixture.api_fixture_id == api_fixture_id).first()

    if not existing_fixture:
        fixture = make_fixture(
            api_fixture_id=api_fixture_id, league=league_name,
            home_team=home_team, away_team=away_team,
            kickoff=kickoff, status=status,
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
    pairs.add((norm_name(home_team), norm_name(away_team)))

    if generate_predictions_for_fixture(db, fixture, lh, la, data_quality):
        counters["generated"] += 1


ratings_holder = {"ratings": {}}


def process_match(db: Session, match, counters, pairs):
    comp = match.get("competition", {})
    comp_code = comp.get("code", "")

    if comp_code not in QUALITY_LEAGUE_CODES:
        counters["skipped"] += 1
        return

    api_fixture_id = match.get("id")
    home_team = match.get("homeTeam", {}).get("name")
    away_team = match.get("awayTeam", {}).get("name")

    if not api_fixture_id or not home_team or not away_team:
        return

    status = match.get("status", "")
    if status not in PREDICTABLE_STATUSES:
        return

    create_and_predict(
        db, counters, pairs, api_fixture_id,
        comp.get("name", ""), home_team, away_team,
        parse_kickoff(match.get("utcDate")), status,
    )


def process_espn_event(db: Session, ev, counters, pairs):
    if ev.get("state") != "pre":
        return

    home_team = ev.get("home")
    away_team = ev.get("away")

    pair = (norm_name(home_team), norm_name(away_team))
    if pair in pairs:
        return

    fid = espn_fixture_id(ev.get("id"))
    if not fid:
        return

    create_and_predict(
        db, counters, pairs, fid,
        ev.get("league", ""), home_team, away_team,
        parse_kickoff(ev.get("date")), "NS",
    )


# ---------------------------------------------------------------- sync
def sync_fixtures_and_predictions():
    if not API_KEY:
        print("ERROR: FOOTBALL_DATA_KEY is missing in Environment Variables.")
        return

    db = SessionLocal()
    today = date.today()
    end_date = today + timedelta(days=7)

    print("Connecting to football-data.org...")
    all_matches = fetch_matches_for_dates(today, end_date)
    print(f"Found {len(all_matches)} matches.")

    cache = load_ratings_cache(db)
    today_str = today.isoformat()
    ratings = {}

    # ---- Layer 1: ESPN current-season ratings (base layer)
    print("Loading ESPN current-season ratings...")
    for slug in RATING_LEAGUES:
        key = f"espn:{slug}"
        entry = cache.get(key)

        if entry and cache_is_fresh(entry):
            if entry.get("teams"):
                ratings.update(entry["teams"])
            continue

        teams = fetch_standings_ratings(slug)
        print(f"ESPN ratings: {slug}, teams={len(teams)}")
        cache[key] = {"updated": today_str, "teams": teams}
        if teams:
            ratings.update(teams)
        time.sleep(0.2)

    # ---- Layer 2: football-data ratings (overlay, has form strings)
    comp_codes = {}
    for match in all_matches:
        comp = match.get("competition", {})
        code = comp.get("code", "")
        if code in QUALITY_LEAGUE_CODES:
            if code not in comp_codes:
                comp_codes[code] = {"count": 0, "priority": code in PRIORITY_LEAGUE_CODES}
            comp_codes[code]["count"] += 1

    sorted_comps = sorted(comp_codes.items(), key=lambda item: (item[1]["priority"], item[1]["count"]), reverse=True)

    for code, info in sorted_comps:
        key = f"fd2:{code}"
        entry = cache.get(key)

        if entry and cache_is_fresh(entry):
            if entry.get("teams"):
                ratings.update(entry["teams"])
            continue

        standings_response = fetch_standings(code)
        teams = build_team_ratings(standings_response) if standings_response else {}
        print(f"Standings loaded: {code}, teams={len(teams)}")
        cache[key] = {"updated": today_str, "teams": teams}
        if teams:
            ratings.update(teams)
        time.sleep(0.2)

    save_ratings_cache(db, cache)
    ratings_holder["ratings"] = ratings
    print(f"Loaded ratings for {len(ratings)} teams.")

    counters = {"processed": 0, "generated": 0, "skipped": 0, "rated": 0, "unrated_skipped": 0}
    pairs = set()

    # ---- football-data fixtures (one bad match must never freeze the whole board)
    for match in all_matches:
        try:
            process_match(db, match, counters, pairs)
        except Exception as e:
            db.rollback()
            print(f"Match sync error (skipped, sync continues): {e}")
            continue

    # ---- ESPN extra-league fixtures (4 days)
    print("Loading ESPN extra leagues...")
    for slug in EXTRA_LEAGUES:
        for day_offset in range(0, 4):
            events = fetch_scoreboard(slug, today + timedelta(days=day_offset))
            for ev in events:
                try:
                    process_espn_event(db, ev, counters, pairs)
                except Exception as e:
                    db.rollback()
                    print(f"ESPN event sync error (skipped, sync continues): {e}")
                    continue
            time.sleep(0.15)

    print(f"Processed rated fixtures: {counters['processed']}")
    print(f"Skipped friendlies/blocked: {counters['skipped']}")
    print(f"Skipped unrated fixtures: {counters['unrated_skipped']}")
    print(f"Generated predictions for {counters['generated']} fixtures.")
    print("Sync complete.")
    db.close()


if __name__ == "__main__":
    sync_fixtures_and_predictions()
