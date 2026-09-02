"""sync_odds.py — bookmaker odds <-> model edge sync (rebuilt Sept 2026).

WHY THE OLD VERSION NEVER WROTE A SINGLE INSIGHT (proven against the live API):

1. h2h outcomes are named by TEAM NAME plus "Draw" (e.g. "Liverpool", "Ipswich
   Town", "Draw"). The old code searched for outcomes literally named
   "home"/"away"/"draw" -> never true -> the whole 1X2/DC_1X section was dead.
2. In the totals market, the line sits on each OUTCOME ("point": 2.5), not on
   the market object. The old code checked market.get("point") == 2.5 ->
   always None -> the whole Over/Under section was dead too.

The sync ran every day anyway (~14 credits/day burned), fetched events, even
matched fixtures — then wrote nothing, forever. Hence market_edge = 0 and
is_value_bet = false on every pick since day one.

WHAT THIS VERSION FIXES / ADDS
- Reads the real API shapes above (verified against a live EPL payload).
- Totals: per bookmaker, only outcomes with point == 2.5, and a book only
  counts when it quotes BOTH Over and Under at that line (books mix lines —
  one offered 3.5 as its headline line in live testing).
- soccer_efl_champ added (the daily board is Championship-heavy).
- Quota guard: reads x-requests-remaining and stops early when the monthly
  free budget is nearly gone (free tier = 500 credits/month; each league call
  costs 2 credits here, so 8 leagues = 16 credits/day).
- Home/away outcome mapping uses the fuzzy team matcher with an order-based
  fallback ([home, away, Draw]).
- Skips absurd prices (< 1.01 or > 500) so one garbage book can never skew
  an average.
- DC_1X insight now stores the DERIVED FAIR price (1 / market probability)
  instead of mislabelling the home-win price as a double-chance price.
  (The Odds API has no double-chance market; the fair price derived from the
  margin-free 1X2 is the honest baseline. A real bookie's DC price carries
  its own margin, i.e. a HIGHER implied probability — so our 4% threshold
  against the fair line is, if anything, generous to ourselves. Noted for
  future tightening.)
- Also writes HOME_WIN and AWAY_WIN insights from the same h2h fetch — zero
  extra credits, value flags on the most common safe-board picks.

UNCHANGED ON PURPOSE: module name, match_and_save_odds(db) (automation.py
imports exactly that), the OddsInsight table shape, VALUE_THRESHOLD = 0.04,
env var ODDS_API_KEY.
"""

import os
import unicodedata
import requests
from difflib import SequenceMatcher
from sqlalchemy.orm import Session

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv absent on the server is fine
    pass

from app import SessionLocal
from sports_models import Fixture, Prediction, OddsInsight

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

# The Odds API requires specific league keys, not a generic "soccer" key
SOCCER_SPORTS_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_efl_champ",  # added: the board is Championship-heavy
]

# We only care if our model is at least 4% better than the bookmaker
VALUE_THRESHOLD = 0.04

# Stop spending when the monthly free budget (500 credits) is nearly gone
MIN_REMAINING_CREDITS = 35

# Sanity bounds for a usable decimal price
MIN_PRICE = 1.01
MAX_PRICE = 500.0

UPCOMING_STATUSES = ["NS", "TBD", "NOT_STARTED", "SCHEDULED", "TIMED"]

# Tokens that add no matching signal ("West Ham United FC" -> "west ham united")
NOISE_TOKENS = {"fc", "afc", "cf", "sc", "ac", "fk", "sk", "club", "cd", "calcio"}


# -----------------------------
# Name matching
# -----------------------------

def normalize_team(name: str) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    for ch in ".,'()-_/":
        text = text.replace(ch, " ")
    tokens = [t for t in text.split() if t and t not in NOISE_TOKENS]
    return " ".join(tokens)


def similar(a, b) -> bool:
    """Kept name for backwards compatibility; now normalizes, tolerates
    containment, and falls back to a TOKEN-SORTED ratio. The old raw 0.80
    ratio matched 'Manchester United' to 'Manchester City' (shared city word)
    — token-sorting kills that false positive while keeping real variants."""
    if not a or not b:
        return False
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if SequenceMatcher(None, na, nb).ratio() > 0.85:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 5 and shorter in longer:
        return True
    # "manchester united" vs "city manchester" sorts far apart -> 0.69 < 0.86
    sorted_a = " ".join(sorted(na.split()))
    sorted_b = " ".join(sorted(nb.split()))
    return SequenceMatcher(None, sorted_a, sorted_b).ratio() > 0.86


