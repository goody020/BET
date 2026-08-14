import os
import requests
from dotenv import load_dotenv


load_dotenv()

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()

API_STANDINGS_URL = "https://v3.football.api-sports.io/standings"

HEADERS = {
    "x-apisports-key": API_KEY,
}

LEAGUES = [
    (39, "Premier League"),
    (140, "La Liga"),
    (135, "Serie A"),
]

SEASONS = [None, 2024, 2025, 2026]


def test_standings(league_id, league_name, season):
    params = {
        "league": league_id,
    }

    if season:
        params["season"] = season

    print("\nTesting:")
    print(params)

    try:
        response = requests.get(
            API_STANDINGS_URL,
            headers=HEADERS,
            params=params,
            timeout=30,
        )
        data = response.json()
    except Exception as e:
        print("Request failed:", e)
        return

    print("HTTP status:", response.status_code)
    print("API status:", data.get("status"))

    if data.get("errors"):
        print("Errors:", data.get("errors"))

    api_response = data.get("response") or []

    print("Response length:", len(api_response))

    if not api_response:
        return

    league_data = api_response[0] or {}
    standings_groups = league_data.get("league", {}).get("standings", [])

    team_count = 0

    for group in standings_groups:
        team_count += len(group)

    print("Teams found:", team_count)

    if standings_groups and standings_groups[0]:
        first_team = standings_groups[0][0]
        print("Sample team:", first_team.get("team", {}).get("name"))


def main():
    if not API_KEY:
        print("ERROR: API_FOOTBALL_KEY missing in .env")
        return

    for league_id, league_name in LEAGUES:
        print("=" * 40)
        print(f"League: {league_name} | ID: {league_id}")

        for season in SEASONS:
            test_standings(league_id, league_name, season)


if __name__ == "__main__":
    main()