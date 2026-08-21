import requests
from datetime import date


BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Leagues football-data.org FREE does not cover -> extra volume
EXTRA_LEAGUES = {
    "sco.1": "Scottish Premiership",
    "tur.1": "Süper Lig",
    "gre.1": "Super League Greece",
    "ksa.1": "Saudi Pro League",
    "egy.1": "Egyptian Premier League",
    "usa.1": "MLS",
    "mex.1": "Liga MX",
    "arg.1": "Liga Profesional Argentina",
    "jpn.1": "J1 League",
    "aus.1": "A-League Men",
    "rsa.1": "South African Premiership",
    "eng.2": "Championship",
}

# Leagues we pull CURRENT-SEASON ratings from (includes the big five)
RATING_LEAGUES = {
    "eng.1": "Premier League",
    "esp.1": "La Liga",
    "ger.1": "Bundesliga",
    "ita.1": "Serie A",
    "fra.1": "Ligue 1",
    "ned.1": "Eredivisie",
    "por.1": "Primeira Liga",
    "bra.1": "Brasileirão",
    **EXTRA_LEAGUES,
}


def fetch_json(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def espn_fixture_id(raw_id):
    try:
        return 1000000000 + (int(raw_id) % 1000000000)
    except Exception:
        return None


def fetch_scoreboard(slug, target_date: date):
    data = fetch_json(
        f"{BASE}/{slug}/scoreboard",
        {"dates": target_date.strftime("%Y%m%d")},
    )

    if not data:
        return []

    league_name = None
    leagues = data.get("leagues") or []
    if leagues:
        league_name = leagues[0].get("name")

    out = []

    for ev in data.get("events") or []:
        comps = ev.get("competitions") or []
        if not comps:
            continue

        competitors = comps[0].get("competitors") or []
        st = ((ev.get("status") or {}).get("type")) or {}
        state = st.get("state")
        completed = bool(st.get("completed")) or state == "post"

        home = away = None
        home_score = away_score = None
        home_ht = away_ht = None

        for c in competitors:
            team = c.get("team") or {}
            entry = {
                "name": team.get("shortDisplayName") or team.get("displayName") or team.get("name"),
            }

            score = None
            if completed:
                try:
                    score = int(c.get("score"))
                except Exception:
                    score = None

            ht = None
            ls = c.get("linescores") or []
            if ls:
                try:
                    ht = int(ls[0].get("value"))
                except Exception:
                    ht = None

            if c.get("homeAway") == "home":
                home = entry
                home_score = score
                home_ht = ht
            else:
                away = entry
                away_score = score
                away_ht = ht

        if not home or not away or not home["name"] or not away["name"]:
            continue

        out.append({
            "id": ev.get("id"),
            "league": league_name or RATING_LEAGUES.get(slug, slug),
            "home": home["name"],
            "away": away["name"],
            "date": ev.get("date"),
            "state": state,
            "home_score": home_score,
            "away_score": away_score,
            "home_ht": home_ht,
            "away_ht": away_ht,
        })

    return out


def fetch_standings_ratings(slug):
    data = fetch_json(f"https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings")

    if not data:
        return {}

    ratings = {}

    def consume(entries):
        for e in entries or []:
            team = e.get("team") or {}
            name = team.get("displayName") or team.get("shortDisplayName") or team.get("name")
            if not name:
                continue

            stats = {s.get("name"): s.get("value") for s in (e.get("stats") or [])}
            played = stats.get("gamesPlayed") or 0
            gf = stats.get("goalsFor")
            ga = stats.get("goalsAgainst")

            if gf is None:
                gf = stats.get("pointsFor")
            if ga is None:
                ga = stats.get("pointsAgainst")

            pts = stats.get("points") or 0

            if not played or gf is None or ga is None:
                continue

            ratings[name.strip().lower()] = {
                "name": name,
                "attack": gf / played,
                "defense": ga / played,
                "points_per_game": pts / played,
                "form": "",
            }

    children = data.get("children") or []
    if children:
        for child in children:
            consume((child.get("standings") or {}).get("entries"))
    else:
        consume((data.get("standings") or {}).get("entries"))

    return ratings