# -----------------------------
# Fetching
# -----------------------------

def _valid_price(price) -> bool:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False
    return MIN_PRICE <= p <= MAX_PRICE


def fetch_odds():
    """Fetch h2h + totals odds for the target leagues. Stops early when the
    monthly credit budget is nearly exhausted (protects the free tier).
    Returns (events, remaining_credits_or_None)."""
    if not ODDS_API_KEY:
        print("ODDS_API_KEY is missing.")
        return [], None

    all_events = []
    remaining = None

    for sport_key in SOCCER_SPORTS_KEYS:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
        }

        try:
            response = requests.get(url, params=params, timeout=30)
        except Exception as e:
            print(f"Failed to fetch odds for {sport_key}: {e}")
            continue

        last = response.headers.get("x-requests-last")
        rem = response.headers.get("x-requests-remaining")
        if rem is not None:
            try:
                remaining = int(rem)
            except ValueError:
                pass

        if response.status_code == 200:
            events = response.json()
            all_events.extend(events)
            print(f"Fetched {len(events)} events for {sport_key} "
                  f"(credits: -{last or '?'}, remaining {rem or '?'})")
        else:
            try:
                detail = response.json()
            except Exception:
                detail = response.text[:200]
            print(f"Odds API error for {sport_key} (HTTP {response.status_code}): {detail}")

        # Protect the monthly budget: stop fetching further leagues
        if remaining is not None and remaining <= MIN_REMAINING_CREDITS:
            print(f"Quota guard: only {remaining} credits left this month — "
              f"skipping remaining leagues.")
            break

    print(f"Total events fetched: {len(all_events)} | monthly credits remaining: {remaining}")
    return all_events, remaining


# -----------------------------
# Market extraction (real API shapes)
# -----------------------------

def extract_h2h_prices(bookmakers, home_team, away_team):
    """Return [[home_price, draw_price, away_price], ...] — one row per book.
    h2h outcomes are named by TEAM NAME plus 'Draw'; map by fuzzy name match,
    falling back to the documented [home, away, Draw] ordering."""
    rows = []
    for bookmaker in bookmakers or []:
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            named = {o.get("name"): o.get("price") for o in outcomes if o.get("name")}

            draw_price = next((named[n] for n in named if n.strip().lower() == "draw"), None)
            home_price = next((named[n] for n in named if n.strip().lower() != "draw" and similar(n, home_team)), None)
            away_price = next((named[n] for n in named if n.strip().lower() != "draw" and similar(n, away_team)), None)

            if home_price is None or away_price is None or draw_price is None:
                # Fallback: the API documents outcomes as [home, away, Draw]
                non_draw = [o.get("price") for o in outcomes if (o.get("name") or "").strip().lower() != "draw"]
                if len(non_draw) >= 2 and draw_price is None:
                    draw_price = next((o.get("price") for o in outcomes if (o.get("name") or "").strip().lower() == "draw"), None)
                if len(non_draw) >= 2:
                    home_price, away_price = home_price or non_draw[0], away_price or non_draw[1]

            if home_price and draw_price and away_price and all(
                _valid_price(p) for p in (home_price, draw_price, away_price)
            ):
                rows.append([float(home_price), float(draw_price), float(away_price)])
    return rows


def extract_totals_25(bookmakers):
    """Return [(over_price, under_price), ...] for the 2.5 line only.
    The line lives on each OUTCOME ('point': 2.5); a book counts only when it
    quotes BOTH sides at exactly that line (books mix 2.0/2.5/3.0/3.5...)."""
    rows = []
    for bookmaker in bookmakers or []:
        for market in bookmaker.get("markets", []):
            if market.get("key") != "totals":
                continue
            over_price = under_price = None
            for outcome in market.get("outcomes", []):
                if outcome.get("point") != 2.5:
                    continue
                name = (outcome.get("name") or "").strip().lower()
                if name == "over":
                    over_price = outcome.get("price")
                elif name == "under":
                    under_price = outcome.get("price")
            if over_price and under_price and _valid_price(over_price) and _valid_price(under_price):
                rows.append((float(over_price), float(under_price)))
    return rows


def calculate_true_prob(odds_list):
    """Removes the bookmaker's margin (overround) to find the true market probability"""
    if not odds_list or len(odds_list) < 2:
        return None

    implied = [1.0 / o for o in odds_list if o > 1.0]
    margin = sum(implied) - 1.0

    if margin <= 0:
        return None

    true_probs = [i / (1.0 + margin) for i in implied]
    return true_probs


# -----------------------------
# Insight writing
# -----------------------------

def _write_insight(db, fixture, market, bookmaker_odds, market_prob, model_prob):
    edge = model_prob - market_prob

    db.query(OddsInsight).filter(
        OddsInsight.fixture_id == fixture.id,
        OddsInsight.market == market,
    ).delete()

    db.add(OddsInsight(
        fixture_id=fixture.id,
        market=market,
        bookmaker_odds=bookmaker_odds,
        market_prob=market_prob,
        model_prob=model_prob,
        value_edge=edge,
        is_value_bet=(edge >= VALUE_THRESHOLD),
    ))
    return edge >= VALUE_THRESHOLD


def _model_prob(db, fixture_id, market):
    pred = db.query(Prediction).filter(
        Prediction.fixture_id == fixture_id,
        Prediction.market == market,
    ).first()
    return pred.probability if pred else None


def match_and_save_odds(db: Session):
    odds_events, remaining = fetch_odds()
    if not odds_events:
        print("No odds events to process.")
        return

    fixtures = db.query(Fixture).filter(Fixture.status.in_(UPCOMING_STATUSES)).all()
    value_bets_found = 0
    processed_fixtures = 0

    for fixture in fixtures:
        matched_event = None
        for event in odds_events:
            if similar(fixture.home_team, event.get("home_team", "")) and \
               similar(fixture.away_team, event.get("away_team", "")):
                matched_event = event
                break

        if not matched_event:
            continue

        processed_fixtures += 1
        bookmakers = matched_event.get("bookmakers", [])

        # --- 1X2 (h2h): DC_1X + HOME_WIN + AWAY_WIN insights ---
        h2h_rows = extract_h2h_prices(bookmakers, matched_event.get("home_team", ""), matched_event.get("away_team", ""))

        if h2h_rows:
            avg_home = sum(r[0] for r in h2h_rows) / len(h2h_rows)
            avg_draw = sum(r[1] for r in h2h_rows) / len(h2h_rows)
            avg_away = sum(r[2] for r in h2h_rows) / len(h2h_rows)

            true_probs = calculate_true_prob([avg_home, avg_draw, avg_away])
            if true_probs:
                market_home_prob, market_draw_prob, market_away_prob = true_probs

                targets = [
                    ("DC_1X", market_home_prob + market_draw_prob, None),  # bookmaker_odds filled below
                    ("HOME_WIN", market_home_prob, avg_home),
                    ("AWAY_WIN", market_away_prob, avg_away),
                ]
                for market, market_prob, price in targets:
                    model_prob = _model_prob(db, fixture.id, market)
                    if model_prob is None:
                        continue
                    if price is None:
                        price = round(1.0 / market_prob, 3) if market_prob > 0 else None  # derived fair price
                    if price is None:
                        continue
                    if _write_insight(db, fixture, market, price, market_prob, model_prob):
                        value_bets_found += 1

        # --- Over/Under 2.5 Goals ---
        totals_rows = extract_totals_25(bookmakers)

        if totals_rows:
            avg_over = sum(r[0] for r in totals_rows) / len(totals_rows)
            avg_under = sum(r[1] for r in totals_rows) / len(totals_rows)

            true_probs = calculate_true_prob([avg_over, avg_under])
            if true_probs:
                market_over_prob = true_probs[0]
                model_prob = _model_prob(db, fixture.id, "OVER_25")
                if model_prob is not None:
                    if _write_insight(db, fixture, "OVER_25", avg_over, market_over_prob, model_prob):
                        value_bets_found += 1

    db.commit()
    print(f"Odds sync complete. Matched {processed_fixtures} fixtures. "
          f"Found {value_bets_found} mathematical value bets. "
          f"Credits remaining this month: {remaining}")


if __name__ == "__main__":
    db = SessionLocal()
    match_and_save_odds(db)
    db.close()
